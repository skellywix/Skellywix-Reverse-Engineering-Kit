import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workbench'))
from evidence import summarize


def elf_object(order, wide, machine):
    # A relocatable object header without program or section tables.
    ident = b'\x7fELF' + bytes((2 if wide else 1, 1 if order == '<' else 2, 1)) + bytes(9)
    layout = 'HHI' + ('QQQ' if wide else 'III') + 'IHHHHHH'
    return ident + struct.pack(order + layout, 1, machine, 1, 0, 0, 0, 0,
                               64 if wide else 52, 0, 0, 0, 0, 0)


def pe64():
    # One zero-filled data section, PE32+ optional header and no directories.
    image = bytearray(512)
    image[:2] = b'MZ'
    struct.pack_into('<I', image, 60, 128)
    struct.pack_into('<4sHHIIIHH', image, 128, b'PE\0\0', 0x8664, 1, 0, 0, 0, 112, 0x22)
    opt = 152
    struct.pack_into('<H', image, opt, 0x20b)
    struct.pack_into('<QII', image, opt + 24, 0x140000000, 4096, 512)
    struct.pack_into('<H', image, opt + 48, 6)
    struct.pack_into('<II', image, opt + 56, 8192, 512)
    struct.pack_into('<H', image, opt + 68, 3)
    struct.pack_into('<8sII', image, opt + 112, b'.bss', 16, 4096)
    struct.pack_into('<I', image, opt + 112 + 36, 0xc0000080)
    return bytes(image)


class HeaderEvidenceTests(unittest.TestCase):
    def test_pe_image(self):
        meta = summarize(pe64())
        self.assertEqual(('pe', 'x86-64'), (meta['format'], meta['architecture']))

    def test_pe_requires_in_bounds_headers(self):
        original = pe64()
        for end in (2, 63, 131, 134, 151, 263, 303, 304, 511):
            with self.subTest(end=end):
                self.assertEqual('unknown', summarize(original[:end])['architecture'])
        for offset, field, value in ((60, 'I', 510), (60, 'I', 0xffffff80),
                                     (134, 'H', 2000), (148, 'H', 480), (152, 'H', 0x10b),
                                     (212, 'I', 303)):
            image = bytearray(original)
            struct.pack_into('<' + field, image, offset, value)
            with self.subTest(offset=offset, value=value):
                self.assertEqual('unknown', summarize(bytes(image))['architecture'])

    def test_elf_byte_orders_and_widths(self):
        for order, wide, machine, label in (('<', True, 62, 'x86-64'),
                                             ('>', False, 40, 'arm')):
            with self.subTest(label=label):
                meta = summarize(elf_object(order, wide, machine))
                self.assertEqual(('elf', label), (meta['format'], meta['architecture']))

    def test_forged_and_partial_elf(self):
        header = elf_object('<', True, 62)
        invalid = [header[:4], header[:20], header[:63]]
        for offset, field, value in ((5, 'B', 0), (6, 'B', 2), (20, 'I', 0), (52, 'H', 52),
                                     (62, 'H', 1), (62, 'H', 0xffff)):
            changed = bytearray(header)
            struct.pack_into('<' + field, changed, offset, value)
            invalid.append(bytes(changed))
        changed = bytearray(header)
        struct.pack_into('<Q', changed, 32, 4096)
        struct.pack_into('<HH', changed, 54, 56, 1)
        invalid.append(bytes(changed))
        for index, sample in enumerate(invalid):
            with self.subTest(index=index):
                meta = summarize(sample)
                self.assertEqual(('elf', 'unknown'), (meta['format'], meta['architecture']))
