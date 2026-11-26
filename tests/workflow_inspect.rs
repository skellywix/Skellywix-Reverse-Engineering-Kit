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
use re_kit_workspace::adapters::InspectorAdapter;
use re_kit_workspace::api::{AppConfig, AppState, router};
use re_kit_workspace::store::Store;
use re_kit_workspace::workflows::inspect;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixListener;
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tower::ServiceExt;

static NEXT_APP: AtomicU64 = AtomicU64::new(1);
type TestResult<T = ()> = Result<T, Box<dyn Error + Send + Sync>>;

const MAX_SAMPLE_BYTES: usize = 16 * 1024 * 1024;
const STATE_EMPTY: &[u8] = include_bytes!("fixtures/workflows/inspect/state_empty.json");
const STATE_A: &[u8] = include_bytes!("fixtures/workflows/inspect/state_a.json");
const STATE_AB: &[u8] = include_bytes!("fixtures/workflows/inspect/state_ab.json");
const RUN_IDENTIFY: &[u8] = include_bytes!("fixtures/workflows/inspect/run_identify.json");
const RUN_COMPARE: &[u8] = include_bytes!("fixtures/workflows/inspect/run_compare.json");
const RUN_HASH: &[u8] = include_bytes!("fixtures/workflows/inspect/run_hash.json");
const REPORT: &[u8] = include_bytes!("fixtures/workflows/inspect/report.json");
const STATE_REVIEWED: &[u8] = include_bytes!("fixtures/workflows/inspect/state_reviewed.json");
const CRITIC_STATUS: &[u8] = include_bytes!("fixtures/workflows/inspect/critic_status.json");
const CRITIC_READY: &[u8] = include_bytes!("fixtures/workflows/inspect/critic_ready.json");
const CRITIC_FAILED: &[u8] = include_bytes!("fixtures/workflows/inspect/critic_failed.json");
const CRITIC_CLAIM: &[u8] = include_bytes!("fixtures/workflows/inspect/critic_claim.json");
const PAIR: &[u8] = include_bytes!("fixtures/workflows/inspect/pair.json");
const STALE: &[u8] = include_bytes!("fixtures/workflows/inspect/stale.json");
const A_BEFORE_B: &[u8] = include_bytes!("fixtures/workflows/inspect/a_before_b.json");

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
                "failed to remove inspect test root {}: {error}",
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
        let root = TestRoot::new("inspect")?;
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
            None,
            Some(socket_path),
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

    fn with_status(status: StatusCode, body: &[u8]) -> Self {
        Self {
            status,
            content_type: "application/json",
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
        let path = root.join("inspector.sock");
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
        self.task.await?
    }
}

