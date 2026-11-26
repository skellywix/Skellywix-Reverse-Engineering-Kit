use std::error::Error;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::http::{Request, Response, StatusCode, header};
use http_body_util::BodyExt;
use re_kit_workspace::api::{AppConfig, AppState, router};
use re_kit_workspace::store::Store;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{oneshot, watch};
use tower::ServiceExt;

static NEXT_APP: AtomicU64 = AtomicU64::new(1);

type TestResult<T = ()> = Result<T, Box<dyn Error>>;

struct TestRoot {
    path: PathBuf,
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_dir_all(&self.path)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!(
                "failed to remove test root {}: {error}",
                self.path.display()
            );
        }
    }
}

struct TestApp {
    app: Router,
    store: Store,
    shutdown: watch::Sender<bool>,
    _shutdown_receiver: watch::Receiver<bool>,
    _root: TestRoot,
}

impl TestApp {
    async fn new(allow_missing_origin: bool) -> TestResult<Self> {
        let suffix = NEXT_APP.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!("re-kit-api-{}-{suffix}", std::process::id()));
        let static_dir = root.join("static");
        std::fs::create_dir_all(static_dir.join("assets"))?;
        std::fs::write(
            static_dir.join("index.html"),
            "<!doctype html><title>RE Kit contract shell</title>",
        )?;
        std::fs::write(
            static_dir.join("assets/app-a1b2c3.js"),
            "console.log('contract');",
        )?;
        let store = Store::open(root.join("workspace.sqlite3")).await?;
        let (shutdown, shutdown_receiver) = watch::channel(false);
        let state = AppState::new(
            store.clone(),
            Arc::new(AppConfig {
                static_dir,
                allow_missing_origin,
            }),
            shutdown_receiver.clone(),
        );
        Ok(Self {
            app: router(state),
            store,
            shutdown,
            _shutdown_receiver: shutdown_receiver,
            _root: TestRoot { path: root },
        })
    }
}

fn request_error(message: &'static str) -> std::io::Error {
    std::io::Error::other(message)
}

fn command_request(uri: &str, key: &str, body: Value) -> TestResult<Request<Body>> {
    Ok(Request::builder()
        .method("POST")
        .uri(uri)
        .header(header::HOST, "workspace.test")
        .header(header::ORIGIN, "http://workspace.test")
        .header(header::CONTENT_TYPE, "application/json")
        .header("Idempotency-Key", key)
        .body(Body::from(serde_json::to_vec(&body)?))?)
}

async fn json_body(response: Response<Body>) -> TestResult<Value> {
    let bytes = response.into_body().collect().await?.to_bytes();
    Ok(serde_json::from_slice(&bytes)?)
}

fn required_json_string(value: &Value, key: &str) -> TestResult<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| request_error("JSON string field is missing").into())
}

async fn create_project(app: &Router, key: &str, name: &str) -> TestResult<Value> {
    let response = app
        .clone()
        .oneshot(command_request(
            "/api/v1/commands",
            key,
            json!({
                "command": "create_project",
                "project_id": null,
                "job_id": null,
                "expected_revision": 0,
                "payload": {"kind": "convert", "name": name}
            }),
        )?)
        .await?;
    if response.status() != StatusCode::OK {
        return Err(request_error("create_project did not succeed").into());
    }
    json_body(response).await
}

async fn start_job(app: &Router, key: &str, project_id: &str) -> TestResult<Value> {
    let response = app
        .clone()
        .oneshot(command_request(
            "/api/v1/commands",
            key,
            json!({
                "command": "start_job",
                "project_id": project_id,
                "job_id": null,
                "expected_revision": 1,
                "payload": {"phase": "analyze"}
            }),
        )?)
        .await?;
    if response.status() != StatusCode::OK {
        return Err(request_error("start_job did not succeed").into());
    }
    json_body(response).await
}

async fn first_sse_chunk(response: Response<Body>) -> TestResult<String> {
    let mut body = response.into_body();
    let next = tokio::time::timeout(Duration::from_secs(2), body.frame()).await?;
    let frame = next.ok_or_else(|| request_error("SSE body ended before its first event"))??;
    let data: Bytes = frame
        .into_data()
        .map_err(|_| request_error("first SSE frame was not data"))?;
    Ok(String::from_utf8(data.to_vec())?)
}

async fn stored_row_count(store: &Store) -> TestResult<i64> {
    Ok(sqlx::query_scalar(
        "SELECT
            (SELECT COUNT(*) FROM projects) +
            (SELECT COUNT(*) FROM jobs) +
            (SELECT COUNT(*) FROM attempts) +
            (SELECT COUNT(*) FROM idempotency_keys) +
            (SELECT COUNT(*) FROM events)",
    )
    .fetch_one(store.pool())
    .await?)
}

