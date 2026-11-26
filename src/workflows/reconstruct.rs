#[cfg(unix)]
mod unix {
    use std::time::Duration;

    use axum::Json;
    use axum::Router;
    use axum::body::Body;
    use axum::extract::{Path, Query, State};
    use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
    use axum::response::{IntoResponse, Response};
    use axum::routing::{get, post};
    use serde::{Deserialize, Serialize};
    use serde_json::{Value, json};

    use tokio::sync::watch;
    use tokio::time::MissedTickBehavior;

    use crate::adapters::{
        AdapterError, FourmilabAction, FourmilabActionReceipt, FourmilabAdapter,
    };
    use crate::api::{AppState, validate_origin};
    use crate::domain::{EngineAvailability, JobStatus, Phase};
    use crate::store::{LegacyEngine, LegacyJobState, LegacyProjection, Store};

    const REQUIRED_CASES: u64 = 2_557;
    const REQUESTED_MODEL: &str = "openai-codex/gpt-6.2-astra";
    const STAGES: [&str; 5] = ["intake", "analyze", "rebuild", "verify", "package"];

    pub(super) fn router() -> Router<AppState> {
        Router::new()
            .route("/state", get(state))
            .route("/start", post(start))
            .route("/runs/{run}/cancel", post(cancel))
            .route("/runs/{run}/retry", post(retry))
            .route("/runs/{run}/package", get(package))
            .route("/releases/stage", post(stage_release))
            .route("/releases/promote", post(promote_release))
            .route("/releases/rollback", post(rollback_release))
    }

    pub(super) async fn poll_once(adapter: &FourmilabAdapter, store: &Store) {
        let projection = match adapter.state(None).await {
            Ok(raw) => projection_from_state(raw),
            Err(error) => down_projection(&error),
        };
        if let Err(error) = store.record_legacy_projection(&projection).await {
            tracing::warn!(%error, engine = "fourmilab", "legacy projection update failed");
        }
    }

    pub(super) async fn record_unavailable(store: &Store) {
        let projection = down_projection(&AdapterError::Unreachable);
        if let Err(error) = store.record_legacy_projection(&projection).await {
            tracing::warn!(%error, engine = "fourmilab", "legacy projection update failed");
        }
    }

