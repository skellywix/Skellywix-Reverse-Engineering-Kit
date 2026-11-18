#!/usr/bin/env python3
"""Operator execution policy and approval decisions for sandbox execution requests.

The local console owns ``<console_dir>`` and writes two kinds of input there:

* ``exec-policy.json``: ordered allow/ask/block rules for execution requests;
* ``approvals/<job_id>/<request_name>``: an operator's decision for one pending request.

The root dispatcher reads both through ``O_NOFOLLOW`` descriptors and accepts only regular,
single-link files owned by the configured console uid that group and other users cannot
write. A policy that exists but cannot be trusted or parsed blocks every request (fail
closed); a missing policy, or a dispatcher without a console uid, allows every request, which
is the behaviour of a deployment without the console.

Rules are an operator safety net, not a security boundary: every request already runs in a
fresh networkless gVisor container, and an agent can always obfuscate a command line. The
``ask`` action puts a human in the loop for each matching request.
"""
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass

POLICY_NAME = "exec-policy.json"
APPROVALS_DIR = "approvals"
MAX_POLICY_BYTES = 256 * 1024
MAX_DECISION_BYTES = 4096
MAX_RULES = 200
MAX_RULE_NAME = 80
MAX_RULE_VALUE = 256
MAX_RULE_ID = 64
MAX_NOTE = 500
ACTIONS = frozenset({"allow", "ask", "block"})
MATCHES = frozenset({"program", "contains", "prefix"})
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 30, 3600, 600
POLICY_KEYS = frozenset({"v", "default_action", "approval_timeout_s", "rules"})
RULE_KEYS = frozenset({"id", "name", "enabled", "priority", "match", "value", "action"})
JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
REQUEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.json$")
APPROVAL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
POLL_INTERVAL_S = 0.5
DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
              | getattr(os, "O_CLOEXEC", 0))


class PolicyError(Exception):
    """A console file exists but cannot be trusted or parsed. The message is fixed text."""


@dataclass(frozen=True)
class Rule:
    name: str
    enabled: bool
    priority: int
    match: str
    value: str
    action: str
    order: int


@dataclass(frozen=True)
class Policy:
    default_action: str = "allow"
    approval_timeout_s: int = TIMEOUT_DEFAULT
    rules: tuple = ()


ALLOW_ALL = Policy()


@dataclass(frozen=True)
class Decision:
    decision: str  # approve | deny
    source: str  # operator | timeout | cancelled | invalid
    note: str | None = None


def _clean_text(value: str, limit: int) -> str:
    return "".join(ch for ch in value if ch.isprintable())[:limit]


def _open_dir(name, owner_uid: int, dir_fd: int | None = None) -> int:
    fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
                or info.st_mode & 0o022):
            raise PolicyError("console directory is not private to the console user")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_owned(dir_fd: int, name: str, cap: int, owner_uid: int,
                forbidden_mode: int = 0o022) -> bytes | None:
    """Read one console file below ``dir_fd`` without following links; None when absent."""
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise PolicyError("console file cannot be opened safely") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != owner_uid or info.st_mode & forbidden_mode):
            raise PolicyError("console file is not a private regular file owned by the console")
        if info.st_size > cap:
            raise PolicyError("console file exceeds its size limit")
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise PolicyError("console file exceeds its size limit")
    return data


def read_console_file(console_dir, owner_uid: int, name: str, cap: int,
                      forbidden_mode: int = 0o022) -> bytes | None:
    """Read one console-owned file (None when absent); PolicyError when it is not trusted."""
    try:
        dir_fd = _open_dir(console_dir, owner_uid)
    except FileNotFoundError:
        return None
    except OSError:
        raise PolicyError("console directory cannot be opened safely") from None
    try:
        return _read_owned(dir_fd, name, cap, owner_uid, forbidden_mode)
    finally:
        os.close(dir_fd)


def _bounded_text(value, limit: int, *, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > limit or (not value and not allow_empty)
            or any(ch in value for ch in "\x00\r\n")):
        raise PolicyError("execution policy has an invalid text field")
    return value


def parse_policy(data: bytes) -> Policy:
    """Strictly validate the console policy document."""
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PolicyError("execution policy is not valid JSON") from None
    if not isinstance(body, dict) or set(body) - POLICY_KEYS or body.get("v") != 1:
        raise PolicyError("execution policy has an unsupported shape")
    default_action = body.get("default_action", "allow")
    if default_action not in ACTIONS:
        raise PolicyError("execution policy default_action is invalid")
    timeout = body.get("approval_timeout_s", TIMEOUT_DEFAULT)
    if type(timeout) is not int or not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
        raise PolicyError("execution policy approval_timeout_s is invalid")
    raw_rules = body.get("rules", [])
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
        raise PolicyError("execution policy rules are invalid")
    rules = []
    for order, item in enumerate(raw_rules):
        if not isinstance(item, dict) or set(item) - RULE_KEYS:
            raise PolicyError("execution policy rule has an unsupported shape")
        if "id" in item:
            _bounded_text(item["id"], MAX_RULE_ID)
        enabled = item.get("enabled", True)
        priority = item.get("priority", 0)
        if type(enabled) is not bool or type(priority) is not int or not -10**6 <= priority <= 10**6:
            raise PolicyError("execution policy rule enabled/priority is invalid")
        if item.get("match") not in MATCHES or item.get("action") not in ACTIONS:
            raise PolicyError("execution policy rule match/action is invalid")
        rules.append(Rule(
            name=_bounded_text(item.get("name"), MAX_RULE_NAME),
            enabled=enabled, priority=priority, match=item["match"],
            value=_bounded_text(item.get("value"), MAX_RULE_VALUE),
            action=item["action"], order=order))
    rules.sort(key=lambda rule: (-rule.priority, rule.order))
    return Policy(default_action, timeout, tuple(rules))


