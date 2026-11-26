use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use re_kit_workspace::domain::{
    CommandKind, CommandRequest, JobStatus, Outcome, Phase, ProjectKind,
};
use re_kit_workspace::events::EventReplay;
use re_kit_workspace::store::{Store, StoreError};
use serde_json::json;

static NEXT_DATABASE: AtomicU64 = AtomicU64::new(1);

struct TestDatabase {
    path: PathBuf,
}

impl TestDatabase {
    async fn open(name: &str) -> Result<(Self, Store), StoreError> {
        let suffix = NEXT_DATABASE.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "re-kit-{name}-{}-{suffix}.sqlite3",
            std::process::id()
        ));
        let store = Store::open(&path).await?;
        Ok((Self { path }, store))
    }
}

impl Drop for TestDatabase {
    fn drop(&mut self) {
        remove_database_file(&self.path);
        remove_database_file(&PathBuf::from(format!("{}-shm", self.path.display())));
        remove_database_file(&PathBuf::from(format!("{}-wal", self.path.display())));
    }
}

fn remove_database_file(path: &Path) {
    if let Err(error) = std::fs::remove_file(path)
        && error.kind() != std::io::ErrorKind::NotFound
    {
        eprintln!("failed to remove test database {}: {error}", path.display());
    }
}

fn create_project_request(name: &str) -> CommandRequest {
    CommandRequest {
        command: CommandKind::CreateProject,
        project_id: None,
        job_id: None,
        expected_revision: 0,
        payload: json!({"kind": ProjectKind::Convert, "name": name}),
    }
}

async fn queued_job(store: &Store, key_prefix: &str) -> Result<(String, String), StoreError> {
    let project = store
        .apply_command(
            &format!("{key_prefix}-project"),
            &create_project_request(key_prefix),
        )
        .await?;
    let job = store
        .apply_command(
            &format!("{key_prefix}-job"),
            &CommandRequest {
                command: CommandKind::StartJob,
                project_id: Some(project.project_id.clone()),
                job_id: None,
                expected_revision: project.revision,
                payload: json!({"phase": "analyze"}),
            },
        )
        .await?;
    let Some(job_id) = job.job_id else {
        panic!("start_job receipt did not contain a job id");
    };
    Ok((project.project_id, job_id))
}

#[tokio::test]
async fn failed_event_insert_rolls_back_transition_and_event() -> Result<(), StoreError> {
    let (_database, store) = TestDatabase::open("rollback").await?;
    let (_project_id, job_id) = queued_job(&store, "rollback").await?;
    let starting = store
        .append_transition(&job_id, 1, JobStatus::Starting, None)
        .await?;

    sqlx::query(
        "CREATE TRIGGER reject_running_event BEFORE INSERT ON events \
         WHEN NEW.payload_json LIKE '%\"to\":\"running\"%' \
         BEGIN SELECT RAISE(ABORT, 'test event failure'); END",
    )
    .execute(store.pool())
    .await?;

    let result = store
        .append_transition(&job_id, starting.revision, JobStatus::Running, None)
        .await;
    assert!(matches!(result, Err(StoreError::Database(_))));

    let snapshot = store.snapshot().await?;
    let job = snapshot.jobs.iter().find(|job| job.id == job_id);
    assert!(matches!(job, Some(job) if job.status == JobStatus::Starting && job.revision == 2));

    let replay = store.replay_events(0).await?;
    let EventReplay::Events { events, .. } = replay else {
        panic!("fresh store unexpectedly reported a retention gap");
    };
    assert!(!events.iter().any(|event| {
        event.event_type == "job.status_changed"
            && event.payload.get("to") == Some(&json!("running"))
    }));
    Ok(())
}

#[tokio::test]
async fn duplicate_idempotency_key_replays_one_job() -> Result<(), StoreError> {
    let (_database, store) = TestDatabase::open("idempotent").await?;
    let project = store
        .apply_command("idempotent-project", &create_project_request("idempotent"))
        .await?;
    let request = CommandRequest {
        command: CommandKind::StartJob,
        project_id: Some(project.project_id),
        job_id: None,
        expected_revision: project.revision,
        payload: json!({"phase": "record"}),
    };

    let first = store.apply_command("same-key", &request).await?;
    let second = store.apply_command("same-key", &request).await?;

    assert!(!first.replayed);
    assert!(second.replayed);
    assert_eq!(first.job_id, second.job_id);
    let job_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM jobs")
        .fetch_one(store.pool())
        .await?;
    assert_eq!(job_count, 1);
    Ok(())
}

#[tokio::test]
async fn reused_idempotency_key_with_different_body_is_rejected() -> Result<(), StoreError> {
    let (_database, store) = TestDatabase::open("idempotency-conflict").await?;
    store
        .apply_command("same-key", &create_project_request("first"))
        .await?;

    let result = store
        .apply_command("same-key", &create_project_request("second"))
        .await;

    assert!(matches!(result, Err(StoreError::IdempotencyConflict)));
    Ok(())
}

#[tokio::test]
async fn stale_project_revision_reports_current_revision() -> Result<(), StoreError> {
    let (_database, store) = TestDatabase::open("stale").await?;
    let project = store
        .apply_command("stale-project", &create_project_request("stale"))
        .await?;

    let result = store
        .apply_command(
            "stale-start",
            &CommandRequest {
                command: CommandKind::StartJob,
                project_id: Some(project.project_id),
                job_id: None,
                expected_revision: 0,
                payload: json!({"phase": "analyze"}),
            },
        )
        .await;

    assert!(matches!(
        result,
        Err(StoreError::StaleRevision {
            current_revision: 1
        })
    ));
    Ok(())
}

