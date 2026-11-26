"""Bounded reads and write-once result files. Every protected file is read through one descriptor."""
import os
import stat
import uuid
from pathlib import Path

from .errors import Refused, ResultLost


def read_regular(path, limit, *, protected=False):
    """Read one regular, non-symlink file once. `protected` also refuses group/other-writable files on POSIX."""
    path = Path(path)
    if path.is_symlink():  # O_NOFOLLOW below is the real guard on POSIX; this keeps other hosts equally strict.
        raise Refused(f'{path.name} is a symbolic link.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise Refused(f'Cannot open {path.name}: not a readable regular file.') from error
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f'{path.name} is not a regular file.')
        if protected and os.name == 'posix' and info.st_mode & 0o022:
            raise Refused(f'{path.name} is writable by group or other; protected files must not be.')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Refused(f'{path.name} exceeds its {limit}-byte limit.')
    return data


def write_once(path, data):
    """Create `path` with `data` atomically and never replace it; read it back to confirm."""
    path = Path(path)
    temp = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError as error:
            raise Refused(f'{path.name} already exists; a verifier record is never replaced.') from error
    except OSError as error:
        raise ResultLost(f'Could not write {path.name}: {error.strerror or error}') from error
    finally:
        temp.unlink(missing_ok=True)
    if path.read_bytes() != data:
        raise ResultLost(f'{path.name} did not read back identically.')


def replace_file(path, data):
    """Atomically replace the latest-state file."""
    path = Path(path)
    temp = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError as error:
        temp.unlink(missing_ok=True)
        raise ResultLost(f'Could not write {path.name}: {error.strerror or error}') from error