def load_policy(console_dir, owner_uid: int | None) -> Policy:
    """Return the console policy, ALLOW_ALL when there is none, or raise PolicyError."""
    if owner_uid is None:
        return ALLOW_ALL
    data = read_console_file(console_dir, owner_uid, POLICY_NAME, MAX_POLICY_BYTES)
    return ALLOW_ALL if data is None else parse_policy(data)


def evaluate(policy: Policy, argv) -> tuple[str, str | None]:
    """First enabled rule by descending priority wins; otherwise the default action."""
    joined = " ".join(argv)
    folded = joined.casefold()
    program = argv[0].rsplit("/", 1)[-1] if argv else ""
    for rule in policy.rules:
        if not rule.enabled:
            continue
        if rule.match == "program":
            hit = program == rule.value
        elif rule.match == "contains":
            hit = rule.value.casefold() in folded
        else:
            hit = joined.startswith(rule.value)
        if hit:
            return rule.action, rule.name
    return policy.default_action, None


def _approvals_job_dir(console_dir, owner_uid: int, job_id: str) -> int | None:
    """Pinned descriptor of approvals/<job_id>, or None when it does not exist yet."""
    try:
        console_fd = _open_dir(console_dir, owner_uid)
    except FileNotFoundError:
        return None
    try:
        try:
            approvals_fd = _open_dir(APPROVALS_DIR, owner_uid, console_fd)
        except FileNotFoundError:
            return None
        try:
            return _open_dir(job_id, owner_uid, approvals_fd)
        except FileNotFoundError:
            return None
        finally:
            os.close(approvals_fd)
    finally:
        os.close(console_fd)


def _check_names(job_id: str, request_name: str) -> None:
    if not JOB_ID_RE.fullmatch(job_id) or not REQUEST_NAME_RE.fullmatch(request_name):
        raise PolicyError("approval names are invalid")


def discard_decision(console_dir, owner_uid: int, job_id: str, request_name: str) -> None:
    """Remove any earlier decision for this request name so it can never be reused."""
    _check_names(job_id, request_name)
    try:
        job_fd = _approvals_job_dir(console_dir, owner_uid, job_id)
    except (OSError, PolicyError):
        return
    if job_fd is None:
        return
    try:
        os.unlink(request_name, dir_fd=job_fd)
    except OSError:
        pass
    finally:
        os.close(job_fd)


def read_decision(console_dir, owner_uid: int, job_id: str, request_name: str,
                  approval_id: str) -> Decision | None:
    """The operator decision for this approval, None while pending; consumed once read."""
    _check_names(job_id, request_name)
    try:
        job_fd = _approvals_job_dir(console_dir, owner_uid, job_id)
    except (OSError, PolicyError):
        return Decision("deny", "invalid", "approval directory is not trusted")
    if job_fd is None:
        return None
    try:
        try:
            data = _read_owned(job_fd, request_name, MAX_DECISION_BYTES, owner_uid)
        except PolicyError:
            data = b""
        if data is None:
            return None
        try:
            os.unlink(request_name, dir_fd=job_fd)
        except OSError:
            pass
    finally:
        os.close(job_fd)
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return Decision("deny", "invalid", "decision file is invalid")
    if (not isinstance(body, dict) or body.get("approval_id") != approval_id
            or body.get("decision") not in ("approve", "deny")):
        return Decision("deny", "invalid", "decision does not match the pending request")
    note = body.get("note")
    note = _clean_text(note, MAX_NOTE) if isinstance(note, str) and note else None
    return Decision(body["decision"], "operator", note)


def wait_for_decision(console_dir, owner_uid: int, job_id: str, request_name: str,
                      approval_id: str, timeout_s: float,
                      stop_event: threading.Event | None = None,
                      poll_s: float = POLL_INTERVAL_S) -> Decision:
    """Poll for the operator decision until it arrives, the timeout passes, or work stops."""
    if not APPROVAL_ID_RE.fullmatch(approval_id):
        raise PolicyError("approval id is invalid")
    deadline = time.monotonic() + timeout_s
    stop = stop_event or threading.Event()
    while True:
        if stop.is_set():
            return Decision("deny", "cancelled")
        decision = read_decision(console_dir, owner_uid, job_id, request_name, approval_id)
        if decision is not None:
            return decision
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Decision("deny", "timeout")
        stop.wait(min(poll_s, remaining))