    pub(super) async fn poll(
        adapter: FourmilabAdapter,
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

    #[derive(Debug, Deserialize)]
    struct StateQuery {
        run: Option<String>,
    }

    async fn state(State(state): State<AppState>, Query(query): Query<StateQuery>) -> Response {
        let Some(adapter) = state.fourmilab.as_ref() else {
            return down_state("legacy engine unreachable");
        };
        match adapter.state(query.run.as_deref()).await {
            Ok(raw) => Json(project_state(raw)).into_response(),
            Err(AdapterError::Unreachable) => down_state("legacy engine unreachable"),
            Err(AdapterError::Timeout) => down_state("legacy engine timed out"),
            Err(error) => WorkflowError::from_adapter(error).into_response(),
        }
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct EmptyObject {}

    async fn start(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(_request): Json<EmptyObject>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        perform_action(&state, FourmilabAction::Start).await
    }

    async fn cancel(
        State(state): State<AppState>,
        Path(run): Path<String>,
        headers: HeaderMap,
        Json(_request): Json<EmptyObject>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        perform_action(&state, FourmilabAction::Cancel { run }).await
    }

    async fn retry(
        State(state): State<AppState>,
        Path(run): Path<String>,
        headers: HeaderMap,
        Json(_request): Json<EmptyObject>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        perform_action(&state, FourmilabAction::Retry { run }).await
    }

    async fn stage_release(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(_request): Json<EmptyObject>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        perform_action(&state, FourmilabAction::Stage).await
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ConfirmedTarget {
        target_digest: String,
        confirm_target: String,
    }

    async fn promote_release(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<ConfirmedTarget>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        if request.target_digest != request.confirm_target
            || !is_lower_hex(&request.target_digest, 64)
        {
            return invalid_target().into_response();
        }
        perform_action(
            &state,
            FourmilabAction::Promote {
                digest: request.target_digest,
            },
        )
        .await
    }

    async fn rollback_release(
        State(state): State<AppState>,
        headers: HeaderMap,
        Json(request): Json<ConfirmedTarget>,
    ) -> Response {
        if let Some(response) = rejected_origin(&headers, &state) {
            return response;
        }
        if request.target_digest != request.confirm_target
            || !is_lower_hex(&request.target_digest, 64)
        {
            return invalid_target().into_response();
        }
        let Some(adapter) = state.fourmilab.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        let raw = match adapter.state(None).await {
            Ok(raw) => raw,
            Err(error) => return WorkflowError::from_adapter(error).into_response(),
        };
        let previous = raw.pointer("/releases/previous").and_then(Value::as_str);
        if previous != Some(request.target_digest.as_str()) {
            return WorkflowError::new(
                StatusCode::CONFLICT,
                "illegal_transition",
                "Rollback target is not the exact previous release",
            )
            .into_response();
        }
        perform_action(&state, FourmilabAction::Rollback).await
    }

    async fn package(State(state): State<AppState>, Path(run): Path<String>) -> Response {
        let Some(adapter) = state.fourmilab.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        let raw = match adapter.state(Some(&run)).await {
            Ok(raw) => raw,
            Err(error) => return WorkflowError::from_adapter(error).into_response(),
        };
        if !has_verified_package(&raw, &run) {
            return WorkflowError::new(
                StatusCode::CONFLICT,
                "illegal_transition",
                "Package download requires a verified legacy receipt",
            )
            .into_response();
        }
        let stream = match adapter.package_stream(&run).await {
            Ok(stream) => stream,
            Err(error) => return WorkflowError::from_adapter(error).into_response(),
        };
        let mut response = Body::from_stream(stream).into_response();
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/zip"),
        );
        response.headers_mut().insert(
            header::CONTENT_DISPOSITION,
            HeaderValue::from_static("attachment; filename=\"fourmilab-base64-verified.zip\""),
        );
        response
    }

    async fn perform_action(state: &AppState, action: FourmilabAction) -> Response {
        let Some(adapter) = state.fourmilab.as_ref() else {
            return WorkflowError::engine_down().into_response();
        };
        match adapter.action(action).await {
            Ok(receipt) => action_receipt(receipt),
            Err(error) => WorkflowError::from_adapter(error).into_response(),
        }
    }

    #[derive(Serialize)]
    struct ActionResponse {
        ok: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        run: Option<String>,
    }

    fn action_receipt(receipt: FourmilabActionReceipt) -> Response {
        Json(ActionResponse {
            ok: receipt.ok,
            run: receipt.run,
        })
        .into_response()
    }

    fn project_state(raw: Value) -> Value {
        let selected_run = raw.get("run").filter(|run| !run.is_null());
        let runs = project_runs(raw.get("runs"), selected_run);
        let selected_run = selected_run.map(project_run);
        let releases = raw.get("releases").cloned().unwrap_or(Value::Null);
        let catalog = raw.get("catalog").cloned().unwrap_or_else(|| json!([]));
        let busy = raw.get("busy").and_then(Value::as_bool).unwrap_or(false);
        let running = raw.get("running").cloned().unwrap_or(Value::Null);
        let update_error = raw.get("update_error").cloned().unwrap_or(Value::Null);
        let builder_connected = raw
            .get("builder_connected")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        json!({
            "schema_version": 1,
            "engine": {
                "state": "up",
                "detail": "available",
            },
            "profile": {
                "id": "fourmilab_base64_1_5",
                "name": "Fourmilab Base64 1.5",
                "fixed": true,
                "required_cases": REQUIRED_CASES,
            },
            "runs": runs,
            "selected_run": selected_run,
            "releases": releases,
            "catalog": catalog,
            "busy": busy,
            "running": running,
            "update_error": update_error,
            "builder_connected": builder_connected,
            "legacy_evidence": raw,
        })
    }

    fn projection_from_state(raw: Value) -> LegacyProjection {
        let mut normalized = project_state(raw.clone());
        if let Some(object) = normalized.as_object_mut() {
            object.remove("legacy_evidence");
        }
        LegacyProjection {
            engine: LegacyEngine::Fourmilab,
            availability: EngineAvailability::Up,
            detail: "available".to_owned(),
            normalized,
            job: reconstruct_job_state(&raw),
            evidence: Some(raw),
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
            engine: LegacyEngine::Fourmilab,
            availability: EngineAvailability::Down,
            detail: detail.to_owned(),
            normalized: Value::Null,
            evidence: None,
            job: None,
        }
    }

    fn reconstruct_job_state(raw: &Value) -> Option<LegacyJobState> {
        let run = raw.get("run")?.as_object()?;
        let legacy_key = run.get("id")?.as_str()?.to_owned();
        let legacy_status = run.get("status")?.as_str()?;
        let status = match legacy_status {
            "queued" => JobStatus::Queued,
            "starting" => JobStatus::Starting,
            "running" | "building" => JobStatus::Running,
            "awaiting_input" => JobStatus::AwaitingInput,
            "blocked" => JobStatus::Blocked,
            "cancelling" => JobStatus::Cancelling,
            "cancelled" => JobStatus::Cancelled,
            "failed" => JobStatus::Failed,
            "verified" | "succeeded" => JobStatus::Succeeded,
            "interrupted" => JobStatus::Interrupted,
            _ => return None,
        };
        let run = Value::Object(run.clone());
        let phase = current_stage(&run).unwrap_or(legacy_status);
        Some(LegacyJobState {
            verified: has_verified_run(&run, &legacy_key),
            legacy_key,
            status,
            phase: Phase::new(phase).ok()?,
        })
    }

    fn project_runs(runs: Option<&Value>, selected: Option<&Value>) -> Value {
        let Some(runs) = runs.and_then(Value::as_array) else {
            return json!([]);
        };
        Value::Array(
            runs.iter()
                .map(|run| {
                    let id = run.get("id").and_then(Value::as_str);
                    let selected_stage = selected
                        .filter(|selected| selected.get("id").and_then(Value::as_str) == id)
                        .and_then(current_stage);
                    let stage = selected_stage
                        .or_else(|| run.get("stage").and_then(Value::as_str))
                        .or_else(|| run.get("status").and_then(Value::as_str));
                    json!({
                        "id": run.get("id").cloned().unwrap_or(Value::Null),
                        "attempt": run.get("attempt").cloned().unwrap_or(Value::Null),
                        "status": run.get("status").cloned().unwrap_or(Value::Null),
                        "stage": stage,
                    })
                })
                .collect(),
        )
    }

    fn project_run(run: &Value) -> Value {
        let events = run
            .get("events")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or_default();
        let attempt = run.get("attempt").and_then(Value::as_u64);
        let manifest = run.get("manifest").unwrap_or(&Value::Null);
        let proof_evidence = events
            .iter()
            .rev()
            .filter(|event| event.get("attempt").and_then(Value::as_u64) == attempt)
            .find_map(|event| event.pointer("/detail/evidence"))
            .cloned()
            .unwrap_or(Value::Null);
        let required = manifest
            .get("required_cases")
            .and_then(Value::as_u64)
            .or_else(|| proof_evidence.get("required").and_then(Value::as_u64))
            .unwrap_or(REQUIRED_CASES);
        let matched = proof_evidence.get("matched").and_then(Value::as_u64);
        let actual_model = events
            .iter()
            .rev()
            .filter(|event| event.get("attempt").and_then(Value::as_u64) == attempt)
            .find_map(|event| {
                event
                    .pointer("/detail/actual_model")
                    .and_then(Value::as_str)
            });
        let package_event = attempt.and_then(|attempt| latest_stage(events, "package", attempt));
        let run_id = run.get("id").and_then(Value::as_str).unwrap_or_default();
        let package_available = has_verified_run(run, run_id);
        let package_sha256 = package_event
            .and_then(|event| event.pointer("/detail/package_sha256"))
            .cloned()
            .unwrap_or(Value::Null);
        let stages = STAGES
            .iter()
            .map(|stage| {
                let event = attempt.and_then(|attempt| latest_stage(events, stage, attempt));
                event.map_or_else(
                    || {
                        json!({
                            "name": stage,
                            "status": "pending",
                            "completed": Value::Null,
                            "required": Value::Null,
                            "detail": {},
                        })
                    },
                    |event| {
                        let completed = event
                            .pointer("/detail/completed")
                            .or_else(|| event.pointer("/detail/evidence/matched"))
                            .cloned()
                            .unwrap_or(Value::Null);
                        let required = event
                            .pointer("/detail/required")
                            .or_else(|| event.pointer("/detail/evidence/required"))
                            .cloned()
                            .unwrap_or(Value::Null);
                        json!({
                            "name": stage,
                            "status": event.get("status").cloned().unwrap_or(Value::Null),
                            "completed": completed,
                            "required": required,
                            "detail": event.get("detail").cloned().unwrap_or_else(|| json!({})),
                        })
                    },
                )
            })
            .collect::<Vec<_>>();
        json!({
            "id": run.get("id").cloned().unwrap_or(Value::Null),
            "attempt": run.get("attempt").cloned().unwrap_or(Value::Null),
            "status": run.get("status").cloned().unwrap_or(Value::Null),
            "stage": current_stage(run),
            "stages": stages,
            "proof": {
                "matched": matched,
                "required": required,
                "verified": package_available,
            },
            "model": {
                "requested": manifest
                    .get("requested_model")
                    .cloned()
                    .unwrap_or_else(|| json!(REQUESTED_MODEL)),
                "actual": actual_model,
            },
            "package": {
                "available": package_available,
                "sha256": package_sha256,
            },
        })
    }

    fn current_stage(run: &Value) -> Option<&str> {
        let attempt = run.get("attempt").and_then(Value::as_u64)?;
        run.get("events")?
            .as_array()?
            .iter()
            .rev()
            .find(|event| event.get("attempt").and_then(Value::as_u64) == Some(attempt))
            .and_then(|event| event.get("stage"))
            .and_then(Value::as_str)
    }

    fn latest_stage<'a>(events: &'a [Value], stage: &str, attempt: u64) -> Option<&'a Value> {
        events.iter().rev().find(|event| {
            event.get("stage").and_then(Value::as_str) == Some(stage)
                && event.get("attempt").and_then(Value::as_u64) == Some(attempt)
        })
    }

