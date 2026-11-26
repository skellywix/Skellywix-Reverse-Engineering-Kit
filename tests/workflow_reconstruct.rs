#![cfg(unix)]

use std::collections::HashMap;
use std::error::Error;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use axum::Router;
use axum::body::{Body, Bytes};
use axum::http::{Request, Response, StatusCode, header};
use http_body_util::BodyExt;
use re_kit_workspace::adapters::FourmilabAdapter;
use re_kit_workspace::api::{AppConfig, AppState, router};
use re_kit_workspace::events::EventReplay;
use re_kit_workspace::store::Store;
use re_kit_workspace::workflows::reconstruct;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixListener;
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tower::ServiceExt;

static NEXT_APP: AtomicU64 = AtomicU64::new(1);
type TestResult<T = ()> = Result<T, Box<dyn Error + Send + Sync>>;

const VERIFIED_STATE: &[u8] = include_bytes!("fixtures/workflows/reconstruct/state_verified.json");
const UNVERIFIED_STATE: &[u8] =
    include_bytes!("fixtures/workflows/reconstruct/state_unverified.json");
const ACTION_RECEIPT: &[u8] = include_bytes!("fixtures/workflows/reconstruct/action.json");
const VERIFIED_RUN: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PREVIOUS_RELEASE: &str = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
const ACTIVE_RELEASE: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";

struct TestRoot {
    path: PathBuf,
}
impl TestRoot {
    fn new(label: &str) -> std::io::Result<Self> {
        let suffix = NEXT_APP.fetch_add(1, Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("re-kit-{label}-{}-{suffix}", std::process::id()));
        std::fs::create_dir_all(&path)?;
        Ok(Self { path })
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_dir_all(&self.path)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!(
                "failed to remove reconstruct test root {}: {error}",
                self.path.display()
            );
        }
    }
}

struct TestApp {
    app: Router,
    root: TestRoot,
    _shutdown: watch::Sender<bool>,
}

impl TestApp {
    async fn new(socket_path: PathBuf) -> TestResult<Self> {
        let root = TestRoot::new("reconstruct")?;
        let static_dir = root.path.join("static");
        std::fs::create_dir_all(&static_dir)?;
        std::fs::write(static_dir.join("index.html"), "<!doctype html>")?;
        let store = Store::open(root.path.join("workspace.sqlite3")).await?;
        let (shutdown, shutdown_receiver) = watch::channel(false);
        let state = AppState::with_engine_sockets(
            store,
            Arc::new(AppConfig {
                static_dir,
                allow_missing_origin: true,
            }),
            shutdown_receiver,
            Some(socket_path),
            None,
        );
        Ok(Self {
            app: router(state),
            _shutdown: shutdown,
            root,
        })
    }
}

#[derive(Debug)]
struct CapturedRequest {
    method: String,
    target: String,
    headers: HashMap<String, String>,
    body: Vec<u8>,
}

struct FakeResponse {
    status: StatusCode,
    content_type: &'static str,
    body: Vec<u8>,
}

impl FakeResponse {
    fn json(body: &[u8]) -> Self {
        Self {
            status: StatusCode::OK,
            content_type: "application/json",
            body: body.to_vec(),
        }
    }

    fn package(body: &[u8]) -> Self {
        Self {
            status: StatusCode::OK,
            content_type: "application/zip",
            body: body.to_vec(),
        }
    }
}

struct FakeEngine {
    path: PathBuf,
    task: JoinHandle<TestResult<Vec<CapturedRequest>>>,
}

impl FakeEngine {
    async fn start(root: &Path, responses: Vec<FakeResponse>) -> TestResult<Self> {
        std::fs::create_dir_all(root)?;
        let path = root.join("fourmilab.sock");
        let listener = UnixListener::bind(&path)?;
        let task = tokio::spawn(async move {
            let mut captured = Vec::with_capacity(responses.len());
            for response in responses {
                let (mut stream, _) = listener.accept().await?;
                captured.push(read_request(&mut stream).await?);
                write_response(&mut stream, response).await?;
            }
            Ok(captured)
        });
        Ok(Self { path, task })
    }

    async fn finish(self) -> TestResult<Vec<CapturedRequest>> {
        let captured = self.task.await??;
        Ok(captured)
    }
}

