//! Operator/verification records are read-only. Only immutable candidate and queue files are published here.
use std::{
    io::ErrorKind,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

use serde::{Deserialize, Serialize, de::DeserializeOwned};
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncWriteExt},
};

use super::{
    MAX_CASES, MAX_SOURCE, PortResult, Profile, SourcePortRoot, V1, blocked, missing,
    registered_kind, unavailable,
};
use crate::{
    api::ApiError,
    artifacts::{CaseObservation, Comparison, ExpectedCase, Sha256Id, Target},
};

const RECORD_CAP: usize = 4 * 1024 * 1024;
const SMALL_RECORD: usize = 16 * 1024;
static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
fn io_error(_: std::io::Error) -> ApiError {
    unavailable("A source-port file could not be read or published.")
}
fn invalid() -> ApiError {
    blocked("A protected source-port record is invalid.")
}

pub(crate) fn valid_attempt(id: &str) -> bool {
    (1..=64).contains(&id.len())
        && id.as_bytes()[0].is_ascii_alphanumeric()
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}
pub(crate) fn valid_source(bytes: &[u8]) -> bool {
    !bytes.is_empty() && bytes.len() <= MAX_SOURCE && !bytes.contains(&0)
}

async fn directory(path: &Path) -> PortResult<bool> {
    match fs::symlink_metadata(path).await {
        Ok(meta) if meta.is_dir() => Ok(true),
        Ok(_) => Err(invalid()),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(false),
        Err(e) => Err(io_error(e)),
    }
}

// expected.json is an operator-installed projection to the verifier's live expected file. No other
// file link is accepted. The projection's approval *directory* may also be an operator-owned link.
async fn read_file(path: &Path, max: usize, projection: bool) -> PortResult<Option<Vec<u8>>> {
    match fs::symlink_metadata(path).await {
        Ok(m) if m.is_file() || (projection && m.is_symlink()) => (),
        Ok(_) => return Err(invalid()),
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io_error(e)),
    }
    let file = fs::File::open(path).await.map_err(io_error)?;
    let m = file.metadata().await.map_err(io_error)?;
    if !m.is_file() || m.len() > max as u64 {
        return Err(invalid());
    }
    let mut bytes = Vec::with_capacity(m.len() as usize);
    file.take((max + 1) as u64)
        .read_to_end(&mut bytes)
        .await
        .map_err(io_error)?;
    if bytes.len() > max {
        return Err(invalid());
    }
    Ok(Some(bytes))
}
async fn record<T: DeserializeOwned>(
    path: &Path,
    max: usize,
    projection: bool,
) -> PortResult<Option<T>> {
    read_file(path, max, projection)
        .await?
        .map(|bytes| serde_json::from_slice(&bytes).map_err(|_| invalid()))
        .transpose()
}
async fn text(path: &Path) -> PortResult<String> {
    let bytes = read_file(path, MAX_SOURCE, false)
        .await?
        .ok_or_else(|| blocked("The frozen source or brief is missing."))?;
    if !valid_source(&bytes) {
        return Err(invalid());
    }
    String::from_utf8(bytes).map_err(|_| invalid())
}

pub(crate) struct Installed {
    pub profile: Profile,
    pub original: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Approval {
    #[serde(rename = "schema_version")]
    _version: V1,
    target: Target,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResultFile {
    #[serde(rename = "schema_version")]
    _version: V1,
    target: Target,
    build_exit: Option<i32>,
    cases: Vec<CaseObservation>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RuntimeFile {
    #[serde(rename = "schema_version")]
    _version: V1,
    pub project_id: String,
    pub attempt_id: Option<String>,
    pub recipe_sha256: Sha256Id,
    pub toolchain_sha256: Sha256Id,
    pub state: RunState,
    // Never return free-form diagnostic text, host paths, or compiler output to a browser.
    #[serde(rename = "detail")]
    _detail: String,
}
#[derive(Deserialize, Clone, Copy)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RunState {
    Exercised,
    Failed,
    Blocked,
    Unavailable,
}
#[derive(Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct JobFile<'a> {
    schema_version: u8,
    project_id: &'a str,
    attempt_id: &'a str,
    candidate_sha256: Sha256Id,
}
impl<'a> From<&'a Target> for JobFile<'a> {
    fn from(t: &'a Target) -> Self {
        Self {
            schema_version: 1,
            project_id: &t.project_id,
            attempt_id: &t.attempt_id,
            candidate_sha256: t.candidate_sha256,
        }
    }
}

