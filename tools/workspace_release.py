"""Stage, probe, promote, and roll back immutable workspace release targets.

The release root contains immutable ``targets/<sha256>.json`` receipts, probe
receipts, exact selector backups, copied state backups, atomic ``active`` and
``previous`` selector files, and an append-only JSON-lines ledger. Candidate
manifests use schema version 1 and name three file identities
(``shell_binary``, ``frontend_manifest``, and ``capability_image``), both legacy
engine identities, and state files grouped as ``database``, ``artifacts``, and
``runtime_state``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
JSON_LIMIT = 8 * 1024**2
COPY_BUFFER_SIZE = 1024**2
ARTIFACT_FIELDS = ("shell_binary", "frontend_manifest", "capability_image")
STATE_GROUPS = ("database", "artifacts", "runtime_state")
LEGACY_ENGINES = ("fourmilab", "inspector")
SELECTORS = ("active", "previous")
LAYOUT_MARKER = "layout.json"
_RELEASE_DIRECTORIES = ("targets", "probes", "backups")
_INITIALIZED_ROOT_ENTRIES = frozenset(
    {
        "release.lock",
        "ledger.jsonl",
        LAYOUT_MARKER,
        *SELECTORS,
        *_RELEASE_DIRECTORIES,
    }
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
OPERATION_PATTERN = re.compile(r"promote-[0-9a-f]{64}-[0-9a-f]{32}")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_OPEN_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
_OPEN_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
_OPEN_LOCK_FLAGS = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
_OPEN_LOCK_CREATE_FLAGS = _OPEN_LOCK_FLAGS | os.O_CREAT | os.O_EXCL

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_DIGEST_MISMATCH = 4
EXIT_PROBE_REQUIRED = 5
EXIT_LOCKED = 6
EXIT_CONFIRMATION = 7
EXIT_LEGACY_RESTART = 8
EXIT_NOT_FOUND = 9
EXIT_CONFLICT = 10
EXIT_STORAGE = 11

JsonObject = dict[str, Any]
FaultHook = Callable[[str], None]


class ReleaseError(RuntimeError):
    """Base class for an expected fail-closed release error."""

    exit_code = EXIT_INVALID

    def __init__(self, message: str):
        super().__init__(message)
        self.exit_code = type(self).exit_code


class InvalidReleaseError(ReleaseError):
    """An input or persisted receipt is malformed."""


class DigestMismatchError(ReleaseError):
    """A file does not match its recorded identity."""

    exit_code = EXIT_DIGEST_MISMATCH


class ProbeRequiredError(ReleaseError):
    """No successful probe is bound to the requested target."""

    exit_code = EXIT_PROBE_REQUIRED


class LockContendedError(ReleaseError):
    """Another release mutation owns the single-writer lock."""

    exit_code = EXIT_LOCKED


class ConfirmationError(ReleaseError):
    """Rollback confirmation does not name the exact active target."""

    exit_code = EXIT_CONFIRMATION


class LegacyRestartError(ReleaseError):
    """A probe plan tries to restart a legacy engine."""

    exit_code = EXIT_LEGACY_RESTART


class ReleaseNotFoundError(ReleaseError):
    """A requested immutable receipt does not exist."""

    exit_code = EXIT_NOT_FOUND


class ReleaseConflictError(ReleaseError):
    """Persisted release state cannot be reconciled safely."""

    exit_code = EXIT_CONFLICT


class ReleaseStorageError(ReleaseError):
    """Release state could not be read or durably written."""

    exit_code = EXIT_STORAGE


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InvalidReleaseError("value is not valid JSON") from error


def _pretty_json(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as error:
        raise InvalidReleaseError("value is not valid JSON") from error


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise InvalidReleaseError(f"JSON contains duplicate key: {key}")
        result[key] = value
    return result


def _decode_json(data: bytes, label: str) -> Any:
    if len(data) > JSON_LIMIT:
        raise InvalidReleaseError(f"{label} exceeds the JSON size limit")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except InvalidReleaseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidReleaseError(f"{label} is not valid UTF-8 JSON") from error


def _regular_stat(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseStorageError(f"cannot inspect {label}") from error
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise InvalidReleaseError(f"{label} is not a regular file")
    return info


def _read_regular_bytes(path: Path, label: str, *, limit: int | None = None) -> bytes:
    before = _regular_stat(path, label)
    if limit is not None and before.st_size > limit:
        raise InvalidReleaseError(f"{label} exceeds its size limit")
    try:
        descriptor = os.open(path, _OPEN_READ_FLAGS)
        with os.fdopen(descriptor, "rb") as reader:
            opened = os.fstat(reader.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != before.st_size:
                raise ReleaseConflictError(f"{label} changed before it was read")
            data = reader.read()
            after = os.fstat(reader.fileno())
    except ReleaseError:
        raise
    except OSError as error:
        raise ReleaseStorageError(f"cannot read {label}") from error
    if (
        after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or len(data) != opened.st_size
    ):
        raise ReleaseConflictError(f"{label} changed while it was read")
    return data


def _hash_regular_file(path: Path, label: str) -> tuple[int, str]:
    before = _regular_stat(path, label)
    digest = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(path, _OPEN_READ_FLAGS)
        with os.fdopen(descriptor, "rb") as reader:
            opened = os.fstat(reader.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != before.st_size:
                raise ReleaseConflictError(f"{label} changed before it was hashed")
            while chunk := reader.read(COPY_BUFFER_SIZE):
                digest.update(chunk)
                total += len(chunk)
            after = os.fstat(reader.fileno())
    except ReleaseError:
        raise
    except OSError as error:
        raise ReleaseStorageError(f"cannot hash {label}") from error
    if (
        after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or total != opened.st_size
    ):
        raise ReleaseConflictError(f"{label} changed while it was hashed")
    return total, digest.hexdigest()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_immutable(path: Path, data: bytes) -> bool:
    """Create and fsync ``path`` once; an identical existing file is idempotent."""
    try:
        descriptor = os.open(path, _OPEN_WRITE_FLAGS, 0o600)
    except FileExistsError:
        existing = _read_regular_bytes(path, str(path), limit=max(len(data), JSON_LIMIT))
        if existing != data:
            raise ReleaseConflictError(f"immutable receipt already differs: {path.name}")
        return False
    except OSError as error:
        raise ReleaseStorageError(f"cannot create immutable receipt: {path.name}") from error

    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:  # The failed exclusive write may not have created the path.
            pass
        except OSError as cleanup_error:
            raise ReleaseStorageError(f"cannot remove partial receipt: {path.name}") from cleanup_error
        raise
    else:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return True


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ReleaseStorageError(f"cannot fsync directory: {directory}") from error


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with a same-directory, flushed temporary file."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:  # os.replace already consumed the temporary file.
            pass
        except OSError as cleanup_error:
            raise ReleaseStorageError("cannot remove selector temporary file") from cleanup_error
        raise


def _real_directory(path: Path, label: str, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ReleaseStorageError(f"cannot create {label}") from error
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseStorageError(f"cannot inspect {label}") from error
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise InvalidReleaseError(f"{label} is not a real directory")


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ReleaseStorageError(f"cannot inspect {path.name}") from error
    return True


def _selector_bytes(target_id: str | None) -> bytes:
    if target_id is not None:
        _validate_target_id(target_id)
    return _pretty_json({"target_id": target_id})


def _release_root_entries(root: Path) -> set[str]:
    try:
        return {entry.name for entry in root.iterdir()}
    except OSError as error:
        raise ReleaseStorageError("cannot enumerate release root") from error


def _validate_layout_marker(root: Path) -> None:
    marker = _require_mapping(
        _decode_json(
            _read_regular_bytes(root / LAYOUT_MARKER, "release layout marker", limit=JSON_LIMIT),
            "release layout marker",
        ),
        "release layout marker",
    )
    _require_exact_keys(
        marker,
        {"schema_version", "selectors_initialized"},
        "release layout marker",
    )
    if marker != {"schema_version": SCHEMA_VERSION, "selectors_initialized": True}:
        raise InvalidReleaseError("release layout marker is invalid")


def _validate_initialized_layout(root: Path, entries: set[str] | None = None) -> None:
    entries = _release_root_entries(root) if entries is None else entries
    missing = sorted(_INITIALIZED_ROOT_ENTRIES - entries)
    if missing:
        raise ReleaseConflictError(
            "initialized release is missing layout entries: " + ", ".join(missing)
        )
    unexpected = sorted(entries - _INITIALIZED_ROOT_ENTRIES)
    if unexpected:
        raise ReleaseConflictError(
            "release root contains unexpected entries: " + ", ".join(unexpected)
        )
    for name in _RELEASE_DIRECTORIES:
        _real_directory(root / name, f"release {name} directory")
    _regular_stat(root / "ledger.jsonl", "release ledger")
    _regular_stat(root / "release.lock", "release writer lock")
    _validate_layout_marker(root)
    for name in SELECTORS:
        _read_selector(root, name)


def _ensure_layout(release_root: Path | str) -> Path:
    """Initialize only a lock-only root; otherwise validate without repair."""
    root = _absolute(release_root)
    _real_directory(root, "release root")
    entries = _release_root_entries(root)
    if entries == {"release.lock"}:
        for name in _RELEASE_DIRECTORIES:
            _real_directory(root / name, f"release {name} directory", create=True)
        _write_immutable(root / "ledger.jsonl", b"")
        for name in SELECTORS:
            _write_immutable(root / name, _selector_bytes(None))
        _write_json_immutable(
            root / LAYOUT_MARKER,
            {"schema_version": SCHEMA_VERSION, "selectors_initialized": True},
        )
        entries = _release_root_entries(root)
    _validate_initialized_layout(root, entries)
    return root


@contextmanager
def release_lock(release_root: Path | str) -> Iterator[None]:
    """Acquire the release root's non-blocking, process-scoped writer lock."""
    root = _absolute(release_root)
    _real_directory(root, "release root", create=True)
    lock_path = root / "release.lock"
    descriptor: int | None = None
    if not _exists(lock_path):
        entries = _release_root_entries(root)
        if entries - {"release.lock"}:
            raise ReleaseConflictError("populated release root is missing its writer lock")
        try:
            descriptor = os.open(lock_path, _OPEN_LOCK_CREATE_FLAGS, 0o600)
        except FileExistsError:  # A concurrent first caller created the shared lock.
            descriptor = None
        except OSError as error:
            raise ReleaseStorageError("cannot create release writer lock") from error
    if descriptor is None:
        try:
            descriptor = os.open(lock_path, _OPEN_LOCK_FLAGS)
        except FileNotFoundError as error:
            raise ReleaseConflictError("release writer lock disappeared during acquisition") from error
        except OSError as error:
            raise ReleaseStorageError("cannot open release writer lock") from error
    try:
        handle = os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise
    try:
        _regular_stat(lock_path, "release writer lock")
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise InvalidReleaseError("release writer lock is not a regular file")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            if handle.write(b"\0") != 1:
                raise ReleaseStorageError("cannot initialize release writer lock")
            handle.flush()
            os.fsync(handle.fileno())
            _fsync_directory(root)
    except BaseException:
        handle.close()
        raise

    locked = False
    try:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise LockContendedError("another release mutation owns the writer lock") from error
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _load_json_source(source: Path | str | Mapping[str, Any], label: str) -> tuple[Any, Path]:
    if isinstance(source, Mapping):
        value = _decode_json(_canonical_json(dict(source)), label)
        return value, Path.cwd()
    path = _absolute(source)
    data = _read_regular_bytes(path, label, limit=JSON_LIMIT)
    return _decode_json(data, label), path.parent


