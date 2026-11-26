//! Interpret present-tense protected observations; GET never claims a job or changes a revision.
use super::{PortResult, Profile, SourcePortRoot, blocked, disk::RunState};
use crate::{
    artifacts::{Comparison, ExpectedCase, GateError, Target, checked_source},
    store::source_port::Selected,
};

const NO_CANDIDATE_RUNTIME: &str =
    "The fixed verifier has no approved candidate-only runtime. No build or comparison ran.";

pub(crate) struct Reading {
    pub phase: &'static str,
    pub approved: bool,
    pub matched: Option<usize>,
    pub blocker: Option<String>,
    pub export: bool,
}
impl Reading {
    fn phase(phase: &'static str, approved: bool) -> Self {
        Self {
            phase,
            approved,
            matched: None,
            blocker: None,
            export: false,
        }
    }
    pub(crate) fn blocked(reason: impl Into<String>, approved: bool) -> Self {
        Self {
            blocker: Some(reason.into()),
            ..Self::phase("blocked", approved)
        }
    }
}

fn coverage(expected: &[ExpectedCase], result: &Comparison) -> bool {
    result.build_exit == Some(0)
        && expected.len() == result.cases.len()
        && expected
            .iter()
            .zip(&result.cases)
            .all(|(case, observed)| case.id == observed.id)
}

impl SourcePortRoot {
    pub(crate) async fn authorize_compare(&self, p: &Profile, s: &Selected) -> PortResult<()> {
        if !p.binds(&s.target) {
            return Err(blocked(
                "The registered source, recipe, toolchain, or corpus has changed.",
            ));
        }
        let _bytes = self.candidate(&s.target).await?;
        if self.approval(&s.target).await?.as_ref() != Some(&s.target) {
            return Err(blocked("The operator has not approved this exact attempt."));
        }
        self.expected(p).await?;
        Ok(())
    }

    pub(crate) async fn reading(
        &self,
        p: &Profile,
        s: &Selected,
        source: &str,
    ) -> PortResult<Reading> {
        let t = &s.target;
        if !p.binds(t) {
            return Ok(Reading::blocked(
                "The installed profile has changed. Submit a new proposal against the current source.",
                false,
            ));
        }
        let approval = self.approval(t).await?;
        let Some(approved) = &approval else {
            return Ok(Reading::phase("awaiting_review", false));
        };
        if approved != t {
            return Ok(Reading::blocked(
                GateError::WrongApproval.to_string(),
                false,
            ));
        }
        if !s.requested {
            return Ok(Reading::phase("approved", true));
        }
        Ok(self
            .comparison_reading(p, t, approved, source)
            .await
            .unwrap_or_else(|_| {
                Reading::blocked(
                    "The protected comparison evidence cannot be validated.",
                    true,
                )
            }))
    }

    async fn comparison_reading(
        &self,
        p: &Profile,
        t: &Target,
        approved: &Target,
        source: &str,
    ) -> PortResult<Reading> {
        if !self.published(t).await? {
            return Ok(Reading {
                blocker: Some(
                    "The request was saved but not published. Request the comparison again."
                        .to_owned(),
                ),
                ..Reading::phase("approved", true)
            });
        }
        let expected = self.expected(p).await?;
        let result = self.result(&p.id, &t.attempt_id).await?;
        let Some(observed) = &result else {
            if let Some(r) = self.runtime(p).await?
                && (r.attempt_id.as_deref() == Some(&t.attempt_id) || r.attempt_id.is_none())
            {
                match r.state {
                    RunState::Blocked => return Ok(Reading::blocked(NO_CANDIDATE_RUNTIME, true)),
                    RunState::Failed => {
                        return Ok(Reading::blocked(
                            "The verifier reported a failed check. No complete comparison is available.",
                            true,
                        ));
                    }
                    RunState::Unavailable => {
                        return Ok(Reading::blocked(
                            "The verifier reported that its prerequisites are unavailable.",
                            true,
                        ));
                    }
                    RunState::Exercised => {
                        return Ok(Reading::blocked(
                            "The reported comparison has no result record.",
                            true,
                        ));
                    }
                }
            }
            return Ok(Reading::phase("queued", true));
        };
        if &observed.target == t
            && observed.build_exit.is_none()
            && observed.cases.is_empty()
            && let Some(r) = self.runtime(p).await?
            && r.attempt_id.as_deref() == Some(t.attempt_id.as_str())
            && matches!(r.state, RunState::Blocked)
        {
            return Ok(Reading::blocked(NO_CANDIDATE_RUNTIME, true));
        }
        let measured = &observed.target == t && coverage(&expected, observed);
        let matched = measured.then(|| {
            expected
                .iter()
                .zip(&observed.cases)
                .filter(|(case, row)| {
                    !row.used_fallback
                        && row.original_sha256 == case.expected_sha256
                        && row.replay_sha256 == case.expected_sha256
                        && row.candidate_output_sha256 == case.expected_sha256
                })
                .count()
        });
        match checked_source(
            t,
            &expected,
            Some(approved),
            Some(observed),
            source.as_bytes(),
        ) {
            Ok(_) => Ok(Reading {
                phase: "verified",
                approved: true,
                matched,
                blocker: None,
                export: true,
            }),
            Err(e) => Ok(Reading {
                phase: if e == GateError::CandidateMismatch {
                    "mismatch"
                } else {
                    "blocked"
                },
                approved: true,
                matched,
                blocker: Some(e.to_string()),
                export: false,
            }),
        }
    }

    pub(crate) async fn recipe_observation(
        &self,
        p: &Profile,
    ) -> PortResult<(&'static str, &'static str)> {
        let Some(r) = self.runtime(p).await? else {
            return Ok(("configured", "No build recorded."));
        };
        match r.state {
            RunState::Blocked => Ok(("unavailable", NO_CANDIDATE_RUNTIME)),
            RunState::Failed => Ok((
                "failed",
                "The verifier reported a failed build or comparison.",
            )),
            RunState::Unavailable => Ok((
                "unavailable",
                "The fixed verifier reported an unavailable prerequisite.",
            )),
            RunState::Exercised => {
                let Some(id) = r.attempt_id else {
                    return Ok((
                        "unavailable",
                        "The reported exercise has no attempt identity.",
                    ));
                };
                let Some(result) = self.result(&p.id, &id).await? else {
                    return Ok(("unavailable", "The reported exercise has no result record."));
                };
                if result.target.attempt_id != id
                    || !p.binds(&result.target)
                    || !coverage(&self.expected(p).await?, &result)
                {
                    return Ok((
                        "failed",
                        "The result does not cover the current recipe and its registered cases.",
                    ));
                }
                Ok((
                    "exercised",
                    "The fixed verifier measured every registered case. See the attempt for match results.",
                ))
            }
        }
    }
}
