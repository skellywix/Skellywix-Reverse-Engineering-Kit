"""Executor contract: fixed docker argv, closed request schema, bounded run, no agent-writable mounts.

The fake docker below is a child-process fixture script written into a temp
dir; the executor under test launches it exactly as it would launch docker.
It can also act as the container: it writes into the mounted out dir, or tampers
with the job directory the way a compromised agent could while the run is live.

The fd-relative copy needs POSIX openat semantics (O_NOFOLLOW, dir_fd). The
executor refuses to run anywhere else, so the tests that exercise a real run are
skipped on platforms without them (Windows) and run under Linux.
"""
import contextlib
import importlib.util
import io
import json
from dataclasses import replace
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock
import threading

CONTAINER = Path(__file__).resolve().parents[1] / "container"
IMAGE = "sha256:" + "a" * 64
JOB = "job-20261116-0001"
RUN_ID = "0123456789abcdef0123456789abcdef"

HAS_FD_OPS = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
              and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
              and os.stat in os.supports_dir_fd and os.listdir in os.supports_fd)
needs_fd_ops = unittest.skipUnless(HAS_FD_OPS, "platform lacks O_NOFOLLOW / dir_fd (POSIX openat)")


def load_executor():
    spec = importlib.util.spec_from_file_location("re_container_executor", CONTAINER / "executor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE_DOCKER = textwrap.dedent('''
    import json, os, sys, time
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    sys.stdout.reconfigure(newline="\\n")
    sys.stderr.reconfigure(newline="\\n")
    with open(os.path.join(here, "calls.jsonl"), "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[0] == "kill":
        with open(os.path.join(here, "killed"), "w") as marker:
            marker.write(args[1])
    elif args[0] == "rm":
        with open(os.path.join(here, "removed"), "w") as marker:
            marker.write(args[-1])
    elif args[0] == "inspect":
        if os.path.exists(os.path.join(here, "removed")):
            sys.stderr.write("Error: No such object: " + args[1] + "\\n")
            sys.exit(1)
    elif args[0] == "run":
        if "--cidfile" in args:
            # Like the real client: the container id is written to the cidfile at create time.
            with open(args[args.index("--cidfile") + 1], "x") as cid:
                cid.write("ab" * 32)
        image = next(n for n, a in enumerate(args) if a.startswith("sha256:"))
        command = args[image + 1:]
        mounts = {}
        for n, a in enumerate(args[:image]):
            if a == "-v":
                src, dst, _mode = args[n + 1].rsplit(":", 2)
                mounts[dst] = src
        in_src, out_src = mounts["/job/in"], mounts["/job/out"]
        run_id = os.path.basename(os.path.dirname(out_src))
        if command[0] == "echo":
            sys.stdout.write(" ".join(command[1:]) + "\\n")
            sys.stderr.write("warn\\n")
        elif command[0] == "fail":
            sys.exit(7)
        elif command[0] == "flood":
            sys.stdout.write("x" * 2000000)
        elif command[0] == "sleep":
            time.sleep(60)
        elif command[0] == "catin":
            with open(os.path.join(in_src, command[1])) as handle:
                sys.stdout.write(handle.read())
        elif command[0] == "writeout":
            target = os.path.join(out_src, command[1])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as handle:
                handle.write(command[2])
        elif command[0] == "linkout":
            os.symlink(command[2], os.path.join(out_src, command[1]))
        elif command[0] == "plantlink":
            # A compromised agent plants a symlink at the copy-back destination: out/<run-id> -> outside.
            os.symlink(command[2], os.path.join(command[1], run_id))
        elif command[0] == "swapout":
            # ... or replaces the whole out dir with a symlink while the run is live.
            job_out = command[1]
            os.rename(job_out, job_out + "-moved")
            os.symlink(command[2], job_out)
''')


class ExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self.executor = load_executor()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.requests = root / "requests"
        self.results = root / "results"
        self.runs = root / "runs"
        for directory in (self.work, self.requests, self.results, self.runs):
            directory.mkdir()
        os.chmod(self.runs, 0o700)
        self.outside = root / "outside"
        self.outside.mkdir()
        self.fake = root / "fake_docker.py"
        self.fake.write_text(FAKE_DOCKER, encoding="utf-8")
        self.uid = os.getuid() if hasattr(os, "getuid") else 10001
        self.gid = os.getgid() if hasattr(os, "getgid") else 10001
        self.cfg = self.executor.Config(
            image=IMAGE, work_root=self.work, requests_dir=self.requests,
            results_dir=self.results, active_job_id=JOB, runs_dir=self.runs,
            agent_uid=self.uid, agent_gid=self.gid,
            docker_prefix=(sys.executable, str(self.fake)))
        self.make_job(JOB)

    def make_job(self, job_id):
        job = self.work / job_id
        (job / "target").mkdir(parents=True)
        (job / "out").mkdir()
        return job

    def calls(self):
        log = self.fake.parent / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def run_calls(self):
        return [c for c in self.calls() if c[0] == "run"]

    def request(self, **overrides):
        body = {"job_id": JOB, "argv": ["echo", "hi"], "timeout_s": 30, "memory_mb": 256}
        body.update(overrides)
        return body

    def run_request(self, **overrides):
        return self.executor.execute(self.cfg, self.executor.validate_request(self.request(**overrides)))

    def mount_sources(self):
        sources = []
        for call in self.run_calls():
            for n, arg in enumerate(call):
                if arg == "-v":
                    sources.append(Path(call[n + 1].rsplit(":", 2)[0]))
        return sources


class RequestValidationTests(ExecutorTestBase):
    def test_valid_request_builds_exactly_the_fixed_argv(self):
        request = self.executor.validate_request(self.request(argv=["objdump", "-d", "/job/in/a.out"]))
        # Default config: the sandbox user is the unprivileged uid/gid 10001 the contract names.
        cfg = self.executor.Config(
            image=IMAGE, work_root=self.work, requests_dir=self.requests,
            results_dir=self.results, active_job_id=JOB, runs_dir=self.runs,
            docker_prefix=self.cfg.docker_prefix)
        run = self.runs / RUN_ID
        expected = [
            *cfg.docker_prefix, "run", "--rm", "--runtime=runsc", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=10001:10001",
            "--label", "re-lab.role=exec", "--label", f"re-lab.job={JOB}",
            "--name", f"re-lab-exec-{RUN_ID}", "--pull=never",
            "--log-driver=none", "--cidfile", str(run / "cid"), "--pids-limit=256",
            "--memory=256m", "--memory-swap=256m", "--cpus=1",
            "--ulimit", "fsize=1073741824", "--ulimit", "nofile=4096:4096",
            "--tmpfs", "/tmp:size=256m,noexec,nosuid,nodev",
            "-v", f"{run / 'in'}:/job/in:ro", "-v", f"{run / 'out'}:/job/out:rw",
            IMAGE, "objdump", "-d", "/job/in/a.out"]
        self.assertEqual(self.executor.build_argv(cfg, request, RUN_ID), expected)

    def test_exec_tmp_is_noexec_but_the_target_mount_remains_executable(self):
        request = self.executor.validate_request(
            self.request(argv=["/job/in/tool"]))
        argv = self.executor.build_argv(self.cfg, request, RUN_ID)
        self.assertEqual(argv[argv.index("--tmpfs") + 1],
                         "/tmp:size=256m,noexec,nosuid,nodev")
        input_mount = next(argv[n + 1] for n, value in enumerate(argv)
                           if value == "-v" and ":/job/in:" in argv[n + 1])
        self.assertEqual(input_mount.rsplit(":", 1)[1], "ro")
        self.assertNotIn("noexec", input_mount)
        self.assertEqual(argv[argv.index(IMAGE) + 1], "/job/in/tool")

    def test_mount_sources_are_always_under_the_runs_root_never_the_job_dir(self):
        request = self.executor.validate_request(self.request())
        argv = self.executor.build_argv(self.cfg, request, RUN_ID)
        sources = [Path(argv[n + 1].rsplit(":", 2)[0]) for n, a in enumerate(argv) if a == "-v"]
        self.assertEqual(len(sources), 2)
        for source in sources:
            self.assertEqual(source.parent.parent, self.runs)
            self.assertNotIn(self.work, source.parents)

    def test_run_id_must_be_a_uuid_hex_so_it_cannot_escape_the_runs_root(self):
        request = self.executor.validate_request(self.request())
        for bad in ("../evil", "..", "a/b", "A" * 32, "0" * 31, "0" * 33, "", f"{RUN_ID}/../x"):
            with self.subTest(run_id=bad):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.build_argv(self.cfg, request, bad)

    def test_defaults_apply_when_limits_are_omitted(self):
        request = self.executor.validate_request({"job_id": JOB, "argv": ["echo"]})
        self.assertEqual((request.timeout_s, request.memory_mb), (60, 512))

    def test_flag_shaped_argv_stays_after_the_image(self):
        request = self.executor.validate_request(self.request(argv=["--privileged", "--network=host"]))
        argv = self.executor.build_argv(self.cfg, request, RUN_ID)
        split = argv.index(IMAGE)
        self.assertEqual(argv[split + 1:], ["--privileged", "--network=host"])
        self.assertNotIn("--privileged", argv[:split])
        self.assertNotIn("--network=host", argv[:split])

    def test_unknown_and_forbidden_keys_are_refused(self):
        forbidden = {
            "network": "host", "env": {"A": "b"}, "environment": ["A=b"],
            "mounts": ["/etc:/etc"], "volumes": ["/:/host"], "devices": ["/dev/kvm"],
            "privileged": True, "user": "root", "image": "evil", "runtime": "runc",
            "cap_add": ["SYS_ADMIN"], "unexpected": 1, "run_id": RUN_ID, "out_dir": "/etc"}
        for key, value in forbidden.items():
            with self.subTest(key=key):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.validate_request(self.request(**{key: value}))

    def test_limits_outside_bounds_are_refused(self):
        cases = [("timeout_s", 601), ("timeout_s", 0), ("timeout_s", -5), ("timeout_s", 1.5),
                 ("timeout_s", True), ("timeout_s", "30"),
                 ("memory_mb", 1025), ("memory_mb", 63), ("memory_mb", True), ("memory_mb", "256")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.validate_request(self.request(**{key: value}))

    def test_limit_boundaries_are_accepted(self):
        for timeout_s, memory_mb in ((1, 64), (600, 1024)):
            with self.subTest(timeout_s=timeout_s, memory_mb=memory_mb):
                request = self.executor.validate_request(
                    self.request(timeout_s=timeout_s, memory_mb=memory_mb))
                self.assertEqual((request.timeout_s, request.memory_mb), (timeout_s, memory_mb))

    def test_job_id_shape_is_enforced(self):
        bad = ["..", "../etc", "job/../x1234567", "a/b-cdefghij", "a\\b-cdefghij", "ABCDEFGH-1",
               "short", "x" * 65, "job_id_underscore", "job id with space", "", None, 12345678]
        for job_id in bad:
            with self.subTest(job_id=job_id):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.validate_request(self.request(job_id=job_id))

    def test_argv_must_be_a_nonempty_list_of_strings(self):
        bad = ["echo hi", [], ["echo", 1], ["echo", None], [["echo"]], ["echo\x00x"], {"a": "b"}, None]
        for argv in bad:
            with self.subTest(argv=argv):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.validate_request(self.request(argv=argv))

    def test_non_object_request_is_refused(self):
        for raw in ([], "x", None, 3):
            with self.subTest(raw=raw):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.validate_request(raw)


@needs_fd_ops
class JobDirectoryTests(ExecutorTestBase):
    """prepare_run opens the agent-owned job dir only through O_NOFOLLOW dirfds."""

    def prepare_fails(self, job_id=JOB):
        request = self.executor.validate_request(self.request(job_id=job_id))
        cfg = replace(self.cfg, active_job_id=job_id)
        with self.assertRaises(self.executor.RequestError):
            self.executor.prepare_run(cfg, request)
        self.assertEqual(list(self.runs.iterdir()), [], "a refused run must leave no run dir behind")

    def test_missing_job_dir_is_refused(self):
        self.prepare_fails("job-never-claimed")

    def test_job_without_target_or_out_is_refused(self):
        job = self.work / "job-without-out"
        (job / "target").mkdir(parents=True)
        self.prepare_fails("job-without-out")

    def test_symlinked_job_dir_is_refused(self):
        os.symlink(self.outside, self.work / "job-link-whole", target_is_directory=True)
        self.prepare_fails("job-link-whole")

    def test_symlinked_out_dir_is_refused(self):
        job = self.work / "job-link-out"
        (job / "target").mkdir(parents=True)
        os.symlink(self.outside, job / "out", target_is_directory=True)
        self.prepare_fails("job-link-out")

    def test_symlinked_target_dir_is_refused(self):
        job = self.work / "job-link-in"
        (job / "out").mkdir(parents=True)
        os.symlink(self.outside, job / "target", target_is_directory=True)
        self.prepare_fails("job-link-in")

    def test_symlinked_job_paths_never_reach_docker(self):
        job = self.work / "job-link-out"
        (job / "target").mkdir(parents=True)
        os.symlink(self.outside, job / "out", target_is_directory=True)
        cfg = replace(self.cfg, active_job_id="job-link-out")
        result = self.executor.process_request_file(
            cfg, self.write_request(self.request(job_id="job-link-out")))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.outside.iterdir()), [])

    def write_request(self, body):
        path = self.requests / "tamper.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def test_symlinked_file_entry_is_refused(self):
        secret = self.outside / "host-secret"
        secret.write_text("TOPSECRET", encoding="utf-8")
        os.symlink(secret, self.work / JOB / "target" / "innocent.txt")
        self.prepare_fails()

    def test_symlinked_directory_entry_is_refused(self):
        os.symlink(self.outside, self.work / JOB / "target" / "dirlink", target_is_directory=True)
        self.prepare_fails()

    def test_nested_symlink_is_refused(self):
        nested = self.work / JOB / "target" / "a" / "b"
        nested.mkdir(parents=True)
        os.symlink(self.outside, nested / "evil", target_is_directory=True)
        self.prepare_fails()

    def test_fifo_entry_is_refused_without_blocking(self):
        os.mkfifo(self.work / JOB / "target" / "pipe")
        self.prepare_fails()

    def test_entry_count_cap(self):
        for n in range(6):
            (self.work / JOB / "target" / f"f{n}").write_text("x", encoding="utf-8")
        with mock.patch.object(self.executor, "MAX_ENTRIES", 5):
            self.prepare_fails()

    def test_depth_cap(self):
        deep = self.work / JOB / "target"
        for n in range(4):
            deep = deep / f"d{n}"
        deep.mkdir(parents=True)
        with mock.patch.object(self.executor, "MAX_DEPTH", 3):
            self.prepare_fails()

    def test_total_size_cap(self):
        (self.work / JOB / "target" / "big").write_bytes(b"x" * 101)
        with mock.patch.object(self.executor, "MAX_INPUT_BYTES", 100):
            self.prepare_fails()

    def test_default_caps_match_the_contract(self):
        self.assertEqual(self.executor.MAX_INPUT_BYTES, 2 * 1024 ** 3)
        self.assertEqual(self.executor.MAX_ENTRIES, 10000)
        self.assertEqual(self.executor.MAX_DEPTH, 32)
        self.assertEqual(self.executor.MAX_OUTPUT_BYTES, 64 * 1024 ** 2)

    def test_runs_root_must_be_a_private_real_directory(self):
        request = self.executor.validate_request(self.request())
        os.chmod(self.runs, 0o755)
        with self.assertRaises(self.executor.RequestError):
            self.executor.prepare_run(self.cfg, request)
        os.chmod(self.runs, 0o700)
        self.runs.rmdir()
        os.symlink(self.outside, self.runs, target_is_directory=True)
        with self.assertRaises(self.executor.RequestError):
            self.executor.prepare_run(self.cfg, request)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_runs_root_must_not_overlap_agent_visible_dirs(self):
        for runs_dir in (self.work, self.work / "inner", self.requests, self.results, self.work.parent):
            with self.subTest(runs_dir=runs_dir):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.Config(
                        image=IMAGE, work_root=self.work, requests_dir=self.requests,
                        results_dir=self.results, active_job_id=JOB, runs_dir=runs_dir)


class ExecutionTests(ExecutorTestBase):
    @needs_fd_ops
    def test_successful_run_returns_bounded_output(self):
        result = self.run_request(argv=["echo", "hello", "world"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "hello world\n")
        self.assertEqual(result["stderr"], "warn\n")

    @needs_fd_ops
    def test_nonzero_exit_is_recorded_as_failed(self):
        result = self.run_request(argv=["fail"])
        self.assertEqual((result["status"], result["exit_code"]), ("failed", 7))

    @needs_fd_ops
    def test_output_is_truncated_at_the_cap(self):
        result = self.run_request(argv=["flood"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["stdout"]), self.executor.OUTPUT_CAP_BYTES)
        self.assertTrue(result["stdout_truncated"])

    @needs_fd_ops
    def test_timeout_kills_the_container_and_records_timeout(self):
        started = time.monotonic()
        result = self.run_request(argv=["sleep"], timeout_s=1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result["status"], "timeout")
        argv = self.run_calls()[0]
        self.assertEqual(argv[argv.index("--cidfile") + 1], str(self.runs / result["run_id"] / "cid"))
        self.assertEqual([c for c in self.calls() if c[0] == "ps"], [], "the kill must not search by volume")
        self.assertEqual((self.fake.parent / "killed").read_text(encoding="utf-8"), "ab" * 32)
        self.assertEqual(list(self.runs.iterdir()), [])

    @needs_fd_ops
    def test_every_launch_uses_fixed_flags_without_a_shell(self):
        self.run_request()
        run = self.run_calls()
        self.assertEqual(len(run), 1)
        for flag in ("--rm", "--runtime=runsc", "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--pids-limit=256", "--cpus=1"):
            self.assertIn(flag, run[0])

    @needs_fd_ops
    def test_the_container_mounts_only_private_copies_under_the_runs_root(self):
        result = self.run_request()
        sources = self.mount_sources()
        self.assertEqual(len(sources), 2)
        for source in sources:
            self.assertEqual(source.parent.parent, self.runs)
            self.assertEqual(source.parent.name, result["run_id"])
            self.assertNotIn(self.work, source.parents)

    @needs_fd_ops
    def test_input_is_copied_so_the_container_reads_a_private_copy(self):
        nested = self.work / JOB / "target" / "sub"
        nested.mkdir()
        (nested / "a.txt").write_text("sample-bytes", encoding="utf-8")
        result = self.run_request(argv=["catin", "sub/a.txt"])
        self.assertEqual(result["stdout"], "sample-bytes")

    @needs_fd_ops
    def test_run_dir_is_removed_after_success_and_failure(self):
        self.run_request()
        self.run_request(argv=["fail"])
        self.assertEqual(list(self.runs.iterdir()), [])

    @needs_fd_ops
    def test_run_dirs_are_private_while_the_container_runs(self):
        modes = {}
        original = self.executor.build_argv

        def spy(cfg, request, run_id):
            for name in ("", "in", "out"):
                info = os.stat(cfg.runs_dir / run_id / name)
                modes[name] = (stat.S_IMODE(info.st_mode), info.st_uid)
            return original(cfg, request, run_id)

        with mock.patch.object(self.executor, "build_argv", spy):
            self.run_request()
        euid = os.geteuid()
        self.assertEqual(modes, {"": (0o700, euid), "in": (0o555, euid), "out": (0o700, self.uid)})

    @needs_fd_ops
    def test_stale_run_dirs_are_swept_at_startup(self):
        stale = self.runs / RUN_ID
        (stale / "out").mkdir(parents=True)
        (stale / "out" / "leftover").write_text("x", encoding="utf-8")
        self.executor.sweep_runs(self.cfg)
        self.assertEqual(list(self.runs.iterdir()), [])


@needs_fd_ops
class SandboxPermissionTests(ExecutorTestBase):
    """The container runs as uid 10001: it reads the root-owned in/ copy and writes only out/."""

    def prepare(self, cfg=None):
        request = self.executor.validate_request(self.request())
        run = self.executor.prepare_run(cfg or self.cfg, request)
        self.addCleanup(run.discard)
        return run

    def test_input_copy_is_root_owned_read_only_and_keeps_only_the_execute_bit(self):
        target = self.work / JOB / "target"
        (target / "sub").mkdir()
        modes = {"tool": 0o755, "sub/data.bin": 0o666, "sub/odd": 0o654}
        for name, mode in modes.items():
            (target / name).write_bytes(b"payload")
            os.chmod(target / name, mode)
        run = self.prepare()
        copied = {}
        for path in (run.path / "in").rglob("*"):
            info = os.stat(path)
            copied[path.relative_to(run.path / "in").as_posix()] = (stat.S_IMODE(info.st_mode), info.st_uid)
        euid = os.geteuid()
        self.assertEqual(copied, {"tool": (0o555, euid), "sub": (0o555, euid),
                                  "sub/data.bin": (0o444, euid), "sub/odd": (0o555, euid)})
        top = os.stat(run.path / "in")
        self.assertEqual((stat.S_IMODE(top.st_mode), top.st_uid), (0o555, euid))

    def test_run_root_is_private_and_out_belongs_to_the_agent_uid_alone(self):
        run = self.prepare()
        root, out = os.stat(run.path), os.stat(run.path / "out")
        self.assertEqual((stat.S_IMODE(root.st_mode), root.st_uid), (0o700, os.geteuid()))
        self.assertEqual((stat.S_IMODE(out.st_mode), out.st_uid, out.st_gid), (0o700, self.uid, self.gid))

    @unittest.skipUnless(hasattr(os, "setresuid") and hasattr(os, "fork") and os.geteuid() == 0,
                         "needs Linux, root and setresuid to act as uid 10001")
    def test_uid_10001_reads_in_and_writes_out_but_cannot_write_in(self):
        cfg = self.executor.Config(
            image=IMAGE, work_root=self.work, requests_dir=self.requests,
            results_dir=self.results, active_job_id=JOB, runs_dir=self.runs,
            agent_uid=10001, agent_gid=10001, docker_prefix=self.cfg.docker_prefix)
        target = self.work / JOB / "target"
        (target / "sub").mkdir()
        (target / "sub" / "a.txt").write_text("sample", encoding="utf-8")
        run = self.prepare(cfg)
        in_fd = os.open(run.path / "in", self.executor.DIR_FLAGS)
        out_fd = os.open(run.path / "out", self.executor.DIR_FLAGS)
        self.addCleanup(os.close, in_fd)
        self.addCleanup(os.close, out_fd)
        pid = os.fork()
        if pid == 0:  # child: become the sandbox user, then use only the pre-opened descriptors
            code = 0
            try:
                os.setgroups([])
                os.setresgid(10001, 10001, 10001)
                os.setresuid(10001, 10001, 10001)
                sub = os.open("sub", self.executor.DIR_FLAGS, dir_fd=in_fd)
                data = os.open("a.txt", os.O_RDONLY, dir_fd=sub)
                if os.read(data, 100) != b"sample":
                    code = 3
                result = os.open("result.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=out_fd)
                os.write(result, b"written")
                try:
                    os.open("intruder", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=in_fd)
                    code = 4
                except PermissionError:  # expected: in/ is read-only for the sandbox uid
                    pass
            except BaseException:
                code = 2
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        written = os.stat(run.path / "out" / "result.txt")
        self.assertEqual((written.st_uid, written.st_gid), (10001, 10001))


@needs_fd_ops
class CopyBackTests(ExecutorTestBase):
    def job_out(self):
        return self.work / JOB / "out"

    def test_output_files_are_copied_to_job_out_run_dir_with_agent_ownership(self):
        result = self.run_request(argv=["writeout", "reports/found.txt", "payload"])
        dest = self.job_out() / result["run_id"] / "reports" / "found.txt"
        self.assertEqual(dest.read_text(encoding="utf-8"), "payload")
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o644)
        self.assertEqual(os.stat(dest).st_uid, self.uid)
        self.assertEqual(result["output_dir"], f"out/{result['run_id']}")
        self.assertEqual(result["output_files"], 1)
        self.assertFalse(result["output_truncated"])

    def test_a_symlink_the_container_wrote_is_not_copied_back(self):
        secret = self.outside / "host-secret"
        secret.write_text("TOPSECRET", encoding="utf-8")
        result = self.run_request(argv=["linkout", "innocent.txt", str(secret)])
        copied = self.job_out() / result["run_id"]
        self.assertEqual(list(copied.iterdir()), [])
        self.assertEqual(result["output_skipped"], 1)

    def test_a_symlink_planted_at_the_destination_is_never_followed(self):
        result = self.run_request(argv=["plantlink", str(self.job_out()), str(self.outside)])
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertEqual(result["status"], "ok")
        self.assertIn("output_error", result)

    def test_swapping_out_for_a_symlink_mid_run_cannot_redirect_the_copy(self):
        # The agent moves out/ aside and plants a symlink in its place while the container runs.
        result = self.run_request(argv=["swapout", str(self.job_out()), str(self.outside)])
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertEqual(result["status"], "ok")

    def test_swap_cannot_redirect_an_output_file_either(self):
        # Same swap, but the container also produced a file to copy back.
        original = self.executor.collect_output

        def swap_then_collect(cfg, run):
            out = self.job_out()
            os.rename(out, str(out) + "-moved")
            os.symlink(self.outside, out)
            return original(cfg, run)

        with mock.patch.object(self.executor, "collect_output", swap_then_collect):
            result = self.run_request(argv=["writeout", "a.txt", "payload"])
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertEqual(result["output_files"], 1)

    def test_output_is_capped_per_run(self):
        with mock.patch.object(self.executor, "MAX_OUTPUT_BYTES", 100):
            result = self.run_request(argv=["writeout", "big.bin", "x" * 200])
        self.assertTrue(result["output_truncated"])
        total = sum(p.stat().st_size for p in (self.job_out() / result["run_id"]).rglob("*") if p.is_file())
        self.assertLessEqual(total, 100)

    def test_existing_files_in_job_out_are_never_overwritten(self):
        keep = self.job_out() / "keep.txt"
        keep.write_text("mine", encoding="utf-8")
        self.run_request(argv=["writeout", "keep.txt", "container"])
        self.assertEqual(keep.read_text(encoding="utf-8"), "mine")


class RequestFileTests(ExecutorTestBase):
    def write_request(self, name, body):
        path = self.requests / name
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return path

    def result(self, name):
        return json.loads((self.results / name).read_text(encoding="utf-8"))

    @needs_fd_ops
    def test_valid_file_runs_writes_result_and_consumes_request(self):
        path = self.write_request("first.json", self.request(argv=["echo", "ok"]))
        self.executor.process_request_file(self.cfg, path)
        self.assertEqual(self.result("first.json")["status"], "ok")
        self.assertEqual(self.result("first.json")["job_id"], JOB)
        self.assertFalse(path.exists())

    @needs_fd_ops
    def test_active_job_request_cannot_name_or_touch_another_job(self):
        other = "job-20261116-9999"
        other_job = self.make_job(other)
        (other_job / "target" / "secret.txt").write_text("OTHER-JOB-SECRET", encoding="utf-8")
        path = self.write_request(
            "cross-job.json", self.request(job_id=other, argv=["catin", "secret.txt"]))
        self.executor.process_request_file(self.cfg, path)
        result = self.result("cross-job.json")
        self.assertEqual(result, {
            "status": "rejected",
            "error": "request job_id does not match the active job",
        })
        self.assertEqual(self.calls(), [])
        self.assertEqual(list((other_job / "out").iterdir()), [])

    def test_invalid_request_is_rejected_without_launching_docker(self):
        path = self.write_request("bad.json", self.request(network="host"))
        self.executor.process_request_file(self.cfg, path)
        outcome = self.result("bad.json")
        self.assertEqual(outcome["status"], "rejected")
        self.assertIn("network", outcome["error"])
        self.assertEqual(self.calls(), [])

    def test_malformed_json_is_rejected_and_content_is_not_echoed(self):
        path = self.write_request("junk.json", "{ SECRET-NOT-JSON ")
        self.executor.process_request_file(self.cfg, path)
        text = (self.results / "junk.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(text)["status"], "rejected")
        self.assertNotIn("SECRET-NOT-JSON", text)

    def test_oversized_request_is_rejected(self):
        path = self.write_request("huge.json", " " * (self.executor.MAX_REQUEST_BYTES + 1))
        self.executor.process_request_file(self.cfg, path)
        self.assertEqual(self.result("huge.json")["status"], "rejected")
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "symlink creation needs POSIX")
    def test_symlinked_request_file_is_rejected(self):
        secret = Path(self.tmp.name) / "host-secret.json"
        secret.write_text(json.dumps(self.request()), encoding="utf-8")
        link = self.requests / "linked.json"
        os.symlink(secret, link)
        self.executor.process_request_file(self.cfg, link)
        self.assertEqual(self.result("linked.json")["status"], "rejected")
        self.assertEqual(self.calls(), [])

    def test_unsafe_request_names_are_ignored(self):
        for name in (".hidden.json", "no-extension", "has space.json"):
            with self.subTest(name=name):
                path = self.write_request(name, self.request())
                self.executor.process_pending(self.cfg)
                self.assertTrue(path.exists())
                self.assertEqual(list(self.results.iterdir()), [])
                path.unlink()

    @needs_fd_ops
    def test_process_pending_handles_each_file_once(self):
        self.write_request("a-one.json", self.request(argv=["echo", "1"]))
        self.write_request("b-two.json", self.request(argv=["echo", "2"]))
        self.assertEqual(self.executor.process_pending(self.cfg), 2)
        self.assertEqual(self.result("a-one.json")["stdout"], "1\n")
        self.assertEqual(self.result("b-two.json")["stdout"], "2\n")
        self.assertEqual(self.executor.process_pending(self.cfg), 0)

    def test_a_request_that_vanishes_is_skipped_without_killing_the_service(self):
        gone = self.requests / "gone.json"
        self.assertIsNone(self.executor.process_request_file(self.cfg, gone))
        self.assertEqual(list(self.results.iterdir()), [])
        # The directory listing named it, then the agent (or a retry) removed it before it was read.
        self.write_request("b-two.json", self.request(network="host"))
        with mock.patch.object(os, "listdir", return_value=["gone.json", "b-two.json"]):
            self.executor.process_pending(self.cfg)
        self.assertEqual(self.result("b-two.json")["status"], "rejected")

    def test_a_result_write_failure_is_logged_and_the_request_is_not_retried_forever(self):
        first = self.write_request("a-one.json", self.request(network="host"))
        second = self.write_request("b-two.json", self.request(network="host"))
        real_write = self.executor._write_result

        def flaky(cfg, name, body):
            if name == "a-one.json":
                raise OSError(28, "No space left on device: SECRET-PATH")
            return real_write(cfg, name, body)

        stderr = io.StringIO()
        with mock.patch.object(self.executor, "_write_result", flaky), contextlib.redirect_stderr(stderr):
            self.executor.process_pending(self.cfg)
        self.assertFalse(first.exists(), "an unanswerable request must be consumed, not rerun every poll")
        self.assertFalse(second.exists())
        self.assertEqual(self.result("b-two.json")["status"], "rejected")
        self.assertIn("result write failed", stderr.getvalue())
        self.assertNotIn("SECRET-PATH", stderr.getvalue())


class ConfigTests(ExecutorTestBase):
    def test_image_must_be_content_addressed(self):
        for image in ("ubuntu:latest", "re-lab-agent:local", "sha256:abc", ""):
            with self.subTest(image=image):
                with self.assertRaises(self.executor.RequestError):
                    self.executor.Config(
                        image=image, work_root=self.work, requests_dir=self.requests,
                        results_dir=self.results, active_job_id=JOB, runs_dir=self.runs)

    def test_digest_pinned_repository_reference_is_accepted(self):
        cfg = self.executor.Config(
            image="re-lab-agent@sha256:" + "b" * 64, work_root=self.work,
            requests_dir=self.requests, results_dir=self.results,
            active_job_id=JOB, runs_dir=self.runs)
        self.assertTrue(cfg.image.endswith("b" * 64))

    def test_runs_dir_defaults_to_the_exec_runs_root_and_agent_uid_to_10001(self):
        cfg = self.executor.Config(
            image=IMAGE, work_root=self.work, requests_dir=self.requests,
            results_dir=self.results, active_job_id=JOB)
        self.assertEqual(cfg.runs_dir, Path("/srv/re-lab/exec/runs"))
        self.assertEqual((cfg.agent_uid, cfg.agent_gid), (10001, 10001))

    def test_executor_is_an_imported_library_not_a_second_service_loop(self):
        self.assertFalse(hasattr(self.executor, "load_config"))
        self.assertFalse(hasattr(self.executor, "main"))



@needs_fd_ops
class CopyFileRaceGuardTests(ExecutorTestBase):
    """_copy_file itself must refuse a link even if one is swapped in after the directory scan."""

    def dirs(self):
        src, dst = Path(self.tmp.name) / "race-src", Path(self.tmp.name) / "race-dst"
        src.mkdir()
        dst.mkdir()
        flags = os.O_RDONLY | os.O_DIRECTORY
        src_fd, dst_fd = os.open(src, flags), os.open(dst, flags)
        self.addCleanup(os.close, src_fd)
        self.addCleanup(os.close, dst_fd)
        return src, dst, src_fd, dst_fd

    def budget(self):
        return self.executor._Budget(1024, 10, "test")

    def test_source_symlink_is_not_opened_even_when_named_directly(self):
        src, dst, src_fd, dst_fd = self.dirs()
        secret = self.outside / "host-secret"
        secret.write_text("TOPSECRET", encoding="utf-8")
        os.symlink(secret, src / "swapped")
        with self.assertRaises(OSError):
            self.executor._copy_file(src_fd, dst_fd, "swapped", self.budget(), strict=True,
                                     owner=None, mode=0o644)
        self.assertEqual(list(dst.iterdir()), [])

    def test_destination_symlink_is_not_written_through(self):
        src, dst, src_fd, dst_fd = self.dirs()
        (src / "f").write_text("payload", encoding="utf-8")
        victim = self.outside / "victim"
        victim.write_text("original", encoding="utf-8")
        os.symlink(victim, dst / "f")
        with self.assertRaises(FileExistsError):
            self.executor._copy_file(src_fd, dst_fd, "f", self.budget(), strict=False,
                                     owner=None, mode=0o644)
        self.assertEqual(victim.read_text(encoding="utf-8"), "original")


class FinalReviewContainmentTests(ExecutorTestBase):
    def test_containment_requires_inspect_to_confirm_not_found(self):
        run = self.runs / RUN_ID
        run.mkdir()
        cid = "ab" * 32
        (run / "cid").write_text(cid, encoding="ascii")

        def docker(argv, **_kwargs):
            command = argv[len(self.cfg.docker_prefix)]
            if command == "inspect":
                return subprocess_completed(argv, 1, stderr=b"Error: No such object: " + cid.encode())
            return subprocess_completed(argv, 1)

        with mock.patch.object(self.executor.subprocess, "run", side_effect=docker) as called:
            self.executor.contain_container(self.cfg.docker_prefix, run / "cid")
        self.assertEqual(
            [call.args[0][len(self.cfg.docker_prefix)] for call in called.call_args_list],
            ["kill", "rm", "inspect"])

    def test_containment_raises_when_container_still_exists(self):
        run = self.runs / RUN_ID
        run.mkdir()
        (run / "cid").write_text("ab" * 32, encoding="ascii")
        present = subprocess_completed(["docker", "inspect"], 0)
        with mock.patch.object(self.executor.subprocess, "run", return_value=present):
            with self.assertRaises(self.executor.ContainmentError):
                self.executor.contain_container(self.cfg.docker_prefix, run / "cid")

    def test_containment_rejects_nonstandard_not_found_exit_code(self):
        run = self.runs / RUN_ID
        run.mkdir()
        cid = "ab" * 32
        (run / "cid").write_text(cid, encoding="ascii")

        def docker(argv, **_kwargs):
            command = argv[len(self.cfg.docker_prefix)]
            if command == "inspect":
                return subprocess_completed(
                    argv, 2, stderr=b"Error: No such object: " + cid.encode())
            return subprocess_completed(argv, 1)

        with mock.patch.object(self.executor.subprocess, "run", side_effect=docker):
            with self.assertRaises(self.executor.ContainmentError):
                self.executor.contain_container(self.cfg.docker_prefix, run / "cid")

    def test_containment_raises_when_started_client_has_no_readable_cidfile(self):
        with self.assertRaises(self.executor.ContainmentError):
            self.executor.contain_container(self.cfg.docker_prefix, self.runs / "missing-cid")

    def test_pending_requests_stop_before_starting_the_next_request(self):
        for name in ("a.json", "b.json", "c.json"):
            (self.requests / name).write_text(json.dumps(self.request()), encoding="utf-8")
        stop = threading.Event()
        started = []

        def process(_cfg, path, stop_event=None):
            started.append(path.name)
            stop.set()
            return {"status": "ok"}

        with mock.patch.object(self.executor, "process_request_file", side_effect=process):
            handled = self.executor.process_pending(self.cfg, stop)
        self.assertEqual(handled, 1)
        self.assertEqual(started, ["a.json"])

    def test_running_request_is_contained_and_cancelled_when_stopped(self):
        request = self.executor.validate_request(self.request())
        run = mock.Mock(run_id=RUN_ID, path=self.runs / RUN_ID)
        stop = threading.Event()
        proc = mock.Mock()
        proc.stdout = io.BytesIO()
        proc.stderr = io.BytesIO()

        def wait(*_args, **_kwargs):
            if not stop.is_set():
                stop.set()
                raise self.executor.subprocess.TimeoutExpired(["docker"], 0.1)
            return 137

        proc.wait.side_effect = wait
        with mock.patch.object(self.executor.subprocess, "Popen", return_value=proc), \
             mock.patch.object(self.executor, "contain_container") as contain:
            result = self.executor._launch(self.cfg, request, run, stop)
        self.assertEqual(result["status"], "cancelled")
        contain.assert_called_once_with(self.cfg.docker_prefix, run.path / "cid")

    def test_containment_failure_reaps_client_holding_capture_pipes(self):
        request = self.executor.validate_request(self.request(timeout_s=1))
        run = mock.Mock(run_id=RUN_ID, path=self.runs / RUN_ID)
        client = self.executor.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=self.executor.subprocess.PIPE,
            stderr=self.executor.subprocess.PIPE)
        errors = []

        def launch():
            try:
                self.executor._launch(self.cfg, request, run)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(self.executor.subprocess, "Popen", return_value=client), \
             mock.patch.object(
                 self.executor, "contain_container",
                 side_effect=self.executor.ContainmentError("uncertain")):
            worker = threading.Thread(target=launch, daemon=True)
            worker.start()
            worker.join(2.5)
            blocked = worker.is_alive()
            if blocked:
                client.kill()
                client.wait(timeout=5)
                worker.join(5)
        self.assertFalse(blocked, "containment failure left the Docker client pipes blocked")
        self.assertIsNotNone(client.poll())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], self.executor.ContainmentError)

    @needs_fd_ops
    def test_sweep_runs_quarantines_scratch_it_cannot_remove(self):
        stale = self.runs / RUN_ID
        stale.mkdir()
        with mock.patch.object(
                self.executor, "_rmtree_force", side_effect=PermissionError("blocked")):
            self.executor.sweep_runs(self.cfg)
        self.assertFalse(stale.exists())
        quarantined = [path.name for path in self.runs.iterdir()]
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(quarantined[0].startswith(".quarantine-"))
        with mock.patch.object(
                self.executor, "_rmtree_force", side_effect=PermissionError("blocked")):
            self.executor.sweep_runs(self.cfg)  # an old quarantine entry is reported, not fatal

    @needs_fd_ops
    def test_sweep_runs_raises_when_scratch_can_be_neither_removed_nor_quarantined(self):
        (self.runs / RUN_ID).mkdir()
        with mock.patch.object(
                self.executor, "_rmtree_force", side_effect=PermissionError("blocked")), \
             mock.patch.object(self.executor.os, "rename", side_effect=PermissionError("blocked")):
            with self.assertRaises(self.executor.ContainmentError):
                self.executor.sweep_runs(self.cfg)

    def test_unexpected_client_wait_error_contains_before_failing(self):
        request = self.executor.validate_request(self.request())
        run = mock.Mock(run_id=RUN_ID, path=self.runs / RUN_ID)
        proc = mock.Mock()
        proc.stdout = io.BytesIO()
        proc.stderr = io.BytesIO()
        proc.wait.side_effect = [OSError("client failed"), 0]
        with mock.patch.object(self.executor.subprocess, "Popen", return_value=proc), \
             mock.patch.object(self.executor, "contain_container") as contain:
            with self.assertRaises(self.executor.ContainmentError):
                self.executor._launch(self.cfg, request, run)
        contain.assert_called_once_with(self.cfg.docker_prefix, run.path / "cid")
        proc.kill.assert_called()

    def test_client_reap_timeout_is_a_containment_failure(self):
        proc = mock.Mock()
        proc.wait.side_effect = self.executor.subprocess.TimeoutExpired(["docker"], 1)
        with self.assertRaises(self.executor.ContainmentError):
            self.executor.stop_docker_client(proc)

    def test_capture_construction_failure_contains_and_reaps(self):
        request = self.executor.validate_request(self.request())
        run = mock.Mock(run_id=RUN_ID, path=self.runs / RUN_ID)
        proc = mock.Mock()
        proc.stdout = io.BytesIO()
        proc.stderr = io.BytesIO()
        proc.wait.return_value = 137
        with mock.patch.object(self.executor.subprocess, "Popen", return_value=proc), \
             mock.patch.object(self.executor, "_Capture", side_effect=MemoryError("exhausted")), \
             mock.patch.object(self.executor, "contain_container") as contain:
            with self.assertRaises(self.executor.ContainmentError):
                self.executor._launch(self.cfg, request, run)
        contain.assert_called_once_with(self.cfg.docker_prefix, run.path / "cid")
        proc.kill.assert_called()
        proc.wait.assert_called()

    def test_deep_request_is_rejected_and_consumed(self):
        path = self.requests / "deep.json"
        path.write_bytes(b"[" * 1100 + b"0" + b"]" * 1100)
        result = self.executor.process_request_file(self.cfg, path)
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(path.exists())
        self.assertTrue((self.results / path.name).is_file())

    @unittest.skipUnless(os.name == "posix", "requires POSIX file names")
    def test_request_name_with_final_newline_is_ignored(self):
        path = self.requests / "request.json\n"
        path.write_text(json.dumps(self.request()), encoding="utf-8")
        with mock.patch.object(self.executor, "process_request_file") as process:
            self.assertEqual(self.executor.process_pending(self.cfg), 0)
        process.assert_not_called()


def subprocess_completed(argv, returncode, stdout=b"", stderr=b""):
    return mock.Mock(args=argv, returncode=returncode, stdout=stdout, stderr=stderr)


class IncrementalCopyWalkTests(ExecutorTestBase):
    def test_twenty_thousand_entries_stop_at_the_cap_without_materializing_the_directory(self):
        scanned = {"count": 0}

        class Entry:
            name = "special"

            def stat(self, *, follow_symlinks=False):
                self.assert_false(follow_symlinks)
                return mock.Mock(st_mode=stat.S_IFIFO)

            @staticmethod
            def assert_false(value):
                if value:
                    raise AssertionError("follow_symlinks must be false")

        class Entries:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                for _ in range(20001):
                    scanned["count"] += 1
                    yield Entry()

        budget = self.executor._Budget(1024, 10000, "job output")
        with mock.patch.object(os, "scandir", return_value=Entries()), \
             mock.patch.object(os, "listdir",
                               side_effect=AssertionError("directory was materialized")):
            with self.assertRaises(self.executor._LimitExceeded):
                self.executor._copy_tree(
                    3, 4, budget, strict=False, owner=None,
                    file_mode=0o644, dir_mode=0o755)
        self.assertEqual(scanned["count"], 10001)
        self.assertEqual(budget.skipped, 10000)


if __name__ == "__main__":
    unittest.main()
