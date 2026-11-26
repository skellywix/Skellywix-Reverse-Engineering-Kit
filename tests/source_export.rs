use re_kit_workspace::artifacts::{
    CaseObservation, Comparison, ExpectedCase, GateError, Sha256Id, Target, checked_source,
};
use serde_json::{Value, json};

const SOURCE: &[u8] = b"fn main() { println!(\"hello\"); }\n";

fn example() -> (Target, Vec<ExpectedCase>, Comparison) {
    let target = Target {
        project_id: "greeting".into(),
        attempt_id: "attempt-1".into(),
        source_sha256: Sha256Id::of(b"original"),
        candidate_sha256: Sha256Id::of(SOURCE),
        corpus_sha256: Sha256Id::of(b"corpus"),
        recipe_sha256: Sha256Id::of(b"recipe"),
        toolchain_sha256: Sha256Id::of(b"toolchain"),
    };
    let expected: Vec<_> = [("blank", false, "greeting\n"), ("unicode", true, "hello\n")]
        .into_iter()
        .map(|(id, held_out, out)| ExpectedCase {
            id: id.into(),
            held_out,
            expected_sha256: Sha256Id::of(out.as_bytes()),
        })
        .collect();
    let cases = expected
        .iter()
        .map(|row| CaseObservation {
            id: row.id.clone(),
            original_sha256: row.expected_sha256,
            replay_sha256: row.expected_sha256,
            candidate_output_sha256: row.expected_sha256,
            used_fallback: false,
        })
        .collect();
    let comparison = Comparison {
        target: target.clone(),
        build_exit: Some(0),
        cases,
    };
    (target, expected, comparison)
}

#[test]
fn exports_the_exact_source_and_rejects_edits_after_a_passing_comparison() {
    let (t, cases, receipt) = example();
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Ok(SOURCE)
    );
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), b"fn main() {}"),
        Err(GateError::ChangedSource)
    );
}

#[test]
fn approval_or_receipt_from_any_other_target_is_unusable() -> Result<(), serde_json::Error> {
    let (t, cases, mut receipt) = example();
    let original = serde_json::to_value(&t)?;
    for field in [
        "project_id",
        "attempt_id",
        "source_sha256",
        "candidate_sha256",
        "corpus_sha256",
        "recipe_sha256",
        "toolchain_sha256",
    ] {
        let mut wrong = original.clone();
        wrong[field] = if field.ends_with("_id") {
            json!("another")
        } else {
            serde_json::to_value(Sha256Id::of(b"other"))?
        };
        let other: Target = serde_json::from_value(wrong)?;
        receipt.target = t.clone();
        assert_eq!(
            checked_source(&t, &cases, Some(&other), Some(&receipt), SOURCE),
            Err(GateError::WrongApproval),
            "{field}"
        );
        receipt.target = other;
        assert_eq!(
            checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
            Err(GateError::WrongResult),
            "{field}"
        );
    }
    Ok(())
}

#[test]
fn no_approval_or_unobserved_build_is_not_a_pass() {
    let (t, cases, mut receipt) = example();
    assert_eq!(
        checked_source(&t, &cases, None, Some(&receipt), SOURCE),
        Err(GateError::NotApproved)
    );
    assert_eq!(
        checked_source(&t, &cases, Some(&t), None, SOURCE),
        Err(GateError::NoResult)
    );
    for exit in [None, Some(1), Some(-1)] {
        receipt.build_exit = exit;
        assert_eq!(
            checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
            Err(GateError::BuildFailed)
        );
    }
}

#[test]
fn held_out_divergence_is_rejected_even_if_the_two_executables_agree() {
    let (t, cases, mut receipt) = example();
    let changed = Sha256Id::of(b"different bytes");
    receipt.cases[1].original_sha256 = changed;
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Err(GateError::BaselineMismatch)
    );
    receipt.cases[1].replay_sha256 = changed;
    receipt.cases[1].candidate_output_sha256 = changed;
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Err(GateError::BaselineMismatch)
    );
    receipt.cases[1].original_sha256 = cases[1].expected_sha256;
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Err(GateError::BaselineMismatch)
    );
    receipt.cases[1].replay_sha256 = cases[1].expected_sha256;
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Err(GateError::CandidateMismatch)
    );
    receipt.cases[1].candidate_output_sha256 = cases[1].expected_sha256;
    receipt.cases[1].used_fallback = true;
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Err(GateError::Fallback)
    );
}

#[test]
fn counts_cannot_hide_a_duplicate_missing_or_unregistered_case() {
    let (t, cases, receipt) = example();
    for ids in [
        vec!["blank"],
        vec!["blank", "blank"],
        vec!["blank", "unknown"],
        vec!["unicode", "blank"],
        vec!["blank", "unicode", "unknown"],
    ] {
        let mut altered = receipt.clone();
        altered.cases = ids
            .iter()
            .map(|id| {
                let mut row = receipt.cases[0].clone();
                row.id = (*id).into();
                row
            })
            .collect();
        assert_eq!(
            checked_source(&t, &cases, Some(&t), Some(&altered), SOURCE),
            Err(GateError::CaseCoverage),
            "{ids:?}"
        );
    }
}

#[test]
fn rejects_empty_duplicate_and_public_only_protected_corpora() {
    let (t, mut cases, mut receipt) = example();
    cases[1].held_out = false;
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Err(GateError::InvalidCorpus)
    );
    cases[1].held_out = true;
    cases[1].id = cases[0].id.clone();
    receipt.cases[1].id = cases[1].id.clone();
    assert_eq!(
        checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
        Err(GateError::InvalidCorpus)
    );
    receipt.cases.clear();
    assert_eq!(
        checked_source(&t, &[], Some(&t), Some(&receipt), SOURCE),
        Err(GateError::InvalidCorpus)
    );
}

#[test]
fn disk_digest_format_is_lowercase_sha256_and_not_an_untrusted_pass_flag()
-> Result<(), serde_json::Error> {
    let wire = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
    assert_eq!(serde_json::to_value(Sha256Id::of(b"abc"))?, json!(wire));
    for invalid in [
        json!(wire.to_ascii_uppercase()),
        json!(&wire[..63]),
        json!("z".repeat(64)),
        Value::Null,
    ] {
        assert!(serde_json::from_value::<Sha256Id>(invalid).is_err());
    }
    let (_, _, r) = example();
    let mut bad = serde_json::to_value(r)?;
    bad["verified"] = json!(true);
    assert!(serde_json::from_value::<Comparison>(bad).is_err());
    Ok(())
}

#[test]
fn even_a_matching_digest_does_not_make_a_binary_a_source_download() {
    for bytes in [&b""[..], &b"\xff"[..], &b"hello\x00world"[..]] {
        let (mut t, cases, mut receipt) = example();
        t.candidate_sha256 = Sha256Id::of(bytes);
        receipt.target = t.clone();
        assert_eq!(
            checked_source(&t, &cases, Some(&t), Some(&receipt), bytes),
            Err(GateError::NotSourceText)
        );
    }
}

#[test]
fn empty_project_and_attempt_ids_do_not_identify_a_review() {
    for field in ["project", "attempt"] {
        let (mut t, cases, mut receipt) = example();
        if field == "project" {
            t.project_id.clear();
        } else {
            t.attempt_id = " ".into();
        }
        receipt.target = t.clone();
        assert_eq!(
            checked_source(&t, &cases, Some(&t), Some(&receipt), SOURCE),
            Err(GateError::InvalidTarget)
        );
    }
}
