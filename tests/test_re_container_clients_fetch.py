"""re_fetch: readiness, waiting, all-or-nothing copies, and safe printing of untrusted results.

`subprocess.run` is replaced by a fake host: ssh answers the readiness check and scp copies from
a local directory standing in for /srv/re-lab/reports. Nothing leaves the machine.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

CONTAINER = Path(__file__).resolve().parents[1] / "container"
JOB = "job-20261118-120000-0123456789abcdef"
REMOTE = f"lab-host:/srv/re-lab/reports/{JOB}"


def load_fetch():
    spec = importlib.util.spec_from_file_location("re_container_re_fetch", CONTAINER / "re_fetch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTime:
    """Stands in for re_fetch's `time` module: sleeping advances the monotonic clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeHost:
    """ssh `test -f` answers from `ready_codes`; scp copies from a local reports directory."""

    def __init__(self, reports_root, ready_codes=(0,), scp_failure=None):
        self.reports_root = Path(reports_root)
        self.ready_codes = list(ready_codes)
        self.scp_failure = scp_failure
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[0] == "ssh":
            code = self.ready_codes.pop(0) if len(self.ready_codes) > 1 else self.ready_codes[0]
            return subprocess.CompletedProcess(argv, code)
        self.assertion_scp(argv)
        source = self.reports_root / argv[-2].split(":", 1)[1].removeprefix("/srv/re-lab/reports/")
        destination = Path(argv[-1])
        if "-r" in argv:
            shutil.copytree(source, destination / source.name)
        else:
            shutil.copyfile(source, destination)
        if self.scp_failure is not None:
            raise subprocess.CalledProcessError(self.scp_failure, argv)
        return subprocess.CompletedProcess(argv, 0)

    @staticmethod
    def assertion_scp(argv):
        if argv[0] != "scp" or argv[-3] != "--" or not os.path.isabs(argv[-1]):
            raise AssertionError(f"unexpected scp argv {argv!r}")

    def scp_calls(self):
        return [argv for argv, _ in self.calls if argv[0] == "scp"]

    def ssh_calls(self):
        return [argv for argv, _ in self.calls if argv[0] == "ssh"]


class FetchTestBase(unittest.TestCase):
    def setUp(self):
        self.fetch = load_fetch()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.reports = self.root / "remote-reports"
        self.dest = self.root / "dest"
        self.publish({"status": "done", "cost_usd": 0.35, "exit_code": 0})

    def publish(self, result, *, raw=None):
        report = self.reports / JOB
        report.mkdir(parents=True, exist_ok=True)
        (report / "report.md").write_text("# Report: test\n", encoding="utf-8")
        (report / "evidence").mkdir(exist_ok=True)
        (report / "evidence" / "E1-strings.txt").write_text("strings\n", encoding="utf-8")
        text = raw if raw is not None else json.dumps(result)
        (report / "result.json").write_text(text, encoding="utf-8")

    def fetch_main(self, host, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.fetch.subprocess, "run", side_effect=host), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.fetch.main([JOB, "--host", "lab-host", *args])
        return code, out.getvalue(), err.getvalue()

    def dest_entries(self):
        return sorted(os.listdir(self.dest)) if self.dest.exists() else []


