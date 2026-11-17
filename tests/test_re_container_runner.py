"""In-container runner contract: one prepared job, fresh profile, bounded OMP invocation."""
import importlib.util
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

CONTAINER = Path(__file__).resolve().parents[1] / "container"
JOB = "job-20261116-0002"


def load_runner():
    spec = importlib.util.spec_from_file_location("re_container_runner", CONTAINER / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE_OMP = textwrap.dedent("""
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    with open(os.path.join(here, "omp-argv.json"), "w") as record:
        json.dump({"argv": args, "cwd": os.getcwd()}, record)
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
    if "VMLANE" in question:
        with open("out/status.json", "w") as status:
            json.dump({"status": "needs-vm-lane", "reason": "Windows execution"}, status)
    if "EXIT3" in question:
        raise SystemExit(3)
    if "EXIT1" in question:
        raise SystemExit(1)
    sys.stdout.write("FINAL ANSWER TEXT\\n")
""")


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
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

    def run_job(self, **overrides):
        args = {
            "omp_cmd": (sys.executable, str(self.fake)),
            "prompt_path": self.prompt,
            "profile_src": None,
            "home": self.root / "home",
        }
        args.update(overrides)
        return self.runner.run_job(self.job, JOB, **args)

    def status(self):
        return json.loads((self.job / "out" / "status.json").read_text(encoding="utf-8"))

    def omp_call(self):
        return json.loads((self.root / "omp-argv.json").read_text(encoding="utf-8"))


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

    def test_main_requires_one_job_id_and_runs_once(self):
        with mock.patch.object(self.runner, "run_job", return_value={"status": "done"}) as run:
            self.assertEqual(self.runner.main(["--job", JOB, "--job-root", str(self.job),
                                               "--profile-src", str(self.root / "missing")]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[:2], (self.job, JOB))

    def test_job_id_must_match_the_fixed_shape(self):
        for bad in ("../escape", "short", "UPPER-CASE", "a/bbbbbbb"):
            with self.subTest(job_id=bad):
                with self.assertRaises(self.runner.RunnerError):
                    self.runner.run_job(self.job, bad, omp_cmd=(sys.executable, str(self.fake)),
                                        prompt_path=self.prompt, profile_src=None, home=self.root / "home")


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
            {"model": "--bad"},
            {"model": "a b"},
            {"model": "safe/model\nmodelRoles:\n  default: injected"},
            {"max_cost_usd": -1},
            {"max_cost_usd": True},
        )
        for index, extra in enumerate(cases):
            with self.subTest(extra=extra):
                (self.root / "omp-argv.json").unlink(missing_ok=True)
                body = {"job_id": JOB, **extra}
                (self.job / "in" / "job.json").write_text(json.dumps(body), encoding="utf-8")
                result = self.run_job()
                self.assertEqual(result["status"], "failed")
                self.assertFalse((self.root / "omp-argv.json").exists())


class OutcomeTests(RunnerTestBase):
    def set_question(self, text):
        (self.job / "in" / "question.md").write_text(text, encoding="utf-8")

    def test_exit_statuses_are_written_for_the_host_dispatcher(self):
        for text, expected, code in (("Analyse it.", "done", 0),
                                     ("EXIT3", "cost-cap", 3),
                                     ("EXIT1", "failed", 1)):
            with self.subTest(expected=expected):
                self.set_question(text)
                result = self.run_job()
                self.assertEqual((result["status"], result["exit_code"]), (expected, code))
                self.assertEqual(self.status(), result)

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
            '["needs-vm-lane"]',
            "not json",
        )
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                (self.job / "out" / "status.json").write_text(raw, encoding="utf-8")
                self.assertIsNone(self.runner.read_vm_lane_request(self.job / "out"))

    @unittest.skipUnless(os.name == "posix", "symlink status test requires POSIX symlinks")
    def test_symlinked_vm_lane_status_is_ignored(self):
        outside = self.root / "outside-status.json"
        outside.write_text(
            '{"status":"needs-vm-lane","reason":"outside"}', encoding="utf-8")
        status = self.job / "out" / "status.json"
        status.unlink(missing_ok=True)
        os.symlink(outside, status)
        self.assertIsNone(self.runner.read_vm_lane_request(self.job / "out"))

    def test_runner_writes_report_and_leaves_session_for_host_publication(self):
        result = self.run_job()
        self.assertEqual(result["status"], "done")
        self.assertEqual((self.job / "out" / "report.md").read_text(encoding="utf-8"),
                         "FINAL ANSWER TEXT\n")
        session = self.job / "session" / "s.jsonl"
        self.assertTrue(session.is_file())
        self.assertNotIn("cost_usd", result, "cost is computed by the trusted host")

    def test_agent_report_is_preferred_to_stdout(self):
        self.set_question("AGENTREPORT")
        self.run_job()
        self.assertEqual((self.job / "out" / "report.md").read_text(encoding="utf-8"),
                         "AGENT REPORT")

    def test_launch_failure_writes_fixed_failure_without_exception_path(self):
        missing = self.root / "SECRET-MISSING-OMP"
        result = self.run_job(omp_cmd=(str(missing),))
        self.assertEqual(result["status"], "failed")
        published = json.dumps(self.status())
        self.assertNotIn("SECRET-MISSING-OMP", published)
        self.assertEqual((self.job / "out" / "report.md").read_text(encoding="utf-8"),
                         self.runner.FAILED_MESSAGE + "\n")


