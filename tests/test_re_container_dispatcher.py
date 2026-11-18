"""Trusted host dispatcher, egress, publication, and per-job mount contracts."""
import hashlib
import importlib.util
import ipaddress
import json
import os
import stat
import sys
import tempfile
import unittest
import threading
import textwrap
from pathlib import Path
from unittest import mock
import yaml

REPO = Path(__file__).resolve().parents[1]
CONTAINER = REPO / "container"
IMAGE = "sha256:" + "a" * 64
EXEC_IMAGE = "sha256:" + "b" * 64
JOB = "job-20261116-0042"

HAS_FD_OPS = (os.name == "posix" and hasattr(os, "O_NOFOLLOW") and
              os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and
              os.stat in os.supports_dir_fd and os.scandir in os.supports_fd)
needs_fd_ops = unittest.skipUnless(HAS_FD_OPS, "requires POSIX openat and O_NOFOLLOW")
needs_symlink = unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")


def load(name):
    path = CONTAINER / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"re_container_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CONTAINER))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def digest(data):
    return hashlib.sha256(data).hexdigest()


class DispatcherTestBase(unittest.TestCase):
    def setUp(self):
        self.dispatcher = load("re-dispatcher")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "re-lab"
        for name in ("jobs/inbox", "jobs/work", "reports", "exec/runs", "secrets"):
            (self.root / name).mkdir(parents=True)
        os.chmod(self.root / "exec" / "runs", 0o700)
        self.models = self.root / "secrets" / "models.env"
        self.models.write_text("TEST_SECRET=PLANTED-SENTINEL\nEMPTY=\n", encoding="utf-8")
        self.cfg = self.dispatcher.Config(
            root=self.root, agent_image=IMAGE, exec_image=EXEC_IMAGE,
            models_env=self.models, docker_prefix=("docker",))

    def make_job(self, job_id=JOB, target=b"submitted", question=b"Analyse it."):
        job = self.root / "jobs" / "inbox" / job_id
        (job / "target").mkdir(parents=True)
        (job / "target" / "tool").write_bytes(target)
        os.chmod(job / "target" / "tool", 0o755)
        (job / "question.md").write_bytes(question)
        files = [
            {"path": "target/tool", "sha256": digest(target), "size": len(target)},
            {"path": "question.md", "sha256": digest(question), "size": len(question)},
        ]
        (job / "job.json").write_text(json.dumps({
            "schema": 1, "job_id": job_id, "files": files,
            "model": "openai/gpt-6.1-sol", "max_cost_usd": 20,
        }), encoding="utf-8")
        return job

    def prepared(self, job_id=JOB):
        self.make_job(job_id)
        work = self.dispatcher.claim_job(self.cfg, job_id)
        self.dispatcher.prepare_job(self.cfg, job_id)
        return work


class EgressPolicyTests(unittest.TestCase):
    def test_squid_rules_block_literal_ips_before_the_domain_allow(self):
        lines = []
        for raw in (CONTAINER / "egress-allowlist").read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                lines.append(line)
        domain = next(line for line in lines if line.startswith("acl model_endpoints "))
        self.assertIn("dstdomain -n ", domain)
        # Model APIs plus the OpenAI subscription sign-in hosts; any addition widens egress.
        self.assertEqual(set(domain.split("dstdomain -n ", 1)[1].split()), {
            "api.openai.com", "api.anthropic.com", "openrouter.ai", "chatgpt.com", "auth.openai.com"})
        self.assertTrue(any("dstdom_regex" in line and "literal" in line for line in lines))
        access = [line for line in lines if line.startswith("http_access ")]
        literal_deny = next(i for i, line in enumerate(access)
                            if line.startswith("http_access deny") and "literal" in line)
        allow = next(i for i, line in enumerate(access) if "allow" in line and "model_endpoints" in line)
        port_deny = next(i for i, line in enumerate(access) if "deny" in line and "!tls_port" in line)
        private_deny = next(i for i, line in enumerate(access) if "deny private_dst" in line)
        self.assertLess(port_deny, allow)
        self.assertLess(private_deny, allow)
        self.assertLess(literal_deny, allow)
        self.assertNotIn("http_access allow all", access)


