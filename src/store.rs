mod legacy;
pub(crate) mod source_port;

pub use legacy::{
    LegacyEngine, LegacyJobState, LegacyProjection, LegacyProjectionReceipt, LegacyProjectionRecord,
};

use std::path::Path;
use std::str::FromStr;
use std::time::Duration;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::migrate::MigrateError;
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::{FromRow, Sqlite, SqlitePool, Transaction};
use thiserror::Error;
use time::OffsetDateTime;
use uuid::Uuid;

use crate::domain::{
    CommandKind, CommandReceipt, CommandRequest, DomainError, Job, JobStatus, Outcome, Phase,
    Project, ProjectKind, ReleasesSnapshot, WorkspaceSnapshot,
};
use crate::events::{EVENT_SCHEMA_VERSION, EventEnvelope, EventReplay, MAX_EVENT_PAYLOAD_BYTES};

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("database migration failed")]
    Migration(#[from] MigrateError),
    #[error("serialization failed")]
    Serialization(#[from] serde_json::Error),
    #[error(transparent)]
    InvalidDomain(#[from] DomainError),
    #[error("request is invalid: {0}")]
    InvalidInput(&'static str),
    #[error("{entity} was not found")]
    NotFound { entity: &'static str },
    #[error("stale revision")]
    StaleRevision { current_revision: u64 },
    #[error("illegal transition from {from} to {to}")]
    IllegalTransition { from: JobStatus, to: JobStatus },
    #[error("idempotency key was reused with a different request")]
    IdempotencyConflict,
    #[error("stored data is invalid")]
    CorruptData,
}

#[derive(Clone)]
pub struct Store {
    pool: SqlitePool,
}

impl Store {
    pub async fn open(path: impl AsRef<Path>) -> Result<Self, StoreError> {
        let options = SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true)
            .foreign_keys(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .busy_timeout(Duration::from_secs(5));
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(options)
            .await?;
        sqlx::migrate!("./migrations").run(&pool).await?;
        Ok(Self { pool })
    }

    #[must_use]
    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    pub async fn apply_command(
        &self,
        idempotency_key: &str,
        request: &CommandRequest,
    ) -> Result<CommandReceipt, StoreError> {
        validate_idempotency_key(idempotency_key)?;
        let request_bytes = serde_json::to_vec(request)?;
        let request_hash = hex::encode(Sha256::digest(&request_bytes));
        let mut transaction = self.pool.begin().await?;

        if let Some(record) = sqlx::query_as::<_, IdempotencyRow>(
            "SELECT request_hash, receipt_json FROM idempotency_keys WHERE key = ?",
        )
        .bind(idempotency_key)
        .fetch_optional(&mut *transaction)
        .await?
        {
            transaction.rollback().await?;
            if record.request_hash != request_hash {
                return Err(StoreError::IdempotencyConflict);
            }
            let mut receipt: CommandReceipt = serde_json::from_str(&record.receipt_json)?;
            receipt.replayed = true;
            return Ok(receipt);
        }

        let receipt = match request.command {
            CommandKind::CreateProject => create_project(&mut transaction, request).await?,
            CommandKind::StartJob => start_job(&mut transaction, request).await?,
            CommandKind::CancelJob => {
                transition_command(
                    &mut transaction,
                    request,
                    JobStatus::Cancelling,
                    "job.status_changed",
                )
                .await?
            }
            CommandKind::ResolveDecision => {
                transition_command(
                    &mut transaction,
                    request,
                    JobStatus::Running,
                    "job.decision_resolved",
                )
                .await?
            }
            CommandKind::RetryJob => {
                new_attempt(&mut transaction, request, NewAttemptKind::Retry).await?
            }
            CommandKind::ResumeJob => {
                new_attempt(&mut transaction, request, NewAttemptKind::Resume).await?
            }
        };

        let receipt_json = serde_json::to_string(&receipt)?;
        let created_at = current_timestamp();
        sqlx::query(
            "INSERT INTO idempotency_keys(key, request_hash, receipt_json, created_at) \
             VALUES (?, ?, ?, ?)",
        )
        .bind(idempotency_key)
        .bind(request_hash)
        .bind(receipt_json)
        .bind(created_at)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(receipt)
    }

    pub async fn append_transition(
        &self,
        job_id: &str,
        expected_revision: u64,
        to: JobStatus,
        phase: Option<Phase>,
    ) -> Result<EventEnvelope, StoreError> {
        let mut transaction = self.pool.begin().await?;
        let job = load_job(&mut transaction, job_id).await?;
        let current_revision = stored_u64(job.revision)?;
        require_revision(expected_revision, current_revision)?;
        let from = parse_job_status(&job.status)?;
        let payload = json!({"from": from, "to": to});
        let event = transition_job(&mut transaction, job, to, phase, payload).await?;
        transaction.commit().await?;
        Ok(event)
    }

    pub async fn reconcile_restart(&self) -> Result<usize, StoreError> {
        let mut transaction = self.pool.begin().await?;
        let jobs = sqlx::query_as::<_, JobRow>(
            "SELECT id, project_id, attempt_id, attempt, status, phase, outcome, revision, \
                    created_at, updated_at \
             FROM jobs \
             WHERE status IN ('starting', 'running', 'awaiting_input', 'blocked', 'cancelling') \
             ORDER BY id",
        )
        .fetch_all(&mut *transaction)
        .await?;
        let count = jobs.len();
        for job in jobs {
            let from = parse_job_status(&job.status)?;
            let payload = json!({
                "from": from,
                "to": JobStatus::Interrupted,
                "reason": "restart_reconciliation"
            });
            transition_job(&mut transaction, job, JobStatus::Interrupted, None, payload).await?;
        }
        transaction.commit().await?;
        Ok(count)
    }

    pub async fn snapshot(&self) -> Result<WorkspaceSnapshot, StoreError> {
        let mut transaction = self.pool.begin().await?;
        let cursor = event_cursor(&mut transaction).await?;
        let generated_at = current_timestamp();
        let project_rows = sqlx::query_as::<_, ProjectRow>(
            "SELECT id, kind, name, revision, created_at FROM projects ORDER BY created_at, id",
        )
        .fetch_all(&mut *transaction)
        .await?;
        let job_rows = sqlx::query_as::<_, JobRow>(
            "SELECT id, project_id, attempt_id, attempt, status, phase, outcome, revision, \
                    created_at, updated_at \
             FROM jobs ORDER BY created_at, id",
        )
        .fetch_all(&mut *transaction)
        .await?;
        let release_rows = sqlx::query_as::<_, ReleaseRow>(
            "SELECT slot, selector_json FROM release_targets ORDER BY slot",
        )
        .fetch_all(&mut *transaction)
        .await?;
        let engines = legacy::load_engine_snapshots(&mut transaction).await?;
        transaction.commit().await?;

        let projects = project_rows
            .into_iter()
            .map(project_from_row)
            .collect::<Result<Vec<_>, _>>()?;
        let jobs = job_rows
            .into_iter()
            .map(job_from_row)
            .collect::<Result<Vec<_>, _>>()?;
        let mut releases = ReleasesSnapshot::default();
        for row in release_rows {
            let selector = serde_json::from_str(&row.selector_json)?;
            match row.slot.as_str() {
                "active" => releases.active = Some(selector),
                "previous" => releases.previous = Some(selector),
                "staged" => releases.staged = Some(selector),
                _ => return Err(StoreError::CorruptData),
            }
        }

        Ok(WorkspaceSnapshot {
            schema_version: EVENT_SCHEMA_VERSION,
            cursor,
            generated_at,
            projects,
            jobs,
            engines,
            releases,
        })
    }

    pub async fn replay_events(&self, after: u64) -> Result<EventReplay, StoreError> {
        let mut transaction = self.pool.begin().await?;
        let high_water = event_cursor(&mut transaction).await?;
        let retained_min = sqlx::query_scalar::<_, Option<i64>>("SELECT MIN(event_id) FROM events")
            .fetch_one(&mut *transaction)
            .await?
            .map(stored_u64)
            .transpose()?;

        let missing_retained_history = high_water > after
            && retained_min.is_none_or(|minimum| after.saturating_add(1) < minimum);
        if after > high_water || missing_retained_history {
            transaction.commit().await?;
            return Ok(EventReplay::Reset { cursor: high_water });
        }

        let after_i64 = i64::try_from(after).map_err(|_| StoreError::CorruptData)?;
        let rows = sqlx::query_as::<_, EventRow>(
            "SELECT event_id, schema_version, project_id, job_id, attempt_id, revision, phase, \
                    event_type, at, payload_json \
             FROM events WHERE event_id > ? ORDER BY event_id",
        )
        .bind(after_i64)
        .fetch_all(&mut *transaction)
        .await?;
        transaction.commit().await?;
        let events = rows
            .into_iter()
            .map(event_from_row)
            .collect::<Result<Vec<_>, _>>()?;
        Ok(EventReplay::Events { events, high_water })
    }
}

#[derive(Debug, FromRow)]
struct IdempotencyRow {
    request_hash: String,
    receipt_json: String,
}

#[derive(Debug, FromRow)]
struct ProjectRow {
    id: String,
    kind: String,
    name: String,
    revision: i64,
    created_at: String,
}

#[derive(Debug, FromRow)]
struct JobRow {
    id: String,
    project_id: String,
    attempt_id: String,
    attempt: i64,
    status: String,
    phase: String,
    outcome: Option<String>,
    revision: i64,
    created_at: String,
    updated_at: String,
}

#[derive(Debug, FromRow)]
struct EventRow {
    event_id: i64,
    schema_version: i64,
    project_id: Option<String>,
    job_id: Option<String>,
    attempt_id: Option<String>,
    revision: i64,
    phase: Option<String>,
    event_type: String,
    at: String,
    payload_json: String,
}

#[derive(Debug, FromRow)]
struct ReleaseRow {
    slot: String,
    selector_json: String,
}

#[derive(Debug, Clone, Copy)]
enum NewAttemptKind {
    Retry,
    Resume,
}

async fn create_project(
    transaction: &mut Transaction<'_, Sqlite>,
    request: &CommandRequest,
) -> Result<CommandReceipt, StoreError> {
    if request.project_id.is_some() || request.job_id.is_some() {
        return Err(StoreError::InvalidInput(
            "create_project does not accept project_id or job_id",
        ));
    }
    require_revision(request.expected_revision, 0)?;
    let kind = payload_string(&request.payload, "kind")?.parse::<ProjectKind>()?;
    let name = payload_string(&request.payload, "name")?.trim();
    if name.is_empty() || name.len() > 200 {
        return Err(StoreError::InvalidInput(
            "project name must contain 1 to 200 bytes",
        ));
    }
    let project_id = new_id(transaction, "p_").await?;
    let created_at = current_timestamp();
    sqlx::query(
        "INSERT INTO projects(id, kind, name, revision, created_at) VALUES (?, ?, ?, 1, ?)",
    )
    .bind(&project_id)
    .bind(kind.as_str())
    .bind(name)
    .bind(&created_at)
    .execute(&mut **transaction)
    .await?;
    let payload = json!({"kind": kind, "name": name});
    let event = append_event(
        transaction,
        NewEvent {
            project_id: Some(&project_id),
            job_id: None,
            attempt_id: None,
            revision: 1,
            phase: None,
            event_type: "project.created",
            at: &created_at,
            payload: &payload,
        },
    )
    .await?;
    Ok(CommandReceipt {
        project_id,
        job_id: None,
        revision: 1,
        cursor: event.event_id,
        replayed: false,
    })
}

async fn start_job(
    transaction: &mut Transaction<'_, Sqlite>,
    request: &CommandRequest,
) -> Result<CommandReceipt, StoreError> {
    if request.job_id.is_some() {
        return Err(StoreError::InvalidInput("start_job does not accept job_id"));
    }
    let project_id = required_identifier(request.project_id.as_deref(), "project_id is required")?;
    if crate::jobs::registered_kind(project_id).is_some() {
        return Err(StoreError::InvalidInput(
            "registered source profiles use the proposal workflow",
        ));
    }
    let current_project_revision = project_revision(transaction, project_id).await?;
    require_revision(request.expected_revision, current_project_revision)?;
    let next_project_revision = checked_increment(current_project_revision)?;
    let phase = Phase::new(payload_string(&request.payload, "phase")?)?;
    let job_id = new_id(transaction, "j_").await?;
    let attempt_id = new_id(transaction, "a_").await?;
    let created_at = current_timestamp();

    let updated = sqlx::query("UPDATE projects SET revision = ? WHERE id = ? AND revision = ?")
        .bind(requested_i64(next_project_revision)?)
        .bind(project_id)
        .bind(requested_i64(current_project_revision)?)
        .execute(&mut **transaction)
        .await?;
    if updated.rows_affected() != 1 {
        let current = project_revision(transaction, project_id).await?;
        return Err(StoreError::StaleRevision {
            current_revision: current,
        });
    }

    sqlx::query(
        "INSERT INTO jobs(\
             id, project_id, attempt_id, attempt, status, phase, outcome, revision, created_at, updated_at\
         ) VALUES (?, ?, ?, 1, 'queued', ?, NULL, 1, ?, ?)",
    )
    .bind(&job_id)
    .bind(project_id)
    .bind(&attempt_id)
    .bind(phase.as_str())
    .bind(&created_at)
    .bind(&created_at)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "INSERT INTO attempts(\
             id, job_id, number, status, phase, outcome, created_at, updated_at\
         ) VALUES (?, ?, 1, 'queued', ?, NULL, ?, ?)",
    )
    .bind(&attempt_id)
    .bind(&job_id)
    .bind(phase.as_str())
    .bind(&created_at)
    .bind(&created_at)
    .execute(&mut **transaction)
    .await?;
    let payload = json!({"attempt": 1});
    let event = append_event(
        transaction,
        NewEvent {
            project_id: Some(project_id),
            job_id: Some(&job_id),
            attempt_id: Some(&attempt_id),
            revision: 1,
            phase: Some(&phase),
            event_type: "job.queued",
            at: &created_at,
            payload: &payload,
        },
    )
    .await?;
    Ok(CommandReceipt {
        project_id: project_id.to_owned(),
        job_id: Some(job_id),
        revision: next_project_revision,
        cursor: event.event_id,
        replayed: false,
    })
}

async fn transition_command(
    transaction: &mut Transaction<'_, Sqlite>,
    request: &CommandRequest,
    to: JobStatus,
    event_type: &'static str,
) -> Result<CommandReceipt, StoreError> {
    let job_id = required_identifier(request.job_id.as_deref(), "job_id is required")?;
    let job = load_job(transaction, job_id).await?;
    if let Some(project_id) = request.project_id.as_deref()
        && project_id != job.project_id
    {
        return Err(StoreError::NotFound { entity: "job" });
    }
    let current_revision = stored_u64(job.revision)?;
    require_revision(request.expected_revision, current_revision)?;
    let from = parse_job_status(&job.status)?;
    let payload = json!({"from": from, "to": to});
    let event = transition_job_with_type(transaction, job, to, None, payload, event_type).await?;
    let project_id = event.project_id.clone().ok_or(StoreError::CorruptData)?;
    Ok(CommandReceipt {
        project_id,
        job_id: Some(job_id.to_owned()),
        revision: event.revision,
        cursor: event.event_id,
        replayed: false,
    })
}

async fn new_attempt(
    transaction: &mut Transaction<'_, Sqlite>,
    request: &CommandRequest,
    kind: NewAttemptKind,
) -> Result<CommandReceipt, StoreError> {
    let job_id = required_identifier(request.job_id.as_deref(), "job_id is required")?;
    let job = load_job(transaction, job_id).await?;
    if let Some(project_id) = request.project_id.as_deref()
        && project_id != job.project_id
    {
        return Err(StoreError::NotFound { entity: "job" });
    }
    let current_revision = stored_u64(job.revision)?;
    require_revision(request.expected_revision, current_revision)?;
    let current_status = parse_job_status(&job.status)?;
    match kind {
        NewAttemptKind::Retry if !current_status.is_terminal() => {
            return Err(StoreError::InvalidInput(
                "retry_job requires a terminal attempt",
            ));
        }
        NewAttemptKind::Resume if current_status != JobStatus::Interrupted => {
            return Err(StoreError::InvalidInput(
                "resume_job requires an interrupted attempt",
            ));
        }
        _ => {}
    }

    let phase = match request.payload.get("phase") {
        Some(Value::String(value)) => Phase::new(value.as_str())?,
        Some(_) => return Err(StoreError::InvalidInput("phase must be a string")),
        None => Phase::new(job.phase.as_str())?,
    };
    let attempt = job.attempt.checked_add(1).ok_or(StoreError::CorruptData)?;
    let next_revision = checked_increment(current_revision)?;
    let attempt_id = new_id(transaction, "a_").await?;
    let now = current_timestamp();
    sqlx::query(
        "UPDATE jobs \
         SET attempt_id = ?, attempt = ?, status = 'queued', phase = ?, outcome = NULL, \
             revision = ?, updated_at = ? \
         WHERE id = ? AND revision = ?",
    )
    .bind(&attempt_id)
    .bind(attempt)
    .bind(phase.as_str())
    .bind(requested_i64(next_revision)?)
    .bind(&now)
    .bind(job_id)
    .bind(job.revision)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "INSERT INTO attempts(\
             id, job_id, number, status, phase, outcome, created_at, updated_at\
         ) VALUES (?, ?, ?, 'queued', ?, NULL, ?, ?)",
    )
    .bind(&attempt_id)
    .bind(job_id)
    .bind(attempt)
    .bind(phase.as_str())
    .bind(&now)
    .bind(&now)
    .execute(&mut **transaction)
    .await?;
    let reason = match kind {
        NewAttemptKind::Retry => "retry",
        NewAttemptKind::Resume => "resume",
    };
    let payload = json!({"attempt": attempt, "reason": reason});
    let event = append_event(
        transaction,
        NewEvent {
            project_id: Some(&job.project_id),
            job_id: Some(job_id),
            attempt_id: Some(&attempt_id),
            revision: next_revision,
            phase: Some(&phase),
            event_type: "job.queued",
            at: &now,
            payload: &payload,
        },
    )
    .await?;
    Ok(CommandReceipt {
        project_id: job.project_id,
        job_id: Some(job_id.to_owned()),
        revision: next_revision,
        cursor: event.event_id,
        replayed: false,
    })
}

async fn transition_job(
    transaction: &mut Transaction<'_, Sqlite>,
    job: JobRow,
    to: JobStatus,
    phase: Option<Phase>,
    payload: Value,
) -> Result<EventEnvelope, StoreError> {
    transition_job_with_type(transaction, job, to, phase, payload, "job.status_changed").await
}

async fn transition_job_with_type(
    transaction: &mut Transaction<'_, Sqlite>,
    job: JobRow,
    to: JobStatus,
    phase: Option<Phase>,
    payload: Value,
    event_type: &'static str,
) -> Result<EventEnvelope, StoreError> {
    let from = parse_job_status(&job.status)?;
    if !from.can_transition_to(to) {
        return Err(StoreError::IllegalTransition { from, to });
    }
    let current_revision = stored_u64(job.revision)?;
    let next_revision = checked_increment(current_revision)?;
    let phase = phase.map_or_else(|| Phase::new(job.phase.as_str()), Ok)?;
    let outcome = to.outcome();
    let now = current_timestamp();
    let updated = sqlx::query(
        "UPDATE jobs \
         SET status = ?, phase = ?, outcome = ?, revision = ?, updated_at = ? \
         WHERE id = ? AND revision = ?",
    )
    .bind(to.as_str())
    .bind(phase.as_str())
    .bind(outcome.map(Outcome::as_str))
    .bind(requested_i64(next_revision)?)
    .bind(&now)
    .bind(&job.id)
    .bind(job.revision)
    .execute(&mut **transaction)
    .await?;
    if updated.rows_affected() != 1 {
        let current = load_job(transaction, &job.id).await?;
        return Err(StoreError::StaleRevision {
            current_revision: stored_u64(current.revision)?,
        });
    }
    sqlx::query(
        "UPDATE attempts \
         SET status = ?, phase = ?, outcome = ?, updated_at = ? \
         WHERE id = ?",
    )
    .bind(to.as_str())
    .bind(phase.as_str())
    .bind(outcome.map(Outcome::as_str))
    .bind(&now)
    .bind(&job.attempt_id)
    .execute(&mut **transaction)
    .await?;
    append_event(
        transaction,
        NewEvent {
            project_id: Some(&job.project_id),
            job_id: Some(&job.id),
            attempt_id: Some(&job.attempt_id),
            revision: next_revision,
            phase: Some(&phase),
            event_type,
            at: &now,
            payload: &payload,
        },
    )
    .await
}

struct NewEvent<'a> {
    project_id: Option<&'a str>,
    job_id: Option<&'a str>,
    attempt_id: Option<&'a str>,
    revision: u64,
    phase: Option<&'a Phase>,
    event_type: &'a str,
    at: &'a str,
    payload: &'a Value,
}

async fn append_event(
    transaction: &mut Transaction<'_, Sqlite>,
    event: NewEvent<'_>,
) -> Result<EventEnvelope, StoreError> {
    let payload_json = serde_json::to_string(event.payload)?;
    if payload_json.len() > MAX_EVENT_PAYLOAD_BYTES {
        return Err(StoreError::InvalidInput("event payload exceeds 16 KiB"));
    }
    let event_id: i64 = sqlx::query_scalar(
        "UPDATE event_cursor SET high_water = high_water + 1 \
         WHERE singleton = 1 RETURNING high_water",
    )
    .fetch_one(&mut **transaction)
    .await?;
    sqlx::query(
        "INSERT INTO events(\
             event_id, schema_version, project_id, job_id, attempt_id, revision, phase, \
             event_type, at, payload_json\
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(event_id)
    .bind(i64::from(EVENT_SCHEMA_VERSION))
    .bind(event.project_id)
    .bind(event.job_id)
    .bind(event.attempt_id)
    .bind(requested_i64(event.revision)?)
    .bind(event.phase.map(Phase::as_str))
    .bind(event.event_type)
    .bind(event.at)
    .bind(payload_json)
    .execute(&mut **transaction)
    .await?;
    Ok(EventEnvelope {
        schema_version: EVENT_SCHEMA_VERSION,
        event_id: stored_u64(event_id)?,
        project_id: event.project_id.map(str::to_owned),
        job_id: event.job_id.map(str::to_owned),
        attempt_id: event.attempt_id.map(str::to_owned),
        revision: event.revision,
        phase: event.phase.cloned(),
        event_type: event.event_type.to_owned(),
        at: event.at.to_owned(),
        payload: event.payload.clone(),
    })
}

async fn load_job(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
) -> Result<JobRow, StoreError> {
    sqlx::query_as::<_, JobRow>(
        "SELECT id, project_id, attempt_id, attempt, status, phase, outcome, revision, \
                created_at, updated_at \
         FROM jobs WHERE id = ?",
    )
    .bind(job_id)
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or(StoreError::NotFound { entity: "job" })
}

async fn project_revision(
    transaction: &mut Transaction<'_, Sqlite>,
    project_id: &str,
) -> Result<u64, StoreError> {
    let revision = sqlx::query_scalar::<_, i64>("SELECT revision FROM projects WHERE id = ?")
        .bind(project_id)
        .fetch_optional(&mut **transaction)
        .await?
        .ok_or(StoreError::NotFound { entity: "project" })?;
    stored_u64(revision)
}

async fn new_id(
    transaction: &mut Transaction<'_, Sqlite>,
    prefix: &str,
) -> Result<String, StoreError> {
    let bytes = sqlx::query_scalar::<_, Vec<u8>>("SELECT randomblob(16)")
        .fetch_one(&mut **transaction)
        .await?;
    let uuid = Uuid::from_slice(&bytes).map_err(|_| StoreError::CorruptData)?;
    Ok(format!("{prefix}{}", uuid.simple()))
}

fn current_timestamp() -> String {
    let now = OffsetDateTime::now_utc();
    let (year, month, day) = now.to_calendar_date();
    let month = u8::from(month);
    let (hour, minute, second, millisecond) = now.to_hms_milli();
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millisecond:03}Z")
}

async fn event_cursor(transaction: &mut Transaction<'_, Sqlite>) -> Result<u64, StoreError> {
    let cursor =
        sqlx::query_scalar::<_, i64>("SELECT high_water FROM event_cursor WHERE singleton = 1")
            .fetch_one(&mut **transaction)
            .await?;
    stored_u64(cursor)
}

fn validate_idempotency_key(key: &str) -> Result<(), StoreError> {
    if key.is_empty()
        || key.len() > 128
        || !key
            .as_bytes()
            .iter()
            .all(|byte| (0x21..=0x7e).contains(byte))
    {
        return Err(StoreError::InvalidInput(
            "idempotency key must be 1 to 128 visible ASCII characters",
        ));
    }
    Ok(())
}

fn required_identifier<'a>(
    value: Option<&'a str>,
    message: &'static str,
) -> Result<&'a str, StoreError> {
    value
        .filter(|identifier| !identifier.is_empty())
        .ok_or(StoreError::InvalidInput(message))
}