def _require_mapping(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise InvalidReleaseError(f"{label} must be a JSON object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise InvalidReleaseError(f"{label} keys differ (missing: {missing}; extra: {extra})")


def _validate_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise InvalidReleaseError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_target_id(target_id: Any) -> str:
    if not isinstance(target_id, str) or not SHA256_PATTERN.fullmatch(target_id):
        raise InvalidReleaseError("target id must be a lowercase SHA-256 identity")
    return target_id


def _candidate_path(raw: Any, base: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise InvalidReleaseError(f"{label} path must be a non-empty string")
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return _absolute(path)


def _checked_candidate_identity(raw: Any, base: Path, label: str) -> JsonObject:
    identity = _require_mapping(raw, label)
    allowed = {"path", "sha256", "size"}
    if not {"path", "sha256"}.issubset(identity) or set(identity) - allowed:
        raise InvalidReleaseError(f"{label} must contain path and sha256, with optional size")
    path = _candidate_path(identity["path"], base, label)
    expected_digest = _validate_digest(identity["sha256"], f"{label} sha256")
    try:
        size, digest = _hash_regular_file(path, label)
    except ReleaseError as error:
        raise DigestMismatchError(f"{label} file cannot be verified") from error
    expected_size = identity.get("size")
    if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
        raise InvalidReleaseError(f"{label} size must be a non-negative integer")
    if expected_size is not None and size != expected_size:
        raise DigestMismatchError(f"{label} size does not match the file")
    if digest != expected_digest:
        raise DigestMismatchError(f"{label} SHA-256 does not match the file")
    return {"path": str(path), "size": size, "sha256": digest}


def _candidate_payload(candidate: Any, base: Path) -> JsonObject:
    manifest = _require_mapping(candidate, "candidate manifest")
    expected = {
        "schema_version",
        *ARTIFACT_FIELDS,
        "legacy_engines",
        "state_snapshot",
    }
    _require_exact_keys(manifest, expected, "candidate manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise InvalidReleaseError("candidate manifest schema_version must be 1")

    payload: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "kind": "workspace_release_target",
        "candidate_manifest_sha256": _json_digest(manifest),
    }
    for field in ARTIFACT_FIELDS:
        payload[field] = _checked_candidate_identity(manifest[field], base, field)

    legacy = _require_mapping(manifest["legacy_engines"], "legacy_engines")
    _require_exact_keys(legacy, set(LEGACY_ENGINES), "legacy_engines")
    normalized_legacy: JsonObject = {}
    for engine in LEGACY_ENGINES:
        identity = legacy[engine]
        if isinstance(identity, str):
            if not identity.strip():
                raise InvalidReleaseError(f"legacy engine {engine} identity is empty")
        elif not isinstance(identity, dict) or not identity:
            raise InvalidReleaseError(f"legacy engine {engine} identity must be a string or object")
        normalized_legacy[engine] = _decode_json(_canonical_json(identity), f"{engine} identity")
    payload["legacy_engines"] = normalized_legacy

    snapshot = _require_mapping(manifest["state_snapshot"], "state_snapshot")
    _require_exact_keys(snapshot, set(STATE_GROUPS), "state_snapshot")
    normalized_snapshot: JsonObject = {}
    seen_paths: set[str] = set()
    for group in STATE_GROUPS:
        entries = snapshot[group]
        if not isinstance(entries, list):
            raise InvalidReleaseError(f"state_snapshot.{group} must be an array")
        normalized_entries = []
        for index, entry in enumerate(entries):
            normalized = _checked_candidate_identity(
                entry,
                base,
                f"state_snapshot.{group}[{index}]",
            )
            if normalized["path"] in seen_paths:
                raise InvalidReleaseError("state snapshot names the same file more than once")
            seen_paths.add(normalized["path"])
            normalized_entries.append(normalized)
        normalized_snapshot[group] = normalized_entries
    payload["state_snapshot"] = normalized_snapshot
    return payload


def _write_json_immutable(path: Path, value: Any) -> bool:
    return _write_immutable(path, _pretty_json(value))


def _validate_recorded_identity(raw: Any, label: str) -> JsonObject:
    identity = _require_mapping(raw, label)
    _require_exact_keys(identity, {"path", "size", "sha256"}, label)
    path = identity["path"]
    size = identity["size"]
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise InvalidReleaseError(f"{label} path must be absolute")
    if type(size) is not int or size < 0:
        raise InvalidReleaseError(f"{label} size must be a non-negative integer")
    digest = _validate_digest(identity["sha256"], f"{label} sha256")
    return {"path": path, "size": size, "sha256": digest}


def _validate_target_receipt(receipt: Any, expected_id: str) -> JsonObject:
    target = _require_mapping(receipt, "target receipt")
    expected_keys = {
        "target_id",
        "schema_version",
        "kind",
        "candidate_manifest_sha256",
        *ARTIFACT_FIELDS,
        "legacy_engines",
        "state_snapshot",
    }
    _require_exact_keys(target, expected_keys, "target receipt")
    if target["target_id"] != expected_id:
        raise ReleaseConflictError("target receipt filename and target_id differ")
    if target["schema_version"] != SCHEMA_VERSION or target["kind"] != "workspace_release_target":
        raise InvalidReleaseError("target receipt schema or kind is invalid")
    _validate_digest(target["candidate_manifest_sha256"], "candidate manifest identity")
    for field in ARTIFACT_FIELDS:
        _validate_recorded_identity(target[field], field)
    legacy = _require_mapping(target["legacy_engines"], "legacy_engines")
    _require_exact_keys(legacy, set(LEGACY_ENGINES), "legacy_engines")
    snapshot = _require_mapping(target["state_snapshot"], "state_snapshot")
    _require_exact_keys(snapshot, set(STATE_GROUPS), "state_snapshot")
    for group in STATE_GROUPS:
        if not isinstance(snapshot[group], list):
            raise InvalidReleaseError(f"target state_snapshot.{group} must be an array")
        for index, entry in enumerate(snapshot[group]):
            _validate_recorded_identity(entry, f"state_snapshot.{group}[{index}]")

    payload = dict(target)
    del payload["target_id"]
    if _json_digest(payload) != expected_id:
        raise ReleaseConflictError("target receipt no longer matches its SHA-256 identity")
    return target


def _load_target(root: Path, target_id: str) -> JsonObject:
    target_id = _validate_target_id(target_id)
    path = root / "targets" / f"{target_id}.json"
    if not _exists(path):
        raise ReleaseNotFoundError(f"release target not found: {target_id}")
    receipt = _decode_json(_read_regular_bytes(path, "target receipt", limit=JSON_LIMIT), "target receipt")
    return _validate_target_receipt(receipt, target_id)


def _verify_target_artifacts(target: Mapping[str, Any]) -> None:
    for field in ARTIFACT_FIELDS:
        identity = _validate_recorded_identity(target[field], field)
        try:
            size, digest = _hash_regular_file(Path(identity["path"]), field)
        except ReleaseError as error:
            raise DigestMismatchError(f"{field} file cannot be verified") from error
        if size != identity["size"] or digest != identity["sha256"]:
            raise DigestMismatchError(f"{field} no longer matches its staged identity")


def _read_ledger(root: Path) -> list[JsonObject]:
    data = _read_regular_bytes(root / "ledger.jsonl", "release ledger")
    if data and not data.endswith(b"\n"):
        raise ReleaseConflictError("release ledger ends with an incomplete record")
    records = []
    for expected_sequence, line in enumerate(data.splitlines(), 1):
        record = _require_mapping(_decode_json(line, "release ledger record"), "release ledger record")
        if record.get("schema_version") != SCHEMA_VERSION:
            raise InvalidReleaseError("release ledger record has an invalid schema version")
        if record.get("sequence") != expected_sequence:
            raise ReleaseConflictError("release ledger sequence is not contiguous")
        if not isinstance(record.get("event"), str) or not isinstance(record.get("at"), str):
            raise InvalidReleaseError("release ledger record is missing event metadata")
        records.append(record)
    return records


def _append_ledger(root: Path, event: str, **fields: Any) -> JsonObject:
    records = _read_ledger(root)
    record = {
        "schema_version": SCHEMA_VERSION,
        "sequence": len(records) + 1,
        "event": event,
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **fields,
    }
    data = _canonical_json(record) + b"\n"
    try:
        descriptor = os.open(
            root / "ledger.jsonl",
            os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0),
        )
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ReleaseStorageError("cannot append the release ledger") from error
    return record


def _ledger_has(records: Sequence[Mapping[str, Any]], event: str, key: str, value: Any) -> bool:
    return any(record.get("event") == event and record.get(key) == value for record in records)


def stage(
    release_root: Path | str,
    candidate: Path | str | Mapping[str, Any],
    *,
    candidate_base: Path | str | None = None,
) -> JsonObject:
    """Validate a candidate manifest and store its immutable target receipt."""
    manifest, source_base = _load_json_source(candidate, "candidate manifest")
    base = _absolute(candidate_base) if candidate_base is not None else source_base
    payload = _candidate_payload(manifest, base)
    target_id = _json_digest(payload)
    receipt = {"target_id": target_id, **payload}

    with release_lock(release_root):
        root = _ensure_layout(release_root)
        created = _write_json_immutable(root / "targets" / f"{target_id}.json", receipt)
        _load_target(root, target_id)
        records = _read_ledger(root)
        if created or not _ledger_has(records, "target_staged", "target_id", target_id):
            _append_ledger(root, "target_staged", target_id=target_id)
    return receipt


def _probe_result(plan: Mapping[str, Any]) -> str:
    if "result" in plan:
        raw = plan["result"]
    elif "status" in plan:
        raw = plan["status"]
    elif "successful" in plan and type(plan["successful"]) is bool:
        raw = "succeeded" if plan["successful"] else "failed"
    else:
        raise InvalidReleaseError("probe plan must record a result")
    if raw in ("success", "succeeded"):
        return "succeeded"
    if raw in ("failure", "failed"):
        return "failed"
    raise InvalidReleaseError("probe result must be succeeded or failed")


def _check_legacy_restart_steps(steps: Any) -> list[Any]:
    if not isinstance(steps, list):
        raise InvalidReleaseError("probe plan steps must be an array")
    for index, step in enumerate(steps):
        text = _canonical_json(step).decode("utf-8").lower()
        if "restart" not in text:
            continue
        for engine in LEGACY_ENGINES:
            if engine in text:
                raise LegacyRestartError(
                    f"probe plan step {index} names a forbidden {engine} restart"
                )
    return steps


def _validate_probe_receipt(receipt: Any, expected_target: str, expected_probe: str) -> JsonObject:
    probe = _require_mapping(receipt, "probe receipt")
    _require_exact_keys(
        probe,
        {
            "probe_id",
            "schema_version",
            "kind",
            "target_id",
            "target_receipt_sha256",
            "result",
            "plan_sha256",
            "plan",
        },
        "probe receipt",
    )
    if probe["probe_id"] != expected_probe or probe["target_id"] != expected_target:
        raise ReleaseConflictError("probe receipt identity does not match its path")
    if probe["target_receipt_sha256"] != expected_target:
        raise ReleaseConflictError("probe receipt is not bound to the exact target")
    if probe["schema_version"] != SCHEMA_VERSION or probe["kind"] != "workspace_probe":
        raise InvalidReleaseError("probe receipt schema or kind is invalid")
    if probe["result"] not in ("succeeded", "failed"):
        raise InvalidReleaseError("probe receipt result is invalid")
    _validate_digest(probe["plan_sha256"], "probe plan identity")
    if _json_digest(probe["plan"]) != probe["plan_sha256"]:
        raise ReleaseConflictError("probe plan no longer matches its identity")
    payload = dict(probe)
    del payload["probe_id"]
    if _json_digest(payload) != expected_probe:
        raise ReleaseConflictError("probe receipt no longer matches its SHA-256 identity")
    _check_legacy_restart_steps(_require_mapping(probe["plan"], "probe plan").get("steps"))
    return probe


def probe_plan(
    release_root: Path | str,
    target_id: str,
    plan: Path | str | Mapping[str, Any],
) -> JsonObject:
    """Validate and record one probe result bound to exactly ``target_id``."""
    target_id = _validate_target_id(target_id)
    raw_plan, _ = _load_json_source(plan, "probe plan")
    normalized_plan = _require_mapping(raw_plan, "probe plan")
    named_target = normalized_plan.get("target_id")
    if named_target is not None and named_target != target_id:
        raise InvalidReleaseError("probe plan names a different target id")
    _check_legacy_restart_steps(normalized_plan.get("steps"))
    result = _probe_result(normalized_plan)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "workspace_probe",
        "target_id": target_id,
        "target_receipt_sha256": target_id,
        "result": result,
        "plan_sha256": _json_digest(normalized_plan),
        "plan": normalized_plan,
    }
    probe_id = _json_digest(payload)
    receipt = {"probe_id": probe_id, **payload}

    with release_lock(release_root):
        root = _ensure_layout(release_root)
        _load_target(root, target_id)
        directory = root / "probes" / target_id
        _real_directory(directory, "target probe directory", create=True)
        created = _write_json_immutable(directory / f"{probe_id}.json", receipt)
        records = _read_ledger(root)
        if created or not _ledger_has(records, "probe_recorded", "probe_id", probe_id):
            _append_ledger(
                root,
                "probe_recorded",
                target_id=target_id,
                probe_id=probe_id,
                result=result,
            )
    return receipt


def _probe_receipts(root: Path, target_id: str) -> list[JsonObject]:
    directory = root / "probes" / target_id
    if not _exists(directory):
        return []
    _real_directory(directory, "target probe directory")
    receipts = []
    try:
        paths = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise ReleaseStorageError("cannot enumerate target probe receipts") from error
    for path in paths:
        if path.suffix != ".json" or not SHA256_PATTERN.fullmatch(path.stem):
            raise InvalidReleaseError("target probe directory contains an unexpected entry")
        receipt = _decode_json(
            _read_regular_bytes(path, "probe receipt", limit=JSON_LIMIT),
            "probe receipt",
        )
        receipts.append(_validate_probe_receipt(receipt, target_id, path.stem))
    return receipts


def _require_successful_probe(root: Path, target_id: str) -> JsonObject:
    successful = [receipt for receipt in _probe_receipts(root, target_id) if receipt["result"] == "succeeded"]
    if not successful:
        raise ProbeRequiredError(f"successful probe receipt required for target {target_id}")
    return successful[-1]


def _parse_selector(data: bytes, root: Path, label: str) -> str | None:
    selector = _require_mapping(_decode_json(data, label), label)
    _require_exact_keys(selector, {"target_id"}, label)
    target_id = selector["target_id"]
    if target_id is None:
        return None
    target_id = _validate_target_id(target_id)
    _load_target(root, target_id)
    return target_id


def _read_selector(root: Path, name: str) -> tuple[bytes, str | None]:
    if name not in SELECTORS:
        raise InvalidReleaseError("unknown release selector")
    data = _read_regular_bytes(root / name, f"{name} selector", limit=JSON_LIMIT)
    return data, _parse_selector(data, root, f"{name} selector")


def _write_selector(root: Path, name: str, data: bytes) -> None:
    _parse_selector(data, root, f"{name} selector")
    _atomic_write(root / name, data)


def _copy_state_file(source: Path, destination: Path, label: str) -> JsonObject:
    before = _regular_stat(source, label)
    try:
        source_descriptor = os.open(source, _OPEN_READ_FLAGS)
    except OSError as error:
        raise ReleaseStorageError(f"cannot open {label} for backup") from error
    try:
        destination_descriptor = os.open(destination, _OPEN_WRITE_FLAGS, 0o600)
    except OSError as error:
        os.close(source_descriptor)
        raise ReleaseStorageError(f"cannot create {label} backup") from error

    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(source_descriptor, "rb") as reader, os.fdopen(
            destination_descriptor, "wb"
        ) as writer:
            opened = os.fstat(reader.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != before.st_size:
                raise ReleaseConflictError(f"{label} changed before backup")
            while chunk := reader.read(COPY_BUFFER_SIZE):
                writer.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or total != opened.st_size
        ):
            raise ReleaseConflictError(f"{label} changed during backup")
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:  # Opening the exclusive backup may have failed before creation.
            pass
        except OSError as cleanup_error:
            raise ReleaseStorageError("cannot remove partial state backup") from cleanup_error
        raise
    return {"size": total, "sha256": digest.hexdigest()}


def _create_backup(
    root: Path,
    target: Mapping[str, Any],
    active_bytes: bytes,
    previous_bytes: bytes,
) -> JsonObject:
    target_id = target["target_id"]
    operation_id = f"promote-{target_id}-{uuid.uuid4().hex}"
    directory = root / "backups" / operation_id
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise ReleaseStorageError("cannot create promotion backup") from error

    try:
        selector_manifest: JsonObject = {}
        for name, data in (("active", active_bytes), ("previous", previous_bytes)):
            backup_name = f"{name}.selector"
            _write_immutable(directory / backup_name, data)
            selector_manifest[name] = {
                "backup_path": backup_name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        state_manifest: JsonObject = {}
        snapshot = _require_mapping(target["state_snapshot"], "state_snapshot")
        for group in STATE_GROUPS:
            group_directory = directory / "state" / group
            _real_directory(group_directory, f"backup {group} directory", create=True)
            entries = []
            for index, recorded in enumerate(snapshot[group]):
                source_identity = _validate_recorded_identity(
                    recorded,
                    f"state_snapshot.{group}[{index}]",
                )
                backup_path = Path("state") / group / f"{index:08d}.bin"
                copied = _copy_state_file(
                    Path(source_identity["path"]),
                    directory / backup_path,
                    f"state_snapshot.{group}[{index}]",
                )
                entries.append(
                    {
                        "path": source_identity["path"],
                        "backup_path": backup_path.as_posix(),
                        **copied,
                    }
                )
            state_manifest[group] = entries
            _fsync_directory(group_directory)
        _fsync_directory(directory / "state")

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "workspace_promotion_backup",
            "operation_id": operation_id,
            "target_id": target_id,
            "selectors": selector_manifest,
            "state_snapshot": state_manifest,
        }
        manifest_data = _pretty_json(manifest)
        _write_immutable(directory / "manifest.json", manifest_data)
        return {
            "operation_id": operation_id,
            "backup_manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
        }
    except BaseException:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:  # A failed backup setup may already have removed the directory.
            pass
        except OSError as cleanup_error:
            raise ReleaseStorageError("cannot remove incomplete promotion backup") from cleanup_error
        raise


def _verify_backup(root: Path, prepared: Mapping[str, Any]) -> JsonObject:
    operation_id = prepared.get("operation_id")
    target_id = prepared.get("target_id")
    if not isinstance(operation_id, str) or not OPERATION_PATTERN.fullmatch(operation_id):
        raise InvalidReleaseError("promotion operation id is invalid")
    target_id = _validate_target_id(target_id)
    expected_manifest_digest = _validate_digest(
        prepared.get("backup_manifest_sha256"),
        "backup manifest identity",
    )
    directory = root / "backups" / operation_id
    _real_directory(directory, "promotion backup directory")
    manifest_data = _read_regular_bytes(
        directory / "manifest.json",
        "promotion backup manifest",
        limit=JSON_LIMIT,
    )
    if hashlib.sha256(manifest_data).hexdigest() != expected_manifest_digest:
        raise ReleaseConflictError("promotion backup manifest digest differs")
    manifest = _require_mapping(
        _decode_json(manifest_data, "promotion backup manifest"),
        "promotion backup manifest",
    )
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "kind",
            "operation_id",
            "target_id",
            "selectors",
            "state_snapshot",
        },
        "promotion backup manifest",
    )
    if (
        manifest["schema_version"] != SCHEMA_VERSION
        or manifest["kind"] != "workspace_promotion_backup"
        or manifest["operation_id"] != operation_id
        or manifest["target_id"] != target_id
    ):
        raise ReleaseConflictError("promotion backup manifest identity differs")

    selector_bytes: JsonObject = {}
    selectors = _require_mapping(manifest["selectors"], "backup selectors")
    _require_exact_keys(selectors, set(SELECTORS), "backup selectors")
    for name in SELECTORS:
        entry = _require_mapping(selectors[name], f"backup {name} selector")
        _require_exact_keys(entry, {"backup_path", "size", "sha256"}, f"backup {name} selector")
        expected_name = f"{name}.selector"
        if entry["backup_path"] != expected_name:
            raise InvalidReleaseError(f"backup {name} selector path is invalid")
        data = _read_regular_bytes(directory / expected_name, f"backup {name} selector", limit=JSON_LIMIT)
        if (
            type(entry["size"]) is not int
            or entry["size"] != len(data)
            or _validate_digest(entry["sha256"], f"backup {name} selector digest")
            != hashlib.sha256(data).hexdigest()
        ):
            raise ReleaseConflictError(f"backup {name} selector identity differs")
        _parse_selector(data, root, f"backup {name} selector")
        selector_bytes[name] = data

    state = _require_mapping(manifest["state_snapshot"], "backup state_snapshot")
    _require_exact_keys(state, set(STATE_GROUPS), "backup state_snapshot")
    for group in STATE_GROUPS:
        entries = state[group]
        if not isinstance(entries, list):
            raise InvalidReleaseError(f"backup state_snapshot.{group} must be an array")
        for index, entry_value in enumerate(entries):
            entry = _require_mapping(entry_value, f"backup {group}[{index}]")
            _require_exact_keys(
                entry,
                {"path", "backup_path", "size", "sha256"},
                f"backup {group}[{index}]",
            )
            expected_path = (Path("state") / group / f"{index:08d}.bin").as_posix()
            if entry["backup_path"] != expected_path or not isinstance(entry["path"], str):
                raise InvalidReleaseError(f"backup {group}[{index}] path is invalid")
            data_path = directory / Path(expected_path)
            size, digest = _hash_regular_file(data_path, f"backup {group}[{index}]")
            if (
                type(entry["size"]) is not int
                or size != entry["size"]
                or _validate_digest(entry["sha256"], f"backup {group}[{index}] digest") != digest
            ):
                raise ReleaseConflictError(f"backup {group}[{index}] identity differs")
    return {"manifest": manifest, "selectors": selector_bytes}


