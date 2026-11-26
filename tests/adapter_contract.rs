#![cfg(unix)]

use std::collections::BTreeMap;
use std::error::Error;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use futures::StreamExt;
use re_kit_workspace::adapters::{
    AdapterError, AdapterLimits, FourmilabAction, FourmilabAdapter, InspectorAdapter,
    InspectorSampleSlot,
};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tokio::time::sleep;

const FOURMILAB_RUN: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const FOURMILAB_STATE: &[u8] = include_bytes!("fixtures/fourmilab/state.json");
const FOURMILAB_ACTION: &[u8] = include_bytes!("fixtures/fourmilab/action.json");
const FOURMILAB_ERROR: &[u8] = include_bytes!("fixtures/fourmilab/error.json");
const INSPECTOR_STATE: &[u8] = include_bytes!("fixtures/inspector/state.json");
const INSPECTOR_RUN: &[u8] = include_bytes!("fixtures/inspector/run.json");
const INSPECTOR_STALE: &[u8] = include_bytes!("fixtures/inspector/stale.json");
const INSPECTOR_A_BEFORE_B: &[u8] = include_bytes!("fixtures/inspector/a_before_b.json");
const INSPECTOR_MCP_INITIALIZE: &[u8] = include_bytes!("fixtures/inspector/mcp_initialize.json");
const MAX_SAMPLE_BYTES: u64 = 16 * 1024 * 1024;

type TestResult = Result<(), Box<dyn Error + Send + Sync>>;

static NEXT_SOCKET: AtomicU64 = AtomicU64::new(1);

#[derive(Debug)]
struct CapturedRequest {
    request_line: String,
    headers: BTreeMap<String, String>,
    body: Vec<u8>,
}

struct WriteStep {
    delay: Duration,
    bytes: Vec<u8>,
}

impl WriteStep {
    fn now(bytes: Vec<u8>) -> Self {
        Self {
            delay: Duration::ZERO,
            bytes,
        }
    }

    fn after(delay: Duration, bytes: Vec<u8>) -> Self {
        Self { delay, bytes }
    }
}

struct FakeServer {
    socket_path: PathBuf,
    directory: PathBuf,
    task: JoinHandle<()>,
}

impl FakeServer {
    fn socket_path(&self) -> &Path {
        &self.socket_path
    }
}

impl Drop for FakeServer {
    fn drop(&mut self) {
        self.task.abort();
        let _ = fs::remove_file(&self.socket_path);
        let _ = fs::remove_dir(&self.directory);
    }
}

async fn spawn_server(
    response: Vec<WriteStep>,
) -> Result<(FakeServer, oneshot::Receiver<CapturedRequest>), Box<dyn Error + Send + Sync>> {
    let directory = std::env::temp_dir().join(format!(
        "re-kit-adapter-{}-{}",
        std::process::id(),
        NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir(&directory)?;
    let socket_path = directory.join("legacy.sock");
    let listener = UnixListener::bind(&socket_path)?;
    let (request_tx, request_rx) = oneshot::channel();
    let task = tokio::spawn(async move {
        let Ok((mut stream, _)) = listener.accept().await else {
            return;
        };
        let Ok(request) = read_request(&mut stream).await else {
            return;
        };
        let _ = request_tx.send(request);
        for step in response {
            if !step.delay.is_zero() {
                sleep(step.delay).await;
            }
            if stream.write_all(&step.bytes).await.is_err() {
                return;
            }
        }
    });
    Ok((
        FakeServer {
            socket_path,
            directory,
            task,
        },
        request_rx,
    ))
}

async fn read_request(stream: &mut UnixStream) -> Result<CapturedRequest, std::io::Error> {
    let mut bytes = Vec::new();
    let header_end = loop {
        if let Some(end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
            break end + 4;
        }
        let mut chunk = [0_u8; 4096];
        let read = stream.read(&mut chunk).await?;
        if read == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "request ended before headers",
            ));
        }
        bytes.extend_from_slice(&chunk[..read]);
    };
    let head = std::str::from_utf8(&bytes[..header_end]).map_err(std::io::Error::other)?;
    let mut lines = head.trim_end_matches("\r\n\r\n").split("\r\n");
    let request_line = lines.next().unwrap_or_default().to_owned();
    let mut headers = BTreeMap::new();
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_owned());
    }
    let content_length = headers
        .get("content-length")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or_default();
    while bytes.len() < header_end + content_length {
        let mut chunk = [0_u8; 4096];
        let read = stream.read(&mut chunk).await?;
        if read == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "request ended before body",
            ));
        }
        bytes.extend_from_slice(&chunk[..read]);
    }
    Ok(CapturedRequest {
        request_line,
        headers,
        body: bytes[header_end..header_end + content_length].to_vec(),
    })
}

