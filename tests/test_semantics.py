import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


TOOL = Path(__file__).resolve().parents[1] / "tools" / "semantics.py"
REFERENCE = hashlib.sha256(b"test reference").hexdigest()
CANDIDATE = hashlib.sha256(b"test candidate").hexdigest()


def event(value, output, *, state="ready", action="scale", after="ready"):
    return {"state": state, "action": action, "input": value, "next_state": after, "output": output}


def trace(rows, artifact=REFERENCE):
    return {"format": "behavior-trace-v1", "basis": "black-box", "interface": "dial",
            "artifact_sha256": artifact, "observations": rows}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BehaviorHandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rows = [event(3, 11), event(-2, -4), event(0, 2), event(3, 11),
                     event(None, "ok", action="pause", after="paused")]

    def put(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def cli(self, *args):
        return subprocess.run([sys.executable, "-IB", str(TOOL), *args], cwd=self.root,
                              capture_output=True, encoding="utf-8", timeout=10, check=False)

    def analyze(self, rows):
        original = self.put("private-trace.json", trace(rows))
        spec = self.root / "spec.json"
        result = self.cli("analyze", original, "--out", spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        return original, spec

    def test_handoff_separates_witnesses_from_an_inferred_rule(self):
        original, spec = self.analyze(self.rows)
        doc = json.loads(spec.read_bytes())
        self.assertEqual(doc["format"], "behavior-spec-v1")
        self.assertEqual(doc["reference"], {"basis": "black-box", "artifact_sha256": REFERENCE,
                                             "trace_sha256": digest(original)})
        self.assertEqual(doc["observed"], [self.rows[4] | {"rows": [5]}, self.rows[1] | {"rows": [2]},
                                           self.rows[2] | {"rows": [3]}, self.rows[0] | {"rows": [1, 4]}])
        self.assertEqual(doc["hypotheses"], [{"kind": "integer-affine", "state": "ready", "action": "scale",
                                             "next_state": "ready", "multiplier": 3, "offset": 2,
                                             "min_input": -2, "max_input": 3, "distinct_inputs": 3}])
        self.assertNotIn(str(original), spec.read_text(encoding="utf-8"))

    def test_no_rule_from_repeats_a_counterexample_or_a_change_of_state(self):
        rows = [event(0, 1), event(1, 2), event(1, 2)]
        rows += [event(i, y, action="curve") for i, y in [(0, 0), (1, 1), (2, 4)]]
        rows += [event(i, i, action="switch", after=a) for i, a in [(0, "ready"), (1, "ready"), (2, "paused")]]
        rows += [event(i, y, action="fraction") for i, y in [(0, 0), (2, 1), (4, 2)]]
        rows += [event(i, y, action="mixed") for i, y in [(1, 1), (2, 2), (3, 3), ("other", "ignored")]]
        _, spec = self.analyze(rows)
        self.assertEqual(json.loads(spec.read_bytes())["hypotheses"], [])

    def test_conflicting_observations_name_witnesses_and_leave_no_handoff(self):
        original = self.put("ambiguous.json", trace([event(1, 3), event(1, 3, after="paused")]))
        spec = self.root / "spec.json"
        result = self.cli("analyze", original, "--out", spec)
        self.assertEqual(result.returncode, 2)
        self.assertIn("rows 1 and 2", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(spec.exists())

    def test_candidate_uses_only_the_handoff_and_extra_inputs_are_not_judged(self):
        original, spec = self.analyze(self.rows)
        original.unlink()  # The implementer has no access to the original capture.
        candidate = self.put("candidate.json", trace(list(reversed(self.rows)) + [event(1, -99)], CANDIDATE))
        result = self.cli("check", spec, candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], report["matched"], report["unassessed"]), ("matches-observed", 4, 1))
        self.assertEqual((report["missing"], report["mismatches"]), ([], []))
        self.assertEqual(report["spec_sha256"], digest(spec))
        self.assertEqual(report["candidate_trace_sha256"], digest(candidate))
        self.assertEqual((report["reference_artifact_sha256"], report["candidate_artifact_sha256"]),
                         (REFERENCE, CANDIDATE))

    def test_checker_reports_wrong_state_wrong_output_and_missing_case(self):
        _, spec = self.analyze(self.rows)
        rows = [event(3, 11, after="paused"), self.rows[4], event(0, 99)]
        result = self.cli("check", spec, self.put("candidate.json", trace(rows, CANDIDATE)))
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], report["matched"]), ("mismatch", 1))
        self.assertEqual(report["missing"], [{"state": "ready", "action": "scale", "input": -2}])
        self.assertEqual([(m["input"], m["expected"], m["actual"]) for m in report["mismatches"]], [
            (0, {"next_state": "ready", "output": 2}, {"next_state": "ready", "output": 99}),
            (3, {"next_state": "ready", "output": 11}, {"next_state": "paused", "output": 11}),
        ])

    def test_incomplete_is_not_a_match_even_when_the_rule_fits(self):
        _, spec = self.analyze(self.rows)
        result = self.cli("check", spec, self.put("partial.json", trace([event(3, 11), event(4, 14)], CANDIDATE)))
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], len(report["missing"]), report["unassessed"]), ("incomplete", 3, 1))

    def test_checker_refuses_unrelated_or_ambiguous_capture(self):
        _, spec = self.analyze(self.rows)
        for doc in (trace(self.rows, CANDIDATE) | {"interface": "other"},
                    trace([event(2, 4), event(2, 7)], CANDIDATE)):
            with self.subTest(doc=doc):
                result = self.cli("check", spec, self.put("candidate.json", doc))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_rechecking_the_same_build_reports_identity_instead_of_refusing(self):
        original, spec = self.analyze(self.rows)
        result = self.cli("check", spec, original)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["same_artifact"])
        self.assertEqual(report["status"], "matches-observed")

    def test_original_source_alone_explains_return_paths_without_running_it(self):
        source = self.root / "original.py"
        source.write_bytes(b"open('ran', 'w').close()\n"
                           b"def adjust(units):\n"
                           b"    if units < 0:\n"
                           b"        return 0\n"
                           b"    else:\n"
                           b"        return 3 * units + 2  # private commentary\n")
        result = self.cli("analyze", "--source", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["format"], report["basis"], report["status"], report["source_sha256"]),
                         ("source-analysis-v1", "source-review", "analyzed", digest(source)))
        self.assertNotIn("private commentary", result.stdout)
        self.assertNotIn("observed", report)
        self.assertFalse((self.root / "ran").exists())
        guard, zero, scale = [f for f in report["facts"] if f["scope"] == "adjust"]
        self.assertEqual(guard["predicate"], {"op": "lt", "left": {"name": "units"}, "right": {"integer": 0}})
        self.assertEqual((zero["value"], zero["enclosing_if"]), ({"integer": 0}, {"line": 3, "arm": "then"}))
        self.assertEqual((scale["value"], scale["enclosing_if"]),
                         ({"integer_affine": {"variable": "units", "multiplier": 3, "offset": 2}},
                          {"line": 3, "arm": "else"}))
        checked = self.cli("check", self.put("source-report.json", report), self.put("candidate.json", trace(self.rows)))
        self.assertEqual(checked.returncode, 2)
        self.assertIn("no observed cases", checked.stderr)

    def test_source_and_observations_coexist_without_either_impersonating_the_other(self):
        source = self.root / "meter.py"
        source.write_bytes(b"def scale(x):\n    return x * 99\n")
        original = self.put("capture.json", trace(self.rows) | {"basis": "source-informed"})
        spec = self.root / "combined.json"
        result = self.cli("analyze", original, "--source", source, "--out", spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(spec.read_bytes())
        self.assertEqual(doc["reference"]["basis"], "source-informed")
        static = doc["source_analysis"]
        self.assertEqual(static["source_sha256"], digest(source))
        self.assertEqual(static["facts"][0]["value"]["integer_affine"]["multiplier"], 99)
        self.assertEqual(doc["hypotheses"][0]["multiplier"], 3)
        source.unlink()
        original.unlink()
        candidate = trace(self.rows, CANDIDATE)
        del candidate["basis"]
        result = self.cli("check", spec, self.put("candidate.json", candidate))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["matched"], report["candidate_basis"], report["same_artifact"]),
                         (4, "unspecified", False))

    def test_source_preserves_small_interface_values_and_stateful_returns(self):
        source = self.root / "transition.py"
        source.write_bytes(b"def step(key, n):\n"
                           b"    if key == 'scale':\n"
                           b"        return ('ready', n + n + n + 2)\n"
                           b"    return ('ready', 'ignored')\n")
        result = self.cli("analyze", "--source", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        guard, scale, ignored = json.loads(result.stdout)["facts"]
        self.assertEqual(guard["predicate"]["right"], {"text": "scale"})
        self.assertEqual(scale["value"]["tuple"], [{"text": "ready"}, {"integer_affine": {
            "variable": "n", "multiplier": 3, "offset": 2}}])
        self.assertEqual(ignored["value"]["tuple"][1], {"text": "ignored"})

    def test_other_source_is_identified_without_claiming_a_parse_or_blocking_observations(self):
        source = self.root / "meter.c"
        source.write_bytes(b"int scale(int x) { return x * 3 + 2; }\n")
        result = self.cli("analyze", self.put("capture.json", trace(self.rows)), "--source", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertEqual((doc["source_analysis"]["status"], doc["source_analysis"]["source_sha256"]),
                         ("identity-only", digest(source)))
        self.assertEqual(len(doc["observed"]), 4)

    def test_syntax_error_is_not_a_successful_source_analysis(self):
        source = self.root / "no-suffix"
        source.write_bytes(b"def broken(: # private-syntax-text\n    pass\n")
        result = self.cli("analyze", "--language", "python", "--source", source)
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 1", result.stderr)
        self.assertNotIn("private-syntax-text", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invalid_input_fails_without_echoing_private_text(self):
        private = "private content must never be echoed"
        valid = trace(self.rows)
        for doc in (valid | {"source": private}, valid | {"basis": "source-review"},
                    valid | {"artifact_sha256": "unidentified"}, trace([]), trace([event(True, 2)]),
                    trace([event(0, 1.0)]), trace([event(2**31, 0)]), trace([event(None, private)]),
                    trace([event(1, 3) | {"note": private}]), trace([event(1, 1)] * 1025)):
            with self.subTest(doc_type=list(doc)):
                result = self.cli("analyze", self.put("bad.json", doc))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(private, result.stderr)
        path = self.root / "bad.json"
        path.write_text(json.dumps(valid).replace('"basis":', '"basis":"source-review","basis":'), encoding="utf-8")
        result = self.cli("analyze", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr.lower())
        path.write_bytes(b" " * (1024 * 1024) + json.dumps(valid).encode())
        self.assertEqual(self.cli("analyze", path).returncode, 2)

    def test_an_edited_hypothesis_or_invalid_witness_is_not_a_valid_spec(self):
        _, spec = self.analyze(self.rows)
        candidate = self.put("candidate.json", trace(self.rows, CANDIDATE))
        for field, value in (("observed", []), ("hypotheses", []), ("rows", [True]), ("rows", [4])):
            with self.subTest(field=field, value=value):
                altered = json.loads(spec.read_bytes())
                if field == "rows":
                    altered["observed"][0][field] = value
                else:
                    altered[field] = value
                result = self.cli("check", self.put("altered.json", altered), candidate)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_analyze_does_not_overwrite_evidence(self):
        original = self.put("trace.json", trace(self.rows))
        before = original.read_bytes()
        result = self.cli("analyze", original, "--out", original)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(original.read_bytes(), before)