async fn read_request(stream: &mut tokio::net::UnixStream) -> TestResult<CapturedRequest> {
    let mut wire = Vec::new();
    let mut buffer = [0_u8; 8192];
    let header_end = loop {
        let count = stream.read(&mut buffer).await?;
        if count == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "legacy request ended before its headers",
            )
            .into());
        }
        wire.extend_from_slice(&buffer[..count]);
        if let Some(position) = wire.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
    };
    let head = std::str::from_utf8(&wire[..header_end])?;
    let mut lines = head.split("\r\n");
    let request_line = lines
        .next()
        .ok_or_else(|| std::io::Error::other("missing request line"))?;
    let mut request_parts = request_line.split_whitespace();
    let method = request_parts
        .next()
        .ok_or_else(|| std::io::Error::other("missing method"))?
        .to_owned();
    let target = request_parts
        .next()
        .ok_or_else(|| std::io::Error::other("missing target"))?
        .to_owned();
    let mut headers = HashMap::new();
    for line in lines.filter(|line| !line.is_empty()) {
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| std::io::Error::other("invalid request header"))?;
        headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_owned());
    }
    let content_length = headers
        .get("content-length")
        .map(|value| value.parse::<usize>())
        .transpose()?
        .unwrap_or_default();
    while wire.len() - header_end < content_length {
        let count = stream.read(&mut buffer).await?;
        if count == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "legacy request ended before its body",
            )
            .into());
        }
        wire.extend_from_slice(&buffer[..count]);
    }
    Ok(CapturedRequest {
        method,
        target,
        headers,
        body: wire[header_end..header_end + content_length].to_vec(),
    })
}

async fn write_response(stream: &mut tokio::net::UnixStream, response: FakeResponse) -> TestResult {
    let reason = response.status.canonical_reason().unwrap_or("Unknown");
    let head = format!(
        "HTTP/1.1 {} {reason}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        response.status.as_u16(),
        response.content_type,
        response.body.len()
    );
    stream.write_all(head.as_bytes()).await?;
    stream.write_all(&response.body).await?;
    stream.shutdown().await?;
    Ok(())
}

fn get_request(uri: &str) -> TestResult<Request<Body>> {
    Ok(Request::builder()
        .method("GET")
        .uri(uri)
        .body(Body::empty())?)
}

fn post_json(uri: &str, body: Value) -> TestResult<Request<Body>> {
    Ok(Request::builder()
        .method("POST")
        .uri(uri)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(serde_json::to_vec(&body)?))?)
}

async fn response_bytes(response: Response<Body>) -> TestResult<Bytes> {
    Ok(response.into_body().collect().await?.to_bytes())
}

async fn response_json(response: Response<Body>) -> TestResult<Value> {
    Ok(serde_json::from_slice(&response_bytes(response).await?)?)
}

#[tokio::test]
async fn state_projects_fixed_proof_model_evidence_and_verified_package() -> TestResult {
    let fake_root = TestRoot::new("reconstruct-fake-state")?;
    let engine =
        FakeEngine::start(&fake_root.path, vec![FakeResponse::json(VERIFIED_STATE)]).await?;
    let test_app = TestApp::new(engine.path.clone()).await?;

    let response = test_app
        .app
        .oneshot(get_request(&format!(
            "/api/v1/reconstruct/state?run={VERIFIED_RUN}"
        ))?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let body = response_json(response).await?;
    assert_eq!(body["schema_version"], 1);
    assert_eq!(body["engine"]["state"], "up");
    assert_eq!(body["profile"]["required_cases"], 2557);
    assert_eq!(body["selected_run"]["proof"]["required"], 2557);
    assert_eq!(body["selected_run"]["proof"]["matched"], 2557);
    assert_eq!(body["selected_run"]["proof"]["verified"], true);
    assert_eq!(
        body["selected_run"]["model"]["requested"],
        "openai-codex/gpt-6.2-astra"
    );
    assert_eq!(
        body["selected_run"]["model"]["actual"],
        "openai-codex/gpt-6.2-astra"
    );
    assert_eq!(
        body["selected_run"]["stages"].as_array().map(Vec::len),
        Some(5)
    );
    let analyze = body["selected_run"]["stages"]
        .as_array()
        .and_then(|stages| stages.iter().find(|stage| stage["name"] == "analyze"))
        .ok_or_else(|| std::io::Error::other("analyze stage must be projected"))?;
    assert_eq!(analyze["completed"], 2557);
    assert_eq!(analyze["required"], 2557);
    assert_eq!(body["selected_run"]["package"]["available"], true);
    assert_eq!(body["legacy_evidence"]["run"]["id"], VERIFIED_RUN);

    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 1);
    assert_eq!(captured[0].method, "GET");
    assert_eq!(captured[0].target, format!("/state?run={VERIFIED_RUN}"));
    Ok(())
}