fn response(status: u16, content_type: &str, body: &[u8]) -> Vec<WriteStep> {
    response_with_headers(status, content_type, body, &[])
}

fn response_with_headers(
    status: u16,
    content_type: &str,
    body: &[u8],
    headers: &[(&str, &str)],
) -> Vec<WriteStep> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        409 => "Conflict",
        _ => "Error",
    };
    let mut response = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    )
    .into_bytes();
    for (name, value) in headers {
        response.extend_from_slice(format!("{name}: {value}\r\n").as_bytes());
    }
    response.extend_from_slice(b"\r\n");
    response.extend_from_slice(body);
    vec![WriteStep::now(response)]
}

fn chunked_response(content_type: &str, body: &[u8], chunk_size: usize) -> Vec<WriteStep> {
    let mut steps = vec![WriteStep::now(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        .into_bytes(),
    )];
    for chunk in body.chunks(chunk_size) {
        let mut frame = format!("{:x}\r\n", chunk.len()).into_bytes();
        frame.extend_from_slice(chunk);
        frame.extend_from_slice(b"\r\n");
        steps.push(WriteStep::now(frame));
    }
    steps.push(WriteStep::now(b"0\r\n\r\n".to_vec()));
    steps
}

fn fast_limits() -> AdapterLimits {
    AdapterLimits {
        connect_timeout: Duration::from_millis(100),
        read_timeout: Duration::from_millis(100),
        ..AdapterLimits::default()
    }
}
fn require_adapter_error<T>(result: Result<T, AdapterError>) -> Result<AdapterError, io::Error> {
    match result {
        Ok(_) => Err(io::Error::other("adapter unexpectedly succeeded")),
        Err(error) => Ok(error),
    }
}

#[tokio::test]
async fn fourmilab_state_decodes_chunked_json() -> TestResult {
    let (server, _request) =
        spawn_server(chunked_response("application/json", FOURMILAB_STATE, 17)).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), fast_limits());

    let state = adapter.state(Some(FOURMILAB_RUN)).await?;

    assert_eq!(state["runs"][0]["id"], FOURMILAB_RUN);
    Ok(())
}

#[tokio::test]
async fn inspector_state_decodes_content_length_json() -> TestResult {
    let (server, _request) =
        spawn_server(response(200, "application/json", INSPECTOR_STATE)).await?;
    let adapter =
        InspectorAdapter::with_limits(server.socket_path(), server.socket_path(), fast_limits());

    let state = adapter.state().await?;

    assert_eq!(state["revision"], 1);
    Ok(())
}
#[tokio::test]
async fn inspector_run_decodes_typed_receipt() -> TestResult {
    let (server, request) = spawn_server(response(200, "application/json", INSPECTOR_RUN)).await?;
    let adapter =
        InspectorAdapter::with_limits(server.socket_path(), server.socket_path(), fast_limits());

    let receipt = adapter
        .run(1, "identify_file", json!({ "slot": "a" }))
        .await?;
    let captured = request.await?;
    let request_body: Value = serde_json::from_slice(&captured.body)?;

    assert_eq!(
        (
            receipt.result["size"].as_u64(),
            receipt.state["revision"].as_u64(),
            captured.request_line.as_str(),
            captured.headers.get("origin").map(String::as_str),
            captured.headers.get("x-yuzu-revision").map(String::as_str),
            request_body,
        ),
        (
            Some(3),
            Some(1),
            "POST /api/run HTTP/1.1",
            Some("http://localhost"),
            Some("1"),
            json!({ "tool": "identify_file", "arguments": { "slot": "a" } }),
        )
    );
    Ok(())
}

#[tokio::test]
async fn state_rejects_body_over_limit() -> TestResult {
    let mut limits = fast_limits();
    limits.max_json_body_bytes = 8;
    let body = vec![b'x'; 64];
    let (server, _request) = spawn_server(response(200, "application/json", &body)).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), limits);

    let error = require_adapter_error(adapter.state(None).await)?;

    assert_eq!(error, AdapterError::TooLarge);
    Ok(())
}

