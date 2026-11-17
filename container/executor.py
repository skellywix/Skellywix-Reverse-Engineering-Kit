#!/usr/bin/env python3
"""Execution-request library imported by the trusted host dispatcher.

The dispatcher gives this module only the active job's request and result
directories. Each closed-schema request can choose a command vector inside the
fixed execution image, a timeout, and a memory limit. This module copies the
root-owned submitted target into a private run directory, starts one fixed-flag
networkless container without a shell, copies bounded output back through pinned
descriptors, writes a bounded result, and consumes the request.

The executor never bind-mounts an agent-writable source. A private
``runs/<run-id>/{in,out}`` holds the immutable input copy and writable output.
The input walk is fd-relative and uses ``O_NOFOLLOW``; links and special files
are refused. The run input preserves source execute bits and is mounted at
``/job/in`` without ``noexec``. The container's ``/tmp`` is a noexec tmpfs.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
IMAGE_RE = re.compile(r"^(sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64})$")
REQUEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.json$")
ALLOWED_KEYS = frozenset({"job_id", "argv", "timeout_s", "memory_mb"})
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 1, 600, 60
MEMORY_MIN, MEMORY_MAX, MEMORY_DEFAULT = 64, 1024, 512
MAX_ARGV_ITEMS = 256
MAX_ARG_BYTES = 8192
MAX_REQUEST_BYTES = 64 * 1024
OUTPUT_CAP_BYTES = 64 * 1024
KILL_GRACE_S = 10
POLL_INTERVAL_S = 0.1
MAX_INPUT_BYTES = 2 * 1024 ** 3
MAX_OUTPUT_BYTES = 64 * 1024 ** 2
MAX_ENTRIES = 10000
MAX_DEPTH = 32
COPY_CHUNK = 1024 * 1024


class RequestError(Exception):
    """A request, job directory or config the executor refuses. Message is safe to return."""

class ContainmentError(Exception):
    """A started container could not be proven absent."""


def _safe(text: object, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "?", str(text))[:limit]


@dataclass(frozen=True)
class Config:
    image: str
    work_root: Path
    requests_dir: Path
    results_dir: Path
    active_job_id: str
    runs_dir: Path = Path("/srv/re-lab/exec/runs")
    agent_uid: int = 10001
    agent_gid: int = 10001
    docker_prefix: tuple = field(default=("docker",))

    def __post_init__(self):
        if not isinstance(self.image, str) or not IMAGE_RE.match(self.image):
            raise RequestError("image must be content-addressed (sha256:<64 hex> or repo@sha256:<64 hex>)")
        object.__setattr__(self, "work_root", Path(self.work_root))
        object.__setattr__(self, "requests_dir", Path(self.requests_dir))
        object.__setattr__(self, "results_dir", Path(self.results_dir))
        object.__setattr__(self, "runs_dir", Path(self.runs_dir))
        object.__setattr__(self, "docker_prefix", tuple(self.docker_prefix))
        if not isinstance(self.active_job_id, str) or not JOB_ID_RE.fullmatch(self.active_job_id):
            raise RequestError("active_job_id must match ^[a-z0-9-]{8,64}$")
        if type(self.agent_uid) is not int or type(self.agent_gid) is not int:
            raise RequestError("agent_uid and agent_gid must be integers")
        for other in (self.work_root, self.requests_dir, self.results_dir):
            if (self.runs_dir == other or self.runs_dir in other.parents or other in self.runs_dir.parents):
                raise RequestError("runs_dir must not overlap work_root, requests_dir or results_dir")


@dataclass(frozen=True)
class ExecRequest:
    job_id: str
    argv: tuple
    timeout_s: int
    memory_mb: int


def _bounded_int(body: dict, key: str, low: int, high: int, default: int) -> int:
    value = body.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise RequestError(f"{key} must be an integer between {low} and {high}")
    return value


def validate_request(body: object) -> ExecRequest:
    if not isinstance(body, dict):
        raise RequestError("request must be a JSON object")
    unknown = sorted(set(body) - ALLOWED_KEYS)
    if unknown:
        raise RequestError("unknown request key: " + ", ".join(_safe(k) for k in unknown[:5]))
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise RequestError("job_id must match ^[a-z0-9-]{8,64}$")
    argv = body.get("argv")
    if (not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV_ITEMS
            or not all(isinstance(a, str) and "\x00" not in a and len(a.encode("utf-8", "replace")) <= MAX_ARG_BYTES
                       for a in argv)):
        raise RequestError("argv must be a non-empty list of strings without NUL bytes")
    if not argv[0]:
        raise RequestError("argv[0] must not be empty")
    return ExecRequest(
        job_id=job_id, argv=tuple(argv),
        timeout_s=_bounded_int(body, "timeout_s", TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT),
        memory_mb=_bounded_int(body, "memory_mb", MEMORY_MIN, MEMORY_MAX, MEMORY_DEFAULT))


DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
FILE_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                   | getattr(os, "O_CLOEXEC", 0))
FILE_CREATE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
JOB_COPY_ERROR = "job directory is not claimed, lacks real in/ and out/, or could not be copied safely"


class _LimitExceeded(RequestError):
    """A copy cap (bytes, entries, depth) was hit."""


class _Budget:
    def __init__(self, max_bytes: int, max_entries: int, what: str):
        self.bytes_left, self.entries_left, self.what = max_bytes, max_entries, what
        self.files = 0
        self.skipped = 0

    def entry(self) -> None:
        if self.entries_left <= 0:
            raise _LimitExceeded(f"{self.what} exceeds the entry limit")
        self.entries_left -= 1

    def take_bytes(self, count: int) -> None:
        if count > self.bytes_left:
            raise _LimitExceeded(f"{self.what} exceeds the size limit")
        self.bytes_left -= count


def _require_fd_ops() -> None:
    ok = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and hasattr(os, "fchown")
          and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
          and os.stat in os.supports_dir_fd and os.unlink in os.supports_dir_fd
          and os.scandir in os.supports_fd)
    if not ok:
        raise RequestError("this platform lacks the fd-relative file operations the executor requires")


def _check_runs_root(cfg: Config) -> None:
    """The runs root must be a real directory owned by us with no group/other access."""
    try:
        info = os.lstat(cfg.runs_dir)
    except OSError:
        raise RequestError("runs directory is not available") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or re.search(r"[:,]", str(cfg.runs_dir))):
        raise RequestError("runs directory must be a private real directory owned by the executor")


def _finish_dir(fd: int, owner: tuple | None, mode: int) -> None:
    if owner is not None:
        os.fchown(fd, *owner)
    os.fchmod(fd, mode)


def _copy_file(src_dir: int, dst_dir: int, name: str, budget: _Budget, *, strict: bool,
               owner: tuple | None, mode: int, exec_mode: int | None = None) -> bool:
    """Copy one regular file by name; return False when it was skipped (non-strict, not regular)."""
    src = os.open(name, FILE_READ_FLAGS, dir_fd=src_dir)
    try:
        if not stat.S_ISREG(os.fstat(src).st_mode):
            if strict:
                raise RequestError("job input contains a link or special file")
            return False
        dst = os.open(name, FILE_CREATE_FLAGS, 0o600, dir_fd=dst_dir)
        try:
            try:
                while chunk := os.read(src, COPY_CHUNK):
                    budget.take_bytes(len(chunk))
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(dst, view):]
                if owner is not None:
                    os.fchown(dst, *owner)
                os.fchmod(dst, exec_mode if exec_mode is not None and os.fstat(src).st_mode & 0o111 else mode)
            except BaseException:
                os.unlink(name, dir_fd=dst_dir)
                raise
        finally:
            os.close(dst)
    finally:
        os.close(src)
    budget.files += 1
    return True


def _copy_tree(src_fd: int, dst_fd: int, budget: _Budget, *, strict: bool, owner: tuple | None,
               file_mode: int, dir_mode: int, exec_mode: int | None = None, depth: int = 0) -> None:
    """Incrementally copy a tree without following links or materializing its directory entries."""
    with os.scandir(src_fd) as entries:
        for entry in entries:
            budget.entry()  # Every entry counts, including links and special files.
            name = entry.name
            kind = stat.S_IFMT(entry.stat(follow_symlinks=False).st_mode)
            if kind == stat.S_IFDIR:
                if depth + 1 > MAX_DEPTH:
                    raise _LimitExceeded(f"{budget.what} is nested too deeply")
                child_src = os.open(name, DIR_FLAGS, dir_fd=src_fd)
                try:
                    os.mkdir(name, 0o700, dir_fd=dst_fd)
                    child_dst = os.open(name, DIR_FLAGS, dir_fd=dst_fd)
                    try:
                        try:
                            _copy_tree(
                                child_src, child_dst, budget, strict=strict, owner=owner,
                                file_mode=file_mode, exec_mode=exec_mode,
                                dir_mode=dir_mode, depth=depth + 1)
                        finally:
                            _finish_dir(child_dst, owner, dir_mode)
                    finally:
                        os.close(child_dst)
                finally:
                    os.close(child_src)
            elif kind == stat.S_IFREG:
                _copy_file(
                    src_fd, dst_fd, name, budget, strict=strict, owner=owner,
                    mode=file_mode, exec_mode=exec_mode)
            elif strict:
                raise RequestError("job input contains a link or special file")
            else:
                budget.skipped += 1


def _retry_after_chmod(func, path, exc) -> None:
    """rmtree error handler: the read-only in/ copy blocks deletion until its parent is writable."""
    if isinstance(exc, FileNotFoundError):
        return
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(os.path.dirname(path), 0o700)
    func(path)


def _rmtree_force(path: Path) -> None:
    shutil.rmtree(path, onexc=_retry_after_chmod)


@dataclass
class Run:
    """Per-request private scratch under the runs root plus the pinned job-dir descriptors."""
    run_id: str
    path: Path
    work_fd: int = -1
    job_fd: int = -1
    out_fd: int = -1

    def discard(self) -> None:
        for attr in ("out_fd", "job_fd", "work_fd"):
            fd = getattr(self, attr)
            if fd >= 0:
                os.close(fd)
                setattr(self, attr, -1)
        try:
            _rmtree_force(self.path)
        except FileNotFoundError:
            pass  # prepare_run failed before it created the run dir; nothing to remove
        except OSError as exc:
            print(f"re-executor: run dir cleanup failed: {type(exc).__name__}", file=sys.stderr, flush=True)


def prepare_run(cfg: Config, request: ExecRequest) -> Run:
    """Pin the job dir through O_NOFOLLOW dirfds and copy its target/ into a private run dir.

    The sandbox runs as the agent uid: in/ is a root-owned read-only copy (so it can read it but
    never change it), out/ belongs to that uid alone, and the run root stays root-only."""
    _require_fd_ops()
    _check_runs_root(cfg)
    run_id = uuid.uuid4().hex
    run = Run(run_id, cfg.runs_dir / run_id)
    try:
        run.work_fd = os.open(cfg.work_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        run.job_fd = os.open(cfg.active_job_id, DIR_FLAGS, dir_fd=run.work_fd)
        run.out_fd = os.open("out", DIR_FLAGS, dir_fd=run.job_fd)
        src_in = os.open("target", DIR_FLAGS, dir_fd=run.job_fd)
        try:
            for path in (run.path, run.path / "in", run.path / "out"):
                os.mkdir(path, 0o700)
            os.chown(run.path / "out", cfg.agent_uid, cfg.agent_gid, follow_symlinks=False)
            dst_in = os.open(run.path / "in", DIR_FLAGS)
            try:
                try:
                    _copy_tree(src_in, dst_in, _Budget(MAX_INPUT_BYTES, MAX_ENTRIES, "job input"),
                               strict=True, owner=None, file_mode=0o444, exec_mode=0o555, dir_mode=0o555)
                finally:
                    _finish_dir(dst_in, None, 0o555)
            finally:
                os.close(dst_in)
        finally:
            os.close(src_in)
    except BaseException as exc:
        run.discard()
        if isinstance(exc, OSError):
            raise RequestError(JOB_COPY_ERROR) from None
        raise
    return run


def collect_output(cfg: Config, run: Run) -> dict:
    """Copy regular files from the private out/ to work/<job>/out/<run-id>/ as the agent uid."""
    owner = (cfg.agent_uid, cfg.agent_gid)
    budget = _Budget(MAX_OUTPUT_BYTES, MAX_ENTRIES, "job output")
    truncated = False
    src = os.open(run.path / "out", DIR_FLAGS)
    try:
        os.mkdir(run.run_id, 0o700, dir_fd=run.out_fd)
        dst = os.open(run.run_id, DIR_FLAGS, dir_fd=run.out_fd)
        try:
            try:
                _copy_tree(src, dst, budget, strict=False, owner=owner, file_mode=0o644, dir_mode=0o755)
            except _LimitExceeded:
                truncated = True
            finally:
                _finish_dir(dst, owner, 0o755)
        finally:
            os.close(dst)
    finally:
        os.close(src)
    return {"output_dir": f"out/{run.run_id}", "output_files": budget.files,
            "output_skipped": budget.skipped, "output_truncated": truncated}


def sweep_runs(cfg: Config) -> None:
    """Remove run dirs a crashed executor left behind. Call only while no request is in flight."""
    _check_runs_root(cfg)
    for name in os.listdir(cfg.runs_dir):
        path = cfg.runs_dir / name
        try:
            if stat.S_ISDIR(os.lstat(path).st_mode):
                _rmtree_force(path)
            else:
                os.unlink(path)
        except OSError as exc:
            raise ContainmentError(
                f"stale run cleanup failed: {type(exc).__name__}") from None

def build_argv(cfg: Config, request: ExecRequest, run_id: str) -> list:
    """The one fixed docker argv. Mount sources are derived from the runs root and a uuid hex only."""
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise RequestError("run id must be 32 lowercase hex characters")
    run_dir = cfg.runs_dir / run_id
    return [
        *cfg.docker_prefix, "run", "--rm", "--runtime=runsc", "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", f"--user={cfg.agent_uid}:{cfg.agent_gid}",
        "--label", "re-lab.role=exec", "--label", f"re-lab.job={request.job_id}",
        "--log-driver=none", "--cidfile", str(run_dir / "cid"), "--pids-limit=256",
        f"--memory={request.memory_mb}m", "--cpus=1", "--tmpfs",
        "/tmp:size=256m,noexec,nosuid,nodev",
        "-v", f"{run_dir / 'in'}:/job/in:ro", "-v", f"{run_dir / 'out'}:/job/out:rw",
        cfg.image, *request.argv]


class _Capture(threading.Thread):
    """Keep at most OUTPUT_CAP_BYTES of a pipe and drain the rest."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream, self.data, self.truncated = stream, bytearray(), False

    def run(self):
        while chunk := self.stream.read(65536):
            room = OUTPUT_CAP_BYTES - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > room:
                self.truncated = True


