"""Observable byte framing (cli-v1, http-v1). The same framing hashes expected, original, replay, and candidate runs.

Integers are big-endian. Every variable-length field is length-prefixed, so no two observations share a frame.
"""
import re
import struct

from .errors import Refused
from .identity import SHA256, sha256

FRAMING = {'cli': 'cli-v1', 'http': 'http-v1'}
MAX_INPUT = 1 << 20
_STATUS = re.compile(rb'HTTP/1\.1 ([1-5][0-9]{2}) [\x20-\x7e]*')
_HEADER = re.compile(rb"([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*([\x20-\x7e]*?)[ \t]*")
_LENGTH = re.compile(rb'[0-9]{1,9}')


def _u16(value):
    return struct.pack('>H', value)


def _u32(value):
    return struct.pack('>I', value)


def _u64(value):
    return struct.pack('>Q', value)


def _bytes(data):
    return _u64(len(data)) + data


def latin1(text, what):
    if not isinstance(text, str):
        raise Refused(f'{what} must be a string.')
    try:
        return text.encode('latin-1')
    except UnicodeEncodeError as error:
        raise Refused(f'{what} has a code point above U+00FF.') from error


def expand(spec):
    """Case stdin from a JSON form: {"latin1": text} or {"repeat_latin1": text, "times": n}, at most 1 MiB."""
    if isinstance(spec, dict) and set(spec) == {'latin1'}:
        data = latin1(spec['latin1'], 'stdin')
    elif isinstance(spec, dict) and set(spec) == {'repeat_latin1', 'times'}:
        unit, times = latin1(spec['repeat_latin1'], 'stdin'), spec['times']
        if type(times) is not int or times < 1 or not unit or len(unit) * times > MAX_INPUT:
            raise Refused('stdin repeat is empty, not a positive integer, or over 1 MiB.')
        data = unit * times
    else:
        raise Refused('stdin must be {"latin1": text} or {"repeat_latin1": text, "times": n}.')
    if len(data) > MAX_INPUT:
        raise Refused('stdin is over 1 MiB.')
    return data


def _cli(exit_status, stdout, stderr):
    return b'cli-v1\n' + _u32(exit_status) + _bytes(stdout) + _bytes(stderr)


def _http_parsed(exit_status, stderr, status, headers, body):
    out = b'http-v1\n' + _u32(exit_status) + _bytes(stderr) + b'\x01' + _u16(status) + _u32(len(headers))
    for name, value in headers:
        out += _u32(len(name)) + name + _u32(len(value)) + value
    return out + _bytes(body)


def parse_response(raw, head_only):
    """Strict HTTP/1.1 response parse. Returns (status, sorted [(lower name, value)], body) or None."""
    end = raw.find(b'\r\n\r\n')
    if end < 0:
        return None
    lines, body = raw[:end].split(b'\r\n'), raw[end + 4:]
    status = _STATUS.fullmatch(lines[0])
    if not status:
        return None
    headers = []
    for line in lines[1:]:
        header = _HEADER.fullmatch(line)
        if not header:
            return None
        headers.append((header.group(1).lower(), header.group(2)))
    lengths = [value for name, value in headers if name == b'content-length']
    if len(lengths) != 1 or not _LENGTH.fullmatch(lengths[0]):
        return None
    if head_only:
        if body:
            return None
    elif len(body) != int(lengths[0]):
        return None
    return int(status.group(1)), sorted(headers), body


def observed_frame(kind, exit_status, stdout, stderr, head_only=False):
    if kind == 'cli':
        return _cli(exit_status, stdout, stderr)
    parsed = parse_response(stdout, head_only)
    if parsed is None:
        return b'http-v1\n' + _u32(exit_status) + _bytes(stderr) + b'\x00' + _bytes(stdout)
    return _http_parsed(exit_status, stderr, *parsed)


def observed_digest(kind, exit_status, stdout, stderr, head_only=False):
    return sha256(observed_frame(kind, exit_status, stdout, stderr, head_only))


def _exit(value):
    if type(value) is not int or not 0 <= value <= 255:
        raise Refused('Expected exit status must be an integer from 0 to 255.')
    return value


def expected_digest(kind, row):
    """Digest of an independently authored expected observable (the oracle in expected_observables.json)."""
    if kind == 'cli':
        keys = {'id', 'held_out', 'exit', 'stdout_latin1', 'stderr_latin1'}
        if not isinstance(row, dict) or set(row) != keys:
            raise Refused('A cli expected observable has the wrong fields.')
        return sha256(_cli(_exit(row['exit']), latin1(row['stdout_latin1'], 'stdout'),
                           latin1(row['stderr_latin1'], 'stderr')))
    keys = {'id', 'held_out', 'exit', 'stderr_latin1', 'status', 'headers', 'body_latin1'}
    if not isinstance(row, dict) or set(row) != keys:
        raise Refused('An http expected observable has the wrong fields.')
    status, headers = row['status'], row['headers']
    if type(status) is not int or not 100 <= status <= 599 or not isinstance(headers, list):
        raise Refused('An http expected observable has a bad status or headers.')
    pairs = []
    for pair in headers:
        if not isinstance(pair, list) or len(pair) != 2 or pair[0] != pair[0].lower():
            raise Refused('Expected headers are [lowercase name, value] pairs.')
        pairs.append((latin1(pair[0], 'header name'), latin1(pair[1], 'header value')))
    return sha256(_http_parsed(_exit(row['exit']), latin1(row['stderr_latin1'], 'stderr'), status, sorted(pairs),
                               latin1(row['body_latin1'], 'body')))


def valid_digest(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None
