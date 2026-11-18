"""Regression tests for the adversarial-review hardening of the dispatcher and executor."""
import base64
import binascii
import json
import os
import shutil
import subprocess
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests.test_re_container_dispatcher import (
    EXEC_IMAGE, IMAGE, JOB, DispatcherTestBase, digest, load, needs_fd_ops)

POSIX_ROOT = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0
needs_root = unittest.skipUnless(POSIX_ROOT, "requires Linux root")
SECRET = "sk-live-0123456789abcdefXYZ"


def events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


class ClaimTests(DispatcherTestBase):
    @needs_fd_ops
    def test_symlinked_inbox_entry_is_never_claimed(self):
        real = self.root / "elsewhere"
        real.mkdir()
        os.symlink(real, self.cfg.inbox / JOB)
        with self.assertRaises(self.dispatcher.JobError):
            self.dispatcher.claim_job(self.cfg, JOB)
        self.assertFalse((self.cfg.work / JOB).exists())

    @needs_fd_ops
    def test_claim_detects_an_entry_swapped_during_the_rename(self):
        self.make_job()
        decoy = self.root / "decoy"
        decoy.mkdir()
        real_rename = os.rename

        def swap_then_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            # The uploader exchanges the verified directory for another one mid-claim.
            real_rename(self.cfg.inbox / JOB, self.root / "moved-away")
            real_rename(decoy, self.cfg.inbox / JOB)
            return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch.object(self.dispatcher.os, "rename", side_effect=swap_then_rename):
            with self.assertRaisesRegex(self.dispatcher.JobError, "changed during the claim"):
                self.dispatcher.claim_job(self.cfg, JOB)

    @needs_fd_ops
    def test_claimed_work_dir_is_root_only_and_holds_the_upload(self):
        self.make_job()
        work = self.dispatcher.claim_job(self.cfg, JOB)
        self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
        self.assertEqual({path.name for path in work.iterdir()}, {"upload"})

    @needs_fd_ops
    def test_reused_ids_are_quarantined_and_the_queue_keeps_moving(self):
        (self.cfg.reports / JOB).mkdir()
        self.make_job()
        later = "job-20261116-0099"
        self.make_job(later)
        os.utime(self.cfg.inbox / JOB, ns=(1, 1))
        ran = []
        self.assertTrue(self.dispatcher.process_once(self.cfg, run_job=lambda _c, j: ran.append(j)))
        self.assertEqual(ran, [later])
        self.assertFalse((self.cfg.inbox / JOB).exists())
        self.assertEqual(len(list((self.root / "jobs" / "quarantine").iterdir())), 1)

    def test_vanishing_inbox_entries_do_not_crash_the_scan(self):
        self.make_job()
        real_scandir = os.scandir

        class Vanishing:
            def __init__(self, entry):
                self.entry = entry
                self.name = entry.name

            def is_dir(self, *, follow_symlinks=True):
                return True

            def stat(self, *, follow_symlinks=True):
                raise FileNotFoundError

        class Listing:
            def __init__(self, path):
                self.inner = real_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.inner.close()

            def __iter__(self):
                return (Vanishing(entry) for entry in self.inner)

        with mock.patch.object(self.dispatcher.os, "scandir", side_effect=Listing):
            self.assertEqual(self.dispatcher.pending_jobs(self.cfg), [])

    @needs_fd_ops
    def test_unexpected_job_errors_are_recorded_without_stopping_the_service(self):
        self.make_job()
        with mock.patch.object(self.dispatcher, "prepare_job",
                               side_effect=NotImplementedError("boom")):
            self.assertTrue(self.dispatcher.process_once(self.cfg))
        result = json.loads((self.cfg.reports / JOB / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "dispatcher error: NotImplementedError")


@needs_fd_ops
class AdmissionTests(DispatcherTestBase):
    def test_writes_through_a_held_descriptor_never_reach_the_verified_copy(self):
        job = self.make_job()
        held = os.open(job / "target" / "tool", os.O_WRONLY)
        self.addCleanup(os.close, held)
        work = self.dispatcher.claim_job(self.cfg, JOB)
        self.dispatcher.prepare_job(self.cfg, JOB)
        os.pwrite(held, b"tampered", 0)
        self.assertEqual((work / "target" / "tool").read_bytes(), b"submitted")
        self.assertFalse((work / "upload").exists())

    def test_upload_directories_are_locked_before_they_are_listed(self):
        job = self.make_job()
        (job / "target" / "sub").mkdir()
        data = b"nested"
        (job / "target" / "sub" / "f").write_bytes(data)
        meta = json.loads((job / "job.json").read_text(encoding="utf-8"))
        meta["files"].append({"path": "target/sub/f", "sha256": digest(data), "size": len(data)})
        (job / "job.json").write_text(json.dumps(meta), encoding="utf-8")
        for path in (job, job / "target", job / "target" / "sub"):
            os.chmod(path, 0o777)  # an upload left group- and world-writable
        modes = []
        real_scandir = self.dispatcher.os.scandir

        def recording_scandir(target):
            if isinstance(target, int):
                modes.append(stat.S_IMODE(os.fstat(target).st_mode))
            return real_scandir(target)

        self.dispatcher.claim_job(self.cfg, JOB)
        with mock.patch.object(self.dispatcher.os, "scandir", side_effect=recording_scandir), \
             mock.patch.object(self.dispatcher.executor, "_require_fd_ops"):
            self.dispatcher.prepare_job(self.cfg, JOB)
        self.assertTrue(modes)
        self.assertTrue(all(mode & 0o022 == 0 for mode in modes), [oct(m) for m in modes])

    def test_hard_linked_upload_files_are_rejected(self):
        job = self.make_job()
        os.link(job / "target" / "tool", job / "target" / "tool-link")
        meta = json.loads((job / "job.json").read_text(encoding="utf-8"))
        meta["files"].append({"path": "target/tool-link", "sha256": digest(b"submitted"),
                              "size": 9})
        (job / "job.json").write_text(json.dumps(meta), encoding="utf-8")
        self.dispatcher.claim_job(self.cfg, JOB)
        with self.assertRaises(self.dispatcher.HashMismatch):
            self.dispatcher.prepare_job(self.cfg, JOB)

    def test_control_characters_in_manifest_paths_are_rejected(self):
        job = self.make_job()
        meta = json.loads((job / "job.json").read_text(encoding="utf-8"))
        meta["files"][0]["path"] = "target/to\x1bol"
        (job / "job.json").write_text(json.dumps(meta), encoding="utf-8")
        self.dispatcher.claim_job(self.cfg, JOB)
        with self.assertRaises(self.dispatcher.JobError):
            self.dispatcher.prepare_job(self.cfg, JOB)

    def test_verified_target_and_question_are_new_root_owned_inodes(self):
        job = self.make_job()
        before = os.stat(job / "target" / "tool").st_ino
        work = self.dispatcher.claim_job(self.cfg, JOB)
        self.dispatcher.prepare_job(self.cfg, JOB)
        self.assertNotEqual(os.stat(work / "target" / "tool").st_ino, before)
        self.assertEqual(stat.S_IMODE((work / "question.md").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE((work / "job.json").stat().st_mode), 0o444)


class ResultAndRedactionTests(DispatcherTestBase):
    def setUp(self):
        super().setUp()
        self.models.write_text(f"OPENAI_API_KEY={SECRET}\nFLAG=1\nSHORT=true\n", encoding="utf-8")

    def test_agent_status_is_reduced_to_validated_claims(self):
        work = self.prepared()
        (work / "out" / "status.json").write_text(json.dumps({
            "job_id": JOB, "status": "failed", "exit_code": "\x1b]52;c;cGF5\x07",
            "model": {"x": 1}, "started": [], "ended": {}, "error": "omp-exit-2",
            "reason": "\x1b[2J"}), encoding="utf-8")
        self.assertEqual(self.dispatcher._read_agent_status(work, JOB),
                         {"status": "failed", "error": "omp-exit-2"})
        (work / "out" / "status.json").write_text(json.dumps({
            "job_id": JOB, "status": "needs-vm-lane", "reason": "needs Windows"}), encoding="utf-8")
        self.assertEqual(self.dispatcher._read_agent_status(work, JOB),
                         {"status": "needs-vm-lane", "reason": "needs Windows"})
        (work / "out" / "status.json").write_text(json.dumps({
            "job_id": JOB, "status": "needs-vm-lane", "reason": "bad\x1b"}), encoding="utf-8")
        self.assertEqual(self.dispatcher._read_agent_status(work, JOB)["status"], "failed")

    @needs_fd_ops
    def test_result_uses_dispatcher_facts_not_agent_claims(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)

        def agent(_cfg, job_id):
            (self.cfg.work / job_id / "out" / "status.json").write_text(json.dumps({
                "job_id": job_id, "status": "done", "exit_code": "forged",
                "model": "evil", "started": "1999", "ended": "x"}), encoding="utf-8")
            return self.dispatcher.AgentResult(exit_code=0, timed_out=False)

        with mock.patch.object(self.dispatcher, "run_agent", side_effect=agent):
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["model"], "openai/gpt-6.1-sol")
        self.assertRegex(result["started"], r"^20\d\d-")

    @needs_fd_ops
    def test_every_evidence_type_is_redacted_and_short_values_are_left_alone(self):
        work = self.prepared()
        evidence = work / "out" / "evidence"
        evidence.mkdir()
        encoded = base64.b64encode(b"xx" + SECRET.encode())
        (evidence / "E7-main-decompile.c").write_text(f"char *k = \"{SECRET}\"; // 1 true", encoding="utf-8")
        (evidence / "E1-strings").write_text(f"{SECRET}\n{binascii.hexlify(SECRET.encode()).decode()}",
                                             encoding="utf-8")
        (evidence / "E2-b64.txt").write_bytes(b"blob " + encoded)
        (evidence / "E3.bin").write_bytes(b"\x00\x01" + SECRET.encode() + b"\x02")
        (work / "out" / "report.md").write_text("value 1 is true", encoding="utf-8")
        self.dispatcher.publish_job(self.cfg, JOB)
        published = self.cfg.reports / JOB / "evidence"
        for name in ("E7-main-decompile.c", "E1-strings", "E2-b64.txt"):
            text = (published / name).read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", text, name)
            self.assertNotIn(SECRET, text)
            self.assertNotIn(binascii.hexlify(SECRET.encode()).decode(), text)
        self.assertIn("1 true", (published / "E7-main-decompile.c").read_text(encoding="utf-8"))
        binary = (published / "E3.bin").read_bytes()
        self.assertEqual(len(binary), 3 + len(SECRET))
        self.assertNotIn(SECRET.encode(), binary)
        self.assertEqual((self.cfg.reports / JOB / "report.md").read_text(encoding="utf-8"),
                         "value 1 is true")
        json.loads((self.cfg.reports / JOB / "result.json").read_text(encoding="utf-8"))

    def test_model_usage_records_count_toward_cost(self):
        data = (json.dumps({"type": "model_usage", "purpose": "cache-warm",
                            "usage": {"cost": {"total": 0.25}}}) + "\n" +
                json.dumps({"message": {"role": "assistant",
                                        "usage": {"cost": {"total": 0.5}}}}) + "\n").encode()
        self.assertEqual(self.dispatcher._session_cost(data), 0.75)


class AgentEnvironmentTests(DispatcherTestBase):
    def console_cfg(self):
        console = self.root / "console"
        console.mkdir(mode=0o750)
        os.chmod(console, 0o750)
        return self.dispatcher.Config(
            root=self.root, agent_image=IMAGE, exec_image=EXEC_IMAGE, models_env=self.models,
            docker_prefix=("docker",), console_uid=os.geteuid() or 10002)

    @needs_root
    def test_console_keys_override_and_unsafe_names_are_dropped(self):
        self.models.write_text("OPENAI_API_KEY=root-value-123456\nLD_PRELOAD=/x.so\n"
                               "NODE_OPTIONS=--require x\nlower=1\n", encoding="utf-8")
        cfg = self.console_cfg()
        with mock.patch.object(self.dispatcher, "_console_env",
                               return_value=[("OPENAI_API_KEY", "console-value-123456"),
                                             ("OPENROUTER_API_KEY", "or-value-1234567")]):
            env = dict(self.dispatcher.agent_environment(cfg))
            secrets = self.dispatcher._secret_values(cfg)
        self.assertEqual(env, {"OPENAI_API_KEY": "console-value-123456",
                               "OPENROUTER_API_KEY": "or-value-1234567"})
        self.assertIn(b"root-value-123456", secrets)
        self.assertIn(b"console-value-123456", secrets)

    @needs_root
    def test_console_env_file_must_be_private_and_owned(self):
        cfg = self.console_cfg()
        env_file = self.root / "console" / "models.env"
        env_file.write_text("ANTHROPIC_API_KEY=console-anthropic-1\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        for path in (self.root / "console", env_file):
            os.chown(path, cfg.console_uid, cfg.console_uid)
        self.assertEqual(self.dispatcher._console_env(cfg),
                         [("ANTHROPIC_API_KEY", "console-anthropic-1")])
        os.chmod(env_file, 0o640)
        self.assertEqual(self.dispatcher._console_env(cfg), [])

    @needs_fd_ops
    def test_agent_env_file_is_private_and_removed_after_the_run(self):
        self.prepared()
        path = self.dispatcher._write_agent_env(self.cfg, JOB)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("TEST_SECRET=PLANTED-SENTINEL\n", path.read_text(encoding="utf-8"))


@needs_fd_ops
class StopConditionTests(DispatcherTestBase):
    def run_with(self, *, stop=None, low=False, cancel=False):
        self.prepared()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 137
        if cancel:
            self.cfg.cancel_dir.mkdir(parents=True, exist_ok=True)
            (self.cfg.cancel_dir / JOB).write_text("", encoding="utf-8")
        if stop:
            self.dispatcher.STOP.set()
        with mock.patch.object(self.dispatcher.subprocess, "Popen", return_value=process), \
             mock.patch.object(self.dispatcher.executor, "process_pending", return_value=0), \
             mock.patch.object(self.dispatcher, "storage_state",
                               return_value=(1, 1, low)), \
             mock.patch.object(self.dispatcher.time, "sleep"), \
             mock.patch.object(self.dispatcher.executor, "contain_container") as contain:
            result = self.dispatcher.run_agent(self.cfg, JOB, wall_timeout_s=3600)
        contain.assert_called_once()
        return result

    def test_storage_reserve_contains_the_agent(self):
        self.assertEqual(self.run_with(low=True).stop_reason, "storage-cap")

    def test_cancel_marker_contains_the_agent(self):
        self.assertEqual(self.run_with(cancel=True).stop_reason, "cancelled")

    def test_dispatcher_stop_contains_the_agent(self):
        self.assertEqual(self.run_with(stop=True).stop_reason, "stopped")

    def test_final_statuses_for_stop_reasons(self):
        final = self.dispatcher._final_status
        result = self.dispatcher.AgentResult
        self.assertEqual(final(result(137, False, "storage-cap"), {"status": "done"})["status"],
                         "storage-cap")
        self.assertEqual(final(result(137, False, "cancelled"), {"status": "done"})["status"],
                         "cancelled")
        self.assertEqual(final(result(137, False, "stopped"), {"status": "done"}),
                         {"status": "failed", "error": "dispatcher stopped"})
        self.assertEqual(final(result(1, False), {"status": "failed", "error": "prompt-missing"}),
                         {"status": "failed", "error": "prompt-missing"})

    def test_cancel_before_start_never_prepares_or_launches(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)
        self.cfg.cancel_dir.mkdir(parents=True)
        (self.cfg.cancel_dir / JOB).write_text("", encoding="utf-8")
        with mock.patch.object(self.dispatcher, "run_agent") as launch:
            result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        launch.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse((self.cfg.cancel_dir / JOB).exists())

    def test_low_storage_pauses_claiming(self):
        self.make_job()
        with mock.patch.object(self.dispatcher, "storage_state", return_value=(1, 1, True)):
            self.assertFalse(self.dispatcher.process_once(self.cfg, run_job=mock.Mock()))
        self.assertTrue((self.cfg.inbox / JOB).exists())
        beat = json.loads((self.cfg.live_root / "dispatcher.json").read_text(encoding="utf-8"))
        self.assertEqual(beat["state"], "storage-low")


@needs_fd_ops
class LiveFeedIntegrationTests(DispatcherTestBase):
    def test_a_job_produces_a_bounded_feed_ending_with_its_status(self):
        self.make_job()
        self.dispatcher.claim_job(self.cfg, JOB)

        def agent(_cfg, job_id):
            work = self.cfg.work / job_id
            (work / "out" / "status.json").write_text(json.dumps(
                {"job_id": job_id, "status": "done"}), encoding="utf-8")
            (work / "out" / "report.md").write_text("# Report", encoding="utf-8")
            return self.dispatcher.AgentResult(exit_code=0, timed_out=False)

        with mock.patch.object(self.dispatcher, "run_agent", side_effect=agent):
            self.dispatcher.run_claimed_job(self.cfg, JOB)
        feed = events(self.cfg.live_root / JOB / "events.jsonl")
        kinds = [event["type"] for event in feed]
        self.assertEqual(kinds[:4], ["job.claimed", "job.verified", "job.files", "agent.started"])
        self.assertEqual(kinds[-1], "job.finished")
        self.assertEqual(feed[-1]["status"], "done")
        self.assertEqual(feed[1]["files"], 2)
        self.assertEqual(feed[2]["total"], 2)
        self.assertEqual({entry["path"] for entry in feed[2]["entries"]},
                         {"target/tool", "question.md"})
        self.assertEqual(feed[2]["entries"][0]["sha256"].__len__(), 64)
        self.assertNotIn("PLANTED-SENTINEL", json.dumps(feed))

    def test_rejected_jobs_report_through_the_feed(self):
        job = self.make_job()
        (job / "target" / "tool").write_bytes(b"changed")
        self.dispatcher.claim_job(self.cfg, JOB)
        self.dispatcher.run_claimed_job(self.cfg, JOB)
        kinds = [event["type"] for event in events(self.cfg.live_root / JOB / "events.jsonl")]
        self.assertEqual(kinds, ["job.claimed", "job.rejected", "job.finished"])


@needs_root
class PolicyGateTests(DispatcherTestBase):
    def setUp(self):
        super().setUp()
        self.console = self.root / "console"
        (self.console / "approvals" / JOB).mkdir(parents=True)
        for path in (self.console, self.console / "approvals", self.console / "approvals" / JOB):
            os.chmod(path, 0o750)
        self.cfg = self.dispatcher.Config(
            root=self.root, agent_image=IMAGE, exec_image=EXEC_IMAGE, models_env=self.models,
            docker_prefix=("docker",), console_uid=10002)
        for path in (self.console, self.console / "approvals", self.console / "approvals" / JOB):
            os.chown(path, 10002, 10002)
        self.feed = mock.Mock()

    def write_policy(self, body):
        path = self.console / "exec-policy.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        os.chown(path, 10002, 10002)
        os.chmod(path, 0o640)

    def test_classification_follows_the_console_policy(self):
        gate = self.dispatcher.ExecPolicyGate(self.cfg, JOB, self.feed)
        self.assertEqual(gate.classify(["sh"]), ("allow", None, None))
        self.write_policy({"v": 1, "default_action": "ask", "rules": [
            {"name": "no shells", "match": "program", "value": "sh", "action": "block"}]})
        self.assertEqual(gate.classify(["/bin/sh", "-c", "x"]),
                         ("block", "no shells", "blocked by execution policy"))
        self.assertEqual(gate.classify(["/job/in/tool"])[0], "ask")
        (self.console / "exec-policy.json").write_text("{", encoding="utf-8")
        self.assertEqual(gate.classify(["/job/in/tool"])[0], "block")

    def test_approval_round_trip(self):
        gate = self.dispatcher.ExecPolicyGate(self.cfg, JOB, self.feed)
        decision_path = self.console / "approvals" / JOB / "run1.json"

        def approve():
            for _ in range(200):
                calls = [c for c in self.feed.emit.call_args_list
                         if c.args[0] == "exec.approval_requested"]
                if calls:
                    approval = calls[0].kwargs["approval_id"]
                    decision_path.write_text(json.dumps(
                        {"approval_id": approval, "decision": "approve"}), encoding="utf-8")
                    os.chown(decision_path, 10002, 10002)
                    os.chmod(decision_path, 0o640)
                    return
                threading.Event().wait(0.02)

        worker = threading.Thread(target=approve)
        worker.start()
        action, error = gate.await_approval("run1.json", ["/job/in/tool"], None, threading.Event())
        worker.join()
        self.assertEqual((action, error), ("allow", None))
        resolved = [c for c in self.feed.emit.call_args_list
                    if c.args[0] == "exec.approval_resolved"]
        self.assertEqual(resolved[0].kwargs["source"], "operator")


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.dispatcher = load("re-dispatcher")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def config(self, **overrides):
        body = {"agent_image": IMAGE, "exec_image": EXEC_IMAGE, "root": self.tmp.name}
        body.update(overrides)
        path = Path(self.tmp.name) / "dispatcher.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return self.dispatcher.load_config(path)

    def test_unsafe_settings_are_refused(self):
        for overrides in ({"network": "bridge"}, {"network": "host"}, {"agent_uid": 0},
                          {"agent_gid": 0}, {"root": "relative/path"}, {"root": "/srv/a:b"},
                          {"docker_prefix": "docker"}, {"docker_prefix": []},
                          {"reportgid": 5}, {"console_uid": 10001}, {"agent_cpus": 0},
                          {"min_free_mib": -1}, {"live": "yes"}):
            with self.subTest(overrides=overrides), self.assertRaises(SystemExit):
                self.config(**overrides)

    def test_non_object_config_is_a_clean_error(self):
        path = Path(self.tmp.name) / "dispatcher.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.dispatcher.load_config(path)

    def test_defaults_are_derived_from_the_root(self):
        cfg = self.config(console_uid=10002, min_free_mib=10)
        self.assertEqual(cfg.models_env, Path(self.tmp.name) / "secrets" / "models.env")
        self.assertEqual(cfg.min_free_bytes, 10 * 1024 ** 2)
        self.assertEqual(cfg.console_uid, 10002)


class OperatorCommandTests(DispatcherTestBase):
    @needs_root
    def test_init_layout_builds_the_exact_tree_and_is_idempotent(self):
        root = Path(self.tmp.name) / "fresh"
        root.mkdir()
        cfg = self.dispatcher.Config(root=root, agent_image=IMAGE, exec_image=EXEC_IMAGE,
                                     models_env=root / "secrets" / "models.env",
                                     console_uid=10002)
        self.dispatcher.init_layout(cfg)
        for relative, uid, gid, mode in self.dispatcher._layout(cfg):
            info = os.lstat(root / relative if relative else root)
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                             (uid, gid, mode), relative)
        self.assertEqual(self.dispatcher.init_layout(cfg), [])

    def test_init_layout_refuses_a_symlinked_directory(self):
        root = Path(self.tmp.name) / "fresh"
        root.mkdir()
        (root / "jobs").mkdir()
        os.symlink(self.tmp.name, root / "jobs" / "inbox")
        cfg = self.dispatcher.Config(root=root, agent_image=IMAGE, exec_image=EXEC_IMAGE,
                                     models_env=root / "secrets" / "models.env")
        with self.assertRaises(self.dispatcher.DispatcherError):
            self.dispatcher.init_layout(cfg)

    def test_prune_removes_only_finished_jobs_past_the_cutoff(self):
        for name, finished, age in (("job-20261101-old0", True, 40), ("job-20261101-new0", True, 1),
                                    ("job-20261101-run0", False, 40)):
            (self.cfg.work / name / "out").mkdir(parents=True)
            if finished:
                (self.cfg.reports / name).mkdir()
                result = self.cfg.reports / name / "result.json"
                result.write_text("{}", encoding="utf-8")
                os.utime(result, (1_000_000_000 - age * 86400,) * 2)
        removed = self.dispatcher.prune(self.cfg, 30, now=1_000_000_000)
        self.assertEqual(removed, ["job-20261101-old0"])
        self.assertFalse((self.cfg.work / "job-20261101-old0").exists())
        self.assertTrue((self.cfg.reports / "job-20261101-old0" / "result.json").exists())
        self.assertTrue((self.cfg.work / "job-20261101-run0").exists())
        self.assertTrue((self.cfg.work / "job-20261101-new0").exists())
        self.dispatcher.prune(self.cfg, 30, include_reports=True, now=1_000_000_000)
        self.assertFalse((self.cfg.reports / "job-20261101-old0").exists())

    def test_preflight_reports_each_check_with_a_fake_docker(self):
        fake = self.root / "fake-docker.py"
        fake.write_text(textwrap.dedent("""
            import json, sys
            args = sys.argv[1:]
            if args[0] == "version":
                print("29.10.0")
            elif args[0] == "info":
                print(json.dumps({"runc": {}, "runsc": {"path": "/usr/bin/runsc"}}))
            elif args[0] == "image":
                print("sha256:" + "a" * 64)
            elif args[0] == "network":
                print(json.dumps({"Internal": True,
                    "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
                    "IPAM": {"Config": [{"Subnet": "172.30.97.0/24"}]}}))
            elif args[0] == "ps":
                print("abc123")
        """), encoding="utf-8")
        cfg = self.dispatcher.Config(root=self.root, agent_image=IMAGE, exec_image=EXEC_IMAGE,
                                     models_env=self.models,
                                     docker_prefix=(sys.executable, str(fake)))
        results = self.dispatcher.preflight(cfg)
        passes = [message for level, message in results if level == "PASS"]
        self.assertIn("docker 29.10.0", passes)
        self.assertIn("runsc runtime registered", passes)
        self.assertIn("re-egress is running", passes)
        self.assertTrue(any("is internal with an isolated gateway" in m for m in passes))
        self.assertTrue(results)

    def test_cleanup_only_mode_runs_only_container_cleanup(self):
        with mock.patch.object(self.dispatcher, "load_config", return_value=self.cfg), \
             mock.patch.object(self.dispatcher, "cleanup_containers") as cleanup, \
             mock.patch.object(self.dispatcher, "recover_interrupted") as recover, \
             mock.patch.object(self.dispatcher, "process_once") as scan:
            self.assertEqual(self.dispatcher.main(["--cleanup-only"]), 0)
        cleanup.assert_called_once_with(self.cfg)
        recover.assert_not_called()
        scan.assert_not_called()

    def test_sigterm_requests_a_graceful_stop(self):
        previous = self.dispatcher._install_signal_handlers()
        try:
            os.kill(os.getpid(), __import__("signal").SIGTERM)
            self.assertTrue(self.dispatcher.STOP.wait(2))
        finally:
            self.dispatcher._restore_signal_handlers(previous)

    def test_cleanup_tolerates_a_failed_rm_when_nothing_remains(self):
        calls = []

        def docker(argv, **_kwargs):
            calls.append(argv[1])
            if argv[1] == "ps":
                out = ("ab" * 32) if calls.count("ps") == 1 else ""
                return mock.Mock(returncode=0, stdout=out)
            return mock.Mock(returncode=1, stdout="", stderr=b"Conflict: removal in progress")

        with mock.patch.object(self.dispatcher.subprocess, "run", side_effect=docker):
            self.dispatcher.cleanup_containers(self.cfg)
        self.assertEqual(calls, ["ps", "rm", "ps"])

    def test_cleanup_fails_closed_when_containers_remain(self):
        def docker(argv, **_kwargs):
            return mock.Mock(returncode=0, stdout="ab" * 32)

        with mock.patch.object(self.dispatcher.subprocess, "run", side_effect=docker), \
             mock.patch.object(self.dispatcher.executor, "REMOVAL_WAIT_S", 0.05), \
             mock.patch.object(self.dispatcher.time, "sleep"):
            with self.assertRaises(self.dispatcher.executor.ContainmentError):
                self.dispatcher.cleanup_containers(self.cfg)


class ExecutorHardeningTests(unittest.TestCase):
    def setUp(self):
        self.executor = load("executor")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("work", "requests", "results", "runs"):
            (self.root / name).mkdir()
        os.chmod(self.root / "runs", 0o700)
        (self.root / "work" / JOB / "target").mkdir(parents=True)
        (self.root / "work" / JOB / "out").mkdir()
        self.cfg = self.executor.Config(
            image=IMAGE, work_root=self.root / "work", requests_dir=self.root / "requests",
            results_dir=self.root / "results", active_job_id=JOB, runs_dir=self.root / "runs",
            agent_uid=os.getuid() if hasattr(os, "getuid") else 10001,
            agent_gid=os.getgid() if hasattr(os, "getgid") else 10001)

    def request(self, **overrides):
        body = {"job_id": JOB, "argv": ["echo", "hi"]}
        body.update(overrides)
        return body

    def test_argv_limits_match_the_schema_and_reject_lone_surrogates(self):
        validate = self.executor.validate_request
        validate(self.request(argv=["x" * self.executor.MAX_ARG_CHARS]))
        for argv in (["x" * (self.executor.MAX_ARG_CHARS + 1)], ["bad\ud83d"],
                     ["y" * 8000] * 40):
            with self.subTest(size=len(argv)), self.assertRaises(self.executor.RequestError):
                validate(self.request(argv=argv))
        schema = json.loads((Path(__file__).resolve().parents[1] / "container" /
                             "exec-request.schema.json").read_text(encoding="utf-8"))
        props = schema["properties"]
        self.assertEqual(props["argv"]["maxItems"], self.executor.MAX_ARGV_ITEMS)
        self.assertEqual(props["argv"]["items"]["maxLength"], self.executor.MAX_ARG_CHARS)
        self.assertEqual(props["timeout_s"]["maximum"], self.executor.TIMEOUT_MAX)
        self.assertEqual(props["memory_mb"]["maximum"], self.executor.MEMORY_MAX)
        self.assertEqual(set(schema["$defs"]["result"]["properties"]["status"]["enum"]),
                         {"ok", "failed", "timeout", "cancelled", "rejected", "error"})

    def test_container_names_are_derived_only_from_known_cidfiles(self):
        name = self.executor.container_name
        self.assertEqual(name(self.root / "runs" / ("a" * 32) / "cid"), "re-lab-exec-" + "a" * 32)
        self.assertEqual(name(self.root / "work" / JOB / "agent.cid"), f"re-lab-agent-{JOB}")
        self.assertIsNone(name(self.root / "runs" / "missing-cid"))
        self.assertIsNone(name(self.root / "runs" / "../etc" / "cid"))

    def test_create_window_timeout_is_contained_by_name_without_a_cidfile(self):
        run_id = "c" * 32
        calls = []

        def docker(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "inspect":
                return mock.Mock(returncode=1, stdout=b"", stderr=b"Error: No such object: x")
            return mock.Mock(returncode=1, stdout=b"", stderr=b"")

        with mock.patch.object(self.executor.subprocess, "run", side_effect=docker):
            self.executor.contain_container(("docker",), self.root / "runs" / run_id / "cid")
        self.assertEqual([call[1] for call in calls], ["kill", "rm", "inspect"])
        self.assertEqual(calls[0][-1], "re-lab-exec-" + run_id)

    def test_a_container_still_being_removed_is_awaited(self):
        run = self.root / "runs" / ("d" * 32)
        run.mkdir()
        (run / "cid").write_text("ab" * 32, encoding="ascii")
        states = iter([b"removing", b"removing"])

        def docker(argv, **_kwargs):
            if argv[1] == "inspect":
                state = next(states, None)
                if state is None:
                    return mock.Mock(returncode=1, stdout=b"", stderr=b"Error: No such container")
                return mock.Mock(returncode=0, stdout=state, stderr=b"")
            return mock.Mock(returncode=1, stdout=b"", stderr=b"")

        with mock.patch.object(self.executor.subprocess, "run", side_effect=docker), \
             mock.patch.object(self.executor.time, "sleep"):
            self.executor.contain_container(("docker",), run / "cid")

    def test_a_running_container_after_rm_is_a_containment_failure(self):
        run = self.root / "runs" / ("e" * 32)
        run.mkdir()
        (run / "cid").write_text("ab" * 32, encoding="ascii")
        running = mock.Mock(returncode=0, stdout=b"running", stderr=b"")
        with mock.patch.object(self.executor.subprocess, "run", return_value=running):
            with self.assertRaises(self.executor.ContainmentError):
                self.executor.contain_container(("docker",), run / "cid")

    def test_a_clean_client_exit_is_followed_by_an_absence_proof(self):
        request = self.executor.validate_request(self.request())
        run = mock.Mock(run_id="f" * 32, path=self.root / "runs" / ("f" * 32))
        proc = mock.Mock()
        proc.stdout = __import__("io").BytesIO()
        proc.stderr = __import__("io").BytesIO()
        proc.wait.return_value = 0
        with mock.patch.object(self.executor.subprocess, "Popen", return_value=proc), \
             mock.patch.object(self.executor, "contain_container") as contain:
            result = self.executor._launch(self.cfg, request, run)
        self.assertEqual(result["status"], "ok")
        contain.assert_called_once_with(self.cfg.docker_prefix, run.path / "cid")

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires GNU rm")
    def test_deep_output_chains_are_removed(self):
        top = self.root / "runs" / ("1" * 32)
        top.mkdir()
        fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(1500):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.close(fd)
        self.executor._rmtree_force(top)
        self.assertFalse(top.exists())

    def test_request_floods_are_refused_without_listing_everything(self):
        for index in range(30):
            (self.root / "requests" / f".noise-{index}").write_text("x", encoding="utf-8")
        with mock.patch.object(self.executor, "MAX_REQUEST_SCAN", 20), \
             mock.patch.object(self.executor.os, "listdir",
                               side_effect=AssertionError("listed the whole directory")):
            with self.assertRaises(self.executor.RequestError):
                self.executor.process_pending(self.cfg)

    def test_a_directory_named_like_a_request_is_rejected_and_moved_aside(self):
        path = self.root / "requests" / "run1.json"
        path.mkdir()
        (path / "inner").write_text("x", encoding="utf-8")
        result = self.executor.process_request_file(self.cfg, path)
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(path.exists())
        self.assertTrue((self.root / "results" / "run1.json").is_file())

    def test_policy_blocks_and_reports_through_the_observer(self):
        observed = []
        policy = mock.Mock()
        policy.classify.return_value = ("block", "no shells", "blocked by execution policy")
        cfg = self.executor.Config(**{**self.cfg.__dict__, "policy": policy,
                                      "observer": lambda kind, **f: observed.append((kind, f))})
        path = self.root / "requests" / "run2.json"
        path.write_text(json.dumps(self.request(argv=["sh", "-c", "id"])), encoding="utf-8")
        with mock.patch.object(self.executor, "execute") as execute:
            result = self.executor.process_request_file(cfg, path)
        execute.assert_not_called()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "blocked by execution policy")
        self.assertEqual([kind for kind, _ in observed], ["exec.request", "exec.result"])
        self.assertEqual(observed[0][1]["policy_action"], "block")

    def test_ask_waits_for_the_operator_through_the_policy(self):
        policy = mock.Mock()
        policy.classify.return_value = ("ask", "review", None)
        policy.await_approval.return_value = ("block", "denied by operator")
        cfg = self.executor.Config(**{**self.cfg.__dict__, "policy": policy})
        path = self.root / "requests" / "run3.json"
        path.write_text(json.dumps(self.request()), encoding="utf-8")
        result = self.executor.process_request_file(cfg, path)
        self.assertEqual((result["status"], result["error"]), ("rejected", "denied by operator"))
        policy.await_approval.assert_called_once()



class InfrastructureHardeningTests(unittest.TestCase):
    CONTAINER = Path(__file__).resolve().parents[1] / "container"

    def egress_lines(self):
        lines = []
        for raw in (self.CONTAINER / "egress-allowlist").read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                lines.append(line)
        return lines

    def test_unlisted_names_are_refused_before_any_dns_lookup(self):
        lines = self.egress_lines()
        access = [line for line in lines if line.startswith("http_access ")]
        self.assertEqual(access[0], "http_access deny !agent_net")
        private = access.index("http_access deny private_dst")
        self.assertLess(access.index("http_access deny !model_endpoints"), private)
        # Every rule before the dst check must be decidable without resolving the name.
        no_dns = {"agent_net", "connect_method", "tls_port", "literal_ip", "model_endpoints"}
        for rule in access[:private]:
            names = {token.lstrip("!") for token in rule.split()[2:]}
            self.assertLessEqual(names, no_dns, rule)
        self.assertEqual(access[-1], "http_access deny all")

    def test_special_purpose_destinations_are_denied(self):
        entries = set()
        for line in self.egress_lines():
            if line.startswith("acl private_dst dst "):
                entries.update(line.split()[3:])
        required = {"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
                    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
                    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4",
                    "240.0.0.0/4", "::1", "64:ff9b::/96", "2002::/16", "fc00::/7", "fe80::/10",
                    "ff00::/8"}
        self.assertLessEqual(required, entries)
        self.assertNotIn("::ffff:0:0/96", entries)  # would match every IPv4 destination

    def test_proxy_hides_its_version_and_serves_only_the_agent_network(self):
        lines = self.egress_lines()
        self.assertIn("httpd_suppress_version_string on", lines)
        compose = yaml.safe_load((self.CONTAINER / "compose.yaml").read_text(encoding="utf-8"))
        subnet = compose["networks"]["re-internal"]["ipam"]["config"][0]["subnet"]
        self.assertIn(f"acl agent_net src {subnet}", lines)

    def test_egress_logs_are_bounded(self):
        compose = yaml.safe_load((self.CONTAINER / "compose.yaml").read_text(encoding="utf-8"))
        logging = compose["services"]["re-egress"]["logging"]
        self.assertEqual(logging["driver"], "local")
        self.assertIn("max-size", logging["options"])
        self.assertIn("max-file", logging["options"])

    @unittest.skipUnless(shutil.which("squid"), "squid is not installed")
    def test_squid_accepts_the_configuration(self):
        done = subprocess.run(["squid", "-k", "parse", "-f", str(self.CONTAINER / "egress-allowlist")],
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])

    def test_dispatcher_unit_contains_jobs_on_stop_and_gates_start(self):
        unit = (self.CONTAINER / "re-dispatcher.service").read_text(encoding="utf-8")
        sections = unit.split("[Service]", 1)
        self.assertIn("StartLimitBurst=", sections[0])
        self.assertIn("StartLimitIntervalSec=", sections[0])
        service = sections[1]
        for directive in ("KillMode=mixed", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
                          "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM",
                          "MemoryDenyWriteExecute=yes", "ProtectKernelModules=yes"):
            self.assertIn(directive, service)
        self.assertRegex(service, r"(?m)^ExecStopPost=.*re-dispatcher\.py .*--cleanup-only$")
        self.assertRegex(service, r"(?m)^ExecStartPre=.*re-dispatcher\.py .*--check$")
        self.assertNotRegex(service, r"(?m)^ReadOnlyPaths=.*docker\.sock")



