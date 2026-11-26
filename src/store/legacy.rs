use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, Sqlite, Transaction};

use crate::domain::{
    EngineAvailability, EngineSnapshot, EnginesSnapshot, JobStatus, Outcome, Phase, ProjectKind,
};

use super::{
    JobRow, NewEvent, Store, StoreError, append_event, checked_increment, current_timestamp,
    load_job, parse_job_status, requested_i64, stored_u64, transition_job,
};

const MAX_LEGACY_STATE_BYTES: usize = 1024 * 1024;
const MAX_LEGACY_DETAIL_BYTES: usize = 512;
const MAX_LEGACY_KEY_BYTES: usize = 128;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LegacyEngine {
    Fourmilab,
    Inspector,
}

impl LegacyEngine {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Fourmilab => "fourmilab",
            Self::Inspector => "inspector",
        }
    }

    const fn project_kind(self) -> ProjectKind {
        match self {
            Self::Fourmilab => ProjectKind::Reconstruct,
            Self::Inspector => ProjectKind::Inspect,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegacyJobState {
    pub legacy_key: String,
    pub status: JobStatus,
    pub phase: Phase,
    pub verified: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegacyProjection {
    pub engine: LegacyEngine,
    pub availability: EngineAvailability,
    pub detail: String,
    pub normalized: Value,
    pub evidence: Option<Value>,
    pub job: Option<LegacyJobState>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegacyProjectionRecord {
    pub engine: LegacyEngine,
    pub availability: EngineAvailability,
    pub detail: String,
    pub normalized: Value,
    pub evidence: Option<Value>,
    pub evidence_sha256: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegacyProjectionReceipt {
    pub changed: bool,
    pub event_id: Option<u64>,
    pub job_id: Option<String>,
}

#[derive(Debug, FromRow)]
struct ProjectionRow {
    engine: String,
    availability: String,
    detail: String,
    state_hash: String,
    normalized_json: String,
    evidence_json: Option<String>,
    evidence_sha256: String,
    updated_at: String,
}

impl Store {
    pub async fn record_legacy_projection(
        &self,
        projection: &LegacyProjection,
    ) -> Result<LegacyProjectionReceipt, StoreError> {
        validate_projection(projection)?;
        let normalized_json = serde_json::to_string(&projection.normalized)?;
        let evidence_json = projection
            .evidence
            .as_ref()
            .map(serde_json::to_string)
            .transpose()?;
        if normalized_json.len() > MAX_LEGACY_STATE_BYTES
            || evidence_json
                .as_ref()
                .is_some_and(|evidence| evidence.len() > MAX_LEGACY_STATE_BYTES)
        {
            return Err(StoreError::InvalidInput(
                "legacy state or evidence exceeds 1 MiB",
            ));
        }
        let evidence_sha256 = hash_bytes(evidence_json.as_deref().unwrap_or("null").as_bytes());
        let state_hash = projection_hash(projection, normalized_json.as_bytes(), &evidence_sha256);
        let mut transaction = self.pool.begin().await?;
        let previous = projection_row(&mut transaction, projection.engine).await?;
        if previous
            .as_ref()
            .is_some_and(|record| record.state_hash == state_hash)
        {
            transaction.commit().await?;
            return Ok(LegacyProjectionReceipt {
                changed: false,
                event_id: None,
                job_id: None,
            });
        }

        let now = current_timestamp();
        sqlx::query(
            "INSERT INTO legacy_engine_projections(\
                 engine, availability, detail, state_hash, normalized_json, evidence_json, \
                 evidence_sha256, updated_at\
             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) \
             ON CONFLICT(engine) DO UPDATE SET \
                 availability = excluded.availability, detail = excluded.detail, \
                 state_hash = excluded.state_hash, normalized_json = excluded.normalized_json, \
                 evidence_json = excluded.evidence_json, \
                 evidence_sha256 = excluded.evidence_sha256, updated_at = excluded.updated_at",
        )
        .bind(projection.engine.as_str())
        .bind(availability_str(&projection.availability)?)
        .bind(&projection.detail)
        .bind(&state_hash)
        .bind(&normalized_json)
        .bind(&evidence_json)
        .bind(&evidence_sha256)
        .bind(&now)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "INSERT INTO legacy_evidence_records(\
                 engine, evidence_sha256, evidence_json, created_at\
             ) VALUES (?, ?, ?, ?) \
             ON CONFLICT(engine, evidence_sha256) DO NOTHING",
        )
        .bind(projection.engine.as_str())
        .bind(&evidence_sha256)
        .bind(evidence_json.as_deref().unwrap_or("null"))
        .bind(&now)
        .execute(&mut *transaction)
        .await?;

        let previous_state = previous
            .as_ref()
            .map(|record| record.availability.as_str())
            .unwrap_or("unknown");
        let payload = json!({
            "engine": projection.engine,
            "from": {
                "state": previous_state,
                "state_hash": previous.as_ref().map(|record| record.state_hash.as_str())
            },
            "to": {
                "state": availability_str(&projection.availability)?,
                "detail": projection.detail,
                "state_hash": state_hash
            },
            "evidence_sha256": evidence_sha256
        });
        let engine_event = append_event(
            &mut transaction,
            NewEvent {
                project_id: None,
                job_id: None,
                attempt_id: None,
                revision: 0,
                phase: None,
                event_type: "engine.state_changed",
                at: &now,
                payload: &payload,
            },
        )
        .await?;

        let job_id = if projection.availability == EngineAvailability::Up {
            if let Some(job_state) = projection.job.as_ref() {
                project_job_state(
                    &mut transaction,
                    projection.engine,
                    job_state,
                    &evidence_sha256,
                )
                .await?
            } else {
                None
            }
        } else {
            None
        };
        transaction.commit().await?;
        Ok(LegacyProjectionReceipt {
            changed: true,
            event_id: Some(engine_event.event_id),
            job_id,
        })
    }

    pub async fn legacy_projection(
        &self,
        engine: LegacyEngine,
    ) -> Result<Option<LegacyProjectionRecord>, StoreError> {
        let mut transaction = self.pool.begin().await?;
        let row = projection_row(&mut transaction, engine).await?;
        transaction.commit().await?;
        row.map(record_from_row).transpose()
    }

    pub async fn legacy_evidence(
        &self,
        engine: LegacyEngine,
        evidence_sha256: &str,
    ) -> Result<Option<Value>, StoreError> {
        validate_evidence_sha256(evidence_sha256)?;
        let evidence_json = sqlx::query_scalar::<_, String>(
            "SELECT evidence_json FROM legacy_evidence_records \
             WHERE engine = ? AND evidence_sha256 = ?",
        )
        .bind(engine.as_str())
        .bind(evidence_sha256)
        .fetch_optional(&self.pool)
        .await?;
        evidence_json
            .map(|evidence| serde_json::from_str(&evidence))
            .transpose()
            .map_err(StoreError::from)
    }
}

pub(super) async fn load_engine_snapshots(
    transaction: &mut Transaction<'_, Sqlite>,
) -> Result<EnginesSnapshot, StoreError> {
    let rows = sqlx::query_as::<_, ProjectionRow>(
        "SELECT engine, availability, detail, state_hash, normalized_json, evidence_json, \
                evidence_sha256, updated_at \
         FROM legacy_engine_projections ORDER BY engine",
    )
    .fetch_all(&mut **transaction)
    .await?;
    let mut snapshots = EnginesSnapshot::default();
    for row in rows {
        let snapshot = EngineSnapshot {
            state: parse_availability(&row.availability)?,
            detail: row.detail,
        };
        match row.engine.as_str() {
            "fourmilab" => snapshots.fourmilab = snapshot,
            "inspector" => snapshots.inspector = snapshot,
            _ => return Err(StoreError::CorruptData),
        }
    }
    Ok(snapshots)
}

async fn projection_row(
    transaction: &mut Transaction<'_, Sqlite>,
    engine: LegacyEngine,
) -> Result<Option<ProjectionRow>, StoreError> {
    Ok(sqlx::query_as::<_, ProjectionRow>(
        "SELECT engine, availability, detail, state_hash, normalized_json, evidence_json, \
                evidence_sha256, updated_at \
         FROM legacy_engine_projections WHERE engine = ?",
    )
    .bind(engine.as_str())
    .fetch_optional(&mut **transaction)
    .await?)
}

fn record_from_row(row: ProjectionRow) -> Result<LegacyProjectionRecord, StoreError> {
    Ok(LegacyProjectionRecord {
        engine: parse_engine(&row.engine)?,
        availability: parse_availability(&row.availability)?,
        detail: row.detail,
        normalized: serde_json::from_str(&row.normalized_json)?,
        evidence: row
            .evidence_json
            .as_deref()
            .map(serde_json::from_str)
            .transpose()?,
        evidence_sha256: row.evidence_sha256,
        updated_at: row.updated_at,
    })
}

async fn project_job_state(
    transaction: &mut Transaction<'_, Sqlite>,
    engine: LegacyEngine,
    state: &LegacyJobState,
    evidence_sha256: &str,
) -> Result<Option<String>, StoreError> {
    validate_legacy_key(&state.legacy_key)?;
    let Some(mut job) = bound_or_unambiguous_job(transaction, engine, &state.legacy_key).await?
    else {
        return Ok(None);
    };
    let job_id = job.id.clone();
    let mut transitioned = false;
    while parse_job_status(&job.status)? != state.status {
        let current = parse_job_status(&job.status)?;
        let Some(next) = next_status_toward(current, state.status) else {
            return Ok(Some(job_id));
        };
        let phase = if next == state.status {
            Some(state.phase.clone())
        } else {
            None
        };
        let payload = json!({
            "source": "legacy",
            "engine": engine,
            "legacy_key": state.legacy_key,
            "from": current,
            "to": next,
            "verified": state.verified && next == JobStatus::Succeeded,
            "evidence_sha256": evidence_sha256
        });
        transition_job(transaction, job, next, phase, payload).await?;
        transitioned = true;
        job = load_job(transaction, &job_id).await?;
    }
    if job.phase != state.phase.as_str() {
        project_job_phase(transaction, &job, engine, state, evidence_sha256).await?;
        transitioned = true;
        job = load_job(transaction, &job_id).await?;
    }
    if state.verified && state.status == JobStatus::Succeeded {
        mark_job_verified(
            transaction,
            &job,
            engine,
            &state.legacy_key,
            evidence_sha256,
            transitioned,
        )
        .await?;
    }
    Ok(Some(job_id))
}

async fn project_job_phase(
    transaction: &mut Transaction<'_, Sqlite>,
    job: &JobRow,
    engine: LegacyEngine,
    state: &LegacyJobState,
    evidence_sha256: &str,
) -> Result<(), StoreError> {
    let status = parse_job_status(&job.status)?;
    let previous_phase = Phase::new(job.phase.clone())?;
    let revision = checked_increment(stored_u64(job.revision)?)?;
    let now = current_timestamp();
    let updated = sqlx::query(
        "UPDATE jobs SET phase = ?, revision = ?, updated_at = ? \
         WHERE id = ? AND revision = ?",
    )
    .bind(state.phase.as_str())
    .bind(requested_i64(revision)?)
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
    sqlx::query("UPDATE attempts SET phase = ?, updated_at = ? WHERE id = ?")
        .bind(state.phase.as_str())
        .bind(&now)
        .bind(&job.attempt_id)
        .execute(&mut **transaction)
        .await?;
    let payload = json!({
        "source": "legacy",
        "engine": engine,
        "legacy_key": state.legacy_key,
        "status": status,
        "from": previous_phase,
        "to": state.phase,
        "verified": state.verified && status == JobStatus::Succeeded,
        "evidence_sha256": evidence_sha256,
    });
    append_event(
        transaction,
        NewEvent {
            project_id: Some(&job.project_id),
            job_id: Some(&job.id),
            attempt_id: Some(&job.attempt_id),
            revision,
            phase: Some(&state.phase),
            event_type: "job.phase_changed",
            at: &now,
            payload: &payload,
        },
    )
    .await?;
    Ok(())
}

async fn bound_or_unambiguous_job(
    transaction: &mut Transaction<'_, Sqlite>,
    engine: LegacyEngine,
    legacy_key: &str,
) -> Result<Option<JobRow>, StoreError> {
    if let Some(job) = sqlx::query_as::<_, JobRow>(
        "SELECT j.id, j.project_id, j.attempt_id, j.attempt, j.status, j.phase, j.outcome, \
                j.revision, j.created_at, j.updated_at \
         FROM legacy_job_bindings b JOIN jobs j ON j.id = b.job_id \
         WHERE b.engine = ? AND b.legacy_key = ?",
    )
    .bind(engine.as_str())
    .bind(legacy_key)
    .fetch_optional(&mut **transaction)
    .await?
    {
        return Ok(Some(job));
    }

    let candidates = sqlx::query_as::<_, JobRow>(
        "SELECT j.id, j.project_id, j.attempt_id, j.attempt, j.status, j.phase, j.outcome, \
                j.revision, j.created_at, j.updated_at \
         FROM jobs j JOIN projects p ON p.id = j.project_id \
         WHERE p.kind = ? \
           AND j.status NOT IN ('cancelled', 'failed', 'succeeded', 'interrupted') \
         ORDER BY j.created_at, j.id LIMIT 2",
    )
    .bind(engine.project_kind().as_str())
    .fetch_all(&mut **transaction)
    .await?;
    if candidates.len() != 1 {
        return Ok(None);
    }
    let job = candidates
        .into_iter()
        .next()
        .ok_or(StoreError::CorruptData)?;
    sqlx::query(
        "INSERT INTO legacy_job_bindings(engine, legacy_key, job_id, created_at) \
         VALUES (?, ?, ?, ?)",
    )
    .bind(engine.as_str())
    .bind(legacy_key)
    .bind(&job.id)
    .bind(current_timestamp())
    .execute(&mut **transaction)
    .await?;
    Ok(Some(job))
}

async fn mark_job_verified(
    transaction: &mut Transaction<'_, Sqlite>,
    job: &JobRow,
    engine: LegacyEngine,
    legacy_key: &str,
    evidence_sha256: &str,
    transitioned: bool,
) -> Result<(), StoreError> {
    if job.outcome.as_deref() == Some(Outcome::Verified.as_str()) {
        return Ok(());
    }
    let revision = if transitioned {
        stored_u64(job.revision)?
    } else {
        checked_increment(stored_u64(job.revision)?)?
    };
    let now = current_timestamp();
    sqlx::query(
        "UPDATE jobs SET outcome = 'verified', revision = ?, updated_at = ? \
         WHERE id = ? AND revision = ?",
    )
    .bind(requested_i64(revision)?)
    .bind(&now)
    .bind(&job.id)
    .bind(job.revision)
    .execute(&mut **transaction)
    .await?;
    sqlx::query("UPDATE attempts SET outcome = 'verified', updated_at = ? WHERE id = ?")
        .bind(&now)
        .bind(&job.attempt_id)
        .execute(&mut **transaction)
        .await?;
    if !transitioned {
        let phase = Phase::new(job.phase.clone())?;
        let payload = json!({
            "source": "legacy",
            "engine": engine,
            "legacy_key": legacy_key,
            "from": JobStatus::Succeeded,
            "to": JobStatus::Succeeded,
            "outcome": Outcome::Verified,
            "evidence_sha256": evidence_sha256
        });
        append_event(
            transaction,
            NewEvent {
                project_id: Some(&job.project_id),
                job_id: Some(&job.id),
                attempt_id: Some(&job.attempt_id),
                revision,
                phase: Some(&phase),
                event_type: "job.status_changed",
                at: &now,
                payload: &payload,
            },
        )
        .await?;
    }
    Ok(())
}

fn next_status_toward(current: JobStatus, target: JobStatus) -> Option<JobStatus> {
    if current == target || current.is_terminal() {
        return None;
    }
    match current {
        JobStatus::Queued => Some(JobStatus::Starting),
        JobStatus::Starting => match target {
            JobStatus::Failed | JobStatus::Interrupted | JobStatus::Cancelling => Some(target),
            JobStatus::Cancelled => Some(JobStatus::Cancelling),
            _ => Some(JobStatus::Running),
        },
        JobStatus::Running => match target {
            JobStatus::Cancelled => Some(JobStatus::Cancelling),
            JobStatus::Queued | JobStatus::Starting => None,
            _ => Some(target),
        },
        JobStatus::AwaitingInput | JobStatus::Blocked => {
            if current.can_transition_to(target) {
                Some(target)
            } else {
                Some(JobStatus::Running)
            }
        }
        JobStatus::Cancelling => match target {
            JobStatus::Cancelled | JobStatus::Failed | JobStatus::Interrupted => Some(target),
            _ => None,
        },
        JobStatus::Cancelled
        | JobStatus::Failed
        | JobStatus::Succeeded
        | JobStatus::Interrupted => None,
    }
}

fn validate_projection(projection: &LegacyProjection) -> Result<(), StoreError> {
    if projection.detail.is_empty() || projection.detail.len() > MAX_LEGACY_DETAIL_BYTES {
        return Err(StoreError::InvalidInput(
            "legacy engine detail must be 1 to 512 bytes",
        ));
    }
    availability_str(&projection.availability)?;
    Ok(())
}

fn validate_legacy_key(key: &str) -> Result<(), StoreError> {
    if key.is_empty()
        || key.len() > MAX_LEGACY_KEY_BYTES
        || !key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b':'))
    {
        return Err(StoreError::InvalidInput(
            "legacy key must be 1 to 128 safe ASCII characters",
        ));
    }
    Ok(())
}

fn validate_evidence_sha256(value: &str) -> Result<(), StoreError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    {
        return Err(StoreError::InvalidInput(
            "legacy evidence hash must be 64 lowercase hexadecimal characters",
        ));
    }
    Ok(())
}

fn availability_str(availability: &EngineAvailability) -> Result<&'static str, StoreError> {
    match availability {
        EngineAvailability::Up => Ok("up"),
        EngineAvailability::Down => Ok("down"),
        EngineAvailability::Unknown => Err(StoreError::InvalidInput(
            "persisted legacy availability must be up or down",
        )),
    }
}

