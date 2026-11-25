"""Serialized, journaled build of the fixed Neverball 1.6.0 Rust port for Windows.

Python 3.13 standard library, Linux only. This module never executes uploaded or user content and never runs
the produced PE on the host. The only executable profile is the pinned offline compiler image below, started
with fixed arguments inside gVisor; nothing in a request chooses a command, image, mount, or path.

The build output is a *candidate*. It is released only after `package()` finds a private, host-owned
qualification receipt (see RECEIPT FORMAT) that binds this exact run, PE, ZIP and journal entry and whose
independent Windows observations of the original and candidate agree. Nothing in this class can write that
receipt; the web layer must not either. A receipt is proof that the *host-owned QA process* said so, not that
this module observed Windows run. See the limits in `qualification_contract()`.

RELEASE LAYOUT (admin-staged, immutable; verified on every start and again before the compiler runs):
    release/source/              GPL Rust port: Cargo.toml, desktop/{Cargo.toml,Cargo.lock,src/}, build/windows-i686.Dockerfile,
                                 COPYING, NOTICE.md. No symlinks, no C/C++ sources, no binaries, no build.rs, no target/.
    release/vendor/              Vendored crates (mounted read-only at /vendor).
    release/dependencies.tgz     Pinned archive of vendor/ (its file hashes must equal vendor/ exactly).
    release/source.sha256        sha256sum-style lines "<64 hex>  source/<path>" or "vendor/<path>": the complete closure of
                                 regular files under source/ and vendor/. Extra, missing, or altered files are refused.
    release/original.zip         The exact Windows release (pinned SHA-256).
    Nothing else may be in release/. Entries must be owned by root or this user and not group/other writable.

JOURNAL: root/<run>/journal.jsonl, one hash-chained fsynced line per transition:
    queued -> build -> package -> waiting_windows -> proven;   any non-terminal state -> failed.
    Terminal: failed, proven. Pins (input, toolchain, actual PE and ZIP hashes) are recorded in event data.

RECEIPT FORMAT: root/<run>/qualification.json (see `qualification_contract()` for the machine-readable form).
"""
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import selectors
import stat
import struct
import subprocess
import tarfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DOCKER = '/usr/bin/docker'
PROMPT = 'Rebuild Neverball 1.6.0 in Rust for Windows.'
PROMPT_SHA256 = '8cbd638edfa5878eab29334b5ecb23e52c10d0f929781810da0daad49dced7ac'
ORIGINAL_ZIP_SHA256 = '865391cc041208e526401a8eb1f14fba44323f17d7e818c5e65e82393ff747fb'
PK3_SHA256 = 'b2eddfbe05443e36719541639cb6246c5dd81260bce67a053d0d2c0ad34bc58c'
PK3_NAME = 'data/data-1.6.0.pk3'

RUN = re.compile(r'[0-9a-f]{32}')
HASH = re.compile(r'[0-9a-f]{64}')
MANIFEST_LINE = re.compile(r'([0-9a-f]{64})  (.+)')
EVIDENCE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')
GENESIS = '0' * 64
RECEIPT = 'qualification.json'
EVIDENCE_DIR = 'evidence'
CANDIDATE = 'candidate.zip'
ARTIFACT = 'neverball.exe'  # private PE bytes on disk; mode 0400; never executed here
BUILD_LOG = 'build.log'
ZIP_STAMP = (2026, 1, 1, 0, 0, 0)

NONTERMINAL = ('queued', 'build', 'package', 'waiting_windows')
BUSY_STATES = ('queued', 'build', 'package')
TRANSITIONS = {None: ('queued',), 'queued': ('build', 'failed'), 'build': ('package', 'failed'),
               'package': ('waiting_windows', 'failed'), 'waiting_windows': ('proven', 'failed')}

MAX_PE = 64 << 20
MIN_PE = 64 << 10
MAX_STDERR = 1 << 20
MAX_JOURNAL = 4 << 20
MAX_RECEIPT = 1 << 20
MAX_EVIDENCE = 8 << 20
MAX_MEMBER = 1 << 30
MAX_ZIP_TOTAL = 2 << 30
BUILD_DEADLINE = 1800
FUTURE_SLACK_NS = 120 * 10**9

SOURCE_REQUIRED = ('source/Cargo.toml', 'source/desktop/Cargo.toml', 'source/desktop/Cargo.lock',
                   'source/desktop/src/main.rs', 'source/build/windows-i686.Dockerfile',
                   'source/build/miniquad-i686.patch', 'source/COPYING', 'source/NOTICE.md')
FORBIDDEN_SOURCE_SUFFIX = ('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.m', '.mm', '.s', '.asm', '.exe', '.dll',
                           '.so', '.dylib', '.a', '.lib', '.o', '.obj', '.sys', '.bat', '.cmd', '.msi', '.com', '.scr')
FORBIDDEN_ZIP_SUFFIX = FORBIDDEN_SOURCE_SUFFIX
NOTICE_TEXT_SUFFIX = ('', '.txt', '.md', '.html', '.htm', '.rst')
NOTICE_NAME = re.compile(r'(?i)(licen[sc]e|copying|notice|authors|readme|changes|credits|thanks|copyright)')
NOTICE_LICENSE = re.compile(r'(?i)(licen[sc]e|copying)')
WEAK_OUTCOMES = {'', 'ok', 'pass', 'passed', 'true', 'false', 'success', 'succeeded', 'none', 'null', 'n/a', 'na',
                 'unknown', 'yes', 'no', 'matched', 'match', 'same', 'equal'}


FRESH_SLACK_NS = 50_000_000  # file mtimes come from a coarse kernel clock


def _is_notice(low, base):
    """A text file of the original release that carries a notice, license or credit (never game data)."""
    if low.startswith('data/') or os.path.splitext(base)[1] not in NOTICE_TEXT_SUFFIX:
        return False
    return bool(NOTICE_NAME.search(base)) or low.startswith(('doc/', 'docs/', 'legal/'))


class GameJobError(RuntimeError):
    """A pinned input, execution boundary, journal, or contract refused this operation."""