fn payload_string<'a>(payload: &'a Value, field: &'static str) -> Result<&'a str, StoreError> {
    payload
        .get(field)
        .and_then(Value::as_str)
        .ok_or(StoreError::InvalidInput(match field {
            "kind" => "payload.kind must be a string",
            "name" => "payload.name must be a string",
            "phase" => "payload.phase must be a string",
            _ => "payload field must be a string",
        }))
}

fn require_revision(expected: u64, current: u64) -> Result<(), StoreError> {
    if expected != current {
        return Err(StoreError::StaleRevision {
            current_revision: current,
        });
    }
    Ok(())
}

fn checked_increment(value: u64) -> Result<u64, StoreError> {
    value.checked_add(1).ok_or(StoreError::CorruptData)
}

fn stored_u64(value: i64) -> Result<u64, StoreError> {
    u64::try_from(value).map_err(|_| StoreError::CorruptData)
}

fn requested_i64(value: u64) -> Result<i64, StoreError> {
    i64::try_from(value).map_err(|_| StoreError::InvalidInput("revision is out of range"))
}

fn parse_job_status(value: &str) -> Result<JobStatus, StoreError> {
    JobStatus::from_str(value).map_err(|_| StoreError::CorruptData)
}

fn project_from_row(row: ProjectRow) -> Result<Project, StoreError> {
    Ok(Project {
        id: row.id,
        kind: ProjectKind::from_str(&row.kind).map_err(|_| StoreError::CorruptData)?,
        name: row.name,
        revision: stored_u64(row.revision)?,
        created_at: row.created_at,
    })
}