def _operation_records(records: Sequence[JsonObject]) -> JsonObject:
    prepared: dict[str, JsonObject] = {}
    completed: dict[str, JsonObject] = {}
    rollback_prepared: dict[str, JsonObject] = {}
    rolled_back: dict[str, JsonObject] = {}
    for record in records:
        operation_id = record.get("operation_id")
        if record["event"] == "promote_prepared":
            if operation_id in prepared:
                raise ReleaseConflictError("promotion operation was prepared more than once")
            prepared[operation_id] = record
        elif record["event"] == "promote_completed":
            if operation_id not in prepared or operation_id in completed:
                raise ReleaseConflictError("promotion completion has no unique preparation")
            completed[operation_id] = record
        elif record["event"] == "rollback_prepared":
            if operation_id not in completed or operation_id in rollback_prepared:
                raise ReleaseConflictError("rollback preparation has no active promotion")
            rollback_prepared[operation_id] = record
        elif record["event"] == "rollback_completed":
            if operation_id not in rollback_prepared or operation_id in rolled_back:
                raise ReleaseConflictError("rollback completion has no unique preparation")
            rolled_back[operation_id] = record
    return {
        "prepared": prepared,
        "completed": completed,
        "rollback_prepared": rollback_prepared,
        "rolled_back": rolled_back,
    }


