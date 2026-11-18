#!/usr/bin/env python3
"""Trusted host dispatcher for one isolated reverse-engineering job at a time.

Admission: a job is claimed by moving its inbox directory, through pinned O_NOFOLLOW
descriptors, into a fresh root-only ``jobs/work/<id>/upload``. Every upload directory is
locked to root before it is listed, and every file is copied into root-owned trees while it is
hashed, so the bytes that are analysed and executed are exactly the bytes that matched the
manifest; descriptors the uploader still holds can only reach the discarded upload.

Execution: one gVisor agent container per job, a request worker that serves the job's
closed-schema execution requests through ``executor``, a wall clock, a storage reserve, cancel
markers and SIGTERM handling, each of which contains the agent and every execution container
before the job is published.

Publication: bounded, redacted copies of the report, evidence and session, and a typed
``result.json`` built from dispatcher-owned facts. A bounded live event feed
(``livefeed``) lets the local console follow the job. Operator commands: ``--check``,
``--init-layout``, ``--prune DAYS`` and ``--cleanup-only``.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import execpolicy
import executor
import livefeed

JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/][A-Za-z0-9._:/-]{0,99}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CONVERSATION_RE = re.compile(r"^[a-z0-9-]{1,64}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
AGENT_ERROR_RE = re.compile(
    r"^(invalid-(job|model|cost|question)|prompt-missing|profile-missing|omp-launch-failed"
    r"|omp-exit-(?:[1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])|output-unwritable"
    r"|runner-error)$")
CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f]")
JOB_TOP_LEVEL = frozenset({"target", "question.md", "job.json"})
UPLOAD = "upload"
MAX_INPUT_BYTES = 2 * 1024 ** 3
MAX_REPORT_BYTES = 5 * 1024 ** 2
MAX_EVIDENCE_FILE_BYTES = 25 * 1024 ** 2
MAX_EVIDENCE_BYTES = 200 * 1024 ** 2
MAX_SESSION_BYTES = 50 * 1024 ** 2
MAX_COST_SCAN_BYTES = 512 * 1024 ** 2
MAX_COST_LINE_BYTES = 8 * 1024 ** 2
MAX_MANIFEST_BYTES = 10 * 1024 ** 2
MAX_ENV_FILE_BYTES = 64 * 1024
MAX_ENV_VALUE_BYTES = 4096
MAX_ENTRIES = 10000
MAX_INBOX_SCAN = 10000
MAX_DEPTH = 32
MAX_NAME_BYTES = 255
STATUS_MAX_BYTES = 4096
REASON_MAX_CHARS = 1000
RESULT_TEXT_MAX = 2000
WALL_TIMEOUT_S = 2 * 3600 + 5 * 60
POLL_INTERVAL_S = 0.25
KILL_GRACE_S = 10
# The request worker's worst case after cancellation: client stop, kill/rm/inspect with the
# removal wait, then the bounded copy-back and discard of one run directory.
WORKER_JOIN_S = 120
HEARTBEAT_TICKS = 120
IDLE_HEARTBEAT_S = 15
COPY_CHUNK = 1024 * 1024
AGENT_STATUSES = frozenset({"done", "cost-cap", "time-cap", "failed", "needs-vm-lane"})
ALLOWED_STATUSES = AGENT_STATUSES
DEFAULT_CONFIG = Path("/etc/re-lab/dispatcher.json")
RESERVED_NETWORKS = frozenset({"bridge", "host", "none", "default"})
# Names an operator or console must not be able to set in the agent environment: they change
# how the runner, OMP, Node, Java or the dynamic loader behave, or where traffic goes.
DENIED_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "HOSTNAME", "TERM", "ENV",
    "BASH_ENV", "PROMPT_COMMAND", "IFS", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "ALL_PROXY", "FTP_PROXY", "GHIDRA_INSTALL_DIR", "NODE_OPTIONS", "NODE_PATH",
    "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GLIBC_TUNABLES", "GCONV_PATH",
    "OMP_MCP_REQUIRE_READY", "OMP_MCP_TIMEOUT_MS"})
DENIED_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHON", "JAVA_", "JDK_", "MALLOC_", "XDG_",
                       "NPM_CONFIG_", "PIP_", "GIT_", "REA_")
CONFIG_KEYS = frozenset({
    "root", "agent_image", "exec_image", "models_env", "network", "agent_uid", "agent_gid",
    "report_gid", "docker_prefix", "console_uid", "agent_cpus", "min_free_mib",
    "min_free_inodes", "live"})
# gVisor cannot reach Docker's embedded DNS on a user-defined network, so agents find the
# proxy through /etc/hosts. Must match re-egress's ipv4_address in compose.yaml.
EGRESS_IP = "172.30.97.2"
EGRESS_SUBNET = "172.30.97.0/24"
DIR_FLAGS = executor.DIR_FLAGS
FILE_READ_FLAGS = executor.FILE_READ_FLAGS
FILE_CREATE_FLAGS = executor.FILE_CREATE_FLAGS

STOP = threading.Event()
_SKIPPED: set = set()


class DispatcherError(Exception):
    """The trusted dispatcher cannot safely process the job."""


class JobError(DispatcherError):
    """The claimed manifest or layout is invalid."""


class DuplicateJob(JobError):
    """The job id was already used by an earlier job."""


class HashMismatch(DispatcherError):
    """Claimed bytes do not match the submitted manifest."""


class PublishError(DispatcherError):
    """Agent output cannot be published safely."""


def _positive_int(value, low: int) -> bool:
    return type(value) is int and value >= low


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
    console_uid: int | None = None
    agent_cpus: float | None = 4.0
    min_free_bytes: int = 1024 ** 3
    min_free_inodes: int = 20000
    live: bool = True

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "models_env", Path(self.models_env))
        if isinstance(self.docker_prefix, (str, bytes)):
            raise DispatcherError("docker_prefix must be a list of strings")
        object.__setattr__(self, "docker_prefix", tuple(self.docker_prefix))
        if not self.root.is_absolute() or any(ch in str(self.root) for ch in ":,"):
            raise DispatcherError("root must be an absolute path without ':' or ','")
        for name, image in (("agent_image", self.agent_image), ("exec_image", self.exec_image)):
            if not isinstance(image, str) or not executor.IMAGE_RE.fullmatch(image):
                raise DispatcherError(f"{name} must be content-addressed")
        if (not isinstance(self.network, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.network)):
            raise DispatcherError("network has an unsupported shape")
        if self.network in RESERVED_NETWORKS:
            raise DispatcherError("network must be the isolated internal network, not a built-in one")
        if (type(self.agent_uid) is not int or type(self.agent_gid) is not int
                or type(self.report_gid) is not int):
            raise DispatcherError("agent uid, agent gid and report gid must be integers")
        if not (_positive_int(self.agent_uid, 1000) and _positive_int(self.agent_gid, 1000)
                and _positive_int(self.report_gid, 1)):
            raise DispatcherError("agent uid/gid must be at least 1000 and report gid at least 1")
        if self.console_uid is not None and (
                not _positive_int(self.console_uid, 1000) or self.console_uid == self.agent_uid):
            raise DispatcherError("console_uid must be an unprivileged uid other than the agent's")
        if not self.docker_prefix or not all(isinstance(p, str) and p for p in self.docker_prefix):
            raise DispatcherError("docker_prefix must be a non-empty list of non-empty strings")
        if self.agent_cpus is not None and (
                isinstance(self.agent_cpus, bool) or not isinstance(self.agent_cpus, (int, float))
                or not 0.01 <= self.agent_cpus <= 1024):
            raise DispatcherError("agent_cpus must be a number between 0.01 and 1024, or null")
        if not (_positive_int(self.min_free_bytes, 0) and _positive_int(self.min_free_inodes, 0)):
            raise DispatcherError("storage reserves must be non-negative integers")
        if type(self.live) is not bool:
            raise DispatcherError("live must be true or false")

    @property
    def inbox(self) -> Path:
        return self.root / "jobs" / "inbox"

    @property
    def work(self) -> Path:
        return self.root / "jobs" / "work"

    @property
    def cancel_dir(self) -> Path:
        return self.root / "jobs" / "cancel"

    @property
    def quarantine(self) -> Path:
        return self.root / "jobs" / "quarantine"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def runs(self) -> Path:
        return self.root / "exec" / "runs"

    @property
    def live_root(self) -> Path:
        return self.root / "live"

    @property
    def console_dir(self) -> Path:
        return self.root / "console"


@dataclass(frozen=True)
class AgentResult:
    exit_code: int | None
    timed_out: bool
    stop_reason: str | None = None  # time-cap | storage-cap | cancelled | stopped


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


def _log(message: str) -> None:
    print(f"re-dispatcher: {message}", flush=True)


def _warn(message: str) -> None:
    print(f"re-dispatcher: {message}", file=sys.stderr, flush=True)


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise JobError("job id must match ^[a-z0-9-]{8,64}$")
    return job_id


def _is_root() -> bool:
    return os.name == "posix" and os.geteuid() == 0


def _valid_name(name: str) -> bool:
    """A path component the lab will publish: no controls, no separators, valid UTF-8."""
    if name in ("", ".", "..") or "/" in name or "\\" in name or CONTROL_CHARS.search(name):
        return False
    try:
        return len(name.encode("utf-8")) <= MAX_NAME_BYTES
    except UnicodeEncodeError:
        return False


# ---------------------------------------------------------------------------------------------
# Manifest verification.

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


def _validate_manifest(meta, job_id: str) -> dict:
    """Map each manifest path to (sha256, size); JobError for any malformed entry."""
    if (not isinstance(meta, dict) or meta.get("job_id") != job_id
            or not isinstance(meta.get("files"), list)):
        raise JobError("job.json does not match the claimed job")
    if len(meta["files"]) > MAX_ENTRIES + 1:
        raise JobError("job.json lists too many files")
    expected = {}
    for item in meta["files"]:
        if not isinstance(item, dict):
            raise JobError("job.json files entries must be objects")
        path, digest, size = item.get("path"), item.get("sha256"), item.get("size")
        parts = path.split("/") if isinstance(path, str) else []
        valid_path = path == "question.md" or (len(parts) >= 2 and parts[0] == "target")
        if (not valid_path or not all(_valid_name(part) for part in parts)
                or path in expected
                or not isinstance(digest, str) or not HASH_RE.fullmatch(digest)
                or isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise JobError("job.json files entry is invalid")
        expected[path] = (digest, size)
    return expected


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
    """Read-only check of a job layout against its manifest (tooling and tests).

    Admission never relies on this path-based check: prepare_job copies and hashes through
    pinned descriptors instead. A claimed job keeps its upload under ``upload/``."""
    work = Path(work)
    if (work / UPLOAD).is_dir() and not (work / UPLOAD).is_symlink():
        work = work / UPLOAD
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
    expected = _validate_manifest(meta, job_id)
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


# ---------------------------------------------------------------------------------------------
# Inbox and claim.

def pending_jobs(cfg: Config) -> list[str]:
    found = []
    examined = 0
    with os.scandir(cfg.inbox) as entries:
        for entry in entries:
            examined += 1
            if examined > MAX_INBOX_SCAN:
                _warn("inbox has more entries than one scan examines; the rest wait")
                break
            if not JOB_ID_RE.fullmatch(entry.name):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                found.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
            except OSError:
                continue  # removed or replaced between the listing and the check
    return [name for _, name in sorted(found)]


def claim_job(cfg: Config, job_id: str) -> Path:
    """Move inbox/<id> into a fresh root-only work/<id>/upload through pinned descriptors."""
    check_job_id(job_id)
    destination = cfg.work / job_id
    if os.path.lexists(destination) or os.path.lexists(cfg.reports / job_id):
        raise DuplicateJob("job id was already used")
    inbox_fd = os.open(cfg.inbox, DIR_FLAGS)
    try:
        try:
            source_fd = os.open(job_id, DIR_FLAGS, dir_fd=inbox_fd)
        except OSError as exc:
            raise JobError(f"claim failed: {type(exc).__name__}") from None
        try:
            source = os.fstat(source_fd)
        finally:
            os.close(source_fd)
        work_fd = os.open(cfg.work, DIR_FLAGS)
        try:
            try:
                os.mkdir(job_id, 0o700, dir_fd=work_fd)
            except OSError as exc:
                raise JobError(f"claim failed: {type(exc).__name__}") from None
            job_fd = os.open(job_id, DIR_FLAGS, dir_fd=work_fd)
            try:
                os.fchmod(job_fd, 0o700)
                try:
                    os.rename(job_id, UPLOAD, src_dir_fd=inbox_fd, dst_dir_fd=job_fd)
                except OSError as exc:
                    os.close(job_fd)
                    job_fd = -1
                    try:
                        os.rmdir(job_id, dir_fd=work_fd)
                    except OSError:
                        pass
                    raise JobError(f"claim failed: {type(exc).__name__}") from None
                moved = os.stat(UPLOAD, dir_fd=job_fd, follow_symlinks=False)
            finally:
                if job_fd >= 0:
                    os.close(job_fd)
        finally:
            os.close(work_fd)
    finally:
        os.close(inbox_fd)
    if not stat.S_ISDIR(moved.st_mode) or (moved.st_dev, moved.st_ino) != (source.st_dev, source.st_ino):
        raise JobError("claimed entry changed during the claim")
    return destination


def _quarantine_inbox_entry(cfg: Config, job_id: str) -> None:
    """Move an unclaimable inbox entry out of the queue so it cannot block later jobs."""
    try:
        try:
            cfg.quarantine.mkdir(mode=0o700)
        except FileExistsError:
            pass
        inbox_fd = os.open(cfg.inbox, DIR_FLAGS)
        try:
            quarantine_fd = os.open(cfg.quarantine, DIR_FLAGS)
            try:
                os.fchmod(quarantine_fd, 0o700)
                os.rename(job_id, f"{job_id}.{time.time_ns()}",
                          src_dir_fd=inbox_fd, dst_dir_fd=quarantine_fd)
            finally:
                os.close(quarantine_fd)
        finally:
            os.close(inbox_fd)
    except OSError as exc:
        _warn(f"{job_id}: inbox entry could not be quarantined ({type(exc).__name__}); skipped")
        _SKIPPED.add(job_id)


# ---------------------------------------------------------------------------------------------
# Admission: copy and hash the upload into root-owned trees.

def _set_owner(path: Path, uid: int, gid: int) -> None:
    if hasattr(os, "chown") and os.name == "posix":
        os.chown(path, uid, gid, follow_symlinks=False)


def _lock_dir(fd: int) -> None:
    """Make an upload directory root-only before it is listed, revoking held descriptors."""
    if _is_root():
        os.fchown(fd, 0, 0)
    os.fchmod(fd, 0o700)


def _copy_upload_file(src_dir: int, name: str, dst_dir: int, budget: _ScanBudget,
                      mode: int | None = None) -> tuple:
    """Copy one uploaded regular file into a new root-owned file, hashing the copied bytes.

    The copy keeps the upload's execute bit (0555/0444) unless ``mode`` is given."""
    try:
        src = os.open(name, FILE_READ_FLAGS, dir_fd=src_dir)
    except OSError:
        raise HashMismatch("job file is unavailable") from None
    try:
        info = os.fstat(src)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HashMismatch("job file is not a single-link regular file")
        dst = os.open(name, FILE_CREATE_FLAGS, 0o600, dir_fd=dst_dir)
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := os.read(src, COPY_CHUNK):
                budget.take(len(chunk))
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dst, view):]
            os.fchmod(dst, mode if mode is not None
                      else 0o555 if info.st_mode & 0o111 else 0o444)
        finally:
            os.close(dst)
    finally:
        os.close(src)
    return digest.hexdigest(), size