async fn read_request(stream: &mut tokio::net::UnixStream) -> TestResult<CapturedRequest> {
    let mut wire = Vec::new();
    let mut buffer = [0_u8; 16 * 1024];
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

fn upload_request(slot: &str, revision: u64, bytes: Vec<u8>) -> TestResult<Request<Body>> {
    let content_length = bytes.len().to_string();
    Ok(Request::builder()
        .method("POST")
        .uri(format!("/api/v1/inspect/samples/{slot}"))
        .header(header::CONTENT_TYPE, "application/octet-stream")
        .header(header::CONTENT_LENGTH, content_length)
        .header("X-Workspace-Revision", revision.to_string())
        .body(Body::from(bytes))?)
}

async fn response_bytes(response: Response<Body>) -> TestResult<Bytes> {
    Ok(response.into_body().collect().await?.to_bytes())
}

async fn response_json(response: Response<Body>) -> TestResult<Value> {
    Ok(serde_json::from_slice(&response_bytes(response).await?)?)
}

#[tokio::test]
async fn state_projects_engine_and_case() -> TestResult {
    let fake_root = TestRoot::new("inspect-fake-state")?;
    let engine = FakeEngine::start(&fake_root.path, vec![FakeResponse::json(STATE_AB)]).await?;
    let test_app = TestApp::new(engine.path.clone()).await?;

    let response = test_app
        .app
        .oneshot(get_request("/api/v1/inspect/state")?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let body = response_json(response).await?;
    assert_eq!(body["schema_version"], 1);
    assert_eq!(body["engine"]["state"], "up");
    assert_eq!(body["state"]["revision"], 2);
    assert_eq!(body["state"]["slots"]["a"]["size"], 3);
    assert_eq!(body["state"]["slots"]["b"]["size"], 0);
    assert_eq!(body["legacy_evidence"]["revision"], 2);
    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 1);
    assert_eq!(captured[0].method, "GET");
    assert_eq!(captured[0].target, "/api/state");
    Ok(())
}

#[tokio::test]
async fn upload_enforces_cap_order_and_replacement_semantics() -> TestResult {
    let fake_root = TestRoot::new("inspect-fake-upload")?;
    let engine = FakeEngine::start(
        &fake_root.path,
        vec![
            FakeResponse::json(STATE_A),
            FakeResponse::with_status(StatusCode::BAD_REQUEST, A_BEFORE_B),
            FakeResponse::json(STATE_A),
        ],
    )
    .await?;
    let test_app = TestApp::new(engine.path.clone()).await?;
    let app = test_app.app;

    let boundary = vec![b'a'; MAX_SAMPLE_BYTES];
    let response = app
        .clone()
        .oneshot(upload_request("a", 0, boundary)?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);

    let response = app
        .clone()
        .oneshot(upload_request("b", 0, Vec::new())?)
        .await?;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(response_json(response).await?["error"], "a_before_b");

    let response = app
        .clone()
        .oneshot(upload_request("a", 2, b"replacement-a".to_vec())?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let body = response_json(response).await?;
    assert!(body["slots"]["a"].is_object());
    assert!(body["slots"]["b"].is_null());

    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 3);
    assert_eq!(captured[0].body.len(), MAX_SAMPLE_BYTES);
    assert_eq!(
        captured[0]
            .headers
            .get("x-yuzu-revision")
            .map(String::as_str),
        Some("0")
    );
    assert_eq!(captured[1].target, "/api/sample/b");
    assert_eq!(captured[2].body, b"replacement-a");

    let missing_root = TestRoot::new("inspect-missing-over-limit")?;
    let over_limit_app = TestApp::new(missing_root.path.join("missing.sock")).await?;
    let response = over_limit_app
        .app
        .oneshot(upload_request("a", 0, vec![0; MAX_SAMPLE_BYTES + 1])?)
        .await?;
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(response_json(response).await?["error"], "sample_too_large");
    Ok(())
}

#[tokio::test]
async fn run_maps_file_id_build_diff_and_hash_check() -> TestResult {
    let fake_root = TestRoot::new("inspect-fake-run")?;
    let engine = FakeEngine::start(
        &fake_root.path,
        vec![
            FakeResponse::json(RUN_IDENTIFY),
            FakeResponse::json(RUN_COMPARE),
            FakeResponse::json(RUN_HASH),
        ],
    )
    .await?;
    let test_app = TestApp::new(engine.path.clone()).await?;
    let app = test_app.app;
    let requests = [
        json!({"revision":1,"tool":"identify_file","arguments":{"slot":"a"}}),
        json!({"revision":2,"tool":"compare_builds","arguments":{}}),
        json!({"revision":1,"tool":"verify_checksum","arguments":{"slot":"a","sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}}),
    ];
    for body in requests {
        let response = app
            .clone()
            .oneshot(post_json("/api/v1/inspect/run", body)?)
            .await?;
        assert_eq!(response.status(), StatusCode::OK);
        assert!(response_json(response).await?["result"].is_object());
    }

    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 3);
    let bodies = captured
        .iter()
        .map(|request| serde_json::from_slice::<Value>(&request.body))
        .collect::<Result<Vec<_>, _>>()?;
    assert_eq!(bodies[0]["tool"], "identify_file");
    assert_eq!(bodies[1]["tool"], "compare_builds");
    assert_eq!(bodies[2]["tool"], "verify_checksum");
    assert!(captured.iter().all(|request| request.target == "/api/run"));
    Ok(())
}

#[tokio::test]
async fn stale_revision_is_passthrough_409_with_current_revision() -> TestResult {
    let fake_root = TestRoot::new("inspect-fake-stale")?;
    let engine = FakeEngine::start(
        &fake_root.path,
        vec![
            FakeResponse::with_status(StatusCode::CONFLICT, STALE),
            FakeResponse::json(STATE_A),
        ],
    )
    .await?;
    let test_app = TestApp::new(engine.path.clone()).await?;
    let response = test_app
        .app
        .oneshot(post_json(
            "/api/v1/inspect/run",
            json!({"revision":0,"tool":"identify_file","arguments":{"slot":"a"}}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::CONFLICT);
    let body = response_json(response).await?;
    assert_eq!(body["error"], "stale_revision");
    assert_eq!(body["current_revision"], 1);
    assert_eq!(
        body["message"],
        "The files changed. Refresh the case and try again."
    );
    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 2);
    assert_eq!(captured[0].target, "/api/run");
    assert_eq!(captured[1].target, "/api/state");
    Ok(())
}

#[tokio::test]
async fn reset_report_review_critic_and_case_key_endpoints_map_exactly() -> TestResult {
    let fake_root = TestRoot::new("inspect-fake-operations")?;
    let engine = FakeEngine::start(
        &fake_root.path,
        vec![
            FakeResponse::json(STATE_EMPTY),
            FakeResponse::json(REPORT),
            FakeResponse::json(STATE_REVIEWED),
            FakeResponse::json(CRITIC_STATUS),
            FakeResponse::json(CRITIC_CLAIM),
            FakeResponse::json(CRITIC_READY),
            FakeResponse::json(CRITIC_STATUS),
            FakeResponse::json(PAIR),
            FakeResponse::json(STATE_A),
        ],
    )
    .await?;
    let test_app = TestApp::new(engine.path.clone()).await?;
    let app = test_app.app;

    let response = app
        .clone()
        .oneshot(post_json("/api/v1/inspect/reset", json!({"revision":2}))?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response_json(response).await?["revision"], 0);

    let response = app
        .clone()
        .oneshot(get_request("/api/v1/inspect/report")?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response
            .headers()
            .get(header::CONTENT_DISPOSITION)
            .and_then(|value| value.to_str().ok()),
        Some("attachment; filename=\"skellywix-re-kit-evidence.json\"")
    );
    assert_eq!(response_json(response).await?["revision"], 1);

    let response = app
        .clone()
        .oneshot(post_json(
            "/api/v1/inspect/review",
            json!({"revision":1,"reference":null,"critique":true}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response_json(response).await?["advisor"]["critique"]["status"],
        "pending"
    );

    let response = app
        .clone()
        .oneshot(get_request("/api/v1/inspect/critic")?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response_json(response).await?["status"], "pending");

    let response = app
        .clone()
        .oneshot(post_json(
            "/api/v1/inspect/critic/claim",
            json!({"revision":1}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response_json(response).await?["ticket"],
        "private-critic-ticket"
    );

    let response = app
        .clone()
        .oneshot(post_json(
            "/api/v1/inspect/critic/finish",
            json!({"revision":1,"ticket":"private-critic-ticket","note":{"text":"Evidence is consistent.","cites":[1]}}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response_json(response).await?["status"], "ready");

    let response = app
        .clone()
        .oneshot(post_json(
            "/api/v1/inspect/critic/cancel",
            json!({"revision":1,"end":1}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);

    let response = app
        .clone()
        .oneshot(post_json("/api/v1/inspect/pair", json!({"revision":1}))?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response_json(response).await?["token"],
        "private-case-token"
    );

    let response = app
        .oneshot(post_json(
            "/api/v1/inspect/disconnect",
            json!({"revision":1}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);

    let captured = engine.finish().await?;
    assert_eq!(
        captured
            .iter()
            .map(|request| request.target.as_str())
            .collect::<Vec<_>>(),
        vec![
            "/api/reset",
            "/api/report",
            "/api/review",
            "/api/critic",
            "/api/critic/claim",
            "/api/critic/finish",
            "/api/critic/cancel",
            "/api/pair",
            "/api/disconnect"
        ]
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&captured[5].body)?,
        json!({
            "ticket":"private-critic-ticket",
            "note":{"text":"Evidence is consistent.","cites":[1]}
        })
    );
    assert_eq!(
        captured[0]
            .headers
            .get("x-yuzu-revision")
            .map(String::as_str),
        Some("2")
    );
    Ok(())
}

#[tokio::test]
async fn critic_finish_forwards_an_explicit_null_note() -> TestResult {
    let fake_root = TestRoot::new("inspect-fake-critic-failure")?;
    let engine =
        FakeEngine::start(&fake_root.path, vec![FakeResponse::json(CRITIC_FAILED)]).await?;
    let test_app = TestApp::new(engine.path.clone()).await?;

    let response = test_app
        .app
        .oneshot(post_json(
            "/api/v1/inspect/critic/finish",
            json!({"revision":1,"ticket":"private-critic-ticket","note":null}),
        )?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response_json(response).await?["status"], "failed");

    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 1);
    assert_eq!(captured[0].target, "/api/critic/finish");
    assert_eq!(
        serde_json::from_slice::<Value>(&captured[0].body)?,
        json!({"ticket":"private-critic-ticket","note":null})
    );
    Ok(())
}

#[tokio::test]
async fn critic_finish_rejects_invalid_notes_before_contacting_the_inspector() -> TestResult {
    let root = TestRoot::new("inspect-critic-closed-note")?;
    let app = TestApp::new(root.path.join("absent.sock")).await?;
    for note in [
        json!({"text":"Check recorded.","cites":[1],"extra":"unrelated"}),
        json!({"text":7,"cites":[1]}),
        json!({"text":"Check recorded."}),
        json!({"text":"Check recorded.","cites":["1"]}),
        json!({"text":"   ","cites":[1]}),
        json!({"text":"a".repeat(321),"cites":[1]}),
        json!({"text":"Check recorded.","cites":[]}),
        json!({"text":"Check recorded.","cites":[1,2,3,4]}),
        json!({"text":"Check recorded.","cites":[0]}),
        json!({"text":"Check recorded.","cites":[1,1]}),
    ] {
        let response = app
            .app
            .clone()
            .oneshot(post_json(
                "/api/v1/inspect/critic/finish",
                json!({"revision":1,"ticket":"fixture-ticket","note":note}),
            )?)
            .await?;
        assert_eq!(response.status(), StatusCode::BAD_REQUEST, "note: {note}");
    }
    Ok(())
}

#[tokio::test]
async fn sample_bytes_are_not_persisted_and_legacy_database_is_untouched() -> TestResult {
    const SAMPLE: &[u8] = b"private-specimen-marker-never-persist";
    let fake_root = TestRoot::new("inspect-fake-no-write")?;
    let engine = FakeEngine::start(&fake_root.path, vec![FakeResponse::json(STATE_A)]).await?;
    let legacy_database = fake_root.path.join("inspector.sqlite3");
    let original = b"legacy-inspector-database-sentinel";
    std::fs::write(&legacy_database, original)?;
    let mut permissions = std::fs::metadata(&legacy_database)?.permissions();
    permissions.set_readonly(true);
    std::fs::set_permissions(&legacy_database, permissions)?;
    let test_app = TestApp::new(engine.path.clone()).await?;

    let response = test_app
        .app
        .oneshot(upload_request("a", 0, SAMPLE.to_vec())?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(std::fs::read(&legacy_database)?, original);
    for path in regular_files_under(&test_app.root.path)? {
        let bytes = std::fs::read(&path)?;
        assert!(
            !bytes.windows(SAMPLE.len()).any(|window| window == SAMPLE),
            "sample persisted in {}",
            path.display()
        );
    }
    let captured = engine.finish().await?;
    assert_eq!(captured.len(), 1);
    assert_eq!(captured[0].target, "/api/sample/a");
    assert_eq!(captured[0].body, SAMPLE);
    Ok(())
}

#[tokio::test]
async fn unreachable_engine_is_projected_as_down_instead_of_empty_case() -> TestResult {
    let missing_root = TestRoot::new("inspect-missing")?;
    let test_app = TestApp::new(missing_root.path.join("missing-inspector.sock")).await?;
    let response = test_app
        .app
        .oneshot(get_request("/api/v1/inspect/state")?)
        .await?;
    assert_eq!(response.status(), StatusCode::OK);
    let body = response_json(response).await?;
    assert_eq!(body["engine"]["state"], "down");
    assert!(
        body["engine"]["detail"]
            .as_str()
            .is_some_and(|detail| !detail.is_empty())
    );
    assert!(body["state"].is_null());
    assert!(body["legacy_evidence"].is_null());
    Ok(())
}

#[tokio::test]
async fn polling_records_unreachable_inspector_as_down() -> TestResult {
    let root = TestRoot::new("inspect-poll-down")?;
    let store = Store::open(root.path.join("poll.sqlite3")).await?;
    let adapter = InspectorAdapter::new(
        root.path.join("missing-desk.sock"),
        root.path.join("missing-agent.sock"),
    );

    inspect::poll_once(&adapter, &store).await;

    let snapshot = store.snapshot().await?;
    assert_eq!(
        snapshot.engines.inspector.state,
        re_kit_workspace::domain::EngineAvailability::Down
    );
    assert_eq!(
        snapshot.engines.inspector.detail,
        "legacy engine unreachable"
    );
    let Some(projection) = store
        .legacy_projection(re_kit_workspace::store::LegacyEngine::Inspector)
        .await?
    else {
        return Err(std::io::Error::other("down poll must retain its projection").into());
    };
    assert!(projection.evidence.is_none());
    assert!(projection.normalized.is_null());
    Ok(())
}

fn regular_files_under(root: &Path) -> TestResult<Vec<PathBuf>> {
    let mut files = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in std::fs::read_dir(directory)? {
            let path = entry?.path();
            if path.is_dir() {
                pending.push(path);
            } else if path.is_file() {
                files.push(path);
            }
        }
    }
    Ok(files)
}
