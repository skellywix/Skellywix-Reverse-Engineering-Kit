"""In-container runner contract: one prepared job, fresh profile, bounded OMP invocation."""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI and the dispatcher suite provide PyYAML
    yaml = None

CONTAINER = Path(__file__).resolve().parents[1] / "container"
JOB = "job-20261116-0002"
needs_yaml = unittest.skipUnless(yaml is not None, "requires PyYAML")


def load_runner():
    spec = importlib.util.spec_from_file_location("re_container_runner", CONTAINER / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE_OMP = textwrap.dedent("""
    import json, os, signal, sys
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    with open(os.path.join(here, "omp-argv.json"), "w") as record:
        json.dump({"argv": args, "cwd": os.getcwd(),
                   "require_ready": os.environ.get("OMP_MCP_REQUIRE_READY"),
                   "has_mcp_timeout": "OMP_MCP_TIMEOUT_MS" in os.environ,
                   "sentinel": os.environ.get("RE_RUNNER_TEST_SENTINEL")}, record)
    session_dir = args[args.index("--session-dir") + 1]
    os.makedirs(session_dir, exist_ok=True)
    question = open(args[-1][1:], encoding="utf-8").read()
    message = {"type": "message", "message": {"role": "assistant",
               "usage": {"cost": {"total": 0.35}}}}
    with open(os.path.join(session_dir, "s.jsonl"), "w") as session:
        session.write(json.dumps(message) + "\\n")
    if "AGENTREPORT" in question:
        with open("out/report.md", "w") as report:
            report.write("AGENT REPORT")
    if "EMPTYREPORT" in question:
        open("out/report.md", "w").close()
    if "VMLANE" in question:
        with open("out/status.json", "w") as status:
            json.dump({"status": "needs-vm-lane", "reason": "Windows execution"}, status)
    if "BIGOUT" in question:
        sys.stdout.write("A" * (3 * 1024 * 1024 // 2))
    if "KILLSELF" in question:
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    if "EXIT3" in question:
        raise SystemExit(3)
    if "EXIT1" in question:
        raise SystemExit(1)
    sys.stdout.write("FINAL ANSWER TEXT\\n")
""")


def make_profile_src(root):
    src = Path(root) / "profile-src"
    (src / "skills" / "rea").mkdir(parents=True)
    (src / "skills" / "rea" / "SKILL.md").write_text("skill", encoding="utf-8")
    (src / "mcp.json").write_text("{}", encoding="utf-8")
    (src / "config.yml").write_text("update:\n  auto: false\n", encoding="utf-8")
    return src


class FakeClock:
    """Stands in for the runner's `time` module: monotonic() replays the given readings."""

    def __init__(self, *readings):
        self._readings = list(readings)

    def monotonic(self):
        return self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]


def quiet(test):
    """Swallow the runner's one-line stdout/stderr log for the rest of `test`."""
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    test.addCleanup(stack.close)


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        quiet(self)
        self.runner = load_runner()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job = self.root / "job"
        for name in ("in/target", "out", "session", "exec/requests", "exec/results"):
            (self.job / name).mkdir(parents=True, exist_ok=True)
        (self.job / "in" / "target" / "sample.bin").write_bytes(b"sample")
        (self.job / "in" / "question.md").write_text("Analyse it.", encoding="utf-8")
        (self.job / "in" / "job.json").write_text(
            json.dumps({"job_id": JOB, "model": "openai/gpt-6.1-sol", "max_cost_usd": 20}),
            encoding="utf-8")
        self.fake = self.root / "fake_omp.py"
        self.fake.write_text(FAKE_OMP, encoding="utf-8")
        self.prompt = self.root / "re-job.md"
        self.prompt.write_text("SYSTEM PROMPT", encoding="utf-8")
        self.profile_src = make_profile_src(self.root)

    def run_job(self, **overrides):
        args = {
            "omp_cmd": (sys.executable, str(self.fake)),
            "prompt_path": self.prompt,
            "profile_src": self.profile_src,
            "home": self.root / "home",
        }
        args.update(overrides)
        return self.runner.run_job(self.job, JOB, **args)

    def status(self):
        return json.loads((self.job / "out" / "status.json").read_text(encoding="utf-8"))

    def report(self):
        return (self.job / "out" / "report.md").read_text(encoding="utf-8")

    def omp_call(self):
        return json.loads((self.root / "omp-argv.json").read_text(encoding="utf-8"))

    def launched(self):
        return (self.root / "omp-argv.json").exists()

    def set_question(self, text):
        (self.job / "in" / "question.md").write_text(text, encoding="utf-8")

    def assert_failed_without_launch(self, result, error):
        self.assertEqual((result["status"], result["error"]), ("failed", error))
        self.assertEqual(self.status(), result)
        self.assertFalse(self.launched(), "OMP must not start")


