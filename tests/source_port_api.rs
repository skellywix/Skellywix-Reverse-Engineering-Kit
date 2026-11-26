//! HTTP boundary tests with separate, disposable operator and verifier records. No code executes.
use std::{
    error::Error,
    fs,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

use axum::{
    Extension, Router,
    body::Body,
    http::{HeaderMap, Method, Request, StatusCode, header},
};
use http_body_util::BodyExt;
use re_kit_workspace::{
    api::{AppConfig, AppState, router},
    jobs::SourcePortRoot,
    store::Store,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::watch;
use tower::ServiceExt;

type TestResult<T = ()> = Result<T, Box<dyn Error>>;
const PROJECT: &str = "/api/v1/convert/projects/line-count";
const ORIGINAL: &str = "print(3)\n";
const SOURCE: &str = "fn main() { println!(\"3\"); }\r\n";
static SEQ: AtomicU64 = AtomicU64::new(0);

fn sha(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

struct Fixture {
    dir: PathBuf,
    app: Router,
    store: Store,
    _shutdown: watch::Sender<bool>,
}
struct Reply {
    status: StatusCode,
    headers: HeaderMap,
    bytes: Vec<u8>,
}
impl Reply {
    fn json(&self) -> TestResult<Value> {
        Ok(serde_json::from_slice(&self.bytes)?)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Err(e) = fs::remove_dir_all(&self.dir) {
            eprintln!("could not clean source-port fixture: {e}");
        }
    }
}
impl Fixture {
    async fn new() -> TestResult<Self> {
        let dir = std::env::temp_dir().join(format!(
            "rekit-source-api-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&dir)?;
        let data = dir.join("private");
        for path in [
            "registry/line-count/public/original",
            "gate/line-count/approval",
            "results/line-count",
            "queue/line-count",
        ] {
            fs::create_dir_all(data.join(path))?;
        }
        let public = data.join("registry/line-count/public");
        fs::write(public.join("original/line_count.py"), ORIGINAL)?;
        fs::write(public.join("brief.md"), "Print a count.\n")?;
        fs::write(
            public.join("profile.json"),
            serde_json::to_vec(
                &json!({"schema_version":1,"id":"line-count","name":"Line count","kind":"cli","language":"python","required_cases":2,"source_sha256":sha(ORIGINAL.as_bytes()),"corpus_sha256":sha(b"private corpus"),"recipe_sha256":sha(b"recipe"),"toolchain_sha256":sha(b"compiler")}),
            )?,
        )?;
        fs::write(
            data.join("gate/line-count/expected.json"),
            serde_json::to_vec(&json!([
                {"id":"a","held_out":false,"expected_sha256":sha(b"one")},
                {"id":"private-case","held_out":true,"expected_sha256":sha(b"two")}
            ]))?,
        )?;
        let store = Store::open(dir.join("control.db")).await?;
        let (shutdown, receiver) = watch::channel(false);
        let app = router(AppState::new(
            store.clone(),
            Arc::new(AppConfig {
                static_dir: dir.join("static"),
                allow_missing_origin: false,
            }),
            receiver,
        ))
        .layer(Extension(SourcePortRoot(Arc::from(data.as_path()))));
        Ok(Self {
            dir,
            app,
            store,
            _shutdown: shutdown,
        })
    }
    async fn finish(self) -> TestResult {
        self.store.pool().close().await;
        Ok(())
    }
    fn private(&self, path: &str) -> PathBuf {
        self.dir.join("private").join(path)
    }
    async fn call(&self, url: &str, post: Option<(&str, Value)>) -> TestResult<Reply> {
        let mut builder = Request::builder()
            .uri(url)
            .header(header::HOST, "workspace.test");
        let body = if let Some((key, payload)) = post {
            builder = builder
                .method(Method::POST)
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::ORIGIN, "http://workspace.test")
                .header("Idempotency-Key", key);
            Body::from(serde_json::to_vec(&payload)?)
        } else {
            Body::empty()
        };
        let response = self.app.clone().oneshot(builder.body(body)?).await?;
        let (parts, body) = response.into_parts();
        Ok(Reply {
            status: parts.status,
            headers: parts.headers,
            bytes: body.collect().await?.to_bytes().to_vec(),
        })
    }
    async fn proposal(&self, key: &str, revision: u64, text: &str) -> TestResult<Reply> {
        self.call(&format!("{PROJECT}/proposals"), Some((key,json!({"expected_revision":revision,"source_sha256":sha(ORIGINAL.as_bytes()),"rust_source":text})))).await
    }
    fn target(&self, id: &str) -> Value {
        json!({"project_id":"line-count","attempt_id":id,"source_sha256":sha(ORIGINAL.as_bytes()),"candidate_sha256":sha(SOURCE.as_bytes()),"corpus_sha256":sha(b"private corpus"),"recipe_sha256":sha(b"recipe"),"toolchain_sha256":sha(b"compiler")})
    }
    fn approve(&self, id: &str, target: Value) -> TestResult {
        Ok(fs::write(
            self.private(&format!("gate/line-count/approval/{id}.json")),
            serde_json::to_vec(&json!({"schema_version":1,"target":target}))?,
        )?)
    }
    fn result(&self, id: &str, second: &[u8]) -> TestResult {
        let path = self.private(&format!("results/line-count/{id}"));
        fs::create_dir_all(&path)?;
        let row = |id: &str, expected: &[u8], actual: &[u8]| json!({"id":id,"original_sha256":sha(expected),"replay_sha256":sha(expected),"candidate_output_sha256":sha(actual),"used_fallback":false});
        Ok(fs::write(
            path.join("result.json"),
            serde_json::to_vec(
                &json!({"schema_version":1,"target":self.target(id),"build_exit":0,"cases":[row("a",b"one",b"one"),row("private-case",b"two",second)]}),
            )?,
        )?)
    }
    async fn compare(&self, id: &str, key: &str, revision: u64) -> TestResult<Reply> {
        self.call(&format!("{PROJECT}/compare"),Some((key,json!({"expected_revision":revision,"attempt_id":id,"candidate_sha256":sha(SOURCE.as_bytes())})))).await
    }
}
fn attempt(detail: &Value) -> TestResult<&str> {
    detail["attempt"]["id"]
        .as_str()
        .ok_or_else(|| std::io::Error::other("missing attempt id").into())
}

#[tokio::test]
async fn reads_only_expose_registered_public_material() -> TestResult {
    let f = Fixture::new().await?;
    let list = f.call("/api/v1/convert/projects", None).await?;
    assert_eq!(list.status, StatusCode::OK);
    assert_eq!(
        list.json()?,
        json!({"schema_version":1,"projects":[{"id":"line-count","name":"Line count","source_sha256":sha(ORIGINAL.as_bytes())}]})
    );
    let detail = f.call(PROJECT, None).await?.json()?;
    assert_eq!(detail["revision"], 0);
    assert_eq!(detail["phase"], "registered");
    assert!(detail["matched"].is_null());
    assert_eq!(detail["required"], 2);
    let packet = f.call(&format!("{PROJECT}/packet"), None).await?.json()?;
    assert_eq!(
        packet,
        json!({"schema_version":1,"project_id":"line-count","source_sha256":sha(ORIGINAL.as_bytes()),"language":"python","source":ORIGINAL,"brief":"Print a count.\n"})
    );
    let state = f.call("/api/v1/runtime/state", None).await?.json()?;
    assert_eq!(state["recipes"][0]["state"], "configured");
    for url in [
        "/api/v1/convert/projects/unknown",
        "/api/v1/convert/projects/line-count/approval",
        "/api/v1/runtime/exec",
    ] {
        assert_eq!(f.call(url, None).await?.status, StatusCode::NOT_FOUND);
    }
    assert!(
        fs::read_dir(f.private("queue/line-count"))?
            .next()
            .is_none()
    );
    fs::write(
        f.private("registry/line-count/public/original/line_count.py"),
        "changed",
    )?;
    assert!(
        !f.call(&format!("{PROJECT}/packet"), None)
            .await?
            .status
            .is_success()
    );
    f.finish().await
}

#[tokio::test]
async fn proposals_are_exact_and_never_queue_without_separate_approval() -> TestResult {
    let f = Fixture::new().await?;
    let p = f.proposal("save", 0, SOURCE).await?;
    assert_eq!(p.status, StatusCode::OK);
    let detail = p.json()?;
    let id = attempt(&detail)?;
    assert_eq!(detail["phase"], "awaiting_review");
    assert_eq!(detail["revision"], 1);
    assert_eq!(detail["attempt"]["rust_source"], SOURCE);
    assert_eq!(
        detail["attempt"]["candidate_sha256"],
        sha(SOURCE.as_bytes())
    );
    let dir = f.private(&format!("queue/line-count/{id}"));
    assert_eq!(fs::read(dir.join("candidate.rs"))?, SOURCE.as_bytes());
    assert!(!dir.join("job.json").exists());
    assert_eq!(
        f.proposal("save", 0, SOURCE).await?.json()?["attempt"]["id"],
        id
    );
    assert_eq!(
        f.proposal("save", 0, "different").await?.json()?["error"],
        "idempotency_conflict"
    );
    assert_eq!(
        f.proposal("stale", 0, "different").await?.json()?["error"],
        "stale_revision"
    );
    assert_eq!(f.compare(id, "run", 1).await?.status, StatusCode::CONFLICT);
    let mut wrong = f.target(id);
    wrong["recipe_sha256"] = json!(sha(b"other"));
    f.approve(id, wrong)?;
    assert_eq!(f.compare(id, "run", 1).await?.status, StatusCode::CONFLICT);
    assert!(!dir.join("job.json").exists());
    f.approve(id, f.target(id))?;
    let approved = f.call(PROJECT, None).await?.json()?;
    assert_eq!(approved["revision"], 1);
    assert_eq!(approved["phase"], "approved");
    assert!(!dir.join("job.json").exists());
    let queued = f.compare(id, "run", 1).await?;
    assert_eq!(queued.status, StatusCode::OK);
    assert_eq!(queued.json()?["phase"], "queued");
    let marker = fs::read(dir.join("job.json"))?;
    assert_eq!(
        serde_json::from_slice::<Value>(&marker)?,
        json!({"schema_version":1,"project_id":"line-count","attempt_id":id,"candidate_sha256":sha(SOURCE.as_bytes())})
    );
    assert_eq!(f.compare(id, "run", 1).await?.json()?["revision"], 2);
    assert_eq!(fs::read(dir.join("job.json"))?, marker);
    assert_eq!(fs::read_dir(f.private("queue/line-count"))?.count(), 1);
    f.finish().await
}

#[tokio::test]
async fn superseded_retry_after_profile_replacement_reports_the_current_revision() -> TestResult {
    let f = Fixture::new().await?;
    assert_eq!(
        f.proposal("original", 0, SOURCE).await?.status,
        StatusCode::OK
    );
    let public = f.private("registry/line-count/public");
    let replacement = b"print(4)\n";
    let profile_path = public.join("profile.json");
    let mut profile: Value = serde_json::from_slice(&fs::read(&profile_path)?)?;
    profile["source_sha256"] = json!(sha(replacement));
    fs::write(public.join("original/line_count.py"), replacement)?;
    fs::write(profile_path, serde_json::to_vec(&profile)?)?;
    let next = f.call(&format!("{PROJECT}/proposals"), Some(("updated", json!({
        "expected_revision": 1, "source_sha256": sha(replacement), "rust_source": "fn main() {}"
    })))).await?;
    assert_eq!(next.status, StatusCode::OK);
    let retry = f.proposal("original", 0, SOURCE).await?;
    let body = retry.json()?;
    let snapshot = f.store.snapshot().await?;
    f.finish().await?;
    assert_eq!(retry.status, StatusCode::CONFLICT);
    assert_eq!(body["error"], "stale_revision");
    assert_eq!(body["current_revision"], 2);
    assert_eq!(snapshot.cursor, 2);
    Ok(())
}

#[tokio::test]
async fn download_rechecks_current_selection_evidence_and_exact_bytes() -> TestResult {
    let f = Fixture::new().await?;
    let d = f.proposal("save", 0, SOURCE).await?;
    assert_eq!(d.status, StatusCode::OK);
    let d = d.json()?;
    let id = attempt(&d)?;
    let url = format!("{PROJECT}/attempts/{id}/source");
    assert_eq!(f.call(&url, None).await?.status, StatusCode::CONFLICT);
    f.approve(id, f.target(id))?;
    f.result(id, b"two")?;
    assert_eq!(
        f.call(&url, None).await?.status,
        StatusCode::CONFLICT,
        "unsolicited evidence must not authorize an export"
    );
    assert_eq!(f.compare(id, "run", 1).await?.status, StatusCode::OK);
    f.result(id, b"bad")?;
    let mismatch = f.call(PROJECT, None).await?.json()?;
    assert_eq!(mismatch["phase"], "mismatch");
    assert_eq!(mismatch["matched"], 1);
    assert_eq!(mismatch["required"], 2);
    assert_eq!(mismatch["export_available"], false);
    assert_eq!(f.call(&url, None).await?.status, StatusCode::CONFLICT);
    f.result(id, b"two")?;
    let detail = f.call(PROJECT, None).await?.json()?;
    assert_eq!(detail["export_available"], true);
    assert_eq!(detail["matched"], 2);
    assert_eq!(detail["phase"], "verified");
    let export = f.call(&url, None).await?;
    assert_eq!(export.status, StatusCode::OK);
    assert_eq!(export.bytes, SOURCE.as_bytes());
    assert_eq!(
        export.headers[header::CONTENT_TYPE],
        "text/plain; charset=utf-8"
    );
    assert_eq!(
        export.headers[header::CONTENT_DISPOSITION],
        "attachment; filename=\"port.rs\""
    );
    assert_eq!(export.headers[header::CACHE_CONTROL], "no-store");
    let bytes = f.private(&format!("queue/line-count/{id}/candidate.rs"));
    fs::write(&bytes, b"edited")?;
    assert_eq!(f.call(&url, None).await?.status, StatusCode::CONFLICT);
    fs::write(&bytes, SOURCE)?;
    fs::remove_file(f.private(&format!("gate/line-count/approval/{id}.json")))?;
    assert_eq!(f.call(&url, None).await?.status, StatusCode::CONFLICT);
    f.approve(id, f.target(id))?;
    assert_eq!(
        f.proposal("new", 2, "fn main() {}\n").await?.status,
        StatusCode::OK
    );
    assert_eq!(f.call(&url, None).await?.status, StatusCode::CONFLICT);
    assert_eq!(
        f.proposal("save", 0, SOURCE).await?.json()?["error"],
        "stale_revision"
    );
    assert_eq!(
        f.call(&format!("{PROJECT}/attempts/not-an-attempt/source"), None)
            .await?
            .status,
        StatusCode::NOT_FOUND
    );
    f.finish().await
}

#[tokio::test]
async fn concurrent_proposals_have_one_winner_and_refuse_client_evidence() -> TestResult {
    let f = Fixture::new().await?;
    let denied=f.call(&format!("{PROJECT}/proposals"),Some(("unsupported",json!({"expected_revision":0,"source_sha256":sha(ORIGINAL.as_bytes()),"rust_source":SOURCE,"approved":true})))).await?;
    assert_eq!(denied.status, StatusCode::BAD_REQUEST);
    assert_eq!(
        f.proposal("nul", 0, "a\0b").await?.status,
        StatusCode::BAD_REQUEST
    );
    let (a, b) = tokio::join!(
        f.proposal("one", 0, SOURCE),
        f.proposal("two", 0, "fn main() {}")
    );
    let mut codes = [a?.status.as_u16(), b?.status.as_u16()];
    codes.sort();
    assert_eq!(codes, [200, 409]);
    assert_eq!(fs::read_dir(f.private("queue/line-count"))?.count(), 1);
    f.finish().await
}

#[tokio::test]
async fn runtime_does_not_report_an_exercise_from_a_status_string() -> TestResult {
    let f = Fixture::new().await?;
    let id = "example-run";
    let file = f.private("results/line-count/runtime.json");
    let mut record = json!({"schema_version":1,"project_id":"line-count","attempt_id":id,"recipe_sha256":sha(b"recipe"),"toolchain_sha256":sha(b"compiler"),"state":"exercised","detail":"Private compiler output."});
    fs::write(&file, serde_json::to_vec(&record)?)?;
    let missing = f.call("/api/v1/runtime/state", None).await?.json()?;
    assert_eq!(missing["recipes"][0]["state"], "unavailable");
    f.result(id, b"does not match")?;
    let measured = f.call("/api/v1/runtime/state", None).await?.json()?;
    assert_eq!(measured["recipes"][0]["state"], "exercised");
    assert!(!measured.to_string().contains("Private compiler"));
    record["recipe_sha256"] = json!(sha(b"obsolete"));
    fs::write(&file, serde_json::to_vec(&record)?)?;
    assert_eq!(
        f.call("/api/v1/runtime/state", None).await?.json()?["recipes"][0]["state"],
        "unavailable"
    );
    f.finish().await
}

#[tokio::test]
async fn a_verifier_refusal_is_not_a_build_or_a_corrupt_runtime_record() -> TestResult {
    let f = Fixture::new().await?;
    let d = f.proposal("save", 0, SOURCE).await?.json()?;
    let id = attempt(&d)?;
    f.approve(id, f.target(id))?;
    assert_eq!(f.compare(id, "compare", 1).await?.status, StatusCode::OK);
    let results = f.private("results/line-count");
    fs::create_dir(results.join(id))?;
    fs::write(
        results.join(id).join("result.json"),
        serde_json::to_vec(&json!({
            "schema_version":1,"target":f.target(id),"build_exit":null,"cases":[]
        }))?,
    )?;
    fs::write(
        results.join("runtime.json"),
        serde_json::to_vec(&json!({
            "schema_version":1,"project_id":"line-count","attempt_id":id,
            "recipe_sha256":sha(b"recipe"),"toolchain_sha256":sha(b"compiler"),
            "state":"blocked","detail":"Internal verifier detail not for display."
        }))?,
    )?;
    let state = f.call("/api/v1/runtime/state", None).await?.json()?;
    let detail = f.call(PROJECT, None).await?.json()?;
    let download = f
        .call(&format!("{PROJECT}/attempts/{id}/source"), None)
        .await?;
    f.finish().await?;
    assert_eq!(state["recipes"][0]["state"], "unavailable");
    assert!(
        state["recipes"][0]["detail"]
            .as_str()
            .is_some_and(|s| s.contains("candidate-only"))
    );
    assert_eq!(detail["phase"], "blocked");
    assert!(
        detail["blocker"]
            .as_str()
            .is_some_and(|s| s.contains("candidate-only"))
    );
    assert!(detail["matched"].is_null());
    assert_eq!(detail["export_available"], false);
    assert_eq!(download.status, StatusCode::CONFLICT);
    assert!(!state.to_string().contains("Internal verifier"));
    assert!(!detail.to_string().contains("Internal verifier"));
    Ok(())
}

#[tokio::test]
async fn only_an_explicit_authorized_retry_can_publish_a_missing_queue_marker() -> TestResult {
    let f = Fixture::new().await?;
    let d = f.proposal("save", 0, SOURCE).await?.json()?;
    let id = attempt(&d)?;
    f.approve(id, f.target(id))?;
    assert_eq!(f.compare(id, "compare", 1).await?.status, StatusCode::OK);
    let marker = f.private(&format!("queue/line-count/{id}/job.json"));
    fs::remove_file(&marker)?;
    let state = f.call(PROJECT, None).await?.json()?;
    assert_eq!(state["phase"], "approved");
    assert_eq!(state["export_available"], false);
    assert!(!marker.exists());
    let approval = f.private(&format!("gate/line-count/approval/{id}.json"));
    fs::remove_file(&approval)?;
    assert_eq!(
        f.compare(id, "compare", 1).await?.status,
        StatusCode::CONFLICT
    );
    assert!(!marker.exists());
    f.approve(id, f.target(id))?;
    assert_eq!(f.compare(id, "compare", 1).await?.json()?["revision"], 2);
    assert!(marker.is_file());
    assert_eq!(f.store.snapshot().await?.cursor, 2);
    f.finish().await
}

#[tokio::test]
async fn registered_proposal_route_rejects_cross_origin_without_freezing_source() -> TestResult {
    let f = Fixture::new().await?;
    let request = Request::post(format!("{PROJECT}/proposals"))
        .header(header::HOST, "workspace.test")
        .header(header::ORIGIN, "https://other.test")
        .header(header::CONTENT_TYPE, "application/json")
        .header("Idempotency-Key", "cross-origin")
        .body(Body::from(serde_json::to_vec(&json!({
            "expected_revision": 0, "source_sha256": sha(ORIGINAL.as_bytes()), "rust_source": SOURCE,
        }))?))?;
    let response = f.app.clone().oneshot(request).await?;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    let bytes = response.into_body().collect().await?.to_bytes();
    assert_eq!(
        serde_json::from_slice::<Value>(&bytes)?["error"],
        "forbidden_origin"
    );
    assert!(
        fs::read_dir(f.private("queue/line-count"))?
            .next()
            .is_none()
    );
    assert_eq!(f.store.snapshot().await?.cursor, 0);
    f.finish().await
}

#[tokio::test]
async fn malformed_result_blocks_export_without_revoking_the_separate_approval() -> TestResult {
    let f = Fixture::new().await?;
    let d = f.proposal("save", 0, SOURCE).await?.json()?;
    let id = attempt(&d)?;
    f.approve(id, f.target(id))?;
    assert_eq!(f.compare(id, "run", 1).await?.status, StatusCode::OK);
    let dir = f.private(&format!("results/line-count/{id}"));
    fs::create_dir(&dir)?;
    fs::write(dir.join("result.json"), b"not a valid result")?;
    let detail = f.call(PROJECT, None).await?.json()?;
    let download = f
        .call(&format!("{PROJECT}/attempts/{id}/source"), None)
        .await?;
    f.finish().await?;
    assert_eq!(detail["attempt"]["approved"], true);
    assert_eq!(detail["phase"], "blocked");
    assert!(detail["matched"].is_null());
    assert_eq!(detail["export_available"], false);
    assert_eq!(download.status, StatusCode::CONFLICT);
    Ok(())
}
