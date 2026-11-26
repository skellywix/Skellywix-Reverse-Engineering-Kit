"""Prepare a fresh private candidate directory for the fixed Fourmilab Base64 1.5 desk (Linux, operator account).

Each run creates a new /tmp/fourmilab-compat.XXXXXXXX holding: a verified read-only copy of the independently
supplied original, a copy of the desk code taken from this checkout, and the connection descriptor the desk and the
subscription companion both require. It queries Docker but never reuses an earlier ledger or receipt, starts the desk,
or contacts a model provider. The supplied file is refused before preparation unless its SHA-256 matches the pin
in fourmilab/contract.py. Procedure: fourmilab/README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import string
import subprocess
import sys
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))

from fourmilab import builder, contract, pipeline, sandbox

BASE = Path('/tmp')  # builder.connection() accepts only /tmp/fourmilab-compat.<8 letters or digits>
PREFIX = 'fourmilab-compat.'
ALPHABET = string.ascii_letters + string.digits
# Everything the desk imports or serves, plus the one tool it hashes. pipeline.FILES is the desk's own list.
CODE = (*pipeline.FILES, 'index.html', 'desk.css', 'app.js')
TOOL = 'dependency_candidates.py'
MAX_REFERENCE = 256 << 20
MAX_CODE = 8 << 20
CHUNK = 1 << 20
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
READ = os.O_RDONLY | NOFOLLOW | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)


def open_regular(path: Path, limit: int):
    """Open a non-link regular file of 1..limit bytes without blocking on a FIFO or following a link."""
    try:
        descriptor = os.open(path, READ)
    except OSError as error:
        raise ValueError(f'Cannot open {path}: it must exist and must not be a symbolic link.') from error
    try:
        reader = os.fdopen(descriptor, 'rb')
    except OSError as error:  # for example a directory
        os.close(descriptor)
        raise ValueError(f'{path} must be a non-empty regular file of at most {limit} bytes.') from error
    info = os.fstat(reader.fileno())
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        reader.close()
        raise ValueError(f'{path} must be a non-empty regular file of at most {limit} bytes.')
    return reader


def copy_file(source: Path, target: Path | None, limit: int, mode: int, expected: str | None = None) -> str:
    """Return the SHA-256 of the bytes read from ``source``; write exactly those bytes to a new ``target``.

    With ``expected`` the bytes must hash to it, or nothing is left behind and ValueError is raised. The digest is
    taken from the same bytes that are written, so the copy cannot differ from what was verified.
    """
    digest = hashlib.sha256()
    size = 0
    with open_regular(source, limit) as reader:
        writer = None if target is None else os.fdopen(os.open(target, NEW, 0o600), 'wb')
        try:
            while chunk := reader.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise ValueError(f'{source} grew past {limit} bytes while it was read.')
                digest.update(chunk)
                if writer is not None:
                    writer.write(chunk)
            result = digest.hexdigest()
            if expected is not None and result != expected:
                raise ValueError(f'{source} is not the pinned Fourmilab Base64 1.5 reference '
                                 f'(SHA-256 {result}, expected {expected}). No other input is accepted.')
            if writer is not None:
                writer.flush()
                os.fsync(writer.fileno())
                os.fchmod(writer.fileno(), mode)
                writer.close()
        except BaseException:
            if writer is not None:
                writer.close()
                target.unlink(missing_ok=True)
            raise
    return result


def new_root(base: Path = BASE) -> Path:
    """Create a fresh, operator-only candidate directory with a name the desk's descriptor check accepts."""
    for _ in range(64):
        path = base / (PREFIX + ''.join(secrets.choice(ALPHABET) for _ in range(8)))
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:  # a name collision only means: draw another random name
            continue
        path.chmod(0o700)
        return path
    raise ValueError(f'Could not find an unused {PREFIX}XXXXXXXX name under {base}.')


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def install_code(app: Path) -> dict[str, str]:
    """Copy the desk code and the dependency reader it hashes into ``app`` (files 0444, directories 0700).

    The layout is fixed by the desk: fourmilab/channel.py must sit at <root>/app/fourmilab/, and the desk hashes
    tools/dependency_candidates.py beside it. A real copy, not a link, so the companion's path arithmetic holds.
    """
    for folder in (app, app / 'fourmilab', app / 'tools'):
        private_dir(folder)
    pairs = [(CHECKOUT / 'fourmilab' / name, app / 'fourmilab' / name) for name in CODE]
    pairs.append((CHECKOUT / 'tools' / TOOL, app / 'tools' / TOOL))
    return {target.relative_to(app).as_posix(): copy_file(source, target, MAX_CODE, 0o444) for source, target in pairs}


