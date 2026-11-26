import base64
import hashlib
import inspect
import io
import json
import os
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fourmilab import releases
from tools import dependency_candidates as dep

KIT_COMMIT = "b" * 40
REA_COMMIT = "a" * 40
REA_VERSION = "6.9.0"
INSPECTOR_SOURCE = b"raise SystemExit('staging must not execute the inspector')\n"
SKILL_SOURCE = b"# Example skill\n"
INSPECTOR_URL = (f"https://raw.githubusercontent.com/{releases.KIT_REPOSITORY}/{KIT_COMMIT}/"
                 f"{releases.INSPECTOR_PATH}")


def rea_archive(*, version=REA_VERSION, skill=SKILL_SOURCE):
    manifest = {
        "name": "rea-agents", "version": version,
        "engines": {"node": "^22.23.4"}, "dependencies": {"sample": "^1.0.0"},
    }
    lock = {
        "name": "rea-agents", "version": version, "lockfileVersion": 3,
        "packages": {
            "": manifest.copy(),
            "node_modules/sample": {
                "integrity": "sha512-" + base64.b64encode(bytes(64)).decode(),
                "resolved": "https://registry.npmjs.org/sample/-/sample-1.0.0.tgz",
            },
        },
    }
    entry = b"throw new Error('staging must not execute JS');\n"
    members = {
        "package.json": json.dumps(manifest).encode(),
        "package-lock.json": json.dumps(lock).encode(),
        "scripts/rea.mjs": entry,
        "scripts/generate-skill-metadata.mjs": entry,
        dep.REA_SKILL: skill,
    }
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(f"rea-{REA_COMMIT}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return blob.getvalue()


def responses(*, prerelease=False, kit_type="commit", inspector=INSPECTOR_SOURCE, archive=None):
    tag = f"rea-agents-{REA_VERSION}"
    github = dep.GITHUB
    return {
        f"{github}/repos/{releases.KIT_REPOSITORY}/git/ref/heads/main": json.dumps(
            {"object": {"type": kit_type, "sha": KIT_COMMIT}}).encode(),
        INSPECTOR_URL: inspector,
        f"{github}/repos/morluto/rea/releases/latest": json.dumps(
            {"tag_name": tag, "draft": False, "prerelease": prerelease}).encode(),
        f"{github}/repos/morluto/rea/git/ref/tags/{tag}": json.dumps(
            {"object": {"type": "commit", "sha": REA_COMMIT}}).encode(),
        f"https://codeload.github.com/morluto/rea/tar.gz/{REA_COMMIT}": archive or rea_archive(),
    }


def fixture_fetch(table):
    def fetch(url, path, limit):
        content = table[url]  # an unexpected URL is a test failure
        if len(content) > limit:
            raise ValueError("download exceeded size limit")
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()
    return fetch


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name) / "snapshot"
        self.directory.mkdir()

    def stage(self, **kwargs):
        return releases._fetch_snapshot(self.directory, fixture_fetch(responses(**kwargs)))

    def staged(self):
        receipt = self.stage()
        # A receipt is stored as JSON between runs, so check what survives that trip.
        return json.loads(json.dumps(receipt))

    def assertRejected(self, receipt, message="."):
        with self.assertRaisesRegex(releases.SnapshotError, message):
            releases.check_snapshot(self.directory, receipt)

    def test_public_interface_accepts_only_a_destination(self):
        self.assertEqual(list(inspect.signature(releases.fetch_snapshot).parameters), ["destination"])

    def test_roundtrip_retains_exact_files_and_matching_receipt(self):
        receipt = self.stage()
        self.assertEqual(receipt["kit"], KIT_COMMIT)
        self.assertEqual(receipt["rea"], REA_VERSION)
        self.assertEqual(receipt["rea_commit"], REA_COMMIT)
        self.assertEqual({path.name for path in self.directory.iterdir()}, releases.FILES)
        self.assertEqual(receipt["files"].keys(), releases.FILES)
        for name, digest in receipt["files"].items():
            self.assertEqual(hashlib.sha256((self.directory / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(receipt["rea_source_sha256"], receipt["files"][releases.ARCHIVE])
        self.assertEqual((self.directory / releases.INSPECTOR).read_bytes(), INSPECTOR_SOURCE)
        self.assertEqual((self.directory / releases.SKILL).read_bytes(), SKILL_SOURCE)
        releases.check_snapshot(self.directory, receipt)
        releases.check_snapshot(str(self.directory), json.loads(json.dumps(receipt)))

    def test_retained_data_is_the_archive_content(self):
        self.stage()
        members = releases._rea_members(self.directory / releases.ARCHIVE, REA_COMMIT)
        for name, content in members.items():
            self.assertEqual((self.directory / name).read_bytes(), content)

    def test_destination_must_be_an_empty_directory(self):
        (self.directory / "stale").write_bytes(b"x")
        with self.assertRaisesRegex(releases.SnapshotError, "empty directory"):
            self.stage()
        self.assertEqual([path.name for path in self.directory.iterdir()], ["stale"])
        with self.assertRaisesRegex(releases.SnapshotError, "empty directory"):
            releases._fetch_snapshot(self.directory / "stale", fixture_fetch(responses()))

    def test_failed_staging_leaves_the_destination_empty(self):
        failures = (
            ({"prerelease": True}, "stable GitHub release"),
            ({"kit_type": "tag"}, "commit"),
            ({"archive": rea_archive(version="6.8.0")}, "manifest"),
        )
        for kwargs, message in failures:
            with self.subTest(kwargs=list(kwargs)):
                with self.assertRaisesRegex(ValueError, message):
                    self.stage(**kwargs)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_empty_inspector_is_rejected(self):
        with self.assertRaisesRegex(releases.SnapshotError, "invalid size"):
            self.stage(inspector=b"")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_oversized_inspector_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.stage(inspector=bytes(releases.INSPECTOR_LIMIT + 1))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_edited_file_is_rejected(self):
        receipt = self.staged()
        for name in sorted(releases.FILES):
            with self.subTest(name=name):
                path = self.directory / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assertRejected(receipt, name)
                path.write_bytes(original)
                releases.check_snapshot(self.directory, receipt)

    def test_same_size_edit_is_rejected(self):
        receipt = self.staged()
        path = self.directory / releases.INSPECTOR
        content = bytearray(path.read_bytes())
        content[0] ^= 1
        path.write_bytes(bytes(content))
        self.assertRejected(receipt, releases.INSPECTOR)

    def test_edit_with_a_rewritten_receipt_digest_still_fails_the_archive_tie(self):
        receipt = self.staged()
        edited = b"# Edited skill\n"
        (self.directory / releases.SKILL).write_bytes(edited)
        receipt["files"][releases.SKILL] = hashlib.sha256(edited).hexdigest()
        self.assertRejected(receipt, "archive")

    def test_missing_file_is_rejected(self):
        receipt = self.staged()
        for name in sorted(releases.FILES):
            with self.subTest(name=name):
                path = self.directory / name
                original = path.read_bytes()
                path.unlink()
                self.assertRejected(receipt, f"missing: {name}")
                path.write_bytes(original)
                releases.check_snapshot(self.directory, receipt)

    def test_unexpected_file_is_rejected(self):
        receipt = self.staged()
        (self.directory / "extra.txt").write_bytes(b"x")
        self.assertRejected(receipt, "unexpected: extra.txt")

    def test_directory_in_place_of_a_file_is_rejected(self):
        receipt = self.staged()
        path = self.directory / releases.LOCK
        path.unlink()
        path.mkdir()
        self.assertRejected(receipt, "not a regular file")

    def test_symlinked_file_is_rejected(self):
        receipt = self.staged()
        path = self.directory / releases.SKILL
        target = self.directory.parent / "elsewhere.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        os.symlink(target, path)
        self.assertRejected(receipt, "not a regular file")

    def test_symlinked_directory_is_rejected(self):
        receipt = self.staged()
        link = self.directory.parent / "link"
        os.symlink(self.directory, link, target_is_directory=True)
        with self.assertRaisesRegex(releases.SnapshotError, "real directory"):
            releases.check_snapshot(link, receipt)

    def test_unreadable_or_non_directory_snapshot_is_rejected(self):
        receipt = self.staged()
        with self.assertRaisesRegex(releases.SnapshotError, "not readable"):
            releases.check_snapshot(self.directory / "absent", receipt)
        with self.assertRaisesRegex(releases.SnapshotError, "not readable|real directory"):
            releases.check_snapshot(self.directory / releases.SKILL, receipt)

    def test_receipt_with_missing_or_extra_keys_is_rejected(self):
        receipt = self.staged()
        for key in receipt:
            with self.subTest(missing=key):
                self.assertRejected({k: v for k, v in receipt.items() if k != key}, "keys")
        self.assertRejected(receipt | {"extra": "x"}, "keys")

    def test_receipt_that_is_not_a_mapping_is_rejected(self):
        self.staged()
        for receipt in (None, [], "receipt", 0):
            with self.subTest(receipt=receipt):
                self.assertRejected(receipt, "keys")

    def test_receipt_file_set_must_match_exactly(self):
        receipt = self.staged()
        digest = receipt["files"][releases.SKILL]
        for name in sorted(releases.FILES):
            with self.subTest(missing=name):
                files = {k: v for k, v in receipt["files"].items() if k != name}
                self.assertRejected(receipt | {"files": files}, "exactly the snapshot files")
        self.assertRejected(receipt | {"files": receipt["files"] | {"extra.txt": digest}},
                            "exactly the snapshot files")
        self.assertRejected(receipt | {"files": []}, "exactly the snapshot files")

    def test_invalid_digest_claims_are_rejected(self):
        receipt = self.staged()
        good = receipt["files"][releases.SKILL]
        for claim in (good.upper(), good[:-1], good + "0", "g" * 64, "", None, 0, [good]):
            with self.subTest(claim=claim):
                files = receipt["files"] | {releases.SKILL: claim}
                self.assertRejected(receipt | {"files": files}, "invalid file digest")
        for claim in (good.upper(), "0" * 63, None):
            with self.subTest(source=claim):
                self.assertRejected(receipt | {"rea_source_sha256": claim}, "rea_source_sha256")

    def test_wrong_but_well_formed_digest_is_rejected(self):
        receipt = self.staged()
        files = receipt["files"] | {releases.INSPECTOR: "0" * 64}
        self.assertRejected(receipt | {"files": files}, releases.INSPECTOR)

    def test_source_digest_must_equal_the_archive_digest(self):
        receipt = self.staged()
        self.assertRejected(receipt | {"rea_source_sha256": "0" * 64}, "source digest")

    def test_invalid_commit_and_version_claims_are_rejected(self):
        receipt = self.staged()
        bad = {"kit": ("B" * 40, "b" * 39, "b" * 41, None, 7),
               "rea_commit": ("A" * 40, "a" * 39, None),
               "rea": ("6.9", "v6.9.0", "6.9.0-rc1", None)}
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertRejected(receipt | {key: value}, f"receipt {key} is invalid")

    def test_receipt_for_another_release_is_rejected(self):
        receipt = self.staged()
        self.assertRejected(receipt | {"rea": "6.8.0"}, "identity")
        self.assertRejected(receipt | {"rea_commit": "c" * 40}, "identity")


if __name__ == "__main__":
    unittest.main()
