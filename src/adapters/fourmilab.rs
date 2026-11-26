//! Adapter for the fixed Fourmilab reconstruction desk.

use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::unix_http::UnixHttp;
use super::{
    AdapterError, AdapterLimits, BodyStream, decode_json, require_content_type, require_success,
};

const AUTHORITY: &str = "127.0.0.1:48784";
const ACTION_BODY_LIMIT: usize = 1024;

/// A mutation accepted by the legacy Fourmilab `/action` endpoint.
#[derive(Debug, Serialize)]
#[serde(tag = "action", rename_all = "lowercase")]
pub enum FourmilabAction {
    /// Start a new reconstruction.
    Start,
    /// Retry a prior run with its original inputs.
    Retry {
        /// Existing 32-character run identifier.
        run: String,
    },
    /// Cancel an active run.
    Cancel {
        /// Existing 32-character run identifier.
        run: String,
    },
    /// Stage the configured release candidate.
    Stage,
    /// Promote a staged release digest.
    Promote {
        /// Lowercase 64-character release digest.
        digest: String,
    },
    /// Restore the previous release.
    Rollback,
}

/// Receipt returned by a successful Fourmilab action.
#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct FourmilabActionReceipt {
    /// Always true for an accepted action.
    pub ok: bool,
    /// New run identifier for start and retry actions.
    #[serde(default)]
    pub run: Option<String>,
}

/// Bounded client for the private Fourmilab desk socket.
#[derive(Clone, Debug)]
pub struct FourmilabAdapter {
    client: UnixHttp,
    limits: AdapterLimits,
}

impl FourmilabAdapter {
    /// Creates an adapter with production limits.
    pub fn new(socket_path: impl AsRef<Path>) -> Self {
        Self::with_limits(socket_path, AdapterLimits::default())
    }

    /// Creates an adapter with explicit limits, primarily for deployment tuning and tests.
    pub fn with_limits(socket_path: impl AsRef<Path>, limits: AdapterLimits) -> Self {
        Self {
            client: UnixHttp::new(socket_path, AUTHORITY, limits),
            limits,
        }
    }

    /// Reads the desk snapshot, optionally selecting one run.
    pub async fn state(&self, run: Option<&str>) -> Result<Value, AdapterError> {
        let target = match run {
            Some(run) => {
                require_lower_hex(run, 32, "Fourmilab run identifier")?;
                format!("/state?run={run}")
            }
            None => "/state".to_owned(),
        };
        let response = self
            .client
            .request(
                "GET",
                &target,
                &[("Accept", "application/json")],
                None,
                self.limits.max_json_body_bytes,
            )
            .await?;
        decode_json(response).await
    }

    /// Sends one explicit operator action with the legacy same-origin marker.
    pub async fn action(
        &self,
        action: FourmilabAction,
    ) -> Result<FourmilabActionReceipt, AdapterError> {
        let body = serde_json::to_vec(&action).map_err(|_| {
            AdapterError::Protocol("Fourmilab action could not be serialized".to_owned())
        })?;
        if body.len() > ACTION_BODY_LIMIT {
            return Err(AdapterError::TooLarge);
        }
        let response = self
            .client
            .request(
                "POST",
                "/action",
                &[
                    ("Origin", "http://127.0.0.1:48784"),
                    ("Content-Type", "application/json"),
                    ("X-Candidate-Action", "1"),
                    ("Accept", "application/json"),
                ],
                Some(&body),
                self.limits.max_json_body_bytes,
            )
            .await?;
        let receipt: FourmilabActionReceipt = decode_json(response).await?;
        if !receipt.ok {
            return Err(AdapterError::Protocol(
                "Fourmilab action receipt was not accepted".to_owned(),
            ));
        }
        Ok(receipt)
    }

    /// Opens a bounded stream for one verified package.
    pub async fn package_stream(&self, run: &str) -> Result<BodyStream, AdapterError> {
        require_lower_hex(run, 32, "Fourmilab run identifier")?;
        let target = format!("/package/{run}");
        let response = self
            .client
            .request(
                "GET",
                &target,
                &[("Accept", "application/zip")],
                None,
                self.limits.max_package_body_bytes,
            )
            .await?;
        let response = require_success(response).await?;
        require_content_type(&response, "application/zip")?;
        Ok(response.into_body_stream())
    }
}

fn require_lower_hex(value: &str, length: usize, label: &str) -> Result<(), AdapterError> {
    if value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err(AdapterError::Protocol(format!("{label} was invalid")))
    }
}
