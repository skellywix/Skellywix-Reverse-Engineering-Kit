"""Stage the optional Neverball Windows rebuild release from public sources.

    python3 -B tools/prepare_neverball.py --root DATA --original NEVERBALL_ZIP [--dependencies-tgz ARCHIVE] [--output DIR]

Writes the release layout `build_desk/game_jobs.py` verifies on every run (source/, vendor/, dependencies.tgz, source.sha256,
original.zip) to DIR, by default DATA/neverball/release. The release is built in a private staging directory next to DIR,
checked with the same `game_jobs.verify_release` the desk uses, and renamed into place only if that passes. A refused attempt
leaves nothing behind.

By default the vendored crates are the ones the dependency update installed under DATA, packed into the canonical
byte-reproducible archive; no other file is needed. That archive must hash to a digest approved in
`build_desk/neverball_archives.py` (the portable one). `--dependencies-tgz` instead supplies an archive the install already
has, which must itself be one of the approved digests (the historical one keeps its original binding). Nothing else is accepted:
not a tree that merely holds the same files, not a freshly generated pin. The original Windows ZIP, the frozen lock and the
recipe must hash to the values in `game_jobs.PINS`. Nothing is downloaded and no program is run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import tomllib
import zlib
from dataclasses import replace
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT))
from build_desk import game_jobs, neverball_archives  # noqa: E402 - import needs the sys.path entry above
from build_desk.maintenance import Maintenance, MaintenanceError  # noqa: E402 - same sys.path entry as above

DEFAULT_STATE = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 're-kit'
SOURCE_DIR = 'examples/neverball'
SKIP_DIRS = frozenset({'target', '.git'})
TEXT_SUFFIXES = ('.rs', '.toml', '.lock', '.md', '.patch', '.dockerfile')
TEXT_NAMES = frozenset({'COPYING', '.gitignore'})
VENDOR_COMPONENT = 'neverball-desktop-lock'
RUNNER_KEY = 'neverball-runner'
REGISTRIES = ('registry+https://github.com/rust-lang/crates.io-index', 'sparse+https://index.crates.io/')
HUNK = re.compile(rb'@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')
ARCHIVE_LIMIT = 2 << 30
BLOCK = 65535


class PrepareError(RuntimeError):
    """An input is missing, is not the pinned file, or does not fit the release. Safe to show; nothing was installed."""


def _digest(path):
    return game_jobs.sha256_file(path)


def _safe(name):
    parts = name.split('/')
    if not name or any(part in ('', '.', '..') or '\\' in part or '\0' in part for part in parts):
        raise PrepareError('An archive or checksum entry has an unsafe name.')


def _files(base):
    """{relative POSIX path: path} for every regular file under `base`; links and special entries are refused."""
    found = {}
    for current, dirs, names in os.walk(base):
        for name in dirs:
            if Path(current, name).is_symlink():
                raise PrepareError(f'Unexpected link in {base.name}/: {name}')
        for name in names:
            path = Path(current, name)
            if path.is_symlink() or not path.is_file():
                raise PrepareError(f'Unexpected entry in {base.name}/: {name}')
            found[path.relative_to(base).as_posix()] = path
    return found


# ---------------------------------------------------------------- source and original

def stage_source(checkout, stage):
    """Copy the port's own text files, LF-normalised: the pins hash the committed LF bytes, not a CRLF working copy."""
    base = Path(checkout) / SOURCE_DIR
    if not base.is_dir() or base.is_symlink():
        raise PrepareError('This checkout has no examples/neverball source.')
    count = 0
    for current, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            path = Path(current, name)
            rel = path.relative_to(base).as_posix()
            if path.is_symlink() or not path.is_file():
                raise PrepareError(f'Unexpected source entry: {rel}')
            if name not in TEXT_NAMES and not name.lower().endswith(TEXT_SUFFIXES):
                raise PrepareError(f'Unexpected source file type: {rel}')
            target = stage / 'source' / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
            count += 1
    return count