def _promotion_result(record: Mapping[str, Any], *, idempotent: bool) -> JsonObject:
    return {
        "target_id": record["target_id"],
        "operation_id": record["operation_id"],
        "status": "active",
        "idempotent": idempotent,
    }


def _rollback_result(record: Mapping[str, Any], *, idempotent: bool) -> JsonObject:
    return {
        "target_id": record["target_id"],
        "operation_id": record["operation_id"],
        "status": "rolled_back",
        "idempotent": idempotent,
    }


def _resume_promotion(
    root: Path,
    prepared: JsonObject,
    *,
    fault_hook: FaultHook | None,
) -> JsonObject:
    backup = _verify_backup(root, prepared)
    target_id = prepared["target_id"]
    active_bytes, active_id = _read_selector(root, "active")
    previous_bytes, _ = _read_selector(root, "previous")
    backed_active = backup["selectors"]["active"]
    backed_previous = backup["selectors"]["previous"]
    pre_active_id = _parse_selector(backed_active, root, "backup active selector")

    if active_id == target_id:
        if previous_bytes != backed_active:
            raise ReleaseConflictError("completed promotion has an unexpected previous selector")
    elif active_bytes == backed_active:
        if previous_bytes not in (backed_previous, backed_active):
            raise ReleaseConflictError("pending promotion selectors cannot be reconciled")
        _write_selector(root, "previous", backed_active)
        if fault_hook is not None:
            fault_hook("after_previous")
        _write_selector(root, "active", _selector_bytes(target_id))
    else:
        raise ReleaseConflictError("pending promotion active selector cannot be reconciled")

    completed = _append_ledger(
        root,
        "promote_completed",
        operation_id=prepared["operation_id"],
        target_id=target_id,
        previous_target=pre_active_id,
    )
    return _promotion_result(completed, idempotent=False)


