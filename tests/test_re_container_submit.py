"""re_submit / re_fetch: remote layout and permissions, with every remote command captured.

Nothing is sent anywhere: `run` (submit) and `subprocess.run` (fetch) are replaced by recorders.
"""
import contextlib
import importlib.util
import io
import json
import os
import shlex
import socket
import stat
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

CONTAINER = Path(__file__).resolve().parents[1] / "container"
JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")


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

    def submitted(self, *extra_args):
        out = io.StringIO()
        with mock.patch.object(self.submit, "run", side_effect=self.calls.append), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.submit.main([str(self.target), "What does it do?", *extra_args]), 0)
        self.printed = out.getvalue()
        return self.calls

    def staging(self, name):
        staged = Path(self.tmp.name) / name
        staged.mkdir()
        return staged

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

    def test_the_final_rename_refuses_to_nest_into_an_existing_job(self):
        calls = self.submitted()
        move = next(c for c in calls if "mv" in c)
        job_id = self.printed.strip()
        self.assertEqual(move, [
            "ssh", "lab-host", "mv", "-T", "--", f"/srv/re-lab/jobs/inbox/.incoming-{job_id}",
            f"/srv/re-lab/jobs/inbox/{job_id}"])
        self.assertIs(calls[-1], move, "the rename is the last remote step")

    def test_every_remote_step_ends_options_before_its_operands(self):
        calls = self.submitted("--executable", self.target.name)
        scp = calls[0]
        self.assertEqual(scp[:5], ["scp", "-p", "-r", "-q", "--"])
        for call in calls[1:]:
            self.assertEqual(call[:2], ["ssh", "lab-host"])
            self.assertIn("--", call)
            self.assertTrue(all(arg.startswith("/srv/re-lab/") for arg in call[call.index("--") + 1:]))

    def test_remote_commands_never_read_the_callers_stdin(self):
        with mock.patch.object(self.submit.subprocess, "run") as run:
            self.submit.run(["ssh", "lab-host", "true"])
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["check"], True)
        self.assertIs(run.call_args.kwargs["shell"], False)

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

    def test_failed_remote_step_removes_the_hidden_upload_and_exits_cleanly(self):
        for failing, step in (("scp", "upload"), ("-R", "permissions"),
                              ("0755", "executable modes"), ("mv", "rename")):
            with self.subTest(step=step):
                calls = []

                def remote(argv):
                    calls.append(argv)
                    if failing in argv[:5] or argv[0] == failing:
                        raise subprocess.CalledProcessError(23, argv)

                out = io.StringIO()
                with mock.patch.object(self.submit, "run", side_effect=remote), \
                        contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as raised:
                        self.submit.main([str(self.target), "Q", "--executable", self.target.name])
                message = str(raised.exception.code)
                self.assertIn(f"{step} step failed (exit code 23)", message)
                self.assertNotIn("\n", message)
                hidden = next(arg for arg in calls[0] if ".incoming-" in arg).split(":", 1)[1]
                self.assertEqual(calls[-1], ["ssh", "lab-host", "rm", "-rf", "--", hidden])
                self.assertEqual(out.getvalue(), "", "no job id is printed for a failed upload")

    def test_cleanup_failure_and_missing_tools_still_exit_with_one_line(self):
        def remote(argv):
            if argv[0] == "scp":
                raise FileNotFoundError(2, "No such file", "scp")
            raise subprocess.CalledProcessError(255, argv)

        with mock.patch.object(self.submit, "run", side_effect=remote):
            with self.assertRaises(SystemExit) as raised:
                self.submit.main([str(self.target), "Q"])
        self.assertIn("upload step failed (scp could not start: FileNotFoundError)",
                      str(raised.exception.code))

    def test_dry_run_needs_no_host(self):
        out = io.StringIO()
        with mock.patch.dict(self.submit.os.environ, {}, clear=True), \
                mock.patch.object(self.submit, "run", side_effect=self.calls.append), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.submit.main([str(self.target), "Q", "--dry-run"]), 0)
        self.assertEqual(self.calls, [])
        meta = json.loads(out.getvalue())
        self.assertEqual(meta["files"][0]["path"], "target/sample.bin")

    def test_host_and_model_must_match_in_full(self):
        for args, message in ((["--host", "lab-host\n"], "unsupported host name"),
                              (["--model", "openai/x\n"], "unsupported model name"),
                              (["--host=-oProxyCommand=x"], "unsupported host name")):
            with self.subTest(args=args):
                with mock.patch.object(self.submit, "run", side_effect=self.calls.append):
                    with self.assertRaisesRegex(SystemExit, message):
                        self.submit.main([str(self.target), "Q", *args])
        self.assertEqual(self.calls, [])

    def test_job_ids_carry_64_random_bits(self):
        now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
        first, second = self.submit.new_job_id(now), self.submit.new_job_id(now)
        self.assertRegex(first, r"^job-20261118-120000-[0-9a-f]{16}$")
        self.assertNotEqual(first, second)
        self.assertTrue(JOB_ID_RE.fullmatch(first), "the dispatcher's job id shape")

    @unittest.skipUnless(os.name == "posix", "POSIX execute modes are unavailable on Windows")
    def test_stage_preserves_execute_bits_and_override_sets_0755(self):
        os.chmod(self.target, 0o751)
        staged = self.staging("staged-preserved")
        self.submit.stage_job(
            staged, "job-20261116-0001", self.target, "Question", None, None)
        copied = staged / "target" / self.target.name
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode) & 0o111, 0o111)

        os.chmod(self.target, 0o644)
        overridden = self.staging("staged-overridden")
        self.submit.stage_job(
            overridden, "job-20261116-0002", self.target, "Question", None, None,
            executables=[self.target.name])
        self.assertEqual(stat.S_IMODE(
            (overridden / "target" / self.target.name).stat().st_mode), 0o755)

    def test_executable_override_must_name_a_collected_file(self):
        staged = self.staging("staged-invalid")
        with self.assertRaises(SystemExit):
            self.submit.stage_job(
                staged, "job-20261116-0001", self.target, "Question", None, None,
                executables=["missing"])

    def make_tree(self, names=("bin/tool", "lib/helper.so", "run.sh")):
        source = Path(self.tmp.name) / "tree"
        for name in names:
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_bytes(name.encode())
        return source

    @unittest.skipUnless(os.name == "posix", "POSIX execute modes are unavailable on Windows")
    def test_executable_spellings_are_normalised(self):
        source = self.make_tree()
        for index, spelling in enumerate(("bin/tool", "./bin/tool", "bin//tool", "bin/./tool",
                                          "bin/tool/")):
            with self.subTest(spelling=spelling):
                staged = self.staging(f"spelled-{index}")
                self.submit.stage_job(staged, "job-20261116-0001", source, "Q", None, None,
                                      executables=[spelling])
                self.assertEqual(
                    stat.S_IMODE((staged / "target" / "bin" / "tool").stat().st_mode), 0o755)
        self.assertEqual(self.submit.normalize_executables(["./bin/tool", "bin//tool", "run.sh"]),
                         ["bin/tool", "run.sh"])

    def test_executable_outside_the_target_is_refused(self):
        source = self.make_tree()
        for index, spelling in enumerate(("../bin/tool", "bin/../../x", "/abs/bin/tool", "",
                                          ".", "tree/bin/tool", "bin\\tool")):
            with self.subTest(spelling=spelling):
                with self.assertRaises(SystemExit):
                    self.submit.stage_job(self.staging(f"bad-{index}"),
                                          "job-20261116-0001", source, "Q", None, None,
                                          executables=[spelling])

    def test_a_missed_executable_lists_up_to_twenty_collected_paths(self):
        source = self.make_tree([f"file-{index:02d}" for index in range(25)])
        with self.assertRaises(SystemExit) as raised:
            self.submit.stage_job(self.staging("missed"), "job-20261116-0001", source, "Q",
                                  None, None, executables=["./bin/tool"])
        message = str(raised.exception.code)
        self.assertIn("'./bin/tool' does not name a collected target file", message)
        self.assertEqual(len(re.findall(r"'file-\d\d'", message)), 20)
        self.assertIn("(and 5 more)", message)

    def test_scp_preserves_staged_modes(self):
        scp = next(call for call in self.submitted() if call[0] == "scp")
        self.assertIn("-p", scp)

    def test_executable_override_is_applied_on_the_remote_posix_host(self):
        with mock.patch.object(self.submit, "run", side_effect=self.calls.append), \
                contextlib.redirect_stdout(io.StringIO()):
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
        with mock.patch.object(self.submit, "run", side_effect=self.calls.append), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.submit.main([
                str(source), "What does it do?",
                "--executable", "one",
                "--executable", "nested/two",
                "--executable", "./one",
            ]), 0)
        chmod = next(call for call in self.calls if "0755" in call)
        paths = chmod[chmod.index("--") + 1:]
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].endswith("/target/one"))
        self.assertTrue(paths[1].endswith("/target/nested/two"))

    @unittest.skipUnless(os.name == "posix", "POSIX names may contain spaces and quotes")
    def test_remote_executable_paths_are_quoted_for_the_remote_shell(self):
        source = self.make_tree(["Setup (1).exe", "it's.sh"])
        with mock.patch.object(self.submit, "run", side_effect=self.calls.append), \
                contextlib.redirect_stdout(io.StringIO()):
            self.submit.main([str(source), "Q", "--executable", "Setup (1).exe",
                              "--executable", "it's.sh"])
        chmod = next(call for call in self.calls if "0755" in call)
        for quoted in chmod[chmod.index("--") + 1:]:
            self.assertEqual(len(shlex.split(quoted)), 1, quoted)

    def test_common_sample_names_are_accepted(self):
        for name in ("__init__.py", ".profile", "Setup (1).exe", "résumé.exe",
                     "_CodeSignature", "-starts-with-dash", "...", "a" * 255, "中" * 85,
                     "name with  spaces", "x​y"):
            with self.subTest(name=name[:20]):
                self.assertTrue(self.submit.valid_name(name))

    def test_dangerous_names_are_refused(self):
        for name in ("", ".", "..", "a/b", "a\\b", "x\x7f", "x\x85", "x\x9b", "tab\tname",
                     "line\n", "\x00", "esc\x1b[2J", "lone\udcff", "a" * 256, "é" * 128):
            with self.subTest(name=repr(name)[:30]):
                self.assertFalse(self.submit.valid_name(name))

    @unittest.skipUnless(os.name == "posix", "requires POSIX file names")
    def test_relaxed_names_survive_staging(self):
        names = ["_internal/__init__.py", ".config/settings", "My App.app/Contents/Info.plist",
                 "résumé.exe", "-x"]
        source = self.make_tree(names)
        staged = self.staging("relaxed")
        meta = self.submit.stage_job(staged, "job-20261116-0001", source, "Q", None, None)
        self.assertEqual(sorted(entry["path"] for entry in meta["files"][:-1]),
                         sorted(f"target/{name}" for name in names))
        for name in names:
            self.assertEqual((staged / "target" / name).read_bytes(), name.encode())

    @unittest.skipUnless(os.name == "posix", "requires POSIX file names")
    def test_final_newline_in_file_name_is_rejected(self):
        source = Path(self.tmp.name) / "source-newline"
        source.mkdir()
        (source / "payload\n").write_bytes(b"payload")
        with self.assertRaises(SystemExit):
            self.submit.collect_target(source)

    @unittest.skipUnless(os.name == "posix", "requires POSIX file names")
    def test_undecodable_file_name_is_rejected(self):
        source = Path(self.tmp.name) / "source-bytes"
        source.mkdir()
        with open(os.path.join(os.fsencode(source), b"bad\xff.bin"), "wb") as handle:
            handle.write(b"x")
        with self.assertRaisesRegex(SystemExit, "unsupported file name"):
            self.submit.collect_target(source)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_special_files_are_refused_before_anything_is_copied(self):
        source = self.make_tree(["a.bin"])
        os.mkfifo(source / "pipe")
        staged = self.staging("fifo")
        with self.assertRaisesRegex(SystemExit, r"special file \(FIFO, socket or device\).*'pipe'"):
            self.submit.stage_job(staged, "job-20261116-0001", source, "Q", None, None)
        self.assertEqual(list(staged.iterdir()), [], "nothing is staged")
        os.unlink(source / "pipe")

        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(source / "sock"))
            with self.assertRaisesRegex(SystemExit, "special file.*'sock'"):
                self.submit.collect_target(source)
        os.unlink(source / "sock")

        try:
            os.mknod(source / "null", stat.S_IFCHR | 0o600, os.makedev(1, 3))
        except (PermissionError, AttributeError, OSError):
            return
        with self.assertRaisesRegex(SystemExit, "special file.*'null'"):
            self.submit.collect_target(source)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_a_special_file_target_is_refused(self):
        fifo = Path(self.tmp.name) / "target-fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(SystemExit, "regular file or a directory"):
            self.submit.collect_target(fifo)

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")
    def test_symlinks_are_still_refused(self):
        source = self.make_tree(["a.bin"])
        os.symlink(source / "a.bin", source / "link")
        with self.assertRaisesRegex(SystemExit, "refusing symlink"):
            self.submit.collect_target(source)
        os.unlink(source / "link")
        os.symlink(source, source / "loop")
        with self.assertRaisesRegex(SystemExit, "refusing symlink"):
            self.submit.collect_target(source)
        with self.assertRaisesRegex(SystemExit, "must not be a symlink"):
            self.submit.collect_target(source / "loop")

    def test_extra_metadata_is_merged_into_job_json(self):
        staged = self.staging("extra")
        extra = {"agent": "malware-triage", "conversation": "conv-0123", "source": "chat"}
        meta = self.submit.stage_job(staged, "job-20261116-0001", self.target, "Q", None, None,
                                     extra=extra)
        written = json.loads((staged / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(written, meta)
        for key, value in extra.items():
            self.assertEqual(written[key], value)
        self.assertEqual(written["job_id"], "job-20261116-0001")

    def test_extra_metadata_is_limited_to_three_id_keys(self):
        bad = ({"other": "x"}, {"job_id": "job-evil-0001"}, {"files": "x"},
               {"agent": "Bad"}, {"agent": "-x"}, {"agent": "a" * 41}, {"agent": 5},
               {"conversation": ""}, {"conversation": "a" * 65}, {"conversation": "a b"},
               {"source": "a" * 33}, {"source": "x\n"}, ["agent"], "agent")
        for index, extra in enumerate(bad):
            with self.subTest(extra=str(extra)[:30]):
                staged = self.staging(f"bad-extra-{index}")
                with self.assertRaises(SystemExit):
                    self.submit.stage_job(staged, "job-20261116-0001", self.target, "Q",
                                          None, None, extra=extra)
                self.assertEqual(list(staged.iterdir()), [], "rejected before staging")
        meta = self.submit.stage_job(self.staging("edge-extra"), "job-20261116-0001",
                                     self.target, "Q", None, None,
                                     extra={"agent": "a" * 40, "conversation": "-" * 64,
                                            "source": "s" * 32})
        self.assertEqual(meta["agent"], "a" * 40)

    def test_extra_is_keyword_only(self):
        with self.assertRaises(TypeError):
            self.submit.stage_job(self.staging("positional"), "job-20261116-0001", self.target,
                                  "Q", None, None, (), {"agent": "x"})

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

            def fake_remote(argv, **kwargs):
                seen.append(argv)
                if argv[0] == "scp":
                    job = Path(argv[-1]) / "job-20261116-0001"
                    job.mkdir()
                    (job / "result.json").write_text(
                        json.dumps({"status": "done", "cost_usd": 0, "exit_code": 0}),
                        encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0)

            with mock.patch.object(fetch.subprocess, "run", side_effect=fake_remote), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    fetch.main(["job-20261116-0001", "--dest", str(dest),
                                "--host", "lab-host"]),
                    0,
                )
            self.assertTrue((dest / "job-20261116-0001" / "result.json").is_file())
        scp = next(call for call in seen if call[0] == "scp")
        self.assertEqual(scp[-2], "lab-host:/srv/re-lab/reports/job-20261116-0001")

    def test_fetch_requires_an_explicit_host_or_environment_variable(self):
        fetch = load("re_fetch")
        with mock.patch.dict(fetch.os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "--host or RE_LAB_HOST"):
                fetch.main(["job-20261116-0001"])


if __name__ == "__main__":
    unittest.main()
