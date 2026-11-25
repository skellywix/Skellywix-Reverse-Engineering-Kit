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

    def stage(self, *, omit_skill=False, prerelease=False, extra_member=None, version=None):
        blob = io.BytesIO()
        root = "rea-" + self.commit
        manifest = self.manifest | {"version": version or self.version}
        members = {
            "package.json": json.dumps(manifest).encode(),
            "package-lock.json": json.dumps(self.lock).encode(),
            "scripts/rea.mjs": self.entry,
            "scripts/generate-skill-metadata.mjs": self.entry,
        }
        if not omit_skill:
            members[".agents/skills/reverse-engineer-anything/SKILL.md"] = self.skill
        if extra_member:
            members[extra_member] = b"unexpected"
        with tarfile.open(fileobj=blob, mode="w:gz") as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(f"{root}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        source = blob.getvalue()
        tag = f"rea-agents-{self.version}"
        responses = {
            updater.GITHUB + "/repos/morluto/rea/releases/latest": json.dumps({
                "tag_name": tag, "draft": False, "prerelease": prerelease,
            }).encode(),
            updater.GITHUB + f"/repos/morluto/rea/git/ref/tags/{tag}": json.dumps({
                "object": {"type": "commit", "sha": self.commit},
            }).encode(),
            f"https://codeload.github.com/morluto/rea/tar.gz/{self.commit}": source,
        }

        def fetch(url, path, limit):
            path.write_bytes(responses[url])
            return hashlib.sha256(responses[url]).hexdigest()

        with TemporaryDirectory() as scratch:
            result = updater.stage_rea(Path(scratch), (6, 8, 0), fetch=fetch)
        return result, hashlib.sha256(source).hexdigest(), hashlib.sha256(members["package-lock.json"]).hexdigest()

    def test_receipt_links_one_github_archive_to_lock_and_skill(self):
        result, source_hash, lock_hash = self.stage()
        self.assertEqual(result["version"], self.version)
        self.assertEqual(result["source"]["sha256"], source_hash)
        self.assertEqual(result["lock"]["sha256"], lock_hash)
        self.assertEqual(result["skill"]["sha256"], hashlib.sha256(self.skill).hexdigest())
        self.assertEqual(result["source"]["url"], f"https://codeload.github.com/morluto/rea/tar.gz/{self.commit}")

    def test_archive_identifies_the_release_version(self):
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.stage(version="6.8.0")

    def test_archive_without_authored_skill_has_no_candidate(self):
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

    def test_prerelease_is_not_eligible(self):
        with self.assertRaisesRegex(ValueError, "stable GitHub release"):
            self.stage(prerelease=True)

class ActiveSourceTests(unittest.TestCase):
    def test_catalog_uses_only_active_product_inputs(self):
        names = {name for name, _stage, _inputs in updater.sources()}

        self.assertTrue({"rea-agents", "workbench-ui", "python:3.13-alpine"} <= names)
        self.assertTrue(any(name.startswith("action:actions/checkout@") for name in names))
        self.assertTrue(any(name.startswith("action:actions/setup-python@") for name in names))
        self.assertTrue(any(name.startswith("action:actions/upload-artifact@") for name in names))
        self.assertFalse({
            "binwalk", "node", "python-tools", "ubuntu-snapshot",
            "NationalSecurityAgency/ghidra", "radareorg/radare2",
        } & names)