def _container_id(cidfile: Path) -> str:
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(cidfile, flags)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ContainmentError("container cidfile is not regular")
            raw = handle.read(65)
    except (OSError, ValueError) as exc:
        raise ContainmentError(
            f"container cidfile is unreadable: {type(exc).__name__}") from None
    try:
        container_id = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ContainmentError("container cidfile is invalid") from None
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ContainmentError("container cidfile is invalid")
    return container_id


def contain_container(docker_prefix: tuple, cidfile: Path) -> None:
    """Kill, remove and prove absent the exact container recorded in ``cidfile``."""
    container_id = _container_id(cidfile)
    try:
        subprocess.run(
            [*docker_prefix, "kill", container_id], capture_output=True,
            timeout=KILL_GRACE_S, check=False)
        subprocess.run(
            [*docker_prefix, "rm", "-f", container_id], capture_output=True,
            timeout=KILL_GRACE_S, check=False)
        inspected = subprocess.run(
            [*docker_prefix, "inspect", container_id], capture_output=True,
            timeout=KILL_GRACE_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContainmentError(
            f"container containment command failed: {type(exc).__name__}") from None
    not_found = (
        inspected.returncode == 1
        and any(marker in inspected.stderr.lower()
                for marker in (b"no such object", b"no such container")))
    if not not_found:
        raise ContainmentError("container absence could not be confirmed")


def stop_docker_client(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError as exc:
        print(f"re-executor: docker client kill failed: {type(exc).__name__}",
              file=sys.stderr, flush=True)
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainmentError(
            f"docker client exit could not be confirmed: {type(exc).__name__}") from None


def _launch(
        cfg: Config, request: ExecRequest, run: Run,
        stop_event: threading.Event | None = None) -> dict:
    argv = build_argv(cfg, request, run.run_id)
    started = time.monotonic()
    result = {"job_id": request.job_id, "run_id": run.run_id}
    if stop_event is not None and stop_event.is_set():
        return {**result, "status": "cancelled", "exit_code": None,
                "stdout": "", "stdout_truncated": False,
                "stderr": "", "stderr_truncated": False, "duration_s": 0.0}
    readers = ()
    started_readers = []
    deadline = started + request.timeout_s
    try:
        proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return {**result, "status": "error", "error": "docker could not be started"}
    try:
        readers = (_Capture(proc.stdout), _Capture(proc.stderr))
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        while True:
            cancelled = stop_event is not None and stop_event.is_set()
            remaining = deadline - time.monotonic()
            if cancelled or remaining <= 0:
                contain_container(cfg.docker_prefix, run.path / "cid")
                stop_docker_client(proc)
                exit_code = None
                status = "cancelled" if cancelled else "timeout"
                break
            try:
                exit_code = proc.wait(timeout=min(POLL_INTERVAL_S, remaining))
                status = "ok" if exit_code == 0 else "failed"
                break
            except subprocess.TimeoutExpired:
                continue
    except ContainmentError:
        try:
            stop_docker_client(proc)
        except ContainmentError as stop_error:
            print(
                f"re-executor: secondary client cleanup failed: {type(stop_error).__name__}",
                file=sys.stderr, flush=True)
        raise
    except Exception as exc:
        failure = None
        try:
            contain_container(cfg.docker_prefix, run.path / "cid")
        except ContainmentError as containment_error:
            failure = containment_error
        try:
            stop_docker_client(proc)
        except ContainmentError as client_error:
            failure = failure or client_error
        if failure is not None:
            raise failure
        raise ContainmentError(
            f"execution lifecycle failed: {type(exc).__name__}") from None
    finally:
        for reader in started_readers:
            reader.join(KILL_GRACE_S)
        streams = (proc.stdout, proc.stderr)
        if not readers:
            for stream in streams:
                stream.close()
        else:
            for reader, stream in zip(readers, streams):
                if reader not in started_readers or not reader.is_alive():
                    stream.close()
    stdout, stderr = readers
    return {**result, "status": status, "exit_code": exit_code,
            "stdout": bytes(stdout.data).decode("utf-8", "replace"), "stdout_truncated": stdout.truncated,
            "stderr": bytes(stderr.data).decode("utf-8", "replace"), "stderr_truncated": stderr.truncated,
            "duration_s": round(time.monotonic() - started, 3)}


def execute(
        cfg: Config, request: ExecRequest,
        stop_event: threading.Event | None = None) -> dict:
    """Copy the job input into a private run dir, run the container on the copies, copy output back."""
    if request.job_id != cfg.active_job_id:
        raise RequestError("request job_id does not match the active job")
    run = prepare_run(cfg, request)  # RequestError here becomes a rejected result
    discard = True
    try:
        result = _launch(cfg, request, run, stop_event)
        if result["status"] != "error":
            try:
                result.update(collect_output(cfg, run))
            except (OSError, RequestError):
                result["output_error"] = "output could not be copied back safely"
        return result
    except ContainmentError:
        discard = False
        raise
    finally:
        if discard:
            run.discard()


def _write_result(cfg: Config, name: str, body: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=cfg.results_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
        os.chmod(tmp, 0o644)
        os.replace(tmp, cfg.results_dir / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _RequestGone(Exception):
    """The request file disappeared between the directory listing and the read."""



def _read_request(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise _RequestGone from None
    except OSError:
        raise RequestError("request is not a readable regular file") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RequestError("request is not a readable regular file")
        raw = handle.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestError("request exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RequestError("request is not valid JSON") from None


def process_request_file(
        cfg: Config, path: Path,
        stop_event: threading.Event | None = None) -> dict | None:
    """Run one request file and answer it; None when the file vanished before it could be read."""
    path = Path(path)
    result_name = path.name
    result: dict
    try:
        try:
            link = stat.S_ISLNK(os.lstat(path).st_mode)
        except FileNotFoundError:
            raise _RequestGone from None
        if link:
            raise RequestError("request is a symlink")
        request = validate_request(_read_request(path))
        result = execute(cfg, request, stop_event)
    except _RequestGone:
        return None
    except RequestError as exc:
        result = {"status": "rejected", "error": str(exc)}
    try:
        _write_result(cfg, result_name, result)
    except OSError as exc:
        # Consume the request anyway: an unanswerable request must not rerun on every poll.
        print(f"re-executor: result write failed: {type(exc).__name__}", file=sys.stderr, flush=True)
    path.unlink(missing_ok=True)
    return result


def process_pending(cfg: Config, stop_event: threading.Event | None = None) -> int:
    handled = 0
    for name in sorted(os.listdir(cfg.requests_dir)):
        if stop_event is not None and stop_event.is_set():
            break
        if not REQUEST_NAME_RE.fullmatch(name):
            continue
        if process_request_file(
                cfg, cfg.requests_dir / name, stop_event) is not None:
            handled += 1
    return handled