fn job_from_row(row: JobRow) -> Result<Job, StoreError> {
    let attempt = u32::try_from(row.attempt).map_err(|_| StoreError::CorruptData)?;
    let outcome = row
        .outcome
        .as_deref()
        .map(Outcome::from_str)
        .transpose()
        .map_err(|_| StoreError::CorruptData)?;
    Ok(Job {
        id: row.id,
        project_id: row.project_id,
        attempt_id: row.attempt_id,
        attempt,
        status: parse_job_status(&row.status)?,
        phase: Phase::new(row.phase).map_err(|_| StoreError::CorruptData)?,
        outcome,
        revision: stored_u64(row.revision)?,
        created_at: row.created_at,
        updated_at: row.updated_at,
    })
}

fn event_from_row(row: EventRow) -> Result<EventEnvelope, StoreError> {
    let schema_version = u8::try_from(row.schema_version).map_err(|_| StoreError::CorruptData)?;
    if schema_version != EVENT_SCHEMA_VERSION {
        return Err(StoreError::CorruptData);
    }
    let phase = row
        .phase
        .map(Phase::new)
        .transpose()
        .map_err(|_| StoreError::CorruptData)?;
    Ok(EventEnvelope {
        schema_version,
        event_id: stored_u64(row.event_id)?,
        project_id: row.project_id,
        job_id: row.job_id,
        attempt_id: row.attempt_id,
        revision: stored_u64(row.revision)?,
        phase,
        event_type: row.event_type,
        at: row.at,
        payload: serde_json::from_str(&row.payload_json)?,
    })
}
