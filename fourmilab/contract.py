"""Fourmilab Base64 1.5 compatibility contract and sandbox observer.

``case_ids``/``match`` are the host-side contract. ``main`` is the in-sandbox
observer, run as ``python3 -I /check.py observe reference|candidate``. It runs
only the fixed cases against the fixed executable ``/input/base64`` and prints
one JSON line per case.
"""
from __future__ import annotations

import base64
import errno
import functools
import hashlib
import itertools
import json
import os
import random
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EXPECTED_CASE_COUNT = 2557
LIVE_PIPE = 'decode/live-pipe'
EXECUTABLE = '/input/base64'
REFERENCE_SHA256 = '0b946702f73b4bc2d478348bc57b5e011a3a02423ca552ae0e2176c9c5f3df22'

SEED = 150
SIZES = (*range(160), 254, 255, 256, 257, 8191, 8192, 8193, 65537)
OPTION_ARGS = (
    ('-d',), ('--noerrcheck', '-d'), ('-de',), ('-ed',), ('--dog',),
    ('--help',), ('--version',), ('--copyright',), ('-u',), ('-?',), ('-z',),
    ('--unknown',), ('--', '-d'), ('-d-',), ('-e-', 'version'), ('-', '-'),
    ('in', 'out'), ('in', '-d', 'out'), ('-d', '-', 'out'), ('missing',),
    ('in', '.'), ('in', 'out', 'extra'), ('missing', '-', 'extra'), ('-d', '.'),
    (b'missing_\xff',), ('in', 'in'),
)
OPTION_DATA = (('TQ', b'TQ'), ('YWJj', b'YWJj'), ('nul-tail', b'T Q=\x00=ignored'))

NOBODY = 65534
CASE_TIMEOUT = 5.0
LIVE_HOLD = 0.25
LIVE_TIMEOUT = 3.0
MAX_STREAM = 1 << 20
MAX_FILE = 1 << 20
PREVIEW = 96
CHUNK = 65536
FILE_NAMES = ('-d', 'in', 'out')
MAX_REPORTED = 8


@dataclass(frozen=True)
class Case:
    id: str
    args: tuple[str | bytes, ...]
    data: bytes
    posix: bool = False
    live: bool = False


def _slug(args: tuple[str | bytes, ...]) -> str:
    text = '_'.join(a.decode('latin-1') if isinstance(a, bytes) else a for a in args)
    return re.sub(r'[^A-Za-z0-9.=-]', '_', text)


@functools.lru_cache(maxsize=1)
def _plan() -> tuple[Case, ...]:
    """Every case, in the order of the prior comparison; live-pipe last."""
    rng = random.Random(SEED)
    cases: list[Case] = []
    for n in SIZES:
        data = rng.randbytes(n)
        cases.append(Case(f'encode/{n}', (), data))
        cases.append(Case(f'decode/{n}', ('--decode',), base64.b64encode(data)))
    for c in range(256):
        for pos in range(4):
            data = b'QUJD' + b'A' * pos + bytes([c]) + b'A' * (3 - pos) + b'YWJj'
            cases.append(Case(f'byte/{c:02x}/{pos}/d', ('-d',), data))
            cases.append(Case(f'byte/{c:02x}/{pos}/dn', ('-dn',), data))
    for group in itertools.product(b'A=', repeat=4):
        cases.append(Case(f'pad/{bytes(group).decode()}', ('-d',), bytes(group) + b'#!'))
    for index, args in enumerate(OPTION_ARGS):
        for posix in (True, False):
            mode = 'posix' if posix else 'plain'
            for label, data in OPTION_DATA:
                cases.append(Case(f'args/{index:02d}-{_slug(args)}/{mode}/{label}',
                                  args, data, posix))
    cases.append(Case(LIVE_PIPE, ('-d',), b'', live=True))
    ids = [case.id for case in cases]
    if len(ids) != EXPECTED_CASE_COUNT or len(set(ids)) != len(ids):
        raise RuntimeError(f'case plan is {len(ids)} cases, {len(set(ids))} unique; '
                           f'expected {EXPECTED_CASE_COUNT} unique')
    if ids[-1] != LIVE_PIPE or ids.count(LIVE_PIPE) != 1:
        raise RuntimeError('decode/live-pipe must be the single last case')
    return tuple(cases)


def case_ids() -> list[str]:
    """Deterministic ids of all declared cases; ``decode/live-pipe`` is last."""
    return [case.id for case in _plan()]


# --- Host-side matching -------------------------------------------------------

def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _names(names: Sequence[str]) -> str:
    shown = ', '.join(names[:MAX_REPORTED])
    return shown + (f', ... (+{len(names) - MAX_REPORTED} more)' if len(names) > MAX_REPORTED else '')


