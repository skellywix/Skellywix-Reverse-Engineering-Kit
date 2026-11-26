use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use re_kit_workspace::domain::{
    CommandKind, CommandRequest, EngineAvailability, JobStatus, Outcome, Phase, ProjectKind,
};
use re_kit_workspace::events::EventReplay;
use re_kit_workspace::store::{LegacyEngine, LegacyJobState, LegacyProjection, Store, StoreError};
use serde_json::{Value, json};

static NEXT_DATABASE: AtomicU64 = AtomicU64::new(1);
const FOURMILAB_STATE: &[u8] = include_bytes!("fixtures/workflows/reconstruct/state_verified.json");
const RUN_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

type TestResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

struct TestDatabase {
    path: PathBuf,
}

impl TestDatabase {
    async fn open(label: &str) -> Result<(Self, Store), StoreError> {
        let suffix = NEXT_DATABASE.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "re-kit-legacy-{label}-{}-{suffix}.sqlite3",
            std::process::id()
        ));
        let store = Store::open(&path).await?;
        Ok((Self { path }, store))
    }
}

impl Drop for TestDatabase {
    fn drop(&mut self) {
        for path in [
            self.path.clone(),
            PathBuf::from(format!("{}-shm", self.path.display())),
            PathBuf::from(format!("{}-wal", self.path.display())),
        ] {
            if let Err(error) = std::fs::remove_file(&path)
                && error.kind() != std::io::ErrorKind::NotFound
            {
                eprintln!("failed to remove {}: {error}", path.display());
            }
        }
    }
}

fn fourmilab_projection() -> TestResult<LegacyProjection> {
    let evidence: Value = serde_json::from_slice(FOURMILAB_STATE)?;
    Ok(LegacyProjection {
        engine: LegacyEngine::Fourmilab,
        availability: EngineAvailability::Up,
        detail: "available".to_owned(),
        normalized: json!({
            "run": RUN_ID,
            "attempt": 1,
            "status": "verified",
            "stage": "package"
        }),
        evidence: Some(evidence),
        job: None,
    })
}

#[tokio::test]
async fn projection_persists_bounded_evidence_and_emits_only_on_change() -> TestResult {
    let (_database, store) = TestDatabase::open("deduplicate").await?;
    let projection = fourmilab_projection()?;

    let first = store.record_legacy_projection(&projection).await?;
    assert!(first.changed);
    assert!(first.event_id.is_some());
    let duplicate = store.record_legacy_projection(&projection).await?;
    assert!(!duplicate.changed);
    assert!(duplicate.event_id.is_none());

    let Some(record) = store.legacy_projection(LegacyEngine::Fourmilab).await? else {
        return Err(std::io::Error::other("projection must be retained").into());
    };
    assert_eq!(record.availability, EngineAvailability::Up);
    assert_eq!(record.detail, "available");
    assert_eq!(record.normalized, projection.normalized);
    assert_eq!(record.evidence, projection.evidence);
    assert_eq!(record.evidence_sha256.len(), 64);

    let EventReplay::Events { events, .. } = store.replay_events(0).await? else {
        panic!("fresh store unexpectedly reported a retention gap");
    };
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].event_type, "engine.state_changed");
    assert_eq!(events[0].payload["engine"], "fourmilab");
    assert_eq!(events[0].payload["evidence_sha256"], record.evidence_sha256);
    assert!(events[0].payload.get("legacy_evidence").is_none());

    let snapshot = store.snapshot().await?;
    assert_eq!(snapshot.engines.fourmilab.state, EngineAvailability::Up);
    assert_eq!(snapshot.engines.fourmilab.detail, "available");
    assert_eq!(
        snapshot.engines.inspector.state,
        EngineAvailability::Unknown
    );
    Ok(())
}