fn assert_security_headers(response: &Response<Body>) {
    assert_eq!(
        response.headers().get(header::CONTENT_SECURITY_POLICY),
        Some(&header::HeaderValue::from_static(
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        ))
    );
    assert_eq!(
        response.headers().get(header::X_CONTENT_TYPE_OPTIONS),
        Some(&header::HeaderValue::from_static("nosniff"))
    );
    assert_eq!(
        response.headers().get(header::REFERRER_POLICY),
        Some(&header::HeaderValue::from_static("no-referrer"))
    );
}

#[tokio::test]
async fn snapshot_is_read_only_and_has_security_headers() -> TestResult {
    let harness = TestApp::new(false).await?;
    let before = stored_row_count(&harness.store).await?;

    let response = harness
        .app
        .clone()
        .oneshot(Request::get("/api/v1/snapshot").body(Body::empty())?)
        .await?;

    assert_eq!(response.status(), StatusCode::OK);
    assert_security_headers(&response);
    let body = json_body(response).await?;
    assert_eq!(body.get("cursor"), Some(&json!(0)));
    let after = stored_row_count(&harness.store).await?;
    assert_eq!(after, before);
    Ok(())
}

#[tokio::test]
async fn duplicate_command_replays_receipt_and_conflicting_body_returns_409() -> TestResult {
    let harness = TestApp::new(false).await?;
    let body = json!({
        "command": "create_project",
        "project_id": null,
        "job_id": null,
        "expected_revision": 0,
        "payload": {"kind": "convert", "name": "same"}
    });

    let first = harness
        .app
        .clone()
        .oneshot(command_request(
            "/api/v1/commands",
            "same-key",
            body.clone(),
        )?)
        .await?;
    let first_body = json_body(first).await?;
    assert_eq!(first_body.get("replayed"), Some(&json!(false)));

    let replay = harness
        .app
        .clone()
        .oneshot(command_request("/api/v1/commands", "same-key", body)?)
        .await?;
    let replay_body = json_body(replay).await?;
    assert_eq!(replay_body.get("replayed"), Some(&json!(true)));
    assert_eq!(replay_body.get("project_id"), first_body.get("project_id"));
    assert_eq!(replay_body.get("cursor"), first_body.get("cursor"));

    let conflict = harness
        .app
        .clone()
        .oneshot(command_request(
            "/api/v1/commands",
            "same-key",
            json!({
                "command": "create_project",
                "project_id": null,
                "job_id": null,
                "expected_revision": 0,
                "payload": {"kind": "convert", "name": "different"}
            }),
        )?)
        .await?;
    assert_eq!(conflict.status(), StatusCode::CONFLICT);
    assert_eq!(
        json_body(conflict).await?.get("error"),
        Some(&json!("idempotency_conflict"))
    );
    let project_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM projects")
        .fetch_one(harness.store.pool())
        .await?;
    assert_eq!(project_count, 1);
    Ok(())
}

#[tokio::test]
async fn stale_revision_returns_409_with_current_revision() -> TestResult {
    let harness = TestApp::new(false).await?;
    let project = create_project(&harness.app, "stale-project", "stale").await?;
    let project_id = required_json_string(&project, "project_id")?;

    let response = harness
        .app
        .clone()
        .oneshot(command_request(
            "/api/v1/commands",
            "stale-job",
            json!({
                "command": "start_job",
                "project_id": project_id,
                "job_id": null,
                "expected_revision": 0,
                "payload": {"phase": "analyze"}
            }),
        )?)
        .await?;

    assert_eq!(response.status(), StatusCode::CONFLICT);
    let body = json_body(response).await?;
    assert_eq!(body.get("error"), Some(&json!("stale_revision")));
    assert_eq!(body.get("current_revision"), Some(&json!(1)));
    Ok(())
}

#[tokio::test]
async fn illegal_transition_returns_typed_conflict() -> TestResult {
    let harness = TestApp::new(false).await?;
    let project = create_project(&harness.app, "illegal-project", "illegal").await?;
    let project_id = required_json_string(&project, "project_id")?;
    let job = start_job(&harness.app, "illegal-job", &project_id).await?;
    let job_id = required_json_string(&job, "job_id")?;

    let response = harness
        .app
        .clone()
        .oneshot(command_request(
            "/api/v1/commands",
            "illegal-cancel",
            json!({
                "command": "cancel_job",
                "project_id": project_id,
                "job_id": job_id,
                "expected_revision": 1,
                "payload": {}
            }),
        )?)
        .await?;

    assert_eq!(response.status(), StatusCode::CONFLICT);
    assert_eq!(
        json_body(response).await?.get("error"),
        Some(&json!("illegal_transition"))
    );
    Ok(())
}

