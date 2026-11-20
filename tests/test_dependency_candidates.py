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

    def stage(self, *, omit_skill=False, prerelease=False, dockerfile=None,
              extra_member=None, version=None):
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
            if dockerfile is None:
                result = updater.stage_rea(Path(scratch), (6, 8, 0), fetch=fetch)
            else:
                result = updater.update_rea(Path(scratch), dockerfile, fetch=fetch)
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

    def test_update_changes_source_version_and_digest_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            dockerfile = Path(directory) / "Dockerfile"
            before = self.dockerfile()
            dockerfile.write_bytes(before.encode())
            _, source_hash, _ = self.stage(dockerfile=dockerfile)
            expected = (before.replace("6.8.0", self.version)
                        .replace("b" * 40, self.commit)
                        .replace("c" * 64, source_hash))
            self.assertEqual(dockerfile.read_bytes(), expected.encode())
            result, _, _ = self.stage(dockerfile=dockerfile)
            self.assertIsNone(result)
            self.assertEqual(dockerfile.read_bytes(), expected.encode())

    def test_failed_update_leaves_original_pins(self):
        for failure, message in (
            ({"version": "6.8.0"}, "manifest"),
            ({"extra_member": "npm-shrinkwrap.json"}, "unsafe"),
            ({"extra_member": "../../escape"}, "unsafe"),
        ):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                dockerfile = Path(directory) / "Dockerfile"
                before = self.dockerfile()
                dockerfile.write_bytes(before.encode())
                with self.assertRaisesRegex(ValueError, message):
                    self.stage(dockerfile=dockerfile, **failure)
                self.assertEqual(dockerfile.read_bytes(), before.encode())

    @staticmethod
    def dockerfile():
        return "\r\n".join((
            "# unrelated",
            "# REA source release 6.8.0",
            "RUN fetch-verified https://codeload.github.com/morluto/rea/tar.gz/" + "b" * 40 + " \\",
            "      " + "c" * 64 + " /tmp/rea-source.tgz \\",
            " && mkdir /opt/rea \\",
            "",
        ))


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