class DeepJsonTests(DispatcherTestBase):
    """Nesting deep enough to raise RecursionError on every supported Python (1,100 levels does not)."""

    @needs_fd_ops
    def test_recursion_bomb_manifest_is_a_fixed_rejection(self):
        job = self.make_job()
        (job / "job.json").write_bytes(b"[" * 100_000 + b"]" * 100_000)
        self.dispatcher.claim_job(self.cfg, JOB)
        result = self.dispatcher.run_claimed_job(self.cfg, JOB)
        self.assertEqual((result["status"], result["error"]),
                         ("rejected-job", "job.json is missing or invalid"))

    def test_recursion_bomb_session_lines_do_not_break_cost_or_feed(self):
        bomb = b"[" * 200_000 + b"]" * 200_000 + b"\n"
        valid = (json.dumps({"message": {"role": "assistant",
                                         "usage": {"cost": {"total": 0.5}}}}) + "\n").encode()
        self.assertEqual(self.dispatcher._session_cost_checked(bomb + valid), (0.5, True))

    def test_deep_exec_request_is_rejected_on_every_python(self):
        executor = load("executor")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("work", "requests", "results", "runs"):
                (root / name).mkdir()
            os.chmod(root / "runs", 0o700)
            cfg = executor.Config(image=IMAGE, work_root=root / "work",
                                  requests_dir=root / "requests", results_dir=root / "results",
                                  active_job_id=JOB, runs_dir=root / "runs")
            path = root / "requests" / "deep.json"
            depth = executor.MAX_REQUEST_BYTES // 2
            path.write_bytes(b"[" * depth + b"]" * depth)
            result = executor.process_request_file(cfg, path)
        self.assertEqual(result["status"], "rejected")
        self.assertIn(result["error"], {"request is not valid JSON", "request must be a JSON object"})


if __name__ == "__main__":
    unittest.main()