class Busy(GameJobError):
    """Another attempt is queued, building, or packaging."""


class ReleaseError(GameJobError):
    """The staged release is not exactly the pinned source closure."""


class JournalError(GameJobError):
    """The run journal is missing structure or its hash chain does not verify."""


class Unqualified(GameJobError):
    """No acceptable qualification receipt exists yet. The run stays in waiting_windows."""


class Rejected(GameJobError):
    """A well-formed, fresh receipt records that the candidate does not match the original."""


class Cancelled(GameJobError):
    """The coordinator was stopped during an attempt."""


@dataclass(frozen=True)
class Pins:
    """Fixed inputs. Values come from desktop-compiler.json, the staged recipe, and the original release."""
    prompt: str = PROMPT
    prompt_sha256: str = PROMPT_SHA256
    image: str = 'sha256:93aac6b24e9536cb987fa1c454b71db35bc423abcb1ea087e19a2a2d65e09b28'
    vendor_tgz_sha256: str = '88a960b6c75d5b303ac2b636b77681cfd1e4f4782434c5a498c1c2c9b5ce8b84'
    lock_sha256: str = '532b4f2870d4b3aa896711bf767d50fb998899adc9c9c7d97251534751b50ac7'
    recipe_sha256: str = '2c197c6457314572378fee50febe1552cc85cd7ad36144e34645f45a40b3af6f'
    original_zip_sha256: str = ORIGINAL_ZIP_SHA256
    pk3_sha256: str = PK3_SHA256


PINS = Pins()


@dataclass(frozen=True)
class Case:
    """One required, named, fresh Windows observation of both the original and the candidate."""
    name: str
    kind: str  # 'discrete': outcomes must be equal. 'continuous': max |original - candidate| <= tolerance.
    tolerance: float = 0.0
    samples: int = 0


REQUIRED = (
    Case('startup_menu', 'discrete'), Case('level_load', 'discrete'), Case('coin_collection', 'discrete'),
    Case('goal_completion', 'discrete'), Case('timeout_failure', 'discrete'), Case('fall_out_failure', 'discrete'),
    Case('recorded_replay', 'discrete'), Case('level_catalog', 'discrete'), Case('pause_resume', 'discrete'),
    Case('challenge_progress', 'discrete'), Case('score_after_relaunch', 'discrete'), Case('audio_output', 'discrete'),
    Case('ball_motion', 'continuous', 0.005, 60), Case('camera_motion', 'continuous', 0.01, 60),
)


def wire(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def clip(text, limit=900):
    text = ''.join(c if c == '\n' or 32 <= ord(c) < 127 or ord(c) > 159 else '?' for c in str(text))
    return text if len(text) <= limit else text[:limit - 3] + '...'


def qualification_contract():
    """Machine-readable receipt contract for the (separate) QA process and for the parent's status page."""
    return {
        'version': 1, 'receipt': RECEIPT, 'evidence_dir': EVIDENCE_DIR,
        'top': ['version', 'run', 'reference_sha256', 'artifact_sha256', 'zip_sha256', 'binding', 'checks'],
        'binding': 'status()["candidate"]["binding"]: hash of the waiting_windows journal entry; exists only after packaging',
        'reference_sha256': ORIGINAL_ZIP_SHA256,
        'side': {'method': 'windows_run', 'subject_sha256': 'original: sha256 of original neverball.exe; candidate: artifact_sha256',
                 'evidence': 'file name in evidence_dir; nonempty; 1..8 MiB; mtime after packaging; must contain the run id, '
                             'the subject hash and the binding as ASCII', 'evidence_sha256': 'sha256 of that file',
                 'outcome': 'discrete: concrete non-boolean observed value (string 1..200 chars or integer), not a bare verdict',
                 'values': 'continuous: equal-length finite number lists, at least `samples` long, original not constant'},
        'comparison': {'metric': 'max_abs_error', 'tolerance': 'must equal the case tolerance', 'observed': 'recomputed by host',
                       'accepted': True},
        'checks': [{'name': c.name, 'kind': c.kind, **({'tolerance': c.tolerance, 'samples': c.samples} if c.samples else {})}
                   for c in REQUIRED],
        'rules': 'Exactly the named checks, no duplicates or extras; evidence names and hashes unique across all sides; '
                 'receipt and evidence owned by the service user or root, not group/other writable.',
        'limits': 'The host verifies shape, bindings, freshness and agreement. It cannot prove the QA process truly ran '
                  'Windows; the receipt directory must be writable only by that process and never by a web request.',
    }


# ---------------------------------------------------------------- filesystem trust

def _part(name):
    if (not name or name in ('.', '..') or len(name) > 255 or '/' in name or '\\' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ReleaseError('A release member has an unsafe name.')
    try:
        name.encode('utf-8')
    except UnicodeEncodeError:
        raise ReleaseError('A release member has an undecodable name.') from None


def _safe_rel(path):
    for part in path.split('/'):
        _part(part)


def _trusted(st, *, directory=False, what='A release entry'):
    if st.st_uid not in (0, os.getuid()):
        raise ReleaseError(f'{what} has an untrusted owner.')
    if st.st_mode & 0o022:
        raise ReleaseError(f'{what} is group or world writable.')
    need = 0o005 if directory else 0o004
    if st.st_mode & need != need:
        raise ReleaseError(f'{what} is not readable by the unprivileged compiler.')


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GameJobError('The game job root must be a private directory owned by this user.')


def _walk(base, prefix):
    found = {}
    stack = [(base, prefix)]
    while stack:
        directory, rel = stack.pop()
        _trusted(os.lstat(directory), directory=True, what='A release directory')
        with os.scandir(directory) as entries:
            for entry in entries:
                _part(entry.name)
                item = f'{rel}/{entry.name}'
                if entry.is_symlink():
                    raise ReleaseError('The release contains a symlink.')
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, item))
                elif entry.is_file(follow_symlinks=False):
                    _trusted(entry.stat(follow_symlinks=False))
                    found[item] = entry.path
                else:
                    raise ReleaseError('The release contains a non-regular member.')
    return found