def stage_original(original, stage, pins):
    original = Path(original)
    if not original.is_file():
        raise PrepareError(f'The original Windows release is not a file: {original}')
    got = _digest(original)
    if got != pins.original_zip_sha256:
        raise PrepareError(f'{original.name} is not the original Neverball 1.6.0 Windows release (SHA-256 {got}, expected '
                           f'{pins.original_zip_sha256}). Download the unmodified ZIP from https://neverball.org/download.')
    shutil.copyfile(original, stage / 'original.zip')


# ---------------------------------------------------------------- vendored dependencies

def stage_vendor_from_archive(archive, stage, approved):
    """A supplied archive is accepted only if its bytes hash to an approved digest; vendor/ is exactly its content. Returns it."""
    archive = Path(archive)
    if not archive.is_file():
        raise PrepareError(f'The dependency archive is not a file: {archive}')
    got = _digest(archive)
    if got not in approved:
        raise PrepareError(f'{archive.name} is not an approved dependency archive (SHA-256 {got}).')
    shutil.copyfile(archive, stage / 'dependencies.tgz')
    seen, total = set(), 0
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isreg() or not member.name.startswith('vendor/') or member.name in seen:
                    raise PrepareError('The dependency archive holds a non-file, foreign, or repeated member.')
                _safe(member.name)
                total += member.size
                if total > ARCHIVE_LIMIT:
                    raise PrepareError('The dependency archive exceeds its bound.')
                seen.add(member.name)
                target = stage / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open('xb') as out:
                    shutil.copyfileobj(src, out, 1 << 20)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise PrepareError(f'The dependency archive cannot be read: {exc}') from None
    if not seen:
        raise PrepareError('The dependency archive is empty.')
    return got