class DeploymentDocumentationTests(unittest.TestCase):
    def test_runbook_provisions_a_fixed_ext4_image_with_an_explicit_inode_count(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("/var/lib/re-lab.img", text)
        self.assertRegex(text, r"truncate -s 40G /var/lib/re-lab\.img")
        self.assertRegex(text, r"mkfs\.ext4[^\n]*-N [0-9]+[^\n]*/var/lib/re-lab\.img")
        self.assertRegex(text, r"/var/lib/re-lab\.img\s+/srv/re-lab\s+ext4\s+loop")

    def test_docs_record_the_accepted_key_risks_and_limited_redaction(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        design = (REPO / "docs" / "design.md").read_text(encoding="utf-8")
        for text in (readme, design):
            self.assertRegex(text, r"(?is)accepted risk.*\$20")
            self.assertRegex(text, r"(?is)accepted risk.*report")
        self.assertRegex(readme, r"(?is)exact-value redaction.*not robust")


class ClaimAndLayoutTests(DispatcherTestBase):
    def test_claim_is_a_same_filesystem_rename_from_inbox_only(self):
        source = self.make_job()
        work = self.dispatcher.claim_job(self.cfg, JOB)
        self.assertFalse(source.exists())
        self.assertEqual(work, self.root / "jobs" / "work" / JOB)
        self.assertEqual(os.stat(work.parent).st_dev,
                         os.stat(self.root / "jobs" / "inbox").st_dev)

    def test_forged_work_directory_is_recovered_and_never_run(self):
        forged = self.root / "jobs" / "work" / "job-20261116-forged"
        forged.mkdir()
        run = mock.Mock()
        recovered = self.dispatcher.recover_interrupted(self.cfg)
        self.dispatcher.process_once(self.cfg, run_job=run)
        self.assertEqual(recovered, ["job-20261116-forged"])
        run.assert_not_called()
        result = json.loads((self.root / "reports" / "job-20261116-forged" /
                             "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "recovered-after-restart")

    def test_prepared_layout_separates_root_and_agent_owned_paths(self):
        work = self.prepared()
        self.assertEqual({p.name for p in work.iterdir()},
                         {"target", "question.md", "job.json", "in", "out", "session", "exec"})
        self.assertEqual((work / "in" / "target" / "tool").read_bytes(), b"submitted")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((work / "target").stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE((work / "target" / "tool").stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE((work / "in").stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE((work / "out").stat().st_mode), 0o700)

    def test_hash_change_is_rejected_before_layout_or_agent_launch(self):
        job = self.make_job()
        (job / "target" / "tool").write_bytes(b"changed")
        self.dispatcher.claim_job(self.cfg, JOB)
        with self.assertRaises(self.dispatcher.HashMismatch):
            self.dispatcher.prepare_job(self.cfg, JOB)
        self.assertFalse((self.root / "jobs" / "work" / JOB / "in").exists())

    @needs_fd_ops
    def test_executor_source_is_target_not_the_agent_visible_in_copy(self):
        work = self.prepared()
        os.chmod(work / "in", 0o755)
        os.chmod(work / "in" / "target", 0o755)
        os.chmod(work / "in" / "target" / "tool", 0o644)
        (work / "in" / "target" / "tool").write_bytes(b"agent-forgery")
        os.chmod(work / "in" / "target" / "tool", 0o444)
        os.chmod(work / "in" / "target", 0o555)
        os.chmod(work / "in", 0o555)
        executor = load("executor")
        exec_cfg = self.dispatcher.executor_config(self.cfg, JOB)
        request = executor.validate_request({"job_id": JOB, "argv": ["true"]})
        run = executor.prepare_run(exec_cfg, request)
        self.addCleanup(run.discard)
        self.assertEqual((run.path / "in" / "tool").read_bytes(), b"submitted")


class AgentArgvTests(DispatcherTestBase):
    def test_agent_receives_only_active_job_mounts_and_fixed_flags(self):
        work = self.prepared()
        argv = self.dispatcher.build_agent_argv(self.cfg, JOB)
        expected_mounts = {
            f"{work / 'in'}:/job/in:ro",
            f"{work / 'out'}:/job/out:rw",
            f"{work / 'session'}:/job/session:rw",
            f"{work / 'exec' / 'requests'}:/job/exec/requests:rw",
            f"{work / 'exec' / 'results'}:/job/exec/results:ro",
        }
        mounts = {argv[n + 1] for n, arg in enumerate(argv) if arg == "-v"}
        self.assertEqual(mounts, expected_mounts)
        joined = "\n".join(argv)
        for forbidden in ("jobs/inbox", "/reports", f"{work / 'target'}:/", "job-other"):
            self.assertNotIn(forbidden, joined)
        image = argv.index(IMAGE)
        self.assertEqual(argv[image + 1:], ["python3", "/opt/re-lab/runner.py", "--job", JOB])
        for flag in ("--rm", "--runtime=runsc", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--user=10001:10001",
                     "--memory=3g", "--memory-swap=3g", "--pids-limit=512", "--pull=never"):
            self.assertIn(flag, argv[:image])
        self.assertEqual(argv[argv.index("--name") + 1], f"re-lab-agent-{JOB}")
        self.assertTrue(any(flag.startswith("--cpus=") for flag in argv[:image]))
        self.assertEqual(argv[argv.index("--label") + 1], "re-lab.role=agent")
        labels = [argv[index + 1] for index, value in enumerate(argv) if value == "--label"]
        self.assertEqual(labels, ["re-lab.role=agent", f"re-lab.job={JOB}"])
        self.assertIn("--log-driver=none", argv[:image])
        self.assertEqual(argv[argv.index("--network") + 1], "re-lab_re-internal")
        # The agent gets a per-job, root-only env file built from validated models.env lines,
        # never the operator's file itself.
        self.assertEqual(argv[argv.index("--env-file") + 1], str(work / "agent.env"))

    def test_compose_keeps_only_proxy_running_and_images_build_only(self):
        text = (CONTAINER / "compose.yaml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^  re-agent:\n(?:.*\n)*?    profiles: \[build-only\]")
        self.assertRegex(text, r"(?m)^  re-exec:\n(?:.*\n)*?    profiles: \[build-only\]")
        egress = text.split("  re-egress:", 1)[1].split("\n  re-agent:", 1)[0]
        self.assertNotIn("profiles:", egress)

    def test_dispatcher_systemd_unit_replaces_executor_unit(self):
        self.assertTrue((CONTAINER / "re-dispatcher.service").is_file())
        self.assertFalse((CONTAINER / "re-executor.service").exists())
        unit = (CONTAINER / "re-dispatcher.service").read_text(encoding="utf-8")
        self.assertIn("User=root", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("/opt/re-lab/re-dispatcher.py", unit)


class ActiveExecutionTests(DispatcherTestBase):
    def test_exec_requests_are_served_only_from_the_active_job(self):
        self.prepared()
        other = self.root / "jobs" / "work" / "job-20261116-other" / "exec" / "requests"
        other.mkdir(parents=True)
        process = mock.Mock()
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        with mock.patch.object(self.dispatcher.subprocess, "Popen", return_value=process), \
             mock.patch.object(self.dispatcher.executor, "process_pending", return_value=0) as pending, \
             mock.patch.object(self.dispatcher.executor, "contain_container") as contain, \
             mock.patch.object(self.dispatcher.time, "sleep"):
            self.dispatcher.run_agent(self.cfg, JOB, wall_timeout_s=30)
        # A clean client exit is still followed by proof that the exact container is gone.
        contain.assert_called_once_with(("docker",), self.root / "jobs" / "work" / JOB / "agent.cid")
        self.assertFalse((self.root / "jobs" / "work" / JOB / "agent.env").exists())
        self.assertGreaterEqual(pending.call_count, 1)
        for call in pending.call_args_list:
            self.assertEqual(call.args[0].requests_dir,
                             self.root / "jobs" / "work" / JOB / "exec" / "requests")
            self.assertNotEqual(call.args[0].requests_dir, other)

    def test_wall_timeout_kills_only_the_cidfile_container(self):
        self.prepared()
        cidfile = self.root / "jobs" / "work" / JOB / "agent.cid"
        cidfile.write_text("ab" * 32, encoding="ascii")
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 137
        clocks = iter((0.0, 31.0, 31.0))
        with mock.patch.object(self.dispatcher.subprocess, "Popen", return_value=process), \
             mock.patch.object(self.dispatcher.executor, "process_pending", return_value=0), \
             mock.patch.object(self.dispatcher.time, "monotonic", side_effect=lambda: next(clocks)), \
             mock.patch.object(self.dispatcher.time, "sleep"), \
             mock.patch.object(self.dispatcher.executor, "contain_container") as contain:
            result = self.dispatcher.run_agent(self.cfg, JOB, wall_timeout_s=30)
        self.assertTrue(result.timed_out)
        contain.assert_called_once_with(("docker",), cidfile)


    def test_wall_timer_is_not_blocked_by_a_running_execution_request(self):
        self.prepared()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 137
        request_started = threading.Event()
        release_request = threading.Event()
        order = []

        def blocked_request(_cfg, _stop):
            order.append("request-start")
            request_started.set()
            release_request.wait(2)
            order.append("request-end")
            return 1

        def kill_agent(*_args, **_kwargs):
            order.append("agent-kill")
            release_request.set()

        clocks = iter((0.0, 31.0))
        with mock.patch.object(self.dispatcher.subprocess, "Popen", return_value=process), \
             mock.patch.object(self.dispatcher.executor, "process_pending",
                               side_effect=blocked_request), \
             mock.patch.object(self.dispatcher.time, "monotonic",
                               side_effect=lambda: next(clocks)), \
             mock.patch.object(self.dispatcher.time, "sleep"), \
             mock.patch.object(self.dispatcher.executor, "contain_container",
                               side_effect=kill_agent):
            result = self.dispatcher.run_agent(self.cfg, JOB, wall_timeout_s=30)
        self.assertTrue(request_started.is_set())
        self.assertTrue(result.timed_out)
        self.assertLess(order.index("agent-kill"), order.index("request-end"))

    def test_agent_containment_failure_reaps_attached_client(self):
        self.prepared()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 137
        clocks = iter((0.0, 31.0))
        with mock.patch.object(self.dispatcher.subprocess, "Popen", return_value=process), \
             mock.patch.object(self.dispatcher.executor, "process_pending", return_value=0), \
             mock.patch.object(
                 self.dispatcher.executor, "contain_container",
                 side_effect=self.dispatcher.executor.ContainmentError("uncertain")), \
             mock.patch.object(
                 self.dispatcher.time, "monotonic", side_effect=lambda: next(clocks)), \
             mock.patch.object(self.dispatcher.time, "sleep"):
            with self.assertRaises(self.dispatcher.executor.ContainmentError):
                self.dispatcher.run_agent(self.cfg, JOB, wall_timeout_s=30)
        process.kill.assert_called()
        process.wait.assert_called()

    def test_agent_thread_start_failure_contains_and_reaps(self):
        self.prepared()
        process = mock.Mock()
        process.wait.return_value = 137
        with mock.patch.object(self.dispatcher.subprocess, "Popen", return_value=process), \
             mock.patch.object(
                 self.dispatcher.threading.Thread, "start",
                 side_effect=RuntimeError("thread unavailable")), \
             mock.patch.object(self.dispatcher.executor, "contain_container") as contain:
            with self.assertRaises(self.dispatcher.executor.ContainmentError):
                self.dispatcher.run_agent(self.cfg, JOB)
        contain.assert_called_once_with(
            self.cfg.docker_prefix, self.cfg.work / JOB / "agent.cid")
        process.kill.assert_called()
        process.wait.assert_called()

    def test_agent_state_construction_precedes_docker_launch(self):
        with mock.patch.object(
                self.dispatcher.threading, "Event", side_effect=MemoryError("exhausted")), \
             mock.patch.object(self.dispatcher.subprocess, "Popen") as launch:
            with self.assertRaises(MemoryError):
                self.dispatcher.run_agent(self.cfg, JOB)
        launch.assert_not_called()

    def test_agent_cleanup_join_failure_still_contains_and_reaps(self):
        self.prepared()
        process = mock.Mock()
        process.poll.side_effect = RuntimeError("poll failed")
        process.wait.return_value = 137
        with mock.patch.object(self.dispatcher.subprocess, "Popen", return_value=process), \
             mock.patch.object(
                 self.dispatcher.threading.Thread, "join",
                 side_effect=RuntimeError("join failed")), \
             mock.patch.object(self.dispatcher.executor, "process_pending", return_value=0), \
             mock.patch.object(self.dispatcher.executor, "contain_container") as contain:
            with self.assertRaises(self.dispatcher.executor.ContainmentError):
                self.dispatcher.run_agent(self.cfg, JOB)
        contain.assert_called_once()
        process.kill.assert_called()
        process.wait.assert_called()


class PublicationTests(DispatcherTestBase):
    def prepare_output(self):
        work = self.prepared()
        (work / "out" / "report.md").write_text(
            "report PLANTED-SENTINEL", encoding="utf-8")
        (work / "out" / "evidence").mkdir()
        (work / "out" / "evidence" / "E1.txt").write_text(
            "evidence PLANTED-SENTINEL", encoding="utf-8")
        message = {"message": {"role": "assistant", "usage": {"cost": {"total": 0.4}}}}
        (work / "session" / "one.jsonl").write_text(json.dumps(message) + "\n", encoding="utf-8")
        (work / "out" / "status.json").write_text(json.dumps({
            "job_id": JOB, "status": "done", "exit_code": 0,
            "model": "openai/gpt-6.1-sol", "started": "2026-11-17T00:00:00Z",
            "ended": "2026-11-17T00:01:00Z",
        }), encoding="utf-8")
        return work

    @needs_symlink
    def test_publish_never_follows_an_evidence_symlink(self):
        work = self.prepare_output()
        outside = self.root / "outside-secret"
        outside.write_text("TOP-SECRET", encoding="utf-8")
        os.symlink(outside, work / "out" / "evidence" / "leak.txt")
        self.dispatcher.publish_job(self.cfg, JOB)
        self.assertFalse((self.root / "reports" / JOB / "evidence" / "leak.txt").exists())

    def test_publish_redacts_exact_secret_values_from_text(self):
        self.prepare_output()
        result = self.dispatcher.publish_job(self.cfg, JOB)
        report = self.root / "reports" / JOB
        self.assertEqual(result["cost_usd"], 0.4)
        for path in (report / "report.md", report / "evidence" / "E1.txt"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", text)
            self.assertNotIn("PLANTED-SENTINEL", text)

    def test_publish_enforces_report_evidence_and_session_caps(self):
        work = self.prepare_output()
        (work / "out" / "report.md").write_bytes(b"r" * 11)
        (work / "out" / "evidence" / "E1.txt").write_bytes(b"e" * 11)
        (work / "session" / "one.jsonl").write_bytes(b"s" * 11)
        with mock.patch.object(self.dispatcher, "MAX_REPORT_BYTES", 10), \
             mock.patch.object(self.dispatcher, "MAX_EVIDENCE_BYTES", 10), \
             mock.patch.object(self.dispatcher, "MAX_SESSION_BYTES", 10):
            self.dispatcher.publish_job(self.cfg, JOB)
        report = self.root / "reports" / JOB
        self.assertFalse((report / "report.md").exists())
        self.assertFalse((report / "evidence" / "E1.txt").exists())
        self.assertLessEqual((report / "session.jsonl").stat().st_size, 10)

    def test_recovery_writes_the_distinct_recovered_status_once(self):
        job = self.root / "jobs" / "work" / JOB
        job.mkdir()
        self.assertEqual(self.dispatcher.recover_interrupted(self.cfg), [JOB])
        result_path = self.root / "reports" / JOB / "result.json"
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8"))["status"],
                         "recovered-after-restart")
        self.assertEqual(self.dispatcher.recover_interrupted(self.cfg), [])

    def test_recovery_scans_existing_session_cost(self):
        job = self.root / "jobs" / "work" / JOB
        session = job / "session"
        session.mkdir(parents=True)
        message = {"message": {"role": "assistant", "usage": {"cost": {"total": 0.4}}}}
        (session / "one.jsonl").write_text(json.dumps(message) + "\n", encoding="utf-8")
        self.dispatcher.recover_interrupted(self.cfg)
        result = json.loads(
            (self.root / "reports" / JOB / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["cost_usd"], 0.4)
        self.assertIs(result["cost_incomplete"], False)

    def test_recovery_contains_malformed_cost_records(self):
        job = self.root / "jobs" / "work" / JOB
        session = job / "session"
        session.mkdir(parents=True)
        valid = {"message": {"role": "assistant", "usage": {"cost": {"total": 0.4}}}}
        hostile = b'{"message":{"role":"assistant","usage":{"cost":{"total":' + \
                  b"9" * 400 + b"}}}}\n"
        (session / "one.jsonl").write_bytes(
            hostile + (json.dumps(valid) + "\n").encode())
        self.dispatcher.recover_interrupted(self.cfg)
        result = json.loads(
            (self.root / "reports" / JOB / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["cost_usd"], 0.4)
        self.assertIs(result["cost_incomplete"], True)

class PortedClaimAndVerificationTests(DispatcherTestBase):
    def result(self, job_id=JOB):
        path = self.root / "reports" / job_id / "result.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def reject(self, mutate, expected):
        job = self.make_job()
        mutate(job)
        self.dispatcher.claim_job(self.cfg, JOB)
        with mock.patch.object(self.dispatcher, "run_agent") as launch:
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], expected)
        self.assertEqual(self.result()["status"], expected)
        launch.assert_not_called()

    def test_second_claim_fails_and_preserves_the_first_claim(self):
        self.make_job()
        first = self.dispatcher.claim_job(self.cfg, JOB)
        with self.assertRaises(self.dispatcher.JobError):
            self.dispatcher.claim_job(self.cfg, JOB)
        self.assertTrue((first / "upload" / "job.json").is_file())

    def test_unsafe_job_ids_are_rejected(self):
        for job_id in ("../escape", "a/b", "UPPER-CASE-1", "short"):
            with self.subTest(job_id=job_id):
                with self.assertRaises(self.dispatcher.JobError):
                    self.dispatcher.claim_job(self.cfg, job_id)

    def test_half_copied_incoming_directories_are_not_pending(self):
        self.make_job()
        (self.cfg.inbox / ".incoming-job-20261116-0099").mkdir()
        self.assertEqual(self.dispatcher.pending_jobs(self.cfg), [JOB])

    def test_pending_jobs_are_oldest_first(self):
        older, newer = "job-20261116-0010", "job-20261116-0011"
        self.make_job(older)
        self.make_job(newer)
        os.utime(self.cfg.inbox / older, ns=(1_000, 1_000))
        os.utime(self.cfg.inbox / newer, ns=(2_000, 2_000))
        self.assertEqual(self.dispatcher.pending_jobs(self.cfg), [older, newer])

    def test_added_target_file_is_rejected_hash(self):
        self.reject(lambda job: (job / "target" / "added.bin").write_bytes(b"added"),
                    "rejected-hash")

    def test_missing_target_file_is_rejected_hash(self):
        self.reject(lambda job: (job / "target" / "tool").unlink(), "rejected-hash")

    def test_changed_question_is_rejected_hash(self):
        self.reject(lambda job: (job / "question.md").write_bytes(b"changed"),
                    "rejected-hash")

    @needs_symlink
    def test_symlink_in_target_is_rejected_hash(self):
        outside = self.root / "outside"
        outside.write_bytes(b"outside")

        def plant(job):
            os.symlink(outside, job / "target" / "link")

        self.reject(plant, "rejected-hash")

    def test_malformed_job_json_is_rejected_job(self):
        self.reject(lambda job: (job / "job.json").write_text("{nope", encoding="utf-8"),
                    "rejected-job")

    def test_any_extra_top_level_entry_is_rejected_job(self):
        self.reject(lambda job: (job / "extra").write_text("x", encoding="utf-8"),
                    "rejected-job")

    def test_the_three_submitted_entries_alone_are_accepted(self):
        job = self.make_job()
        self.assertEqual({path.name for path in job.iterdir()},
                         {"target", "question.md", "job.json"})
        self.dispatcher.claim_job(self.cfg, JOB)
        meta = self.dispatcher.prepare_job(self.cfg, JOB)
        self.assertEqual(meta["job_id"], JOB)

    @needs_symlink
    def test_agent_copy_has_no_exec_bits_while_target_keeps_them(self):
        work = self.prepared()
        target_mode = stat.S_IMODE((work / "target" / "tool").stat().st_mode)
        copy_mode = stat.S_IMODE((work / "in" / "target" / "tool").stat().st_mode)
        self.assertNotEqual(target_mode & 0o111, 0)
        self.assertEqual(copy_mode & 0o111, 0)
        self.assertEqual(copy_mode & 0o222, 0)

    def test_rejected_job_never_launches_the_agent_or_a_profile(self):
        job = self.make_job()
        (job / "job.json").write_text("{nope", encoding="utf-8")
        self.dispatcher.claim_job(self.cfg, JOB)
        with mock.patch.object(self.dispatcher, "run_agent") as launch:
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], "rejected-job")
        launch.assert_not_called()
        self.assertFalse((self.root / "jobs" / "work" / JOB / "home").exists())


class PortedAgentLaunchTests(DispatcherTestBase):
    def test_agent_home_and_tmp_are_per_job_tmpfs_mounts(self):
        argv = self.dispatcher.build_agent_argv(self.cfg, JOB)
        tmpfs = [argv[index + 1] for index, value in enumerate(argv) if value == "--tmpfs"]
        self.assertEqual(tmpfs, ["/home/re:size=512m,noexec,nosuid,nodev",
                                 "/tmp:size=512m,noexec,nosuid,nodev"])


class PortedHostOutcomeTests(DispatcherTestBase):
    def test_exit_zero_three_other_and_timeout_map_to_final_statuses(self):
        cases = (
            ("job-outcome-0001", "done", 0, False, "done"),
            ("job-outcome-0002", "cost-cap", 0, False, "cost-cap"),
            ("job-outcome-0003", "done", 7, False, "failed"),
            ("job-outcome-0004", "done", 137, True, "time-cap"),
            ("job-outcome-0005", "cost-cap", 1, False, "failed"),
            ("job-outcome-0006", "time-cap", 0, False, "time-cap"),
        )
        for job_id, runner_status, exit_code, timed_out, expected in cases:
            with self.subTest(expected=expected):
                self.make_job(job_id)
                self.dispatcher.claim_job(self.cfg, job_id)

                def run_agent(_cfg, active_id):
                    work = self.root / "jobs" / "work" / active_id
                    (work / "out" / "report.md").write_text("report", encoding="utf-8")
                    (work / "out" / "status.json").write_text(json.dumps({
                        "job_id": active_id, "status": runner_status,
                        "exit_code": exit_code, "model": "openai/gpt-6.1-sol",
                        "started": "2026-11-17T00:00:00Z",
                        "ended": "2026-11-17T00:01:00Z",
                    }), encoding="utf-8")
                    return self.dispatcher.AgentResult(exit_code, timed_out)

                with mock.patch.object(self.dispatcher, "run_agent", side_effect=run_agent):
                    result = self.dispatcher.run_claimed_job(self.cfg, job_id)
                self.assertEqual(result["status"], expected)


class PortedPublicationContractTests(DispatcherTestBase):
    def prepared_output(self, status="done", extra=None):
        work = self.prepared()
        (work / "out" / "report.md").write_text("report", encoding="utf-8")
        body = {
            "job_id": JOB, "status": status, "exit_code": 0 if status == "done" else 1,
            "model": "openai/gpt-6.1-sol", "started": "2026-11-17T00:00:00Z",
            "ended": "2026-11-17T00:01:00Z",
        }
        body.update(extra or {})
        (work / "out" / "status.json").write_text(json.dumps(body), encoding="utf-8")
        return work

    def test_result_json_records_the_contract_fields(self):
        self.prepared_output()
        result = self.dispatcher.publish_job(self.cfg, JOB)
        self.assertEqual(set(result), {
            "job_id", "status", "exit_code", "cost_usd", "cost_incomplete",
            "model", "started", "ended",
        })
        self.assertEqual((result["job_id"], result["status"], result["cost_usd"]),
                         (JOB, "done", 0.0))

    def test_failed_result_keeps_the_contract_fields(self):
        self.prepared_output("failed")
        result = self.dispatcher.publish_job(self.cfg, JOB)
        self.assertTrue({
            "job_id", "status", "exit_code", "cost_usd", "cost_incomplete",
            "model", "started", "ended",
        } <= set(result))
        self.assertEqual(result["status"], "failed")

    def test_exception_text_never_reaches_result_json(self):
        self.prepared_output("failed", {"error": "LEAKED-EXCEPTION-TEXT"})
        self.dispatcher.publish_job(self.cfg, JOB)
        text = (self.cfg.reports / JOB / "result.json").read_text(encoding="utf-8")
        self.assertNotIn("LEAKED-EXCEPTION-TEXT", text)

    def test_session_cost_sums_assistant_usage_only(self):
        lines = [
            {"message": {"role": "assistant", "usage": {"cost": {"total": 0.1}}}},
            {"message": {"role": "assistant", "usage": {"cost": {"total": 0.25}}}},
            {"message": {"role": "user", "usage": {"cost": {"total": 9.0}}}},
        ]
        data = ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
        self.assertEqual(self.dispatcher._session_cost(data), 0.35)

    def test_session_cost_ignores_malformed_and_non_assistant_lines(self):
        data = (
            b'not json\n'
            b'{"message":{"role":"user","usage":{"cost":{"total":9}}}}\n'
            b'{"message":{"role":"assistant","usage":{"cost":{"total":"bad"}}}}\n'
            b'{"message":{"role":"assistant","usage":{"cost":{"total":0.4}}}}\n'
        )
        self.assertEqual(self.dispatcher._session_cost(data), 0.4)

    def test_missing_session_tree_has_zero_cost(self):
        destination = self.root / "merged.jsonl"
        self.assertEqual(
            self.dispatcher._scan_session_cost(self.root / "missing-session"),
            (0.0, False))
        self.dispatcher._merge_sessions(self.root / "missing-session", destination, ())
        self.assertEqual(destination.read_bytes(), b"")

    def test_session_jsonl_is_published_for_the_active_job(self):
        work = self.prepared_output()
        line = '{"message":{"role":"assistant","usage":{"cost":{"total":0.2}}}}\n'
        (work / "session" / "one.jsonl").write_text(line, encoding="utf-8")
        self.dispatcher.publish_job(self.cfg, JOB)
        published = self.cfg.reports / JOB / "session.jsonl"
        self.assertEqual(published.read_text(encoding="utf-8"), line)

    def test_evidence_total_cap_applies_across_the_whole_tree(self):
        source = self.root / "evidence-source"
        destination = self.root / "evidence-destination"
        for relative, size in (("a/one.bin", 60), ("b/two.bin", 60),
                               ("c/three.bin", 30), ("top.bin", 5)):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
        destination.mkdir()
        budget = self.dispatcher._ScanBudget(100)
        self.dispatcher._copy_evidence(source, destination, budget, ())
        copied = [path for path in destination.rglob("*") if path.is_file()]
        self.assertLessEqual(sum(path.stat().st_size for path in copied), 100)
        self.assertGreater(len(copied), 0)
        self.assertLess(len(copied), 4)

    def test_evidence_per_file_cap_skips_only_the_oversized_file(self):
        source = self.root / "evidence-source"
        destination = self.root / "evidence-destination"
        source.mkdir()
        destination.mkdir()
        (source / "large.bin").write_bytes(b"x" * 31)
        (source / "small.bin").write_bytes(b"x" * 5)
        with mock.patch.object(self.dispatcher, "MAX_EVIDENCE_FILE_BYTES", 30):
            self.dispatcher._copy_evidence(
                source, destination, self.dispatcher._ScanBudget(100), ())
        self.assertFalse((destination / "large.bin").exists())
        self.assertEqual((destination / "small.bin").read_bytes(), b"x" * 5)


class IncrementalPublishWalkTests(DispatcherTestBase):
    def test_twenty_thousand_entries_stop_at_the_publish_cap_without_materializing(self):
        scanned = {"count": 0}

        class Entry:
            name = "special"

            @staticmethod
            def is_symlink():
                return False

            @staticmethod
            def is_dir(*, follow_symlinks=False):
                return False

            @staticmethod
            def is_file(*, follow_symlinks=False):
                return False

        class Entries:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                for _ in range(20001):
                    scanned["count"] += 1
                    yield Entry()

        destination = self.root / "evidence-destination"
        destination.mkdir()
        (self.root / "evidence-source").mkdir()
        with mock.patch.object(os, "scandir", return_value=Entries()), \
             mock.patch.object(os, "listdir",
                               side_effect=AssertionError("directory was materialized")):
            with self.assertRaises(self.dispatcher.PublishError):
                self.dispatcher._copy_evidence(
                    self.root / "evidence-source", destination,
                    self.dispatcher._ScanBudget(1024), ())
        self.assertEqual(scanned["count"], 10001)


class FinalReviewRegressionTests(DispatcherTestBase):
    def test_internal_network_uses_isolated_gateway_modes(self):
        compose = yaml.safe_load((CONTAINER / "compose.yaml").read_text(encoding="utf-8"))
        network = compose["networks"]["re-internal"]
        self.assertIs(network["internal"], True)
        self.assertEqual(network["driver_opts"], {
            "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
            "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
        })

    def test_agents_reach_proxy_at_compose_static_address_without_dns(self):
        compose = yaml.safe_load((CONTAINER / "compose.yaml").read_text(encoding="utf-8"))
        egress = compose["services"]["re-egress"]
        address = egress["networks"]["re-internal"]["ipv4_address"]
        subnet = compose["networks"]["re-internal"]["ipam"]["config"][0]["subnet"]
        self.assertIn(ipaddress.ip_address(address), ipaddress.ip_network(subnet))
        self.assertEqual(self.dispatcher.EGRESS_IP, address)
        self.assertIn("/etc/re-lab/egress-resolv.conf:/etc/resolv.conf:ro", egress["volumes"])
        argv = self.dispatcher.build_agent_argv(self.cfg, JOB)
        self.assertEqual(argv[argv.index("--add-host") + 1], f"re-egress:{address}")

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_agent_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "agent-output"
        os.mkfifo(fifo)
        returned = []
        worker = threading.Thread(
            target=lambda: returned.append(self.dispatcher._read_regular(fifo, 1024)),
            daemon=True)
        worker.start()
        worker.join(0.5)
        self.assertFalse(worker.is_alive(), "opening an agent FIFO blocked the dispatcher")
        self.assertEqual(returned, [None])

    def test_agent_launch_oserror_becomes_a_failed_result(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)
        with mock.patch.object(self.dispatcher, "run_agent", side_effect=OSError("private")):
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "agent execution failed: OSError")
        stored = json.loads((self.cfg.reports / JOB / "result.json").read_text(encoding="utf-8"))
        self.assertNotIn("private", json.dumps(stored))

    def test_publication_oserror_becomes_a_failed_result(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)
        agent = self.dispatcher.AgentResult(exit_code=0, timed_out=False)
        with mock.patch.object(self.dispatcher, "run_agent", return_value=agent), \
             mock.patch.object(self.dispatcher, "publish_job", side_effect=OSError("private")):
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "publication failed: OSError")


    def test_publication_failure_preserves_known_cost_and_marks_it_incomplete(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)

        def agent(_cfg, job_id):
            session = self.cfg.work / job_id / "session" / "one.jsonl"
            message = {
                "message": {"role": "assistant", "usage": {"cost": {"total": 0.4}}}
            }
            session.write_text(json.dumps(message) + "\n", encoding="utf-8")
            return self.dispatcher.AgentResult(exit_code=0, timed_out=False)

        with mock.patch.object(self.dispatcher, "run_agent", side_effect=agent), \
             mock.patch.object(self.dispatcher, "publish_job", side_effect=OSError("private")):
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["cost_usd"], 0.4)
        self.assertIs(result["cost_incomplete"], True)

    def test_malformed_agent_status_becomes_a_fixed_failed_result(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)

        def agent(_cfg, job_id):
            status = self.cfg.work / job_id / "out" / "status.json"
            status.write_text(
                json.dumps({"job_id": job_id, "status": []}), encoding="utf-8")
            return self.dispatcher.AgentResult(exit_code=0, timed_out=False)

        with mock.patch.object(self.dispatcher, "run_agent", side_effect=agent):
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "agent status invalid")

    def test_agent_status_read_error_becomes_a_fixed_failed_result(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)
        agent = self.dispatcher.AgentResult(exit_code=0, timed_out=False)
        with mock.patch.object(self.dispatcher, "run_agent", return_value=agent), \
             mock.patch.object(
                 self.dispatcher, "_read_agent_status", side_effect=OSError("private")):
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "agent status invalid")

    def test_deep_manifest_becomes_a_rejected_result(self):
        inbox = self.make_job()
        (inbox / "job.json").write_bytes(b"[" * 1100 + b"0" + b"]" * 1100)
        self.dispatcher.claim_job(self.cfg, JOB)
        result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], "rejected-job")
        stored = json.loads(
            (self.cfg.reports / JOB / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "rejected-job")

    def test_cost_scan_counts_session_larger_than_publication_cap(self):
        work = self.prepared()
        line = json.dumps({
            "message": {"role": "assistant", "usage": {"cost": {"total": 0.75}}}
        }).encode() + b"\n"
        session = work / "session" / "large.jsonl"
        with session.open("wb") as handle:
            handle.write(line)
            handle.truncate(60 * 1024 ** 2)
        result = self.dispatcher.publish_job(self.cfg, JOB)
        self.assertEqual(result["cost_usd"], 0.75)
        self.assertIs(result["cost_incomplete"], True)
        self.assertLessEqual(
            (self.cfg.reports / JOB / "session.jsonl").stat().st_size,
            self.dispatcher.MAX_SESSION_BYTES)

    def test_unreadable_session_marks_cost_incomplete(self):
        source = self.root / "session-unreadable"
        source.mkdir()
        blocked = source / "blocked.jsonl"
        blocked.write_text("{}\n", encoding="utf-8")
        real_open = self.dispatcher.os.open

        def refuse(path, *args, **kwargs):
            if Path(path) == blocked:
                raise PermissionError("blocked")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(self.dispatcher.os, "open", side_effect=refuse):
            cost, incomplete = self.dispatcher._scan_session_cost(source)
        self.assertEqual(cost, 0.0)
        self.assertIs(incomplete, True)

    def test_nonfinite_accumulated_cost_is_marked_incomplete(self):
        source = self.root / "session-overflow"
        source.mkdir()
        message = {"message": {"role": "assistant", "usage": {"cost": {"total": 1e308}}}}
        (source / "one.jsonl").write_text(
            json.dumps(message) + "\n" + json.dumps(message) + "\n",
            encoding="utf-8")
        cost, incomplete = self.dispatcher._scan_session_cost(source)
        self.assertEqual(cost, 0.0)
        self.assertIs(incomplete, True)

    def test_deeply_nested_and_oversized_cost_lines_are_bounded(self):
        source = self.root / "session-hostile"
        source.mkdir()
        valid = {"message": {"role": "assistant", "usage": {"cost": {"total": 0.4}}}}
        deep = (b"[" * 1100) + b"0" + (b"]" * 1100) + b"\n"
        oversized = b"x" * 300 + b"\n"
        (source / "one.jsonl").write_bytes(
            deep + oversized + (json.dumps(valid) + "\n").encode())
        with mock.patch.object(self.dispatcher, "MAX_COST_LINE_BYTES", 256):
            cost, incomplete = self.dispatcher._scan_session_cost(source)
        self.assertEqual(cost, 0.4)
        self.assertIs(incomplete, True)

    def test_nonfinite_cost_value_marks_accounting_incomplete(self):
        source = self.root / "session-nonfinite"
        source.mkdir()
        valid = {"message": {"role": "assistant", "usage": {"cost": {"total": 0.4}}}}
        (source / "one.jsonl").write_text(
            '{"message":{"role":"assistant","usage":{"cost":{"total":1e999}}}}\n'
            '{"message":{"role":"assistant","usage":{"cost":{"total":-1e999}}}}\n'
            + json.dumps(valid) + "\n",
            encoding="utf-8")
        cost, incomplete = self.dispatcher._scan_session_cost(source)
        self.assertEqual(cost, 0.4)
        self.assertIs(incomplete, True)

    def test_midstream_read_error_preserves_cost_and_consumes_remaining_budget(self):
        source = self.root / "broken.jsonl"
        source.write_bytes(b"placeholder")
        valid = (
            json.dumps({
                "message": {"role": "assistant", "usage": {"cost": {"total": 0.4}}}
            }).encode() + b"\n")

        class BrokenReader:
            def __init__(self, fd):
                self.fd = fd
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                os.close(self.fd)

            def fileno(self):
                return self.fd

            def readline(self, _size):
                self.calls += 1
                if self.calls == 1:
                    return valid
                raise OSError("read failed")

            def read(self, _size):
                raise OSError("read failed")

        with mock.patch.object(
                self.dispatcher.os, "fdopen",
                side_effect=lambda fd, _mode: BrokenReader(fd)):
            cost, consumed, incomplete = self.dispatcher._scan_cost_file(source, 1000)
        self.assertEqual(cost, 0.4)
        self.assertEqual(consumed, 1000)
        self.assertIs(incomplete, True)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "requires Linux root for ownership checks")
    def test_published_report_tree_is_group_readable(self):
        work = self.prepared()
        (work / "out" / "report.md").write_text("report", encoding="utf-8")
        (work / "out" / "evidence").mkdir()
        (work / "out" / "evidence" / "proof.txt").write_text("proof", encoding="utf-8")
        self.dispatcher.publish_job(self.cfg, JOB)
        report = self.cfg.reports / JOB
        for path in report.rglob("*"):
            expected = 0o750 if path.is_dir() else 0o640
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, path)
            self.assertEqual(path.stat().st_gid, self.cfg.report_gid, path)
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o750)
        self.assertEqual(report.stat().st_gid, self.cfg.report_gid)

    @needs_fd_ops
    def test_submit_prepare_and_executor_preserve_executable_mode(self):
        submit = load("re_submit")
        source = self.root / "source"
        source.mkdir()
        tool = source / "tool"
        tool.write_bytes(b"payload")
        os.chmod(tool, 0o644)
        staging = self.cfg.inbox / JOB
        staging.mkdir()
        submit.stage_job(
            staging, JOB, source, "Question", None, None, executables=["tool"])
        self.dispatcher.claim_job(self.cfg, JOB)
        self.dispatcher.prepare_job(self.cfg, JOB)
        self.assertNotEqual(
            stat.S_IMODE((self.cfg.work / JOB / "target" / "tool").stat().st_mode) & 0o111, 0)
        request = self.dispatcher.executor.validate_request({"job_id": JOB, "argv": ["true"]})
        run = self.dispatcher.executor.prepare_run(self.dispatcher.executor_config(self.cfg, JOB), request)
        self.addCleanup(run.discard)
        self.assertNotEqual(stat.S_IMODE((run.path / "in" / "tool").stat().st_mode) & 0o111, 0)


