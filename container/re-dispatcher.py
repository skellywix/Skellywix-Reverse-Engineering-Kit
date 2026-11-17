#!/usr/bin/env python3
"""Trusted host dispatcher for one isolated reverse-engineering job at a time."""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import executor

JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_TOP_LEVEL = frozenset({"target", "question.md", "job.json"})
MAX_INPUT_BYTES = 2 * 1024 ** 3
MAX_REPORT_BYTES = 5 * 1024 ** 2
MAX_EVIDENCE_FILE_BYTES = 25 * 1024 ** 2
MAX_EVIDENCE_BYTES = 200 * 1024 ** 2
MAX_SESSION_BYTES = 50 * 1024 ** 2
MAX_COST_SCAN_BYTES = 512 * 1024 ** 2
MAX_COST_LINE_BYTES = 8 * 1024 ** 2
MAX_MANIFEST_BYTES = 10 * 1024 ** 2
MAX_ENTRIES = 10000
MAX_DEPTH = 32
STATUS_MAX_BYTES = 4096
WALL_TIMEOUT_S = 2 * 3600 + 5 * 60
POLL_INTERVAL_S = 0.25
KILL_GRACE_S = 10
TEXT_SUFFIXES = frozenset({".md", ".txt", ".json", ".jsonl", ".log", ".csv", ".yaml", ".yml"})
ALLOWED_STATUSES = frozenset({"done", "cost-cap", "failed", "needs-vm-lane"})
DEFAULT_CONFIG = Path("/etc/re-lab/dispatcher.json")
# gVisor cannot reach Docker's embedded DNS on a user-defined network, so agents find the
# proxy through /etc/hosts. Must match re-egress's ipv4_address in compose.yaml.
EGRESS_IP = "172.30.97.2"


class DispatcherError(Exception):
    """The trusted dispatcher cannot safely process the job."""


class JobError(DispatcherError):
    """The claimed manifest or layout is invalid."""


class HashMismatch(DispatcherError):
    """Claimed bytes do not match the submitted manifest."""


class PublishError(DispatcherError):
    """Agent output cannot be published safely."""


@dataclass(frozen=True)
class Config:
    root: Path
    agent_image: str
    exec_image: str
    models_env: Path
    network: str = "re-lab_re-internal"
    agent_uid: int = 10001
    agent_gid: int = 10001
    report_gid: int = 10001
    docker_prefix: tuple = field(default=("docker",))

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "models_env", Path(self.models_env))
        object.__setattr__(self, "docker_prefix", tuple(self.docker_prefix))
        for name, image in (("agent_image", self.agent_image), ("exec_image", self.exec_image)):
            if not isinstance(image, str) or not executor.IMAGE_RE.fullmatch(image):
                raise DispatcherError(f"{name} must be content-addressed")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.network):
            raise DispatcherError("network has an unsupported shape")
        if (type(self.agent_uid) is not int or type(self.agent_gid) is not int
                or type(self.report_gid) is not int):
            raise DispatcherError("agent uid, agent gid and report gid must be integers")

    @property
    def inbox(self) -> Path:
        return self.root / "jobs" / "inbox"

    @property
    def work(self) -> Path:
        return self.root / "jobs" / "work"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def runs(self) -> Path:
        return self.root / "exec" / "runs"


@dataclass(frozen=True)
class AgentResult:
    exit_code: int | None
    timed_out: bool