def _copy_upload_tree(src_fd: int, dst_fd: int, prefix: str, budget: _ScanBudget,
                      actual: dict, depth: int = 0) -> None:
    _lock_dir(src_fd)
    with os.scandir(src_fd) as entries:
        for entry in entries:
            budget.entry()
            name = entry.name
            if not _valid_name(name):
                raise HashMismatch("job file name is not allowed")
            relative = f"{prefix}/{name}"
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError:
                raise HashMismatch("job files changed during admission") from None
            if stat.S_ISLNK(mode):
                raise HashMismatch("symlink in job files")
            if stat.S_ISDIR(mode):
                if depth + 1 > MAX_DEPTH:
                    raise HashMismatch("job files are nested too deeply")
                try:
                    child_src = os.open(name, DIR_FLAGS, dir_fd=src_fd)
                except OSError:
                    raise HashMismatch("job files changed during admission") from None
                try:
                    os.mkdir(name, 0o700, dir_fd=dst_fd)
                    child_dst = os.open(name, DIR_FLAGS, dir_fd=dst_fd)
                    try:
                        _copy_upload_tree(child_src, child_dst, relative, budget, actual, depth + 1)
                    finally:
                        os.fchmod(child_dst, 0o555)
                        os.close(child_dst)
                finally:
                    os.close(child_src)
            elif stat.S_ISREG(mode):
                actual[relative] = _copy_upload_file(src_fd, name, dst_fd, budget)
            else:
                raise HashMismatch("special file in job files")


