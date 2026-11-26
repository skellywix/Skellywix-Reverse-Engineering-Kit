//! Source-port commands and their events share the workspace transaction. No verifier evidence is stored here.
use super::{
    IdempotencyRow, NewEvent, Phase, Sqlite, StoreError, Transaction, append_event,
    checked_increment, current_timestamp, new_id, requested_i64, stored_u64,
};
use crate::{
    artifacts::Target,
    jobs::{Profile, disk::valid_attempt},
};
use serde_json::json;

pub(crate) struct Selected {
    pub target: Target,
    pub revision: u64,
    pub requested: bool,
}

pub(crate) async fn current(
    tx: &mut Transaction<'_, Sqlite>,
    project: &str,
) -> Result<Option<Selected>, StoreError> {
    let row: Option<(String, String, i64, bool)> = sqlx::query_as(
        "SELECT a.id, a.target_json, p.revision, a.compare_requested FROM source_port_attempts a JOIN projects p ON a.project_id=p.id WHERE a.project_id=? ORDER BY a.proposal_revision DESC LIMIT 1"
    ).bind(project).fetch_optional(&mut **tx).await?;
    row.map(|(id, json, revision, requested)| {
        let target: Target = serde_json::from_str(&json)?;
        if target.project_id != project || target.attempt_id != id || !valid_attempt(&id) {
            return Err(StoreError::CorruptData);
        }
        Ok(Selected {
            target,
            revision: stored_u64(revision)?,
            requested,
        })
    })
    .transpose()
}

pub(crate) async fn exists(
    tx: &mut Transaction<'_, Sqlite>,
    project: &str,
    attempt: &str,
) -> Result<bool, StoreError> {
    Ok(sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM source_port_attempts WHERE project_id=? AND id=?)",
    )
    .bind(project)
    .bind(attempt)
    .fetch_one(&mut **tx)
    .await?)
}

pub(crate) fn validate_key(key: &str) -> Result<(), StoreError> {
    super::validate_idempotency_key(key)
}

pub(crate) async fn replay(
    tx: &mut Transaction<'_, Sqlite>,
    key: &str,
    hash: &str,
) -> Result<Option<String>, StoreError> {
    let row = sqlx::query_as::<_, IdempotencyRow>(
        "SELECT request_hash, receipt_json FROM idempotency_keys WHERE key=?",
    )
    .bind(key)
    .fetch_optional(&mut **tx)
    .await?;
    row.map(|r| {
        if r.request_hash != hash {
            return Err(StoreError::IdempotencyConflict);
        }
        Ok(serde_json::from_str(&r.receipt_json)?)
    })
    .transpose()
}

pub(crate) async fn remember(
    tx: &mut Transaction<'_, Sqlite>,
    key: &str,
    hash: &str,
    id: &str,
) -> Result<(), StoreError> {
    sqlx::query(
        "INSERT INTO idempotency_keys(key, request_hash, receipt_json, created_at) VALUES(?,?,?,?)",
    )
    .bind(key)
    .bind(hash)
    .bind(serde_json::to_string(id)?)
    .bind(current_timestamp())
    .execute(&mut **tx)
    .await?;
    Ok(())
}

pub(crate) async fn next_id(tx: &mut Transaction<'_, Sqlite>) -> Result<String, StoreError> {
    new_id(tx, "port_").await
}

pub(crate) async fn record_proposal(
    tx: &mut Transaction<'_, Sqlite>,
    profile: &Profile,
    target: &Target,
    previous: u64,
) -> Result<(), StoreError> {
    let rev = checked_increment(previous)?;
    let at = current_timestamp();
    if previous == 0 {
        sqlx::query(
            "INSERT INTO projects(id,kind,name,revision,created_at) VALUES(?,'convert',?,1,?)",
        )
        .bind(&profile.id)
        .bind(&profile.name)
        .bind(&at)
        .execute(&mut **tx)
        .await?;
    } else {
        advance(tx, &profile.id, previous, rev).await?;
    }
    sqlx::query("INSERT INTO source_port_attempts(id,project_id,proposal_revision,target_json) VALUES(?,?,?,?)").bind(&target.attempt_id).bind(&profile.id).bind(requested_i64(rev)?).bind(serde_json::to_string(target)?).execute(&mut **tx).await?;
    event(tx, target, rev, "awaiting_review", "source.proposed", &at).await
}

pub(crate) async fn record_compare(
    tx: &mut Transaction<'_, Sqlite>,
    selected: &Selected,
) -> Result<(), StoreError> {
    if selected.requested {
        return Ok(());
    }
    let rev = checked_increment(selected.revision)?;
    advance(tx, &selected.target.project_id, selected.revision, rev).await?;
    let updated = sqlx::query(
        "UPDATE source_port_attempts SET compare_requested=1 WHERE id=? AND compare_requested=0",
    )
    .bind(&selected.target.attempt_id)
    .execute(&mut **tx)
    .await?;
    if updated.rows_affected() != 1 {
        return Err(StoreError::CorruptData);
    }
    event(
        tx,
        &selected.target,
        rev,
        "queued",
        "source.comparison_requested",
        &current_timestamp(),
    )
    .await
}

async fn advance(
    tx: &mut Transaction<'_, Sqlite>,
    id: &str,
    previous: u64,
    next: u64,
) -> Result<(), StoreError> {
    let updated = sqlx::query("UPDATE projects SET revision=? WHERE id=? AND revision=?")
        .bind(requested_i64(next)?)
        .bind(id)
        .bind(requested_i64(previous)?)
        .execute(&mut **tx)
        .await?;
    if updated.rows_affected() != 1 {
        return Err(StoreError::StaleRevision {
            current_revision: super::project_revision(tx, id).await?,
        });
    }
    Ok(())
}

async fn event(
    tx: &mut Transaction<'_, Sqlite>,
    target: &Target,
    rev: u64,
    phase: &str,
    kind: &str,
    at: &str,
) -> Result<(), StoreError> {
    append_event(
        tx,
        NewEvent {
            project_id: Some(&target.project_id),
            job_id: None,
            attempt_id: Some(&target.attempt_id),
            revision: rev,
            phase: Some(&Phase::new(phase)?),
            event_type: kind,
            at,
            payload: &json!({"candidate_sha256":target.candidate_sha256}),
        },
    )
    .await?;
    Ok(())
}
