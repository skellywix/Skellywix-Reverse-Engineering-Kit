//! Adapter for the passive inspector desk and metadata-only MCP socket.

use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::io::AsyncRead;

use super::unix_http::UnixHttp;
use super::{AdapterError, AdapterLimits, decode_json, require_content_type, require_success};

const AUTHORITY: &str = "localhost";
const JSON_BODY_LIMIT: usize = 8192;
const MAX_SAMPLE_BYTES: u64 = 16 * 1024 * 1024;

/// One of the inspector's two transient specimen slots.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InspectorSampleSlot {
    /// Primary specimen. Replacing it clears B.
    A,
    /// Comparison specimen. The legacy engine requires A first.
    B,
}

impl InspectorSampleSlot {
    fn path(self) -> &'static str {
        match self {
            Self::A => "/api/sample/a",
            Self::B => "/api/sample/b",
        }
    }
}

/// Result and updated state returned by `/api/run`.
#[derive(Debug, Deserialize, PartialEq)]
pub struct InspectorRunReceipt {
    /// Opaque legacy evidence for the selected tool.
    pub result: Value,
    /// Updated case projection after recording the evidence.
    pub state: Value,
}

/// One-time agent credential and the updated case state.
#[derive(Debug, Deserialize, PartialEq)]
pub struct InspectorPairReceipt {
    /// Private bearer token issued by the legacy engine.
    pub token: String,
    /// Updated case projection.
    pub state: Value,
}

/// One response from the metadata-only Streamable HTTP MCP endpoint.
#[derive(Debug, PartialEq)]
pub struct InspectorMcpReceipt {
    /// HTTP status, including 202 for accepted notifications.
    pub status: u16,
    /// JSON-RPC body when the endpoint returned one.
    pub body: Option<Value>,
    /// Session identifier returned by MCP initialization.
    pub session_id: Option<String>,
}

/// Bounded client for both owner-private inspector sockets.
#[derive(Clone, Debug)]
pub struct InspectorAdapter {
    desk: UnixHttp,
    agent: UnixHttp,
    limits: AdapterLimits,
}

impl InspectorAdapter {
    /// Creates an adapter with production limits.
    pub fn new(desk_socket: impl AsRef<Path>, agent_socket: impl AsRef<Path>) -> Self {
        Self::with_limits(desk_socket, agent_socket, AdapterLimits::default())
    }

    /// Creates an adapter with explicit limits, primarily for deployment tuning and tests.
    pub fn with_limits(
        desk_socket: impl AsRef<Path>,
        agent_socket: impl AsRef<Path>,
        limits: AdapterLimits,
    ) -> Self {
        Self {
            desk: UnixHttp::new(desk_socket, AUTHORITY, limits),
            agent: UnixHttp::new(agent_socket, AUTHORITY, limits),
            limits,
        }
    }

    /// Reads the current in-memory case projection.
    pub async fn state(&self) -> Result<Value, AdapterError> {
        self.get_json("/api/state").await
    }

    /// Streams one specimen into A or B without retaining a second Rust-side copy.
    pub async fn upload_sample<R>(
        &self,
        revision: u64,
        slot: InspectorSampleSlot,
        content_length: u64,
        body: R,
    ) -> Result<Value, AdapterError>
    where
        R: AsyncRead + Unpin + Send,
    {
        if content_length > MAX_SAMPLE_BYTES {
            return Err(AdapterError::TooLarge);
        }
        let revision = revision.to_string();
        let response = self
            .desk
            .request_from(
                "POST",
                slot.path(),
                &[
                    ("Origin", "http://localhost"),
                    ("Content-Type", "application/octet-stream"),
                    ("X-Yuzu-Revision", &revision),
                    ("Accept", "application/json"),
                ],
                content_length,
                body,
                self.limits.max_json_body_bytes,
            )
            .await?;
        decode_json(response).await
    }

    /// Clears both specimens and the case ledger.
    pub async fn reset(&self, revision: u64) -> Result<Value, AdapterError> {
        self.post_json("/api/reset", revision, &json!({})).await
    }

    /// Issues a one-case metadata-agent credential.
    pub async fn pair(&self, revision: u64) -> Result<InspectorPairReceipt, AdapterError> {
        self.post_json("/api/pair", revision, &json!({})).await
    }

    /// Revokes the current metadata-agent credential.
    pub async fn disconnect(&self, revision: u64) -> Result<Value, AdapterError> {
        self.post_json("/api/disconnect", revision, &json!({}))
            .await
    }

    /// Runs one passive inspector evidence tool.
    pub async fn run(
        &self,
        revision: u64,
        tool: &str,
        arguments: Value,
    ) -> Result<InspectorRunReceipt, AdapterError> {
        self.post_json(
            "/api/run",
            revision,
            &json!({ "tool": tool, "arguments": arguments }),
        )
        .await
    }

    /// Downloads the current `yuzu.report/1` evidence object.
    pub async fn report(&self) -> Result<Value, AdapterError> {
        self.get_json("/api/report").await
    }

    /// Runs the deterministic folio review and optionally requests a critic.
    pub async fn review(
        &self,
        revision: u64,
        reference: Option<&str>,
        critique: bool,
    ) -> Result<Value, AdapterError> {
        self.post_json(
            "/api/review",
            revision,
            &json!({ "reference": reference, "critique": critique }),
        )
        .await
    }