def _read_at(dir_fd: int, name: str, cap: int) -> bytes | None:
    """Read a regular file below dir_fd without following links; None when unusable."""
    try:
        fd = os.open(name, FILE_READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None
        data = handle.read(cap + 1)
    return data if len(data) <= cap else None


def _write_new_at(dir_fd: int, name: str, data: bytes, mode: int) -> None:
    fd = os.open(name, FILE_CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _make_dir_at(dir_fd: int, name: str, mode: int, owner: tuple | None = None) -> int:
    os.mkdir(name, 0o700, dir_fd=dir_fd)
    fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    if owner is not None and os.name == "posix":
        os.fchown(fd, *owner)
    os.fchmod(fd, mode)
    return fd


def prepare_job(cfg: Config, job_id: str) -> dict:
    """Verify the claimed upload by copying it into root-owned trees and build the job layout."""
    executor._require_fd_ops()
    work = cfg.work / check_job_id(job_id)
    job_fd = os.open(work, DIR_FLAGS)
    try:
        info = os.fstat(job_fd)
        if (_is_root() and info.st_uid != 0) or info.st_mode & 0o077:
            raise JobError("claimed job directory is not private to the dispatcher")
        try:
            upload_fd = os.open(UPLOAD, DIR_FLAGS, dir_fd=job_fd)
        except OSError:
            raise JobError("claimed upload is unavailable") from None
        try:
            _lock_dir(upload_fd)
            names = set()
            with os.scandir(upload_fd) as entries:
                for entry in entries:
                    names.add(entry.name)
                    if len(names) > len(JOB_TOP_LEVEL):
                        break
            if names != JOB_TOP_LEVEL:
                raise JobError("claimed job must contain only target, question.md and job.json")
            raw = _read_at(upload_fd, "job.json", MAX_MANIFEST_BYTES)
            try:
                if raw is None:
                    raise ValueError
                meta = json.loads(raw)
            except (ValueError, RecursionError):
                raise JobError("job.json is missing or invalid") from None
            expected = _validate_manifest(meta, job_id)
            budget = _ScanBudget(MAX_INPUT_BYTES, error_type=HashMismatch, limit_name="job input")
            actual = {}
            try:
                target_src = os.open("target", DIR_FLAGS, dir_fd=upload_fd)
            except OSError:
                raise HashMismatch("target directory is unavailable") from None
            try:
                target_dst = _make_dir_at(job_fd, "target", 0o700)
                try:
                    _copy_upload_tree(target_src, target_dst, "target", budget, actual)
                finally:
                    os.fchmod(target_dst, 0o555)
                    os.close(target_dst)
            finally:
                os.close(target_src)
            budget.entry()
            actual["question.md"] = _copy_upload_file(
                upload_fd, "question.md", job_fd, budget, mode=0o444)
            if set(actual) != set(expected):
                raise HashMismatch("file set differs from job.json")
            for path, value in actual.items():
                if value != expected[path]:
                    raise HashMismatch("file content differs from job.json")
            _write_new_at(job_fd, "job.json", raw, 0o444)
        finally:
            os.close(upload_fd)
        try:
            executor._rmtree_force(work / UPLOAD)
        except OSError as exc:
            _warn(f"{job_id}: verified upload could not be removed: {type(exc).__name__}")
        in_fd = _make_dir_at(job_fd, "in", 0o700)
        try:
            agent_target = _make_dir_at(in_fd, "target", 0o700)
            source = os.open("target", DIR_FLAGS, dir_fd=job_fd)
            try:
                executor._copy_tree(
                    source, agent_target,
                    executor._Budget(MAX_INPUT_BYTES, MAX_ENTRIES, "agent input"),
                    strict=True, owner=None, file_mode=0o444, dir_mode=0o555)
            finally:
                os.close(source)
                os.fchmod(agent_target, 0o555)
                os.close(agent_target)
            for name in ("question.md", "job.json"):
                data = _read_at(job_fd, name, MAX_MANIFEST_BYTES)
                if data is None:
                    raise JobError("verified job files are unavailable")
                _write_new_at(in_fd, name, data, 0o444)
        finally:
            os.fchmod(in_fd, 0o555)
            os.close(in_fd)
        agent = (cfg.agent_uid, cfg.agent_gid)
        for name in ("out", "session"):
            os.close(_make_dir_at(job_fd, name, 0o700, agent))
        exec_fd = _make_dir_at(job_fd, "exec", 0o755)
        try:
            os.close(_make_dir_at(exec_fd, "requests", 0o700, agent))
            os.close(_make_dir_at(exec_fd, "results", 0o755))
        finally:
            os.close(exec_fd)
    finally:
        os.close(job_fd)
    return meta


# ---------------------------------------------------------------------------------------------
# Agent environment and launch.

def _denied_env_name(name: str) -> bool:
    return name in DENIED_ENV_NAMES or name.startswith(DENIED_ENV_PREFIXES)


def _env_entries(data: bytes, source: str) -> list:
    """Docker env-file lines (NAME=value) with safe names; other lines are ignored with a warning."""
    entries = []
    for number, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if (not separator or not ENV_NAME_RE.fullmatch(name) or _denied_env_name(name)
                or "\x00" in value or len(value.encode("utf-8", "replace")) > MAX_ENV_VALUE_BYTES):
            _warn(f"{source}: line {number} ignored (missing value, unsafe name or oversized)")
            continue
        entries.append((name, value))
    return entries


def _root_env(cfg: Config) -> list:
    data = _read_regular(cfg.models_env, MAX_ENV_FILE_BYTES)
    if data is None:
        raise PublishError("models.env cannot be read")
    return _env_entries(data, "models.env")


def _console_env(cfg: Config) -> list:
    if cfg.console_uid is None:
        return []
    try:
        data = execpolicy.read_console_file(
            cfg.console_dir, cfg.console_uid, "models.env", MAX_ENV_FILE_BYTES, 0o077)
    except execpolicy.PolicyError as exc:
        _warn(f"console models.env ignored: {exc}")
        return []
    return [] if data is None else _env_entries(data, "console models.env")


@dataclass(frozen=True, repr=False)
class _JobEnvironment:
    entries: tuple[tuple[str, str], ...]
    secrets: tuple[bytes, ...]


def _read_job_environment(cfg: Config) -> _JobEnvironment:
    """Take one snapshot for launch, live events, and all publication paths."""
    entries = _root_env(cfg) + _console_env(cfg)
    values = {value.encode("utf-8") for _, value in entries}
    # The console wins for injection. Shadowed values still need redaction.
    return _JobEnvironment(
        tuple(sorted(dict(entries).items())),
        tuple(sorted(v for v in values if len(v) >= livefeed.MIN_SECRET_BYTES)))


def _write_agent_env(cfg: Config, job_id: str, environment: _JobEnvironment) -> Path:
    path = cfg.work / check_job_id(job_id) / "agent.env"
    path.unlink(missing_ok=True)
    lines = "".join(f"{name}={value}\n" for name, value in environment.entries)
    fd = os.open(path, FILE_CREATE_FLAGS, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(lines)
    return path


def executor_config(cfg: Config, job_id: str, *, policy=None, observer=None) -> executor.Config:
    work = cfg.work / check_job_id(job_id)
    return executor.Config(
        image=cfg.exec_image, work_root=cfg.work, active_job_id=job_id,
        requests_dir=work / "exec" / "requests",
        results_dir=work / "exec" / "results", runs_dir=cfg.runs,
        agent_uid=cfg.agent_uid, agent_gid=cfg.agent_gid,
        docker_prefix=cfg.docker_prefix, policy=policy, observer=observer)


def _agent_cpus(cfg: Config) -> str | None:
    if cfg.agent_cpus is None:
        return None
    cpus = min(float(cfg.agent_cpus), float(os.cpu_count() or 1))
    return f"{cpus:g}"


def build_agent_argv(cfg: Config, job_id: str) -> list[str]:
    work = cfg.work / check_job_id(job_id)
    cpus = _agent_cpus(cfg)
    return [
        *cfg.docker_prefix, "run", "--rm", "--runtime=runsc", "--network", cfg.network,
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        f"--user={cfg.agent_uid}:{cfg.agent_gid}", "--memory=3g", "--memory-swap=3g",
        "--pids-limit=512", *([f"--cpus={cpus}"] if cpus else []),
        "--label", "re-lab.role=agent", "--label", f"re-lab.job={job_id}",
        "--name", f"re-lab-agent-{job_id}", "--pull=never",
        "--log-driver=none",
        "--tmpfs", "/home/re:size=512m,noexec,nosuid,nodev",
        "--tmpfs", "/tmp:size=512m,noexec,nosuid,nodev",
        "--env-file", str(work / "agent.env"),
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


class ExecPolicyGate:
    """Operator execution rules and approvals for one job (see execpolicy)."""

    def __init__(self, cfg: Config, job_id: str, feed):
        self.cfg = cfg
        self.job_id = job_id
        self.feed = feed
        self.policy = execpolicy.ALLOW_ALL

    def classify(self, argv) -> tuple:
        try:
            self.policy = execpolicy.load_policy(self.cfg.console_dir, self.cfg.console_uid)
        except execpolicy.PolicyError as exc:
            _warn(f"{self.job_id}: execution blocked: {exc}")
            return "block", "invalid execution policy", "execution policy is invalid"
        action, rule = execpolicy.evaluate(self.policy, argv)
        return action, rule, ("blocked by execution policy" if action == "block" else None)

    def await_approval(self, name: str, argv, rule, stop_event) -> tuple:
        approval_id = uuid.uuid4().hex
        try:
            execpolicy.discard_decision(self.cfg.console_dir, self.cfg.console_uid, self.job_id, name)
        except execpolicy.PolicyError:
            return "block", "approval request is invalid"
        timeout = self.policy.approval_timeout_s
        self.feed.emit("exec.approval_requested", name=name, argv=list(argv), rule=rule,
                       timeout_s=timeout, approval_id=approval_id)
        decision = execpolicy.wait_for_decision(
            self.cfg.console_dir, self.cfg.console_uid, self.job_id, name, approval_id,
            timeout, stop_event)
        self.feed.emit("exec.approval_resolved", name=name, decision=decision.decision,
                       source=decision.source, note=decision.note)
        if decision.source == "cancelled":
            return "cancelled", None
        if decision.decision == "approve":
            return "allow", None
        return "block", {"timeout": "approval timed out",
                         "invalid": "approval decision was invalid"}.get(
                             decision.source, "denied by operator")


def storage_state(cfg: Config) -> tuple:
    """(free bytes, free inodes, low) for the lab filesystem as an unprivileged writer sees it."""
    try:
        info = os.statvfs(cfg.root)
    except OSError:
        return 0, 0, True
    free_bytes = info.f_bavail * info.f_frsize
    free_inodes = info.f_favail if info.f_files else -1
    low = free_bytes < cfg.min_free_bytes or (
        info.f_files > 0 and info.f_favail < cfg.min_free_inodes)
    return free_bytes, free_inodes, bool(low)


def _cancel_requested(cfg: Config, job_id: str) -> bool:
    try:
        os.lstat(cfg.cancel_dir / job_id)
    except OSError:
        return False
    return True


def _clear_cancel(cfg: Config, job_id: str) -> None:
    try:
        os.unlink(cfg.cancel_dir / job_id)
    except OSError:
        pass


def _stop_reason(cfg: Config, job_id: str) -> str | None:
    if STOP.is_set():
        return "stopped"
    if _cancel_requested(cfg, job_id):
        return "cancelled"
    if storage_state(cfg)[2]:
        return "storage-cap"
    return None


def _queue_length(cfg: Config) -> int:
    try:
        return len(pending_jobs(cfg))
    except OSError:
        return 0


def _heartbeat(cfg: Config, state: str, active_job: str | None = None) -> None:
    if not cfg.live:
        return
    free_bytes, free_inodes, _low = storage_state(cfg)
    livefeed.write_heartbeat(
        cfg.live_root, cfg.report_gid, state=state, active_job=active_job,
        queue=_queue_length(cfg), free_bytes=free_bytes, free_inodes=free_inodes)


def run_agent(cfg: Config, job_id: str, wall_timeout_s: float = WALL_TIMEOUT_S, *,
              environment: _JobEnvironment | None = None, feed=None) -> AgentResult:
    work = cfg.work / check_job_id(job_id)
    if feed is None:
        feed = livefeed.NullFeed()
    policy = ExecPolicyGate(cfg, job_id, feed) if cfg.console_uid is not None else None
    exec_cfg = executor_config(cfg, job_id, policy=policy, observer=feed.emit)
    request_stop = threading.Event()
    request_ready = threading.Event()
    request_errors = []
    worker_started = False
    containment_attempted = False
    tailer = livefeed.SessionTailer(work / "session", feed)

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
    env_file = _write_agent_env(cfg, job_id, environment or _read_job_environment(cfg))
    try:
        (work / "agent.cid").unlink(missing_ok=True)
        process = subprocess.Popen(
            build_agent_argv(cfg, job_id), shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except BaseException:
        env_file.unlink(missing_ok=True)
        raise
    try:
        request_worker.start()
        worker_started = True
        if not request_ready.wait(KILL_GRACE_S):
            raise RuntimeError("execution worker did not become ready")
        started = time.monotonic()
        timed_out = False
        request_failed = False
        stop_reason = None
        tick = 0
        while process.poll() is None:
            if request_errors:
                request_failed = True
                break
            if time.monotonic() - started >= wall_timeout_s:
                timed_out = True
                stop_reason = "time-cap"
                break
            reason = _stop_reason(cfg, job_id)
            if reason is not None:
                stop_reason = reason
                if reason == "cancelled":
                    feed.emit("job.cancel_requested")
                elif reason == "storage-cap":
                    free_bytes, free_inodes, _low = storage_state(cfg)
                    feed.emit("storage.low", free_bytes=free_bytes, free_inodes=free_inodes)
                break
            tailer.poll()
            tick += 1
            if tick % HEARTBEAT_TICKS == 0:
                _heartbeat(cfg, "running", job_id)
            time.sleep(POLL_INTERVAL_S)
        request_stop.set()
        if stop_reason is not None or request_failed:
            executor.stop_docker_client(process)
        exit_code = process.wait(timeout=KILL_GRACE_S)
        # Only a reaped client can no longer start a container after the absence check.
        contain_agent()
        request_worker.join(WORKER_JOIN_S)
        if request_worker.is_alive():
            raise RuntimeError("execution worker did not stop after cancellation")
        for _ in range(8):
            tailer.poll()
        if request_errors:
            if isinstance(request_errors[0], executor.ContainmentError):
                raise request_errors[0]
            _warn(f"execution worker failed: {type(request_errors[0]).__name__}")
            request_failed = True
        if request_failed and exit_code == 0:
            exit_code = 1
        feed.emit("agent.exited", exit_code=exit_code, timed_out=timed_out)
        return AgentResult(exit_code=exit_code, timed_out=timed_out, stop_reason=stop_reason)
    except Exception as exc:
        failure = exc if isinstance(exc, executor.ContainmentError) else None
        try:
            request_stop.set()
        except Exception as cleanup_error:
            failure = failure or executor.ContainmentError(
                f"execution worker cancellation failed: {type(cleanup_error).__name__}")
        if not containment_attempted:
            try:
                executor.stop_docker_client(process)
            except executor.ContainmentError as client_error:
                failure = failure or client_error
            try:
                contain_agent()
            except executor.ContainmentError as containment_error:
                failure = failure or containment_error
        if worker_started:
            try:
                request_worker.join(WORKER_JOIN_S)
                if request_worker.is_alive() and failure is None:
                    failure = executor.ContainmentError(
                        "execution worker did not stop after cancellation")
            except Exception as cleanup_error:
                failure = failure or executor.ContainmentError(
                    f"execution worker cleanup failed: {type(cleanup_error).__name__}")
        if failure is not None:
            raise failure
        raise executor.ContainmentError(
            f"agent lifecycle failed: {type(exc).__name__}") from None
    finally:
        env_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------------------------
# Redaction and publication.

class _Redactor:
    def __init__(self, secrets: tuple):
        variants = livefeed.secret_variants(secrets)
        self.pattern = re.compile(b"|".join(re.escape(v) for v in variants)) if variants else None

    def text(self, data: bytes) -> bytes:
        return self.pattern.sub(b"[REDACTED]", data) if self.pattern else data

    def binary(self, data: bytes) -> bytes:
        if not self.pattern:
            return data
        return self.pattern.sub(lambda match: b"*" * len(match.group(0)), data)

    def auto(self, data: bytes) -> bytes:
        """Text keeps a visible marker; binary keeps its length so offsets stay valid."""
        return self.binary(data) if b"\x00" in data[:8192] else self.text(data)


_REDACTORS: dict = {}


def _redactor(secrets: tuple) -> _Redactor:
    key = tuple(secrets)
    if key not in _REDACTORS:
        if len(_REDACTORS) > 16:
            _REDACTORS.clear()
        _REDACTORS[key] = _Redactor(key)
    return _REDACTORS[key]


def _redact(data: bytes, secrets: tuple) -> bytes:
    return _redactor(secrets).text(data)


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


def _publish_name(name: str, redactor: _Redactor) -> str | None:
    if not _valid_name(name):
        return None
    cleaned = redactor.text(name.encode("utf-8")).decode("utf-8", "replace")
    return cleaned if _valid_name(cleaned) else None


def _copy_evidence(
        source: Path, destination: Path, budget: _ScanBudget,
        secrets: tuple, report_gid: int = 10001, depth: int = 0) -> None:
    """Copy agent evidence through pinned descriptors; links and special files are skipped."""
    try:
        src_fd = os.open(source, DIR_FLAGS)
    except OSError:
        return
    try:
        _copy_evidence_fd(src_fd, Path(destination), budget, _redactor(secrets), report_gid, depth)
    finally:
        os.close(src_fd)


def _copy_evidence_fd(src_fd: int, destination: Path, budget: _ScanBudget,
                      redactor: _Redactor, report_gid: int, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise PublishError("published evidence is nested too deeply")
    with os.scandir(src_fd) as entries:
        for entry in entries:
            budget.entry()
            if entry.is_symlink():
                continue
            published = _publish_name(entry.name, redactor)
            if published is None:
                continue
            target = destination / published
            if entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(entry.name, DIR_FLAGS, dir_fd=src_fd)
                except OSError:
                    continue
                try:
                    _make_report_dir(target, report_gid)
                    _copy_evidence_fd(child, target, budget, redactor, report_gid, depth + 1)
                finally:
                    os.close(child)
            elif entry.is_file(follow_symlinks=False):
                data = _read_at(src_fd, entry.name, MAX_EVIDENCE_FILE_BYTES)
                if data is None or not budget.fits(len(data)):
                    continue
                data = redactor.auto(data)
                if not budget.fits(len(data)):
                    continue
                try:
                    _write_exclusive(target, data, report_gid)
                except FileExistsError:
                    continue
                budget.take(len(data))


def _record_cost(record) -> float | None:
    """The billed cost of one session record: assistant turns and off-transcript model_usage."""
    if not isinstance(record, dict):
        return None
    if record.get("type") == "model_usage":
        usage = record.get("usage")
    else:
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        usage = message.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    value = cost.get("total") if isinstance(cost, dict) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value


def _session_cost_checked(data: bytes) -> tuple[float, bool]:
    total = 0.0
    incomplete = False
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            incomplete = True
            continue
        value = _record_cost(record)
        if value is None:
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
        source: Path, destination: Path, secrets: tuple,
        report_gid: int = 10001) -> None:
    remaining = MAX_SESSION_BYTES
    redactor = _redactor(secrets)
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
                            data = redactor.text(data)[:remaining]
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
    """The agent's validated claims: status, plus a reason or an error code where allowed.

    Everything else in out/status.json (exit code, model, times, free text) is ignored: the
    dispatcher records those facts itself."""
    data = _read_regular(work / "out" / "status.json", STATUS_MAX_BYTES)
    if data is None:
        return {"status": "failed", "error": "agent status missing"}
    try:
        body = json.loads(data)
    except (ValueError, RecursionError):
        body = None
    status = body.get("status") if isinstance(body, dict) else None
    if (not isinstance(body, dict) or body.get("job_id") != job_id
            or not isinstance(status, str) or status not in AGENT_STATUSES):
        return {"status": "failed", "error": "agent status invalid"}
    claims = {"status": status}
    if status == "needs-vm-lane":
        reason = body.get("reason")
        if (not isinstance(reason, str) or not 1 <= len(reason) <= REASON_MAX_CHARS
                or not reason.isprintable()):
            return {"status": "failed", "error": "agent status invalid"}
        claims["reason"] = reason
    error = body.get("error")
    if status == "failed" and isinstance(error, str) and AGENT_ERROR_RE.fullmatch(error):
        claims["error"] = error
    return claims


def _clean_result(result: dict, secrets: tuple) -> dict:
    redactor = _redactor(secrets)
    clean = {}
    for key, value in result.items():
        if isinstance(value, str):
            value = redactor.text(value.encode("utf-8", "replace")).decode("utf-8", "replace")
            value = "".join(ch for ch in value if ch.isprintable())[:RESULT_TEXT_MAX]
        elif isinstance(value, float) and not math.isfinite(value):
            value = None
        elif isinstance(value, bool) or value is None or isinstance(value, int):
            pass
        elif not isinstance(value, (int, float)):
            value = None
        clean[key] = value
    return clean


def _write_result(
        path: Path, result: dict, secrets: tuple = (),
        report_gid: int = 10001) -> dict:
    """Atomically write result.json from typed fields; strings are redacted one by one."""
    _make_report_dir(path.parent, report_gid, exist_ok=True)
    clean = _clean_result(result, secrets)
    raw = (json.dumps(clean, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".result-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        _set_report_mode(Path(temporary), report_gid, 0o640)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return clean


def _job_meta(cfg: Config, job_id: str) -> dict:
    data = _read_regular(cfg.work / job_id / "job.json", MAX_MANIFEST_BYTES)
    try:
        meta = json.loads(data) if data is not None else {}
    except (ValueError, RecursionError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _job_model(meta: dict) -> str | None:
    model = meta.get("model")
    return model if isinstance(model, str) and MODEL_RE.fullmatch(model) else None


def _default_result(cfg: Config, job_id: str) -> dict:
    claims = _read_agent_status(cfg.work / job_id, job_id)
    now = utc_now()
    result = {"job_id": job_id, "status": claims["status"], "exit_code": None,
              "model": _job_model(_job_meta(cfg, job_id)), "started": now, "ended": now}
    for key in ("reason", "error"):
        if key in claims:
            result[key] = claims[key]
    return result


def publish_job(cfg: Config, job_id: str, status: dict | None = None, *, secrets: tuple) -> dict:
    work = cfg.work / check_job_id(job_id)
    report = cfg.reports / job_id
    _make_report_dir(report, cfg.report_gid)
    redactor = _redactor(secrets)
    result = dict(status) if status is not None else _default_result(cfg, job_id)
    report_data = _read_regular(work / "out" / "report.md", MAX_REPORT_BYTES)
    if report_data is not None:
        _write_exclusive(
            report / "report.md", redactor.text(report_data)[:MAX_REPORT_BYTES], cfg.report_gid)
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
    return _write_result(report / "result.json", result, secrets, cfg.report_gid)


# ---------------------------------------------------------------------------------------------
# Job lifecycle.

def _open_feed(cfg: Config, job_id: str, secrets: tuple):
    if not cfg.live:
        return livefeed.NullFeed()
    return livefeed.LiveFeed(cfg.live_root, job_id, cfg.report_gid, secrets).open()


def _finish_feed(feed, result: dict) -> None:
    feed.emit("job.finished", status=result.get("status"), cost_usd=result.get("cost_usd", 0.0),
              cost_incomplete=bool(result.get("cost_incomplete", False)),
              error=result.get("error"))


def recover_interrupted(cfg: Config) -> list[str]:
    recovered = []
    with os.scandir(cfg.work) as entries:
        names = [entry.name for entry in entries
                 if JOB_ID_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)]
    for name in sorted(names):
        result_path = cfg.reports / name / "result.json"
        if result_path.exists():
            continue
        cost, incomplete = _scan_session_cost(cfg.work / name / "session")
        result = {"job_id": name, "status": "recovered-after-restart",
                  "exit_code": None, "cost_usd": cost,
                  "cost_incomplete": incomplete, "model": None,
                  "started": utc_now(), "ended": utc_now()}
        try:
            _write_result(result_path, result, report_gid=cfg.report_gid)
        except OSError as exc:
            _warn(f"{name}: recovery result could not be written: {type(exc).__name__}")
            continue
        # Recovery emits only dispatcher-owned status and cost, never old job payloads.
        feed = _open_feed(cfg, name, ())
        _finish_feed(feed, result)
        feed.close()
        recovered.append(name)
    return recovered


def _terminal_status(job_id: str, status: str, error: str) -> dict:
    return {"job_id": job_id, "status": status, "exit_code": None,
            "cost_usd": 0.0, "cost_incomplete": False, "model": None,
            "started": utc_now(), "ended": utc_now(), "error": error}


def _record_containment_failure(cfg: Config, job_id: str, feed=None) -> None:
    status = _terminal_status(job_id, "failed", "containment failed")
    cost, _incomplete = _scan_session_cost(cfg.work / job_id / "session")
    status.update(cost_usd=cost, cost_incomplete=True)
    try:
        _write_result(
            cfg.reports / job_id / "result.json", status,
            report_gid=cfg.report_gid)
    except OSError as exc:
        _warn(f"containment result write failed: {type(exc).__name__}")
    if feed is not None:
        _finish_feed(feed, status)


def _final_status(agent: AgentResult, claims: dict) -> dict:
    if agent.timed_out or agent.stop_reason == "time-cap":
        return {"status": "time-cap"}
    if agent.stop_reason == "storage-cap":
        return {"status": "storage-cap", "error": "lab storage reserve reached"}
    if agent.stop_reason == "cancelled":
        return {"status": "cancelled", "error": "cancelled by request"}
    if agent.stop_reason == "stopped":
        return {"status": "failed", "error": "dispatcher stopped"}
    if agent.exit_code != 0:
        error = claims.get("error") if claims.get("status") == "failed" else None
        return {"status": "failed", "error": error or f"agent exit {agent.exit_code}"}
    return dict(claims)


def _verified_event(feed, meta: dict) -> None:
    files = meta.get("files") if isinstance(meta.get("files"), list) else []
    agent = meta.get("agent")
    conversation = meta.get("conversation")
    max_cost = meta.get("max_cost_usd")
    feed.emit("job.verified", files=len(files),
              bytes=sum(item.get("size", 0) for item in files
                        if isinstance(item, dict) and type(item.get("size")) is int),
              model=_job_model(meta),
              max_cost_usd=max_cost if isinstance(max_cost, (int, float))
              and not isinstance(max_cost, bool) else None,
              agent=agent if isinstance(agent, str) and SLUG_RE.fullmatch(agent) else None,
              conversation=conversation if isinstance(conversation, str)
              and CONVERSATION_RE.fullmatch(conversation) else None)


MAX_FEED_FILES = 200
FEED_FILES_CHUNK = 40


def _files_events(feed, meta: dict) -> None:
    """Publish the verified file list (bounded) so the console can show what is analysed."""
    files = [item for item in meta.get("files", []) if isinstance(item, dict)]
    entries = [{"path": item.get("path"), "size": item.get("size"), "sha256": item.get("sha256")}
               for item in files[:MAX_FEED_FILES]]
    for offset in range(0, len(entries), FEED_FILES_CHUNK):
        feed.emit("job.files", offset=offset, total=len(files),
                  entries=entries[offset:offset + FEED_FILES_CHUNK])


def run_claimed_job(cfg: Config, job_id: str) -> dict:
    check_job_id(job_id)
    try:
        environment = _read_job_environment(cfg)
    except (DispatcherError, OSError):
        result = _terminal_status(job_id, "failed", "model environment unreadable")
        return _write_result(cfg.reports / job_id / "result.json", result, report_gid=cfg.report_gid)
    feed = _open_feed(cfg, job_id, environment.secrets)
    try:
        return _run_claimed_job(cfg, job_id, feed, environment)
    finally:
        feed.close()


def _reject(cfg: Config, job_id: str, feed, secrets: tuple, status: str, error: str) -> dict:
    result = _terminal_status(job_id, status, error)
    feed.emit("job.rejected", status=status, error=error)
    written = _write_result(cfg.reports / job_id / "result.json", result, secrets, cfg.report_gid)
    _finish_feed(feed, written)
    return written


def _run_claimed_job(cfg: Config, job_id: str, feed, environment: _JobEnvironment) -> dict:
    secrets = environment.secrets
    result_path = cfg.reports / job_id / "result.json"
    feed.emit("job.claimed")
    _heartbeat(cfg, "running", job_id)
    if _cancel_requested(cfg, job_id):
        _clear_cancel(cfg, job_id)
        return _reject(cfg, job_id, feed, secrets, "cancelled", "cancelled before start")
    try:
        meta = prepare_job(cfg, job_id)
    except JobError as exc:
        return _reject(cfg, job_id, feed, secrets, "rejected-job", str(exc))
    except HashMismatch as exc:
        return _reject(cfg, job_id, feed, secrets, "rejected-hash", str(exc))
    except (OSError, executor.RequestError) as exc:
        return _reject(cfg, job_id, feed, secrets, "failed",
                       f"host preparation failed: {type(exc).__name__}")
    _verified_event(feed, meta)
    _files_events(feed, meta)
    model = _job_model(meta)
    feed.emit("agent.started", model=model)
    started = utc_now()
    try:
        agent = run_agent(cfg, job_id, environment=environment, feed=feed)
    except executor.ContainmentError:
        _record_containment_failure(cfg, job_id, feed)
        raise
    except Exception as exc:
        status = _terminal_status(
            job_id, "failed", f"agent execution failed: {type(exc).__name__}")
        status["cost_usd"], status["cost_incomplete"] = _scan_session_cost(
            cfg.work / job_id / "session")
        written = _write_result(result_path, status, secrets, cfg.report_gid)
        _finish_feed(feed, written)
        return written
    ended = utc_now()
    try:
        claims = _read_agent_status(cfg.work / job_id, job_id)
    except (OSError, RuntimeError, ValueError, TypeError, RecursionError):
        claims = {"status": "failed", "error": "agent status invalid"}
    final = _final_status(agent, claims)
    status = {"job_id": job_id, "status": final["status"], "exit_code": agent.exit_code,
              "model": model, "started": started, "ended": ended}
    for key in ("reason", "error"):
        if key in final:
            status[key] = final[key]
    if agent.stop_reason == "cancelled":
        _clear_cancel(cfg, job_id)
    try:
        published = publish_job(cfg, job_id, status, secrets=secrets)
    except executor.ContainmentError:
        _record_containment_failure(cfg, job_id, feed)
        raise
    except Exception as exc:
        cost, _incomplete = _scan_session_cost(cfg.work / job_id / "session")
        status.update(
            status="failed", cost_usd=cost, cost_incomplete=True, ended=utc_now(),
            error=f"publication failed: {type(exc).__name__}")
        published = _write_result(result_path, status, secrets, cfg.report_gid)
    if published.get("status") == "storage-cap":
        for name in ("out", "session"):
            try:
                executor._rmtree_force(cfg.work / job_id / name)
            except OSError as exc:
                _warn(f"{job_id}: {name}/ could not be removed after the storage cap: "
                      f"{type(exc).__name__}")
    _finish_feed(feed, published)
    return published


def _labeled_containers(cfg: Config) -> list[str]:
    try:
        result = subprocess.run(
            [*cfg.docker_prefix, "ps", "-aq", "--no-trunc",
             "--filter", "label=re-lab.role"],
            capture_output=True, timeout=KILL_GRACE_S, check=False, text=True,
            stdin=subprocess.DEVNULL)
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
    """Remove every labeled agent/exec container and prove none remain.

    'docker rm -f' may report a conflict for a container the daemon is already removing
    (--rm), so its exit status is not trusted either way: the re-list decides."""
    container_ids = _labeled_containers(cfg)
    if not container_ids:
        return
    try:
        subprocess.run(
            [*cfg.docker_prefix, "rm", "-f", *container_ids],
            capture_output=True, timeout=KILL_GRACE_S, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise executor.ContainmentError(
            f"startup container cleanup failed: {type(exc).__name__}") from None
    waited = 0.0
    while _labeled_containers(cfg):
        if waited >= executor.REMOVAL_WAIT_S:
            raise executor.ContainmentError("startup containers remain after cleanup")
        time.sleep(executor.REMOVAL_POLL_S)
        waited += executor.REMOVAL_POLL_S


_STORAGE_LOW_LOGGED = [False]


def process_once(cfg: Config, run_job=run_claimed_job) -> bool:
    if STOP.is_set():
        return False
    jobs = [job for job in pending_jobs(cfg) if job not in _SKIPPED]
    if not jobs:
        return False
    if storage_state(cfg)[2]:
        if not _STORAGE_LOW_LOGGED[0]:
            _warn("lab storage is below its reserve; new jobs wait until space is freed")
            _STORAGE_LOW_LOGGED[0] = True
        _heartbeat(cfg, "storage-low")
        return False
    _STORAGE_LOW_LOGGED[0] = False
    for job_id in jobs:
        try:
            claim_job(cfg, job_id)
        except JobError as exc:
            _warn(f"{job_id}: claim refused: {exc}")
            _quarantine_inbox_entry(cfg, job_id)
            continue
        try:
            run_job(cfg, job_id)
        except executor.ContainmentError:
            raise
        except Exception as exc:
            _warn(f"{job_id}: job ended by an unexpected error: {type(exc).__name__}")
            try:
                if not (cfg.reports / job_id / "result.json").exists():
                    _write_result(cfg.reports / job_id / "result.json", _terminal_status(
                        job_id, "failed", f"dispatcher error: {type(exc).__name__}"),
                        report_gid=cfg.report_gid)
            except OSError as write_error:
                _warn(f"{job_id}: failure result could not be written: "
                      f"{type(write_error).__name__}")
        _heartbeat(cfg, "idle")
        return True
    return False


# ---------------------------------------------------------------------------------------------
# Operator commands.

def _layout(cfg: Config) -> list:
    """(relative path, owner uid, group gid, mode) for every directory the lab needs."""
    entries = [
        ("", 0, 0, 0o755), ("jobs", 0, 0, 0o755),
        ("jobs/inbox", 0, cfg.report_gid, 0o2770), ("jobs/cancel", 0, cfg.report_gid, 0o2770),
        ("jobs/work", 0, 0, 0o700), ("jobs/quarantine", 0, 0, 0o700),
        ("reports", 0, cfg.report_gid, 0o750), ("exec", 0, 0, 0o700),
        ("exec/runs", 0, 0, 0o700), ("live", 0, cfg.report_gid, 0o750),
        ("secrets", 0, 0, 0o700)]
    if cfg.console_uid is not None:
        entries += [("console", cfg.console_uid, cfg.report_gid, 0o750),
                    ("console/approvals", cfg.console_uid, cfg.report_gid, 0o750)]
    return entries


def init_layout(cfg: Config) -> list[str]:
    """Create or repair the lab directory tree with exact owners and modes (idempotent)."""
    changed = []
    for relative, uid, gid, mode in _layout(cfg):
        path = cfg.root / relative if relative else cfg.root
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            info = os.lstat(path)
            changed.append(f"created {path}")
        if not stat.S_ISDIR(info.st_mode):
            raise DispatcherError(f"{path} exists and is not a real directory")
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
            if _is_root():
                os.chown(path, uid, gid, follow_symlinks=False)
            os.chmod(path, mode, follow_symlinks=False)
            changed.append(f"set {path} to {uid}:{gid} {mode:04o}")
    return changed


def _check_dir(results: list, path: Path, uid: int, gid: int, mode: int) -> None:
    """Fail only on security-relevant deviations; report cosmetic ones as warnings.

    Security-relevant: missing required directories, links, the wrong owner, write access
    for anyone but the owner (and the report group where the layout grants it), and any
    group/other access to the private executor runs root."""
    optional = path.name in ("cancel", "quarantine", "live", "approvals")
    try:
        info = os.lstat(path)
    except OSError:
        results.append(("WARN" if optional else "FAIL",
                        f"{path} is missing (run --init-layout)"))
        return
    actual = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode):
        results.append(("FAIL", f"{path} is not a real directory"))
        return
    allowed_write = 0o200 | (0o020 if mode & 0o020 else 0)
    problems = []
    if info.st_uid != uid:
        problems.append(f"owner {info.st_uid} (expected {uid})")
    if actual & 0o222 & ~allowed_write:
        problems.append(f"mode {actual:04o} lets others write")
    if path.name == "runs" and actual & 0o077:
        problems.append(f"mode {actual:04o} must grant no group or other access")
    if problems:
        results.append(("FAIL", f"{path}: " + ", ".join(problems)))
    elif info.st_gid != gid or actual != mode:
        results.append(("WARN", f"{path} is {info.st_uid}:{info.st_gid} {actual:04o}; "
                                f"--init-layout sets {uid}:{gid} {mode:04o}"))
    else:
        results.append(("PASS", f"{path} {mode:04o}"))


def _docker_text(cfg: Config, *args: str) -> tuple:
    try:
        done = subprocess.run([*cfg.docker_prefix, *args], capture_output=True, text=True,
                              timeout=30, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}"
    return done.returncode, (done.stdout or "").strip()


def _version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def preflight(cfg: Config) -> list:
    """Read-only deployment checks; returns (level, message) pairs."""
    results = []
    if sys.version_info < (3, 12):
        results.append(("FAIL", "python 3.12 or newer is required"))
    for relative, uid, gid, mode in _layout(cfg):
        _check_dir(results, cfg.root / relative if relative else cfg.root, uid, gid, mode)
    try:
        info = os.lstat(cfg.models_env)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077):
            results.append(("FAIL", f"{cfg.models_env} must be a regular root file, mode 0600"))
        else:
            results.append(("PASS", f"{cfg.models_env} is private"))
    except OSError:
        results.append(("FAIL", f"{cfg.models_env} is missing"))
    free_bytes, free_inodes, low = storage_state(cfg)
    results.append(("WARN" if low else "PASS",
                    f"storage: {free_bytes // 1024 ** 2} MiB and {free_inodes} inodes free "
                    f"(reserve {cfg.min_free_bytes // 1024 ** 2} MiB, {cfg.min_free_inodes} inodes)"))
    code, version = _docker_text(cfg, "version", "--format", "{{.Server.Version}}")
    if code != 0 or not version:
        results.append(("FAIL", "docker server is not reachable"))
    else:
        parsed = _version_tuple(version)
        if parsed < (28, 0, 0):
            results.append(("FAIL", f"docker {version}: 28.0+ is required for the isolated gateway"))
        elif parsed < (29, 0, 0):
            results.append(("WARN", f"docker {version}: 29.x carries the 2026 security fixes"))
        else:
            results.append(("PASS", f"docker {version}"))
        code, runtimes = _docker_text(cfg, "info", "--format", "{{json .Runtimes}}")
        results.append(("PASS", "runsc runtime registered") if code == 0 and '"runsc"' in runtimes
                       else ("FAIL", "docker info does not list the runsc runtime"))
        for name, image in (("agent", cfg.agent_image), ("exec", cfg.exec_image)):
            code, _ = _docker_text(cfg, "image", "inspect", "--format", "{{.Id}}", image)
            results.append(("PASS", f"{name} image present") if code == 0
                           else ("FAIL", f"{name} image {image} is not present"))
        code, network = _docker_text(cfg, "network", "inspect", "--format", "{{json .}}", cfg.network)
        try:
            body = json.loads(network) if code == 0 else {}
        except ValueError:
            body = {}
        options = body.get("Options") or {}
        subnets = [item.get("Subnet") for item in (body.get("IPAM") or {}).get("Config") or []]
        if (body.get("Internal") is True
                and options.get("com.docker.network.bridge.gateway_mode_ipv4") == "isolated"
                and EGRESS_SUBNET in subnets):
            results.append(("PASS", f"network {cfg.network} is internal with an isolated gateway"))
        else:
            results.append(("FAIL", f"network {cfg.network} must exist, be internal, use subnet "
                                    f"{EGRESS_SUBNET} and gateway_mode_ipv4=isolated"))
        code, egress = _docker_text(cfg, "ps", "-q", "--filter", "status=running",
                                    "--filter", "label=com.docker.compose.service=re-egress")
        results.append(("PASS", "re-egress is running") if code == 0 and egress
                       else ("WARN", "the re-egress proxy container is not running"))
    runsc = shutil.which("runsc")
    if runsc:
        try:
            output = subprocess.run([runsc, "--version"], capture_output=True, text=True,
                                    timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        match = re.search(r"release-(\d{8})\.(\d+)", output)
        if match and (int(match.group(1)), int(match.group(2))) >= (20260831, 0):
            results.append(("PASS", f"runsc release-{match.group(1)}.{match.group(2)}"))
        else:
            results.append(("WARN", "runsc release-20260831.0 or later is recommended "
                                    "(CVE-2026-95702, CVE-2026-96812)"))
    else:
        results.append(("WARN", "runsc is not on PATH; its version was not checked"))
    return results


def prune(cfg: Config, older_than_days: float, include_reports: bool = False,
          now: float | None = None) -> list[str]:
    """Delete finished jobs' work trees (and optionally reports) older than the cutoff.

    A job is finished when reports/<id>/result.json exists; the active job never has one,
    so it is never selected."""
    if older_than_days < 0:
        raise DispatcherError("the prune age must not be negative")
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    names = set()
    for directory in (cfg.work, cfg.reports):
        try:
            with os.scandir(directory) as entries:
                names |= {entry.name for entry in entries if JOB_ID_RE.fullmatch(entry.name)}
        except OSError:
            continue
    removed = []
    for name in sorted(names):
        try:
            finished = os.stat(cfg.reports / name / "result.json").st_mtime
        except OSError:
            continue
        if finished > cutoff:
            continue
        targets = [cfg.work / name, cfg.live_root / name]
        if include_reports:
            targets.append(cfg.reports / name)
        for target in targets:
            executor._rmtree_force(target)
        removed.append(name)
    try:
        with os.scandir(cfg.quarantine) as entries:
            stale = [Path(entry.path) for entry in entries
                     if entry.stat(follow_symlinks=False).st_mtime <= cutoff]
    except OSError:
        stale = []
    for path in stale:
        executor._rmtree_force(path)
        removed.append(f"quarantine/{path.name}")
    return removed


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise DispatcherError("the config must be a JSON object")
        unknown = sorted(set(raw) - CONFIG_KEYS)
        if unknown:
            raise DispatcherError("unknown config key: " + ", ".join(unknown[:5]))
        prefix = raw.get("docker_prefix", ["docker"])
        if not isinstance(prefix, list):
            raise DispatcherError("docker_prefix must be a list of strings")
        free_mib = raw.get("min_free_mib", 1024)
        if not _positive_int(free_mib, 0):
            raise DispatcherError("min_free_mib must be a non-negative integer")
        root = Path(raw.get("root", "/srv/re-lab"))
        return Config(
            root=root, agent_image=raw["agent_image"], exec_image=raw["exec_image"],
            models_env=Path(raw.get("models_env", str(root / "secrets" / "models.env"))),
            network=raw.get("network", "re-lab_re-internal"),
            agent_uid=raw.get("agent_uid", 10001), agent_gid=raw.get("agent_gid", 10001),
            report_gid=raw.get("report_gid", 10001), docker_prefix=tuple(prefix),
            console_uid=raw.get("console_uid"), agent_cpus=raw.get("agent_cpus", 4.0),
            min_free_bytes=free_mib * 1024 ** 2,
            min_free_inodes=raw.get("min_free_inodes", 20000), live=raw.get("live", True))
    except (OSError, ValueError, KeyError, TypeError, DispatcherError) as exc:
        raise SystemExit(f"re-dispatcher: invalid config: {exc}") from None


def _install_signal_handlers() -> dict:
    previous = {}

    def request_stop(_signum, _frame):
        STOP.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass
    return previous


def _restore_signal_handlers(previous: dict) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError, TypeError):
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="process at most one job and exit")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--check", action="store_true",
                         help="run read-only deployment checks and exit")
    command.add_argument("--init-layout", action="store_true",
                         help="create or repair the lab directory tree and exit")
    command.add_argument("--cleanup-only", action="store_true",
                         help="remove every labeled job container and exit (ExecStopPost)")
    command.add_argument("--prune", type=float, metavar="DAYS",
                         help="delete finished jobs' work trees older than DAYS and exit")
    parser.add_argument("--include-reports", action="store_true",
                        help="with --prune, also delete the published reports")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.check:
        results = preflight(cfg)
        for level, message in results:
            print(f"{level:4} {message}")
        return 1 if any(level == "FAIL" for level, _ in results) else 0
    if args.init_layout:
        for change in init_layout(cfg):
            print(change)
        return 0
    if args.cleanup_only:
        cleanup_containers(cfg)
        return 0
    if args.prune is not None:
        for name in prune(cfg, args.prune, args.include_reports):
            print(f"pruned {name}")
        return 0
    previous = _install_signal_handlers()
    try:
        cleanup_containers(cfg)
        executor.sweep_runs(executor_config(cfg, "job-placeholder"))
        for job_id in recover_interrupted(cfg):
            _log(f"{job_id}: recovered-after-restart")
        if args.once:
            process_once(cfg)
            return 0
        _heartbeat(cfg, "idle")
        last_heartbeat = time.monotonic()
        while not STOP.is_set():
            if not process_once(cfg):
                STOP.wait(args.poll_interval)
            if time.monotonic() - last_heartbeat >= IDLE_HEARTBEAT_S:
                _heartbeat(cfg, "idle")
                last_heartbeat = time.monotonic()
        _heartbeat(cfg, "stopped")
        return 0
    finally:
        _restore_signal_handlers(previous)


if __name__ == "__main__":
    sys.exit(main())