def promote(
    release_root: Path | str,
    target_id: str,
    *,
    fault_hook: FaultHook | None = None,
) -> JsonObject:
    """Back up live selectors/state and atomically select a successfully probed target."""
    target_id = _validate_target_id(target_id)
    with release_lock(release_root):
        root = _ensure_layout(release_root)
        target = _load_target(root, target_id)
        _verify_target_artifacts(target)
        _require_successful_probe(root, target_id)
        records = _read_ledger(root)
        operations = _operation_records(records)
        pending_promotions = [
            record
            for operation_id, record in operations["prepared"].items()
            if operation_id not in operations["completed"]
        ]
        pending_rollbacks = [
            record
            for operation_id, record in operations["rollback_prepared"].items()
            if operation_id not in operations["rolled_back"]
        ]
        if pending_rollbacks:
            raise ReleaseConflictError("an unfinished rollback must be reconciled first")
        if pending_promotions:
            if len(pending_promotions) != 1 or pending_promotions[0]["target_id"] != target_id:
                raise ReleaseConflictError("an unfinished promotion must be reconciled first")
            return _resume_promotion(root, pending_promotions[0], fault_hook=fault_hook)

        active_bytes, active_id = _read_selector(root, "active")
        previous_bytes, _ = _read_selector(root, "previous")
        if active_id == target_id:
            for record in reversed(records):
                if (
                    record["event"] == "promote_completed"
                    and record.get("target_id") == target_id
                    and record.get("operation_id") not in operations["rolled_back"]
                ):
                    _verify_backup(root, operations["prepared"][record["operation_id"]])
                    return _promotion_result(record, idempotent=True)
            raise ReleaseConflictError("active selector has no completed promotion receipt")

        backup = _create_backup(root, target, active_bytes, previous_bytes)
        prepared = _append_ledger(
            root,
            "promote_prepared",
            target_id=target_id,
            operation_id=backup["operation_id"],
            backup_manifest_sha256=backup["backup_manifest_sha256"],
            previous_target=active_id,
        )
        return _resume_promotion(root, prepared, fault_hook=fault_hook)