    fn has_verified_package(raw: &Value, expected_run: &str) -> bool {
        raw.get("run")
            .is_some_and(|run| has_verified_run(run, expected_run))
    }

    fn has_verified_run(run: &Value, expected_run: &str) -> bool {
        let Some(attempt) = run.get("attempt").and_then(Value::as_u64) else {
            return false;
        };
        if run.get("id").and_then(Value::as_str) != Some(expected_run)
            || run.get("status").and_then(Value::as_str) != Some("verified")
        {
            return false;
        }
        let events = run
            .get("events")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or_default();
        let Some(package) = latest_stage(events, "package", attempt) else {
            return false;
        };
        let evidence = package.pointer("/detail/evidence");
        package.get("status").and_then(Value::as_str) == Some("verified")
            && evidence
                .and_then(|value| value.get("run"))
                .and_then(Value::as_str)
                == Some(expected_run)
            && evidence
                .and_then(|value| value.get("attempt"))
                .and_then(Value::as_u64)
                == Some(attempt)
            && evidence
                .and_then(|value| value.get("required"))
                .and_then(Value::as_u64)
                == Some(REQUIRED_CASES)
            && evidence
                .and_then(|value| value.get("matched"))
                .and_then(Value::as_u64)
                == Some(REQUIRED_CASES)
            && evidence
                .and_then(|value| value.get("manifest_sha256"))
                .and_then(Value::as_str)
                == run.get("manifest_hash").and_then(Value::as_str)
    }