#[tokio::test]
async fn illegal_transition_changes_neither_job_nor_event_log() -> Result<(), StoreError> {
    let (_database, store) = TestDatabase::open("illegal").await?;
    let (_project_id, job_id) = queued_job(&store, "illegal").await?;
    let before = store.snapshot().await?;

    let result = store
        .append_transition(&job_id, 1, JobStatus::Running, Some(Phase::new("record")?))
        .await;

    assert!(matches!(
        result,
        Err(StoreError::IllegalTransition {
            from: JobStatus::Queued,
            to: JobStatus::Running
        })
    ));
    let after = store.snapshot().await?;
    assert_eq!(after.cursor, before.cursor);
    assert_eq!(after.jobs, before.jobs);
    Ok(())
}

#[tokio::test]
async fn succeeded_attempt_is_unverified_without_checked_evidence() -> Result<(), StoreError> {
    let (_database, store) = TestDatabase::open("unverified-success").await?;
    let project = store
        .apply_command(
            "unverified-project",
            &create_project_request("unverified-success"),
        )
        .await?;
    let job = store
        .apply_command(
            "unverified-job",
            &CommandRequest {
                command: CommandKind::StartJob,
                project_id: Some(project.project_id),
                job_id: None,
                expected_revision: project.revision,
                payload: json!({
                    "phase": "analyze",
                    "verified": true,
                    "outcome": "verified"
                }),
            },
        )
        .await?;
    let Some(job_id) = job.job_id else {
        panic!("start_job receipt did not contain a job id");
    };
    let starting = store
        .append_transition(&job_id, 1, JobStatus::Starting, None)
        .await?;
    let running = store
        .append_transition(&job_id, starting.revision, JobStatus::Running, None)
        .await?;
    store
        .append_transition(&job_id, running.revision, JobStatus::Succeeded, None)
        .await?;

    let snapshot = store.snapshot().await?;
    let job = snapshot.jobs.iter().find(|job| job.id == job_id);
    assert!(matches!(
        job,
        Some(job)
            if job.status == JobStatus::Succeeded
                && job.outcome == Some(Outcome::Unverified)
    ));
    let stored_attempt: (String, Option<String>) =
        sqlx::query_as("SELECT status, outcome FROM attempts WHERE job_id = ?")
            .bind(&job_id)
            .fetch_one(store.pool())
            .await?;
    assert_eq!(
        stored_attempt,
        ("succeeded".to_owned(), Some("unverified".to_owned()))
    );
    Ok(())
}

#[tokio::test]
async fn restart_reconciliation_interrupts_uncertain_attempts() -> Result<(), StoreError> {
    let (_database, store) = TestDatabase::open("reconcile").await?;

    let (_project_id, starting_id) = queued_job(&store, "starting").await?;
    store
        .append_transition(&starting_id, 1, JobStatus::Starting, None)
        .await?;

    let (_project_id, running_id) = queued_job(&store, "running").await?;
    store
        .append_transition(&running_id, 1, JobStatus::Starting, None)
        .await?;
    store
        .append_transition(&running_id, 2, JobStatus::Running, None)
        .await?;

    let (_project_id, waiting_id) = queued_job(&store, "waiting").await?;
    store
        .append_transition(&waiting_id, 1, JobStatus::Starting, None)
        .await?;
    store
        .append_transition(&waiting_id, 2, JobStatus::Running, None)
        .await?;
    store
        .append_transition(&waiting_id, 3, JobStatus::AwaitingInput, None)
        .await?;

    let (_project_id, blocked_id) = queued_job(&store, "blocked").await?;
    store
        .append_transition(&blocked_id, 1, JobStatus::Starting, None)
        .await?;
    store
        .append_transition(&blocked_id, 2, JobStatus::Running, None)
        .await?;
    store
        .append_transition(&blocked_id, 3, JobStatus::Blocked, None)
        .await?;

    let (_project_id, cancelling_id) = queued_job(&store, "cancelling").await?;
    store
        .append_transition(&cancelling_id, 1, JobStatus::Starting, None)
        .await?;
    store
        .append_transition(&cancelling_id, 2, JobStatus::Running, None)
        .await?;
    store
        .append_transition(&cancelling_id, 3, JobStatus::Cancelling, None)
        .await?;

    let reconciled = store.reconcile_restart().await?;
    assert_eq!(reconciled, 5);

    let snapshot = store.snapshot().await?;
    for job_id in [
        &starting_id,
        &running_id,
        &waiting_id,
        &blocked_id,
        &cancelling_id,
    ] {
        let job = snapshot.jobs.iter().find(|job| job.id == job_id.as_str());
        assert!(matches!(job, Some(job) if job.status == JobStatus::Interrupted));
    }
    let blocked_attempt_status: String =
        sqlx::query_scalar("SELECT status FROM attempts WHERE job_id = ?")
            .bind(&blocked_id)
            .fetch_one(store.pool())
            .await?;
    assert_eq!(blocked_attempt_status, "interrupted");
    let EventReplay::Events { events, .. } = store.replay_events(0).await? else {
        panic!("fresh store unexpectedly reported a retention gap");
    };
    assert!(events.iter().any(|event| {
        event.job_id.as_deref() == Some(blocked_id.as_str())
            && event.revision == 5
            && event.payload.get("from") == Some(&json!("blocked"))
            && event.payload.get("to") == Some(&json!("interrupted"))
            && event.payload.get("reason") == Some(&json!("restart_reconciliation"))
    }));
    Ok(())
}
