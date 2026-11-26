//! Reviewed source only. No provider, compiler, command runner, or release selector is reachable here.
use axum::{
    Extension, Json, Router,
    extract::{DefaultBodyLimit, Path, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{Sqlite, Transaction};

use crate::{
    api::{ApiError, AppState},
    artifacts::Sha256Id,
    jobs::{
        MAX_SOURCE, PROJECTS, PortResult, Profile, ProjectSummary, SourcePortRoot, blocked,
        disk::{valid_attempt, valid_source},
        evidence::Reading,
        missing, unavailable,
    },
    store::{
        Store, StoreError,
        source_port::{self as control, Selected},
    },
};

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/projects", get(projects))
        .route("/projects/{id}", get(project))
        .route("/projects/{id}/packet", get(packet))
        .route(
            "/projects/{id}/proposals",
            post(propose).layer(DefaultBodyLimit::max(6 * MAX_SOURCE + 4096)),
        )
        .route(
            "/projects/{id}/compare",
            post(compare).layer(DefaultBodyLimit::max(4096)),
        )
        .route("/projects/{id}/attempts/{attempt}/source", get(download))
        .layer(middleware::from_fn(no_store))
}

pub(super) async fn no_store(request: axum::extract::Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        axum::http::HeaderValue::from_static("no-store"),
    );
    response
}
pub(super) fn configured(root: Option<Extension<SourcePortRoot>>) -> PortResult<SourcePortRoot> {
    root.map(|Extension(r)| r)
        .ok_or_else(|| unavailable("The reviewed-source registry is not configured."))
}

#[derive(Serialize)]
struct Projects {
    schema_version: u8,
    projects: Vec<ProjectSummary>,
}
async fn projects(root: Option<Extension<SourcePortRoot>>) -> PortResult<Json<Projects>> {
    let root = configured(root)?;
    root.registry_ready().await?;
    let mut projects = Vec::with_capacity(PROJECTS.len());
    for id in PROJECTS {
        if let Some(p) = root.installed(id).await? {
            projects.push(p.profile.into_summary());
        }
    }
    Ok(Json(Projects {
        schema_version: 1,
        projects,
    }))
}
#[derive(Serialize)]
struct Packet {
    schema_version: u8,
    project_id: String,
    source_sha256: Sha256Id,
    language: String,
    source: String,
    brief: String,
}
async fn packet(
    root: Option<Extension<SourcePortRoot>>,
    Path(id): Path<String>,
) -> PortResult<Response> {
    let root = configured(root)?;
    let installed = root.required_project(&id).await?;
    let p = installed.profile;
    let brief = root.brief(&p).await?;
    Ok((
        [(
            header::CONTENT_DISPOSITION,
            "attachment; filename=\"proposal-packet.json\"",
        )],
        Json(Packet {
            schema_version: 1,
            project_id: p.id,
            source_sha256: p.source_sha256,
            language: p.language,
            source: installed.original,
            brief,
        }),
    )
        .into_response())
}

#[derive(Serialize)]
struct Attempt {
    id: String,
    candidate_sha256: Sha256Id,
    rust_source: String,
    approved: bool,
}
#[derive(Serialize)]
struct Detail {
    schema_version: u8,
    project: ProjectSummary,
    revision: u64,
    phase: &'static str,
    attempt: Option<Attempt>,
    matched: Option<usize>,
    required: usize,
    blocker: Option<String>,
    export_available: bool,
}

fn db(e: sqlx::Error) -> ApiError {
    ApiError::from_store(StoreError::Database(e))
}
fn stale(current_revision: u64) -> ApiError {
    ApiError::from_store(StoreError::StaleRevision { current_revision })
}
fn revision(s: Option<&Selected>) -> u64 {
    s.map_or(0, |s| s.revision)
}