#[tokio::test]
async fn polling_persists_raw_evidence_and_suppresses_identical_events() -> TestResult {
    let fake_root = TestRoot::new("reconstruct-fake-poll")?;
    let engine = FakeEngine::start(
        &fake_root.path,
        vec![
            FakeResponse::json(VERIFIED_STATE),
            FakeResponse::json(VERIFIED_STATE),
        ],
    )
    .await?;
    let store = Store::open(fake_root.path.join("poll.sqlite3")).await?;
    let adapter = FourmilabAdapter::new(&engine.path);

    reconstruct::poll_once(&adapter, &store).await;
    reconstruct::poll_once(&adapter, &store).await;

    let snapshot = store.snapshot().await?;
    assert_eq!(
        snapshot.engines.fourmilab.state,
        re_kit_workspace::domain::EngineAvailability::Up
    );
    let EventReplay::Events { events, .. } = store.replay_events(0).await? else {
        panic!("fresh poll store unexpectedly reported a retention gap");
    };
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].event_type, "engine.state_changed");
    assert_eq!(events[0].payload["engine"], "fourmilab");
    let Some(projection) = store
        .legacy_projection(re_kit_workspace::store::LegacyEngine::Fourmilab)
        .await?
    else {
        return Err(std::io::Error::other("poll must retain its projection").into());
    };
    assert_eq!(
        projection
            .evidence
            .as_ref()
            .and_then(|evidence| evidence.pointer("/run/id"))
            .and_then(Value::as_str),
        Some(VERIFIED_RUN)
    );

    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 2);
    assert!(
        captured
            .iter()
            .all(|request| request.method == "GET" && request.target == "/state")
    );
    Ok(())
}
#[tokio::test]
async fn actions_map_to_legacy_semantics_and_rollback_checks_exact_target() -> TestResult {
    let fake_root = TestRoot::new("reconstruct-fake-actions")?;
    let mut responses = (0..5)
        .map(|_| FakeResponse::json(ACTION_RECEIPT))
        .collect::<Vec<_>>();
    responses.extend([
        FakeResponse::json(VERIFIED_STATE),
        FakeResponse::json(VERIFIED_STATE),
        FakeResponse::json(ACTION_RECEIPT),
    ]);
    let engine = FakeEngine::start(&fake_root.path, responses).await?;
    let test_app = TestApp::new(engine.path.clone()).await?;
    let app = test_app.app;

    let actions = vec![
        (String::from("/api/v1/reconstruct/start"), json!({})),
        (
            format!("/api/v1/reconstruct/runs/{VERIFIED_RUN}/cancel"),
            json!({}),
        ),
        (
            format!("/api/v1/reconstruct/runs/{VERIFIED_RUN}/retry"),
            json!({}),
        ),
        (
            String::from("/api/v1/reconstruct/releases/stage"),
            json!({}),
        ),
        (
            String::from("/api/v1/reconstruct/releases/promote"),
            json!({"target_digest":ACTIVE_RELEASE,"confirm_target":ACTIVE_RELEASE}),
        ),
    ];
    for (uri, body) in actions {
        let response = app.clone().oneshot(post_json(&uri, body)?).await?;
        assert_eq!(response.status(), StatusCode::OK, "{uri}");
    }

    let wrong_target = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
    let response = app
        .clone()
        .oneshot(post_json(
            "/api/v1/reconstruct/releases/rollback",
            json!({"target_digest":wrong_target,"confirm_target":wrong_target}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::CONFLICT);
    assert_eq!(
        response_json(response).await?["error"],
        "illegal_transition"
    );

    let response = app
        .oneshot(post_json(
            "/api/v1/reconstruct/releases/rollback",
            json!({"target_digest":PREVIOUS_RELEASE,"confirm_target":PREVIOUS_RELEASE}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);

    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 8);
    let action_bodies = captured
        .iter()
        .filter(|request| request.target == "/action")
        .map(|request| serde_json::from_slice::<Value>(&request.body))
        .collect::<Result<Vec<_>, _>>()?;
    assert_eq!(
        action_bodies
            .iter()
            .map(|body| body["action"].as_str().unwrap_or_default())
            .collect::<Vec<_>>(),
        vec!["start", "cancel", "retry", "stage", "promote", "rollback"]
    );
    assert_eq!(action_bodies[0], json!({"action":"start"}));
    assert_eq!(action_bodies[3], json!({"action":"stage"}));
    assert_eq!(action_bodies[4]["digest"], ACTIVE_RELEASE);
    assert_eq!(action_bodies[5], json!({"action":"rollback"}));
    assert!(action_bodies[5].get("target").is_none());
    assert_eq!(
        captured
            .iter()
            .filter(|request| request.target == "/state")
            .count(),
        2
    );
    assert!(captured.iter().all(|request| {
        request.headers.get("host").map(String::as_str) == Some("127.0.0.1:48784")
    }));
    Ok(())
}

#[tokio::test]
async fn package_streams_only_after_verified_receipt() -> TestResult {
    const PACKAGE: &[u8] = b"PK\x03\x04verified-fourmilab-package";
    let verified_root = TestRoot::new("reconstruct-fake-package")?;
    let verified_engine = FakeEngine::start(
        &verified_root.path,
        vec![
            FakeResponse::json(VERIFIED_STATE),
            FakeResponse::package(PACKAGE),
        ],
    )
    .await?;
    let verified_app = TestApp::new(verified_engine.path.clone()).await?;
    let response = verified_app
        .app
        .oneshot(get_request(&format!(
            "/api/v1/reconstruct/runs/{VERIFIED_RUN}/package"
        ))?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers().get(header::CONTENT_TYPE),
        Some(&header::HeaderValue::from_static("application/zip"))
    );
    assert_eq!(
        response
            .headers()
            .get(header::CONTENT_DISPOSITION)
            .and_then(|value| value.to_str().ok()),
        Some("attachment; filename=\"fourmilab-base64-verified.zip\"")
    );
    assert_eq!(response_bytes(response).await?.as_ref(), PACKAGE);
    let captured = verified_engine.finish().await?;
    assert_eq!(captured.len(), 2);
    assert_eq!(captured[0].target, format!("/state?run={VERIFIED_RUN}"));
    assert_eq!(captured[1].target, format!("/package/{VERIFIED_RUN}"));

    let unverified_root = TestRoot::new("reconstruct-fake-unverified")?;
    let unverified_engine = FakeEngine::start(
        &unverified_root.path,
        vec![FakeResponse::json(UNVERIFIED_STATE)],
    )
    .await?;
    let unverified_app = TestApp::new(unverified_engine.path.clone()).await?;
    let response = unverified_app
        .app
        .oneshot(get_request(
            "/api/v1/reconstruct/runs/cccccccccccccccccccccccccccccccc/package",
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::CONFLICT);
    assert_eq!(
        response_json(response).await?["error"],
        "illegal_transition"
    );
    let captured = unverified_engine.finish().await?;
    assert_eq!(
        captured.len(),
        1,
        "unverified package must not be requested"
    );
    assert!(captured[0].target.starts_with("/state?run="));
    Ok(())
}

#[tokio::test]
async fn unreachable_engine_is_projected_as_down_instead_of_empty_success() -> TestResult {
    let missing_root = TestRoot::new("reconstruct-missing")?;
    let missing_socket = missing_root.path.join("missing-fourmilab.sock");
    let test_app = TestApp::new(missing_socket).await?;

    let response = test_app
        .app
        .oneshot(get_request("/api/v1/reconstruct/state")?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let body = response_json(response).await?;
    assert_eq!(body["engine"]["state"], "down");
    assert!(
        body["engine"]["detail"]
            .as_str()
            .is_some_and(|detail| !detail.is_empty())
    );
    assert!(body["legacy_evidence"].is_null());
    assert!(body["runs"].is_null());
    assert!(body["selected_run"].is_null());
    Ok(())
}

#[tokio::test]
async fn workflow_uses_legacy_http_socket_without_writing_legacy_sqlite() -> TestResult {
    let fake_root = TestRoot::new("reconstruct-fake-read-only")?;
    let engine =
        FakeEngine::start(&fake_root.path, vec![FakeResponse::json(VERIFIED_STATE)]).await?;
    let legacy_database = fake_root.path.join("desk.sqlite3");
    let original = b"legacy-database-sentinel";
    std::fs::write(&legacy_database, original)?;
    let mut permissions = std::fs::metadata(&legacy_database)?.permissions();
    permissions.set_readonly(true);
    std::fs::set_permissions(&legacy_database, permissions)?;

    let test_app = TestApp::new(engine.path.clone()).await?;
    let response = test_app
        .app
        .oneshot(get_request("/api/v1/reconstruct/state")?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(std::fs::read(&legacy_database)?, original);
    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 1);
    assert_eq!(captured[0].method, "GET");
    assert_eq!(captured[0].target, "/state");
    assert!(test_app.root.path.join("workspace.sqlite3").exists());
    Ok(())
}
