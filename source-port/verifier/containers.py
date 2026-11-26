"""The only execution boundary: fresh, networkless, image-ID pinned gVisor containers with fixed argv.

The two image IDs and the containment flags are the Fourmilab standard (fourmilab/sandbox.py), copied rather than
imported so this module shares no executor with it. Nothing here accepts a caller-supplied command: the argv for each
role is assembled by run.py from compile-time constants plus case data from the pinned registry.
"""
import json
import os
import selectors
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass

from . import candidate_image, elf
from .errors import MeasurementFailed, Unavailable

OBSERVER_IMAGE = 'sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e'
COMPILER_IMAGE = 'sha256:7c7d4eb4d05b7781187a327809466cfddf29753f6defdc3d19a0e291951b7a37'
# The runtime for compiled candidates. Until a reviewed candidate-only image is pinned this is the observer image, which
# keeps candidate_runtime_blocker() unconditional. Never set it to an unreviewed image ID.
CANDIDATE_IMAGE = OBSERVER_IMAGE
DOCKER = '/usr/bin/docker'
# Static and non-PIE: the candidate-only runtime has no loader or library, so the program must not need either.
BUILD_SCRIPT = ('rustc --edition=2021 --crate-type=bin --crate-name=port -C opt-level=2 -C debuginfo=0 '
                '-C strip=symbols -C target-feature=+crt-static -C relocation-model=static -F unsafe_code '
                '/source.rs -o /tmp/port && cat /tmp/port')
BUILD_ARGV = ('/bin/sh', '-ec', BUILD_SCRIPT)
ORIGINAL_ARGV = ('python3', '-IB', '/app/main.py')
CANDIDATE_ENTRYPOINT = '/candidate'
ELF64_LE = elf.LITTLE_ENDIAN_ELF64
ROLES = ('build', 'original', 'candidate')
IMAGES = {'build': COMPILER_IMAGE, 'original': OBSERVER_IMAGE, 'candidate': CANDIDATE_IMAGE}
STREAM_CEILING = 64 << 10
BUILD_OUTPUT_CEILING = 12 << 20
# A candidate task count of 1 stops fork and clone, not execve: it is defense in depth, not a no-fallback proof. The
# original is never mounted in a candidate container; screen.py records what the candidate source names.
PIDS = {'build': 64, 'original': 64, 'candidate': 1}
DEADLINE_SECONDS = {'build': 100, 'original': 10, 'candidate': 10}
DOCKER_ENVIRONMENT_ERRORS = (125, 126, 127)
# Image IDs a reviewer has shown to hold only the candidate: no interpreter, shell, original program, or fallback path.
# Empty: no image is reviewed. Pinning one is a reviewed code change by a human who has compared the exact archive
# bytes (candidate_image.py) and the ID Docker reports for the loaded image: set CANDIDATE_IMAGE and name it here.
REVIEWED_CANDIDATE_ONLY_IMAGES = frozenset()


def candidate_runtime_blocker():
    """None only for a reviewed candidate-only image; otherwise the plain reason no receipt can be issued.

    A runtime "no fallback" needs a candidate-only runtime. The original runs `python3` on OBSERVER_IMAGE and the
    compiler image carries the toolchain and a shell, so a candidate sharing either image has a fallback path within
    reach; any other image ID is merely unreviewed, not proof. Blocked today.
    """
    image = IMAGES['candidate']
    if image in (IMAGES['original'], IMAGES['build']):
        return ('No candidate-only runtime exists in the fixed environment: the candidate would run on the same pinned '
                'image as the original program and its interpreter, or as the compiler. Nothing was run and no receipt '
                'was issued.')
    if image not in REVIEWED_CANDIDATE_ONLY_IMAGES:
        return ('The candidate image is not a reviewed candidate-only runtime: no reviewed candidate-only image is '
                'pinned. Nothing was run and no receipt was issued.')
    return None


@dataclass(frozen=True)
class RunSpec:
    role: str
    argv: tuple
    mounts: tuple  # ((absolute host file Path, container path), ...), always read-only
    stdin: bytes = b''

    @property
    def image(self):
        return IMAGES[self.role]


@dataclass(frozen=True)
class RunOutput:
    status: int
    stdout: bytes
    stderr: bytes


