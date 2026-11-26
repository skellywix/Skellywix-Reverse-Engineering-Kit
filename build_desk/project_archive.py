"""Passive reading of an uploaded source project: one ZIP, tar (optionally gzip), or a single text source file.

Nothing here writes uploaded members to the host filesystem or executes them. Members are validated while the
archive is listed and are later streamed straight into the sandbox (as a tar on stdin) or into a packaged ZIP:

* names must be relative, use '/' separators, and carry no '..', drive letter, colon, backslash or control character;
* only regular files and directories are accepted: symlinks, hard links, devices, FIFOs, sockets, sparse members
  and encrypted ZIP members are refused;
* duplicate names (also case-insensitively) are refused;
* member count, per-member size, total expanded size and per-member compression ratio are bounded, and the bytes
  actually read are checked against the declared sizes.

A single top-level directory shared by every member (the `project-main/` prefix of a GitHub download) is removed.
"""
import gzip
import io
import lzma
import re
import stat
import tarfile
import zipfile
import zlib
from dataclasses import dataclass, field

ZIP_MAGIC = (b'PK\x03\x04', b'PK\x05\x06')
GZIP_MAGIC = b'\x1f\x8b'
TAR_MAGIC_OFFSET = 257
CHUNK = 1 << 20
_PART_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f:\\]')
_PROJECT_NAME = re.compile(r'[^A-Za-z0-9._-]+')
_READ_ERRORS = (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, zlib.error, lzma.LZMAError,
                NotImplementedError, RuntimeError)


@dataclass(frozen=True)
class Limits:
    """Bounds for one upload. Defaults suit an ordinary project with vendored dependencies."""
    max_input: int = 256 << 20
    max_members: int = 100_000
    max_member: int = 256 << 20
    max_expanded: int = 1 << 30
    max_ratio: int = 250            # applies to members larger than ratio_floor
    ratio_floor: int = 1 << 20
    max_name: int = 1024
    max_text_source: int = 16 << 20


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    executable: bool
    key: object = field(repr=False, compare=False)


class SourceTree:
    """Validated, prefix-stripped regular files of one upload, readable member by member."""

    def __init__(self, kind, entries, dirs, prefix, opener, limits):
        self.kind = kind                      # 'zip' | 'tar' | 'file'
        self.prefix = prefix                  # stripped top-level directory, or ''
        self._entries = {e.name: e for e in entries}
        self.dirs = frozenset(dirs)
        self._opener = opener
        self._limits = limits

    @property
    def names(self):
        return sorted(self._entries)

    def entries(self, under=''):
        """Entries below directory `under` (''=all), sorted by name."""
        if not under:
            return [self._entries[n] for n in self.names]
        base = under.rstrip('/') + '/'
        return [self._entries[n] for n in self.names if n.startswith(base)]

    def has(self, name):
        return name in self._entries

    def get(self, name):
        return self._entries.get(name)

    def total_size(self):
        return sum(e.size for e in self._entries.values())

    def read(self, entry, limit=None):
        """The member's bytes, checked against its declared size. `limit` refuses larger members early."""
        if limit is not None and entry.size > limit:
            raise ValueError(f'{entry.name} is larger than {limit} bytes.')
        out = bytearray()
        for block in self.iter_bytes(entry):
            out.extend(block)
        return bytes(out)

    def iter_bytes(self, entry):
        """Stream the member in blocks; raises ValueError if the stored bytes disagree with the declared size."""
        seen = 0
        with self._opener(entry) as stream:
            while True:
                try:
                    block = stream.read(CHUNK)
                except ValueError:
                    raise
                except _READ_ERRORS as error:
                    raise ValueError(f'Archive member {clip_name(entry.name)} cannot be read ({type(error).__name__}).') from error
                if not block:
                    break
                seen += len(block)
                if seen > entry.size:
                    raise ValueError(f'Archive member {clip_name(entry.name)} holds more data than it declares.')
                yield block
        if seen != entry.size:
            raise ValueError(f'Archive member {clip_name(entry.name)} is truncated.')

    def project_name(self, fallback):
        return project_name(self.prefix, fallback)


def project_name(text, fallback):
    """A filesystem- and URL-safe short name derived from `text`, else `fallback`."""
    name = _PROJECT_NAME.sub('-', text or '').strip('.-_')[:48].strip('.-_')
    return name or fallback