#[tokio::test]
async fn sse_replays_after_last_event_id_header() -> TestResult {
    let harness = TestApp::new(false).await?;
    let project = create_project(&harness.app, "events-project", "events").await?;
    let project_id = required_json_string(&project, "project_id")?;
    start_job(&harness.app, "events-job", &project_id).await?;

    let response = harness
        .app
        .clone()
        .oneshot(
            Request::get("/api/v1/events?after=0")
                .header("Last-Event-ID", "1")
                .body(Body::empty())?,
        )
        .await?;

    assert_eq!(response.status(), StatusCode::OK);
    let chunk = first_sse_chunk(response).await?;
    assert!(chunk.contains("id: 2"), "unexpected SSE chunk: {chunk}");
    assert!(
        chunk.contains("event: workspace"),
        "unexpected SSE chunk: {chunk}"
    );
    assert!(
        chunk.contains("\"type\":\"job.queued\""),
        "unexpected SSE chunk: {chunk}"
    );
    assert!(!chunk.contains("project.created"));
    Ok(())
}

#[tokio::test]
async fn sse_emits_reset_when_cursor_precedes_retained_history() -> TestResult {
    let harness = TestApp::new(false).await?;
    let project = create_project(&harness.app, "gap-project", "gap").await?;
    let project_id = required_json_string(&project, "project_id")?;
    start_job(&harness.app, "gap-job", &project_id).await?;
    sqlx::query("DELETE FROM events WHERE event_id = 1")
        .execute(harness.store.pool())
        .await?;

    let response = harness
        .app
        .clone()
        .oneshot(Request::get("/api/v1/events?after=0").body(Body::empty())?)
        .await?;

    let chunk = first_sse_chunk(response).await?;
    assert!(
        chunk.contains("event: reset"),
        "unexpected SSE chunk: {chunk}"
    );
    assert!(
        chunk.contains("data: {\"cursor\":2}"),
        "unexpected SSE chunk: {chunk}"
    );
    Ok(())
}

#[tokio::test]
async fn idle_sse_connection_drains_within_graceful_shutdown_bound() -> TestResult {
    let harness = TestApp::new(false).await?;
    create_project(&harness.app, "shutdown-project", "shutdown").await?;
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await?;
    let address = listener.local_addr()?;
    let app = harness.app.clone();
    let shutdown = harness.shutdown.clone();
    let (graceful_tx, graceful_rx) = oneshot::channel();
    let mut server = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = graceful_rx.await;
                let _ = shutdown.send(true);
            })
            .await
    });

    let mut connection = tokio::net::TcpStream::connect(address).await?;
    connection
        .write_all(
            b"GET /api/v1/events?after=0 HTTP/1.1\r\nHost: workspace.test\r\nAccept: text/event-stream\r\nConnection: keep-alive\r\n\r\n",
        )
        .await?;
    let response_headers = tokio::time::timeout(Duration::from_secs(2), async {
        let mut received = Vec::new();
        let mut buffer = [0_u8; 512];
        loop {
            let read = connection.read(&mut buffer).await?;
            if read == 0 {
                return Err(request_error("SSE response ended before its headers"));
            }
            received.extend_from_slice(&buffer[..read]);
            let has_headers = received.windows(4).any(|window| window == b"\r\n\r\n");
            let has_event = received
                .windows(b"event: workspace".len())
                .any(|window| window == b"event: workspace")
                && received.windows(2).any(|window| window == b"\n\n");
            if has_headers && has_event {
                return Ok(received);
            }
        }
    })
    .await
    .map_err(|_| request_error("timed out waiting for the initial SSE event"))??;
    let response_headers = String::from_utf8(response_headers)?;
    assert!(response_headers.starts_with("HTTP/1.1 200"));
    assert!(
        response_headers
            .to_ascii_lowercase()
            .contains("content-type: text/event-stream")
    );
    assert!(response_headers.contains("event: workspace"));

    graceful_tx
        .send(())
        .map_err(|_| request_error("graceful shutdown trigger was dropped"))?;
    let server_result = match tokio::time::timeout(Duration::from_secs(2), &mut server).await {
        Ok(result) => result?,
        Err(_) => {
            server.abort();
            return Err(request_error("idle SSE stream prevented graceful shutdown").into());
        }
    };
    server_result?;
    tokio::time::timeout(Duration::from_secs(2), async {
        let mut buffer = [0_u8; 512];
        loop {
            if connection.read(&mut buffer).await? == 0 {
                return Ok::<(), std::io::Error>(());
            }
        }
    })
    .await
    .map_err(|_| request_error("SSE connection did not close after server drain"))??;
    Ok(())
}