#[tokio::test]
async fn state_rejects_wrong_content_type() -> TestResult {
    let (server, _request) = spawn_server(response(200, "text/plain", FOURMILAB_STATE)).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), fast_limits());

    let error = require_adapter_error(adapter.state(None).await)?;

    assert!(matches!(error, AdapterError::Protocol(_)));
    Ok(())
}

#[tokio::test]
async fn inspector_preserves_stale_revision_error() -> TestResult {
    let (server, _request) =
        spawn_server(response(409, "application/json", INSPECTOR_STALE)).await?;
    let adapter =
        InspectorAdapter::with_limits(server.socket_path(), server.socket_path(), fast_limits());

    let error = require_adapter_error(
        adapter
            .run(0, "identify_file", json!({ "slot": "a" }))
            .await,
    )?;

    assert_eq!(
        error,
        AdapterError::Legacy {
            status: 409,
            code: "The files changed. Refresh the case and try again.".to_owned(),
        }
    );
    Ok(())
}

#[tokio::test]
async fn fourmilab_action_emits_trusted_legacy_headers() -> TestResult {
    let (server, request) =
        spawn_server(response(200, "application/json", FOURMILAB_ACTION)).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), fast_limits());

    let receipt = adapter.action(FourmilabAction::Start).await?;
    let captured = request.await?;

    assert_eq!(
        (
            receipt.ok,
            receipt.run.as_deref(),
            captured.request_line.as_str(),
            captured.headers.get("host").map(String::as_str),
            captured.headers.get("origin").map(String::as_str),
            captured
                .headers
                .get("x-candidate-action")
                .map(String::as_str),
            captured.headers.get("content-type").map(String::as_str),
            serde_json::from_slice::<Value>(&captured.body)?,
        ),
        (
            true,
            Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            "POST /action HTTP/1.1",
            Some("127.0.0.1:48784"),
            Some("http://127.0.0.1:48784"),
            Some("1"),
            Some("application/json"),
            json!({ "action": "start" }),
        )
    );
    Ok(())
}

#[tokio::test]
async fn fourmilab_package_stream_yields_multiple_chunks() -> TestResult {
    let package = vec![0x5a; 40 * 1024];
    let mut limits = fast_limits();
    limits.stream_chunk_bytes = 4096;
    let (server, _request) = spawn_server(response(200, "application/zip", &package)).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), limits);

    let mut stream = adapter.package_stream(FOURMILAB_RUN).await?;
    let mut received = Vec::new();
    let mut chunks = 0;
    while let Some(chunk) = stream.next().await {
        received.extend_from_slice(&chunk?);
        chunks += 1;
    }

    assert_eq!((received, chunks > 1), (package, true));
    Ok(())
}

#[tokio::test]
async fn fourmilab_maps_legacy_error_body() -> TestResult {
    let (server, _request) =
        spawn_server(response(400, "application/json", FOURMILAB_ERROR)).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), fast_limits());

    let error = require_adapter_error(
        adapter
            .state(Some("cccccccccccccccccccccccccccccccc"))
            .await,
    )?;

    assert_eq!(
        error,
        AdapterError::Legacy {
            status: 400,
            code: "Invalid run selector.".to_owned(),
        }
    );
    Ok(())
}

#[tokio::test]
async fn package_stream_reports_mid_body_disconnect() -> TestResult {
    let head = b"HTTP/1.1 200 OK\r\nContent-Type: application/zip\r\nContent-Length: 20\r\nConnection: close\r\n\r\npartial".to_vec();
    let (server, _request) = spawn_server(vec![WriteStep::now(head)]).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), fast_limits());

    let mut stream = adapter.package_stream(FOURMILAB_RUN).await?;
    let mut terminal_error = None;
    while let Some(chunk) = stream.next().await {
        if let Err(error) = chunk {
            terminal_error = Some(error);
            break;
        }
    }

    assert!(matches!(terminal_error, Some(AdapterError::Protocol(_))));
    Ok(())
}

#[tokio::test]
async fn state_times_out_while_waiting_for_headers() -> TestResult {
    let mut limits = fast_limits();
    limits.read_timeout = Duration::from_millis(20);
    let success_response = response(200, "application/json", FOURMILAB_STATE);
    let bytes = success_response
        .into_iter()
        .next()
        .ok_or_else(|| io::Error::other("fixture response was empty"))?
        .bytes;
    let delayed = WriteStep::after(Duration::from_millis(200), bytes);
    let (server, _request) = spawn_server(vec![delayed]).await?;
    let adapter = FourmilabAdapter::with_limits(server.socket_path(), limits);

    let error = require_adapter_error(adapter.state(None).await)?;

    assert_eq!(error, AdapterError::Timeout);
    Ok(())
}