def _parse_manifest(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ReleaseError('source.sha256 is not UTF-8.') from None
    if not text.endswith('\n') or '\r' in text:
        raise ReleaseError('source.sha256 must be LF-terminated.')
    entries = {}
    for line in text[:-1].split('\n'):
        match = MANIFEST_LINE.fullmatch(line)
        if not match:
            raise ReleaseError('source.sha256 has a malformed line.')
        digest, path = match.groups()
        if not path.startswith(('source/', 'vendor/')):
            raise ReleaseError('source.sha256 names a path outside source/ and vendor/.')
        _safe_rel(path)
        if path in entries:
            raise ReleaseError('source.sha256 repeats a path.')
        entries[path] = digest
    if not entries:
        raise ReleaseError('source.sha256 is empty.')
    return entries


def _zip_names(infos):
    names = []
    for info in infos:
        name = info.filename
        if name.endswith('/'):
            continue
        if name.startswith('/') or '\\' in name or re.match(r'[A-Za-z]:', name):
            raise ReleaseError('A ZIP member has an unsafe name.')
        _safe_rel(name)
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise ReleaseError('A ZIP member is a symlink.')
        if info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ReleaseError('A ZIP member is encrypted or uses an unsupported method.')
        if info.file_size > MAX_MEMBER:
            raise ReleaseError('A ZIP member is too large.')
        names.append(name)
    if len(set(names)) != len(names):
        raise ReleaseError('The original ZIP repeats a member.')
    return names


def _strip(names):
    tops = {n.split('/', 1)[0] for n in names}
    if len(tops) == 1 and all('/' in n for n in names):
        top = tops.pop()
        return lambda n: n[len(top) + 1:]
    return lambda n: n


def analyse_original(path, pins=PINS):
    """Identify the exact members this job may carry from the original release. Returns hashes, not content."""
    if sha256_file(path) != pins.original_zip_sha256:
        raise ReleaseError('original.zip differs from its pinned SHA-256.')
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = _zip_names(infos)
        if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
            raise ReleaseError('The original ZIP expands beyond the bound.')
        rel = _strip(names)
        exe_hashes, exe, pk3, notices = {}, [], [], []
        for name in names:
            r = rel(name)
            low = r.lower()
            base = low.rsplit('/', 1)[-1]
            if low.endswith('.exe'):
                with archive.open(name) as stream:
                    exe_hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
                if base == 'neverball.exe':
                    exe.append(name)
            elif low == PK3_NAME or low.endswith('/' + PK3_NAME):
                pk3.append(name)
            elif _is_notice(low, base):
                notices.append((r, name))
        if len(exe) != 1 or len(pk3) != 1:
            raise ReleaseError('The original ZIP must hold exactly one neverball.exe and one data-1.6.0.pk3.')
        with archive.open(pk3[0]) as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != pins.pk3_sha256:
                raise ReleaseError('data-1.6.0.pk3 differs from its pinned SHA-256.')
        if not any(NOTICE_LICENSE.search(r.rsplit('/', 1)[-1]) for r, _ in notices):
            raise ReleaseError('The original ZIP holds no license notice.')
        if len(notices) > 64 or any(archive.getinfo(n).file_size > 4 << 20 for _, n in notices):
            raise ReleaseError('The notice set is outside its bound.')
    return {'exe_sha256': exe_hashes[exe[0]], 'exe_hashes': sorted(set(exe_hashes.values())), 'pk3': pk3[0],
            'notices': sorted(notices)}


def verify_release(release, pins=PINS):
    """Prove `release` is exactly the pinned immutable closure; returns facts needed to build. Raises ReleaseError."""
    release = Path(release)
    if not release.is_absolute() or release.is_symlink() or not release.is_dir():
        raise ReleaseError('The release must be an absolute directory, not a symlink.')
    _trusted(release.lstat(), directory=True, what='The release directory')
    if sorted(p.name for p in release.iterdir()) != ['dependencies.tgz', 'original.zip', 'source', 'source.sha256', 'vendor']:
        raise ReleaseError('The release must hold exactly source/, vendor/, dependencies.tgz, source.sha256 and original.zip.')
    for name in ('dependencies.tgz', 'original.zip', 'source.sha256'):
        item = release / name
        if item.is_symlink() or not item.is_file():
            raise ReleaseError(f'{name} is not a regular file.')
        _trusted(item.lstat(), what=name)
    for name in ('source', 'vendor'):
        if (release / name).is_symlink() or not (release / name).is_dir():
            raise ReleaseError(f'{name}/ is not a directory.')
    if sha256_file(release / 'dependencies.tgz') != pins.vendor_tgz_sha256:
        raise ReleaseError('dependencies.tgz differs from its pinned SHA-256.')
    manifest_bytes = (release / 'source.sha256').read_bytes()
    manifest = _parse_manifest(manifest_bytes)
    actual = {**_walk(release / 'source', 'source'), **_walk(release / 'vendor', 'vendor')}
    if set(actual) != set(manifest):
        raise ReleaseError('The release has extra or missing members beyond source.sha256.')
    for path, location in actual.items():
        if sha256_file(location) != manifest[path]:
            raise ReleaseError(f'{path} differs from source.sha256.')
    for path in SOURCE_REQUIRED:
        if path not in manifest:
            raise ReleaseError(f'{path} is missing from the source closure.')
    if manifest['source/desktop/Cargo.lock'] != pins.lock_sha256:
        raise ReleaseError('The frozen Cargo.lock differs from its pin.')
    if manifest['source/build/windows-i686.Dockerfile'] != pins.recipe_sha256:
        raise ReleaseError('The compiler recipe differs from its pin.')
    for path in manifest:
        if path.startswith('source/'):
            parts = path.split('/')
            name = parts[-1].lower()
            if (name.endswith(FORBIDDEN_SOURCE_SUFFIX) or name == 'build.rs'
                    or any(p in ('target', '.git') for p in parts[:-1])):
                raise ReleaseError('The source tree holds a forbidden member (C code, binary, build script or target/).')
    vendor = {p: h for p, h in manifest.items() if p.startswith('vendor/')}
    packed, total = {}, 0
    with tarfile.open(release / 'dependencies.tgz', 'r:gz') as tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isreg() or not member.name.startswith('vendor/') or member.name in packed:
                raise ReleaseError('dependencies.tgz holds a non-file, foreign, or repeated member.')
            _safe_rel(member.name)
            total += member.size
            if total > 2 << 30 or len(packed) > 400000:
                raise ReleaseError('dependencies.tgz exceeds its bound.')
            with tar.extractfile(member) as stream:
                packed[member.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if packed != vendor:
        raise ReleaseError('dependencies.tgz does not match vendor/.')
    original = analyse_original(release / 'original.zip', pins)
    return {'release': str(release), 'manifest_sha256': sha256_bytes(manifest_bytes), 'source': sorted(
            p for p in manifest if p.startswith('source/')), 'original': original}


# ---------------------------------------------------------------- PE

def check_pe(data):
    """Structural proof that bytes are a 32-bit x86 PE32 GUI executable (not DLL) with a coherent entry point."""
    n = len(data)
    if not MIN_PE <= n <= MAX_PE or data[:2] != b'MZ':
        raise GameJobError('Compiler output is not a PE program of plausible size.')
    (lfanew,) = struct.unpack_from('<I', data, 0x3C)
    if lfanew < 0x40 or lfanew > n - 24 or data[lfanew:lfanew + 4] != b'PE\0\0':
        raise GameJobError('Compiler output has no PE header.')
    machine, count, _stamp, _symbols, _nsyms, opt_size, flags = struct.unpack_from('<HHIIIHH', data, lfanew + 4)
    opt = lfanew + 24
    if machine != 0x14C or not flags & 0x0002 or flags & 0x2000 or not flags & 0x0100:
        raise GameJobError('Compiler output is not an i386 executable image.')
    if opt_size < 96 or opt + opt_size > n or struct.unpack_from('<H', data, opt)[0] != 0x10B:
        raise GameJobError('Compiler output is not PE32.')
    entry, = struct.unpack_from('<I', data, opt + 16)
    image, headers = struct.unpack_from('<II', data, opt + 56)
    subsystem, = struct.unpack_from('<H', data, opt + 68)
    if subsystem != 2:
        raise GameJobError('Compiler output is not a Windows GUI program.')
    table = opt + opt_size
    if not 1 <= count <= 96 or table + count * 40 > n or not entry or image < headers:
        raise GameJobError('Compiler output has an invalid section table.')
    in_code = False
    for index in range(count):
        _name, vsize, va, raw_size, raw_ptr = struct.unpack_from('<8sIIII', data, table + index * 40)
        (chars,) = struct.unpack_from('<I', data, table + index * 40 + 36)
        if raw_ptr + raw_size > n:
            raise GameJobError('Compiler output has a section beyond the file.')
        if chars & 0x20000000 and va <= entry < va + max(vsize, raw_size):
            in_code = True
    if not in_code:
        raise GameJobError('The PE entry point is not inside an executable section.')
    return {'machine': 'i386', 'format': 'PE32', 'subsystem': 'windows-gui', 'sections': count, 'bytes': n}


# ---------------------------------------------------------------- candidate ZIP

def _info(name, mode):
    info = zipfile.ZipInfo(name, ZIP_STAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _copy(archive, name, stream, mode=0o644):
    with archive.open(_info(name, mode), 'w', force_zip64=True) as out:
        while block := stream.read(1 << 20):
            out.write(block)


def build_candidate(target, release, facts, exe_path, provenance, pins=PINS):
    """Write the private candidate ZIP exclusively at `target` (must not exist)."""
    release = Path(release)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w+b') as handle, zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        with exe_path.open('rb') as stream:
            _copy(out, 'neverball.exe', stream, 0o755)
        with zipfile.ZipFile(release / 'original.zip') as original:
            with original.open(facts['original']['pk3']) as stream:
                digest = hashlib.sha256()
                with out.open(_info(PK3_NAME, 0o644), 'w', force_zip64=True) as dest:
                    while block := stream.read(1 << 20):
                        digest.update(block)
                        dest.write(block)
            if digest.hexdigest() != pins.pk3_sha256:
                raise ReleaseError('data-1.6.0.pk3 changed while packaging.')
            for rel_path, member in facts['original']['notices']:
                with original.open(member) as stream:
                    _copy(out, 'notices/' + rel_path, stream)
        with (release / 'dependencies.tgz').open('rb') as stream:
            _copy(out, 'build/dependencies.tgz', stream)
        out.writestr(_info('provenance.json', 0o644), wire(provenance) + '\n')
        for path in facts['source']:
            with (release / path).open('rb') as stream:
                _copy(out, path, stream, 0o644)


def verify_candidate_zip(path, artifact_sha256, pins=PINS):
    """Independently re-read a candidate ZIP and check its required members and hashes."""
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise GameJobError('The candidate ZIP failed its integrity check.')
        names = [i.filename for i in archive.infolist()]
        if len(set(names)) != len(names):
            raise GameJobError('The candidate ZIP repeats a member.')
        lowered = [n.lower() for n in names]
        if [n for n in lowered if n.endswith('.exe')] != ['neverball.exe'] or any(
                n.endswith(FORBIDDEN_ZIP_SUFFIX) for n in lowered if n != 'neverball.exe'):
            raise GameJobError('The candidate ZIP carries an unexpected executable or C member.')
        required = {'neverball.exe': artifact_sha256, PK3_NAME: pins.pk3_sha256,
                    'build/dependencies.tgz': pins.vendor_tgz_sha256,
                    'source/desktop/Cargo.lock': pins.lock_sha256,
                    'source/build/windows-i686.Dockerfile': pins.recipe_sha256}
        for name, want in required.items():
            if name not in names:
                raise GameJobError(f'The candidate ZIP lacks {name}.')
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != want:
                    raise GameJobError(f'{name} in the candidate ZIP has the wrong hash.')
        for need in ('provenance.json', 'source/Cargo.toml', 'source/COPYING'):
            if need not in names:
                raise GameJobError(f'The candidate ZIP lacks {need}.')
        if not any(n.startswith('notices/') for n in names):
            raise GameJobError('The candidate ZIP carries no original notices.')
        if not any(n.startswith('source/desktop/src/') for n in names):
            raise GameJobError('The candidate ZIP carries no port sources.')


# ---------------------------------------------------------------- JSON and receipt inputs

def _strict_json(data):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate key')
        return dict(items)

    def constant(name):
        raise ValueError('non-finite number')
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def _private_read(path, limit, *, newer_than_ns=None, what='file'):
    """Read a regular, host-owned, not group/other-writable file without following symlinks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise Unqualified(f'{what} is missing or not a regular file.') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022
                or not 1 <= info.st_size <= limit):
            raise Unqualified(f'{what} is empty, oversized, or not owned and protected by the host.')
        if newer_than_ns is not None and not newer_than_ns - FRESH_SLACK_NS <= info.st_mtime_ns <= time.time_ns() + FUTURE_SLACK_NS:
            raise Unqualified(f'{what} was not recorded after this candidate was packaged.')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return stream.read(limit + 1)
    finally:
        os.close(fd)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _expect(cond, message):
    if not cond:
        raise Unqualified(message)


class GameJobs:
    """One fixed job type, one attempt at a time, durable and honest after a restart."""

    docker = DOCKER

    def __init__(self, root, release, *, pins=PINS):
        root = Path(root)
        if not root.is_absolute() or not Path(release).is_absolute():
            raise ValueError('root and release must be absolute paths.')
        self.root, self.release, self.pins = root, Path(release), pins
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._active = None
        self._thread = None
        _private_dir(root)
        self._fd = os.open(root / 'game_jobs.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            raise GameJobError('Another coordinator owns this game job root.') from None
        try:
            self._recover()
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(90)
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------ journal

    def _journal(self, run):
        return self.root / run / 'journal.jsonl'

    def _load(self, run):
        if not RUN.fullmatch(run):
            raise ValueError('Unknown run.')
        try:
            fd = os.open(self._journal(run), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise ValueError('Unknown run.') from None
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(MAX_JOURNAL + 1)
        if len(raw) > MAX_JOURNAL:
            raise JournalError('The journal exceeds its bound.')
        committed = raw.rfind(b'\n') + 1  # an unterminated tail is an uncommitted torn write
        events, prev, state = [], GENESIS, None
        for number, line in enumerate(raw[:committed].split(b'\n')[:-1]):
            try:
                entry = json.loads(line)
            except ValueError:
                raise JournalError('A journal line is not JSON.') from None
            keys = {'seq', 'ns', 'at', 'state', 'message', 'data', 'prev', 'hash'}
            if (not isinstance(entry, dict) or set(entry) != keys or entry['seq'] != number or entry['prev'] != prev
                    or entry['hash'] != sha256_bytes(wire({k: v for k, v in entry.items() if k != 'hash'}).encode('ascii'))
                    or entry['state'] not in TRANSITIONS.get(state, ()) or not isinstance(entry['data'], dict)):
                raise JournalError('The journal hash chain or transition order does not verify.')
            events.append(entry)
            prev, state = entry['hash'], entry['state']
        if not events:
            raise JournalError('The journal is empty.')
        return events, committed

    def _append(self, run, state, message, data=None):
        with self._lock:
            if state == 'queued':
                if self._journal(run).exists():
                    raise GameJobError('That run already has a journal.')
                events, committed, flags = [], 0, os.O_EXCL
            else:
                (events, committed), flags = self._load(run), 0
            previous = events[-1]['state'] if events else None
            if state not in TRANSITIONS.get(previous, ()):
                raise GameJobError(f'Illegal job transition {previous} -> {state}.')
            now = time.time_ns()
            entry = {'seq': len(events), 'ns': now, 'at': datetime.fromtimestamp(now / 1e9, timezone.utc).isoformat(timespec='milliseconds'),
                     'state': state, 'message': clip(message, 1500), 'data': data or {},
                     'prev': events[-1]['hash'] if events else GENESIS}
            entry['hash'] = sha256_bytes(wire(entry).encode('ascii'))
            line = (wire(entry) + '\n').encode('ascii')
            fd = os.open(self._journal(run), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | flags, 0o600)
            try:
                if os.fstat(fd).st_size != committed:
                    os.ftruncate(fd, committed)
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            return entry

    def _run_ids(self):
        runs = []
        for child in self.root.iterdir():
            if RUN.fullmatch(child.name) and child.is_dir() and not child.is_symlink():
                try:
                    runs.append((self._load(child.name)[0][0]['ns'], child.name))
                except GameJobError:
                    runs.append((0, child.name))
                except ValueError:
                    continue
        return [name for _, name in sorted(runs)]

    # ------------------------------------------------------------ public API

    def busy(self):
        with self._lock:
            return self._active is not None

    def start(self, prompt, reference_sha256):
        if prompt != self.pins.prompt or sha256_bytes(prompt.encode('utf-8')) != self.pins.prompt_sha256:
            raise ValueError('This job accepts only its fixed prompt.')
        if not isinstance(reference_sha256, str) or not HASH.fullmatch(reference_sha256):
            raise ValueError('The reference digest must be 64 lowercase hexadecimal characters.')
        if reference_sha256 != self.pins.original_zip_sha256:
            raise ValueError('Upload the pinned original Windows release; no other reference is accepted.')
        with self._lock:
            if self._fd is None or self._stop.is_set():
                raise GameJobError('The coordinator is stopped.')
            if self._active is not None:
                raise Busy('A game job is already queued or running.')
            self._active = '-'
        run = None
        try:
            facts = verify_release(self.release, self.pins)
            run = uuid.uuid4().hex
            (self.root / run).mkdir(mode=0o700)
            self._append(run, 'queued', 'Queued. The pinned release verified; waiting for the compiler.', {
                'prompt_sha256': self.pins.prompt_sha256, 'reference_sha256': reference_sha256,
                'compiler_image': self.pins.image, 'vendor_tgz_sha256': self.pins.vendor_tgz_sha256,
                'lock_sha256': self.pins.lock_sha256, 'recipe_sha256': self.pins.recipe_sha256,
                'source_manifest_sha256': facts['manifest_sha256'], 'original_zip_sha256': self.pins.original_zip_sha256,
                'pk3_sha256': self.pins.pk3_sha256})
            with self._lock:
                self._active = run
                self._thread = threading.Thread(target=self._work, args=(run, facts), name='game-job-' + run, daemon=True)
                self._thread.start()
        except BaseException as exc:
            with self._lock:
                self._active = None
            if run is not None and not self._journal(run).exists():
                try:
                    (self.root / run).rmdir()
                except OSError as cleanup:
                    exc.add_note(f'The empty run directory could not be removed: {cleanup}')
            raise
        return run

    def status(self, run=None):
        with self._lock:
            if run is None:
                ids = self._run_ids()
                if not ids:
                    return {'id': None, 'state': 'idle', 'message': 'No game job has run.', 'events': [], 'busy': False}
                run = ids[-1]
            elif not isinstance(run, str) or not RUN.fullmatch(run):
                raise ValueError('Unknown run.')
            try:
                events, _ = self._load(run)
            except JournalError as exc:
                return {'id': run, 'state': 'failed', 'message': 'Journal integrity check failed: ' + str(exc), 'events': [],
                        'busy': False}
            pending = None
            if events[-1]['state'] == 'waiting_windows':
                try:
                    events = self._settle(run, events)
                except Unqualified as exc:
                    pending = str(exc)
                except Rejected:
                    events, _ = self._load(run)
            last = events[-1]
            view = {'id': run, 'state': last['state'], 'message': last['message'], 'busy': self._active == run,
                    'events': [{k: e[k] for k in ('seq', 'at', 'state', 'message', 'data')} for e in events]}
            ready = self._candidate(events)
            if ready:
                view['candidate'] = {k: ready[k] for k in ('artifact_sha256', 'zip_sha256', 'binding')}
            if last['state'] == 'waiting_windows':
                view['qualification'] = qualification_contract()
                view['qualification_pending'] = pending or 'No qualification receipt has been recorded.'
            if last['state'] == 'proven':
                view['verified_sha256'] = last['data']['verified_sha256']
            return view

    def package(self, run):
        """Return (private candidate ZIP path, sha256) only for a run with an accepted qualification receipt."""
        with self._lock:
            if not isinstance(run, str) or not RUN.fullmatch(run):
                raise ValueError('Unknown run.')
            events, _ = self._load(run)
            if events[-1]['state'] == 'waiting_windows':
                events = self._settle(run, events)
            if events[-1]['state'] != 'proven':
                raise GameJobError(f'Run is {events[-1]["state"]}, not proven; no package is available.')
            ready = self._candidate(events)
            path = self.root / run / CANDIDATE
            if path.is_symlink() or not path.is_file() or sha256_file(path) != ready['zip_sha256'] or (
                    events[-1]['data']['verified_sha256'] != ready['zip_sha256']):
                raise GameJobError('The candidate ZIP no longer matches its journaled hash.')
            verify_candidate_zip(path, ready['artifact_sha256'], self.pins)
            return path, ready['zip_sha256']

    def receipt_path(self, run):
        """Where the host-owned QA process must place qualification.json (never a web-writable location)."""
        if not RUN.fullmatch(run):
            raise ValueError('Unknown run.')
        return self.root / run / RECEIPT

    def evidence_dir(self, run):
        if not RUN.fullmatch(run):
            raise ValueError('Unknown run.')
        return self.root / run / EVIDENCE_DIR

    # ------------------------------------------------------------ build

    def _candidate(self, events):
        for entry in events:
            if entry['state'] == 'waiting_windows':
                package = next(e for e in events if e['state'] == 'package')
                return {'artifact_sha256': package['data']['artifact_sha256'], 'zip_sha256': entry['data']['zip_sha256'],
                        'binding': entry['hash'], 'ns': entry['ns'], 'original_exe_sha256': entry['data']['original_exe_sha256']}
        return None

    def _work(self, run, facts):
        try:
            self._append(run, 'build', 'Compiling the Windows game offline in the pinned sandbox.', {'compiler_image': self.pins.image})
            exe = self._build(run, facts)
            self._append(run, 'package', 'Compiled and checked a PE32 GUI program. Assembling the candidate ZIP.', exe)
            ready = self._package(run, facts, exe['artifact_sha256'])
            (self.root / run / EVIDENCE_DIR).mkdir(mode=0o700, exist_ok=True)
            self._append(run, 'waiting_windows', 'Candidate ready. Waiting for independent Windows qualification; '
                         'it is not downloadable until a qualification receipt is accepted.', ready)
        except Exception as exc:
            own = isinstance(exc, (GameJobError, ValueError))
            try:
                if not own:
                    logging.getLogger(__name__).exception('Game build %s failed', run)
                message = str(exc) if own else f'Internal error ({type(exc).__name__}). The build could not finish.'
                self._append(run, 'failed', message)
            except Exception:
                # Continuing is safe: the journal keeps its last committed state and the next start recovers it as failed.
                logging.getLogger(__name__).exception('Could not journal the failure of game job %s', run)
        finally:
            with self._lock:
                self._active = None

    def _build(self, run, facts):
        again = verify_release(self.release, self.pins)
        if again['manifest_sha256'] != facts['manifest_sha256']:
            raise ReleaseError('The release changed after it was accepted.')
        output = self._compile(run, facts)
        pe = check_pe(output)
        digest = sha256_bytes(output)
        if digest in facts['original']['exe_hashes']:
            raise GameJobError('The compiler output is a byte copy of an original executable.')
        path = self.root / run / ARTIFACT
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        log = self.root / run / BUILD_LOG
        return {'artifact_sha256': digest, 'pe': pe, 'build_log_sha256': sha256_file(log) if log.is_file() else None}

    def _package(self, run, facts, artifact_sha256):
        directory = self.root / run
        part, final = directory / (CANDIDATE + '.part'), directory / CANDIDATE
        provenance = {
            'format': 1, 'run': run, 'target': 'i686-pc-windows-gnu', 'compiler_image': self.pins.image,
            'build': 'cargo build --frozen --release in the pinned offline sandbox, from the included source',
            'source_manifest_sha256': facts['manifest_sha256'], 'lock_sha256': self.pins.lock_sha256,
            'vendor_tgz_sha256': self.pins.vendor_tgz_sha256, 'recipe_sha256': self.pins.recipe_sha256,
            'artifact_sha256': artifact_sha256, 'original_zip_sha256': self.pins.original_zip_sha256,
            'data_pk3_sha256': self.pins.pk3_sha256}
        build_candidate(part, self.release, facts, directory / ARTIFACT, provenance, self.pins)
        with part.open('rb') as written:
            os.fsync(written.fileno())
        verify_candidate_zip(part, artifact_sha256, self.pins)
        os.rename(part, final)
        os.chmod(final, 0o400)
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return {'zip_sha256': sha256_file(final), 'zip_bytes': final.stat().st_size,
                'original_exe_sha256': facts['original']['exe_sha256']}

    def _docker_command(self, run, source, vendor):
        for item in (source, vendor):
            if not item.is_absolute() or ',' in str(item) or item.is_symlink() or not item.is_dir():
                raise ReleaseError('A pinned mount is missing or not a plain directory.')
        return [self.docker, 'run', '--rm', '--name', 'gamejob-' + run, '--pull=never', '--runtime=runsc', '--network=none',
                '--read-only', '--user=65534:65534', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--cpus=2',
                '--memory=4g', '--memory-swap=4g', '--pids-limit=512', '--log-driver=none', '--ulimit=nofile=4096:4096',
                '--ulimit=core=0:0', '--env=LC_ALL=C', '--env=TMPDIR=/tmp', '--env=CARGO_INCREMENTAL=0',
                '--tmpfs', '/work:rw,nosuid,nodev,exec,uid=65534,gid=65534,mode=0700,size=2g',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,uid=65534,gid=65534,mode=1777,size=256m',
                '--mount', f'type=bind,src={source},dst=/src,readonly', '--mount', f'type=bind,src={vendor},dst=/vendor,readonly',
                self.pins.image]

    def _compile(self, run, facts):
        """Run the pinned image's own entrypoint; its stdout is the linked PE. Never executes the PE."""
        seen = subprocess.run([self.docker, 'image', 'inspect', '--format', '{{.Id}}', self.pins.image], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30)
        if seen.returncode or seen.stdout.decode('ascii', 'replace').strip() != self.pins.image:
            raise GameJobError('The pinned compiler image is not installed on this host.')
        cmd = self._docker_command(run, self.release / 'source', self.release / 'vendor')
        name = 'gamejob-' + run
        deadline = time.monotonic() + BUILD_DEADLINE
        stdout, stderr = bytearray(), bytearray()
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                with selectors.DefaultSelector() as sel:
                    sel.register(process.stdout, selectors.EVENT_READ, stdout)
                    sel.register(process.stderr, selectors.EVENT_READ, stderr)
                    while sel.get_map():
                        if self._stop.is_set():
                            raise Cancelled('The coordinator stopped during the build.')
                        if time.monotonic() > deadline:
                            raise GameJobError('The compiler exceeded its fixed deadline.')
                        for key, _ in sel.select(0.25):
                            block = os.read(key.fd, 65536)
                            if not block:
                                sel.unregister(key.fd)
                                continue
                            key.data.extend(block)
                            if len(key.data) > (MAX_PE if key.data is stdout else MAX_STDERR):
                                raise GameJobError('Compiler output exceeded its bound.')
                status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                    rm = subprocess.run([self.docker, 'rm', '-f', name], capture_output=True, timeout=30)
                    if rm.returncode and b'No such container' not in rm.stderr:
                        raise GameJobError(f'Operator must inspect the stopped job container {name}.')
        log = self.root / run / BUILD_LOG
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(bytes(stderr))
        if status:
            raise GameJobError(f'The compiler exited {status}. See the private build log for this run.')
        return bytes(stdout)

    # ------------------------------------------------------------ qualification

    def _settle(self, run, events):
        """If an acceptable receipt exists, journal `proven`; a fresh mismatching one journals `failed`."""
        ready = self._candidate(events)
        path = self.root / run / CANDIDATE
        if path.is_symlink() or not path.is_file() or sha256_file(path) != ready['zip_sha256']:
            self._append(run, 'failed', 'The candidate ZIP no longer matches its journaled hash.')
            raise Rejected('The candidate ZIP no longer matches its journaled hash.')
        try:
            proof = self._qualify(run, ready)
        except Rejected as exc:
            self._append(run, 'failed', 'Qualification rejected the candidate: ' + str(exc))
            raise
        try:
            verify_candidate_zip(path, ready['artifact_sha256'], self.pins)
        except (GameJobError, zipfile.BadZipFile, OSError) as exc:
            self._append(run, 'failed', 'The candidate ZIP failed its post-qualification re-check: ' + str(exc))
            raise Rejected('The candidate ZIP failed its post-qualification re-check.') from exc
        self._append(run, 'proven', 'Independent Windows qualification accepted this exact candidate.', {
            'verified_sha256': ready['zip_sha256'], 'artifact_sha256': ready['artifact_sha256'], **proof})
        return self._load(run)[0]

    def _qualify(self, run, ready):
        directory = self.root / run
        raw = _private_read(directory / RECEIPT, MAX_RECEIPT, newer_than_ns=ready['ns'], what='The qualification receipt')
        try:
            receipt = _strict_json(raw)
        except ValueError:
            raise Unqualified('The qualification receipt is not strict JSON.') from None
        _expect(isinstance(receipt, dict) and set(receipt) == {'version', 'run', 'reference_sha256', 'artifact_sha256',
                'zip_sha256', 'binding', 'checks'}, 'The receipt has the wrong top-level fields.')
        _expect(receipt['version'] == 1 and type(receipt['version']) is int, 'Unsupported receipt version.')
        for key, want in (('run', run), ('reference_sha256', self.pins.original_zip_sha256),
                          ('artifact_sha256', ready['artifact_sha256']), ('zip_sha256', ready['zip_sha256']),
                          ('binding', ready['binding'])):
            _expect(receipt[key] == want, f'The receipt {key} does not bind this candidate.')
        checks = receipt['checks']
        required = {case.name: case for case in REQUIRED}
        _expect(isinstance(checks, list) and all(isinstance(c, dict) for c in checks), 'The receipt checks are malformed.')
        _expect(sorted(str(c.get('name')) for c in checks) == sorted(required),
                'The receipt must hold exactly the required named checks once each.')
        used_names, used_hashes, parsed = set(), set(), []
        subjects = (('original', ready['original_exe_sha256']), ('candidate', ready['artifact_sha256']))
        for item in checks:
            case = required[item['name']]
            keys = {'name', 'kind', 'original', 'candidate'} | ({'comparison'} if case.kind == 'continuous' else set())
            _expect(set(item) == keys and item['kind'] == case.kind, f'Check {case.name} has the wrong fields or kind.')
            sides = {}
            for label, subject in subjects:
                sides[label] = self._side(directory, run, case, label, item[label], subject, ready, used_names, used_hashes)
            parsed.append((case, sides, item.get('comparison')))
        for case, sides, comparison in parsed:
            if case.kind == 'discrete':
                a, b = sides['original']['outcome'], sides['candidate']['outcome']
                if type(a) is not type(b) or a != b:
                    raise Rejected(f'check {case.name}: the candidate outcome differs from the original.')
                continue
            a, b = sides['original']['values'], sides['candidate']['values']
            _expect(len(a) == len(b), f'Check {case.name} series differ in length.')
            observed = max(abs(x - y) for x, y in zip(a, b))
            _expect(isinstance(comparison, dict) and set(comparison) == {'metric', 'tolerance', 'observed', 'accepted'}
                    and comparison['metric'] == 'max_abs_error' and comparison['tolerance'] == case.tolerance
                    and type(comparison['tolerance']) is not bool and _number(comparison['observed'])
                    and abs(comparison['observed'] - observed) <= 1e-9 and isinstance(comparison['accepted'], bool),
                    f'Check {case.name} declares a comparison that does not match the host rule or data.')
            if comparison['accepted'] is not True or observed > case.tolerance:
                raise Rejected(f'check {case.name}: continuous comparison exceeds {case.tolerance}.')
        return {'receipt_sha256': sha256_bytes(raw), 'evidence_sha256': sorted(used_hashes)}

    def _side(self, directory, run, case, label, side, subject, ready, used_names, used_hashes):
        where = f'Check {case.name} {label}'
        value_key = 'outcome' if case.kind == 'discrete' else 'values'
        _expect(isinstance(side, dict) and set(side) == {'method', 'subject_sha256', 'evidence', 'evidence_sha256', value_key},
                f'{where} has the wrong fields.')
        _expect(side['method'] == 'windows_run', f'{where} is not a recorded Windows run.')
        _expect(side['subject_sha256'] == subject, f'{where} observed a different executable.')
        name, digest = side['evidence'], side['evidence_sha256']
        _expect(isinstance(name, str) and EVIDENCE_NAME.fullmatch(name) and isinstance(digest, str) and HASH.fullmatch(digest),
                f'{where} evidence reference is malformed.')
        _expect(name not in used_names and digest not in used_hashes, f'{where} reuses evidence.')
        folder = directory / EVIDENCE_DIR
        info = folder.lstat() if folder.exists() or folder.is_symlink() else None
        _expect(info is not None and stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022,
                'The evidence directory is missing or not host-protected.')
        data = _private_read(folder / name, MAX_EVIDENCE, newer_than_ns=ready['ns'], what=f'{where} evidence')
        _expect(sha256_bytes(data) == digest, f'{where} evidence does not match its hash.')
        for needle in (run, subject, ready['binding']):
            _expect(needle.encode('ascii') in data, f'{where} evidence does not mention this run, executable and candidate binding.')
        used_names.add(name)
        used_hashes.add(digest)
        value = side[value_key]
        if case.kind == 'discrete':
            _expect((isinstance(value, int) and not isinstance(value, bool)) or (
                    isinstance(value, str) and 1 <= len(value) <= 200 and value.strip().lower() not in WEAK_OUTCOMES),
                    f'{where} outcome is not a concrete observed value.')
        else:
            _expect(isinstance(value, list) and case.samples <= len(value) <= 100000 and all(_number(v) for v in value),
                    f'{where} values are not a finite series of at least {case.samples} samples.')
            if label == 'original':
                _expect(len(set(value)) >= 2, f'{where} values do not vary.')
        return {'evidence_sha256': digest, value_key: value}

    # ------------------------------------------------------------ recovery

    def _remove_container(self, run):
        try:
            rm = subprocess.run([self.docker, 'rm', '-f', 'gamejob-' + run], capture_output=True, timeout=30,
                                stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return f'Container cleanup could not be confirmed; the operator must inspect gamejob-{run}.'
        if rm.returncode == 0:
            return 'A leftover job container was removed.'
        if b'No such container' in rm.stderr:
            return 'No job container remained.'
        return f'Container cleanup could not be confirmed; the operator must inspect gamejob-{run}.'

    def _recover(self):
        """Never resume an unobserved side effect. Interrupted attempts become failed and say where they stopped."""
        for run in self._run_ids():
            try:
                events, _ = self._load(run)
            except GameJobError:
                continue
            state = events[-1]['state']
            if state not in BUSY_STATES:
                continue
            note = self._remove_container(run) if state == 'build' else ''
            (self.root / run / (CANDIDATE + '.part')).unlink(missing_ok=True)
            self._append(run, 'failed', f'Interrupted by a coordinator restart while {state}; the attempt was not resumed. '
                         f'Start a new run. {note}'.strip(), {'interrupted_state': state})
