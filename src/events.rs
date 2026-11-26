use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::domain::Phase;

pub const EVENT_SCHEMA_VERSION: u8 = 1;
pub const MAX_EVENT_PAYLOAD_BYTES: usize = 16 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventEnvelope {
    pub schema_version: u8,
    pub event_id: u64,
    pub project_id: Option<String>,
    pub job_id: Option<String>,
    pub attempt_id: Option<String>,
    pub revision: u64,
    pub phase: Option<Phase>,
    #[serde(rename = "type")]
    pub event_type: String,
    pub at: String,
    pub payload: Value,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EventReplay {
    Events {
        events: Vec<EventEnvelope>,
        high_water: u64,
    },
    Reset {
        cursor: u64,
    },
}
