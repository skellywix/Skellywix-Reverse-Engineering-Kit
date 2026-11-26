"""A structural check that a compiled candidate needs no dynamic loader.

The candidate-only runtime holds no files, so a program that names a loader (a PT_INTERP header) or is not a plain
little-endian x86-64 executable cannot start there. This reads only the ELF program-header table of bytes the verifier
already holds; it never runs them.
"""
import struct

LITTLE_ENDIAN_ELF64 = b'\x7fELF\x02\x01'
PT_LOAD = 1
PT_INTERP = 3
ET_EXEC = 2
ET_DYN = 3
EM_X86_64 = 62
HEADER_SIZE = 64
PROGRAM_HEADER_SIZE = 56
PROGRAM_HEADER_LIMIT = 64


def static_problem(data):
    """None for a little-endian x86-64 ELF64 executable with loadable segments and no interpreter; else a fixed phrase."""
    if len(data) < HEADER_SIZE or not data.startswith(LITTLE_ENDIAN_ELF64):
        return 'The compiler produced no little-endian ELF64 program.'
    kind, machine = struct.unpack_from('<HH', data, 16)
    phoff, = struct.unpack_from('<Q', data, 32)
    entry_size, count = struct.unpack_from('<HH', data, 54)
    if kind not in (ET_EXEC, ET_DYN) or machine != EM_X86_64:
        return 'The compiled program is not an x86-64 executable.'
    if entry_size != PROGRAM_HEADER_SIZE or not 0 < count <= PROGRAM_HEADER_LIMIT:
        return 'The compiled program has a malformed program-header table.'
    if phoff < HEADER_SIZE or phoff + entry_size * count > len(data):
        return 'The compiled program has a malformed program-header table.'
    types = [struct.unpack_from('<I', data, phoff + entry_size * index)[0] for index in range(count)]
    if PT_INTERP in types:
        return 'The compiled program is dynamically linked: it names a loader, and the candidate runtime has none.'
    if PT_LOAD not in types:
        return 'The compiled program has no loadable segment.'
    return None