def _resume_rollback(
    root: Path,
    promotion: JsonObject,
    *,
    fault_hook: FaultHook | None,
) -> JsonObject:
    backup = _verify_backup(root, promotion)
    target_id = promotion["target_id"]
    active_bytes, active_id = _read_selector(root, "active")
    previous_bytes, _ = _read_selector(root, "previous")
    backed_active = backup["selectors"]["active"]
    backed_previous = backup["selectors"]["previous"]

    if active_id == target_id:
        if previous_bytes not in (backed_active, backed_previous):
            raise ReleaseConflictError("pending rollback selectors cannot be reconciled")
        _write_selector(root, "previous", backed_previous)
        if fault_hook is not None:
            fault_hook("after_previous")
        _write_selector(root, "active", backed_active)
    elif active_bytes == backed_active and previous_bytes == backed_previous:
        pass
    else:
        raise ReleaseConflictError("pending rollback active selector cannot be reconciled")

    completed = _append_ledger(
        root,
        "rollback_completed",
        operation_id=promotion["operation_id"],
        target_id=target_id,
    )
    return _rollback_result(completed, idempotent=False)


def rollback(
    release_root: Path | str,
    target_id: str,
    *,
    confirm_target: str,
    fault_hook: FaultHook | None = None,
) -> JsonObject:
    """Restore exact pre-promotion selectors without rewriting any live state file."""
    target_id = _validate_target_id(target_id)
    if confirm_target != target_id:
        raise ConfirmationError("rollback requires confirmation of the exact target id")

    with release_lock(release_root):
        root = _ensure_layout(release_root)
        _load_target(root, target_id)
        records = _read_ledger(root)
        operations = _operation_records(records)
        pending_promotions = [
            operation_id
            for operation_id in operations["prepared"]
            if operation_id not in operations["completed"]
        ]
        if pending_promotions:
            raise ReleaseConflictError("an unfinished promotion must be reconciled first")
        pending_rollbacks = [
            operation_id
            for operation_id in operations["rollback_prepared"]
            if operation_id not in operations["rolled_back"]
        ]
        if pending_rollbacks:
            if len(pending_rollbacks) != 1:
                raise ReleaseConflictError("multiple unfinished rollbacks exist")
            operation_id = pending_rollbacks[0]
            promotion = operations["prepared"][operation_id]
            if promotion["target_id"] != target_id:
                raise ReleaseConflictError("another target has an unfinished rollback")
            return _resume_rollback(root, promotion, fault_hook=fault_hook)

        active_bytes, active_id = _read_selector(root, "active")
        previous_bytes, _ = _read_selector(root, "previous")
        if active_id != target_id:
            for record in reversed(records):
                if record["event"] != "rollback_completed" or record.get("target_id") != target_id:
                    continue
                promotion = operations["prepared"][record["operation_id"]]
                backup = _verify_backup(root, promotion)
                if (
                    active_bytes == backup["selectors"]["active"]
                    and previous_bytes == backup["selectors"]["previous"]
                ):
                    return _rollback_result(record, idempotent=True)
                break
            raise ConfirmationError("rollback target is not the exact active target")

        promotion = None
        for record in reversed(records):
            if (
                record["event"] == "promote_completed"
                and record.get("target_id") == target_id
                and record.get("operation_id") not in operations["rolled_back"]
            ):
                promotion = operations["prepared"][record["operation_id"]]
                break
        if promotion is None:
            raise ReleaseConflictError("active target has no reversible promotion receipt")
        _append_ledger(
            root,
            "rollback_prepared",
            operation_id=promotion["operation_id"],
            target_id=target_id,
        )
        return _resume_rollback(root, promotion, fault_hook=fault_hook)