#[tokio::test]
async fn mismatched_origin_is_rejected() -> TestResult {
    let harness = TestApp::new(false).await?;
    let request = Request::builder()
        .method("POST")
        .uri("/api/v1/commands")
        .header(header::HOST, "workspace.test")
        .header(header::ORIGIN, "https://other.test")
        .header(header::CONTENT_TYPE, "application/json")
        .header("Idempotency-Key", "origin")
        .body(Body::from(serde_json::to_vec(&json!({
            "command": "create_project",
            "project_id": null,
            "job_id": null,
            "expected_revision": 0,
            "payload": {"kind": "convert", "name": "origin"}
        }))?))?;

    let response = harness.app.oneshot(request).await?;

    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert_eq!(
        json_body(response).await?.get("error"),
        Some(&json!("forbidden_origin"))
    );
    Ok(())
}

#[tokio::test]
async fn shared_origin_guard_covers_workflow_mutations_and_preserves_reads() -> TestResult {
    let harness = TestApp::new(false).await?;
    let rejected = harness
        .app
        .clone()
        .oneshot(
            Request::post("/api/v1/inspect/guard-probe")
                .header(header::HOST, "workspace.test")
                .header(header::ORIGIN, "https://other.test")
                .body(Body::empty())?,
        )
        .await?;
    assert_eq!(rejected.status(), StatusCode::FORBIDDEN);
    assert_eq!(
        json_body(rejected).await?.get("error"),
        Some(&json!("forbidden_origin"))
    );

    let read_only = harness
        .app
        .oneshot(
            Request::get("/api/v1/inspect/guard-probe")
                .header(header::HOST, "workspace.test")
                .header(header::ORIGIN, "https://other.test")
                .body(Body::empty())?,
        )
        .await?;
    assert_eq!(read_only.status(), StatusCode::NOT_FOUND);

    let tooling = TestApp::new(true).await?;
    let missing_origin_allowed = tooling
        .app
        .oneshot(
            Request::post("/api/v1/inspect/guard-probe")
                .header(header::HOST, "workspace.test")
                .body(Body::empty())?,
        )
        .await?;
    assert_eq!(missing_origin_allowed.status(), StatusCode::NOT_FOUND);
    Ok(())
}

#[tokio::test]
async fn command_body_over_64_kib_returns_413() -> TestResult {
    let harness = TestApp::new(false).await?;
    let request = Request::builder()
        .method("POST")
        .uri("/api/v1/commands")
        .header(header::HOST, "workspace.test")
        .header(header::ORIGIN, "http://workspace.test")
        .header(header::CONTENT_TYPE, "application/json")
        .header("Idempotency-Key", "oversize")
        .body(Body::from(vec![b'a'; 64 * 1024 + 1]))?;

    let response = harness.app.oneshot(request).await?;

    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(
        json_body(response).await?.get("error"),
        Some(&json!("payload_too_large"))
    );
    Ok(())
}

#[tokio::test]
async fn unknown_api_route_returns_json_404() -> TestResult {
    let harness = TestApp::new(false).await?;

    let response = harness
        .app
        .oneshot(Request::get("/api/unknown").body(Body::empty())?)
        .await?;

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    assert_eq!(
        response.headers().get(header::CONTENT_TYPE),
        Some(&header::HeaderValue::from_static("application/json"))
    );
    assert_eq!(
        json_body(response).await?.get("error"),
        Some(&json!("not_found"))
    );
    Ok(())
}

#[tokio::test]
async fn spa_fallback_and_asset_cache_apply_only_to_frontend_routes() -> TestResult {
    let harness = TestApp::new(false).await?;

    let frontend = harness
        .app
        .clone()
        .oneshot(Request::get("/convert/runs/j_1").body(Body::empty())?)
        .await?;
    assert_eq!(frontend.status(), StatusCode::OK);
    assert_eq!(
        frontend.headers().get(header::CACHE_CONTROL),
        Some(&header::HeaderValue::from_static("no-cache"))
    );
    assert_security_headers(&frontend);
    let frontend_bytes = frontend.into_body().collect().await?.to_bytes();
    assert!(String::from_utf8(frontend_bytes.to_vec())?.contains("RE Kit contract shell"));

    let asset = harness
        .app
        .clone()
        .oneshot(Request::get("/assets/app-a1b2c3.js").body(Body::empty())?)
        .await?;
    assert_eq!(asset.status(), StatusCode::OK);
    assert_eq!(
        asset.headers().get(header::CACHE_CONTROL),
        Some(&header::HeaderValue::from_static(
            "public, max-age=31536000, immutable"
        ))
    );

    let unknown = harness
        .app
        .oneshot(Request::get("/not-a-frontend-route").body(Body::empty())?)
        .await?;
    assert_eq!(unknown.status(), StatusCode::NOT_FOUND);
    let bytes = unknown.into_body().collect().await?.to_bytes();
    assert!(!String::from_utf8(bytes.to_vec())?.contains("RE Kit contract shell"));
    Ok(())
}