async fn detail(
    store: &Store,
    root: &SourcePortRoot,
    p: Profile,
    wanted: Option<&str>,
) -> PortResult<Json<Detail>> {
    let mut tx = store.pool().begin().await.map_err(db)?;
    let selected = control::current(&mut tx, &p.id)
        .await
        .map_err(ApiError::from_store)?;
    let revision = revision(selected.as_ref());
    if let Some(id) = wanted
        && selected.as_ref().map(|s| s.target.attempt_id.as_str()) != Some(id)
    {
        return Err(stale(revision));
    }
    let required = p.required_cases;
    let (phase, attempt, matched, blocker, export_available) = if let Some(s) = selected {
        let source = root.candidate(&s.target).await?;
        let r = root.reading(&p, &s, &source).await.unwrap_or_else(|_| {
            Reading::blocked(
                "The protected approval or verifier evidence cannot be validated.",
                false,
            )
        });
        let a = Attempt {
            id: s.target.attempt_id,
            candidate_sha256: s.target.candidate_sha256,
            rust_source: source,
            approved: r.approved,
        };
        (r.phase, Some(a), r.matched, r.blocker, r.export)
    } else {
        ("registered", None, None, None, false)
    };
    tx.commit().await.map_err(db)?;
    Ok(Json(Detail {
        schema_version: 1,
        project: p.into_summary(),
        revision,
        phase,
        attempt,
        matched,
        required,
        blocker,
        export_available,
    }))
}
async fn project(
    State(state): State<AppState>,
    root: Option<Extension<SourcePortRoot>>,
    Path(id): Path<String>,
) -> PortResult<Json<Detail>> {
    let root = configured(root)?;
    let p = root.required_project(&id).await?.profile;
    detail(&state.store, &root, p, None).await
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Proposal {
    expected_revision: u64,
    source_sha256: Sha256Id,
    rust_source: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Compare {
    expected_revision: u64,
    attempt_id: String,
    candidate_sha256: Sha256Id,
}

fn json_error(e: JsonRejection) -> ApiError {
    if e.status() == StatusCode::PAYLOAD_TOO_LARGE {
        ApiError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "payload_too_large",
            "The request exceeds the source-port size limit.",
        )
    } else {
        ApiError::bad_request(
            "The request must contain only the fields required by this operation.",
        )
    }
}
fn key(headers: &HeaderMap) -> PortResult<&str> {
    let key = headers
        .get("Idempotency-Key")
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| ApiError::bad_request("Idempotency-Key is required."))?;
    control::validate_key(key).map_err(ApiError::from_store)?;
    Ok(key)
}
fn fingerprint(fields: impl Serialize) -> PortResult<String> {
    let bytes =
        serde_json::to_vec(&fields).map_err(|_| ApiError::from_store(StoreError::CorruptData))?;
    Ok(hex::encode(Sha256::digest(bytes)))
}
async fn writer(store: &Store) -> PortResult<Transaction<'static, Sqlite>> {
    store.pool().begin_with("BEGIN IMMEDIATE").await.map_err(db)
}

/// Check idempotency before the expected revision; neither a retry nor an old key can reselect an attempt.
async fn intent(
    tx: &mut Transaction<'_, Sqlite>,
    project: &str,
    key: &str,
    hash: &str,
    expected: u64,
) -> PortResult<(Option<Selected>, bool)> {
    let saved = control::current(tx, project)
        .await
        .map_err(ApiError::from_store)?;
    let current = revision(saved.as_ref());
    if let Some(prior) = control::replay(tx, key, hash)
        .await
        .map_err(ApiError::from_store)?
    {
        if saved.as_ref().is_some_and(|s| s.target.attempt_id == prior) {
            return Ok((saved, true));
        }
        return Err(stale(current));
    }
    if expected != current {
        return Err(stale(current));
    }
    Ok((saved, false))
}

async fn propose(
    State(state): State<AppState>,
    root: Option<Extension<SourcePortRoot>>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Result<Json<Proposal>, JsonRejection>,
) -> PortResult<Json<Detail>> {
    let root = configured(root)?;
    let key = key(&headers)?;
    let Json(body) = body.map_err(json_error)?;
    if body.rust_source.len() > MAX_SOURCE {
        return Err(ApiError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "payload_too_large",
            "Rust source exceeds 256 KiB.",
        ));
    }
    if !valid_source(body.rust_source.as_bytes()) {
        return Err(ApiError::bad_request(
            "Rust source must be nonempty UTF-8 text without NUL bytes.",
        ));
    }
    let p = root.required_project(&id).await?.profile;
    let digest = Sha256Id::of(body.rust_source.as_bytes());
    let hash = fingerprint((
        "source-port/propose",
        &id,
        body.expected_revision,
        body.source_sha256,
        digest,
    ))?;
    let mut tx = writer(&state.store).await?;
    let (selected, replay) = intent(&mut tx, &id, key, &hash, body.expected_revision).await?;
    let attempt = if replay {
        selected.ok_or_else(missing)?.target.attempt_id
    } else {
        if p.source_sha256 != body.source_sha256 {
            return Err(blocked("The proposal names a different original source."));
        }
        let t = p.target(
            control::next_id(&mut tx)
                .await
                .map_err(ApiError::from_store)?,
            digest,
        );
        control::record_proposal(&mut tx, &p, &t, body.expected_revision)
            .await
            .map_err(ApiError::from_store)?;
        control::remember(&mut tx, key, &hash, &t.attempt_id)
            .await
            .map_err(ApiError::from_store)?;
        root.freeze(&t, body.rust_source.as_bytes()).await?;
        t.attempt_id
    };
    tx.commit().await.map_err(db)?;
    detail(&state.store, &root, p, Some(&attempt)).await
}