fn parse_availability(value: &str) -> Result<EngineAvailability, StoreError> {
    match value {
        "up" => Ok(EngineAvailability::Up),
        "down" => Ok(EngineAvailability::Down),
        _ => Err(StoreError::CorruptData),
    }
}

fn parse_engine(value: &str) -> Result<LegacyEngine, StoreError> {
    match value {
        "fourmilab" => Ok(LegacyEngine::Fourmilab),
        "inspector" => Ok(LegacyEngine::Inspector),
        _ => Err(StoreError::CorruptData),
    }
}

fn projection_hash(
    projection: &LegacyProjection,
    normalized_json: &[u8],
    evidence_sha256: &str,
) -> String {
    let mut digest = Sha256::new();
    digest.update(projection.engine.as_str().as_bytes());
    digest.update([0]);
    digest.update(match projection.availability {
        EngineAvailability::Up => b"up".as_slice(),
        EngineAvailability::Down => b"down".as_slice(),
        EngineAvailability::Unknown => b"unknown".as_slice(),
    });
    digest.update([0]);
    digest.update(projection.detail.as_bytes());
    digest.update([0]);
    digest.update(normalized_json);
    digest.update([0]);
    digest.update(evidence_sha256.as_bytes());
    hex::encode(digest.finalize())
}

fn hash_bytes(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