class ProfileTests(RunnerTestBase):
    CHAT_ROLES = ("default", "smol", "slow", "vision", "plan", "commit", "task", "advisor",
                  "tiny", "memory", "judge")

    def make_profile_src(self):
        src = self.root / "profile-src"
        (src / "skills" / "rea").mkdir(parents=True)
        (src / "skills" / "rea" / "SKILL.md").write_text("skill", encoding="utf-8")
        (src / "mcp.json").write_text("{}", encoding="utf-8")
        (src / "config.yml").write_text("update:\n  auto: false\n", encoding="utf-8")
        return src

    def roles(self, config_text):
        block = re.search(r"^modelRoles:\n((?:[ \t]+.*\n?)+)", config_text, re.M)
        self.assertIsNotNone(block)
        roles = {}
        for line in block.group(1).splitlines():
            key, _, value = line.strip().partition(":")
            roles[key] = json.loads(value.strip())
        return roles

    def test_every_model_role_is_pinned_to_the_job_model(self):
        home = self.root / "home"
        self.runner.sync_profile(self.make_profile_src(), home, "anthropic/claude-sonnet-5-5")
        config = (home / ".omp" / "profiles" / "re-lab" / "agent" / "config.yml").read_text(
            encoding="utf-8")
        roles = self.roles(config)
        self.assertEqual(set(roles), set(self.CHAT_ROLES))
        self.assertEqual(roles["default"], "anthropic/claude-sonnet-5-5:high")
        self.assertEqual(roles["tiny"], "anthropic/claude-sonnet-5-5")

    def test_yaml_significant_model_is_quoted_without_injecting_settings(self):
        home = self.root / "home"
        self.runner.sync_profile(self.make_profile_src(), home, "a:b/c.d-e_f")
        config = (home / ".omp" / "profiles" / "re-lab" / "agent" / "config.yml").read_text(
            encoding="utf-8")
        self.assertEqual(set(self.roles(config).values()),
                         {"a:b/c.d-e_f:high", "a:b/c.d-e_f"})
        self.assertEqual(config.count("modelRoles:"), 1)

    def test_profile_is_rebuilt_from_scratch(self):
        home = self.root / "home"
        src = self.make_profile_src()
        self.runner.sync_profile(src, home, "openai/gpt-6.1-sol")
        agent = home / ".omp" / "profiles" / "re-lab" / "agent"
        (agent / "stale").write_text("old", encoding="utf-8")
        self.runner.sync_profile(src, home, "openai/gpt-6.1-sol")
        self.assertFalse((agent / "stale").exists())
        self.assertEqual((agent / "skills" / "rea" / "SKILL.md").read_text(encoding="utf-8"),
                         "skill")

    def test_run_job_rebuilds_profile_for_selected_model(self):
        home = self.root / "home"
        src = self.make_profile_src()
        self.run_job(profile_src=src, home=home)
        config = (home / ".omp" / "profiles" / "re-lab" / "agent" / "config.yml").read_text(
            encoding="utf-8")
        self.assertEqual(self.roles(config)["default"], "openai/gpt-6.1-sol:high")


class StaticProfileTests(unittest.TestCase):
    def test_shipped_profile_blocks_foreign_discovery_and_has_no_model_roles(self):
        config = (CONTAINER / "omp-profile" / "config.yml").read_text(encoding="utf-8")
        self.assertRegex(config, r"(?m)^mcp:\n  enableProjectConfig: false\n")
        providers = set(re.findall(
            r"(?m)^  - (\S+)$", config.split("disabledProviders:", 1)[1].split("\n\n")[0]))
        self.assertGreaterEqual(
            providers, {"claude", "claude-plugins", "codex", "gemini", "opencode",
                        "github", "cursor", "agents", "agents-md", "claude-md",
                        "custom-context"})
        self.assertNotIn("modelRoles", config)


if __name__ == "__main__":
    unittest.main()