def docker_argv(spec, name):
    """The complete docker command for one run. Raises Unavailable for a mount that is not a pinned regular file."""
    if spec.role not in ROLES or not spec.argv:
        raise Unavailable('Only the fixed build, original, and candidate roles can run.')
    build = spec.role == 'build'
    ram = '768m' if build else '256m'
    flags = ['--rm', '--name', name, '--pull=never', '--runtime=runsc', '--network=none',
             '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--cpus=1',
             '--memory', ram, '--memory-swap', ram, f'--pids-limit={PIDS[spec.role]}', '--log-driver=none',
             '--ulimit=nofile=128:128', '--ulimit=core=0:0', '--user', '65534:65534',
             '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,mode=1777,size=' + ('128m' if build else '16m'),
             '--env=LC_ALL=C', '--env=HOME=/tmp', '--workdir', '/tmp']
    if not build:
        flags.append('--interactive')
    for src, dest in spec.mounts:
        if not src.is_absolute() or ',' in str(src) or src.is_symlink() or not src.is_file():
            raise Unavailable('A pinned mount is missing or not a regular file.')
        flags += ['--mount', f'type=bind,src={src},dst={dest},readonly']
    return [DOCKER, 'run', *flags, '--entrypoint', spec.argv[0], spec.image, *spec.argv[1:]]


def recipe():
    """Everything that defines how code runs. run.py hashes it into recipe_sha256, so any change is a new recipe."""
    return {
        'build': list(BUILD_ARGV),
        'original_argv': list(ORIGINAL_ARGV),
        'candidate_entrypoint': CANDIDATE_ENTRYPOINT,
        'baseline_runs': 2,
        'candidate_runs': 1,
        'stream_ceiling': STREAM_CEILING,
        'build_output_ceiling': BUILD_OUTPUT_CEILING,
        'deadline_seconds': DEADLINE_SECONDS,
        'docker': {role: docker_argv(RunSpec(role, ('/x',), ()), 'name')[1:] for role in ROLES},
    }


def toolchain():
    return {'compiler_image': COMPILER_IMAGE, 'observer_image': OBSERVER_IMAGE}