@dataclass
class _ScanBudget:
    max_bytes: int
    max_entries: int = MAX_ENTRIES
    error_type: type[DispatcherError] = PublishError
    limit_name: str = "published output"
    bytes_used: int = 0
    entries: int = 0

    def entry(self) -> None:
        self.entries += 1
        if self.entries > self.max_entries:
            raise self.error_type(f"{self.limit_name} exceeds the entry limit")

    def fits(self, size: int) -> bool:
        return size >= 0 and self.bytes_used + size <= self.max_bytes

    def take(self, size: int) -> None:
        if not self.fits(size):
            raise self.error_type(f"{self.limit_name} exceeds the size limit")
        self.bytes_used += size


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise JobError("job id must match ^[a-z0-9-]{8,64}$")
    return job_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise HashMismatch("job contains a non-regular file")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_regular(base: Path, prefix: str, budget: _ScanBudget, depth: int = 0):
    if depth > MAX_DEPTH:
        raise HashMismatch("job files are nested too deeply")
    with os.scandir(base) as entries:
        for entry in entries:
            budget.entry()
            relative = f"{prefix}/{entry.name}"
            if entry.is_symlink():
                raise HashMismatch("symlink in job files")
            if entry.is_dir(follow_symlinks=False):
                yield from _walk_regular(Path(entry.path), relative, budget, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                info = entry.stat(follow_symlinks=False)
                budget.take(info.st_size)
                yield relative, Path(entry.path), info.st_size
            else:
                raise HashMismatch("special file in job files")


def load_and_verify_job(work: Path, job_id: str) -> dict:
    try:
        with os.scandir(work) as entries:
            names = {entry.name for entry in entries}
    except OSError:
        raise JobError("claimed job is unreadable") from None
    if names != JOB_TOP_LEVEL:
        raise JobError("claimed job must contain only target, question.md and job.json")
    try:
        raw = _read_regular(work / "job.json", MAX_MANIFEST_BYTES)
        if raw is None:
            raise JobError("job.json is missing or invalid")
        meta = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        raise JobError("job.json is missing or invalid") from None
    if not isinstance(meta, dict) or meta.get("job_id") != job_id or not isinstance(meta.get("files"), list):
        raise JobError("job.json does not match the claimed job")
    expected = {}
    for item in meta["files"]:
        if not isinstance(item, dict):
            raise JobError("job.json files entries must be objects")
        path, digest, size = item.get("path"), item.get("sha256"), item.get("size")
        parts = path.split("/") if isinstance(path, str) else []
        valid_path = path == "question.md" or (len(parts) >= 2 and parts[0] == "target")
        if (not valid_path or any(part in ("", ".", "..") for part in parts)
                or "\\" in path or path in expected
                or not isinstance(digest, str) or not HASH_RE.fullmatch(digest)
                or isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise JobError("job.json files entry is invalid")
        expected[path] = (digest, size)
    target = work / "target"
    if target.is_symlink() or not target.is_dir():
        raise HashMismatch("target directory is unavailable")
    budget = _ScanBudget(MAX_INPUT_BYTES, error_type=HashMismatch, limit_name="job input")
    actual = {path: (source, size) for path, source, size in _walk_regular(target, "target", budget)}
    question = work / "question.md"
    if question.is_symlink() or not question.is_file():
        raise HashMismatch("question.md is unavailable")
    question_size = question.stat().st_size
    budget.entry()
    budget.take(question_size)
    actual["question.md"] = (question, question_size)
    if set(actual) != set(expected):
        raise HashMismatch("file set differs from job.json")
    for path, (source, size) in actual.items():
        expected_digest, expected_size = expected[path]
        if size != expected_size or sha256_file(source) != expected_digest:
            raise HashMismatch("file content differs from job.json")
    return meta


def pending_jobs(cfg: Config) -> list[str]:
    found = []
    with os.scandir(cfg.inbox) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and JOB_ID_RE.fullmatch(entry.name):
                found.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
    return [name for _, name in sorted(found)]


def claim_job(cfg: Config, job_id: str) -> Path:
    check_job_id(job_id)
    source = cfg.inbox / job_id
    destination = cfg.work / job_id
    if os.path.lexists(destination):
        raise JobError("job is already claimed")
    try:
        os.rename(source, destination)
    except OSError as exc:
        raise JobError(f"claim failed: {type(exc).__name__}") from None
    return destination


def _set_owner(path: Path, uid: int, gid: int) -> None:
    if hasattr(os, "chown") and os.name == "posix":
        os.chown(path, uid, gid, follow_symlinks=False)


def _secure_source_tree(path: Path) -> None:
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                _secure_source_tree(child)
            elif entry.is_file(follow_symlinks=False):
                mode = 0o555 if entry.stat(follow_symlinks=False).st_mode & 0o111 else 0o444
                _set_owner(child, 0, 0)
                os.chmod(child, mode, follow_symlinks=False)
            else:
                raise HashMismatch("special file in claimed source")
    _set_owner(path, 0, 0)
    os.chmod(path, 0o555, follow_symlinks=False)


def _copy_agent_tree(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    with os.scandir(source) as entries:
        for entry in entries:
            target = destination / entry.name
            if entry.is_dir(follow_symlinks=False):
                _copy_agent_tree(Path(entry.path), target)
            elif entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                shutil.copyfile(entry.path, target, follow_symlinks=False)
                os.chmod(target, 0o444)
            else:
                raise HashMismatch("special file in claimed source")
    os.chmod(destination, 0o555)


def prepare_job(cfg: Config, job_id: str) -> dict:
    work = cfg.work / check_job_id(job_id)
    meta = load_and_verify_job(work, job_id)
    _set_owner(work, 0, 0)
    os.chmod(work, 0o700)
    _secure_source_tree(work / "target")
    for name in ("question.md", "job.json"):
        path = work / name
        _set_owner(path, 0, 0)
        os.chmod(path, 0o444)
    in_dir = work / "in"
    in_dir.mkdir(mode=0o700)
    _copy_agent_tree(work / "target", in_dir / "target")
    for name in ("question.md", "job.json"):
        shutil.copyfile(work / name, in_dir / name, follow_symlinks=False)
        os.chmod(in_dir / name, 0o444)
    os.chmod(in_dir, 0o555)
    for name in ("out", "session", "exec/requests"):
        path = work / name
        path.mkdir(parents=True, mode=0o700)
        _set_owner(path, cfg.agent_uid, cfg.agent_gid)
        os.chmod(path, 0o700)
    results = work / "exec" / "results"
    results.mkdir(mode=0o755)
    _set_owner(results, 0, 0)
    os.chmod(results, 0o755)
    return meta


def executor_config(cfg: Config, job_id: str) -> executor.Config:
    work = cfg.work / check_job_id(job_id)
    return executor.Config(
        image=cfg.exec_image, work_root=cfg.work, active_job_id=job_id,
        requests_dir=work / "exec" / "requests",
        results_dir=work / "exec" / "results", runs_dir=cfg.runs,
        agent_uid=cfg.agent_uid, agent_gid=cfg.agent_gid,
        docker_prefix=cfg.docker_prefix)


def build_agent_argv(cfg: Config, job_id: str) -> list[str]:
    work = cfg.work / check_job_id(job_id)
    return [
        *cfg.docker_prefix, "run", "--rm", "--runtime=runsc", "--network", cfg.network,
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        f"--user={cfg.agent_uid}:{cfg.agent_gid}", "--memory=3g", "--pids-limit=512",
        "--label", "re-lab.role=agent", "--label", f"re-lab.job={job_id}",
        "--log-driver=none", "--tmpfs", "/home/re:size=512m", "--tmpfs", "/tmp:size=512m",
        "--env-file", str(cfg.models_env),
        "-e", "HTTPS_PROXY=http://re-egress:3128", "-e", "HTTP_PROXY=http://re-egress:3128",
        "-e", "NO_PROXY=re-egress,localhost,127.0.0.1", "--add-host", f"re-egress:{EGRESS_IP}",
        "--cidfile", str(work / "agent.cid"),
        "-v", f"{work / 'in'}:/job/in:ro",
        "-v", f"{work / 'out'}:/job/out:rw",
        "-v", f"{work / 'session'}:/job/session:rw",
        "-v", f"{work / 'exec' / 'requests'}:/job/exec/requests:rw",
        "-v", f"{work / 'exec' / 'results'}:/job/exec/results:ro",
        cfg.agent_image, "python3", "/opt/re-lab/runner.py", "--job", job_id,
    ]



def run_agent(cfg: Config, job_id: str, wall_timeout_s: float = WALL_TIMEOUT_S) -> AgentResult:
    work = cfg.work / check_job_id(job_id)
    exec_cfg = executor_config(cfg, job_id)
    request_stop = threading.Event()
    request_ready = threading.Event()
    request_errors = []
    worker_started = False
    containment_attempted = False

    def serve_requests() -> None:
        while not request_stop.is_set():
            request_ready.set()
            try:
                handled = executor.process_pending(exec_cfg, request_stop)
            except Exception as exc:
                request_errors.append(exc)
                request_stop.set()
                return
            if not handled:
                request_stop.wait(POLL_INTERVAL_S)

    def contain_agent() -> None:
        nonlocal containment_attempted
        containment_attempted = True
        executor.contain_container(cfg.docker_prefix, work / "agent.cid")

    request_worker = threading.Thread(
        target=serve_requests, name=f"re-exec-{job_id}", daemon=False)
    process = subprocess.Popen(
        build_agent_argv(cfg, job_id), shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        request_worker.start()
        worker_started = True
        if not request_ready.wait(KILL_GRACE_S):
            raise RuntimeError("execution worker did not become ready")
        started = time.monotonic()
        timed_out = False
        request_failed = False
        while process.poll() is None:
            if request_errors:
                request_failed = True
                request_stop.set()
                contain_agent()
                break
            if time.monotonic() - started >= wall_timeout_s:
                timed_out = True
                request_stop.set()
                contain_agent()
                break
            time.sleep(POLL_INTERVAL_S)
        request_stop.set()
        request_worker.join(KILL_GRACE_S)
        if request_worker.is_alive():
            raise RuntimeError("execution worker did not stop after cancellation")
        exit_code = process.wait(timeout=KILL_GRACE_S)
        if request_errors:
            if isinstance(request_errors[0], executor.ContainmentError):
                raise request_errors[0]
            print(
                f"re-dispatcher: execution worker failed: {type(request_errors[0]).__name__}",
                file=sys.stderr, flush=True)
            request_failed = True
        if request_failed and exit_code == 0:
            exit_code = 1
        return AgentResult(exit_code=exit_code, timed_out=timed_out)
    except Exception as exc:
        failure = exc if isinstance(exc, executor.ContainmentError) else None
        try:
            request_stop.set()
        except Exception as cleanup_error:
            failure = failure or executor.ContainmentError(
                f"execution worker cancellation failed: {type(cleanup_error).__name__}")
        if worker_started:
            try:
                request_worker.join(KILL_GRACE_S)
                if request_worker.is_alive() and failure is None:
                    failure = executor.ContainmentError(
                        "execution worker did not stop after cancellation")
            except Exception as cleanup_error:
                failure = failure or executor.ContainmentError(
                    f"execution worker cleanup failed: {type(cleanup_error).__name__}")
        if not containment_attempted:
            try:
                contain_agent()
            except executor.ContainmentError as containment_error:
                failure = containment_error
        try:
            executor.stop_docker_client(process)
        except executor.ContainmentError as client_error:
            failure = failure or client_error
        if failure is not None:
            raise failure
        raise executor.ContainmentError(
            f"agent lifecycle failed: {type(exc).__name__}") from None


def _secret_values(path: Path) -> tuple[bytes, ...]:
    values = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PublishError(f"models.env cannot be read: {type(exc).__name__}") from None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        value = stripped.split("=", 1)[1]
        if value:
            values.append(value.encode("utf-8"))
    return tuple(values)


def _redact(data: bytes, secrets: tuple[bytes, ...]) -> bytes:
    for secret in secrets:
        data = data.replace(secret, b"[REDACTED]")
    return data


def _read_regular(path: Path, cap: int) -> bytes | None:
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None
        data = handle.read(cap + 1)
    return data if len(data) <= cap else None


def _set_report_mode(path: Path, report_gid: int, mode: int) -> None:
    _set_owner(path, 0, report_gid)
    os.chmod(path, mode, follow_symlinks=False)


def _make_report_dir(path: Path, report_gid: int, *, exist_ok: bool = False) -> None:
    path.mkdir(mode=0o750, parents=True, exist_ok=exist_ok)
    _set_report_mode(path, report_gid, 0o750)


def _write_exclusive(path: Path, data: bytes, report_gid: int = 10001) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o640)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    _set_report_mode(path, report_gid, 0o640)


def _copy_evidence(
        source: Path, destination: Path, budget: _ScanBudget,
        secrets: tuple[bytes, ...], report_gid: int = 10001, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise PublishError("published evidence is nested too deeply")
    try:
        entries = os.scandir(source)
    except OSError:
        return
    with entries:
        for entry in entries:
            budget.entry()
            target = destination / entry.name
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                _make_report_dir(target, report_gid)
                _copy_evidence(
                    Path(entry.path), target, budget, secrets, report_gid, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                if size > MAX_EVIDENCE_FILE_BYTES or not budget.fits(size):
                    continue
                data = _read_regular(Path(entry.path), MAX_EVIDENCE_FILE_BYTES)
                if data is None:
                    continue
                if Path(entry.name).suffix.lower() in TEXT_SUFFIXES:
                    data = _redact(data, secrets)
                if not budget.fits(len(data)):
                    continue
                _write_exclusive(target, data, report_gid)
                budget.take(len(data))


def _session_cost_checked(data: bytes) -> tuple[float, bool]:
    total = 0.0
    incomplete = False
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            incomplete = True
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        value = cost.get("total") if isinstance(cost, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        try:
            finite = math.isfinite(value)
        except OverflowError:
            incomplete = True
            continue
        if not finite:
            incomplete = True
            continue
        if value < 0:
            continue
        candidate = total + value
        if not math.isfinite(candidate):
            total = 0.0
            incomplete = True
        else:
            total = candidate
    return total, incomplete


def _session_cost(data: bytes) -> float:
    return _session_cost_checked(data)[0]


def _scan_cost_file(path: Path, remaining: int) -> tuple[float, int, bool]:
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    total = 0.0
    consumed = 0
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return 0.0, 0, True
            incomplete = False
            while consumed < remaining:
                available = remaining - consumed
                line = handle.readline(min(MAX_COST_LINE_BYTES + 1, available + 1))
                if len(line) > available:
                    return total_if_finite(total), remaining, True
                if not line:
                    return total_if_finite(total), consumed, incomplete
                consumed += len(line)
                if len(line) > MAX_COST_LINE_BYTES:
                    incomplete = True
                    while not line.endswith(b"\n") and consumed < remaining:
                        line = handle.readline(min(1024 ** 2, remaining - consumed))
                        if not line:
                            break
                        consumed += len(line)
                    continue
                line_cost, line_incomplete = _session_cost_checked(line)
                candidate = total + line_cost
                if not math.isfinite(candidate):
                    total = 0.0
                    incomplete = True
                else:
                    total = candidate
                incomplete = incomplete or line_incomplete
            if handle.read(1):
                return total_if_finite(total), consumed, True
            return total_if_finite(total), consumed, incomplete
    except OSError:
        return total_if_finite(total), remaining, True


def _scan_session_cost(source: Path) -> tuple[float, bool]:
    total = 0.0
    bytes_scanned = 0
    entries_seen = 0
    incomplete = False
    stack = [(source, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > MAX_DEPTH:
            incomplete = True
            continue
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            if directory != source:
                incomplete = True
            continue
        except OSError:
            incomplete = True
            continue
        with entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_ENTRIES:
                    return total_if_finite(total), True
                try:
                    if entry.is_symlink():
                        if entry.name.endswith(".jsonl"):
                            incomplete = True
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), depth + 1))
                    elif entry.name.endswith(".jsonl"):
                        if not entry.is_file(follow_symlinks=False):
                            incomplete = True
                            continue
                        remaining = MAX_COST_SCAN_BYTES - bytes_scanned
                        if remaining <= 0:
                            return total_if_finite(total), True
                        cost, consumed, file_incomplete = _scan_cost_file(
                            Path(entry.path), remaining)
                        candidate = total + cost
                        if not math.isfinite(candidate):
                            total = 0.0
                            incomplete = True
                        else:
                            total = candidate
                        bytes_scanned += consumed
                        incomplete = incomplete or file_incomplete
                        if file_incomplete and bytes_scanned >= MAX_COST_SCAN_BYTES:
                            return total_if_finite(total), True
                except OSError:
                    incomplete = True
    return total_if_finite(total), incomplete


def _merge_sessions(
        source: Path, destination: Path, secrets: tuple[bytes, ...],
        report_gid: int = 10001) -> None:
    remaining = MAX_SESSION_BYTES
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(destination, flags, 0o640)
    try:
        with os.fdopen(fd, "wb") as output:
            stack = [(source, 0)]
            entries_seen = 0
            while stack and remaining:
                directory, depth = stack.pop()
                if depth > MAX_DEPTH:
                    raise PublishError("session tree is nested too deeply")
                try:
                    entries = os.scandir(directory)
                except OSError:
                    continue
                with entries:
                    for entry in entries:
                        entries_seen += 1
                        if entries_seen > MAX_ENTRIES:
                            raise PublishError("session tree exceeds the entry limit")
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), depth + 1))
                        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                            data = _read_regular(Path(entry.path), remaining)
                            if data is None:
                                continue
                            data = _redact(data, secrets)[:remaining]
                            output.write(data)
                            if data and not data.endswith(b"\n") and len(data) < remaining:
                                output.write(b"\n")
                                remaining -= 1
                            remaining -= len(data)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    _set_report_mode(destination, report_gid, 0o640)


def total_if_finite(value: float) -> float:
    return round(value, 6) if math.isfinite(value) else 0.0


def _read_agent_status(work: Path, job_id: str) -> dict:
    data = _read_regular(work / "out" / "status.json", STATUS_MAX_BYTES)
    if data is None:
        return {"job_id": job_id, "status": "failed", "exit_code": None,
                "model": None, "started": utc_now(), "ended": utc_now()}
    try:
        body = json.loads(data)
    except (ValueError, RecursionError):
        body = None
    required = {"job_id", "status", "exit_code", "model", "started", "ended"}
    status_name = body.get("status") if isinstance(body, dict) else None
    if (not isinstance(body, dict) or body.get("job_id") != job_id
            or not isinstance(status_name, str) or status_name not in ALLOWED_STATUSES
            or not required <= set(body)):
        return {"job_id": job_id, "status": "failed", "exit_code": None,
                "model": None, "started": utc_now(), "ended": utc_now(),
                "error": "agent status invalid"}
    return {key: body[key] for key in (*required, "reason") if key in body}


def _write_result(
        path: Path, result: dict, secrets: tuple[bytes, ...] = (),
        report_gid: int = 10001) -> None:
    _make_report_dir(path.parent, report_gid, exist_ok=True)
    raw = _redact((json.dumps(result, indent=2) + "\n").encode("utf-8"), secrets)
    fd, temporary = tempfile.mkstemp(prefix=".result-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        _set_report_mode(Path(temporary), report_gid, 0o640)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def publish_job(cfg: Config, job_id: str, status: dict | None = None) -> dict:
    work = cfg.work / check_job_id(job_id)
    report = cfg.reports / job_id
    _make_report_dir(report, cfg.report_gid)
    secrets = _secret_values(cfg.models_env)
    result = dict(status or _read_agent_status(work, job_id))
    report_data = _read_regular(work / "out" / "report.md", MAX_REPORT_BYTES)
    if report_data is not None:
        _write_exclusive(
            report / "report.md", _redact(report_data, secrets)[:MAX_REPORT_BYTES],
            cfg.report_gid)
    evidence_source = work / "out" / "evidence"
    if evidence_source.is_dir() and not evidence_source.is_symlink():
        evidence_dest = report / "evidence"
        _make_report_dir(evidence_dest, cfg.report_gid)
        _copy_evidence(
            evidence_source, evidence_dest, _ScanBudget(MAX_EVIDENCE_BYTES),
            secrets, cfg.report_gid)
    result["cost_usd"], result["cost_incomplete"] = _scan_session_cost(work / "session")
    _merge_sessions(
        work / "session", report / "session.jsonl", secrets, cfg.report_gid)
    _write_result(report / "result.json", result, secrets, cfg.report_gid)
    return result


def recover_interrupted(cfg: Config) -> list[str]:
    recovered = []
    with os.scandir(cfg.work) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or not JOB_ID_RE.fullmatch(entry.name):
                continue
            result_path = cfg.reports / entry.name / "result.json"
            if result_path.exists():
                continue
            cost, incomplete = _scan_session_cost(Path(entry.path) / "session")
            result = {"job_id": entry.name, "status": "recovered-after-restart",
                      "exit_code": None, "cost_usd": cost,
                      "cost_incomplete": incomplete, "model": None,
                      "started": utc_now(), "ended": utc_now()}
            _write_result(result_path, result, report_gid=cfg.report_gid)
            recovered.append(entry.name)
    return recovered


def _terminal_status(job_id: str, status: str, error: str) -> dict:
    return {"job_id": job_id, "status": status, "exit_code": None,
            "cost_usd": 0.0, "cost_incomplete": False, "model": None,
            "started": utc_now(), "ended": utc_now(), "error": error}


def _record_containment_failure(cfg: Config, job_id: str) -> None:
    status = _terminal_status(job_id, "failed", "containment failed")
    cost, incomplete = _scan_session_cost(cfg.work / job_id / "session")
    status.update(cost_usd=cost, cost_incomplete=True)
    try:
        _write_result(
            cfg.reports / job_id / "result.json", status,
            report_gid=cfg.report_gid)
    except OSError as exc:
        print(f"re-dispatcher: containment result write failed: {type(exc).__name__}",
              file=sys.stderr, flush=True)


def run_claimed_job(cfg: Config, job_id: str) -> dict:
    result_path = cfg.reports / job_id / "result.json"
    try:
        prepare_job(cfg, job_id)
    except JobError as exc:
        status = _terminal_status(job_id, "rejected-job", str(exc))
        _write_result(result_path, status, report_gid=cfg.report_gid)
        return status
    except HashMismatch as exc:
        status = _terminal_status(job_id, "rejected-hash", str(exc))
        _write_result(result_path, status, report_gid=cfg.report_gid)
        return status
    except (OSError, executor.RequestError) as exc:
        status = _terminal_status(
            job_id, "failed", f"host preparation failed: {type(exc).__name__}")
        _write_result(result_path, status, report_gid=cfg.report_gid)
        return status
    try:
        agent = run_agent(cfg, job_id)
    except executor.ContainmentError:
        _record_containment_failure(cfg, job_id)
        raise
    except Exception as exc:
        status = _terminal_status(
            job_id, "failed", f"agent execution failed: {type(exc).__name__}")
        status["cost_usd"], status["cost_incomplete"] = _scan_session_cost(
            cfg.work / job_id / "session")
        _write_result(result_path, status, report_gid=cfg.report_gid)
        return status
    try:
        status = _read_agent_status(cfg.work / job_id, job_id)
    except (OSError, RuntimeError, ValueError, TypeError, RecursionError):
        status = _terminal_status(job_id, "failed", "agent status invalid")
    if agent.timed_out:
        status.update(status="time-cap", exit_code=None, ended=utc_now())
    elif agent.exit_code != 0 and status["status"] not in {"cost-cap", "failed"}:
        status.update(status="failed", exit_code=agent.exit_code, ended=utc_now())
    try:
        return publish_job(cfg, job_id, status)
    except executor.ContainmentError:
        _record_containment_failure(cfg, job_id)
        raise
    except Exception as exc:
        cost, _incomplete = _scan_session_cost(cfg.work / job_id / "session")
        status.update(
            status="failed", cost_usd=cost, cost_incomplete=True, ended=utc_now(),
            error=f"publication failed: {type(exc).__name__}")
        _write_result(result_path, status, report_gid=cfg.report_gid)
        return status


def _labeled_containers(cfg: Config) -> list[str]:
    try:
        result = subprocess.run(
            [*cfg.docker_prefix, "ps", "-aq", "--no-trunc",
             "--filter", "label=re-lab.role"],
            capture_output=True, timeout=KILL_GRACE_S, check=False, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise executor.ContainmentError(
            f"startup container list failed: {type(exc).__name__}") from None
    if result.returncode != 0:
        raise executor.ContainmentError("startup container list failed")
    container_ids = result.stdout.split()
    if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in container_ids):
        raise executor.ContainmentError("startup container list was invalid")
    return container_ids


def cleanup_containers(cfg: Config) -> None:
    """Remove every labeled agent/exec container before accepting another job."""
    container_ids = _labeled_containers(cfg)
    if container_ids:
        try:
            removed = subprocess.run(
                [*cfg.docker_prefix, "rm", "-f", *container_ids],
                capture_output=True, timeout=KILL_GRACE_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise executor.ContainmentError(
                f"startup container cleanup failed: {type(exc).__name__}") from None
        if removed.returncode != 0:
            raise executor.ContainmentError("startup container cleanup failed")
    if _labeled_containers(cfg):
        raise executor.ContainmentError("startup containers remain after cleanup")


def process_once(cfg: Config, run_job=run_claimed_job) -> bool:
    jobs = pending_jobs(cfg)
    if not jobs:
        return False
    job_id = jobs[0]
    claim_job(cfg, job_id)
    run_job(cfg, job_id)
    return True


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return Config(
            root=Path(raw.get("root", "/srv/re-lab")),
            agent_image=raw["agent_image"], exec_image=raw["exec_image"],
            models_env=Path(raw.get("models_env", "/srv/re-lab/secrets/models.env")),
            network=raw.get("network", "re-lab_re-internal"),
            agent_uid=raw.get("agent_uid", 10001), agent_gid=raw.get("agent_gid", 10001),
            report_gid=raw.get("report_gid", 10001),
            docker_prefix=tuple(raw.get("docker_prefix", ["docker"])))
    except (OSError, ValueError, KeyError, TypeError, DispatcherError) as exc:
        raise SystemExit(f"re-dispatcher: invalid config: {exc}") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    cleanup_containers(cfg)
    executor.sweep_runs(executor_config(cfg, "job-placeholder"))
    for job_id in recover_interrupted(cfg):
        print(f"re-dispatcher: {job_id}: recovered-after-restart", flush=True)
    if args.once:
        return 0 if process_once(cfg) else 0
    while True:
        if not process_once(cfg):
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
