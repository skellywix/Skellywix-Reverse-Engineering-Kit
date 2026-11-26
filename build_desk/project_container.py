"""The only execution boundary of project builds: one fresh, networkless, image-ID-pinned container per run.

* No host mount of any kind: input arrives as a tar on stdin, output leaves as a tar on stdout. The Docker socket,
  host paths, volumes and the network are never available to the container.
* Fixed argv: the entrypoint is `/bin/sh -c <bootstrap>`; the build script itself travels inside the stdin tar.
* gVisor (`--runtime=runsc`) by default, no capabilities, no new privileges, read-only root, unprivileged user,
  bounded memory/CPU/pids/files, tmpfs scratch with a size cap, a wall-clock deadline and bounded output streams.

The host process never executes anything the container produced.
"""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')
ENV_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,31}')
ENV_VALUE = re.compile(r'[A-Za-z0-9._/+:=-]{0,200}')
STDERR_KEEP = 256 << 10
PROBE_TTL = 30.0
DOCKER_ENVIRONMENT_ERRORS = (125, 126, 127)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0

LIMITS = (
    '--cpus=2', '--memory=4g', '--memory-swap=4g', '--pids-limit=1024', '--ulimit=nofile=4096:4096',
    '--ulimit=core=0:0',
)


@dataclass(frozen=True)
class Spec:
    image: str
    name: str
    bootstrap: str
    env: tuple          # ((NAME, value), ...)
    feed: object        # callable(binary_stream): writes the stdin tar
    deadline: float     # seconds
    max_stdout: int


@dataclass(frozen=True)
class Result:
    status: int | None
    stdout: bytes
    stderr: bytes       # the last STDERR_KEEP bytes
    timed_out: bool = False
    overflow: bool = False
    stopped: bool = False
    feed_error: str | None = None


class _Bounded:
    """Reads a pipe on a thread. 'head' keeps the first `limit` bytes and flags overflow; 'tail' keeps the last."""

    def __init__(self, stream, limit, keep):
        self.limit = limit
        self.keep = keep
        self.data = bytearray()
        self.overflow = False
        self._stream = stream
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            while block := self._stream.read(65536):
                if self.keep == 'head':
                    room = self.limit - len(self.data)
                    self.data.extend(block[:max(room, 0)])
                    if len(block) > room:
                        self.overflow = True
                        return
                else:
                    self.data.extend(block)
                    if len(self.data) > 2 * self.limit:
                        del self.data[:len(self.data) - self.limit]
        except (OSError, ValueError):
            return  # the pipe closed under us because the process was killed; the status carries the failure
        finally:
            self._stream.close()

    def result(self):
        data = bytes(self.data)
        return data[-self.limit:] if self.keep == 'tail' else data


