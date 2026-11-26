#[cfg(unix)]
mod unix {
    use std::io;
    use std::pin::Pin;
    use std::task::{Context, Poll, ready};
    use std::time::Duration;

    use axum::Json;
    use axum::Router;
    use axum::body::{BodyDataStream, Bytes};
    use axum::extract::{DefaultBodyLimit, Path, Request, State};
    use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
    use axum::response::{IntoResponse, Response};
    use axum::routing::{get, post};
    use futures::Stream;
    use serde::Deserialize;
    use serde_json::{Value, json};
    use tokio::io::{AsyncRead, ReadBuf};
    use tokio::sync::watch;
    use tokio::time::MissedTickBehavior;

    use crate::adapters::{
        AdapterError, InspectorAdapter, InspectorPairReceipt, InspectorRunReceipt,
        InspectorSampleSlot,
    };
    use crate::api::{AppState, validate_origin};
    use crate::domain::{EngineAvailability, JobStatus, Phase};
    use crate::store::{LegacyEngine, LegacyJobState, LegacyProjection, Store};

    const MAX_SAMPLE_BYTES: u64 = 16 * 1024 * 1024;
    const MAX_CRITIC_NOTE_CHARS: usize = 320;
    const MAX_CRITIC_CITATIONS: usize = 3;

    pub(super) fn router() -> Router<AppState> {
        Router::new()
            .route("/state", get(state))
            .route(
                "/samples/{slot}",
                post(upload).layer(DefaultBodyLimit::disable()),
            )
            .route("/run", post(run))
            .route("/reset", post(reset))
            .route("/report", get(report))
            .route("/review", post(review))
            .route("/critic", get(critic_status))
            .route("/critic/claim", post(critic_claim))
            .route("/critic/finish", post(critic_finish))
            .route("/critic/cancel", post(critic_cancel))
            .route("/pair", post(pair))
            .route("/disconnect", post(disconnect))
    }

    pub(super) async fn poll_once(adapter: &InspectorAdapter, store: &Store) {
        let projection = match adapter.state().await {
            Ok(case) => projection_from_case(case),
            Err(error) => down_projection(&error),
        };
        if let Err(error) = store.record_legacy_projection(&projection).await {
            tracing::warn!(%error, engine = "inspector", "legacy projection update failed");
        }
    }

    pub(super) async fn record_unavailable(store: &Store) {
        let projection = down_projection(&AdapterError::Unreachable);
        if let Err(error) = store.record_legacy_projection(&projection).await {
            tracing::warn!(%error, engine = "inspector", "legacy projection update failed");
        }
    }

    pub(super) async fn poll(
        adapter: InspectorAdapter,
        store: Store,
        interval: Duration,
        mut shutdown: watch::Receiver<bool>,
    ) {
        if *shutdown.borrow() {
            return;
        }
        poll_once(&adapter, &store).await;
        if interval.is_zero() {
            return;
        }
        let mut ticker = tokio::time::interval(interval);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
        ticker.tick().await;
        loop {
            tokio::select! {
                _ = ticker.tick() => poll_once(&adapter, &store).await,
                result = shutdown.changed() => {
                    if result.is_err() || *shutdown.borrow() {
                        break;
                    }
                }
            }
        }
    }

    async fn state(State(state): State<AppState>) -> Response {
        let Some(adapter) = state.inspector.as_ref() else {
            return down_state("legacy engine unreachable");
        };
        match adapter.state().await {
            Ok(case) => ready_state(case),
            Err(AdapterError::Unreachable) => down_state("legacy engine unreachable"),
            Err(AdapterError::Timeout) => down_state("legacy engine timed out"),
            Err(error) => WorkflowError::from_adapter(error).into_response(),
        }
    }

