"""Build the checked-out shell and both UIs from one explicit dependency set, never from moving tags."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .maintenance import Docker, MaintenanceError

_IGNORE = {'.git', 'node_modules', 'dist', 'target', '__pycache__', '.vite'}
_INTERFACES = (('frontend', 'frontend'), ('static-inspection/workbench/ui', 'inspector-ui'))
_OUTPUTS = ('re-kit-workspace', 'frontend/index.html', 'inspector-ui/index.html')
# A long-lived desk may outlive a checkout update. Never stamp old commands with a new recipe.
_LOADED_RECIPE = hashlib.sha256(Path(__file__).read_bytes()).digest()


def _sources(directory: Path):
    if not directory.is_dir() or directory.is_symlink():
        raise MaintenanceError(f'Missing workspace source directory: {directory.name}')
    for base, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORE)
        for name in sorted(files):
            if name.startswith('.') or name.endswith(('.tsbuildinfo', '.pyc')):
                continue
            path = Path(base, name)
            if path.is_symlink() or not path.is_file():
                raise MaintenanceError(f'Unexpected workspace build input: {name}')
            yield path


def _copy(files, source: Path, target: Path, snapshot: dict[Path, bytes]) -> None:
    for path in files:
        dest = target / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(snapshot[path])


def _output_digest(out: Path) -> str:
    if out.is_symlink() or not out.is_dir():
        raise MaintenanceError('The workspace build output directory is missing or invalid')
    digest = hashlib.sha256()
    for path in sorted(out.rglob('*')):
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise MaintenanceError('The workspace build emitted a link or a special file')
        if stat.S_ISREG(mode):
            digest.update(path.relative_to(out).as_posix().encode() + b'\0')
            with path.open('rb') as stream:
                digest.update(hashlib.file_digest(stream, 'sha256').digest())
    for name in _OUTPUTS:
        path = out / name
        if not path.is_file() or not path.stat().st_size:
            raise MaintenanceError(f'The workspace build did not produce {name}')
    if not os.access(out / _OUTPUTS[0], os.X_OK):
        raise MaintenanceError('The workspace build did not produce an executable shell')
    return digest.hexdigest()


def _cached(entry: Path) -> Path:
    stamp = entry / 'sha256'
    out = entry / 'app'
    if (entry.is_symlink() or not entry.is_dir() or stamp.is_symlink() or not stamp.is_file()
            or stamp.stat().st_size != 64 or stamp.read_bytes() != _output_digest(out).encode()):
        raise MaintenanceError(f'Cached build {entry.name} failed its integrity check; do not launch it')
    return out


def build_workspace(docker: Docker, checkout: Path, base: Path, records: dict, cache: Path) -> Path:
    """Compile offline or reuse a completed build of these exact sources, trees and images."""
    try:
        images = {row['key']: row['image_id'] for row in records.values() if 'image_id' in row}
        compiler, node = images['compiler'], images['node']
        cargo = records['workspace-cargo']
        npm = {name: records[name] for _, name in _INTERFACES}
        identity = [compiler, node, cargo['lock_sha256'], cargo['tree_sha256'],
                    *((row['lock_sha256'], row['tree_sha256']) for row in npm.values())]
    except KeyError as exc:
        raise MaintenanceError('Incomplete dependency set for the workspace build') from exc
    rust = [checkout / 'Cargo.toml', *_sources(checkout / 'src'), *_sources(checkout / 'migrations')]
    uis = []
    for folder, name in _INTERFACES:
        source = checkout / folder
        uis.append((source, name, [p for p in _sources(source) if p != source / 'package-lock.json']))
    with Path(__file__).open('rb') as recipe:
        digest = hashlib.file_digest(recipe, 'sha256')
    if digest.digest() != _LOADED_RECIPE:
        raise MaintenanceError('Workspace build code changed on disk; restart the workspace before updating')
    digest.update(json.dumps(identity, separators=(',', ':')).encode())
    # The checkout can be edited while a build runs. Hash and compile the same bytes.
    snapshot = {}
    for path in [*rust, *(p for _, _, files in uis for p in files)]:
        digest.update(path.relative_to(checkout).as_posix().encode() + b'\0')
        snapshot[path] = path.read_bytes()
        digest.update(hashlib.sha256(snapshot[path]).digest())
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = cache.lstat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and
            (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700)):
        raise MaintenanceError('The workspace build cache must be a private, operator-owned directory')
    final = cache / digest.hexdigest()
    if os.path.lexists(final):
        return _cached(final)

    with tempfile.TemporaryDirectory(dir=cache) as scratch:
        temp = Path(scratch)
        entry = temp / 'result'
        entry.mkdir()
        out, csrc, target = entry / 'app', temp / 'rust', temp / 'target'
        for directory in (out, csrc, target):
            directory.mkdir()
        _copy(rust, checkout, csrc, snapshot)
        # Older installations only recorded the lock hash. Never substitute a different checkout lock.
        lock = base / cargo['lock_file'] if cargo.get('lock_file') else checkout / 'Cargo.lock'
        data = lock.read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(data).hexdigest() != cargo['lock_sha256']:
            raise MaintenanceError('The workspace Cargo lock does not match the selected dependencies')
        (csrc / 'Cargo.lock').write_bytes(data)
        vendor = base / cargo['asset'] if cargo.get('asset') else temp / 'empty-vendor'
        if not cargo.get('asset') and cargo.get('crates') == 0:
            vendor.mkdir()
        if not vendor.is_dir():
            raise MaintenanceError('The workspace Cargo vendor directory is missing')
        docker.run(compiler, ['sh', '-ec',
            '''cargo --config 'source.crates-io.replace-with="vendor"' --config 'source.vendor.directory="/vendor"' build --locked --offline --release --bin re-kit-workspace && install -m 0555 /target/release/re-kit-workspace /out/re-kit-workspace'''],
            mounts=((csrc, '/src', True), (vendor, '/vendor', True), (target, '/target', False), (out, '/out', False)),
            env={'HOME': '/tmp', 'CARGO_HOME': '/tmp/cargo', 'CARGO_TARGET_DIR': '/target', 'CARGO_BUILD_JOBS': '2'},
            workdir='/src', timeout=1800)
        for source, name, files in uis:
            dep = base / npm[name]['asset']
            work, dest = temp / name, out / name
            work.mkdir()
            dest.mkdir()
            _copy(files, source, work, snapshot)
            shutil.copyfile(dep / 'package-lock.json', work / 'package-lock.json')
            docker.run(node, ['sh', '-ec', 'cp -a /deps/node_modules .; npm run build; cp -a dist/. /out/'],
                mounts=((dep, '/deps', True), (work, '/work', False), (dest, '/out', False)),
                env={'HOME': '/tmp', 'npm_config_offline': 'true', 'npm_config_ignore_scripts': 'true'},
                workdir='/work', timeout=600)
        (entry / 'sha256').write_bytes(_output_digest(out).encode())
        try:
            entry.rename(final)
        except OSError as exc:
            # A launcher can finish the same build while an update holds its own transaction lock.
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            return _cached(final)
    return final / 'app'
