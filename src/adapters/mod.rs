//! Bounded clients for the private Fourmilab and inspector Unix sockets.

#[cfg(unix)]
mod fourmilab;
#[cfg(unix)]
mod inspector;
#[cfg(unix)]
mod unix_http;

#[cfg(unix)]
pub use fourmilab::{FourmilabAction, FourmilabActionReceipt, FourmilabAdapter};
#[cfg(unix)]
pub use inspector::{
    InspectorAdapter, InspectorMcpReceipt, InspectorPairReceipt, InspectorRunReceipt,
    InspectorSampleSlot,
};
#[cfg(unix)]
pub use unix_http::BodyStream;

#[cfg(unix)]
use std::time::Duration;

/// Resource and timeout limits shared by both private legacy adapters.
#[cfg(unix)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AdapterLimits {
    /// Maximum time allowed to establish a Unix-socket connection.
    pub connect_timeout: Duration,
    /// Maximum total time allowed for request I/O and response reads.
    pub read_timeout: Duration,
    /// Maximum response-header bytes, including the terminating blank line.
    pub max_header_bytes: usize,
    /// Maximum decoded JSON response bytes.
    pub max_json_body_bytes: usize,
    /// Maximum decoded Fourmilab package bytes.
    pub max_package_body_bytes: usize,
    /// Preferred chunk size for streamed bodies.
    pub stream_chunk_bytes: usize,
}

#[cfg(unix)]
impl Default for AdapterLimits {
    fn default() -> Self {
        Self {
            connect_timeout: Duration::from_secs(2),
            read_timeout: Duration::from_secs(30),
            max_header_bytes: 32 * 1024,
            max_json_body_bytes: 1024 * 1024,
            max_package_body_bytes: 512 * 1024 * 1024,
            stream_chunk_bytes: 16 * 1024,
        }
    }
}

/// Failures visible at the private legacy-engine boundary.
#[cfg(unix)]
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum AdapterError {
    /// The Unix socket could not be reached.
    #[error("legacy engine is unreachable")]
    Unreachable,
    /// A configured connect or I/O deadline elapsed.
    #[error("legacy engine request timed out")]
    Timeout,
    /// A request or response exceeded its explicit size limit.
    #[error("legacy message exceeded its configured size limit")]
    TooLarge,
    /// The legacy engine rejected a well-formed request.
    #[error("legacy engine returned HTTP {status}: {code}")]
    Legacy {
        /// Legacy HTTP status.
        status: u16,
        /// Stable legacy error text from the JSON `error` field.
        code: String,
    },
    /// The peer violated the bounded HTTP or endpoint contract.
    #[error("legacy protocol error: {0}")]
    Protocol(String),
}

#[cfg(unix)]
async fn decode_json<T>(response: unix_http::PendingResponse) -> Result<T, AdapterError>
where
    T: serde::de::DeserializeOwned,
{
    let response = require_success(response).await?;
    require_content_type(&response, "application/json")?;
    let body = response.collect().await?;
    serde_json::from_slice(&body)
        .map_err(|_| AdapterError::Protocol("legacy response was not valid JSON".to_owned()))
}

#[cfg(unix)]
async fn require_success(
    response: unix_http::PendingResponse,
) -> Result<unix_http::PendingResponse, AdapterError> {
    if (200..300).contains(&response.status()) {
        return Ok(response);
    }
    let status = response.status();
    require_content_type(&response, "application/json")?;
    let body = response.collect().await?;
    let value: serde_json::Value = serde_json::from_slice(&body).map_err(|_| {
        AdapterError::Protocol("legacy error response was not valid JSON".to_owned())
    })?;
    let code = value
        .get("error")
        .and_then(serde_json::Value::as_str)
        .filter(|code| !code.is_empty())
        .ok_or_else(|| {
            AdapterError::Protocol("legacy error response omitted its error code".to_owned())
        })?;
    Err(AdapterError::Legacy {
        status,
        code: code.to_owned(),
    })
}

#[cfg(unix)]
fn require_content_type(
    response: &unix_http::PendingResponse,
    expected: &str,
) -> Result<(), AdapterError> {
    let actual = response
        .header("content-type")
        .and_then(|value| value.split(';').next())
        .map(str::trim);
    if actual.is_some_and(|value| value.eq_ignore_ascii_case(expected)) {
        Ok(())
    } else {
        Err(AdapterError::Protocol(format!(
            "legacy response content type was not {expected}"
        )))
    }
}