def write_connection(root: Path, host: str) -> str:
    """Write the descriptor and return the hash both ends must agree on; builder.connection() is the validator."""
    payload = json.dumps({'directory': str(root), 'host': host}, sort_keys=True, separators=(',', ':')).encode('ascii')
    path = root / 'connection.json'
    with os.fdopen(os.open(path, NEW, 0o600), 'wb') as out:
        out.write(payload)
    path.chmod(0o400)
    return builder.connection(root)[1]


def runtime_problems(run=subprocess.run, docker: str = sandbox.DOCKER) -> list[str]:
    """Read-only Docker questions the fixed sandbox depends on. An empty list means they all have the right answer."""
    if not os.path.isfile(docker) or not os.access(docker, os.X_OK):
        return [f'Docker is not installed at {docker}; the fixed sandbox uses exactly that path.']

    def query(*args: str) -> str | None:
        try:
            done = run([docker, *args], capture_output=True, timeout=30, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):  # reported below as "not ready"; the preflight never proceeds on it
            return None
        return done.stdout.decode('utf-8', 'replace').strip() if done.returncode == 0 else None

    problems = []
    for label, image in (('observer', sandbox.PYTHON), ('compiler', sandbox.RUST)):
        if query('image', 'inspect', '--format', '{{.Id}}', image) != image:
            problems.append(f'The {label} image {image} is not present, or Docker reports a different image ID for it.')
    try:
        runtimes = json.loads(query('info', '--format', '{{json .Runtimes}}') or 'null')
    except ValueError:  # unparseable output is treated as "runsc not registered" and reported below
        runtimes = None
    if not isinstance(runtimes, dict) or 'runsc' not in runtimes:
        problems.append('The gVisor runsc runtime is not registered with Docker.')
    return problems


def prepare(reference: Path, host: str, base: Path = BASE, check_runtime: bool = True) -> dict:
    """Create and return a fresh candidate directory, or raise with nothing left behind."""
    if check_runtime and (problems := runtime_problems()):
        raise ValueError('Docker is not ready: ' + ' '.join(problems))
    # Refuse a wrong, linked or special file before any directory exists.
    copy_file(reference, None, MAX_REFERENCE, 0, contract.REFERENCE_SHA256)
    root = new_root(base)
    try:
        connection = write_connection(root, host)
        code = install_code(root / 'app')
        copy_file(reference, root / 'reference', MAX_REFERENCE, 0o555, contract.REFERENCE_SHA256)
        sandbox.check_reference(root / 'reference')
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return {'root': root, 'connection_sha256': connection, 'code_files': len(code)}


def default_reference() -> Path:
    # Where the workspace's dependency view looks for the operator-supplied original (workspace-dependencies.json).
    from tools.workspace import DEFAULT_STATE
    return DEFAULT_STATE / 'fourmilab' / 'reference'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--reference', type=Path,
                        help='the independently supplied original Fourmilab Base64 1.5 program '
                             '(default: <workspace data directory>/fourmilab/reference)')
    parser.add_argument('--companion-host', required=True,
                        help='ssh host name or alias, resolvable by the companion, for THIS account on THIS host '
                             '(use localhost when the companion runs on the same machine)')
    parser.add_argument('--skip-runtime-check', action='store_true',
                        help='skip the read-only Docker preflight (the desk still needs Docker to run)')
    args = parser.parse_args(argv)
    if not sys.platform.startswith('linux') or os.getuid() == 0:
        parser.error('Use a non-root account on the Linux host that will run the desk.')
    try:
        result = prepare(args.reference or default_reference(), args.companion_host,
                         check_runtime=not args.skip_runtime_check)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Not prepared: {error}', file=sys.stderr)
        return 1
    root = result['root']
    print(f'Prepared {root}\n'
          f'  reference: read-only copy, SHA-256 {contract.REFERENCE_SHA256}\n'
          f'  code:      {result["code_files"]} files copied from this checkout to {root}/app\n'
          f'  connection descriptor SHA-256 {result["connection_sha256"]} (companion must carry the same descriptor)\n'
          f'\nNext, as this account (complete procedure: fourmilab/README.md):\n'
          f'  1. desk:      cd {root}/app && python3 -B -m fourmilab.desk serve {root}\n'
          f'  2. first release only (stage and promote through the desk page): see README step 3\n'
          f'  3. workspace: python3 {shlex.quote(str(CHECKOUT / "tools" / "workspace.py"))} '
          f'--fourmilab-socket {root}/desk.sock\n'
          f'  4. companion, in your own session for each run: python3 -B {root}/app/fourmilab/builder.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