def _empty_status() -> JsonObject:
    return {
        "schema_version": SCHEMA_VERSION,
        "active": None,
        "previous": None,
        "targets": [],
        "successful_probes": [],
        "pending": [],
        "ledger_records": 0,
    }


def _read_status_layout(release_root: Path | str) -> Path | None:
    root = _absolute(release_root)
    if not _exists(root):
        return None
    _real_directory(root, "release root")
    entries = _release_root_entries(root)
    if not entries:
        return None
    if entries == {"release.lock"}:
        _regular_stat(root / "release.lock", "release writer lock")
        return None
    _validate_initialized_layout(root, entries)
    return root


def status(release_root: Path | str) -> JsonObject:
    """Return verified state without creating or repairing release files."""
    root = _read_status_layout(release_root)
    if root is None:
        return _empty_status()
    _, active = _read_selector(root, "active")
    _, previous = _read_selector(root, "previous")
    try:
        target_paths = sorted((root / "targets").iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise ReleaseStorageError("cannot enumerate release targets") from error
    targets = []
    successful_probes = []
    for path in target_paths:
        if path.suffix != ".json" or not SHA256_PATTERN.fullmatch(path.stem):
            raise InvalidReleaseError("targets directory contains an unexpected entry")
        _load_target(root, path.stem)
        targets.append(path.stem)
        if any(receipt["result"] == "succeeded" for receipt in _probe_receipts(root, path.stem)):
            successful_probes.append(path.stem)
    records = _read_ledger(root)
    operations = _operation_records(records)
    pending = [
        {"operation": "promote", "operation_id": operation_id, "target_id": record["target_id"]}
        for operation_id, record in operations["prepared"].items()
        if operation_id not in operations["completed"]
    ]
    pending.extend(
        {
            "operation": "rollback",
            "operation_id": operation_id,
            "target_id": record["target_id"],
        }
        for operation_id, record in operations["rollback_prepared"].items()
        if operation_id not in operations["rolled_back"]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "active": active,
        "previous": previous,
        "targets": targets,
        "successful_probes": successful_probes,
        "pending": pending,
        "ledger_records": len(records),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage_parser = subparsers.add_parser("stage", help="validate and store a candidate target")
    stage_parser.add_argument("release_root", type=Path)
    stage_parser.add_argument("candidate", type=Path)

    probe_parser = subparsers.add_parser(
        "probe-plan",
        help="validate and record a target-bound probe plan receipt",
    )
    probe_parser.add_argument("release_root", type=Path)
    probe_parser.add_argument("target_id")
    probe_parser.add_argument("plan", type=Path)

    promote_parser = subparsers.add_parser("promote", help="back up and select a target")
    promote_parser.add_argument("release_root", type=Path)
    promote_parser.add_argument("target_id")

    rollback_parser = subparsers.add_parser("rollback", help="restore exact pre-promotion selectors")
    rollback_parser.add_argument("release_root", type=Path)
    rollback_parser.add_argument("target_id")
    rollback_parser.add_argument("--confirm-target", required=True)

    status_parser = subparsers.add_parser("status", help="show verified release state")
    status_parser.add_argument("release_root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the workspace release CLI and return a process exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            result = stage(args.release_root, args.candidate)
        elif args.command == "probe-plan":
            result = probe_plan(args.release_root, args.target_id, args.plan)
        elif args.command == "promote":
            result = promote(args.release_root, args.target_id)
        elif args.command == "rollback":
            result = rollback(
                args.release_root,
                args.target_id,
                confirm_target=args.confirm_target,
            )
        else:
            result = status(args.release_root)
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
    except OSError as error:
        print(f"error: release storage operation failed ({type(error).__name__})", file=sys.stderr)
        return EXIT_STORAGE
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
