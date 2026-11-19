"""Inspect one local file as bytes. No sample parser or external program is used."""
import sys

# Do not import from the launcher's search path if invoked without isolation.
if __name__ == "__main__" and not sys.flags.isolated:
    print("inspection requires python3 -I", file=sys.stderr)
    raise SystemExit(2)

import hashlib
import json
import os
import stat


MAX_BYTES = 2 * 1024 ** 3
CHUNK_BYTES = 1024 * 1024
SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "png"), (b"%PDF-", "pdf"), (b"PK\x03\x04", "zip"),
              (b"\x7fELF", "elf"), (b"MZ", "mz"))
HEADER_BYTES = max(len(prefix) for prefix, _ in SIGNATURES)
USAGE = "usage: python3 -I inspect_file.py BASENAME [EXPECTED_SHA256]"


class InspectionError(Exception):
    """Only fixed, sample-independent messages cross the CLI boundary."""


def _valid_name(name: str) -> bool:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return False
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name):
        return False
    try:
        return len(name.encode("utf-8")) <= 255
    except UnicodeEncodeError:
        return False


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _inspect_fd(fd: int, before: os.stat_result) -> dict[str, int | str]:
    digest = hashlib.sha256()
    header = bytearray()
    remaining = before.st_size
    while remaining:
        chunk = os.read(fd, min(CHUNK_BYTES, remaining))
        if not chunk:
            raise InspectionError("file changed during inspection")
        digest.update(chunk)
        if len(header) < HEADER_BYTES:
            header.extend(chunk[:HEADER_BYTES - len(header)])
        remaining -= len(chunk)
    if os.read(fd, 1) or _identity(os.fstat(fd)) != _identity(before):
        raise InspectionError("file changed during inspection")
    signature = next((label for prefix, label in SIGNATURES if header.startswith(prefix)), "unknown")
    return {"version": 1, "size": before.st_size, "sha256": digest.hexdigest(), "signature": signature}


def inspect_file(name: str) -> dict[str, int | str]:
    """Inspect a basename relative to the operator's current directory on Linux."""
    if sys.platform != "linux" or not hasattr(os, "O_PATH"):
        raise InspectionError("Linux with procfs is required")
    if not _valid_name(name):
        raise InspectionError("expected a single safe filename")
    # Pin the object for metadata only: a FIFO or device must not be opened for I/O.
    pinned = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(pinned)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise InspectionError("expected a single-link regular file")
        if not 0 <= before.st_size <= MAX_BYTES:
            raise InspectionError("input exceeds the size limit")
        # Reopen our own pinned inode, not the untrusted name (which can be replaced).
        fd = os.open(f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_CLOEXEC)
        try:
            return _inspect_fd(fd, before)
        finally:
            os.close(fd)
    finally:
        os.close(pinned)


def main(args: list[str]) -> int:
    if len(args) not in (1, 2):
        print(USAGE, file=sys.stderr)
        return 2
    expected = args[1] if len(args) == 2 else None
    if expected is not None and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
        print("expected a lowercase SHA-256 digest", file=sys.stderr)
        return 2
    try:
        result = inspect_file(args[0])
        if expected is not None and result["sha256"] != expected:
            raise InspectionError("SHA-256 mismatch")
    except InspectionError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError:
        print("input unavailable", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