#[tokio::test]
async fn oversized_evidence_changes_neither_projection_nor_events() -> TestResult {
    let (_database, store) = TestDatabase::open("bounded").await?;
    let initial = fourmilab_projection()?;
    store.record_legacy_projection(&initial).await?;
    let Some(before) = store.legacy_projection(LegacyEngine::Fourmilab).await? else {
        return Err(std::io::Error::other("initial projection must be retained").into());
    };

    let oversized = LegacyProjection {
        normalized: json!({"status":"oversized"}),
        evidence: Some(json!({"padding":"x".repeat(1024 * 1024)})),
        ..initial
    };
    let result = store.record_legacy_projection(&oversized).await;
    assert!(matches!(result, Err(StoreError::InvalidInput(_))));

    let Some(after) = store.legacy_projection(LegacyEngine::Fourmilab).await? else {
        return Err(std::io::Error::other("initial projection must remain").into());
    };
    assert_eq!(after, before);
    let EventReplay::Events { events, .. } = store.replay_events(0).await? else {
        panic!("fresh store unexpectedly reported a retention gap");
    };
    assert_eq!(events.len(), 1);
    Ok(())
}

#[tokio::test]
async fn projection_and_unambiguous_job_transition_commit_together() -> TestResult {
    let (_database, store) = TestDatabase::open("job-transition").await?;
    let project = store
        .apply_command(
            "legacy-project",
            &CommandRequest {
                command: CommandKind::CreateProject,
                project_id: None,
                job_id: None,
                expected_revision: 0,
                payload: json!({"kind": ProjectKind::Reconstruct, "name":"legacy"}),
            },
        )
        .await?;
    let queued = store
        .apply_command(
            "legacy-job",
            &CommandRequest {
                command: CommandKind::StartJob,
                project_id: Some(project.project_id),
                job_id: None,
                expected_revision: project.revision,
                payload: json!({"phase":"intake"}),
            },
        )
        .await?;
    let Some(job_id) = queued.job_id else {
        return Err(std::io::Error::other("queued receipt must contain job id").into());
    };

    let mut projection = fourmilab_projection()?;
    projection.job = Some(LegacyJobState {
        legacy_key: RUN_ID.to_owned(),
        status: JobStatus::Succeeded,
        phase: Phase::new("package")?,
        verified: true,
    });
    let receipt = store.record_legacy_projection(&projection).await?;
    assert!(receipt.changed);
    assert_eq!(receipt.job_id.as_deref(), Some(job_id.as_str()));

    let snapshot = store.snapshot().await?;
    let Some(job) = snapshot.jobs.iter().find(|job| job.id == job_id) else {
        return Err(std::io::Error::other("mapped job must remain in the snapshot").into());
    };
    assert_eq!(job.status, JobStatus::Succeeded);
    assert_eq!(job.phase, Phase::new("package")?);
    assert_eq!(job.outcome, Some(Outcome::Verified));

    let cursor = snapshot.cursor;
    let duplicate = store.record_legacy_projection(&projection).await?;
    assert!(!duplicate.changed);
    assert_eq!(store.snapshot().await?.cursor, cursor);
    Ok(())
}