class ReadinessTests(FetchTestBase):
    def test_ready_report_is_checked_then_copied(self):
        host = FakeHost(self.reports)
        code, out, _ = self.fetch_main(host, "--dest", str(self.dest))
        self.assertEqual(code, 0)
        argv, kwargs = host.calls[0]
        self.assertEqual(argv, ["ssh", "-n", "lab-host", "test", "-f",
                                f"/srv/re-lab/reports/{JOB}/result.json"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        scp = host.scp_calls()
        self.assertEqual(len(scp), 1)
        self.assertEqual(scp[0][:4], ["scp", "-r", "-q", "--"])
        self.assertEqual(scp[0][-2], REMOTE)
        self.assertIs(host.calls[1][1]["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.dest_entries(), [JOB])
        self.assertEqual((self.dest / JOB / "evidence" / "E1-strings.txt").read_text(), "strings\n")
        self.assertEqual(out.splitlines(), [
            f"{JOB}: status=done cost_usd=0.35 exit_code=0",
            f"report: {self.dest / JOB / 'report.md'}",
        ])

    def test_missing_result_exits_3_without_copying(self):
        host = FakeHost(self.reports, ready_codes=(1,))
        code, out, err = self.fetch_main(host, "--dest", str(self.dest))
        self.assertEqual(code, 3)
        self.assertIn("not ready (queued, running, or unknown id)", err)
        self.assertEqual(out, "")
        self.assertEqual(host.scp_calls(), [])
        self.assertEqual(self.dest_entries(), [])

    def test_transport_failure_is_not_reported_as_not_ready(self):
        host = FakeHost(self.reports, ready_codes=(255,))
        with self.assertRaisesRegex(SystemExit, "ssh failed with exit 255"):
            self.fetch_main(host, "--dest", str(self.dest))
        self.assertEqual(host.scp_calls(), [])

    def test_status_prints_only_the_status_line_and_copies_no_report(self):
        host = FakeHost(self.reports)
        code, out, _ = self.fetch_main(host, "--status", "--dest", str(self.dest))
        self.assertEqual(code, 0)
        self.assertEqual(out, f"{JOB}: status=done cost_usd=0.35 exit_code=0\n")
        scp = host.scp_calls()
        self.assertEqual(len(scp), 1)
        self.assertEqual(scp[0][:3], ["scp", "-q", "--"])
        self.assertEqual(scp[0][-2], f"{REMOTE}/result.json")
        self.assertFalse(self.dest.exists(), "--status writes nothing to --dest")

    def test_status_works_even_when_the_report_was_already_fetched(self):
        (self.dest / JOB).mkdir(parents=True)
        code, out, _ = self.fetch_main(FakeHost(self.reports), "--status", "--dest", str(self.dest))
        self.assertEqual(code, 0)
        self.assertIn("status=done", out)


class WaitTests(FetchTestBase):
    def run_waiting(self, host, *args):
        clock = FakeTime()
        with mock.patch.object(self.fetch, "time", clock):
            result = self.fetch_main(host, "--dest", str(self.dest), "--wait", *args)
        return clock, result

    def test_wait_polls_until_the_result_appears(self):
        host = FakeHost(self.reports, ready_codes=(1, 1, 0))
        clock, (code, out, _) = self.run_waiting(host)
        self.assertEqual(code, 0)
        self.assertEqual(clock.sleeps, [15, 15])
        self.assertEqual(len(host.ssh_calls()), 3)
        self.assertIn("status=done", out)

    def test_wait_gives_up_after_the_timeout(self):
        host = FakeHost(self.reports, ready_codes=(1,))
        clock, (code, _, err) = self.run_waiting(host, "--timeout", "40", "--interval", "15")
        self.assertEqual(code, 3)
        self.assertEqual(clock.sleeps, [15, 15, 10])
        self.assertEqual([kwargs.get("timeout") for argv, kwargs in host.calls if argv[0] == "ssh"],
                         [40, 25, 10])
        self.assertIn("wait deadline expired", err)
        self.assertEqual(host.scp_calls(), [])

    def test_ready_probe_observed_late_reports_expiry_not_a_missing_report(self):
        host, clock = FakeHost(self.reports), FakeTime()

        def delayed_reply(argv, **kwargs):
            clock.now += 2
            return host(argv, **kwargs)

        with mock.patch.object(self.fetch, "time", clock):
            code, _, err = self.fetch_main(delayed_reply, "--wait", "--timeout", "1")
        self.assertEqual(code, 3)
        self.assertIn("wait deadline expired", err)
        self.assertNotIn("not ready", err)
        self.assertEqual(host.scp_calls(), [])

    def test_stalled_ssh_is_bounded_and_not_reported_as_a_missing_result(self):
        def stalled(argv, **kwargs):
            self.assertEqual(argv[0], "ssh")
            self.assertEqual(kwargs.get("timeout"), 4)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with self.assertRaisesRegex(SystemExit, "readiness check timed out"):
            self.run_waiting(stalled, "--timeout", "4")
        self.assertEqual(self.dest_entries(), [])

    def test_zero_wait_budget_starts_no_remote_process(self):
        host = FakeHost(self.reports)
        _, (code, _, _) = self.run_waiting(host, "--timeout", "0")
        self.assertEqual(code, 3)
        self.assertEqual(host.calls, [])

    def test_default_wait_budget_is_7800_seconds_in_15_second_steps(self):
        host = FakeHost(self.reports, ready_codes=(1,))
        clock, (code, _, _) = self.run_waiting(host)
        self.assertEqual(code, 3)
        self.assertEqual(sum(clock.sleeps), 7800)
        self.assertTrue(all(0 < pause <= 15 for pause in clock.sleeps))

    def test_wait_options_are_validated(self):
        for args in (["--timeout", "-1"], ["--timeout", "nan"], ["--interval", "0"],
                     ["--interval", "inf"]):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit):
                    self.fetch_main(FakeHost(self.reports), "--wait", *args)


class AllOrNothingCopyTests(FetchTestBase):
    def test_failed_copy_leaves_nothing_and_a_retry_succeeds(self):
        failing = FakeHost(self.reports, scp_failure=1)
        with self.assertRaisesRegex(SystemExit, "scp failed with exit 1"):
            self.fetch_main(failing, "--dest", str(self.dest))
        self.assertEqual(self.dest_entries(), [], "no partial copy or temporary directory")
        code, _, _ = self.fetch_main(FakeHost(self.reports), "--dest", str(self.dest))
        self.assertEqual(code, 0)
        self.assertEqual(self.dest_entries(), [JOB])

    def test_unreadable_result_leaves_nothing_behind(self):
        for raw in ("not json", "[" * 5000, ""):
            with self.subTest(raw=raw[:10]):
                self.publish(None, raw=raw)
                with self.assertRaisesRegex(SystemExit, "result.json is unreadable"):
                    self.fetch_main(FakeHost(self.reports), "--dest", str(self.dest))
                self.assertEqual(self.dest_entries(), [])

    def test_copy_is_made_in_a_private_directory_inside_dest(self):
        host = FakeHost(self.reports)
        self.fetch_main(host, "--dest", str(self.dest))
        staging = Path(host.scp_calls()[0][-1])
        self.assertEqual(staging.parent, self.dest)
        self.assertTrue(staging.name.startswith(".re-fetch-"))
        self.assertFalse(staging.exists(), "the temporary directory is removed after the rename")

    def test_existing_destination_is_refused_before_contacting_the_host(self):
        (self.dest / JOB).mkdir(parents=True)
        host = FakeHost(self.reports)
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.fetch_main(host, "--dest", str(self.dest))
        self.assertEqual(host.calls, [])


class ValidationTests(FetchTestBase):
    def test_identifiers_must_match_in_full(self):
        for argv in ([JOB + "\n", "--host", "lab-host"], [JOB, "--host", "lab-host\n"],
                     [JOB, "--host=-oProxyCommand=x"], ["short", "--host", "lab-host"]):
            with self.subTest(argv=argv):
                with mock.patch.object(self.fetch.subprocess, "run") as run:
                    with self.assertRaises(SystemExit):
                        self.fetch.main(argv)
                run.assert_not_called()

    def test_dest_starting_with_a_dash_is_refused(self):
        host = FakeHost(self.reports)
        with self.assertRaisesRegex(SystemExit, "--dest"):
            self.fetch_main(host, "--dest=-oProxyCommand=x")
        self.assertEqual(host.calls, [])

    def test_relative_dest_with_a_colon_is_passed_to_scp_as_a_local_absolute_path(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        host = FakeHost(self.reports)
        code, _, _ = self.fetch_main(host, "--dest", "reports:2026")
        self.assertEqual(code, 0)
        local = host.scp_calls()[0][-1]
        self.assertTrue(os.path.isabs(local))
        self.assertTrue((self.root / "reports:2026" / JOB / "result.json").is_file())


class SafePrintingTests(FetchTestBase):
    PAYLOADS = ("\x1b[2J\x1b]0;owned\x07", "0\x1b[2K\rjob-x: status=done", "line1\nline2",
                "\x1b]52;c;Zm9yZ2Vk\x07", "‮evil", "\x9b31m")

    def assert_terminal_safe(self, text):
        for char in text:
            if char == "\n":
                continue
            self.assertNotIn(unicodedata.category(char), ("Cc", "Cf"), repr(char))

    def test_control_sequences_in_every_field_are_escaped(self):
        for payload in self.PAYLOADS:
            with self.subTest(payload=repr(payload)):
                shutil.rmtree(self.dest, ignore_errors=True)
                self.publish({"status": payload, "cost_usd": payload, "exit_code": payload,
                              "error": payload, "model": payload, "reason": payload,
                              "started": payload, "ended": payload})
                code, out, _ = self.fetch_main(FakeHost(self.reports), "--dest", str(self.dest))
                self.assertEqual(code, 0)
                self.assert_terminal_safe(out)
                lines = out.splitlines()
                self.assertEqual(len(lines), 2, lines)
                self.assertTrue(lines[0].startswith(f"{JOB}: status="))
                self.assertIn(f"exit_code={ascii(payload)[:77]}", lines[0])

    def test_status_mode_is_escaped_too(self):
        self.publish({"status": "done\x1b[2J", "cost_usd": 1, "exit_code": None})
        code, out, _ = self.fetch_main(FakeHost(self.reports), "--status")
        self.assertEqual(code, 0)
        self.assert_terminal_safe(out)
        self.assertEqual(out, f"{JOB}: status='done\\x1b[2J' cost_usd=1 exit_code=None\n")

    def test_values_are_validated_by_type(self):
        status_line = self.fetch.status_line
        self.assertEqual(status_line(JOB, {"status": "needs-vm-lane", "cost_usd": 1.5,
                                           "exit_code": None, "error": "omp-exit-1"}),
                         f"{JOB}: status=needs-vm-lane cost_usd=1.5 exit_code=None "
                         "error='omp-exit-1'")
        line = status_line(JOB, {"status": "DONE", "cost_usd": float("nan"), "exit_code": True})
        self.assertEqual(line, f"{JOB}: status='DONE' cost_usd=nan exit_code=True")
        line = status_line(JOB, {"status": ["done"], "cost_usd": "1", "exit_code": 3.0})
        self.assertEqual(line, f"{JOB}: status=['done'] cost_usd='1' exit_code=3.0")
        self.assertEqual(status_line(JOB, ["not", "an", "object"]),
                         f"{JOB}: result.json is not a JSON object")
        self.assertLessEqual(len(status_line(JOB, {"status": "x" * 5000})), 200)

    def test_valid_cost_infinity_and_bool_are_not_trusted(self):
        shown = self.fetch.status_line(JOB, {"status": "done", "cost_usd": True, "exit_code": 0})
        self.assertIn("cost_usd=True", shown)
        self.assertFalse(self.fetch._is_finite_number(True))
        self.assertFalse(self.fetch._is_finite_number(float("inf")))
        self.assertTrue(self.fetch._is_finite_number(0))


if __name__ == "__main__":
    unittest.main()