    /// Reads critic availability for the loaded revision.
    pub async fn critic_status(&self) -> Result<Value, AdapterError> {
        self.get_json("/api/critic").await
    }

    /// Claims one pending, closed critic brief.
    pub async fn critic_claim(&self, revision: u64) -> Result<Value, AdapterError> {
        self.post_json("/api/critic/claim", revision, &json!({}))
            .await
    }

    /// Finishes or explicitly fails one claimed critic ticket.
    pub async fn critic_finish(
        &self,
        revision: u64,
        ticket: &str,
        note: Option<Value>,
    ) -> Result<Value, AdapterError> {
        self.post_json(
            "/api/critic/finish",
            revision,
            &json!({ "ticket": ticket, "note": note }),
        )
        .await
    }

    /// Cancels only the critic snapshot identified by its ending event number.
    pub async fn critic_cancel(&self, revision: u64, end: u64) -> Result<Value, AdapterError> {
        self.post_json("/api/critic/cancel", revision, &json!({ "end": end }))
            .await
    }

    /// Sends one JSON-RPC packet to the metadata-only MCP endpoint.
    pub async fn mcp_request(
        &self,
        token: &str,
        session_id: Option<&str>,
        protocol_version: Option<&str>,
        packet: Value,
    ) -> Result<InspectorMcpReceipt, AdapterError> {
        let body = serde_json::to_vec(&packet).map_err(|_| {
            AdapterError::Protocol("inspector MCP packet could not be serialized".to_owned())
        })?;
        if body.len() > JSON_BODY_LIMIT {
            return Err(AdapterError::TooLarge);
        }
        let authorization = format!("Bearer {token}");
        let mut headers = Vec::with_capacity(5);
        headers.push(("Authorization", authorization.as_str()));
        headers.push(("Content-Type", "application/json"));
        headers.push(("Accept", "application/json"));
        if let Some(session_id) = session_id {
            headers.push(("Mcp-Session-Id", session_id));
        }
        if let Some(protocol_version) = protocol_version {
            headers.push(("MCP-Protocol-Version", protocol_version));
        }
        let response = self
            .agent
            .request(
                "POST",
                "/mcp",
                &headers,
                Some(&body),
                self.limits.max_json_body_bytes,
            )
            .await?;
        self.mcp_receipt(response).await
    }

    /// Closes the active metadata-agent MCP session.
    pub async fn mcp_disconnect(&self, token: &str, session_id: &str) -> Result<(), AdapterError> {
        let authorization = format!("Bearer {token}");
        let response = self
            .agent
            .request(
                "DELETE",
                "/mcp",
                &[
                    ("Authorization", &authorization),
                    ("Mcp-Session-Id", session_id),
                    ("Accept", "application/json"),
                ],
                None,
                self.limits.max_json_body_bytes,
            )
            .await?;
        let receipt = self.mcp_receipt(response).await?;
        if receipt.body.is_some() {
            return Err(AdapterError::Protocol(
                "MCP disconnect returned an unexpected body".to_owned(),
            ));
        }
        Ok(())
    }

    async fn get_json<T>(&self, target: &str) -> Result<T, AdapterError>
    where
        T: serde::de::DeserializeOwned,
    {
        let response = self
            .desk
            .request(
                "GET",
                target,
                &[("Accept", "application/json")],
                None,
                self.limits.max_json_body_bytes,
            )
            .await?;
        decode_json(response).await
    }

    async fn post_json<T, P>(
        &self,
        target: &str,
        revision: u64,
        payload: &P,
    ) -> Result<T, AdapterError>
    where
        T: serde::de::DeserializeOwned,
        P: Serialize + ?Sized,
    {
        let body = serde_json::to_vec(payload).map_err(|_| {
            AdapterError::Protocol("inspector request could not be serialized".to_owned())
        })?;
        if body.len() > JSON_BODY_LIMIT {
            return Err(AdapterError::TooLarge);
        }
        let revision = revision.to_string();
        let response = self
            .desk
            .request(
                "POST",
                target,
                &[
                    ("Origin", "http://localhost"),
                    ("Content-Type", "application/json"),
                    ("X-Yuzu-Revision", &revision),
                    ("Accept", "application/json"),
                ],
                Some(&body),
                self.limits.max_json_body_bytes,
            )
            .await?;
        decode_json(response).await
    }

    async fn mcp_receipt(
        &self,
        response: super::unix_http::PendingResponse,
    ) -> Result<InspectorMcpReceipt, AdapterError> {
        let response = require_success(response).await?;
        require_content_type(&response, "application/json")?;
        let status = response.status();
        let session_id = response.header("mcp-session-id").map(str::to_owned);
        let bytes = response.collect().await?;
        let body = if bytes.is_empty() {
            None
        } else {
            Some(serde_json::from_slice(&bytes).map_err(|_| {
                AdapterError::Protocol("inspector MCP response was not valid JSON".to_owned())
            })?)
        };
        Ok(InspectorMcpReceipt {
            status,
            body,
            session_id,
        })
    }
}