    fn down_state(detail: &'static str) -> Response {
        Json(json!({
            "schema_version": 1,
            "engine": {
                "state": "down",
                "detail": detail,
            },
            "profile": {
                "id": "fourmilab_base64_1_5",
                "name": "Fourmilab Base64 1.5",
                "fixed": true,
                "required_cases": REQUIRED_CASES,
            },
            "runs": Value::Null,
            "selected_run": Value::Null,
            "releases": Value::Null,
            "catalog": Value::Null,
            "busy": Value::Null,
            "running": Value::Null,
            "update_error": Value::Null,
            "builder_connected": Value::Null,
            "legacy_evidence": Value::Null,
        }))
        .into_response()
    }

    fn rejected_origin(headers: &HeaderMap, state: &AppState) -> Option<Response> {
        validate_origin(headers, &state.config)
            .err()
            .map(IntoResponse::into_response)
    }

    fn invalid_target() -> WorkflowError {
        WorkflowError::new(
            StatusCode::BAD_REQUEST,
            "bad_request",
            "Release target and confirmation must be the same lowercase digest",
        )
    }

    fn is_lower_hex(value: &str, length: usize) -> bool {
        value.len() == length
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }

    struct WorkflowError {
        status: StatusCode,
        code: &'static str,
        message: String,
    }

