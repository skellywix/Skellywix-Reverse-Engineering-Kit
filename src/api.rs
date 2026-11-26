use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::extract::rejection::BytesRejection;
use axum::extract::{DefaultBodyLimit, Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header};
use axum::middleware::{self, Next};
use axum::response::sse::{Event, KeepAlive};
use axum::response::{IntoResponse, Response, Sse};
use axum::routing::{any, get, post};
use futures::{Stream, stream};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::sync::watch;

#[cfg(unix)]
use crate::adapters::{AdapterLimits, FourmilabAdapter, InspectorAdapter};
use crate::assets;
use crate::domain::{CommandReceipt, CommandRequest, WorkspaceSnapshot};
use crate::events::{EventEnvelope, EventReplay};
use crate::store::{Store, StoreError};
use crate::workflows;

const MAX_COMMAND_BODY_BYTES: usize = 64 * 1024;
const CSP: &str = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'";

#[derive(Debug)]
pub struct AppConfig {
    pub static_dir: PathBuf,
    pub allow_missing_origin: bool,
}

#[derive(Clone)]
pub struct AppState {
    pub store: Store,
    pub config: Arc<AppConfig>,
    pub shutdown: watch::Receiver<bool>,
    #[cfg(unix)]
    pub(crate) fourmilab: Option<FourmilabAdapter>,
    #[cfg(unix)]
    pub(crate) inspector: Option<InspectorAdapter>,
}

impl AppState {
    pub fn new(store: Store, config: Arc<AppConfig>, shutdown: watch::Receiver<bool>) -> Self {
        Self {
            store,
            config,
            shutdown,
            #[cfg(unix)]
            fourmilab: None,
            #[cfg(unix)]
            inspector: None,
        }
    }

    #[cfg(unix)]
    pub fn with_engine_sockets(
        store: Store,
        config: Arc<AppConfig>,
        shutdown: watch::Receiver<bool>,
        fourmilab_socket: Option<PathBuf>,
        inspector_socket: Option<PathBuf>,
    ) -> Self {
        Self::with_engine_configuration(
            store,
            config,
            shutdown,
            fourmilab_socket,
            inspector_socket.clone(),
            inspector_socket,
            AdapterLimits::default(),
        )
    }

    #[cfg(not(unix))]
    pub fn with_engine_sockets(
        store: Store,
        config: Arc<AppConfig>,
        shutdown: watch::Receiver<bool>,
        _fourmilab_socket: Option<PathBuf>,
        _inspector_socket: Option<PathBuf>,
    ) -> Self {
        Self::new(store, config, shutdown)
    }

    #[cfg(unix)]
    pub fn with_engine_configuration(
        store: Store,
        config: Arc<AppConfig>,
        shutdown: watch::Receiver<bool>,
        fourmilab_socket: Option<PathBuf>,
        inspector_socket: Option<PathBuf>,
        inspector_agent_socket: Option<PathBuf>,
        limits: AdapterLimits,
    ) -> Self {
        let fourmilab =
            fourmilab_socket.map(|socket| FourmilabAdapter::with_limits(socket, limits));
        let inspector =
            inspector_socket
                .zip(inspector_agent_socket)
                .map(|(desk_socket, agent_socket)| {
                    InspectorAdapter::with_limits(desk_socket, agent_socket, limits)
                });
        Self {
            store,
            config,
            shutdown,
            fourmilab,
            inspector,
        }
    }
}

pub fn router(state: AppState) -> Router {
    let command_route = post(command)
        .fallback(api_not_found)
        .layer(DefaultBodyLimit::max(MAX_COMMAND_BODY_BYTES));
    let api_routes = Router::<AppState>::new()
        .route("/api/v1/snapshot", get(snapshot).fallback(api_not_found))
        .route("/api/v1/commands", command_route)
        .route("/api/v1/events", get(events).fallback(api_not_found))
        .nest("/api/v1/reconstruct", workflows::reconstruct::router())
        .nest("/api/v1/inspect", workflows::inspect::router())
        .nest("/api/v1/convert", workflows::convert::router())
        .nest("/api/v1/runtime", workflows::runtime::router())
        .nest("/api/v1/releases", workflows::releases::router())
        .route("/api", any(api_not_found))
        .route("/api/{*path}", any(api_not_found))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            mutation_origin_guard,
        ));
    let frontend_routes = Router::<AppState>::new()
        .route("/", get(assets::index))
        .route("/assets/{*path}", get(assets::asset))
        .route("/reconstruct", get(assets::index))
        .route("/reconstruct/{*path}", get(assets::index))
        .route("/inspect", get(assets::index))
        .route("/inspect/{*path}", get(assets::index))
        .route("/convert", get(assets::index))
        .route("/convert/{*path}", get(assets::index))
        .route("/runtime", get(assets::index))
        .route("/runtime/{*path}", get(assets::index))
        .route("/releases", get(assets::index))
        .route("/releases/{*path}", get(assets::index))
        .fallback(assets::not_found);

    api_routes
        .merge(frontend_routes)
        .layer(middleware::from_fn(security_headers))
        .with_state(state)
}