class SingleJobBoundaryTests(RunnerTestBase):
    def test_runner_has_no_host_claim_hash_or_publish_api(self):
        for name in ("claim", "pending_jobs", "verify_hashes", "recover_interrupted", "_publish"):
            self.assertFalse(hasattr(self.runner, name), name)

    def test_runner_reads_only_the_prepared_job_view(self):
        self.run_job()
        call = self.omp_call()
        self.assertEqual(Path(call["cwd"]), self.job)
        self.assertEqual(call["argv"][-1], "@" + str(self.job / "in" / "question.md"))
        self.assertEqual(call["argv"][call["argv"].index("--session-dir") + 1],
                         str(self.job / "session"))

    def test_main_requires_one_job_id_and_passes_the_profile_through(self):
        with mock.patch.object(self.runner, "run_job", return_value={"status": "done"}) as run:
            self.assertEqual(self.runner.main(["--job", JOB, "--job-root", str(self.job),
                                               "--profile-src", str(self.profile_src)]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[:2], (self.job, JOB))
        self.assertEqual(run.call_args.kwargs["profile_src"], self.profile_src)

    def test_main_fails_closed_when_the_profile_source_is_missing(self):
        with mock.patch.object(self.runner, "DEFAULT_PROMPT", self.prompt):
            code = self.runner.main(["--job", JOB, "--job-root", str(self.job),
                                     "--profile-src", str(self.root / "missing")])
        self.assertEqual(code, 1)
        self.assertEqual((self.status()["status"], self.status()["error"]),
                         ("failed", "profile-missing"))
        self.assertFalse(self.launched())

    def test_main_exit_code_follows_the_status(self):
        for status, code in (("done", 0), ("cost-cap", 0), ("needs-vm-lane", 0),
                             ("time-cap", 0), ("failed", 1)):
            with self.subTest(status=status):
                with mock.patch.object(self.runner, "run_job", return_value={"status": status}):
                    self.assertEqual(self.runner.main(
                        ["--job", JOB, "--job-root", str(self.job),
                         "--profile-src", str(self.profile_src)]), code)

    def test_job_id_must_match_the_fixed_shape(self):
        for bad in ("../escape", "short", "UPPER-CASE", "a/bbbbbbb"):
            with self.subTest(job_id=bad):
                with self.assertRaises(self.runner.RunnerError):
                    self.runner.run_job(self.job, bad, omp_cmd=(sys.executable, str(self.fake)),
                                        prompt_path=self.prompt, profile_src=self.profile_src,
                                        home=self.root / "home")


class LaunchTests(RunnerTestBase):
    def test_launch_matches_the_existing_omp_contract(self):
        self.run_job()
        argv = self.omp_call()["argv"]
        expected = ["-p", "--profile", "re-lab", "--model", "openai/gpt-6.1-sol",
                    "--thinking", "high", "--max-cost", "20", "--max-time", "2h"]
        self.assertEqual(argv[:len(expected)], expected)
        for flag in ("--no-extensions", "--no-context-files", "--no-rules"):
            self.assertIn(flag, argv)
        self.assertNotIn("--no-skills", argv)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], str(self.prompt))

    def test_max_time_flag_is_derived_from_one_constant(self):
        self.assertEqual(self.runner.MAX_TIME_S, 7200)
        self.assertEqual(self.runner.format_max_time(self.runner.MAX_TIME_S), "2h")
        for seconds, text in ((5400, "90m"), (7201, "7201s"), (60, "1m")):
            self.assertEqual(self.runner.format_max_time(seconds), text)

    def test_job_model_and_lower_cost_override_defaults(self):
        (self.job / "in" / "job.json").write_text(
            json.dumps({"job_id": JOB, "model": "anthropic/claude-sonnet-5-5",
                        "max_cost_usd": 7.5}), encoding="utf-8")
        result = self.run_job()
        argv = self.omp_call()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "anthropic/claude-sonnet-5-5")
        self.assertEqual(argv[argv.index("--max-cost") + 1], "7.5")
        self.assertEqual(result["model"], "anthropic/claude-sonnet-5-5")

    def test_cost_is_clamped_at_twenty_and_override_is_ignored(self):
        (self.job / "in" / "job.json").write_text(
            json.dumps({"job_id": JOB, "max_cost_usd": 500,
                        "cap_override": {"max_cost_usd": 500}}), encoding="utf-8")
        self.run_job()
        argv = self.omp_call()["argv"]
        self.assertEqual(argv[argv.index("--max-cost") + 1], "20")

    def test_invalid_model_or_cost_fails_without_launch(self):
        cases = (
            ({"model": "--bad"}, "invalid-model"),
            ({"model": "a b"}, "invalid-model"),
            ({"model": "safe/model\nmodelRoles:\n  default: injected"}, "invalid-model"),
            ({"max_cost_usd": -1}, "invalid-cost"),
            ({"max_cost_usd": True}, "invalid-cost"),
            ({"max_cost_usd": "5"}, "invalid-cost"),
        )
        for extra, error in cases:
            with self.subTest(extra=extra):
                (self.root / "omp-argv.json").unlink(missing_ok=True)
                body = {"job_id": JOB, **extra}
                (self.job / "in" / "job.json").write_text(json.dumps(body), encoding="utf-8")
                self.assert_failed_without_launch(self.run_job(), error)

    def test_unusable_job_metadata_fails_without_launch(self):
        for raw in ("not json", json.dumps({"job_id": "job-other-0001"}), "[" * 5000, "[]"):
            with self.subTest(raw=raw[:20]):
                (self.job / "in" / "job.json").write_text(raw, encoding="utf-8")
                self.assert_failed_without_launch(self.run_job(), "invalid-job")

    def test_omp_requires_mcp_readiness_and_keeps_the_mcp_timeout_unset(self):
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("OMP_MCP_TIMEOUT_MS", "OMP_MCP_REQUIRE_READY")}
        environ["RE_RUNNER_TEST_SENTINEL"] = "kept"
        with mock.patch.dict(os.environ, environ, clear=True):
            self.run_job()
        call = self.omp_call()
        self.assertEqual(call["require_ready"], "1")
        self.assertFalse(call["has_mcp_timeout"])
        self.assertEqual(call["sentinel"], "kept", "the rest of the environment is inherited")