def check_name(raw, limits):
    """Validate one member name; returns (normalized_name, is_dir). Raises ValueError."""
    if not isinstance(raw, str) or not raw:
        raise ValueError('An archive member has an empty name.')
    if len(raw) > limits.max_name:
        raise ValueError('An archive member name is too long.')
    is_dir = raw.endswith('/')
    name = raw.rstrip('/')
    while name.startswith('./'):
        name = name[2:]
    if not name or name == '.':
        return '', True
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ValueError(f'Archive member {clip_name(raw)} uses an absolute path.')
    for part in name.split('/'):
        if part in ('', '.', '..'):
            raise ValueError(f'Archive member {clip_name(raw)} escapes or is not a plain relative path.')
        if len(part) > 255 or _PART_FORBIDDEN.search(part):
            raise ValueError(f'Archive member {clip_name(raw)} has an unsafe name.')
    return name, is_dir


def clip_name(name, limit=120):
    text = ''.join(c if 32 <= ord(c) < 127 else '?' for c in str(name))
    return text if len(text) <= limit else text[:limit - 3] + '...'


def detect(data):
    """'zip', 'gzip', 'tar' or 'file' from leading bytes; never from a client-supplied name."""
    if data[:4] in ZIP_MAGIC:
        return 'zip'
    if data[:2] == GZIP_MAGIC:
        return 'gzip'
    if data[TAR_MAGIC_OFFSET:TAR_MAGIC_OFFSET + 5] == b'ustar':
        return 'tar'
    return 'file'


def text_source(data, limits):
    """Decode a single uploaded source file. Raises ValueError for binary or oversized content."""
    if len(data) > limits.max_text_source:
        raise ValueError('A single source file is larger than the text source limit.')
    if b'\x00' in data:
        raise ValueError('The upload is neither a ZIP nor a tar archive, and is not a text source file.')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('The upload is neither a ZIP nor a tar archive, and is not UTF-8 text.') from None


