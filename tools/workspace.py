"""Start the local workspace from a checkout. No host Rust, Node, database, or model account required."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT))
from build_desk.maintenance import Maintenance, MaintenanceError

INSPECT = CHECKOUT / 'static-inspection' / 'workbench'
PROFILES = ('line-count', 'status-handler')
DEFAULT_STATE = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 're-kit'


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f'Use an operator-owned directory with mode 0700: {path}')
    return path.resolve()


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def start_process(stack, argv, **kwargs):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **kwargs)
    stack.callback(stop_process, process)
    return process


def worker(stack, image, mounts, env, argv, memory, entry):
    args = ['docker', 'run', '--detach', '--rm', '--init', '--pull=never', '--network=none', '--read-only',
            '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--pids-limit=96', '--cpus=2', f'--memory={memory}', f'--memory-swap={memory}', '--log-driver=local',
            '--log-opt=max-size=1m', '--log-opt=max-file=1', '--log-opt=compress=false',
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=32m']
    for path, dst, writable in mounts:
        if ',' in str(path) or not path.exists():
            raise ValueError(f'Invalid or missing mount: {path}')
        args.extend(['--mount', f'type=bind,src={path},dst={dst}' + ('' if writable else ',readonly')])
    for key, value in env.items():
        args.extend(['--env', f'{key}={value}'])
    cid = subprocess.check_output([*args, '--entrypoint', entry, image, *argv], text=True).strip()
    if len(cid) != 64 or any(c not in '0123456789abcdef' for c in cid):
        raise RuntimeError('Docker did not return a container ID.')
    stack.callback(subprocess.run, ['docker', 'stop', '-t', '3', cid], stdout=subprocess.DEVNULL, check=False)
    return cid


def wait_for(check, children, label, seconds=25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if any(p.poll() is not None for p in children):
            raise RuntimeError(f'A workspace process exited while waiting for {label}.')
        if check():
            return
        time.sleep(0.1)
    raise RuntimeError(f'Timed out waiting for {label}.')


def convert_mounts(root):
    """Expose per-profile inputs and results read-only; only the comparison queue is writable."""
    data = private_dir(root / 'convert')
    setup = [sys.executable, '-EsB', '-m', 'portsetup']
    for command in ('install', 'check'):
        subprocess.run([*setup, command, '--root', str(data)], cwd=CHECKOUT / 'source-port', check=True)
    mounts = []
    for profile in PROFILES:
        registry = data / 'registry' / profile
        gate = registry / 'gate'
        if not gate.is_dir():
            raise ValueError(f'{profile} uses the legacy verifier layout. Use a separate --root; the original is unchanged.')
        for path, dst, writable in (
            (registry / 'public', f'/ports/registry/{profile}/public', False),
            (gate, f'/ports/gate/{profile}', False),
            (data / 'approvals' / profile, f'/ports/gate/{profile}/approval', False),
            (data / 'queue' / profile, f'/ports/queue/{profile}', True),
            (data / 'results' / profile, f'/ports/results/{profile}', False),
        ):
            if not path.is_dir() or path.resolve(strict=True) != path:
                raise ValueError(f'Expected a real profile directory: {path}')
            mounts.append((path, dst, writable))
    return mounts


def serve(args):
    import fcntl
    os.umask(0o077)
    root = private_dir(args.root.absolute())
    if root.is_relative_to(CHECKOUT) or CHECKOUT.is_relative_to(root) or ',' in str(root):
        raise ValueError('Keep the operator data directory separate from the checkout, in a path without commas.')
    activate = shutil.which('systemd-socket-activate')
    proxy = next((p for p in ('/usr/lib/systemd/systemd-socket-proxyd', '/lib/systemd/systemd-socket-proxyd') if os.access(p, os.X_OK)), None)
    if not activate or not proxy:
        raise RuntimeError('Install systemd-socket-activate and systemd-socket-proxyd on the Linux host.')
    m = Maintenance(root, CHECKOUT)
    authority = f'127.0.0.1:{args.port}'
    with contextlib.ExitStack() as stack:
        lock = stack.enter_context((root / 'workspace.lock').open('a'))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('This workspace is already open.') from exc
        if not (root / 'dependencies' / 'active.json').is_file():
            print('Resolving upstream dependencies. The first start needs network access…', flush=True)
            result = m.bootstrap(wait=True)
            if result['state'] != 'updated':
                raise RuntimeError(result['message'])
        built = m.installed_application()
        ipc = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='re-kit-ipc-')))
        web_ipc = private_dir(ipc / 'web')
        insp_ipc = private_dir(ipc / 'inspect')
        desk_ipc = private_dir(ipc / 'build')
        desk_argv = [sys.executable, '-B', '-m', 'build_desk.desk', str(root), '--port', str(args.port), '--socket-directory', str(desk_ipc)]
        if args.game_release:
            desk_argv += ['--game-release', str(args.game_release.resolve(strict=True))]
        desk = start_process(stack, desk_argv, cwd=CHECKOUT, env={**os.environ, 'RE_KIT_WORKSPACE': '1'})
        wait_for(lambda: (desk_ipc / 'desk.sock').is_socket(), [desk], 'the Build service')
        inspector_files = [(INSPECT / name, '/app/' + name, False) for name in ('server.py', 'protocol.py', 'case_state.py', 'evidence.py')]
        worker(stack, m.image_for('inspector-worker'), [*inspector_files, (insp_ipc, '/ipc', True), (built / 'inspector-ui', '/desk', False)], {},
               ['-IB', '/app/server.py', '--ipc', '/ipc', '--web', '/desk', '--paired'], '160m', 'python3')
        wait_for(lambda: all((insp_ipc / name).is_socket() for name in ('desk.sock', 'agent.sock')), [desk], 'the passive inspector')
        mounts = [(web_ipc, '/listen', True), (desk_ipc, '/build', False), (insp_ipc, '/inspect', False),
                  (built / 're-kit-workspace', '/kit', False), (built / 'frontend', '/web', False),
                  (private_dir(root / 'workspace-db'), '/db', True)]
        mounts.extend(convert_mounts(root))
        env = {'RE_KIT_WEB_SOCKET': '/listen/workspace.sock', 'RE_KIT_BUILD_SOCKET': '/build/desk.sock', 'RE_KIT_LISTEN_ADDR': authority,
               'RE_KIT_DATABASE_PATH': '/db/workspace.sqlite', 'RE_KIT_STATIC_DIR': '/web', 'RE_KIT_SOURCE_PORT_ROOT': '/ports',
               'RE_KIT_INSPECTOR_DESK_SOCKET': '/inspect/desk.sock', 'RE_KIT_INSPECTOR_AGENT_SOCKET': '/inspect/agent.sock'}
        if args.fourmilab_socket:
            socket = args.fourmilab_socket.resolve(strict=True)
            if not socket.is_socket():
                raise ValueError('Expected the operator-installed Fourmilab desk socket.')
            mounts.append((socket, '/fourmilab/desk.sock', False))
            env['RE_KIT_FOURMILAB_SOCKET'] = '/fourmilab/desk.sock'
        cid = worker(stack, m.image_for('python'), mounts, env, [], '384m', '/kit')
        watcher = start_process(stack, ['docker', 'wait', cid], stdout=subprocess.DEVNULL)
        websock = web_ipc / 'workspace.sock'
        wait_for(websock.is_socket, [desk, watcher], 'the isolated workspace shell')
        forward = start_process(stack, [activate, '-l', authority, proxy, str(websock)])
        children = [desk, watcher, forward]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def reachable():
            try:
                with opener.open(f'http://{authority}/project/profiles', timeout=2) as response:
                    return json.load(response)['workspace'] is True
            except OSError:
                return False
        wait_for(reachable, children, 'the front page')
        print(f'Workspace ready: http://{authority}/\nPress Ctrl-C to stop. Export open Inspect evidence before stopping.', flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        raise RuntimeError('A required workspace process exited.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_STATE, help='private state directory outside the checkout')
    parser.add_argument('--port', type=int, default=48784, help='loopback HTTP port')
    parser.add_argument('--game-release', type=Path, help='optional independently provisioned Neverball release directory')
    parser.add_argument('--fourmilab-socket', type=Path, help='optional operator-provisioned Fourmilab desk socket')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.getuid() == 0 or not 1024 <= args.port <= 65535:
        parser.error('Use a non-root Linux Docker account and a local port in 1024–65535.')
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        serve(args)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError, MaintenanceError, subprocess.SubprocessError) as exc:
        print(f'Workspace not running: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
