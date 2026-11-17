"""re_submit / re_fetch: remote layout and permissions, with every remote command captured.

Nothing is sent anywhere: `run` (submit) and `subprocess.run` (fetch) are replaced by recorders.
"""
import importlib.util
import json
import os
import stat
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CONTAINER = Path(__file__).resolve().parents[1] / "container"


def load(name):
    spec = importlib.util.spec_from_file_location(f"re_container_{name}", CONTAINER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def granted_bits(spec):
    """Parse a symbolic chmod spec (e.g. 'u+rwX,g+rwX,o-rwx') into {class: set of permissions added}."""
    added = {"u": set(), "g": set(), "o": set()}
    for clause in spec.split(","):
        match = re.fullmatch(r"([ugoa]*)([-+=])([rwxXst]*)", clause)
        if not match:
            raise AssertionError(f"unparseable chmod clause {clause!r}")
        who, op, perms = match.groups()
        classes = set("ugo") if who in ("", "a") or "a" in who else set(who)
        if op in "+=":
            for cls in classes:
                added[cls] |= set(perms)
    return added


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.submit = load("re_submit")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "sample.bin"
        self.target.write_bytes(b"\x7fELF")
        self.calls = []
        self.host_env = mock.patch.dict(
            self.submit.os.environ, {"RE_LAB_HOST": "lab-host"})
        self.host_env.start()
        self.addCleanup(self.host_env.stop)

    def submitted(self):
        with mock.patch.object(self.submit, "run", side_effect=self.calls.append):
            self.assertEqual(self.submit.main([str(self.target), "What does it do?"]), 0)
        return self.calls

    def test_submit_requires_an_explicit_host_or_environment_variable(self):
        with mock.patch.dict(self.submit.os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "--host or RE_LAB_HOST"):
                self.submit.main([str(self.target), "What does it do?"])

    def test_the_job_lands_under_the_single_jobs_mount_inbox(self):
        calls = self.submitted()
        remote_paths = [arg for argv in calls for arg in argv if "/srv/re-lab" in arg]
        self.assertTrue(remote_paths)
        for path in remote_paths:
            self.assertIn("/srv/re-lab/jobs/inbox/", path)
        self.assertEqual([c for c in remote_paths if re.search(r"/srv/re-lab/inbox", c)], [])

    def test_the_final_rename_stays_inside_the_inbox_directory(self):
        calls = self.submitted()
        move = next(c for c in calls if "mv" in c)
        self.assertTrue(move[-2].startswith("/srv/re-lab/jobs/inbox/.incoming-"))
        self.assertTrue(move[-1].startswith("/srv/re-lab/jobs/inbox/job-"))

    def test_the_remote_chmod_grants_group_access_and_never_write_to_others(self):
        calls = self.submitted()
        chmod = next(c for c in calls if "chmod" in c)
        spec = chmod[chmod.index("chmod") + 2]
        added = granted_bits(spec)
        self.assertGreaterEqual(added["g"], {"r", "w"})
        self.assertNotIn("w", added["o"], spec)
        self.assertNotIn("go+w", spec)
        self.assertNotIn("a+w", spec)
        self.assertNotIn("o+", spec)

    @unittest.skipUnless(os.name == "posix", "POSIX execute modes are unavailable on Windows")
    def test_stage_preserves_execute_bits_and_override_sets_0755(self):
        os.chmod(self.target, 0o751)
        staged = Path(self.tmp.name) / "staged-preserved"
        staged.mkdir()
        self.submit.stage_job(
            staged, "job-20261116-0001", self.target, "Question", None, None)
        copied = staged / "target" / self.target.name
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode) & 0o111, 0o111)

        os.chmod(self.target, 0o644)
        overridden = Path(self.tmp.name) / "staged-overridden"
        overridden.mkdir()
        self.submit.stage_job(
            overridden, "job-20261116-0002", self.target, "Question", None, None,
            executables=[self.target.name])
        self.assertEqual(stat.S_IMODE(
            (overridden / "target" / self.target.name).stat().st_mode), 0o755)

    def test_executable_override_must_name_a_collected_file(self):
        staged = Path(self.tmp.name) / "staged-invalid"
        staged.mkdir()
        with self.assertRaises(SystemExit):
            self.submit.stage_job(
                staged, "job-20261116-0001", self.target, "Question", None, None,
                executables=["missing"])

    def test_scp_preserves_staged_modes(self):
        scp = next(call for call in self.submitted() if call[0] == "scp")
        self.assertIn("-p", scp)

    def test_executable_override_is_applied_on_the_remote_posix_host(self):
        with mock.patch.object(self.submit, "run", side_effect=self.calls.append):
            self.assertEqual(self.submit.main([
                str(self.target), "What does it do?",
                "--executable", self.target.name,
            ]), 0)
        chmod = next(call for call in self.calls if "0755" in call)
        mode = chmod.index("0755")
        self.assertEqual(chmod[mode + 1], "--")
        self.assertTrue(chmod[mode + 2].endswith("/target/" + self.target.name))

    def test_repeatable_executable_paths_are_applied_once_each(self):
        source = Path(self.tmp.name) / "source"
        (source / "nested").mkdir(parents=True)
        (source / "one").write_bytes(b"one")
        (source / "nested" / "two").write_bytes(b"two")
        with mock.patch.object(self.submit, "run", side_effect=self.calls.append):
            self.assertEqual(self.submit.main([
                str(source), "What does it do?",
                "--executable", "one",
                "--executable", "nested/two",
                "--executable", "one",
            ]), 0)
        chmod = next(call for call in self.calls if "0755" in call)
        paths = chmod[chmod.index("--") + 1:]
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].endswith("/target/one"))
        self.assertTrue(paths[1].endswith("/target/nested/two"))

    @unittest.skipUnless(os.name == "posix", "requires POSIX file names")
    def test_final_newline_in_file_name_is_rejected(self):
        source = Path(self.tmp.name) / "source-newline"
        source.mkdir()
        (source / "payload\n").write_bytes(b"payload")
        with self.assertRaises(SystemExit):
            self.submit.collect_target(source)



    def test_the_parser_rejects_what_it_cannot_read(self):
        self.assertEqual(granted_bits("go+w")["o"], {"w"})
        with self.assertRaises(AssertionError):
            granted_bits("nonsense")


class FetchTests(unittest.TestCase):
    def test_reports_are_fetched_from_the_reports_directory_outside_the_jobs_mount(self):
        fetch = load("re_fetch")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            seen = []

            def fake_scp(argv, **kwargs):
                seen.append(argv)
                job = dest / "job-20261116-0001"
                job.mkdir()
                (job / "result.json").write_text(json.dumps({"status": "done", "cost_usd": 0, "exit_code": 0}),
                                                 encoding="utf-8")

            with mock.patch.object(fetch.subprocess, "run", side_effect=fake_scp):
                self.assertEqual(
                    fetch.main(["job-20261116-0001", "--dest", str(dest),
                                "--host", "lab-host"]),
                    0,
                )
        self.assertEqual(seen[0][-2], "lab-host:/srv/re-lab/reports/job-20261116-0001")

    def test_fetch_requires_an_explicit_host_or_environment_variable(self):
        fetch = load("re_fetch")
        with mock.patch.dict(fetch.os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "--host or RE_LAB_HOST"):
                fetch.main(["job-20261116-0001"])


if __name__ == "__main__":
    unittest.main()