def _index_rows(label: str, rows: Sequence[Any], required: set[str], known: set[str],
                problems: list[str]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    duplicate: list[str] = []
    unknown: list[str] = []
    unrequired: list[str] = []
    malformed = 0
    for row in rows:
        name = row.get('id') if isinstance(row, Mapping) else None
        if not isinstance(name, str):
            malformed += 1
        elif name not in known:
            unknown.append(name)
        elif name not in required:
            unrequired.append(name)
        elif name in indexed:
            duplicate.append(name)
        else:
            indexed[name] = row
    if malformed:
        problems.append(f'{label}: {malformed} malformed row(s) without a string id')
    for what, names in (('unknown', unknown), ('unrequired', unrequired),
                        ('duplicate', duplicate)):
        if names:
            problems.append(f'{label}: {what} case(s): {_names(sorted(set(names)))}')
    missing = sorted(required - indexed.keys())
    if missing:
        problems.append(f'{label}: missing case(s): {_names(missing)}')
    return indexed


def match(required: Sequence[str], expected_rows: Sequence[Mapping[str, Any]],
          actual_rows: Sequence[Mapping[str, Any]]) -> int:
    """Return the number of required cases verified, or raise ValueError.

    Each required case must appear exactly once in both row sets and be
    identical in every field; a repeat of another row never fills a gap.
    Order is irrelevant.
    """
    known = set(case_ids())
    problems: list[str] = []
    if isinstance(required, str) or not required:
        raise ValueError('required must be a non-empty list of case ids')
    wanted = set(required)
    if len(wanted) != len(required):
        counts = {name for name in required if list(required).count(name) > 1}
        problems.append(f'required: duplicate case(s): {_names(sorted(counts, key=str))}')
    bad = sorted(str(name) for name in wanted if name not in known)
    if bad:
        problems.append(f'required: unknown case(s): {_names(bad)}')
    wanted &= known
    expected = _index_rows('expected', expected_rows, wanted, known, problems)
    actual = _index_rows('actual', actual_rows, wanted, known, problems)
    for name in sorted(expected.keys() & actual.keys()):
        try:
            same = _canon(expected[name]) == _canon(actual[name])
            fields = sorted(key for key in expected[name].keys() | actual[name].keys()
                            if _canon(expected[name].get(key, _MISSING)) !=
                            _canon(actual[name].get(key, _MISSING)))
        except (TypeError, ValueError) as exc:
            problems.append(f'{name}: row is not comparable JSON ({exc})')
            continue
        if not same:
            problems.append(f'{name}: mismatch in field(s): {_names([str(f) for f in fields])}')
    if problems:
        raise ValueError('; '.join(problems))
    return len(required)


_MISSING = '<missing field>'


# --- Sandbox observer ---------------------------------------------------------

class CaseError(RuntimeError):
    """Infrastructure or limit failure for one case."""


def _blob(data: bytes) -> dict[str, Any]:
    return {'len': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'preview': data[:PREVIEW].hex()}


def _put(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        os.fchown(handle.fileno(), NOBODY, NOBODY)


def _file_state(path: str) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink
            return {'kind': 'symlink'}
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return {'kind': 'dir' if stat.S_ISDIR(info.st_mode) else 'other'}
        if info.st_size > MAX_FILE:
            raise CaseError(f'file {os.path.basename(path)!r} exceeds {MAX_FILE} bytes')
        chunks = []
        while chunk := os.read(fd, CHUNK):
            chunks.append(chunk)
            if sum(map(len, chunks)) > MAX_FILE:
                raise CaseError(f'file {os.path.basename(path)!r} exceeds {MAX_FILE} bytes')
        return {'kind': 'file', **_blob(b''.join(chunks))}
    finally:
        os.close(fd)


def _drop_privileges(limits: list[tuple[int, int]]) -> None:
    """Runs in the forked child: become nobody, verify, then apply limits."""
    os.setgroups([])
    os.setgid(NOBODY)
    os.setuid(NOBODY)
    if (os.getresuid() != (NOBODY,) * 3 or os.getresgid() != (NOBODY,) * 3
            or os.getgroups()):
        raise RuntimeError('privilege drop incomplete')
    try:
        os.setuid(0)
    except PermissionError:
        pass  # Expected: an unprivileged process cannot regain uid 0.
    else:
        raise RuntimeError('privileges can be regained')
    import resource
    for kind, value in limits:
        resource.setrlimit(kind, (value, value))


def _spawn(case: Case, cwd: str, preexec) -> subprocess.Popen:
    env = {'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'}
    if case.posix:
        env['POSIXLY_CORRECT'] = '1'
    return subprocess.Popen(['base64', *case.args], executable=EXECUTABLE, cwd=cwd,
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0, close_fds=True,
                            start_new_session=True, preexec_fn=preexec)


def _pump(proc: subprocess.Popen, data: bytes, deadline: float, *, hold_stdin=False) -> tuple[bytes, bytes]:
    """Keep the live case's pipe open after the last byte; all other cases send EOF."""
    with selectors.DefaultSelector() as selector:
        return _transfer(proc, data, deadline, selector, hold_stdin)


def _transfer(proc, data, deadline, sel, hold_stdin) -> tuple[bytes, bytes]:
    captured: dict[int, bytearray] = {}
    for stream in (proc.stdout, proc.stderr):
        os.set_blocking(stream.fileno(), False)
        sel.register(stream.fileno(), selectors.EVENT_READ)
        captured[stream.fileno()] = bytearray()
    stdin_fd = proc.stdin.fileno()
    view = memoryview(data)
    sent = 0
    if data:
        os.set_blocking(stdin_fd, False)
        sel.register(stdin_fd, selectors.EVENT_WRITE)
    elif not hold_stdin:
        proc.stdin.close()
    while sel.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CaseError('timed out')
        for key, _ in sel.select(remaining):
            fd = key.fd
            if fd == stdin_fd:
                try:
                    sent += os.write(fd, view[sent:sent + CHUNK])
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    sent = len(data)
                if sent >= len(data):
                    sel.unregister(fd)
                    if not hold_stdin:
                        proc.stdin.close()
                continue
            try:
                chunk = os.read(fd, CHUNK)
            except BlockingIOError:
                continue
            if not chunk:
                sel.unregister(fd)
                continue
            buffer = captured[fd]
            buffer += chunk
            if len(buffer) > MAX_STREAM:
                raise CaseError(f'output exceeded {MAX_STREAM} bytes')
    try:
        proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
    except subprocess.TimeoutExpired:
        raise CaseError('timed out') from None
    return bytes(captured[proc.stdout.fileno()]), bytes(captured[proc.stderr.fileno()])


def _reap(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # Already gone: nothing left in the group to kill.
    proc.wait()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        stream.close()


def _run(case: Case, cwd: str, preexec) -> tuple[int, bytes, bytes]:
    proc = _spawn(case, cwd, preexec)
    try:
        if case.live:
            try:
                proc.stdin.write(b'TQ==')
            except BrokenPipeError:
                pass  # Child closed stdin early; the decoder result still decides the case.
            try:
                proc.wait(timeout=LIVE_HOLD)
            except subprocess.TimeoutExpired:
                pass  # Expected: the writer still holds the pipe.
            else:
                raise CaseError('decoder returned before block was full or EOF')
            stdout, stderr = _pump(proc, b' ' * 252, time.monotonic() + LIVE_TIMEOUT, hold_stdin=True)
        else:
            stdout, stderr = _pump(proc, case.data, time.monotonic() + CASE_TIMEOUT)
        return proc.returncode, stdout, stderr
    finally:
        _reap(proc)


def _observe_case(case: Case, root: str, index: int, preexec) -> dict[str, Any]:
    cwd = os.path.join(root, f'{index:04d}')
    os.mkdir(cwd, 0o755)
    os.chmod(cwd, 0o1777)  # sticky: the child cannot replace files it does not own
    try:
        stored = b'' if case.live else case.data
        _put(os.path.join(cwd, 'in'), stored)
        _put(os.path.join(cwd, '-d'), stored)
        code, stdout, stderr = _run(case, cwd, preexec)
        files = {name: _file_state(os.path.join(cwd, name)) for name in FILE_NAMES}
    finally:
        shutil.rmtree(cwd)
    return {'id': case.id, 'exit': code, 'stdout': _blob(stdout),
            'stderr': _blob(stderr), 'files': files}


def _check_executable(mode: str) -> None:
    info = os.stat(EXECUTABLE)
    if not stat.S_ISREG(info.st_mode) or not os.access(EXECUTABLE, os.X_OK):
        raise CaseError(f'{EXECUTABLE} is not an executable file')
    if mode == 'reference':
        digest = hashlib.sha256()
        with open(EXECUTABLE, 'rb') as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
        if digest.hexdigest() != REFERENCE_SHA256:
            raise CaseError(f'{EXECUTABLE} is not the known reference (sha256 mismatch)')


def observe(mode: str) -> int:
    import resource
    os.umask(0o022)
    limits = [(resource.RLIMIT_CPU, 10), (resource.RLIMIT_AS, 512 << 20),
              (resource.RLIMIT_FSIZE, MAX_FILE), (resource.RLIMIT_NOFILE, 32),
              (resource.RLIMIT_NPROC, 32), (resource.RLIMIT_CORE, 0)]
    preexec = functools.partial(_drop_privileges, limits)
    try:
        _check_executable(mode)
        root = tempfile.mkdtemp(prefix='fourmilab-', dir='/tmp')
    except (OSError, CaseError) as exc:
        sys.stderr.write(f'infra error: {str(exc)[:200]}\n')
        return 2
    try:
        os.chmod(root, 0o711)
        for index, case in enumerate(_plan()):
            try:
                row = _observe_case(case, root, index, preexec)
            except Exception as exc:  # Report which case failed, then stop non-zero.
                sys.stderr.write(f'case {case.id}: {type(exc).__name__}: {str(exc)[:200]}\n')
                return 1
            sys.stdout.write(_canon(row) + '\n')
            sys.stdout.flush()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2 or argv[0] != 'observe' or argv[1] not in ('reference', 'candidate'):
        sys.stderr.write('usage: check.py observe reference|candidate\n')
        return 2
    return observe(argv[1])


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
