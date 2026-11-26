//! Evidence required to export an exact reviewed-source proposal. No execution or filesystem I/O.

use std::fmt;

use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use sha2::{Digest, Sha256};
use thiserror::Error;

/// SHA-256 of exact bytes. Disk and API records use lowercase hex without a prefix.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Sha256Id([u8; 32]);

impl Sha256Id {
    pub fn of(bytes: &[u8]) -> Self {
        Self(Sha256::digest(bytes).into())
    }
}

impl Serialize for Sha256Id {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut text = [0; 64];
        hex::encode_to_slice(self.0, &mut text).map_err(serde::ser::Error::custom)?;
        serializer.serialize_str(std::str::from_utf8(&text).map_err(serde::ser::Error::custom)?)
    }
}

impl<'de> Deserialize<'de> for Sha256Id {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct HexDigest;
        impl de::Visitor<'_> for HexDigest {
            type Value = Sha256Id;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("64 lowercase hexadecimal characters")
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
                let mut bytes = [0; 32];
                if value.bytes().any(|b| b.is_ascii_uppercase())
                    || hex::decode_to_slice(value, &mut bytes).is_err()
                {
                    return Err(E::custom("expected a lowercase SHA-256 digest"));
                }
                Ok(Sha256Id(bytes))
            }
        }
        deserializer.deserialize_str(HexDigest)
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub project_id: String,
    pub attempt_id: String,
    pub source_sha256: Sha256Id,
    pub candidate_sha256: Sha256Id,
    pub corpus_sha256: Sha256Id,
    pub recipe_sha256: Sha256Id,
    pub toolchain_sha256: Sha256Id,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExpectedCase {
    pub id: String,
    pub held_out: bool,
    pub expected_sha256: Sha256Id,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CaseObservation {
    pub id: String,
    pub original_sha256: Sha256Id,
    pub replay_sha256: Sha256Id,
    pub candidate_output_sha256: Sha256Id,
    pub used_fallback: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Comparison {
    pub target: Target,
    pub build_exit: Option<i32>,
    pub cases: Vec<CaseObservation>,
}

#[derive(Clone, Copy, Debug, Eq, Error, PartialEq)]
pub enum GateError {
    #[error("The project or attempt identity is missing.")]
    InvalidTarget,
    #[error("This candidate has no operator approval.")]
    NotApproved,
    #[error("The approval is for a different target.")]
    WrongApproval,
    #[error("No comparison result is available.")]
    NoResult,
    #[error("The comparison is for a different target.")]
    WrongResult,
    #[error("The verifier did not observe a successful build.")]
    BuildFailed,
    #[error("The protected corpus needs unique ordered cases including a held-out case.")]
    InvalidCorpus,
    #[error("The measured cases do not cover the registered cases exactly.")]
    CaseCoverage,
    #[error("The original application did not reproduce the registered baseline.")]
    BaselineMismatch,
    #[error("The Rust proposal differs from the registered baseline.")]
    CandidateMismatch,
    #[error("A case used the original application as a fallback.")]
    Fallback,
    #[error("The proposed source no longer matches the reviewed bytes.")]
    ChangedSource,
    #[error("The artifact is not nonempty UTF-8 source text.")]
    NotSourceText,
}

/// Check one frozen attempt and return the same bytes the caller must serve.
///
/// `current` and `expected` come from the selected attempt and protected registry.
/// `approval` and `result` must come from independent operator- and verifier-owned
/// stores, never a client request, model response, or candidate-writable directory.
pub fn checked_source<'a>(
    current: &Target,
    expected: &[ExpectedCase],
    approval: Option<&Target>,
    result: Option<&Comparison>,
    source: &'a [u8],
) -> Result<&'a [u8], GateError> {
    if current.project_id.trim().is_empty() || current.attempt_id.trim().is_empty() {
        return Err(GateError::InvalidTarget);
    }
    if approval.ok_or(GateError::NotApproved)? != current {
        return Err(GateError::WrongApproval);
    }
    let result = result.ok_or(GateError::NoResult)?;
    if &result.target != current {
        return Err(GateError::WrongResult);
    }
    if result.build_exit != Some(0) {
        return Err(GateError::BuildFailed);
    }
    if !expected.iter().any(|c| c.held_out)
        || expected.iter().any(|c| c.id.trim().is_empty())
        || expected.windows(2).any(|rows| rows[0].id >= rows[1].id)
    {
        return Err(GateError::InvalidCorpus);
    }
    if expected.len() != result.cases.len() {
        return Err(GateError::CaseCoverage);
    }
    for (case, observed) in expected.iter().zip(&result.cases) {
        if case.id != observed.id {
            return Err(GateError::CaseCoverage);
        }
        if observed.original_sha256 != case.expected_sha256
            || observed.replay_sha256 != case.expected_sha256
        {
            return Err(GateError::BaselineMismatch);
        }
        if observed.used_fallback {
            return Err(GateError::Fallback);
        }
        if observed.candidate_output_sha256 != case.expected_sha256 {
            return Err(GateError::CandidateMismatch);
        }
    }
    if Sha256Id::of(source) != current.candidate_sha256 {
        return Err(GateError::ChangedSource);
    }
    if source.is_empty() || source.contains(&0) || std::str::from_utf8(source).is_err() {
        return Err(GateError::NotSourceText);
    }
    Ok(source)
}