impl SourcePortRoot {
    pub(crate) async fn registry_ready(&self) -> PortResult<()> {
        if !directory(&self.0.join("registry")).await? {
            return Err(unavailable("No source registry is mounted."));
        }
        Ok(())
    }
    pub(crate) async fn installed(&self, id: &str) -> PortResult<Option<Installed>> {
        let (kind, filename) = registered_kind(id).ok_or_else(missing)?;
        let project = self.0.join("registry").join(id);
        if !directory(&project).await? {
            return Ok(None);
        }
        let public = project.join("public");
        let p: Profile = record(&public.join("profile.json"), SMALL_RECORD, false)
            .await?
            .ok_or_else(invalid)?;
        if p.id != id
            || p.kind != kind
            || p.language != "python"
            || p.name.trim().is_empty()
            || p.name.len() > 200
            || !(1..=MAX_CASES).contains(&p.required_cases)
        {
            return Err(invalid());
        }
        let original = text(&public.join("original").join(filename)).await?;
        if Sha256Id::of(original.as_bytes()) != p.source_sha256 {
            return Err(blocked(
                "The original source differs from the installed profile.",
            ));
        }
        Ok(Some(Installed {
            profile: p,
            original,
        }))
    }
    pub(crate) async fn required_project(&self, id: &str) -> PortResult<Installed> {
        if registered_kind(id).is_none() {
            return Err(missing());
        }
        self.registry_ready().await?;
        self.installed(id).await?.ok_or_else(missing)
    }
    pub(crate) async fn brief(&self, p: &Profile) -> PortResult<String> {
        text(&self.0.join("registry").join(&p.id).join("public/brief.md")).await
    }
    pub(crate) async fn expected(&self, p: &Profile) -> PortResult<Vec<ExpectedCase>> {
        let cases: Vec<ExpectedCase> = record(
            &self.0.join("gate").join(&p.id).join("expected.json"),
            RECORD_CAP,
            true,
        )
        .await?
        .ok_or_else(|| blocked("The protected comparison cases are unavailable."))?;
        if cases.len() != p.required_cases
            || !cases.iter().any(|r| r.held_out)
            || cases.iter().any(|r| r.id.trim().is_empty())
            || cases.windows(2).any(|w| w[0].id >= w[1].id)
        {
            return Err(blocked(
                "The protected cases do not match the installed profile.",
            ));
        }
        Ok(cases)
    }
    pub(crate) async fn approval(&self, t: &Target) -> PortResult<Option<Target>> {
        let file = self
            .0
            .join("gate")
            .join(&t.project_id)
            .join("approval")
            .join(format!("{}.json", t.attempt_id));
        Ok(record::<Approval>(&file, SMALL_RECORD, false)
            .await?
            .map(|r| r.target))
    }
    pub(crate) async fn result(&self, project: &str, id: &str) -> PortResult<Option<Comparison>> {
        if registered_kind(project).is_none() || !valid_attempt(id) {
            return Err(invalid());
        }
        let file = self
            .0
            .join("results")
            .join(project)
            .join(id)
            .join("result.json");
        Ok(record::<ResultFile>(&file, RECORD_CAP, false)
            .await?
            .map(|r| Comparison {
                target: r.target,
                build_exit: r.build_exit,
                cases: r.cases,
            }))
    }
    pub(crate) async fn runtime(&self, p: &Profile) -> PortResult<Option<RuntimeFile>> {
        let file = self.0.join("results").join(&p.id).join("runtime.json");
        let state = record::<RuntimeFile>(&file, SMALL_RECORD, false).await?;
        if let Some(r) = &state
            && (r.project_id != p.id
                || r.recipe_sha256 != p.recipe_sha256
                || r.toolchain_sha256 != p.toolchain_sha256
                || r.attempt_id.as_deref().is_some_and(|id| !valid_attempt(id)))
        {
            return Err(invalid());
        }
        Ok(state)
    }
    fn queue_dir(&self, t: &Target) -> PortResult<PathBuf> {
        if registered_kind(&t.project_id).is_none() || !valid_attempt(&t.attempt_id) {
            return Err(invalid());
        }
        Ok(self.0.join("queue").join(&t.project_id).join(&t.attempt_id))
    }
    pub(crate) async fn candidate(&self, t: &Target) -> PortResult<String> {
        let dir = self.queue_dir(t)?;
        if !directory(&dir).await? {
            return Err(blocked("The proposed source is missing."));
        }
        let source = text(&dir.join("candidate.rs")).await?;
        if Sha256Id::of(source.as_bytes()) != t.candidate_sha256 {
            return Err(blocked(
                "The proposed source no longer matches the frozen attempt.",
            ));
        }
        Ok(source)
    }
    pub(crate) async fn freeze(&self, t: &Target, bytes: &[u8]) -> PortResult<()> {
        let project = self.0.join("queue").join(&t.project_id);
        if !directory(&project).await? {
            return Err(unavailable(
                "No writable proposal queue is mounted for this project.",
            ));
        }
        let dir = self.queue_dir(t)?;
        #[cfg(unix)]
        fs::DirBuilder::new()
            .mode(0o750)
            .create(&dir)
            .await
            .map_err(io_error)?;
        #[cfg(not(unix))]
        fs::create_dir(&dir).await.map_err(io_error)?;
        // An interrupted proposal can leave candidate.rs only. The verifier requires job.json.
        write_new(&dir.join("candidate.rs"), bytes)
            .await
            .map_err(io_error)?;
        sync_dir(&dir).await.map_err(io_error)?;
        sync_dir(&project).await.map_err(io_error)
    }
    pub(crate) async fn published(&self, t: &Target) -> PortResult<bool> {
        let path = self.queue_dir(t)?.join("job.json");
        let Some(bytes) = read_file(&path, SMALL_RECORD, false).await? else {
            return Ok(false);
        };
        let marker: JobFile<'_> = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
        if marker != JobFile::from(t) {
            return Err(blocked("The queue marker names a different target."));
        }
        Ok(true)
    }
    pub(crate) async fn publish(&self, t: &Target) -> PortResult<()> {
        let dir = self.queue_dir(t)?;
        if self.published(t).await? {
            // A previous publisher may have linked the marker but failed to sync its directory.
            return sync_dir(&dir).await.map_err(io_error);
        }
        let data = serde_json::to_vec(&JobFile::from(t)).map_err(|_| invalid())?;
        let mut published_temp = None;
        for _ in 0..32 {
            let name = format!(
                ".job-{}-{}",
                std::process::id(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
            );
            let file = dir.join(name);
            match write_new(&file, &data).await {
                Ok(()) => {
                    published_temp = Some(file);
                    break;
                }
                Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(io_error(e)),
            }
        }
        let file = published_temp
            .ok_or_else(|| unavailable("The queue has too many incomplete publications."))?;
        let linked = fs::hard_link(&file, dir.join("job.json")).await;
        fs::remove_file(&file).await.map_err(io_error)?;
        match linked {
            Ok(()) => (),
            Err(e) if e.kind() == ErrorKind::AlreadyExists && self.published(t).await? => (),
            Err(e) => return Err(io_error(e)),
        }
        sync_dir(&dir).await.map_err(io_error)
    }
}

async fn write_new(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o640);
    let mut file = options.open(path).await?;
    file.write_all(bytes).await?;
    file.sync_all().await
}
#[cfg(unix)]
async fn sync_dir(path: &Path) -> std::io::Result<()> {
    fs::File::open(path).await?.sync_all().await
}
#[cfg(not(unix))]
async fn sync_dir(_path: &Path) -> std::io::Result<()> {
    Ok(())
}
