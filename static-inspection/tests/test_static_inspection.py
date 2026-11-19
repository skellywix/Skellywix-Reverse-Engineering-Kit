"""Exercise the local inspector with inert data and real filesystem objects."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


CLI = Path(__file__).resolve().parents[1] / "inspect_file.py"
ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class StaticInspection(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="static-inspection-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.intake = self.root / "intake"
        self.intake.mkdir()
        self.sample = self.intake / "sample.bin"
        self.sample.write_bytes(b"abc")

    def invoke(self, *args, isolated=True):
        flags = ["-I", "-B"] if isolated else ["-B"]
        return subprocess.run([sys.executable, *flags, str(CLI), *args], cwd=self.intake,
                              capture_output=True, timeout=8, check=False)

    def assert_rejected(self, *args, code=1, isolated=True):
        result = self.invoke(*args, isolated=isolated)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.strip())
        return result.stderr

    def test_known_digest_and_exact_report(self):
        self.sample.chmod(0o755)  # Execute permission does not change the inspection.
        result = self.invoke(self.sample.name, ABC)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"version": 1, "size": 3, "sha256": ABC, "signature": "unknown"})
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.sample.read_bytes(), b"abc")
        self.assertEqual(list(self.intake.iterdir()), [self.sample])

    def test_empty_file(self):
        self.sample.write_bytes(b"")
        result = self.invoke(self.sample.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"version": 1, "size": 0, "sha256": EMPTY, "signature": "unknown"})

    def test_hashes_past_first_chunk_without_disclosing_sample_or_name(self):
        self.sample = self.intake / "private-name.bin"
        content = b"%PDF-private-evidence" + b"\0" * (1024 * 1024 + 17)
        self.sample.write_bytes(content)
        result = self.invoke(self.sample.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"version": 1, "size": len(content),
                         "sha256": hashlib.sha256(content).hexdigest(), "signature": "pdf"})
        self.assertNotIn(b"private", result.stdout + result.stderr)

    def test_signatures_describe_prefixes_not_validated_formats(self):
        for content, label in ((b"MZ", "mz"), (b"\x7fELF", "elf"), (b"PK\x03\x04", "zip"),
                              (b"\x89PNG\r\n\x1a\n", "png"), (b"\x7fEL", "unknown")):
            with self.subTest(label=label):
                self.sample.write_bytes(content)
                result = self.invoke(self.sample.name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["signature"], label)

    def test_expected_digest_is_enforced_before_reporting(self):
        self.assert_rejected(self.sample.name, "0" * 64)
        self.assertNotIn(b"private", self.assert_rejected(self.sample.name, "private", code=2))

    def test_single_basename_cannot_escape_intake(self):
        outside = self.root / "private-outside"
        outside.write_bytes(b"abc")
        (self.intake / "subdir").mkdir()
        (self.intake / "subdir/item").write_bytes(b"abc")
        for existing in ("bad\\name", "bad\nname"):
            (self.intake / existing).write_bytes(b"abc")
        for bad in ("../private-outside", str(outside), "subdir/item", "bad\\name", "bad\nname"):
            with self.subTest(name=repr(bad)):
                self.assertNotIn(os.fsencode(bad), self.assert_rejected(bad))

    def test_rejects_symbolic_links_and_multiply_linked_regular_files(self):
        outside = self.root / "outside"
        outside.write_bytes(b"abc")
        (self.intake / "link").symlink_to(outside)
        self.assert_rejected("link")  # Its target is still a valid single-link regular file.
        os.link(outside, self.intake / "hardlink")
        self.assert_rejected("hardlink")

    def test_rejects_folder_fifo_and_missing_file_without_blocking(self):
        (self.intake / "folder").mkdir()
        os.mkfifo(self.intake / "fifo")
        for name in ("folder", "fifo", "missing"):
            with self.subTest(name=name):
                self.assert_rejected(name)

    def test_rejects_sparse_oversize_before_reading_it(self):
        with self.sample.open("wb") as handle:
            handle.truncate(2 * 1024 ** 3 + 1)
        self.assert_rejected(self.sample.name)

    def test_refuses_nonisolated_interpreter(self):
        self.assert_rejected(self.sample.name, code=2, isolated=False)

    def test_growth_during_inspection_cannot_publish_an_early_digest(self):
        spec = importlib.util.spec_from_file_location("tested_inspector", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        read = os.read
        changed = False

        def append_after_read(fd, size):
            nonlocal changed
            data = read(fd, size)
            if data and not changed:
                with self.sample.open("ab") as writer:
                    writer.write(b"d")
                changed = True
            return data

        saved = Path.cwd()
        self.addCleanup(os.chdir, saved)
        os.chdir(self.intake)
        with patch.object(os, "read", append_after_read), self.assertRaises(module.InspectionError):
            module.inspect_file(self.sample.name)
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
