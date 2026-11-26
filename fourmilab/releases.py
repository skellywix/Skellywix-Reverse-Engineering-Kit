"""Stage and recheck the pinned inputs for the Fourmilab Base64 compatibility candidate.

The snapshot is data only: this module downloads, hashes and copies files. It never
executes, imports, unpacks or promotes anything it fetched. Running the fixed inspector in
an unprivileged container and promoting a snapshot are the caller's separate jobs.

A snapshot is one flat directory holding exactly the files in ``FILES``. The receipt
returned by ``fetch_snapshot`` is kept outside that directory; ``check_snapshot`` must be
called with it before every use of the directory.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import dependency_candidates as dep

KIT_REPOSITORY = "skellywix/Skellywix-Reverse-Engineering-Kit"
KIT_BRANCH = "main"
INSPECTOR_PATH = "static-inspection/inspect_file.py"
INSPECTOR_LIMIT = dep.METADATA_LIMIT

INSPECTOR = "inspect_file.py"
SKILL = "rea-skill.md"
MANIFEST = "rea-package.json"
LOCK = "rea-lock.json"
ARCHIVE = "rea-source.tgz"
FILES = frozenset({INSPECTOR, SKILL, MANIFEST, LOCK, ARCHIVE})
RECEIPT_KEYS = frozenset({"kit", "rea", "rea_commit", "files", "rea_source_sha256"})

REA_PACKAGE = "rea-agents"
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


class SnapshotError(ValueError):
    """The snapshot or its receipt is not exactly what was staged."""


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _regular_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise SnapshotError(f"cannot inspect {path.name}") from error
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise SnapshotError(f"{path.name} is not a regular file")
    if not 0 < info.st_size <= dep.ARCHIVE_LIMIT:
        raise SnapshotError(f"{path.name} has an invalid size")
    return info


def _digest(path: Path) -> str:
    """SHA-256 of a regular, non-link file, read without following links."""
    expected = _regular_file(path)
    try:
        fd = os.open(path, _OPEN_FLAGS)
        with os.fdopen(fd, "rb") as content:
            opened = os.fstat(content.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != expected.st_size:
                raise SnapshotError(f"{path.name} changed while it was read")
            return hashlib.file_digest(content, "sha256").hexdigest()
    except OSError as error:
        raise SnapshotError(f"cannot read {path.name}") from error


def _copy_private(source: Path, target: Path, created: list[Path]) -> None:
    """Create ``target`` exclusively (owner-only), record it, and copy ``source`` into it."""
    fd = os.open(target, _CREATE_FLAGS, 0o600)
    created.append(target)
    with os.fdopen(fd, "wb") as writer, source.open("rb") as reader:
        shutil.copyfileobj(reader, writer, 1024**2)


def _kit_commit(scratch: Path, fetch) -> str:
    url = f"{dep.GITHUB}/repos/{KIT_REPOSITORY}/git/ref/heads/{KIT_BRANCH}"
    target = dep.read_json(url, scratch, fetch)["object"]
    if target["type"] != "commit" or not COMMIT.fullmatch(target["sha"]):
        raise ValueError("the Kit branch does not identify a commit")
    return target["sha"]


def _rea_members(archive: Path, commit: str) -> dict[str, bytes]:
    """Read the three authored files from the archive, keyed by retained filename."""
    root = f"rea-{commit}"
    names = {
        MANIFEST: f"{root}/package.json",
        LOCK: f"{root}/package-lock.json",
        SKILL: f"{root}/{dep.REA_SKILL}",
    }
    contents = dep.tar_members(archive, set(names.values()), source_root=root)
    return {retained: contents[member] for retained, member in names.items()}


def _verify_rea(members: dict[str, bytes], version: str) -> None:
    manifest = json.loads(members[MANIFEST])
    lock = json.loads(members[LOCK])
    if manifest["name"] != REA_PACKAGE or manifest["version"] != version:
        raise ValueError("REA manifest does not match the release")
    dep.validate_npm_lock(lock, manifest)
    if not members[SKILL].strip():
        raise ValueError("REA skill is empty")


def _stage(scratch: Path, fetch) -> tuple[dict, dict[str, Path]]:
    """Download and verify everything inside ``scratch``; return the receipt and file paths."""
    kit = _kit_commit(scratch, fetch)
    inspector = scratch / INSPECTOR
    fetch(f"https://raw.githubusercontent.com/{KIT_REPOSITORY}/{kit}/{INSPECTOR_PATH}",
          inspector, INSPECTOR_LIMIT)
    _regular_file(inspector)

    # Every release is newer than version zero, so stage_rea never reports "nothing new".
    release = dep.stage_rea(scratch, (0,), fetch)
    if release is None:
        raise ValueError("no stable REA release was staged")
    archive = scratch / ARCHIVE  # stage_rea downloads to this exact name
    if not COMMIT.fullmatch(release["commit"]) or _digest(archive) != release["source"]["sha256"]:
        raise ValueError("REA archive does not match the staged release")
    members = _rea_members(archive, release["commit"])
    _verify_rea(members, release["version"])
    for retained, expected in ((LOCK, release["lock"]["sha256"]), (SKILL, release["skill"]["sha256"])):
        if hashlib.sha256(members[retained]).hexdigest() != expected:
            raise ValueError("REA file digest does not match the staged release")

    paths = {INSPECTOR: inspector, ARCHIVE: archive}
    for retained, content in members.items():
        paths[retained] = scratch / retained
        paths[retained].write_bytes(content)
    files = {name: _digest(paths[name]) for name in sorted(FILES)}
    receipt = {
        "kit": kit,
        "rea": release["version"],
        "rea_commit": release["commit"],
        "files": files,
        "rea_source_sha256": files[ARCHIVE],
    }
    return receipt, paths


def _fetch_snapshot(destination: Path, fetch) -> dict:
    destination = Path(destination)
    try:
        info = destination.lstat()
    except OSError as error:
        raise SnapshotError("snapshot destination is not readable") from error
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise SnapshotError("snapshot destination must be an empty directory")
    try:
        empty = not any(destination.iterdir())
    except OSError as error:
        raise SnapshotError("snapshot destination is not readable") from error
    if not empty:
        raise SnapshotError("snapshot destination must be an empty directory")

    created = []
    try:
        with TemporaryDirectory(prefix="fourmilab-releases-") as directory:
            receipt, paths = _stage(Path(directory), fetch)
            for name in sorted(FILES):
                _copy_private(paths[name], destination / name, created)
        check_snapshot(destination, receipt)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return receipt


def fetch_snapshot(destination: Path) -> dict:
    """Stage the pinned Kit inspector and latest stable REA release into an empty directory.

    Returns the receipt that ``check_snapshot`` needs. The caller owns ``destination``.
    """
    return _fetch_snapshot(destination, dep.download)


def _check_receipt(receipt: object) -> None:
    if not isinstance(receipt, dict) or receipt.keys() != RECEIPT_KEYS:
        raise SnapshotError("receipt does not have exactly the expected keys")
    for key, pattern in (("kit", COMMIT), ("rea", VERSION), ("rea_commit", COMMIT),
                         ("rea_source_sha256", SHA256)):
        value = receipt[key]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise SnapshotError(f"receipt {key} is invalid")
    files = receipt["files"]
    if not isinstance(files, dict) or files.keys() != FILES:
        raise SnapshotError("receipt does not name exactly the snapshot files")
    if any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in files.values()):
        raise SnapshotError("receipt contains an invalid file digest")
    if files[ARCHIVE] != receipt["rea_source_sha256"]:
        raise SnapshotError("receipt source digest does not match the archive digest")


def check_snapshot(directory: Path, receipt: dict) -> None:
    """Raise ``SnapshotError`` unless ``directory`` is exactly the snapshot ``receipt`` describes."""
    _check_receipt(receipt)
    directory = Path(directory)
    try:
        info = directory.lstat()
        names = {entry.name for entry in os.scandir(directory)}
    except OSError as error:
        raise SnapshotError("snapshot directory is not readable") from error
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise SnapshotError("snapshot is not a real directory")
    if names != FILES:
        missing = ", ".join(sorted(FILES - names)) or "none"
        extra = ", ".join(sorted(names - FILES)) or "none"
        raise SnapshotError(f"snapshot files differ (missing: {missing}; unexpected: {extra})")
    for name in sorted(FILES):
        if _digest(directory / name) != receipt["files"][name]:
            raise SnapshotError(f"{name} does not match its recorded SHA-256")

    # The hashes are only as good as the receipt, so tie the retained data back to the
    # archive and to the release identity on every check.
    try:
        members = _rea_members(directory / ARCHIVE, receipt["rea_commit"])
        for retained, content in members.items():
            if (directory / retained).read_bytes() != content:
                raise SnapshotError(f"{retained} is not the archive's authored file")
        _verify_rea(members, receipt["rea"])
    except SnapshotError:
        raise
    except (OSError, EOFError, tarfile.TarError, KeyError, TypeError, ValueError) as error:
        raise SnapshotError("retained REA data does not match the release identity") from error