class HardeningInputTests(RunnerTestBase):
    def test_missing_empty_or_irregular_prompt_fails_closed(self):
        empty = self.root / "empty-prompt.md"
        empty.write_text("", encoding="utf-8")
        for prompt in (self.root / "missing-prompt.md", empty, self.root):
            with self.subTest(prompt=prompt.name):
                self.assert_failed_without_launch(self.run_job(prompt_path=prompt), "prompt-missing")

    @unittest.skipUnless(os.name == "posix", "requires POSIX FIFOs")
    def test_fifo_prompt_fails_closed_without_blocking(self):
        fifo = self.root / "fifo-prompt.md"
        os.mkfifo(fifo)
        self.assert_failed_without_launch(self.run_job(prompt_path=fifo), "prompt-missing")

    def test_missing_or_incomplete_profile_source_fails_closed(self):
        no_config = make_profile_src(self.root / "no-config")
        (no_config / "config.yml").unlink()
        empty_mcp = make_profile_src(self.root / "empty-mcp")
        (empty_mcp / "mcp.json").write_text("", encoding="utf-8")
        mcp_dir = make_profile_src(self.root / "mcp-dir")
        (mcp_dir / "mcp.json").unlink()
        (mcp_dir / "mcp.json").mkdir()
        for source in (None, self.root / "missing", self.prompt, no_config, empty_mcp, mcp_dir):
            with self.subTest(source=str(source)):
                self.assert_failed_without_launch(
                    self.run_job(profile_src=source), "profile-missing")
        self.assertFalse((self.root / "home" / ".omp").exists(), "no profile is installed")

    def test_question_must_be_bounded_utf8(self):
        cap = self.runner.QUESTION_MAX_BYTES
        self.assertEqual(cap, 5 * 1024 * 1024)
        question = self.job / "in" / "question.md"
        for data in (b"\xff\xfe not utf-8", b"x" * (cap + 1), b"\xed\xa0\x80 surrogate"):
            with self.subTest(size=len(data)):
                question.write_bytes(data)
                self.assert_failed_without_launch(self.run_job(), "invalid-question")
        question.write_bytes(b"x" * cap)
        self.assertEqual(self.run_job()["status"], "done")

    def test_question_must_be_a_regular_file(self):
        question = self.job / "in" / "question.md"
        question.unlink()
        self.assert_failed_without_launch(self.run_job(), "invalid-question")
        question.mkdir()
        self.assert_failed_without_launch(self.run_job(), "invalid-question")

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks and FIFOs")
    def test_question_link_or_fifo_is_refused(self):
        question = self.job / "in" / "question.md"
        outside = self.root / "outside-question.md"
        outside.write_text("Analyse it.", encoding="utf-8")
        question.unlink()
        os.symlink(outside, question)
        self.assert_failed_without_launch(self.run_job(), "invalid-question")
        question.unlink()
        os.mkfifo(question)
        self.assert_failed_without_launch(self.run_job(), "invalid-question")