def docker_query(args):
    """A read-only docker CLI query for preflight; the arguments are fixed by preflight()."""
    try:
        done = subprocess.run([DOCKER, *args], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unavailable('Docker query failed.') from error
    if done.returncode:
        raise Unavailable(f'Docker query failed (exit {done.returncode}).')
    return done.stdout


def _text_list(value):
    """None for an absent or null list; the list for a list of strings; anything else is a malformed description."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError
    return value


def inspect_image(image):
    """The facts the candidate-only shape depends on, from Docker's own `image inspect` JSON for one image.

    Docker omits RootFS.Layers for an image with no layers, so an absent Layers key on a valid layers rootfs means
    zero layers. Every structural object (the array of one image, RootFS, Config) must be present and well formed;
    anything else fails closed as Unavailable, never as a default.
    """
    raw = docker_query(['image', 'inspect', image])
    try:
        described = json.loads(raw.decode('utf-8'))
        if not isinstance(described, list) or len(described) != 1 or not isinstance(described[0], dict):
            raise ValueError
        found = described[0]
        rootfs, config = found.get('RootFS'), found.get('Config')
        if not isinstance(rootfs, dict) or rootfs.get('Type') != 'layers' or not isinstance(config, dict):
            raise ValueError
        identifier, system, architecture = found.get('Id'), found.get('Os'), found.get('Architecture')
        if not all(isinstance(value, str) and value for value in (identifier, system, architecture)):
            raise ValueError
        return {'id': identifier, 'layers': _text_list(rootfs.get('Layers')) or [],
                'entrypoint': _text_list(config.get('Entrypoint')), 'command': _text_list(config.get('Cmd')),
                'env': _text_list(config.get('Env')), 'platform': f'{system}/{architecture}'}
    except (UnicodeDecodeError, ValueError) as error:
        raise Unavailable('Docker did not describe the image.') from error


def check_candidate_image(image):
    """The live candidate-only image must have the generated shape: no layers, entrypoint, command, or environment.

    A check at the real boundary on top of the ID pin. It does not make an image reviewed.
    """
    facts = inspect_image(image)
    if facts['id'] != image:
        raise Unavailable('Docker described a different image than the pinned candidate image.')
    problem = candidate_image.shape_problem(facts['layers'], facts['entrypoint'], facts['command'], facts['env'],
                                            facts['platform'])
    if problem:
        raise Unavailable(problem)


def require_linux():
    if not sys.platform.startswith('linux'):
        raise Unavailable(f'Real execution needs Linux with Docker and gVisor; this host is {sys.platform}.')


def preflight():
    """Fail closed with a plain reason unless Docker, every pinned image, and the runsc runtime are present."""
    require_linux()
    if not os.path.isfile(DOCKER):
        raise Unavailable(f'Docker is not installed at {DOCKER}.')
    for image in dict.fromkeys((COMPILER_IMAGE, OBSERVER_IMAGE, IMAGES['candidate'])):
        found = docker_query(['image', 'inspect', '--format', '{{.Id}}', image]).decode('utf-8', 'replace').strip()
        if found != image:
            raise Unavailable(f'Pinned image {image} is not preloaded.')
    if IMAGES['candidate'] not in (OBSERVER_IMAGE, COMPILER_IMAGE):
        check_candidate_image(IMAGES['candidate'])
    try:
        runtimes = json.loads(docker_query(['info', '--format', '{{json .Runtimes}}']))
    except ValueError as error:
        raise Unavailable('Docker did not report its runtimes.') from error
    if not isinstance(runtimes, dict) or 'runsc' not in runtimes:
        raise Unavailable('The gVisor runsc runtime is not registered with Docker.')


def _feed(stream, data, problems):
    """Deliver stdin from a thread. Only a closed pipe is benign; any other failure is reported to launch()."""
    try:
        stream.write(data)
        stream.close()
    except BrokenPipeError:  # The program exited without reading all stdin: that is observable behavior.
        try:
            stream.close()
        except BrokenPipeError:  # Closing flushes the same unread bytes into the same closed pipe.
            return
    except (OSError, ValueError):
        problems.append('Could not deliver stdin.')  # The OSError text can name host details; it stays off the browser.


def launch(spec):
    """Run one fresh container to completion. Returns its exit status and complete streams, or raises.

    A timeout, an output over its bound, or a container that cannot be accounted for is MeasurementFailed, never a
    prefix. Docker's own failure statuses (125-127) are Unavailable.
    """
    require_linux()
    name = 'source-port-' + uuid.uuid4().hex
    command = docker_argv(spec, name)
    role = spec.role
    ceiling = BUILD_OUTPUT_CEILING if role == 'build' else STREAM_CEILING
    deadline = time.monotonic() + DEADLINE_SECONDS[role]
    stdin = subprocess.PIPE if role != 'build' else subprocess.DEVNULL
    problems = []
    with subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        stdout, stderr = bytearray(), bytearray()
        feeder = None
        try:
            if stdin == subprocess.PIPE:
                feeder = threading.Thread(target=_feed, args=(process.stdin, spec.stdin, problems), daemon=True)
                feeder.start()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, stdout)
                selector.register(process.stderr, selectors.EVENT_READ, stderr)
                while selector.get_map():
                    if time.monotonic() > deadline:
                        raise MeasurementFailed('Fixed sandbox deadline exceeded.')
                    for key, _ in selector.select(0.25):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fd)
                            continue
                        key.data.extend(data)
                        if len(key.data) > (ceiling if key.data is stdout else STREAM_CEILING):
                            raise MeasurementFailed('Sandbox output exceeded its bound.')
            try:
                status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise MeasurementFailed('Fixed sandbox deadline exceeded.') from error
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
                removed = subprocess.run([DOCKER, 'rm', '-f', name], capture_output=True, timeout=15)
                if removed.returncode and b'No such container' not in removed.stderr:
                    raise Unavailable(f'Operator must inspect the stopped job container {name}.')
    if feeder is not None:
        feeder.join(5)
    if problems or (feeder is not None and feeder.is_alive()):
        raise MeasurementFailed(problems[0] if problems else 'Could not finish delivering stdin.')
    if status in DOCKER_ENVIRONMENT_ERRORS:  # Docker or the candidate can exit this way; its stderr is never echoed.
        raise Unavailable(f'Fixed sandbox exited {status}.')
    return RunOutput(status, bytes(stdout), bytes(stderr))
