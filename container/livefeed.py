#!/usr/bin/env python3
"""Bounded, sanitized live event feed for the local console.

Only the root dispatcher writes it. ``<live_root>/dispatcher.json`` is an atomically replaced
heartbeat. ``<live_root>/<job_id>/events.jsonl`` is an append-only stream of JSON lines (at
most 16 KiB each, 16 MiB per job) describing one job: admission, model usage, tool calls,
sandbox requests and results, approvals, and the final status. Directories are mode 0750 and
files 0640, owned by root and the report group, so the console reads them without ever being
able to write them.

Most strings in the feed come from hostile data (model output, tool output, sandbox output).
Every string is redacted for the configured secret values, stripped of control and
bidirectional-override characters, and truncated before it is written. The session tailer
reads the agent's session tree while the agent is still running, so every directory and file
is opened relative to a pinned descriptor with ``O_NOFOLLOW``; links, special files, and
anything beyond the walk and byte caps are ignored. A feed failure never affects the job: the
feed disables itself instead of raising.
"""
import json
import math
import os
import re
import stat
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

FEED_VERSION = 1
FEED_NAME = "events.jsonl"
HEARTBEAT_NAME = "dispatcher.json"
MAX_EVENT_BYTES = 16 * 1024
MAX_FEED_BYTES = 16 * 1024 ** 2
TERMINAL_RESERVE_BYTES = 64 * 1024
TEXT_LIMIT = 1000
SHORT_LIMIT = 120
ARGV_ITEMS = 32
ARGV_ITEM_LIMIT = 300
MIN_SECRET_CHARS = 4
LONG_FIELDS = frozenset({"text", "input", "output", "stdout", "stderr", "error", "note"})
LIST_CAPS = {"entries": 40}  # lists of small records, e.g. job.files chunks
UNSAFE_CHARS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f؜​-‏ -‮⁦-⁩﻿]")
DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
              | getattr(os, "O_CLOEXEC", 0))


def utc_ms() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def utc_s() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _warn(message: str) -> None:
    print(f"re-dispatcher: live feed: {message}", file=sys.stderr, flush=True)


def secret_strings(secrets) -> tuple[str, ...]:
    """Secret values as text, longest first, ignoring values too short to be keys."""
    values = set()
    for secret in secrets:
        text = secret.decode("utf-8", "ignore") if isinstance(secret, bytes) else str(secret)
        if len(text) >= MIN_SECRET_CHARS:
            values.add(text)
    return tuple(sorted(values, key=len, reverse=True))


def clean_text(value, limit: int, secrets: tuple[str, ...] = ()) -> str:
    """Redact, strip control/bidi characters, and truncate one untrusted string."""
    text = value if isinstance(value, str) else str(value)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    text = UNSAFE_CHARS.sub("", text)
    if len(text) > limit:
        text = text[:max(limit - 1, 0)] + "…"
    return text