class DockerRunner:
    """Runs one Spec to completion. `runtime=None` is refused: the sandbox is gVisor or nothing."""

    def __init__(self, docker=None, runtime='runsc', probe_ttl=PROBE_TTL):
        if not runtime:
            raise ValueError('A container runtime is required.')
        self._docker = docker
        self.runtime = runtime
        self._ttl = probe_ttl
        self._cache = {}
        self._lock = threading.Lock()

    def docker(self):
        path = self._docker or shutil.which('docker')
        if not path:
            raise RuntimeError('Docker is not installed or is not on PATH.')
        return path

    def _query(self, args):
        try:
            done = subprocess.run([self.docker(), *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                  creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('Docker did not answer.') from error
        return done

    def preflight(self, image):
        """Raises RuntimeError with a plain reason unless Docker, the runtime and the pinned image are present."""
        if not IMAGE_ID.fullmatch(image or ''):
            raise RuntimeError('The compiler image is not pinned to an image ID.')
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(image)
            if hit and now - hit[0] < self._ttl:
                if hit[1]:
                    raise RuntimeError(hit[1])
                return
        reason = self._probe(image)
        with self._lock:
            self._cache[image] = (time.monotonic(), reason)
        if reason:
            raise RuntimeError(reason)

    def _probe(self, image):
        try:
            info = self._query(['info', '--format', '{{json .Runtimes}}'])
        except RuntimeError as error:
            return str(error)
        if info.returncode:
            return 'Docker is not running.'
        try:
            runtimes = json.loads(info.stdout.decode('utf-8', 'replace') or 'null') or {}
        except ValueError:
            runtimes = {}
        if self.runtime not in runtimes:
            return f'The {self.runtime} container runtime is not registered with Docker.'
        try:
            seen = self._query(['image', 'inspect', '--format', '{{.Id}}', image])
        except RuntimeError as error:
            return str(error)
        if seen.returncode or seen.stdout.decode('ascii', 'replace').strip() != image:
            return 'The pinned image is not installed on this host.'
        return None

    def argv(self, spec):
        """The complete docker command for one run. Contains no mount, socket, port or network."""
        if not IMAGE_ID.fullmatch(spec.image):
            raise ValueError('The image is not pinned to an image ID.')
        if not re.fullmatch(r'projbuild-[0-9a-f]{32}', spec.name):
            raise ValueError('Unexpected container name.')
        env = []
        for key, value in spec.env:
            if not ENV_NAME.fullmatch(key) or not ENV_VALUE.fullmatch(value):
                raise ValueError('Unexpected container environment.')
            env.append(f'--env={key}={value}')
        return [self.docker(), 'run', '--rm', '-i', '--name', spec.name, '--pull=never', f'--runtime={self.runtime}',
                '--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', *LIMITS, '--log-driver=none',
                '--env=LC_ALL=C', '--env=HOME=/work/home', '--env=TMPDIR=/work/tmp', '--env=CARGO_HOME=/work/cargo',
                '--env=CARGO_TARGET_DIR=/work/target', '--env=CARGO_INCREMENTAL=0', '--env=CARGO_NET_OFFLINE=true',
                '--env=CARGO_TERM_COLOR=never', '--env=SOURCE_DATE_EPOCH=315532800', *env,
                '--tmpfs', '/work:rw,nosuid,nodev,exec,uid=65534,gid=65534,mode=0700,size=2g',
                '--tmpfs', '/tmp:rw,nosuid,nodev,exec,uid=65534,gid=65534,mode=1777,size=256m',
                '--entrypoint', '/bin/sh', spec.image, '-c', spec.bootstrap]

    def _remove(self, name):
        done = self._query(['rm', '-f', name])
        if done.returncode and b'No such container' not in done.stderr:
            raise RuntimeError(f'Operator must inspect the stopped build container {name}.')

    def discard(self, name):
        """Remove a leftover build container (after an interrupted run). RuntimeError if that cannot be confirmed."""
        if not re.fullmatch(r'projbuild-[0-9a-f]{32}', name):
            raise ValueError('Unexpected container name.')
        self._remove(name)

    def run(self, spec, stop):
        """Start the container, stream `spec.feed` to its stdin, and wait. `stop` is a threading.Event."""
        argv = self.argv(spec)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=NO_WINDOW)
        except OSError as error:
            raise RuntimeError('Docker could not be started.') from error
        out = _Bounded(process.stdout, spec.max_stdout, 'head')
        err = _Bounded(process.stderr, STDERR_KEEP, 'tail')
        problem = []

        def pump():
            try:
                spec.feed(process.stdin)
            except (BrokenPipeError, ConnectionResetError):
                return  # the container ended before reading everything; its exit status explains why
            except ValueError as error:
                problem.append(str(error))  # an archive member failed its integrity check; text is ours
            except OSError:
                problem.append('The project could not be delivered to the sandbox.')
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass  # a broken stdin pipe at close means the container already exited

        feeder = threading.Thread(target=pump, daemon=True)
        feeder.start()
        deadline = time.monotonic() + spec.deadline
        verdict = None
        try:
            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if stop.is_set():
                    verdict = 'stopped'
                elif time.monotonic() > deadline:
                    verdict = 'timeout'
                elif out.overflow:
                    verdict = 'overflow'
                elif problem:
                    verdict = 'feed'
                if verdict:
                    break
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
                self._remove(spec.name)
        feeder.join(10)
        out.thread.join(10)
        err.thread.join(10)
        return Result(process.returncode, out.result(), err.result(), verdict == 'timeout',
                      out.overflow or verdict == 'overflow', verdict == 'stopped', problem[0] if problem else None)