class OutcomeTests(RunnerTestBase):
    def test_exit_statuses_are_written_for_the_host_dispatcher(self):
        for text, expected, code in (("Analyse it.", "done", 0),
                                     ("EXIT3", "cost-cap", 3),
                                     ("EXIT1", "failed", 1)):
            with self.subTest(expected=expected):
                self.set_question(text)
                result = self.run_job()
                self.assertEqual((result["status"], result["exit_code"]), (expected, code))
                self.assertEqual(self.status(), result)

    def test_error_is_present_only_on_failed_statuses(self):
        for text, error in (("Analyse it.", None), ("EXIT3", None), ("VMLANE", None),
                            ("EXIT1", "omp-exit-1")):
            with self.subTest(question=text):
                self.set_question(text)
                result = self.run_job()
                self.assertEqual(result.get("error"), error)
                self.assertEqual(self.status().get("error"), error)

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals")
    def test_signal_exit_is_reported_as_a_shell_style_exit_code(self):
        self.set_question("KILLSELF")
        result = self.run_job()
        self.assertEqual((result["status"], result["exit_code"], result["error"]),
                         ("failed", 137, "omp-exit-137"))

    def test_error_vocabulary_is_closed(self):
        runner = self.runner
        for code in ("invalid-job", "invalid-model", "invalid-cost", "invalid-question",
                     "prompt-missing", "profile-missing", "omp-launch-failed",
                     "output-unwritable", "runner-error", "omp-exit-1", "omp-exit-255"):
            self.assertTrue(runner.is_error_code(code), code)
        for code in ("omp-exit-0", "omp-exit-256", "omp-exit-01", "boom", "", None, 3):
            self.assertFalse(runner.is_error_code(code), code)
        self.assertEqual(runner._result(JOB, "failed", None, None, "s", error="boom")["error"],
                         "runner-error")
        self.assertNotIn("error", runner._result(JOB, "done", 0, "m", "s", error="omp-exit-1"))
        self.assertEqual(runner.RunnerError("x", "made-up").code, "runner-error")

    def test_valid_vm_lane_request_survives_as_the_final_status(self):
        self.set_question("VMLANE")
        result = self.run_job()
        self.assertEqual(result["status"], "needs-vm-lane")
        self.assertEqual(result["reason"], "Windows execution")

    def test_invalid_vm_lane_request_is_ignored(self):
        (self.job / "out" / "status.json").write_text(
            '{"status":"needs-vm-lane","reason":"","extra":true}', encoding="utf-8")
        self.assertFalse(self.runner.read_vm_lane_request(self.job / "out"))

    def test_vm_lane_status_is_ignored_after_nonzero_exit(self):
        for question, expected in (("VMLANE EXIT1", "failed"), ("VMLANE EXIT3", "cost-cap")):
            with self.subTest(expected=expected):
                self.set_question(question)
                self.assertEqual(self.run_job()["status"], expected)

    def test_invalid_vm_lane_status_shapes_are_ignored(self):
        invalid = (
            '{"status":"done","reason":"x"}',
            '{"status":"needs-vm-lane","reason":"x","extra":true}',
            '{"status":"needs-vm-lane"}',
            '{"status":"needs-vm-lane","reason":5}',
            '{"status":"needs-vm-lane","reason":""}',
            '{"status":"needs-vm-lane","reason":"' + "x" * 1001 + '"}',
            '{"status":"needs-vm-lane","reason":"line1\\nline2"}',
            '{"status":"needs-vm-lane","reason":"\\u001b]52;c;Zm9yZ2Vk\\u0007"}',
            '{"status":"needs-vm-lane","reason":"\\u202eevil"}',
            '{"status":"needs-vm-lane","reason":"\\ud800"}',
            '{"status":"needs-vm-lane","reason":"' + "\U0001f600" * 800 + '"}',
            '["needs-vm-lane"]',
            "[" * 4000,
            "not json",
        )
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                (self.job / "out" / "status.json").write_text(raw, encoding="utf-8")
                self.assertIsNone(self.runner.read_vm_lane_request(self.job / "out"))

    def test_vm_lane_reason_at_the_documented_limit_is_accepted(self):
        reason = "é" * 1000
        (self.job / "out" / "status.json").write_text(
            json.dumps({"status": "needs-vm-lane", "reason": reason}, ensure_ascii=False),
            encoding="utf-8")
        self.assertEqual(self.runner.read_vm_lane_request(self.job / "out"), reason)
        status = self.runner._status_bytes(self.runner._result(
            JOB, "needs-vm-lane", 0, "openai/gpt-6.1-sol", "s", reason=reason))
        self.assertEqual(json.loads(status.decode("utf-8"))["reason"], reason)

    @unittest.skipUnless(os.name == "posix", "symlink status test requires POSIX symlinks")
    def test_symlinked_vm_lane_status_is_ignored(self):
        outside = self.root / "outside-status.json"
        outside.write_text(
            '{"status":"needs-vm-lane","reason":"outside"}', encoding="utf-8")
        status = self.job / "out" / "status.json"
        status.unlink(missing_ok=True)
        os.symlink(outside, status)
        self.assertIsNone(self.runner.read_vm_lane_request(self.job / "out"))

    def test_largest_possible_status_fits_the_host_read_cap(self):
        runner = self.runner
        for reason in ("é" * 1000, "中" * 1000, '"' * 1000, "\\" * 1000):
            with self.subTest(reason=reason[:1]):
                self.assertLessEqual(len(reason.encode("utf-8")), runner.STATUS_REASON_MAX_BYTES)
                body = runner._result("j" * 64, "needs-vm-lane", 255, "m" * 100,
                                      "2026-11-16T00:00:00Z", reason=reason)
                self.assertLessEqual(len(runner._status_bytes(body)), runner.STATUS_MAX_BYTES)

    def test_runner_writes_a_stub_report_with_final_output_when_the_agent_wrote_none(self):
        result = self.run_job()
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.report(), (
            "# Report: incomplete\nThe agent did not write out/report.md; status: done.\n"
            "\n## Final agent output (unstructured)\n\nFINAL ANSWER TEXT\n"))
        session = self.job / "session" / "s.jsonl"
        self.assertTrue(session.is_file())
        self.assertNotIn("cost_usd", result, "cost is computed by the trusted host")

    def test_stub_report_names_the_cap_status(self):
        self.set_question("EXIT3")
        self.run_job()
        self.assertTrue(self.report().startswith(
            "# Report: incomplete\nThe agent did not write out/report.md; status: cost-cap.\n"))

    def test_empty_agent_report_is_replaced_by_the_stub(self):
        self.set_question("EMPTYREPORT")
        self.run_job()
        self.assertTrue(self.report().startswith("# Report: incomplete\n"))
        self.assertIn("FINAL ANSWER TEXT", self.report())

    def test_final_output_in_the_stub_is_capped_at_one_mebibyte(self):
        self.set_question("BIGOUT")
        self.run_job()
        report = self.report()
        body = report.split("## Final agent output (unstructured)\n\n", 1)[1]
        self.assertEqual(body.count("A"), self.runner.REPORT_STDOUT_MAX_BYTES)
        self.assertIn("[output truncated at 1 MiB]", body)

    def test_agent_report_is_preferred_to_stdout(self):
        self.set_question("AGENTREPORT")
        self.run_job()
        self.assertEqual(self.report(), "AGENT REPORT")

    def test_failed_message_points_at_the_published_error_field(self):
        message = self.runner.FAILED_MESSAGE
        self.assertIn('"error"', message)
        self.assertIn("result.json", message)
        self.assertNotIn("log", message)

    def test_launch_failure_writes_fixed_failure_without_exception_path(self):
        missing = self.root / "SECRET-MISSING-OMP"
        result = self.run_job(omp_cmd=(str(missing),))
        self.assertEqual((result["status"], result["error"]), ("failed", "omp-launch-failed"))
        self.assertNotIn("SECRET-MISSING-OMP", json.dumps(self.status()))
        report = self.report()
        self.assertEqual(report, (
            "# Report: incomplete\nThe agent did not write out/report.md; status: failed.\n"
            f"\n{self.runner.FAILED_MESSAGE}.\n"))
        self.assertNotIn("SECRET-MISSING-OMP", report)

    def test_unexpected_exception_is_a_runner_error_without_its_text(self):
        with mock.patch.object(self.runner, "sync_profile",
                               side_effect=RuntimeError("SECRET-EXCEPTION-TEXT")):
            result = self.run_job()
        self.assertEqual((result["status"], result["error"]), ("failed", "runner-error"))
        for text in (json.dumps(self.status()), self.report()):
            self.assertNotIn("SECRET-EXCEPTION-TEXT", text)