    async fn upload(
        State(state): State<AppState>,
        Path(slot): Path<String>,
        request: Request,
    ) -> Response {
        if let Some(response) = rejected_origin(request.headers(), &state) {
            return response;
        }
        let revision = match revision_header(request.headers()) {
            Ok(revision) => revision,
            Err(error) => return error.into_response(),
        };
        let content_length = match content_length(request.headers()) {
            Ok(content_length) => content_length,
            Err(error) => return error.into_response(),
        };
        if content_length > MAX_SAMPLE_BYTES {
            return WorkflowError::new(
                StatusCode::PAYLOAD_TOO_LARGE,
                "sample_too_large",
                "Inspector samples may not exceed 16 MiB",
            )
            .into_response();
        }
        let slot = match slot.as_str() {
            "a" => InspectorSampleSlot::A,
            "b" => InspectorSampleSlot::B,
            _ => {
                return WorkflowError::new(
                    StatusCode::BAD_REQUEST,
                    "invalid_slot",
                    "Inspector slot must be a or b",
                )
                .into_response();
            }
        };
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        let reader = BodyReader::new(request.into_body().into_data_stream());
        match map_result(
            adapter,
            adapter
                .upload_sample(revision, slot, content_length, reader)
                .await,
        )
        .await
        {
            Ok(case) => {
                record_case(&state, &case).await;
                Json(case).into_response()
            }
            Err(error) => error.into_response(),
        }
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct RunRequest {
        revision: u64,
        tool: String,
        arguments: Value,
    }

    async fn run(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<RunRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let (revision, tool, arguments) = match run_arguments(request) {
            Ok(arguments) => arguments,
            Err(error) => return error.into_response(),
        };
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(adapter, adapter.run(revision, tool, arguments).await).await {
            Ok(receipt) => {
                record_case(&state, &receipt.state).await;
                run_response(receipt)
            }
            Err(error) => error.into_response(),
        }
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct RevisionRequest {
        revision: u64,
    }

    async fn reset(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<RevisionRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(adapter, adapter.reset(request.revision).await).await {
            Ok(case) => {
                record_case(&state, &case).await;
                Json(case).into_response()
            }
            Err(error) => error.into_response(),
        }
    }

    async fn report(State(state): State<AppState>) -> Response {
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match adapter.report().await {
            Ok(report) => {
                let mut response = Json(report).into_response();
                response.headers_mut().insert(
                    header::CONTENT_DISPOSITION,
                    HeaderValue::from_static(
                        "attachment; filename=\"skellywix-re-kit-evidence.json\"",
                    ),
                );
                response
            }
            Err(error) => WorkflowError::from_adapter(error).into_response(),
        }
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ReviewRequest {
        revision: u64,
        reference: Value,
        critique: bool,
    }

    async fn review(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<ReviewRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let reference = match &request.reference {
            Value::Null => None,
            Value::String(reference) => Some(reference.as_str()),
            _ => {
                return WorkflowError::bad_request("Review reference must be a string or null")
                    .into_response();
            }
        };
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(
            adapter,
            adapter
                .review(request.revision, reference, request.critique)
                .await,
        )
        .await
        {
            Ok(case) => {
                record_case(&state, &case).await;
                Json(case).into_response()
            }
            Err(error) => error.into_response(),
        }
    }

    async fn critic_status(State(state): State<AppState>) -> Response {
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match adapter.critic_status().await {
            Ok(status) => Json(status).into_response(),
            Err(error) => WorkflowError::from_adapter(error).into_response(),
        }
    }

    async fn critic_claim(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<RevisionRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(adapter, adapter.critic_claim(request.revision).await).await {
            Ok(claim) => Json(claim).into_response(),
            Err(error) => error.into_response(),
        }
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct CriticFinishRequest {
        revision: u64,
        ticket: String,
        note: Value,
    }

    fn valid_critic_note(note: &Value) -> bool {
        let Some(fields) = note.as_object() else {
            return false;
        };
        let (Some(text), Some(cites)) = (
            fields.get("text").and_then(Value::as_str),
            fields.get("cites").and_then(Value::as_array),
        ) else {
            return false;
        };
        fields.len() == 2
            && text.chars().nth(MAX_CRITIC_NOTE_CHARS).is_none()
            && !text.trim().is_empty()
            && !text.chars().any(char::is_control)
            && (1..=MAX_CRITIC_CITATIONS).contains(&cites.len())
            && cites
                .iter()
                .enumerate()
                .all(|(i, cite)| cite.as_u64().is_some_and(|n| n > 0) && !cites[..i].contains(cite))
    }

    async fn critic_finish(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<CriticFinishRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let CriticFinishRequest {
            revision,
            ticket,
            note,
        } = request;
        let note = if note.is_null() {
            None
        } else if valid_critic_note(&note) {
            Some(note)
        } else {
            return WorkflowError::bad_request(
                "A critic note must be null or contain short text and one to three distinct finding numbers",
            )
            .into_response();
        };
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(
            adapter,
            adapter.critic_finish(revision, &ticket, note).await,
        )
        .await
        {
            Ok(status) => Json(status).into_response(),
            Err(error) => error.into_response(),
        }
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct CriticCancelRequest {
        revision: u64,
        end: u64,
    }

    async fn critic_cancel(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<CriticCancelRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(
            adapter,
            adapter.critic_cancel(request.revision, request.end).await,
        )
        .await
        {
            Ok(status) => Json(status).into_response(),
            Err(error) => error.into_response(),
        }
    }

    async fn pair(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<RevisionRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(adapter, adapter.pair(request.revision).await).await {
            Ok(receipt) => {
                record_case(&state, &receipt.state).await;
                pair_response(receipt)
            }
            Err(error) => error.into_response(),
        }
    }

    async fn disconnect(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<RevisionRequest>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        let Some(adapter) = state.inspector.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match map_result(adapter, adapter.disconnect(request.revision).await).await {
            Ok(case) => {
                record_case(&state, &case).await;
                Json(case).into_response()
            }
            Err(error) => error.into_response(),
        }
    }

    fn run_arguments(request: RunRequest) -> Result<(u64, &'static str, Value), WorkflowError> {
        let Some(arguments) = request.arguments.as_object() else {
            return Err(WorkflowError::bad_request(
                "Inspector tool arguments must be an object",
            ));
        };
        let tool = match request.tool.as_str() {
            "identify_file" => {
                if arguments.len() != 1 {
                    return Err(WorkflowError::bad_request(
                        "identify_file accepts only slot",
                    ));
                }
                required_slot(arguments.get("slot"))?;
                "identify_file"
            }
            "compare_builds" => {
                if !arguments.is_empty() {
                    return Err(WorkflowError::bad_request(
                        "compare_builds arguments must be empty",
                    ));
                }
                "compare_builds"
            }
            "verify_checksum" => {
                if arguments.len() != 2 {
                    return Err(WorkflowError::bad_request(
                        "verify_checksum requires only slot and sha256",
                    ));
                }
                required_slot(arguments.get("slot"))?;
                let digest = arguments
                    .get("sha256")
                    .and_then(Value::as_str)
                    .filter(|digest| {
                        digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
                    });
                if digest.is_none() {
                    return Err(WorkflowError::bad_request(
                        "verify_checksum sha256 must be a 64-character hexadecimal digest",
                    ));
                }
                "verify_checksum"
            }
            _ => {
                return Err(WorkflowError::bad_request(
                    "Inspector tool must be identify_file, compare_builds, or verify_checksum",
                ));
            }
        };
        Ok((request.revision, tool, request.arguments))
    }

    fn required_slot(slot: Option<&Value>) -> Result<&str, WorkflowError> {
        match slot.and_then(Value::as_str) {
            Some(slot @ ("a" | "b")) => Ok(slot),
            _ => Err(WorkflowError::bad_request("Inspector slot must be a or b")),
        }
    }

    async fn map_result<T>(
        adapter: &InspectorAdapter,
        result: Result<T, AdapterError>,
    ) -> Result<T, WorkflowError> {
        match result {
            Ok(value) => Ok(value),
            Err(AdapterError::Legacy { status: 409, code }) => {
                let current_revision = adapter
                    .state()
                    .await
                    .ok()
                    .and_then(|state| state.get("revision").and_then(Value::as_u64));
                Err(
                    WorkflowError::new(StatusCode::CONFLICT, "stale_revision", code)
                        .with_current_revision(current_revision),
                )
            }
            Err(AdapterError::Legacy { status: 400, code }) if code == "Add file A first." => Err(
                WorkflowError::new(StatusCode::BAD_REQUEST, "a_before_b", code),
            ),
            Err(error) => Err(WorkflowError::from_adapter(error)),
        }
    }

    fn revision_header(headers: &HeaderMap) -> Result<u64, WorkflowError> {
        headers
            .get("X-Workspace-Revision")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse().ok())
            .ok_or_else(|| {
                WorkflowError::bad_request("X-Workspace-Revision must be an unsigned integer")
            })
    }

    fn content_length(headers: &HeaderMap) -> Result<u64, WorkflowError> {
        headers
            .get(header::CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse().ok())
            .ok_or_else(|| {
                WorkflowError::new(
                    StatusCode::LENGTH_REQUIRED,
                    "content_length_required",
                    "Sample upload requires Content-Length",
                )
            })
    }

    fn projection_from_case(case: Value) -> LegacyProjection {
        LegacyProjection {
            engine: LegacyEngine::Inspector,
            availability: EngineAvailability::Up,
            detail: "available".to_owned(),
            normalized: case.clone(),
            job: inspector_job_state(&case),
            evidence: Some(case),
        }
    }

    fn down_projection(error: &AdapterError) -> LegacyProjection {
        let detail = match error {
            AdapterError::Timeout => "legacy engine timed out",
            AdapterError::Unreachable
            | AdapterError::TooLarge
            | AdapterError::Legacy { .. }
            | AdapterError::Protocol(_) => "legacy engine unreachable",
        };
        LegacyProjection {
            engine: LegacyEngine::Inspector,
            availability: EngineAvailability::Down,
            detail: detail.to_owned(),
            normalized: Value::Null,
            evidence: None,
            job: None,
        }
    }

    fn inspector_job_state(case: &Value) -> Option<LegacyJobState> {
        let has_samples = case
            .get("slots")
            .and_then(Value::as_object)
            .is_some_and(|slots| !slots.is_empty());
        let critique_pending = case
            .pointer("/advisor/critique/status")
            .and_then(Value::as_str)
            == Some("pending");
        let (status, phase) = if critique_pending {
            (JobStatus::AwaitingInput, "review")
        } else if has_samples {
            (JobStatus::Running, "inspect")
        } else {
            (JobStatus::Queued, "intake")
        };
        Some(LegacyJobState {
            legacy_key: "current".to_owned(),
            status,
            phase: Phase::new(phase).ok()?,
            verified: false,
        })
    }

    async fn record_case(state: &AppState, case: &Value) {
        let projection = projection_from_case(case.clone());
        if let Err(error) = state.store.record_legacy_projection(&projection).await {
            tracing::warn!(%error, engine = "inspector", "legacy mutation projection failed");
        }
    }

    fn ready_state(case: Value) -> Response {
        Json(json!({
            "schema_version": 1,
            "engine": {
                "state": "up",
                "detail": "available",
            },
            "state": case.clone(),
            "legacy_evidence": case,
        }))
        .into_response()
    }

    fn down_state(detail: &'static str) -> Response {
        Json(json!({
            "schema_version": 1,
            "engine": {
                "state": "down",
                "detail": detail,
            },
            "state": Value::Null,
            "legacy_evidence": Value::Null,
        }))
        .into_response()
    }

    fn run_response(receipt: InspectorRunReceipt) -> Response {
        Json(json!({
            "result": receipt.result,
            "state": receipt.state,
        }))
        .into_response()
    }

    fn pair_response(receipt: InspectorPairReceipt) -> Response {
        Json(json!({
            "token": receipt.token,
            "state": receipt.state,
        }))
        .into_response()
    }

    fn rejected_origin(headers: &HeaderMap, state: &AppState) -> Option<Response> {
        validate_origin(headers, &state.config)
            .err()
            .map(IntoResponse::into_response)
    }

    struct BodyReader {
        stream: BodyDataStream,
        chunk: Bytes,
        offset: usize,
    }

    impl BodyReader {
        fn new(stream: BodyDataStream) -> Self {
            Self {
                stream,
                chunk: Bytes::new(),
                offset: 0,
            }
        }
    }

    impl AsyncRead for BodyReader {
        fn poll_read(
            mut self: Pin<&mut Self>,
            context: &mut Context<'_>,
            buffer: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            loop {
                if self.offset < self.chunk.len() {
                    let available = &self.chunk[self.offset..];
                    let count = available.len().min(buffer.remaining());
                    buffer.put_slice(&available[..count]);
                    self.offset += count;
                    return Poll::Ready(Ok(()));
                }
                match ready!(Pin::new(&mut self.stream).poll_next(context)) {
                    Some(Ok(chunk)) => {
                        self.chunk = chunk;
                        self.offset = 0;
                    }
                    Some(Err(error)) => return Poll::Ready(Err(io::Error::other(error))),
                    None => return Poll::Ready(Ok(())),
                }
            }
        }
    }

    struct WorkflowError {
        status: StatusCode,
        code: &'static str,
        message: String,
        current_revision: Option<u64>,
    }

    impl WorkflowError {
        fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
            Self {
                status,
                code,
                message: message.into(),
                current_revision: None,
            }
        }

        fn bad_request(message: impl Into<String>) -> Self {
            Self::new(StatusCode::BAD_REQUEST, "bad_request", message)
        }

        fn engine_down() -> Self {
            Self::new(
                StatusCode::BAD_GATEWAY,
                "engine_unavailable",
                "Inspector engine is not available",
            )
        }

        fn with_current_revision(mut self, current_revision: Option<u64>) -> Self {
            self.current_revision = current_revision;
            self
        }

        fn from_adapter(error: AdapterError) -> Self {
            match error {
                AdapterError::Unreachable | AdapterError::Timeout => Self::engine_down(),
                AdapterError::TooLarge => Self::new(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "sample_too_large",
                    "Inspector request exceeded its configured limit",
                ),
                AdapterError::Legacy { status, code } => Self::new(
                    StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
                    "legacy_rejected",
                    code,
                ),
                AdapterError::Protocol(_) => Self::new(
                    StatusCode::BAD_GATEWAY,
                    "legacy_protocol_error",
                    "Inspector engine returned an invalid response",
                ),
            }
        }
    }

    impl IntoResponse for WorkflowError {
        fn into_response(self) -> Response {
            let mut body = json!({
                "error": self.code,
                "message": self.message,
            });
            if let Some(current_revision) = self.current_revision {
                body["current_revision"] = Value::from(current_revision);
            }
            (self.status, Json(body)).into_response()
        }
    }
}

#[cfg(unix)]
pub fn router() -> axum::Router<crate::api::AppState> {
    unix::router()
}

#[cfg(unix)]
pub async fn poll_once(adapter: &crate::adapters::InspectorAdapter, store: &crate::store::Store) {
    unix::poll_once(adapter, store).await;
}

#[cfg(unix)]
pub async fn record_unavailable(store: &crate::store::Store) {
    unix::record_unavailable(store).await;
}

#[cfg(unix)]
pub async fn poll(
    adapter: crate::adapters::InspectorAdapter,
    store: crate::store::Store,
    interval: std::time::Duration,
    shutdown: tokio::sync::watch::Receiver<bool>,
) {
    unix::poll(adapter, store, interval, shutdown).await;
}

#[cfg(not(unix))]
pub fn router() -> axum::Router<crate::api::AppState> {
    use axum::Json;
    use axum::routing::get;
    use serde_json::json;

    axum::Router::new().route(
        "/state",
        get(|| async {
            Json(json!({
                "schema_version": 1,
                "engine": {
                    "state": "down",
                    "detail": "legacy engine unreachable",
                },
                "state": null,
                "legacy_evidence": null,
            }))
        }),
    )
}