    impl WorkflowError {
        fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
            Self {
                status,
                code,
                message: message.into(),
            }
        }

        fn engine_down() -> Self {
            Self::new(
                StatusCode::BAD_GATEWAY,
                "engine_unavailable",
                "Fourmilab engine is not available",
            )
        }

        fn from_adapter(error: AdapterError) -> Self {
            match error {
                AdapterError::Unreachable | AdapterError::Timeout => Self::engine_down(),
                AdapterError::TooLarge => Self::new(
                    StatusCode::BAD_GATEWAY,
                    "legacy_response_too_large",
                    "Fourmilab engine response exceeded its configured limit",
                ),
                AdapterError::Legacy { status, code } => Self::new(
                    StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
                    "legacy_rejected",
                    code,
                ),
                AdapterError::Protocol(_) => Self::new(
                    StatusCode::BAD_GATEWAY,
                    "legacy_protocol_error",
                    "Fourmilab engine returned an invalid response",
                ),
            }
        }
    }

    impl IntoResponse for WorkflowError {
        fn into_response(self) -> Response {
            (
                self.status,
                Json(json!({
                    "error": self.code,
                    "message": self.message,
                })),
            )
                .into_response()
        }
    }
}

#[cfg(unix)]
pub fn router() -> axum::Router<crate::api::AppState> {
    unix::router()
}

#[cfg(unix)]
pub async fn poll_once(adapter: &crate::adapters::FourmilabAdapter, store: &crate::store::Store) {
    unix::poll_once(adapter, store).await;
}

#[cfg(unix)]
pub async fn record_unavailable(store: &crate::store::Store) {
    unix::record_unavailable(store).await;
}

#[cfg(unix)]
pub async fn poll(
    adapter: crate::adapters::FourmilabAdapter,
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
                "profile": {
                    "id": "fourmilab_base64_1_5",
                    "name": "Fourmilab Base64 1.5",
                    "fixed": true,
                    "required_cases": 2557,
                },
                "runs": null,
                "selected_run": null,
                "releases": null,
                "catalog": null,
                "busy": null,
                "running": null,
                "update_error": null,
                "builder_connected": null,
                "legacy_evidence": null,
            }))
        }),
    )
}