def _clean_value(key: str, value, secrets: tuple[str, ...], shrink: int = 1):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -2 ** 63 <= value < 2 ** 63 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        limit = TEXT_LIMIT if key in LONG_FIELDS else SHORT_LIMIT
        return clean_text(value, max(limit // shrink, 16), secrets)
    if isinstance(value, (list, tuple)):
        cap = LIST_CAPS.get(key, ARGV_ITEMS)
        items = []
        for item in list(value)[:cap]:
            if isinstance(item, dict):
                items.append({str(name)[:40]: _clean_value(str(name), field, secrets, shrink)
                              for name, field in list(item.items())[:8]
                              if not isinstance(field, (dict, list, tuple))})
            else:
                items.append(clean_text(item, max(ARGV_ITEM_LIMIT // shrink, 16), secrets))
        if len(value) > cap and all(isinstance(item, str) for item in items):
            items.append(f"… {len(value) - cap} more")
        return items
    return clean_text(value, max(SHORT_LIMIT // shrink, 16), secrets)


class LiveFeed:
    """Append-only, size-capped event writer for one job. Thread-safe; never raises."""

    def __init__(self, live_root, job_id: str, gid: int, secrets=()):
        self.live_root = Path(live_root)
        self.job_id = job_id
        self.gid = gid
        self.secrets = secret_strings(secrets)
        self._lock = threading.Lock()
        self._fd = -1
        self._seq = 0
        self._size = 0
        self._truncated = False
        self._disabled = False

    @property
    def path(self) -> Path:
        return self.live_root / self.job_id / FEED_NAME

    def open(self) -> "LiveFeed":
        try:
            ensure_live_root(self.live_root, self.gid)
            job_dir = self.live_root / self.job_id
            try:
                job_dir.mkdir(mode=0o750)
            except FileExistsError:
                if not stat.S_ISDIR(os.lstat(job_dir).st_mode):
                    raise OSError("live job path is not a directory") from None
            _own(job_dir, self.gid, 0o750)
            flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
            fd = os.open(self.path, flags, 0o640)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                raise OSError("live feed is not a regular file")
            if os.name == "posix" and os.geteuid() == 0:
                os.fchown(fd, 0, self.gid)
            os.fchmod(fd, 0o640)
            self._fd = fd
            self._size = info.st_size
            self._seq = _last_seq(self.path) if info.st_size else 0
        except OSError as exc:
            self._disabled = True
            _warn(f"{self.job_id}: disabled: {type(exc).__name__}")
        return self

    def emit(self, event_type: str, **fields) -> None:
        with self._lock:
            if self._disabled or self._fd < 0:
                return
            terminal = event_type == "job.finished"
            if self._truncated and not terminal:
                return
            try:
                line = self._encode(event_type, fields)
                limit = MAX_FEED_BYTES - (0 if terminal else TERMINAL_RESERVE_BYTES)
                if self._size + len(line) > limit:
                    if not terminal:
                        self._truncated = True
                        self._write(self._encode("feed.truncated", {}))
                    return
                self._write(line)
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                self._disabled = True
                _warn(f"{self.job_id}: disabled: {type(exc).__name__}")

    def _encode(self, event_type: str, fields: dict) -> bytes:
        for shrink in (1, 4, 16):
            event = {"v": FEED_VERSION, "seq": self._seq + 1, "ts": utc_ms(),
                     "job": self.job_id, "type": event_type}
            for key, value in fields.items():
                if key not in event:
                    event[key] = _clean_value(key, value, self.secrets, shrink)
            line = (json.dumps(event, ensure_ascii=False, separators=(",", ":"),
                               allow_nan=False) + "\n").encode("utf-8")
            if len(line) <= MAX_EVENT_BYTES:
                return line
        event = {"v": FEED_VERSION, "seq": self._seq + 1, "ts": utc_ms(),
                 "job": self.job_id, "type": event_type, "oversized": True}
        return (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")

    def _write(self, line: bytes) -> None:
        view = memoryview(line)
        while view:
            view = view[os.write(self._fd, view):]
        self._seq += 1
        self._size += len(line)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = -1


class NullFeed:
    """Stand-in when no live root is configured."""
    path = None

    def open(self):
        return self

    def emit(self, event_type: str, **fields) -> None:
        return None

    def close(self) -> None:
        return None


def _own(path: Path, gid: int, mode: int) -> None:
    if os.name == "posix" and os.geteuid() == 0:
        os.chown(path, 0, gid, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)


def ensure_live_root(live_root, gid: int) -> None:
    live_root = Path(live_root)
    try:
        live_root.mkdir(mode=0o750, parents=False)
    except FileExistsError:
        if not stat.S_ISDIR(os.lstat(live_root).st_mode):
            raise OSError("live root is not a directory") from None
    _own(live_root, gid, 0o750)


def _last_seq(path: Path) -> int:
    """Continue numbering after an existing feed (a restarted dispatcher re-opening it)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 2 * MAX_EVENT_BYTES))
            tail = handle.read()
    except OSError:
        return 0
    for raw in reversed(tail.splitlines()):
        try:
            seq = json.loads(raw).get("seq")
        except (ValueError, AttributeError, RecursionError):
            continue
        if type(seq) is int and seq > 0:
            return seq
    return 0


def write_heartbeat(live_root, gid: int, **fields) -> bool:
    """Atomically replace dispatcher.json; returns False instead of raising."""
    live_root = Path(live_root)
    body = {"v": FEED_VERSION, "ts": utc_s()}
    body.update(fields)
    try:
        ensure_live_root(live_root, gid)
        fd, temporary = tempfile.mkstemp(prefix=".dispatcher-", dir=live_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
                handle.write("\n")
            _own(Path(temporary), gid, 0o640)
            os.replace(temporary, live_root / HEARTBEAT_NAME)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except (OSError, ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------------------------
# Session tailing: agent session JSONL records -> feed events.

def _count(usage: dict, *keys) -> int:
    for key in keys:
        value = usage.get(key)
        if type(value) is int and 0 <= value < 10 ** 12:
            return value
        if isinstance(value, float) and math.isfinite(value) and 0 <= value < 1e12:
            return int(value)
    return 0


def _cost(usage: dict) -> float:
    cost = usage.get("cost")
    value = cost.get("total") if isinstance(cost, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        return float(value) if math.isfinite(value) and value >= 0 else 0.0
    except OverflowError:
        return 0.0


def _compact(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:4 * TEXT_LIMIT]
    except (ValueError, TypeError, RecursionError):
        return "<unprintable>"


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content[:64]:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        if sum(len(part) for part in parts) > 4 * TEXT_LIMIT:
            break
    return "\n".join(parts)


def record_events(record) -> list:
    """Map one session record to (event_type, fields) pairs; unknown shapes map to nothing."""
    if not isinstance(record, dict):
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        message = record if isinstance(record.get("role"), str) else None
    if message is None:
        return []
    role = message.get("role")
    events = []
    if role == "assistant":
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            events.append(("agent.text", {"text": content}))
        elif isinstance(content, list):
            for item in content[:64]:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "text" and isinstance(item.get("text"), str) and item["text"].strip():
                    events.append(("agent.text", {"text": item["text"]}))
                elif kind in ("toolCall", "tool_use", "toolUse", "function_call"):
                    arguments = item.get("arguments", item.get("input", item.get("args")))
                    call_id = item.get("id")
                    events.append(("tool.call", {
                        "tool": str(item.get("name") or "?"),
                        "call_id": call_id if isinstance(call_id, str) else None,
                        "input": _compact(arguments)}))
        usage = message.get("usage")
        if isinstance(usage, dict):
            model = message.get("model")
            provider = message.get("provider") or message.get("api")
            events.append(("llm.usage", {
                "model": model if isinstance(model, str) else None,
                "provider": provider if isinstance(provider, str) else None,
                "input": _count(usage, "input", "input_tokens", "inputTokens"),
                "output": _count(usage, "output", "output_tokens", "outputTokens"),
                "cache_read": _count(usage, "cacheRead", "cache_read_input_tokens", "cache_read"),
                "cache_write": _count(
                    usage, "cacheWrite", "cache_creation_input_tokens", "cache_write"),
                "cost_usd": round(_cost(usage), 6)}))
    elif role in ("toolResult", "tool"):
        tool = message.get("toolName") or message.get("name")
        call_id = message.get("toolCallId") or message.get("tool_call_id")
        events.append(("tool.result", {
            "tool": tool if isinstance(tool, str) else None,
            "call_id": call_id if isinstance(call_id, str) else None,
            "is_error": message.get("isError") is True or message.get("is_error") is True,
            "output": _content_text(message.get("content"))}))
    return events


class SessionTailer:
    """Incrementally turn complete JSONL lines under the session dir into feed events."""

    MAX_DEPTH = 2
    MAX_ENTRIES = 512
    MAX_FILES = 64
    READ_BUDGET = 256 * 1024
    MAX_LINE = 1024 * 1024
    MAX_EVENTS = 500

    def __init__(self, session_dir, feed):
        self.session_dir = Path(session_dir)
        self.feed = feed
        self._files: dict[tuple[int, int], list] = {}  # (dev, ino) -> [offset, skipping]
        self._disabled = False

    def poll(self) -> None:
        if self._disabled:
            return
        try:
            self._poll()
        except Exception as exc:  # a hostile session tree must never break job control
            self._disabled = True
            _warn(f"session tailer disabled: {type(exc).__name__}")

    def _poll(self) -> None:
        try:
            root_fd = os.open(self.session_dir, DIR_FLAGS)
        except OSError:
            return
        budget = [self.READ_BUDGET, self.MAX_EVENTS]
        entries = 0
        stack = [(root_fd, 0)]
        try:
            while stack:
                dir_fd, depth = stack.pop()
                try:
                    with os.scandir(dir_fd) as listing:
                        for entry in listing:
                            entries += 1
                            if entries > self.MAX_ENTRIES or budget[0] <= 0 or budget[1] <= 0:
                                return
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if depth < self.MAX_DEPTH:
                                    try:
                                        stack.append(
                                            (os.open(entry.name, DIR_FLAGS, dir_fd=dir_fd),
                                             depth + 1))
                                    except OSError:
                                        continue
                            elif (entry.name.endswith(".jsonl")
                                  and entry.is_file(follow_symlinks=False)):
                                self._read_file(dir_fd, entry.name, budget)
                finally:
                    os.close(dir_fd)
        finally:
            for dir_fd, _depth in stack:
                os.close(dir_fd)

    def _read_file(self, dir_fd: int, name: str, budget: list) -> None:
        try:
            fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
        except OSError:
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return
            key = (info.st_dev, info.st_ino)
            state = self._files.get(key)
            if state is None:
                if len(self._files) >= self.MAX_FILES:
                    return
                state = self._files[key] = [0, False]
            if info.st_size < state[0]:
                state[0], state[1] = 0, False  # rewritten from the start
            while budget[0] > 0 and budget[1] > 0 and state[0] < info.st_size:
                want = min(budget[0], self.MAX_LINE + 1, info.st_size - state[0])
                chunk = os.pread(fd, want, state[0])
                if not chunk:
                    return
                budget[0] -= len(chunk)
                if state[1]:
                    newline = chunk.find(b"\n")
                    if newline < 0:
                        state[0] += len(chunk)
                        continue
                    state[0] += newline + 1
                    state[1] = False
                    continue
                end = chunk.rfind(b"\n")
                if end < 0:
                    if len(chunk) > self.MAX_LINE:
                        state[0] += len(chunk)
                        state[1] = True
                    return  # an incomplete line: wait for the rest
                consumed = 0
                for line in chunk[:end + 1].split(b"\n")[:-1]:
                    consumed += len(line) + 1
                    self._emit_line(line, budget)
                    if budget[1] <= 0:
                        break
                state[0] += consumed
        finally:
            os.close(fd)

    def _emit_line(self, line: bytes, budget: list) -> None:
        if not line.strip() or len(line) > self.MAX_LINE:
            return
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            return
        for event_type, fields in record_events(record):
            if budget[1] <= 0:
                return
            budget[1] -= 1
            self.feed.emit(event_type, **fields)
