"""The candidate-only runtime: a Docker image archive that holds metadata and no layers.

A candidate program is bind-mounted at /candidate and started with --entrypoint /candidate. The image behind it must
therefore hold nothing at all: no shell, no interpreter, no loader or library (the compiled program is static), no
original program, no files. This module generates that image as a deterministic `docker load` archive from constants,
so the exact bytes can be reviewed and rebuilt anywhere without Docker, and states the shape the verifier re-checks
against the live image before a run. The tar blocks are written directly, so the bytes do not depend on the Python
version's tarfile module.

Nothing here pins or reviews an image. ARCHIVE_SHA256 is the digest of what this generator produces, so a change to the
generator is visible. The local image ID Docker derives from a loaded archive is not computed here: it is read from
Docker at the real boundary and becomes trusted only when a reviewer pins it in containers.py (see LAYOUT.md).
"""
import hashlib
import io
import json
import tarfile

ARCHITECTURE = 'amd64'  # linux/amd64, the platform of the pinned compiler and observer images
OS = 'linux'
CREATED = '1970-01-01T00:00:00Z'  # fixed: the image ID must not depend on when the archive was built
BLOCK = 512
RECORD = 10240
# sha256 of the archive bytes this module generates. Not a review: it only detects a generator change.
ARCHIVE_SHA256 = 'f1d034b9d5653c10472234f3beec2d09032734e98525419a2ec886504d1023b1'


def configuration():
    """The image configuration. No Env, Cmd, Entrypoint, User, ports, volumes, or layers: nothing to run or reach."""
    return {'architecture': ARCHITECTURE, 'os': OS, 'created': CREATED, 'config': {},
            'rootfs': {'type': 'layers', 'diff_ids': []}}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def configuration_bytes():
    return _json(configuration())


def configuration_sha256():
    return hashlib.sha256(configuration_bytes()).hexdigest()


def manifest_bytes():
    """No RepoTags (the image is addressed only by its ID) and an empty Layers list."""
    return _json([{'Config': f'{configuration_sha256()}.json', 'RepoTags': None, 'Layers': []}])


def files():
    """The only archive members, in order."""
    return ((f'{configuration_sha256()}.json', configuration_bytes()), ('manifest.json', manifest_bytes()))


def _header(name, size):
    """One USTAR header: regular file, mode 0644, owner 0:0, time 0."""
    block = bytearray(BLOCK)

    def put(offset, data):
        block[offset:offset + len(data)] = data
    put(0, name.encode('ascii'))
    put(100, b'0000644\0' + b'0000000\0' + b'0000000\0' + b'%011o\0' % size + b'%011o\0' % 0)
    put(148, b' ' * 8)  # the checksum field counts as spaces while summing
    block[156] = ord('0')
    put(257, b'ustar\x0000')
    put(329, b'0000000\0' + b'0000000\0')
    put(148, b'%06o\0 ' % sum(block))
    return bytes(block)


def archive():
    """Deterministic archive bytes: fixed order, owner, mode, and time; USTAR; padded to whole 10240-byte records."""
    out = bytearray()
    for name, data in files():
        out += _header(name, len(data)) + data + bytes(-len(data) % BLOCK)
    out += bytes(2 * BLOCK)
    out += bytes(-len(out) % RECORD)
    return bytes(out)


def archive_sha256():
    return hashlib.sha256(archive()).hexdigest()


def describe(data):
    """Facts for a reviewer about archive bytes. Raises ValueError unless they are exactly what archive() generates."""
    if data != archive():
        raise ValueError('These bytes are not the archive this generator produces.')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r') as tar:
        members = [(info.name, info.size, info.isreg()) for info in tar.getmembers()]
    digest = hashlib.sha256(data).hexdigest()
    return {'archive_sha256': digest, 'members': members, 'configuration_sha256': configuration_sha256(),
            'configuration': configuration(), 'layers': 0, 'matches_pinned_generator_digest': digest == ARCHIVE_SHA256}


def shape_problem(layers, entrypoint, command, env, platform):
    """None when a live image has the generated shape; otherwise a fixed phrase. Arguments are decoded inspect fields.

    This is a check at the real boundary, not a review: the pinned image ID is what a reviewer approves.
    """
    if layers not in (None, []):
        return 'The candidate image holds filesystem layers.'
    if entrypoint not in (None, []) or command not in (None, []) or env not in (None, []):
        return 'The candidate image defines an entrypoint, command, or environment.'
    if platform != f'{OS}/{ARCHITECTURE}':
        return 'The candidate image is not linux/amd64.'
    return None