#[tokio::test]
async fn state_reports_unreachable_socket() -> TestResult {
    let directory = std::env::temp_dir().join(format!(
        "re-kit-adapter-missing-{}-{}",
        std::process::id(),
        NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
    ));
    let adapter = FourmilabAdapter::with_limits(directory.join("missing.sock"), fast_limits());

    let error = require_adapter_error(adapter.state(None).await)?;

    assert_eq!(error, AdapterError::Unreachable);
    Ok(())
}

#[tokio::test]
async fn inspector_upload_streams_body_with_fixed_headers() -> TestResult {
    let (server, request) =
        spawn_server(response(200, "application/json", INSPECTOR_STATE)).await?;
    let adapter =
        InspectorAdapter::with_limits(server.socket_path(), server.socket_path(), fast_limits());
    let (mut writer, reader) = tokio::io::duplex(2);
    let write_task = tokio::spawn(async move {
        writer.write_all(b"abc").await?;
        writer.shutdown().await
    });

    let state = adapter
        .upload_sample(0, InspectorSampleSlot::A, 3, reader)
        .await?;
    write_task.await??;
    let captured = request.await?;

    assert_eq!(
        (
            state["revision"].as_u64(),
            captured.request_line.as_str(),
            captured.headers.get("host").map(String::as_str),
            captured.headers.get("origin").map(String::as_str),
            captured.headers.get("x-yuzu-revision").map(String::as_str),
            captured.headers.get("content-type").map(String::as_str),
            captured.body.as_slice(),
        ),
        (
            Some(1),
            "POST /api/sample/a HTTP/1.1",
            Some("localhost"),
            Some("http://localhost"),
            Some("0"),
            Some("application/octet-stream"),
            b"abc".as_slice(),
        )
    );
    Ok(())
}

#[tokio::test]
async fn inspector_rejects_upload_larger_than_16_mib_before_connect() -> TestResult {
    let missing = std::env::temp_dir().join("re-kit-oversize-upload-missing.sock");
    let adapter = InspectorAdapter::with_limits(&missing, &missing, fast_limits());
    let reader = tokio::io::empty();

    let error = require_adapter_error(
        adapter
            .upload_sample(0, InspectorSampleSlot::A, MAX_SAMPLE_BYTES + 1, reader)
            .await,
    )?;

    assert_eq!(error, AdapterError::TooLarge);
    Ok(())
}

#[tokio::test]
async fn inspector_preserves_a_before_b_error() -> TestResult {
    let (server, _request) =
        spawn_server(response(400, "application/json", INSPECTOR_A_BEFORE_B)).await?;
    let adapter =
        InspectorAdapter::with_limits(server.socket_path(), server.socket_path(), fast_limits());
    let reader = tokio::io::empty();

    let error = require_adapter_error(
        adapter
            .upload_sample(0, InspectorSampleSlot::B, 0, reader)
            .await,
    )?;

    assert_eq!(
        error,
        AdapterError::Legacy {
            status: 400,
            code: "Add file A first.".to_owned(),
        }
    );
    Ok(())
}

#[tokio::test]
async fn inspector_mcp_initialize_preserves_agent_headers_and_session() -> TestResult {
    let response = response_with_headers(
        200,
        "application/json",
        INSPECTOR_MCP_INITIALIZE,
        &[("Mcp-Session-Id", "private-session")],
    );
    let (server, request) = spawn_server(response).await?;
    let adapter =
        InspectorAdapter::with_limits(server.socket_path(), server.socket_path(), fast_limits());
    let packet = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "clientInfo": { "name": "workspace", "version": "1" },
            "capabilities": {}
        }
    });

    let receipt = adapter
        .mcp_request("private-token", None, None, packet.clone())
        .await?;
    let captured = request.await?;

    assert_eq!(
        (
            receipt.session_id.as_deref(),
            receipt.body.as_ref().and_then(|body| body["id"].as_u64()),
            captured.request_line.as_str(),
            captured.headers.get("authorization").map(String::as_str),
            serde_json::from_slice::<Value>(&captured.body)?,
        ),
        (
            Some("private-session"),
            Some(1),
            "POST /mcp HTTP/1.1",
            Some("Bearer private-token"),
            packet,
        )
    );
    Ok(())
}
