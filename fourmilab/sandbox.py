"""The only three executable profiles in the Fourmilab candidate. No caller-supplied command."""
import hashlib
import json
import os
import selectors
import subprocess
import time
import uuid
from pathlib import Path

from . import contract

HERE = Path(__file__).resolve().parent
PYTHON = 'sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e'
RUST = 'sha256:7c7d4eb4d05b7781187a327809466cfddf29753f6defdc3d19a0e291951b7a37'
DOCKER = '/usr/bin/docker'
BUILD = 'rustc --edition=2021 -C opt-level=2 -C strip=symbols /source.rs -o /tmp/base64 && cat /tmp/base64'


class Unavailable(RuntimeError):
    """A pinned dependency, provider, or execution boundary is unavailable."""


class Cancelled(RuntimeError):
    """The operator stopped this attempt."""


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def freeze(source, target, pin):
    """Materialize the pinned bytes once in this private attempt."""
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != pin:
        raise Unavailable(f'{source.name} differs from its pinned SHA-256.')
    with target.open('xb') as output:
        output.write(data)
    target.chmod(0o444)


def check_reference(path):
    if path.is_symlink() or not path.is_file() or file_hash(path) != contract.REFERENCE_SHA256:
        raise Unavailable('Install the pinned Fourmilab Base64 1.5 reference. No other input is accepted.')


class Box:
    def __init__(self, cancel):
        self.cancel = cancel

    def _run(self, image, command, mounts, *, observer=False, build=False, progress=None):
        name = 'fourmilab-' + uuid.uuid4().hex
        ram = '768m' if build else '256m'
        flags = ['--rm', '--name', name, '--pull=never', '--runtime=runsc', '--network=none',
                 '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--cpus=1',
                 '--memory', ram, '--memory-swap', ram, '--pids-limit=64', '--log-driver=none',
                 '--ulimit=nofile=128:128', '--ulimit=core=0:0', '--user', '0:0' if observer else '65534:65534',
                 '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,mode=1777,size=' + ('128m' if build else '16m'),
                 '--env=LC_ALL=C', '--env=HOME=/tmp', '--workdir', '/input']
        if observer:
            # Only the trusted observer retains these; the specimen drops every identity and group.
            for cap in ('SETUID', 'SETGID', 'CHOWN', 'KILL', 'DAC_OVERRIDE', 'FOWNER'):
                flags += ['--cap-add', cap]
        for src, dest in mounts:
            if not src.is_absolute() or ',' in str(src) or src.is_symlink() or not src.is_file():
                raise Unavailable('A pinned mount is missing or not a regular file.')
            flags += ['--mount', f'type=bind,src={src},dst={dest},readonly']
        cmd = [DOCKER, 'run', *flags, '--entrypoint', command[0], image, *command[1:]]
        ceiling = 12 << 20 if build or observer else 64 << 10
        deadline = time.monotonic() + (600 if observer else 100)
        if self.cancel.is_set():
            raise Cancelled('Cancelled before launch.')
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            stdout, stderr = bytearray(), bytearray()
            lines = 0
            try:
                with selectors.DefaultSelector() as sel:
                    sel.register(process.stdout, selectors.EVENT_READ, stdout)
                    sel.register(process.stderr, selectors.EVENT_READ, stderr)
                    while sel.get_map():
                        if self.cancel.is_set():
                            raise Cancelled('Cancelled by operator.')
                        if time.monotonic() > deadline:
                            raise Unavailable('Fixed sandbox deadline exceeded.')
                        for key, _ in sel.select(0.25):
                            data = os.read(key.fd, 65536)
                            if not data:
                                sel.unregister(key.fd)
                                continue
                            buffer = key.data
                            buffer.extend(data)
                            if len(buffer) > (ceiling if buffer is stdout else 65536):
                                raise ValueError('Sandbox output exceeded its bound.')
                            if progress and buffer is stdout:
                                previous = lines
                                lines += data.count(b'\n')
                                if previous // 256 != lines // 256:
                                    progress(lines)
                status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                    rm = subprocess.run([DOCKER, 'rm', '-f', name], capture_output=True, timeout=15)
                    if rm.returncode and b'No such container' not in rm.stderr:
                        raise Unavailable(f'Operator must inspect the stopped job container {name}.')
        if status:
            reason = stderr.decode('utf-8', 'replace')[:1800].strip()
            if build and status not in (125, 126, 127):
                raise ValueError('Rust compilation failed: ' + reason)
            raise Unavailable(f'Fixed sandbox exited {status}: {reason}')
        return bytes(stdout)

    def inspect(self, reference, inspector):
        check_reference(reference)
        reply = json.loads(self._run(PYTHON, ['python3', '-IB', '/inspect.py', 'base64', contract.REFERENCE_SHA256],
                                     [(reference, '/input/base64'), (inspector, '/inspect.py')]))
        if reply != {'version': 1, 'sha256': contract.REFERENCE_SHA256, 'size': reference.stat().st_size, 'signature': 'elf'}:
            raise Unavailable('Pinned inspector did not identify the reference ELF.')
        return reply

    def examples(self, reference, script):
        check_reference(reference)
        return json.loads(self._run(PYTHON, ['python3', '-IB', '/examples.py'],
                                    [(reference, '/input/base64'), (script, '/examples.py')]))

    def observe(self, executable, *, original, progress, checker):
        if original:
            check_reference(executable)
        raw = self._run(PYTHON, ['python3', '-IB', '/check.py', 'observe', 'reference' if original else 'candidate'],
                        [(executable, '/input/base64'), (checker, '/check.py')], observer=True, progress=progress)
        return [json.loads(line) for line in raw.splitlines()]

    def compile(self, source, output):
        binary = self._run(RUST, ['/bin/sh', '-ec', BUILD], [(source, '/source.rs')], build=True)
        if not binary.startswith(b'\x7fELF\x02\x01'):
            raise ValueError('Compiler returned no little-endian ELF64 program.')
        with output.open('xb') as stream:
            stream.write(binary)
        output.chmod(0o555)
