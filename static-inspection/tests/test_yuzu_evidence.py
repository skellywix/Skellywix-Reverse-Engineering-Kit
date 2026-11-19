"""Fixed consumer oracles for a byte-only inspection case."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workbench'))
import evidence


ABC = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
EMPTY = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'


class EvidenceTests(unittest.TestCase):
    def test_known_file_and_fixed_fields(self):
        self.assertEqual(evidence.summarize(b'abc'), {'size': 3, 'sha256': ABC,
                         'format': 'unknown', 'architecture': 'unknown'})

    def test_header_architecture_without_extracting_text(self):
        header = bytearray(64)
        header[:7] = b'\x7fELF\x02\x01\x01'
        header[16:24] = b'\x03\x00\x3e\x00\x01\x00\x00\x00'
        header[52:54] = b'\x40\x00'
        row = evidence.summarize(bytes(header) + b'private-content-never-a-command')
        self.assertEqual((row['format'], row['architecture']), ('elf', 'x86-64'))
        self.assertNotIn('private-content', json.dumps(row))
        self.assertEqual(evidence.summarize(b'\x7fELF')['architecture'], 'unknown')

    def test_comparison_checksum_and_denial(self):
        slots = {'a': evidence.summarize(b'abc'), 'b': evidence.summarize(b'')}
        self.assertEqual(evidence.analyze('identify_file', {'slot': 'a'}, slots)['sha256'], ABC)
        self.assertEqual(evidence.analyze('verify_checksum', {'slot': 'b', 'sha256': ABC}, slots),
                         {'matches': False, 'actual': EMPTY, 'expected': ABC})
        self.assertTrue(evidence.analyze('verify_checksum', {'slot': 'a', 'sha256': ABC}, slots)['matches'])
        self.assertEqual(evidence.analyze('compare_builds', {}, slots),
                         {'identical': False, 'size_delta': -3, 'format_changed': False, 'architecture_changed': False})
        for tool, args in [('run_command', {}), ('identify_file', {'path': '/etc/passwd'}),
                           ('identify_file', {'slot': 'a', 'raw': True}), ('verify_checksum', {'slot':'a','sha256':'XYZ'}),
                           ('compare_builds', {'a': 'a'}), ('identify_file', {'slot': '../a'})]:
            with self.subTest(tool=tool, args=args), self.assertRaises(ValueError):
                evidence.analyze(tool, args, slots)
        with self.assertRaises(ValueError):
            evidence.analyze('compare_builds', {}, {'b':slots['b']})

    def test_discovery_contains_only_the_closed_case_tools(self):
        self.assertEqual({r['name'] for r in evidence.TOOL_SCHEMAS},
                         {'identify_file', 'verify_checksum', 'compare_builds', 'export_report'})
        with self.assertRaises(ValueError):
            evidence.summarize(bytes(evidence.MAX_SAMPLE + 1))


if __name__ == '__main__':
    unittest.main()