class DispatcherStartupOrderTests(DispatcherTestBase):
    def test_recovery_runs_before_the_first_inbox_scan(self):
        order = []

        def record(name, result=None):
            def callback(*_args, **_kwargs):
                order.append(name)
                return result
            return callback

        with mock.patch.object(self.dispatcher, "load_config", return_value=self.cfg), \
             mock.patch.object(self.dispatcher, "cleanup_containers",
                               side_effect=record("cleanup")), \
             mock.patch.object(self.dispatcher.executor, "sweep_runs",
                               side_effect=record("sweep")), \
             mock.patch.object(self.dispatcher, "recover_interrupted",
                               side_effect=record("recover", [])), \
             mock.patch.object(self.dispatcher, "process_once",
                               side_effect=record("scan", False)):
            self.assertEqual(self.dispatcher.main(["--once"]), 0)
        self.assertEqual(order, ["cleanup", "sweep", "recover", "scan"])
    def test_cleanup_precedes_sweep_recovery_and_claim_with_fake_docker(self):
        log = self.root / "startup-order.log"
        removed = self.root / "removed"
        fake = self.root / "fake-docker.py"
        fake.write_text(textwrap.dedent("""
            import pathlib
            import sys
            log = pathlib.Path(sys.argv[1])
            removed = pathlib.Path(sys.argv[2])
            args = sys.argv[3:]
            with log.open("a", encoding="utf-8") as handle:
                handle.write("docker:" + args[0] + "\\n")
            if args[0] == "ps" and "--no-trunc" not in args:
                print("ab" * 6)
            elif args[0] == "ps" and not removed.exists():
                print("ab" * 32)
            elif args[0] == "rm":
                removed.write_text("yes", encoding="utf-8")
        """), encoding="utf-8")
        cfg = self.dispatcher.Config(
            root=self.root, agent_image=IMAGE, exec_image=EXEC_IMAGE,
            models_env=self.models,
            docker_prefix=(sys.executable, str(fake), str(log), str(removed)))

        def record(name, result=None):
            def callback(*_args, **_kwargs):
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(name + "\n")
                return result
            return callback

        with mock.patch.object(self.dispatcher, "load_config", return_value=cfg), \
             mock.patch.object(self.dispatcher.executor, "sweep_runs",
                               side_effect=record("sweep")), \
             mock.patch.object(self.dispatcher, "recover_interrupted",
                               side_effect=record("recover", [])), \
             mock.patch.object(self.dispatcher, "process_once",
                               side_effect=record("claim", False)):
            self.assertEqual(self.dispatcher.main(["--once"]), 0)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), [
            "docker:ps", "docker:rm", "docker:ps", "sweep", "recover", "claim"])

    def test_sweep_failure_stops_before_recovery_and_claim(self):
        with mock.patch.object(self.dispatcher, "load_config", return_value=self.cfg), \
             mock.patch.object(self.dispatcher, "cleanup_containers"), \
             mock.patch.object(
                 self.dispatcher.executor, "sweep_runs",
                 side_effect=self.dispatcher.executor.ContainmentError("blocked")), \
             mock.patch.object(self.dispatcher, "recover_interrupted") as recover, \
             mock.patch.object(self.dispatcher, "process_once") as claim:
            with self.assertRaises(self.dispatcher.executor.ContainmentError):
                self.dispatcher.main(["--once"])
        recover.assert_not_called()
        claim.assert_not_called()



if __name__ == "__main__":
    unittest.main()
