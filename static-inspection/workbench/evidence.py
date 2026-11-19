"""Bounded file identification and actions over retained metadata only."""

import hashlib
import re
from struct import unpack_from


MAX_SAMPLE = 16 * 1024 * 1024
_UNKNOWN = 'unknown'
_ELF_MACHINES = {3: 'x86', 40: 'arm', 62: 'x86-64', 183: 'arm64'}
_PE_MACHINES = {0x14c: 'x86', 0x1c0: 'arm', 0x1c2: 'arm', 0x1c4: 'arm',
                0x8664: 'x86-64', 0xaa64: 'arm64'}
_PREFIXES = ((b'%PDF-', 'pdf'), (b'\x89PNG\r\n\x1a\n', 'png'),
             (b'PK\x03\x04', 'zip'), (b'PK\x05\x06', 'zip'), (b'PK\x07\x08', 'zip'))


_Buffer = bytes | bytearray


def _elf_architecture(data: _Buffer) -> str:
    if len(data) < 16 or data[4] not in (1, 2) or data[5] not in (1, 2) or data[6] != 1:
        return _UNKNOWN
    wide = data[4] == 2
    size = 64 if wide else 52
    if len(data) < size:
        return _UNKNOWN
    order = '<' if data[5] == 1 else '>'
    layout = 'HHI' + ('QQQ' if wide else 'III') + 'IHHHHHH'
    kind, machine, version, _, phoff, shoff, _, ehsize, phent, phnum, shent, shnum, shstr = (
        unpack_from(order + layout, data, 16))
    if version != 1 or ehsize != size or kind not in (1, 2, 3, 4) or phnum == 0xffff:
        return _UNKNOWN
    if shnum >= 0xff00 or shstr and shstr >= shnum:
        return _UNKNOWN
    if wide and machine in (3, 40):
        return _UNKNOWN
    if data[5] == 2 and machine in (3, 62):
        return _UNKNOWN
    # Check declared table extents without reading any table entries. Extended
    # numbering needs section data, so leave those uncommon cases unidentified.
    for offset, stride, count, expected in ((phoff, phent, phnum, 56 if wide else 32),
                                            (shoff, shent, shnum, 64 if wide else 40)):
        if count == 0:
            if offset != 0:
                return _UNKNOWN
        elif stride != expected or offset < size or offset + stride * count > len(data):
            return _UNKNOWN
    return _ELF_MACHINES.get(machine, _UNKNOWN)


def _dos_identity(data: _Buffer) -> tuple[str, str]:
    if len(data) < 64:
        return 'mz', _UNKNOWN
    offset, = unpack_from('<I', data, 60)
    if offset < 64 or not data.startswith(b'PE\0\0', offset):
        return 'mz', _UNKNOWN
    optional = offset + 24  # signature and complete COFF file header
    if optional > len(data):
        return 'pe', _UNKNOWN
    machine, sections = unpack_from('<HH', data, offset + 4)
    opt_size, = unpack_from('<H', data, offset + 20)
    section_end = optional + opt_size + 40 * sections
    if not 1 <= sections <= 96 or opt_size < 2 or section_end > len(data):
        return 'pe', _UNKNOWN
    magic, = unpack_from('<H', data, optional)
    arch = _PE_MACHINES.get(machine, _UNKNOWN)
    wide = arch in ('x86-64', 'arm64')
    fixed_size = 112 if wide else 96
    if opt_size < fixed_size or magic != (0x20b if wide else 0x10b):
        return 'pe', _UNKNOWN
    header_size, = unpack_from('<I', data, optional + 60)
    if not section_end <= header_size <= len(data):
        return 'pe', _UNKNOWN
    directories, = unpack_from('<I', data, optional + fixed_size - 4)
    if fixed_size + directories * 8 > opt_size:
        return 'pe', _UNKNOWN
    return 'pe', arch


def summarize(data: _Buffer) -> dict:
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_SAMPLE:
        raise ValueError('Invalid sample')
    fmt = arch = _UNKNOWN
    if data.startswith(b'\x7fELF'):
        fmt, arch = 'elf', _elf_architecture(data)
    elif data.startswith(b'MZ'):
        fmt, arch = _dos_identity(data)
    else:
        for prefix, label in _PREFIXES:
            if data.startswith(prefix):
                fmt = label
                break
    return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'format': fmt, 'architecture': arch}


_SLOT = {'type': 'string', 'enum': ['a', 'b']}
_DIGEST = {'type': 'string', 'minLength': 64, 'maxLength': 64, 'pattern': '^[0-9a-fA-F]{64}$'}
_ACTIONS = {
    'identify_file': ('Return the selected sample’s identity metadata.', {'slot': _SLOT}),
    'verify_checksum': ('Compare the selected sample with an expected SHA-256.',
                        {'slot': _SLOT, 'sha256': _DIGEST}),
    'compare_builds': ('Compare build B with build A.', {}),
    'export_report': ('Return the current case report.', {}),
}
TOOL_SCHEMAS = [
    {'name': name, 'description': description,
     'inputSchema': {'type': 'object', 'properties': properties, 'required': list(properties),
                     'additionalProperties': False}}
    for name, (description, properties) in _ACTIONS.items()
]


def validate_arguments(tool: str, arguments: dict) -> None:
    if not isinstance(tool, str) or tool not in _ACTIONS:
        raise ValueError('Unknown tool')
    if not isinstance(arguments, dict) or arguments.keys() != _ACTIONS[tool][1].keys():
        raise ValueError('Invalid arguments')
    if 'slot' in arguments and arguments['slot'] not in ('a', 'b'):
        raise ValueError('Invalid slot')
    if 'sha256' in arguments:
        digest = arguments['sha256']
        if not isinstance(digest, str) or len(digest) != 64 or not re.fullmatch('[0-9a-fA-F]{64}', digest):
            raise ValueError('Invalid checksum')


def analyze(tool: str, arguments: dict, slots: dict) -> dict:
    validate_arguments(tool, arguments)
    if tool == 'export_report':
        return {}  # The service adds case history and constructs the report.
    if tool == 'compare_builds':
        if 'a' not in slots or 'b' not in slots:
            raise ValueError('Both samples required')
        a, b = slots['a'], slots['b']
        return {'identical': a['sha256'] == b['sha256'] and a['size'] == b['size'],
                'size_delta': b['size'] - a['size'], 'format_changed': a['format'] != b['format'],
                'architecture_changed': a['architecture'] != b['architecture']}
    if arguments['slot'] not in slots:
        raise ValueError('Sample required')
    meta = slots[arguments['slot']]
    if tool == 'identify_file':
        return meta.copy()
    expected = arguments['sha256'].lower()
    return {'matches': meta['sha256'] == expected, 'actual': meta['sha256'], 'expected': expected}