async fn compare(
    State(state): State<AppState>,
    root: Option<Extension<SourcePortRoot>>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Result<Json<Compare>, JsonRejection>,
) -> PortResult<Json<Detail>> {
    let root = configured(root)?;
    let key = key(&headers)?;
    let Json(body) = body.map_err(json_error)?;
    if !valid_attempt(&body.attempt_id) {
        return Err(ApiError::bad_request("Invalid attempt ID."));
    }
    let p = root.required_project(&id).await?.profile;
    let hash = fingerprint((
        "source-port/compare",
        &id,
        body.expected_revision,
        &body.attempt_id,
        body.candidate_sha256,
    ))?;
    let mut tx = writer(&state.store).await?;
    let (s, replay) = intent(&mut tx, &id, key, &hash, body.expected_revision).await?;
    let s = s.ok_or_else(|| blocked("No proposal is selected."))?;
    if s.target.attempt_id != body.attempt_id || s.target.candidate_sha256 != body.candidate_sha256
    {
        return Err(blocked(
            "The comparison does not name the selected proposal.",
        ));
    }
    root.authorize_compare(&p, &s).await?;
    if !replay {
        control::record_compare(&mut tx, &s)
            .await
            .map_err(ApiError::from_store)?;
        control::remember(&mut tx, key, &hash, &body.attempt_id)
            .await
            .map_err(ApiError::from_store)?;
    }
    tx.commit().await.map_err(db)?;

    // Queue publication follows the durable intent. A retry can finish an interrupted publication,
    // but cannot publish a superseded or revoked candidate. No GET or background task can publish.
    let mut tx = writer(&state.store).await?;
    let current = control::current(&mut tx, &id)
        .await
        .map_err(ApiError::from_store)?
        .ok_or_else(missing)?;
    if !current.requested || current.target != s.target {
        return Err(stale(current.revision));
    }
    let p = root.required_project(&id).await?.profile;
    root.authorize_compare(&p, &current).await?;
    root.publish(&current.target).await?;
    tx.commit().await.map_err(db)?;
    detail(&state.store, &root, p, Some(&body.attempt_id)).await
}

async fn download(
    State(state): State<AppState>,
    root: Option<Extension<SourcePortRoot>>,
    Path((id, attempt)): Path<(String, String)>,
) -> PortResult<Response> {
    if !valid_attempt(&attempt) {
        return Err(missing());
    }
    let root = configured(root)?;
    let mut tx = state.store.pool().begin().await.map_err(db)?;
    if !control::exists(&mut tx, &id, &attempt)
        .await
        .map_err(ApiError::from_store)?
    {
        return Err(missing());
    }
    let s = control::current(&mut tx, &id)
        .await
        .map_err(ApiError::from_store)?
        .ok_or_else(missing)?;
    if s.target.attempt_id != attempt {
        return Err(blocked("This attempt is no longer selected."));
    }
    let denied = || blocked("The current attempt has no complete, matching protected evidence.");
    let p = root
        .required_project(&id)
        .await
        .map_err(|_| denied())?
        .profile;
    let source = root.candidate(&s.target).await.map_err(|_| denied())?;
    if !root
        .reading(&p, &s, &source)
        .await
        .map_err(|_| denied())?
        .export
    {
        return Err(denied());
    }
    tx.commit().await.map_err(db)?;
    // Move the verified buffer into the response. Never reopen the candidate path after checking it.
    Ok((
        [
            (header::CONTENT_TYPE, "text/plain; charset=utf-8"),
            (
                header::CONTENT_DISPOSITION,
                "attachment; filename=\"port.rs\"",
            ),
        ],
        source.into_bytes(),
    )
        .into_response())
}
