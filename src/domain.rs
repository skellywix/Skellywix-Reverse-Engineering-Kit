use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;
use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum DomainError {
    #[error("invalid job status")]
    InvalidJobStatus,
    #[error("invalid project kind")]
    InvalidProjectKind,
    #[error("invalid outcome")]
    InvalidOutcome,
    #[error("phase must be a lowercase token")]
    InvalidPhase,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Queued,
    Starting,
    Running,
    AwaitingInput,
    Blocked,
    Cancelling,
    Cancelled,
    Failed,
    Succeeded,
    Interrupted,
}

impl JobStatus {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::AwaitingInput => "awaiting_input",
            Self::Blocked => "blocked",
            Self::Cancelling => "cancelling",
            Self::Cancelled => "cancelled",
            Self::Failed => "failed",
            Self::Succeeded => "succeeded",
            Self::Interrupted => "interrupted",
        }
    }

    #[must_use]
    pub const fn can_transition_to(self, next: Self) -> bool {
        matches!(
            (self, next),
            (Self::Queued, Self::Starting)
                | (
                    Self::Starting,
                    Self::Running | Self::Failed | Self::Interrupted | Self::Cancelling
                )
                | (
                    Self::Running,
                    Self::AwaitingInput
                        | Self::Blocked
                        | Self::Cancelling
                        | Self::Failed
                        | Self::Succeeded
                        | Self::Interrupted
                )
                | (
                    Self::AwaitingInput,
                    Self::Running | Self::Cancelling | Self::Interrupted
                )
                | (
                    Self::Blocked,
                    Self::Running | Self::Cancelling | Self::Failed | Self::Interrupted
                )
                | (
                    Self::Cancelling,
                    Self::Cancelled | Self::Failed | Self::Interrupted
                )
        )
    }

    #[must_use]
    pub const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Cancelled | Self::Failed | Self::Succeeded | Self::Interrupted
        )
    }

    #[must_use]
    pub const fn outcome(self) -> Option<Outcome> {
        match self {
            Self::Cancelled | Self::Interrupted => Some(Outcome::Unverified),
            Self::Failed => Some(Outcome::Failed),
            Self::Succeeded => Some(Outcome::Unverified),
            _ => None,
        }
    }
}

impl fmt::Display for JobStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for JobStatus {
    type Err = DomainError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "queued" => Ok(Self::Queued),
            "starting" => Ok(Self::Starting),
            "running" => Ok(Self::Running),
            "awaiting_input" => Ok(Self::AwaitingInput),
            "blocked" => Ok(Self::Blocked),
            "cancelling" => Ok(Self::Cancelling),
            "cancelled" => Ok(Self::Cancelled),
            "failed" => Ok(Self::Failed),
            "succeeded" => Ok(Self::Succeeded),
            "interrupted" => Ok(Self::Interrupted),
            _ => Err(DomainError::InvalidJobStatus),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProjectKind {
    Reconstruct,
    Inspect,
    Convert,
}

impl ProjectKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Reconstruct => "reconstruct",
            Self::Inspect => "inspect",
            Self::Convert => "convert",
        }
    }
}

impl fmt::Display for ProjectKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for ProjectKind {
    type Err = DomainError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "reconstruct" => Ok(Self::Reconstruct),
            "inspect" => Ok(Self::Inspect),
            "convert" => Ok(Self::Convert),
            _ => Err(DomainError::InvalidProjectKind),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Verified,
    Unverified,
    Failed,
}

impl Outcome {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Verified => "verified",
            Self::Unverified => "unverified",
            Self::Failed => "failed",
        }
    }
}

impl FromStr for Outcome {
    type Err = DomainError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "verified" => Ok(Self::Verified),
            "unverified" => Ok(Self::Unverified),
            "failed" => Ok(Self::Failed),
            _ => Err(DomainError::InvalidOutcome),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
#[serde(transparent)]
pub struct Phase(String);

impl Phase {
    pub fn new(value: impl Into<String>) -> Result<Self, DomainError> {
        let value = value.into();
        let mut bytes = value.bytes();
        let Some(first) = bytes.next() else {
            return Err(DomainError::InvalidPhase);
        };
        if !first.is_ascii_lowercase()
            || !bytes.all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
            })
        {
            return Err(DomainError::InvalidPhase);
        }
        Ok(Self(value))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Phase {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for Phase {
    type Err = DomainError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        Self::new(value)
    }
}

impl<'de> Deserialize<'de> for Phase {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        Self::new(value).map_err(serde::de::Error::custom)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandKind {
    CreateProject,
    StartJob,
    CancelJob,
    RetryJob,
    ResumeJob,
    ResolveDecision,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandRequest {
    pub command: CommandKind,
    pub project_id: Option<String>,
    pub job_id: Option<String>,
    pub expected_revision: u64,
    #[serde(default = "empty_object")]
    pub payload: Value,
}

fn empty_object() -> Value {
    Value::Object(serde_json::Map::new())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandReceipt {
    pub project_id: String,
    pub job_id: Option<String>,
    pub revision: u64,
    pub cursor: u64,
    pub replayed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Project {
    pub id: String,
    pub kind: ProjectKind,
    pub name: String,
    pub revision: u64,
    pub created_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Job {
    pub id: String,
    pub project_id: String,
    pub attempt_id: String,
    pub attempt: u32,
    pub status: JobStatus,
    pub phase: Phase,
    pub outcome: Option<Outcome>,
    pub revision: u64,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineAvailability {
    Up,
    Down,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EngineSnapshot {
    pub state: EngineAvailability,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EnginesSnapshot {
    pub fourmilab: EngineSnapshot,
    pub inspector: EngineSnapshot,
}

impl Default for EnginesSnapshot {
    fn default() -> Self {
        let unavailable = || EngineSnapshot {
            state: EngineAvailability::Unknown,
            detail: "not observed".to_owned(),
        };
        Self {
            fourmilab: unavailable(),
            inspector: unavailable(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct ReleasesSnapshot {
    pub active: Option<Value>,
    pub previous: Option<Value>,
    pub staged: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceSnapshot {
    pub schema_version: u8,
    pub cursor: u64,
    pub generated_at: String,
    pub projects: Vec<Project>,
    pub jobs: Vec<Job>,
    pub engines: EnginesSnapshot,
    pub releases: ReleasesSnapshot,
}