async fn snapshot(State(state): State<AppState>) -> Result<Json<WorkspaceSnapshot>, ApiError> {
    state
        .store
        .snapshot()
        .await
        .map(Json)
        .map_err(ApiError::from_store)
}

async fn command(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<Json<CommandReceipt>, ApiError> {
    if !has_json_content_type(&headers) {
        return Err(ApiError::bad_request(
            "Content-Type must be application/json",
        ));
    }
    let idempotency_key = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| ApiError::bad_request("Idempotency-Key is required"))?;
    let bytes = body.map_err(|rejection| {
        if rejection.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::new(
                StatusCode::PAYLOAD_TOO_LARGE,
                "payload_too_large",
                "request body exceeds 64 KiB",
            )
        } else {
            ApiError::bad_request("request body could not be read")
        }
    })?;
    let request: CommandRequest = serde_json::from_slice(&bytes)
        .map_err(|_| ApiError::bad_request("request body is not valid command JSON"))?;
    state
        .store
        .apply_command(idempotency_key, &request)
        .await
        .map(Json)
        .map_err(ApiError::from_store)
}

async fn events(
    State(state): State<AppState>,
    headers: HeaderMap,
    uri: Uri,
) -> Result<Sse<impl Stream<Item = Result<Event, serde_json::Error>>>, ApiError> {
    let Query(query) = Query::<EventsQuery>::try_from_uri(&uri)
        .map_err(|_| ApiError::bad_request("after must be an unsigned integer"))?;
    let mut after = query.after.unwrap_or(0);
    if let Some(last_event_id) = headers.get("last-event-id") {
        after = last_event_id
            .to_str()
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or_else(|| ApiError::bad_request("Last-Event-ID must be an unsigned integer"))?;
    }
    let replay = state
        .store
        .replay_events(after)
        .await
        .map_err(ApiError::from_store)?;
    let stream_state = EventStreamState::new(state.store, state.shutdown, after, replay);
    Ok(Sse::new(event_stream(stream_state)).keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("heartbeat"),
    ))
}

#[derive(Debug, Deserialize)]
struct EventsQuery {
    after: Option<u64>,
}

struct EventStreamState {
    store: Store,
    cursor: u64,
    pending: VecDeque<EventEnvelope>,
    reset: Option<u64>,
    stopped: bool,
    shutdown: watch::Receiver<bool>,
}

impl EventStreamState {
    fn new(
        store: Store,
        shutdown: watch::Receiver<bool>,
        cursor: u64,
        replay: EventReplay,
    ) -> Self {
        match replay {
            EventReplay::Events { events, .. } => Self {
                store,
                cursor,
                pending: events.into(),
                reset: None,
                stopped: false,
                shutdown,
            },
            EventReplay::Reset { cursor: high_water } => Self {
                store,
                cursor,
                pending: VecDeque::new(),
                reset: Some(high_water),
                stopped: false,
                shutdown,
            },
        }
    }
}

