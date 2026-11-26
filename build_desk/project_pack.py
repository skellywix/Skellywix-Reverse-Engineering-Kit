"""Byte-level plumbing for project builds: the stdin tar for the sandbox, its output tar, and the deliverable ZIP.

Everything is streamed from the validated SourceTree; nothing an upload names is ever written to a host path.
"""
import hashlib
import itertools
import json
import re
import tarfile
import zipfile

from . import project_archive as pa

FIXED_MTIME = 1577836800                     # 2020-01-01: older than anything the sandbox builds
ZIP_STAMP = (2020, 1, 1, 0, 0, 0)
SANDBOX_UID = 65534
MAX_OUTPUT = 128 << 20                       # bytes of build output accepted from the sandbox
MAX_OUTPUT_FILES = 64
MAX_ARTIFACT = 256 << 20                     # bytes of the finished deliverable
LISTED_FILES = 1000
OUTPUT_NAME = re.compile(r'bin/[A-Za-z0-9._+-]{1,100}')
TOOLCHAIN_NAME = 'toolchain.txt'
MAX_TOOLCHAIN = 2048
OUTPUT_LIMITS = pa.Limits(max_input=MAX_OUTPUT + (4 << 20), max_members=MAX_OUTPUT_FILES + 8, max_member=MAX_OUTPUT,
                          max_expanded=MAX_OUTPUT, max_ratio=1 << 30, max_name=128)
CLAIMS = ('Assembled from the uploaded project files only. Any compiled output was produced offline in a sandbox and '
          'was never run. No claim is made that it behaves like any other program, release or source revision.')


class _Blocks:
    """File-like adapter over an iterator of byte blocks, for tarfile.addfile."""

    def __init__(self, blocks):
        self._blocks = iter(blocks)
        self._block = b''
        self._pos = 0

    def read(self, size=-1):
        out = []
        want = size
        while want != 0:
            if self._pos >= len(self._block):
                self._block = next(self._blocks, None)
                self._pos = 0
                if self._block is None:
                    self._block = b''
                    break
                continue
            end = len(self._block) if want < 0 else min(len(self._block), self._pos + want)
            piece = self._block[self._pos:end]
            self._pos = end
            out.append(piece)
            if want > 0:
                want -= len(piece)
        return b''.join(out)


def _add_bytes(tar, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = FIXED_MTIME
    info.uid = info.gid = SANDBOX_UID
    tar.addfile(info, _Blocks([data]))


def write_stage(stream, script, helpers, tree, plan):
    """Write the sandbox input to `stream`: run.sh, helper files, and the planned project files under src/."""
    with tarfile.open(fileobj=stream, mode='w|', format=tarfile.PAX_FORMAT) as tar:
        _add_bytes(tar, 'run.sh', script.encode('utf-8'))
        for name, text in sorted(helpers.items()):
            _add_bytes(tar, name, text.encode('utf-8'))
        for source, dest in plan.files:
            entry = tree.get(source)
            blocks = tree.iter_bytes(entry)
            first = next(blocks, b'')
            info = tarfile.TarInfo('src/' + dest)
            info.size = entry.size
            info.mode = 0o755 if entry.executable or first.startswith(b'#!') else 0o644
            info.mtime = FIXED_MTIME
            info.uid = info.gid = SANDBOX_UID
            tar.addfile(info, _Blocks(itertools.chain([first], blocks)))


def read_output(stdout):
    """Validate the sandbox's output tar. Returns (tree, bin_entries, toolchain_lines). Raises ValueError."""
    if pa.detect(stdout) != 'tar':
        raise ValueError('The sandbox returned something other than a tar of build output.')
    tree = pa.open_upload(stdout, OUTPUT_LIMITS, None, strip_prefix=False)
    binaries = [e for e in tree.entries() if OUTPUT_NAME.fullmatch(e.name)]
    if not binaries:
        raise ValueError('The build produced no executable or library files to package.')
    lines = []
    extra = tree.get(TOOLCHAIN_NAME)
    if extra is not None:
        text = tree.read(extra, MAX_TOOLCHAIN).decode('utf-8', 'replace')
        lines = [pa.clip_name(line, 160) for line in text.splitlines()[:8] if line.strip()]
    return tree, binaries, lines


def write_zip(path, tree, members, info, limit=MAX_ARTIFACT):
    """Write a deterministic ZIP at `path` (must not exist). `members` is ((source name, archive name, mode), ...).
    PACKAGE-INFO.json is appended last, completed with sizes and SHA-256 of every file. Raises ValueError if the
    deliverable would exceed `limit`. Returns the number of payload files."""
    listed = []
    total = 0
    with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, arcname, mode in members:
            entry = tree.get(source)
            stamp = zipfile.ZipInfo(arcname, ZIP_STAMP)
            stamp.compress_type = zipfile.ZIP_DEFLATED
            stamp.create_system = 3
            stamp.external_attr = (0o100000 | mode) << 16
            digest = hashlib.sha256()
            size = 0
            with archive.open(stamp, 'w', force_zip64=True) as out:
                for block in tree.iter_bytes(entry):
                    size += len(block)
                    total += len(block)
                    if total > limit:
                        raise ValueError('The packaged output would be larger than the output limit.')
                    digest.update(block)
                    out.write(block)
            listed.append({'path': arcname, 'bytes': size, 'sha256': digest.hexdigest()})
        document = dict(info, file_count=len(listed), files=listed[:LISTED_FILES], claims=CLAIMS)
        data = json.dumps(document, ensure_ascii=True, sort_keys=True, indent=1).encode('ascii')
        stamp = zipfile.ZipInfo('PACKAGE-INFO.json', ZIP_STAMP)
        stamp.compress_type = zipfile.ZIP_DEFLATED
        stamp.create_system = 3
        stamp.external_attr = (0o100000 | 0o644) << 16
        archive.writestr(stamp, data)
    return len(listed)


def payload_sha256(path):
    """Identify the delivered files independently of the ZIP's per-run provenance."""
    try:
        with zipfile.ZipFile(path) as archive:
            digest = hashlib.sha256()
            for entry in sorted(archive.infolist(), key=lambda item: item.filename):
                if entry.filename == 'PACKAGE-INFO.json':
                    continue
                with archive.open(entry) as stream:
                    content = hashlib.file_digest(stream, 'sha256').digest()
                name = entry.filename.encode('utf-8')
                digest.update(len(name).to_bytes(4, 'big'))
                digest.update(name)
                digest.update((entry.external_attr >> 16).to_bytes(2, 'big'))
                digest.update(content)
            return digest.hexdigest()
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError('The delivered files could not be verified.') from exc
