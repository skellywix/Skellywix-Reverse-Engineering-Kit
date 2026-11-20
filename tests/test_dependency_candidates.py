import base64
import hashlib
import io
import json
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import dependency_candidates as updater


class ReaCandidateTests(unittest.TestCase):
    def setUp(self):
        self.version = "6.9.0"
        self.commit = "a" * 40
        self.manifest = {
            "name": "rea-agents",
            "version": self.version,
            "engines": {"node": "^22.23.4"},
            "dependencies": {"sample": "^1.0.0"},
        }
        self.lock = {
            "name": "rea-agents",
            "version": self.version,
            "lockfileVersion": 3,
            "packages": {
                "": self.manifest.copy(),
                "node_modules/sample": {
                    "integrity": "sha512-" + base64.b64encode(bytes(64)).decode(),
                    "resolved": "https://registry.npmjs.org/sample/-/sample-1.0.0.tgz",
                },
            },
        }
        self.skill = b"# Example skill\n"
        self.entry = b"throw new Error('staging must not execute JS');\n"

    def stage(self, *, omit_skill=False, corrupt_integrity=False):
        blob = io.BytesIO()
        members = {
            "package/package.json": json.dumps(self.manifest).encode(),
            "package/scripts/rea.mjs": self.entry,
        }
        if not omit_skill:
            members["package/" + updater.REA_SKILL] = self.skill
        with tarfile.open(fileobj=blob, mode="w:gz") as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        package = blob.getvalue()
        tarball = updater.NPM + f"/rea-agents/-/rea-agents-{self.version}.tgz"
        source = f"https://raw.githubusercontent.com/morluto/rea/{self.commit}/"
        integrity = hashlib.sha512(package if not corrupt_integrity else b"wrong").digest()
        lock = json.dumps(self.lock).encode()
        responses = {
            updater.NPM + "/rea-agents/latest": json.dumps({
                **self.manifest,
                "dist": {"tarball": tarball, "integrity": "sha512-" + base64.b64encode(integrity).decode()},
            }).encode(),
            tarball: package,
            updater.GITHUB + f"/repos/morluto/rea/git/ref/tags/rea-agents-{self.version}": json.dumps({
                "object": {"type": "commit", "sha": self.commit},
            }).encode(),
            source + "package-lock.json": lock,
        }

        def fetch(url, path, limit):
            path.write_bytes(responses[url])
            return hashlib.sha256(responses[url]).hexdigest()

        with TemporaryDirectory() as scratch:
            result = updater.stage_rea(Path(scratch), (6, 8, 0), fetch=fetch)
        return result, hashlib.sha256(package).hexdigest(), hashlib.sha256(lock).hexdigest()

    def test_receipt_links_verified_package_lock_and_bundled_skill(self):
        result, package_hash, lock_hash = self.stage()
        self.assertEqual(result["version"], self.version)
        self.assertEqual(result["package"]["sha256"], package_hash)
        self.assertEqual(result["lock"]["sha256"], lock_hash)
        self.assertEqual(result["skill"]["sha256"], hashlib.sha256(self.skill).hexdigest())
        self.assertIn(self.commit, result["lock"]["url"])

    def test_integrity_failure_has_no_candidate(self):
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.stage(corrupt_integrity=True)

    def test_package_without_bundled_skill_has_no_candidate(self):
        with self.assertRaisesRegex(ValueError, "missing.*skill"):
            self.stage(omit_skill=True)

    def test_lock_from_another_version_has_no_candidate(self):
        self.lock["version"] = "6.8.0"
        with self.assertRaisesRegex(ValueError, "lock.*manifest"):
            self.stage()

    def test_dependency_from_another_registry_has_no_candidate(self):
        self.lock["packages"]["node_modules/sample"]["resolved"] = "https://example.invalid/sample.tgz"
        with self.assertRaisesRegex(ValueError, "registry"):
            self.stage()


class ReleaseCandidateTests(unittest.TestCase):
    def test_only_matching_newer_stable_asset_is_eligible(self):
        def release(tag, asset, *, prerelease=False):
            return {"tag_name": tag, "prerelease": prerelease, "assets": [{"name": asset}]}

        selected = updater.match_release(
            [release("v2.0.0", "tool-2.0.0-linux-amd64.tar.gz"),
             release("v1.4.0", "tool-1.4.0-linux-arm64.tar.gz"),
             release("v1.3.0", "tool-1.3.0-linux-amd64.tar.gz", prerelease=True),
             release("v1.2.0", "tool-1.2.0-linux-amd64.tar.gz")],
            r"v(\d+\.\d+\.\d+)", "tool-{version}-linux-amd64.tar.gz", (1, 1, 0), major=1,
        )
        self.assertEqual(selected, ("1.2.0", "v1.2.0", {"name": "tool-1.2.0-linux-amd64.tar.gz"}))
