//! Files shared by the source-port control plane and its fixed verifier.

use axum::http::StatusCode;
use serde::{Deserialize, Deserializer, Serialize, de};
use std::{path::Path, sync::Arc};
use tokio::fs;

use crate::{
    api::ApiError,
    artifacts::{Sha256Id, Target},
};

pub(crate) mod disk;
pub(crate) mod evidence;

pub(crate) const PROJECTS: [&str; 2] = ["line-count", "status-handler"];
pub(crate) const MAX_SOURCE: usize = 256 * 1024;
pub(crate) const MAX_CASES: usize = 4096;
pub(crate) type PortResult<T> = Result<T, ApiError>;

pub(crate) fn registered_kind(id: &str) -> Option<(&'static str, &'static str)> {
    match id {
        "line-count" => Some(("cli", "line_count.py")),
        "status-handler" => Some(("http", "status_handler.py")),
        _ => None,
    }
}

pub(crate) fn unavailable(reason: &'static str) -> ApiError {
    ApiError::new(StatusCode::BAD_GATEWAY, "engine_unavailable", reason)
}
pub(crate) fn blocked(reason: &'static str) -> ApiError {
    ApiError::new(StatusCode::CONFLICT, "illegal_transition", reason)
}
pub(crate) fn missing() -> ApiError {
    ApiError::new(
        StatusCode::NOT_FOUND,
        "not_found",
        "Unknown source project or attempt.",
    )
}

#[derive(Debug)]
pub(crate) struct V1;
impl<'de> Deserialize<'de> for V1 {
    fn deserialize<D: Deserializer<'de>>(input: D) -> Result<Self, D::Error> {
        match u64::deserialize(input)? {
            1 => Ok(Self),
            _ => Err(de::Error::custom("unsupported record version")),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Profile {
    #[serde(rename = "schema_version")]
    _version: V1,
    pub id: String,
    pub name: String,
    pub kind: String,
    pub language: String,
    pub source_sha256: Sha256Id,
    pub corpus_sha256: Sha256Id,
    pub recipe_sha256: Sha256Id,
    pub toolchain_sha256: Sha256Id,
    pub required_cases: usize,
}

#[derive(Serialize)]
pub(crate) struct ProjectSummary {
    id: String,
    name: String,
    source_sha256: Sha256Id,
}

impl Profile {
    pub fn into_summary(self) -> ProjectSummary {
        ProjectSummary {
            id: self.id,
            name: self.name,
            source_sha256: self.source_sha256,
        }
    }
    pub fn target(&self, attempt_id: String, candidate_sha256: Sha256Id) -> Target {
        Target {
            project_id: self.id.clone(),
            attempt_id,
            source_sha256: self.source_sha256,
            candidate_sha256,
            corpus_sha256: self.corpus_sha256,
            recipe_sha256: self.recipe_sha256,
            toolchain_sha256: self.toolchain_sha256,
        }
    }
    pub fn binds(&self, target: &Target) -> bool {
        target.project_id == self.id
            && target.source_sha256 == self.source_sha256
            && target.corpus_sha256 == self.corpus_sha256
            && target.recipe_sha256 == self.recipe_sha256
            && target.toolchain_sha256 == self.toolchain_sha256
    }
}

/// Operator-configured root; never accepted from an HTTP request.
#[derive(Clone)]
pub struct SourcePortRoot(pub Arc<Path>);

impl SourcePortRoot {
    /// Refuse a mount that could be served as a static asset or contain the static tree.
    pub async fn outside_static(root: &Path, static_dir: &Path) -> std::io::Result<Self> {
        let root = fs::canonicalize(root).await?;
        let assets = fs::canonicalize(static_dir).await?;
        if !fs::metadata(&root).await?.is_dir()
            || root.starts_with(&assets)
            || assets.starts_with(&root)
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "source-port and static roots must be separate directories",
            ));
        }
        Ok(Self(Arc::from(root)))
    }
}