fn event_stream(state: EventStreamState) -> impl Stream<Item = Result<Event, serde_json::Error>> {
    stream::unfold(state, |mut state| async move {
        loop {
            if *state.shutdown.borrow() {
                return None;
            }
            if state.stopped {
                return None;
            }
            if let Some(cursor) = state.reset.take() {
                state.stopped = true;
                let data = serde_json::to_string(&json!({"cursor": cursor}));
                let event = data.map(|data| Event::default().event("reset").data(data));
                return Some((event, state));
            }
            if let Some(envelope) = state.pending.pop_front() {
                state.cursor = envelope.event_id;
                let event_id = envelope.event_id.to_string();
                let data = serde_json::to_string(&envelope);
                let event =
                    data.map(|data| Event::default().id(event_id).event("workspace").data(data));
                return Some((event, state));
            }

            tokio::select! {
                changed = state.shutdown.changed() => {
                    if changed.is_err() || *state.shutdown.borrow() {
                        return None;
                    }
                }
                () = tokio::time::sleep(Duration::from_millis(250)) => {
                    match state.store.replay_events(state.cursor).await {
                        Ok(EventReplay::Events { events, .. }) => {
                            state.pending = events.into();
                        }
                        Ok(EventReplay::Reset { cursor }) => {
                            state.reset = Some(cursor);
                        }
                        Err(error) => {
                            tracing::error!(error = %error, "event replay failed");
                            return None;
                        }
                    }
                }
            }
        }
    })
}

async fn api_not_found() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "not_found", "API route not found")
}

async fn mutation_origin_guard(
    State(state): State<AppState>,
    request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let method = request.method();
    let is_safe = method == Method::GET
        || method == Method::HEAD
        || method == Method::OPTIONS
        || method == Method::TRACE;
    if !is_safe {
        validate_origin(request.headers(), &state.config)?;
    }
    Ok(next.run(request).await)
}

async fn security_headers(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    response.headers_mut().insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CSP),
    );
    response.headers_mut().insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    response.headers_mut().insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    response
}

pub(crate) fn validate_origin(headers: &HeaderMap, config: &AppConfig) -> Result<(), ApiError> {
    let Some(origin) = headers.get(header::ORIGIN) else {
        if config.allow_missing_origin {
            return Ok(());
        }
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "forbidden_origin",
            "Origin header is required",
        ));
    };
    let host = headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .ok_or_else(forbidden_origin)?;
    let origin = origin
        .to_str()
        .ok()
        .and_then(|value| value.parse::<Uri>().ok())
        .ok_or_else(forbidden_origin)?;
    let valid_path = origin
        .path_and_query()
        .is_none_or(|value| value.as_str() == "/");
    let matches_host = origin
        .authority()
        .is_some_and(|authority| authority.as_str().eq_ignore_ascii_case(host));
    if origin.scheme().is_none() || !valid_path || !matches_host {
        return Err(forbidden_origin());
    }
    Ok(())
}

fn forbidden_origin() -> ApiError {
    ApiError::new(
        StatusCode::FORBIDDEN,
        "forbidden_origin",
        "Origin does not match Host",
    )
}

fn has_json_content_type(headers: &HeaderMap) -> bool {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("application/json"))
}

#[derive(Debug)]
pub(crate) struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
    current_revision: Option<u64>,
}

impl ApiError {
    pub(crate) const fn new(status: StatusCode, code: &'static str, message: &'static str) -> Self {
        Self {
            status,
            code,
            message,
            current_revision: None,
        }
    }

    pub(crate) const fn bad_request(message: &'static str) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "bad_request", message)
    }

    pub(crate) fn from_store(error: StoreError) -> Self {
        match error {
            StoreError::InvalidInput(_) | StoreError::InvalidDomain(_) => {
                Self::bad_request("request is invalid")
            }
            StoreError::NotFound { .. } => {
                Self::new(StatusCode::NOT_FOUND, "not_found", "resource not found")
            }
            StoreError::StaleRevision { current_revision } => Self {
                status: StatusCode::CONFLICT,
                code: "stale_revision",
                message: "resource revision is stale",
                current_revision: Some(current_revision),
            },
            StoreError::IllegalTransition { .. } => Self::new(
                StatusCode::CONFLICT,
                "illegal_transition",
                "job transition is not legal",
            ),
            StoreError::IdempotencyConflict => Self::new(
                StatusCode::CONFLICT,
                "idempotency_conflict",
                "idempotency key was reused with a different request",
            ),
            internal => {
                tracing::error!(error = %internal, "store operation failed");
                Self::new(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal",
                    "internal server error",
                )
            }
        }
    }
}

#[derive(Serialize)]
struct ErrorBody {
    error: &'static str,
    message: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    current_revision: Option<u64>,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(ErrorBody {
                error: self.code,
                message: self.message,
                current_revision: self.current_revision,
            }),
        )
            .into_response()
    }
}