#[tokio::test]
async fn same_status_phase_change_updates_the_job_and_emits_an_event() -> TestResult {
    let (_database, store) = TestDatabase::open("job-phase-transition").await?;
    let project = store
        .apply_command(
            "phase-project",
            &CommandRequest {
                command: CommandKind::CreateProject,
                project_id: None,
                job_id: None,
                expected_revision: 0,
                payload: json!({"kind": ProjectKind::Reconstruct, "name":"phase"}),
            },
        )
        .await?;
    let queued = store
        .apply_command(
            "phase-job",
            &CommandRequest {
                command: CommandKind::StartJob,
                project_id: Some(project.project_id),
                job_id: None,
                expected_revision: project.revision,
                payload: json!({"phase":"intake"}),
            },
        )
        .await?;
    let Some(job_id) = queued.job_id else {
        return Err(std::io::Error::other("queued receipt must contain job id").into());
    };

    let mut analyze = fourmilab_projection()?;
    analyze.normalized = json!({"run":RUN_ID,"status":"running","stage":"analyze"});
    analyze.evidence = Some(json!({"observation":"analyze"}));
    analyze.job = Some(LegacyJobState {
        legacy_key: RUN_ID.to_owned(),
        status: JobStatus::Running,
        phase: Phase::new("analyze")?,
        verified: false,
    });
    store.record_legacy_projection(&analyze).await?;
    let cursor = store.snapshot().await?.cursor;

    let mut rebuild = analyze;
    rebuild.normalized = json!({"run":RUN_ID,"status":"running","stage":"rebuild"});
    rebuild.evidence = Some(json!({"observation":"rebuild"}));
    rebuild.job = Some(LegacyJobState {
        legacy_key: RUN_ID.to_owned(),
        status: JobStatus::Running,
        phase: Phase::new("rebuild")?,
        verified: false,
    });
    let receipt = store.record_legacy_projection(&rebuild).await?;
    assert!(receipt.changed);
    assert_eq!(receipt.job_id.as_deref(), Some(job_id.as_str()));

    let snapshot = store.snapshot().await?;
    let Some(job) = snapshot.jobs.iter().find(|job| job.id == job_id) else {
        return Err(std::io::Error::other("mapped job must remain in the snapshot").into());
    };
    assert_eq!(job.status, JobStatus::Running);
    assert_eq!(job.phase, Phase::new("rebuild")?);

    let EventReplay::Events { events, .. } = store.replay_events(cursor).await? else {
        panic!("recent phase transition unexpectedly reported a retention gap");
    };
    assert_eq!(
        events
            .iter()
            .map(|event| event.event_type.as_str())
            .collect::<Vec<_>>(),
        vec!["engine.state_changed", "job.phase_changed"]
    );
    let phase_event = &events[1];
    assert_eq!(phase_event.job_id.as_deref(), Some(job_id.as_str()));
    assert_eq!(
        phase_event.phase.as_ref().map(Phase::as_str),
        Some("rebuild")
    );
    assert_eq!(phase_event.payload["status"], "running");
    assert_eq!(phase_event.payload["from"], "analyze");
    assert_eq!(phase_event.payload["to"], "rebuild");
    Ok(())
}

#[tokio::test]
async fn event_evidence_hash_resolves_after_a_later_projection_without_duplicate_bytes()
-> TestResult {
    let (_database, store) = TestDatabase::open("evidence-history").await?;
    let first = fourmilab_projection()?;
    let first_evidence = first
        .evidence
        .clone()
        .ok_or_else(|| std::io::Error::other("fixture must include evidence"))?;
    store.record_legacy_projection(&first).await?;
    let EventReplay::Events { events, .. } = store.replay_events(0).await? else {
        panic!("fresh store unexpectedly reported a retention gap");
    };
    let evidence_sha256 = events[0].payload["evidence_sha256"]
        .as_str()
        .ok_or_else(|| std::io::Error::other("event must reference evidence"))?
        .to_owned();

    let mut repeated_evidence = first;
    repeated_evidence.normalized =
        json!({"run":RUN_ID,"attempt":1,"status":"running","stage":"verify"});
    store.record_legacy_projection(&repeated_evidence).await?;

    let mut latest = repeated_evidence;
    latest.normalized = json!({"run":RUN_ID,"attempt":1,"status":"running","stage":"package"});
    latest.evidence = Some(json!({"observation":"later"}));
    store.record_legacy_projection(&latest).await?;

    assert_eq!(
        store
            .legacy_evidence(LegacyEngine::Fourmilab, &evidence_sha256)
            .await?,
        Some(first_evidence)
    );
    let retained_copies: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM legacy_evidence_records \
         WHERE engine = ? AND evidence_sha256 = ?",
    )
    .bind(LegacyEngine::Fourmilab.as_str())
    .bind(&evidence_sha256)
    .fetch_one(store.pool())
    .await?;
    assert_eq!(retained_copies, 1);
    Ok(())
}