def _stored_gzip(source, target):
    """gzip `source` with stored (uncompressed) deflate blocks and an empty header: the same bytes on every machine and zlib."""
    crc = size = 0
    with source.open('rb') as src, target.open('xb') as out:
        out.write(b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff')
        block = src.read(BLOCK)
        while True:
            following = src.read(BLOCK)
            out.write(struct.pack('<BHH', 0 if following else 1, len(block), len(block) ^ 0xFFFF) + block)
            crc, size = zlib.crc32(block, crc), size + len(block)
            if not following:
                break
            block = following
        out.write(struct.pack('<II', crc, size & 0xFFFFFFFF))


def deterministic_archive(vendor, target):
    """Write `vendor` as vendor/<crate>/... in a byte-reproducible tgz: sorted members, mode 0644, no owner or time, stored gzip."""
    scratch = target.with_name(target.name + '.tar')
    try:
        with tarfile.open(scratch, 'w', format=tarfile.GNU_FORMAT) as tar:
            for rel, path in sorted(_files(vendor).items()):
                info = tarfile.TarInfo('vendor/' + rel)
                info.size, info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = path.stat().st_size, 0o644, 0, 0, 0, '', ''
                with path.open('rb') as stream:
                    tar.addfile(info, stream)
        _stored_gzip(scratch, target)
    finally:
        scratch.unlink(missing_ok=True)


def stage_vendor_from_crates(installed, stage, approved):
    """Pack the updater-installed crates canonically. The archive must hash to an approved digest, which is returned."""
    shutil.copytree(installed, stage / 'vendor', symlinks=False)
    deterministic_archive(stage / 'vendor', stage / 'dependencies.tgz')
    got = _digest(stage / 'dependencies.tgz')
    if got not in approved:
        raise PrepareError(
            f'A canonical archive of the installed crates has SHA-256 {got}, which is not an approved digest '
            f'({", ".join(approved)}). Run the dependency update so the installed crates are the approved set, or pass an '
            'approved archive with --dependencies-tgz. A maintainer must review any new digest and add it to '
            'build_desk/neverball_archives.py; this tool never accepts a different digest by itself.')
    return got


def check_vendor(vendor, lock_text):
    """Every vendored crate is intact (per-file checksums) and the set is exactly the crates.io packages Cargo.lock names."""
    expected = []
    for package in tomllib.loads(lock_text).get('package', []):
        source = package.get('source')
        if source is None:
            continue  # the port's own path crates are not vendored
        if source not in REGISTRIES or not game_jobs.HASH.fullmatch(str(package.get('checksum', ''))):
            raise PrepareError('Cargo.lock names a dependency that is not a checksummed crates.io package.')
        expected.append(package['checksum'])
    found = []
    for crate in sorted(vendor.iterdir()):
        marker = crate / '.cargo-checksum.json'
        if crate.is_symlink() or not crate.is_dir() or not marker.is_file():
            raise PrepareError(f'vendor/{crate.name} is not a vendored crate.')
        try:
            record = json.loads(marker.read_bytes())
        except ValueError:
            raise PrepareError(f'vendor/{crate.name} has an unreadable checksum record.') from None
        if not isinstance(record, dict) or not isinstance(record.get('files'), dict) or not isinstance(record.get('package'), str):
            raise PrepareError(f'vendor/{crate.name} has a malformed checksum record.')
        for rel, digest in record['files'].items():
            _safe(rel)
            path = crate / rel
            if path.is_symlink() or not path.is_file() or _digest(path) != digest:
                raise PrepareError(f'vendor/{crate.name}/{rel} does not match its crate checksum.')
        found.append(record['package'])
    if sorted(found) != sorted(expected):
        raise PrepareError('The vendored crates are not exactly the crates Cargo.lock names.')
    return len(found)


def check_patch(vendor, patch):
    """The pinned miniquad patch applies to the vendored crate (strict: no fuzz), as the runner's `patch --fuzz=0` will need."""
    crate = vendor / 'miniquad'
    if not crate.is_dir():
        raise PrepareError('vendor/miniquad is missing, so the pinned Windows patch cannot apply.')
    lines, index, target, hunks = patch.split(b'\n'), 0, None, 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(b'+++ b/'):
            target = line[6:].decode('utf-8')
            _safe(target)
        elif line.startswith(b'@@'):
            match = HUNK.fullmatch(line)
            if match is None or target is None:
                raise PrepareError('The pinned Windows patch is malformed.')
            old_count, new_count = int(match[1] or 1), int(match[2] or 1)
            old, new = [], 0
            while len(old) < old_count or new < new_count:
                index += 1
                if index >= len(lines):
                    raise PrepareError('The pinned Windows patch is truncated.')
                tag, body = lines[index][:1], lines[index][1:]
                if tag in (b' ', b''):
                    old.append(body)
                    new += 1
                elif tag == b'-':
                    old.append(body)
                elif tag == b'+':
                    new += 1
                else:
                    raise PrepareError('The pinned Windows patch is malformed.')
            path = crate / target
            if path.is_symlink() or not path.is_file():
                raise PrepareError(f'vendor/miniquad/{target} is missing, so the pinned Windows patch cannot apply.')
            data, span = path.read_bytes().split(b'\n'), len(old)
            if not any(data[k:k + span] == old for k in range(len(data) - span + 1)):
                raise PrepareError(f'The pinned Windows patch no longer applies to vendor/miniquad/{target}.')
            hunks += 1
        index += 1
    if not hunks:
        raise PrepareError('The pinned Windows patch holds no hunks.')
    return hunks


# ---------------------------------------------------------------- release

def _manifest(stage):
    lines = [f'{_digest(path)}  {top}/{rel}' for top in ('source', 'vendor') for rel, path in _files(stage / top).items()]
    (stage / 'source.sha256').write_bytes(('\n'.join(sorted(lines)) + '\n').encode('ascii'))


def _modes(stage):
    """Readable by the unprivileged compiler, writable by nobody else: what `verify_release` requires."""
    for current, dirs, names in os.walk(stage):
        os.chmod(current, 0o755)
        for name in names:
            os.chmod(Path(current, name), 0o644)


def _same_tree(left, right):
    """Number of files that differ between two directory trees (by relative path and SHA-256)."""
    a = {rel: _digest(path) for rel, path in _files(left).items()}
    b = {rel: _digest(path) for rel, path in _files(right).items()}
    return len(set(a.items()) ^ set(b.items()))


def prepare(checkout, output, original, *, archive=None, vendor=None, pins=game_jobs.PINS, approved=neverball_archives.APPROVED):
    """Stage and verify the release at `output`. `vendor` is the updater-installed crates (packed canonically); `archive` an
    approved archive supplied instead, with `vendor` then only compared for information.

    The release is bound to the one approved digest its dependencies.tgz hashes to (`pins` with that vendor digest). `approved`
    and `pins` are injectable for fixtures. Returns facts about the staged release. Raises PrepareError (nothing at `output`)
    when any input or check refuses.
    """
    checkout, output = Path(checkout), Path(output)
    if not output.is_absolute():
        raise PrepareError('The output directory must be an absolute path.')
    if os.path.lexists(output):
        raise PrepareError(f'{output} already exists. Move it aside first; nothing was changed.')
    if archive is None and vendor is None:
        raise PrepareError('There are no vendored crates. Run the dependency update first, or pass --dependencies-tgz.')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.neverball-release-', dir=output.parent))
    try:
        stage_original(original, stage, pins)
        sources = stage_source(checkout, stage)
        if archive is not None:
            digest, origin = stage_vendor_from_archive(archive, stage, approved), 'supplied archive'
        else:
            digest, origin = stage_vendor_from_crates(vendor, stage, approved), 'installed crates'
        lock = (stage / 'source/desktop/Cargo.lock').read_text(encoding='utf-8')
        crates = check_vendor(stage / 'vendor', lock)
        hunks = check_patch(stage / 'vendor', (stage / 'source/build/miniquad-i686.patch').read_bytes())
        agreement = None
        if archive is not None and vendor is not None:
            agreement = _same_tree(vendor, stage / 'vendor')
        _manifest(stage)
        _modes(stage)
        try:
            facts = game_jobs.verify_release(stage, replace(pins, vendor_tgz_sha256=digest))
        except (game_jobs.GameJobError, OSError) as exc:
            raise PrepareError(f'The staged release does not verify: {exc}') from None
        os.rename(stage, output)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {'release': str(output), 'manifest_sha256': facts['manifest_sha256'], 'source_files': sources, 'crates': crates,
            'patch_hunks': hunks, 'archive_sha256': digest, 'archive_source': origin, 'installed_crates_differ_in_files': agreement}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=DEFAULT_STATE, help='workspace data directory the dependency update installed into')
    parser.add_argument('--original', type=Path, required=True, help='the unmodified Neverball 1.6.0 Windows release ZIP')
    parser.add_argument('--dependencies-tgz', type=Path, help='an approved dependency archive to use instead of the installed crates (optional)')
    parser.add_argument('--output', type=Path, help='release directory to create (default: ROOT/neverball/release)')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('Prepare the release on the Linux host that runs the workspace.')
    root = args.root.absolute()
    maintenance = Maintenance(root, CHECKOUT, upstream=None)
    try:
        facts = prepare(CHECKOUT, (args.output or root / 'neverball' / 'release').absolute(), args.original.absolute(),
                        archive=args.dependencies_tgz.absolute() if args.dependencies_tgz else None,
                        vendor=maintenance.asset_path(VENDOR_COMPONENT))
    except PrepareError as exc:
        print(f'Not prepared: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(facts, indent=2, sort_keys=True))
    try:
        maintenance.image_for(RUNNER_KEY)
    except MaintenanceError:
        print('Note: the Windows compiler image is not installed yet. Run tools/update_workspace.py bootstrap (or update) first.',
              file=sys.stderr)
    print(f'Start the workspace with: python3 -B tools/workspace.py --root {root} --game-release {facts["release"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