class TimeCapTests(RunnerTestBase):
    def run_timed(self, question, elapsed):
        self.set_question(question)
        with mock.patch.object(self.runner, "time", FakeClock(100.0, 100.0 + elapsed)):
            return self.run_job()

    def test_a_run_that_used_the_whole_budget_is_a_time_cap(self):
        cap = self.runner.MAX_TIME_S - self.runner.TIME_CAP_MARGIN_S
        for question, elapsed, expected in (("Analyse it.", cap, "time-cap"),
                                            ("Analyse it.", cap + 60, "time-cap"),
                                            ("EXIT1", cap, "time-cap"),
                                            ("VMLANE", cap, "time-cap"),
                                            ("EXIT3", cap, "cost-cap"),
                                            ("Analyse it.", cap - 0.5, "done"),
                                            ("EXIT1", cap - 0.5, "failed")):
            with self.subTest(question=question, elapsed=elapsed):
                result = self.run_timed(question, elapsed)
                self.assertEqual(result["status"], expected)
                self.assertEqual(self.status(), result)
                if expected == "time-cap":
                    self.assertNotIn("error", result)
                    self.assertNotIn("reason", result)
                    self.assertTrue(self.report().startswith(
                        "# Report: incomplete\nThe agent did not write out/report.md; "
                        "status: time-cap.\n"))

    def test_time_cap_keeps_the_agent_report(self):
        result = self.run_timed("AGENTREPORT", self.runner.MAX_TIME_S)
        self.assertEqual(result["status"], "time-cap")
        self.assertEqual(self.report(), "AGENT REPORT")