def open_upload(data, limits, single_name, spool=None, strip_prefix=True):
    """Return a SourceTree for `data`. A non-archive upload becomes one member named `single_name` (or ValueError if
    `single_name` is None). `spool` is a binary file object for a decompressed gzip tar (bounded by max_expanded).
    `strip_prefix` removes one shared top-level directory; macOS `__MACOSX/` resource forks are always dropped."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError('The upload is empty.')
    if len(data) > limits.max_input:
        raise ValueError(f'The upload exceeds {limits.max_input} bytes.')
    kind = detect(data)
    if kind == 'zip':
        return _open_zip(bytes(data), limits, strip_prefix)
    if kind == 'gzip':
        if spool is None:
            spool = io.BytesIO()
        _gunzip(data, spool, limits)
        return _open_tar(spool, limits, strip_prefix)
    if kind == 'tar':
        return _open_tar(io.BytesIO(bytes(data)), limits, strip_prefix)
    if single_name is None:
        raise ValueError('This profile needs a ZIP or tar archive of the project.')
    text_source(data, limits)
    blob = bytes(data)
    entry = Entry(single_name, len(blob), False, None)
    return SourceTree('file', [entry], (), '', lambda _e: io.BytesIO(blob), limits)


def _gunzip(data, spool, limits):
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(bytes(data))) as stream:
            while block := stream.read(CHUNK):
                total += len(block)
                if total > limits.max_expanded + (1 << 20):
                    raise ValueError('The compressed tar expands beyond the size limit.')
                spool.write(block)
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ValueError('The gzip stream is corrupt.') from error
    spool.seek(0)


def _strip_prefix(files, dirs):
    """Remove one shared top directory when every file lives below it."""
    if not files:
        return '', files, dirs
    tops = {n.split('/', 1)[0] for n in files}
    if len(tops) != 1:
        return '', files, dirs
    top = next(iter(tops))
    if any('/' not in n for n in files):
        return '', files, dirs
    cut = len(top) + 1
    return top, {n[cut:]: v for n, v in files.items()}, {d[cut:] for d in dirs if d.startswith(top + '/')}


def _admit(files, dirs, name, is_dir, size, executable, key, folded, limits):
    if not name or name == '__MACOSX' or name.startswith('__MACOSX/'):
        return
    if is_dir:
        if name in files:
            raise ValueError(f'Archive member {clip_name(name)} is both a file and a directory.')
        dirs.add(name)
        return
    lowered = name.casefold()
    if name in files or lowered in folded or name in dirs:
        raise ValueError(f'Archive member {clip_name(name)} appears more than once (names are compared ignoring case).')
    if size < 0 or size > limits.max_member:
        raise ValueError(f'Archive member {clip_name(name)} exceeds the per-file size limit.')
    if len(files) >= limits.max_members:
        raise ValueError(f'The archive has more than {limits.max_members} files.')
    folded.add(lowered)
    files[name] = (size, executable, key)


def _finish(kind, files, dirs, opener, limits, strip):
    total = sum(v[0] for v in files.values())
    if total > limits.max_expanded:
        raise ValueError('The archive expands beyond the size limit.')
    for name in files:  # a file may not also be used as a directory
        parent = name.rsplit('/', 1)[0] if '/' in name else ''
        while parent:
            if parent in files:
                raise ValueError(f'Archive member {clip_name(parent)} is both a file and a directory.')
            parent = parent.rsplit('/', 1)[0] if '/' in parent else ''
    if not files:
        raise ValueError('The archive contains no files.')
    prefix, files, dirs = _strip_prefix(files, dirs) if strip else ('', files, dirs)
    entries = [Entry(n, s, x, k) for n, (s, x, k) in files.items()]
    for entry in entries:
        parent = entry.name
        while '/' in parent:
            parent = parent.rsplit('/', 1)[0]
            dirs.add(parent)
    return SourceTree(kind, entries, dirs, prefix, opener, limits)


def _open_zip(data, limits, strip):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError) as error:
        raise ValueError('The ZIP archive is corrupt.') from error
    if len(infos) > limits.max_members * 2:
        raise ValueError(f'The archive has more than {limits.max_members} files.')
    files, dirs, folded = {}, set(), set()
    for info in infos:
        name, is_dir = check_name(info.filename, limits)
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        if info.create_system == 3 and kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            what = 'a symbolic link' if kind == stat.S_IFLNK else 'a device or special file'
            raise ValueError(f'Archive member {clip_name(info.filename)} is {what}; only regular files are accepted.')
        if info.flag_bits & 0x1:
            raise ValueError(f'Archive member {clip_name(info.filename)} is encrypted.')
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            raise ValueError(f'Archive member {clip_name(info.filename)} uses an unsupported compression method.')
        is_dir = is_dir or info.is_dir() or kind == stat.S_IFDIR
        if not is_dir and info.file_size > limits.ratio_floor:
            if info.compress_size <= 0 or info.file_size > info.compress_size * limits.max_ratio:
                raise ValueError(f'Archive member {clip_name(info.filename)} has an implausible compression ratio.')
        executable = bool(info.create_system == 3 and mode & 0o100)
        _admit(files, dirs, name, is_dir, info.file_size, executable, info, folded, limits)

    def opener(entry):
        try:
            return archive.open(entry.key)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as error:
            raise ValueError(f'Archive member {clip_name(entry.name)} cannot be read.') from error

    return _finish('zip', files, dirs, opener, limits, strip)


def _open_tar(stream, limits, strip):
    try:
        archive = tarfile.open(fileobj=stream, mode='r:')
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError('The tar archive is corrupt.') from error
    files, dirs, folded = {}, set(), set()
    count = 0
    try:
        for member in archive:
            count += 1
            if count > limits.max_members * 2:
                raise ValueError(f'The archive has more than {limits.max_members} files.')
            if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
                continue
            name, is_dir = check_name(member.name, limits)
            if member.issym() or member.islnk():
                raise ValueError(f'Archive member {clip_name(member.name)} is a link; only regular files are accepted.')
            if member.issparse():
                raise ValueError(f'Archive member {clip_name(member.name)} is a sparse file.')
            if member.isdir():
                is_dir = True
            elif not member.isreg():
                raise ValueError(f'Archive member {clip_name(member.name)} is a device or special file.')
            _admit(files, dirs, name, is_dir, member.size, bool(member.mode & 0o100), member, folded, limits)
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError('The tar archive is corrupt.') from error

    def opener(entry):
        handle = archive.extractfile(entry.key)
        if handle is None:
            raise ValueError(f'Archive member {clip_name(entry.name)} cannot be read.')
        return handle

    return _finish('tar', files, dirs, opener, limits, strip)