class StatusWriteTests(RunnerTestBase):
    def test_status_is_written_even_when_the_report_step_fails(self):
        with mock.patch.object(self.runner, "_ensure_report", side_effect=OSError("disk")):
            result = self.run_job()
        self.assertEqual((result["status"], result["error"]), ("failed", "output-unwritable"))
        self.assertEqual(self.status(), result)

    def test_report_failure_does_not_mask_the_first_failure(self):
        self.set_question("EXIT1")
        with mock.patch.object(self.runner, "_ensure_report", side_effect=OSError("disk")):
            result = self.run_job()
        self.assertEqual(result["error"], "omp-exit-1")

    def test_status_write_moves_aside_a_directory_planted_after_the_check(self):
        (self.job / "out" / "status.json").mkdir()
        (self.job / "out" / "status.json" / "inner").write_text("planted", encoding="utf-8")
        self.runner._write_atomic(self.job / "out", "status.json", b'{"x": 1}\n')
        self.assertEqual(self.status(), {"x": 1})
        aside = [p for p in (self.job / "out").iterdir() if p.name.startswith(".quarantine-")]
        self.assertEqual(len(aside), 1)
        self.assertTrue(aside[0].is_dir())

    def test_writes_leave_no_temporary_files(self):
        self.run_job()
        leftovers = [p.name for p in (self.job / "out").iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    @unittest.skipUnless(os.name == "posix", "requires POSIX permissions")
    def test_status_and_stub_report_are_world_readable_like_before(self):
        self.run_job()
        for name in ("status.json", "report.md"):
            self.assertEqual((self.job / "out" / name).stat().st_mode & 0o777, 0o644, name)

    def test_unwritable_output_is_reported(self):
        with mock.patch.object(self.runner, "_create_output",
                               side_effect=self.runner.RunnerError("x", "output-unwritable")):
            result = self.run_job()
        self.assertEqual((result["status"], result["error"]), ("failed", "output-unwritable"))
        self.assertFalse(self.launched())


class ProfileTests(RunnerTestBase):
    CHAT_ROLES = ("default", "smol", "slow", "vision", "plan", "commit", "task", "advisor",
                  "tiny", "memory", "judge", "widget")

    def roles(self, config_text):
        block = re.search(r"^modelRoles:\n((?:[ \t]+.*\n?)+)", config_text, re.M)
        self.assertIsNotNone(block)
        roles = {}
        for line in block.group(1).splitlines():
            key, _, value = line.strip().partition(":")
            roles[key] = json.loads(value.strip())
        return roles

    def installed_config(self, home):
        return (home / ".omp" / "profiles" / "re-lab" / "agent" / "config.yml").read_text(
            encoding="utf-8")

    def test_every_model_role_is_pinned_to_the_job_model(self):
        home = self.root / "home"
        self.runner.sync_profile(self.profile_src, home, "anthropic/claude-sonnet-5-5")
        roles = self.roles(self.installed_config(home))
        self.assertEqual(set(roles), set(self.CHAT_ROLES))
        self.assertEqual(roles["default"], "anthropic/claude-sonnet-5-5:high")
        self.assertEqual(roles["tiny"], "anthropic/claude-sonnet-5-5")
        self.assertEqual(roles["widget"], "anthropic/claude-sonnet-5-5")

    def test_yaml_significant_model_is_quoted_without_injecting_settings(self):
        home = self.root / "home"
        self.runner.sync_profile(self.profile_src, home, "a:b/c.d-e_f")
        config = self.installed_config(home)
        self.assertEqual(set(self.roles(config).values()),
                         {"a:b/c.d-e_f:high", "a:b/c.d-e_f"})
        self.assertEqual(config.count("modelRoles:"), 1)

    def test_profile_is_rebuilt_from_scratch(self):
        home = self.root / "home"
        self.runner.sync_profile(self.profile_src, home, "openai/gpt-6.1-sol")
        agent = home / ".omp" / "profiles" / "re-lab" / "agent"
        (agent / "stale").write_text("old", encoding="utf-8")
        self.runner.sync_profile(self.profile_src, home, "openai/gpt-6.1-sol")
        self.assertFalse((agent / "stale").exists())
        self.assertEqual((agent / "skills" / "rea" / "SKILL.md").read_text(encoding="utf-8"),
                         "skill")

    def test_run_job_rebuilds_profile_for_selected_model(self):
        home = self.root / "home"
        self.run_job(home=home)
        self.assertEqual(self.roles(self.installed_config(home))["default"],
                         "openai/gpt-6.1-sol:high")

    @needs_yaml
    def test_shipped_profile_installs_as_valid_yaml_with_hardening_and_pins(self):
        home = self.root / "home"
        self.runner.sync_profile(CONTAINER / "omp-profile", home, "openai/gpt-6.1-sol")
        config = yaml.safe_load(self.installed_config(home))
        self.assertEqual(set(config["modelRoles"]), set(self.CHAT_ROLES))
        for tool in ("fetch", "web_search", "browser"):
            self.assertIs(config[tool]["enabled"], False, tool)
        self.assertNotIn("native", config["disabledProviders"])


# OMP 18.22.0 setting ids (packages/coding-agent/src register({ id })), verified at the pinned tag:
# session/settings.ts retry.*, modes/settings.ts update.auto, mcp/settings.ts mcp.*,
# config/model-settings.ts *Providers / modelRole*, tools/settings.ts fetch/web_search/vault/
# security/tools.maxTimeout, tools/browser/settings.ts browser.enabled, autolearn/settings.ts.
OMP_18_22_SETTING_IDS = frozenset({
    "retry.enabled", "retry.maxRetries", "retry.modelFallback", "retry.usageAwareFallback",
    "retry.fallbackChains", "update.auto", "mcp.enableProjectConfig", "mcp.startupTimeoutMs",
    "enabledProviders", "disabledProviders", "modelRoles", "modelRoleStorage",
    "fetch.enabled", "web_search.enabled", "browser.enabled", "vault.enabled",
    "security.enabled", "tools.maxTimeout", "autolearn.enabled",
})
# Discovery provider ids in packages/coding-agent/src/discovery/*.ts that read project or user
# configuration (PROVIDER_ID constants plus skillshare's and the managed-skills registration).
# `native` (builtin.ts) loads the profile's own mcp.json and skills/ and must stay enabled;
# `builtin-defaults` serves rules embedded in the binary.
OMP_18_22_CONFIG_PROVIDERS = frozenset({
    "agent-plugins", "agents", "agents-md", "claude", "claude-md", "claude-plugins", "cline",
    "codex", "cursor", "custom-context", "gemini", "github", "mcp-json", "omp-managed",
    "omp-plugins", "opencode", "skillshare", "ssh-config", "ssh-json", "vscode", "windsurf",
})


def _setting_paths(mapping, prefix=""):
    """Yield the dotted setting id of every configured value, stopping at known setting ids."""
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if path in OMP_18_22_SETTING_IDS or not isinstance(value, dict):
            yield path
        else:
            yield from _setting_paths(value, path + ".")


def _load_unique_yaml(text):
    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _value in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(f"duplicate key {key!r} in config.yml")
            seen.add(key)
        return loader.construct_mapping(node, deep)

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return yaml.load(text, Loader=UniqueKeyLoader)


class StaticProfileTests(unittest.TestCase):
    def config_text(self):
        return (CONTAINER / "omp-profile" / "config.yml").read_text(encoding="utf-8")

    def test_shipped_profile_blocks_foreign_discovery_and_has_no_model_roles(self):
        config = self.config_text()
        self.assertRegex(config, r"(?m)^mcp:\n  enableProjectConfig: false\n")
        self.assertNotIn("modelRoles", config)

    @needs_yaml
    def test_every_configured_key_is_a_pinned_omp_setting_id(self):
        config = _load_unique_yaml(self.config_text())
        paths = list(_setting_paths(config))
        self.assertEqual(sorted(set(paths) - OMP_18_22_SETTING_IDS), [])
        self.assertNotIn("modelRoles", config)

    def test_the_setting_walk_catches_typos(self):
        typo = {"fetch": {"enable": False}, "web_serach": {"enabled": False},
                "retry": {"fallbackChains": {"openai/*": []}}}
        self.assertEqual(set(_setting_paths(typo)) - OMP_18_22_SETTING_IDS,
                         {"fetch.enable", "web_serach.enabled"})

    @needs_yaml
    def test_internet_tools_are_disabled(self):
        config = _load_unique_yaml(self.config_text())
        for tool in ("fetch", "web_search", "browser"):
            self.assertEqual(config.get(tool), {"enabled": False}, tool)

    @needs_yaml
    def test_every_configuration_discovery_provider_is_disabled(self):
        config = _load_unique_yaml(self.config_text())
        providers = config["disabledProviders"]
        self.assertEqual(len(providers), len(set(providers)))
        self.assertEqual(set(providers), OMP_18_22_CONFIG_PROVIDERS)
        self.assertNotIn("native", providers)
        self.assertIs(config["mcp"]["enableProjectConfig"], False)
        self.assertIs(config["retry"]["modelFallback"], False)
        self.assertIs(config["update"]["auto"], False)

    def test_rea_mcp_timeout_covers_the_first_ghidra_query(self):
        servers = json.loads((CONTAINER / "omp-profile" / "mcp.json").read_text(
            encoding="utf-8"))["mcpServers"]
        self.assertEqual(set(servers), {"rea"})
        timeout = servers["rea"]["timeout"]
        self.assertIsInstance(timeout, int)
        self.assertGreaterEqual(timeout, 330000, "REA's first Ghidra query budget is 330 s")


if __name__ == "__main__":
    unittest.main()
