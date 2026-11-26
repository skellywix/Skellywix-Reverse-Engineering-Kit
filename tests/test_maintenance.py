"""Tests for build_desk.maintenance with a fake Docker runner and a fake downloader. No network, no Docker."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_desk import maintenance as mt  # noqa: E402 - import needs the sys.path entry above

REPO = Path(__file__).resolve().parents[1]
UPSTREAM = ("example/kit", "main")
REF_URL = "https://api.github.com/repos/example/kit/git/ref/heads/main"
REMOTE_COMMIT = "b" * 40
RAW_URL = f"https://raw.githubusercontent.com/example/kit/{REMOTE_COMMIT}/workspace-dependencies.json"
REA_COMMIT = "a" * 40
REA_URL = f"https://codeload.github.com/example/rea/tar.gz/{REA_COMMIT}"
ACTION_SHA = "c" * 40


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(n: int) -> str:
    return f"sha256:{n:064x}"


def dump(value) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def rea_archive(package: bytes, *, extra: bool = True) -> bytes:
    root = f"rea-{REA_COMMIT}"
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        members = {"package.json": package, "package-lock.json": b"{}\n", ".agents/skills/x/SKILL.md": b"skill\n"}
        if extra:
            members["scripts/rea.mjs"] = b"process.exit(1)\n"  # must never be extracted
        for name, content in members.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


REA_PACKAGE = b'{"name":"rea","version":"1.0.0"}\n'


class FakeWorld:
    """Fake docker (runner) and fake downloads (fetch), recording every call."""

    def __init__(self):
        self.calls: list = []
        self.fail: list = []          # substrings; a docker call containing one fails
        self.missing: set = set()     # image IDs docker no longer has
        self.downloads: dict = {}     # url -> bytes | Exception
        self.versions = {"rustc": "rustc 1.100.0 (abc)", "cargo": "cargo 1.100.0 (abc)", "python3": "Python 3.13.15",
                         "node": "v24.22.3", "npm": "11.0.0"}

    def image_id(self, reference: str) -> str:
        return "sha256:" + sha(reference.encode())

    def runner(self, argv: list, timeout: int):
        self.calls.append(list(argv))
        joined = " ".join(map(str, argv))
        if any(item in joined for item in self.fail):
            return 1, "simulated failure"
        verb = argv[1]
        if verb == "version":
            return 0, "linux\n"
        if verb == "pull":
            self.missing.discard(self.image_id(argv[-1]))  # a pull restores an image that was removed
            return 0, ""
        if verb == "build":
            return 0, ""
        if verb == "image":
            target = argv[-1]
            if argv[4] == "{{.Id}}":
                return (1, "") if target in self.missing else (0, target)
            reference = target
            digests = [reference.split("/")[-1]] if "@" in reference else []
            return 0, f"{self.image_id(reference)}\t{json.dumps(digests)}\tlinux/amd64\n"
        if verb == "run":
            return self._run(argv)
        return 1, "unexpected docker call"

    def _run(self, argv: list):
        at = next(i for i, item in enumerate(argv) if re.fullmatch(r"sha256:[0-9a-f]{64}", item))
        command = [argv[argv.index("--entrypoint") + 1], *argv[at + 1:]]
        if command[0] in self.versions and command[1:] == ["--version"]:
            return 0, self.versions[command[0]] + "\n"
        if command[:2] == ["npm", "ci"]:
            mount = next(item for item in argv if item.startswith("type=bind") and "dst=/work" in item)
            work = Path(mount.split("src=", 1)[1].split(",", 1)[0])
            lock = json.loads((work / "package-lock.json").read_text())
            installed = {k: v for k, v in lock["packages"].items() if k}
            (work / "node_modules").mkdir()
            (work / "node_modules" / ".package-lock.json").write_text(json.dumps({"packages": installed}))
            (work / "node_modules" / "x.js").write_text("module.exports = 1\n")
            return 0, "added\n"
        return 1, "unexpected command"

    def fetch(self, url: str, destination: Path, limit: int) -> str:
        self.calls.append(["fetch", url])
        value = self.downloads.get(url)
        if value is None:
            raise mt.MaintenanceError(f"no fake download for {url}")
        if isinstance(value, Exception):
            raise value
        destination.write_bytes(value)
        return sha(value)

    def commands(self, name: str) -> list:
        return [c for c in self.calls if c[1] == "run" and name in c]


class Fixture:
    def __init__(self, case: unittest.TestCase):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.checkout, self.root = base / "checkout", base / "data"
        self.checkout.mkdir()
        self.root.mkdir()
        self.world = FakeWorld()
        self.write("Cargo.toml", b'[package]\nname = "w"\nversion = "0.1.0"\n')
        self.write("Cargo.lock", b'version = 4\n\n[[package]]\nname = "w"\nversion = "0.1.0"\n')
        self.write("frontend/package.json", b'{"name":"f","version":"1.0.0"}\n')
        self.set_frontend_lock(self.npm_lock("https://registry.npmjs.org/x/-/x-1.0.0.tgz"))
        self.write(".github/workflows/ci.yml", f"steps:\n  - uses: actions/checkout@{ACTION_SHA}\n".encode())
        self.manifest = self.base_manifest()
        self.world.downloads[REA_URL] = rea_archive(REA_PACKAGE)
        self.save()

    def write(self, relative: str, data: bytes) -> None:
        path = self.checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def npm_lock(resolved: str) -> dict:
        return {"lockfileVersion": 3, "packages": {
            "": {"name": "f", "version": "1.0.0"},
            "node_modules/x": {"version": "1.0.0", "resolved": resolved, "integrity": "sha512-AAAA"}}}

    def set_frontend_lock(self, lock: dict) -> None:
        self.write("frontend/package-lock.json", dump(lock))
        if hasattr(self, "manifest"):
            self.component("frontend")["lock_sha256"] = sha(dump(lock))
            self.save()

    def image(self, name: str, n: int, **extra) -> dict:
        python = name in ("python", "inspector-worker")
        node = name == "node"
        tool = "python3" if python else "node" if node else "rustc"
        expect = r"^Python 3\." if python else r"^v24\." if node else r"^rustc 1\.100\."
        return {"name": name, "family": "oci-image", "scope": "runtime", "key": name, "version": name + " test",
                "repository": f"docker.io/library/{name}", "tag": "t", "digest": digest(n),
                "verify": [{"argv": [tool, "--version"], "expect": expect}], **extra}

    def base_manifest(self) -> dict:
        lock = sha(_lf((self.checkout / "Cargo.lock").read_bytes()))
        return {"schema": 1, "revision": 1, "reviewed": True, "components": [
            self.image("compiler", 1), self.image("python", 2), self.image("node", 3),
            self.image("inspector-worker", 4, activation="restart"),
            {"name": "workspace-cargo", "family": "cargo-lock", "scope": "runtime", "version": "lock",
             "image": "compiler", "manifest": "Cargo.toml", "context": ".", "lock": "Cargo.lock", "lock_sha256": lock},
            {"name": "frontend", "family": "npm-lock", "scope": "runtime", "version": "npm", "image": "node",
             "manifest": "frontend/package.json", "manifest_sha256": sha(_lf((self.checkout / "frontend/package.json").read_bytes())),
             "lock": "frontend/package-lock.json",
             "lock_sha256": sha(_lf((self.checkout / "frontend/package-lock.json").read_bytes()))},
            {"name": "rea", "family": "reference-archive", "scope": "runtime", "version": "REA 1.0.0",
             "repository": "example/rea", "tag": "rea-agents-1.0.0", "commit": REA_COMMIT,
             "files": {"package.json": sha(REA_PACKAGE), "package-lock.json": sha(b"{}\n"),
                       ".agents/skills/x/SKILL.md": sha(b"skill\n")}},
            {"name": "reference-input", "family": "operator-asset", "scope": "runtime", "optional": True,
             "version": "operator file", "path": "inputs/reference", "sha256": "0" * 64},
            {"name": "ci-actions", "family": "ci-actions", "scope": "ci", "version": "actions",
             "workflows": ".github/workflows", "actions": {"actions/checkout": {"sha": ACTION_SHA, "version": "v1"}}},
        ]}

    def component(self, name: str, manifest: dict | None = None) -> dict:
        return next(c for c in (manifest or self.manifest)["components"] if c["name"] == name)

    def save(self, manifest: dict | None = None) -> None:
        self.write(mt.MANIFEST_NAME, dump(manifest or self.manifest))

    def bumped(self, **digests: int) -> dict:
        manifest = copy.deepcopy(self.manifest)
        manifest["revision"] += 1
        for name, n in digests.items():
            self.component(name, manifest)["digest"] = digest(n)
        return manifest

    def publish(self, manifest: dict | None, *, ref=None, raw=None) -> None:
        self.world.downloads[REF_URL] = ref if ref is not None else dump({"object": {"type": "commit", "sha": REMOTE_COMMIT}})
        self.world.downloads[RAW_URL] = raw if raw is not None else dump(manifest)

    def maintenance(self, upstream=None) -> mt.Maintenance:
        return mt.Maintenance(self.root, self.checkout, runner=self.world.runner, fetch=self.world.fetch,
                              docker="docker", upstream=upstream)


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


class ManifestValidation(unittest.TestCase):
    def check(self, mutate):
        manifest = Fixture(self).manifest
        mutate(manifest)
        with self.assertRaises(mt.ManifestError):
            mt.manifest_from_bytes(dump(manifest))

    def test_fixture_manifest_is_valid(self):
        self.assertEqual(1, mt.manifest_from_bytes(dump(Fixture(self).manifest)).revision)

    def test_unreviewed_manifest_is_refused(self):
        self.check(lambda m: m.update(reviewed=False))

    def test_unknown_family_duplicate_name_and_field(self):
        self.check(lambda m: m["components"][0].update(family="apt"))
        self.check(lambda m: m["components"].append(copy.deepcopy(m["components"][0])))
        self.check(lambda m: m["components"][0].update(surprise=1))

    def test_command_and_registry_are_allow_listed(self):
        self.check(lambda m: m["components"][0]["verify"][0].update(argv=["sh", "-c", "id"]))
        self.check(lambda m: m["components"][0].update(repository="evil.example/library/rust"))

    def test_reference_archive_carries_no_runtime_fields(self):
        self.check(lambda m: next(c for c in m["components"] if c["name"] == "rea").update(image="node"))
        self.check(lambda m: next(c for c in m["components"] if c["name"] == "rea").update(npm_lock="package-lock.json"))

    def test_runtime_and_ci_scopes_stay_separate(self):
        self.check(lambda m: next(c for c in m["components"] if c["name"] == "ci-actions").update(scope="runtime"))

    def test_duplicate_json_keys_are_refused(self):
        with self.assertRaises(mt.MaintenanceError):
            mt.manifest_from_bytes(b'{"schema":1,"schema":1}')

    def test_shipped_manifest_validates_and_checkout_pins_do_not_drift(self):
        manifest = mt.load_manifest(REPO / mt.MANIFEST_NAME)
        self.assertTrue(manifest.raw["reviewed"])
        m = mt.Maintenance(Path(tempfile.gettempdir()) / "unused-root-never-written", REPO, upstream=None, docker="")
        drift = [(c["name"], m._source(c)[1]) for c in manifest.components if m._source(c)[0] == "drift"]
        self.assertEqual([], drift)
        required_absent = [c["name"] for c in manifest.components
                           if c["family"] in mt.SOURCE_BOUND and not c["optional"] and m._source(c)[0] == "absent"]
        self.assertEqual([], required_absent)
        rea = manifest.by_name["rea"]
        self.assertEqual("reference-archive", rea["family"])
        self.assertTrue({"image", "npm_lock", "node_ranges", "scripts"}.isdisjoint(rea))


class Installation(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.m = self.fx.maintenance()

    def test_bootstrap_installs_and_selects_atomically(self):
        before = self.m.image_for("python")
        self.assertEqual(f"docker.io/library/python@{digest(2)}", before)
        result = self.m.bootstrap(wait=True)
        self.assertEqual("updated", result["state"], result["message"])
        selection = json.loads((self.fx.root / "dependencies" / "active.json").read_text())
        self.assertEqual(f"r1-{self.m._active_manifest(selection).sha256[:12]}", selection["set"])
        self.assertTrue(re.fullmatch(r"sha256:[0-9a-f]{64}", self.m.image_for("python")))
        self.assertNotEqual(before, self.m.image_for("python"))
        for key in ("compiler", "node", "inspector-worker"):
            self.assertTrue(self.m.image_for(key).startswith("sha256:"))
        self.assertEqual([], self.m.verify(deep=True, images=True))
        self.assertTrue((self.m.asset_path("frontend") / "node_modules" / "x.js").is_file())
        self.assertIsNone(self.m.asset_path("workspace-cargo"))  # no external crates in this lock: nothing vendored
        self.assertIsNone(self.m.asset_path("nope"))
        with self.assertRaises(mt.Unavailable):
            self.m.image_for("nope")
        status = self.m.status()
        self.assertEqual("updated", status["state"])
        self.assertEqual(["reference-input"], status["optional_absent"])
        self.assertFalse(status["restart_required"])
        self.assertEqual({"kind": "checkout"}, status["origin"])

    def test_bootstrap_refuses_once_installed_and_update_is_idempotent(self):
        self.m.bootstrap(wait=True)
        self.assertEqual("failed", self.m.bootstrap(wait=True)["state"])
        again = self.fx.maintenance().update(wait=True)
        self.assertEqual("current", again["state"], again["message"])
        self.assertEqual(1, len(list((self.fx.root / "dependencies" / "sets").iterdir())))

    def test_reference_archive_is_data_only(self):
        self.m.bootstrap(wait=True)
        reference = self.m.asset_path("rea")
        files = sorted(p.relative_to(reference).as_posix() for p in reference.rglob("*") if p.is_file())
        self.assertEqual([".agents/skills/x/SKILL.md", "package-lock.json", "package.json"], files)
        self.assertEqual(1, len(self.fx.world.commands("ci")), "npm ci ran for something other than the frontend lock")
        mounted = str(Path("components") / "rea")
        self.assertEqual([], [c for c in self.fx.world.calls if c[1:2] == ["run"] and mounted in " ".join(c)])
        receipt = json.loads((self.fx.root / "dependencies" / "sets" / self.m.status()["set"] / "receipt.json").read_text())
        self.assertEqual("reference", receipt["components"]["rea"]["role"])

    def test_reference_archive_digest_mismatch_fails_closed(self):
        self.fx.world.downloads[REA_URL] = rea_archive(b'{"name":"rea","version":"1.0.0","x":1}\n')
        result = self.m.bootstrap(wait=True)
        self.assertEqual("failed", result["state"])
        self.assertIn("does not match", result["message"])
        self.assertFalse((self.fx.root / "dependencies" / "active.json").exists())

    def test_failed_update_keeps_previous_selection_and_cases(self):
        self.m.bootstrap(wait=True)
        cases = self.fx.root / "cases"
        cases.mkdir()
        (cases / "one.json").write_bytes(b'{"case":1}')
        active = (self.fx.root / "dependencies" / "active.json").read_bytes()
        old = self.m.image_for("python")
        self.fx.save(self.fx.bumped(python=22))
        self.fx.world.fail.append(digest(22))
        result = self.fx.maintenance().update(wait=True)
        self.assertEqual("failed", result["state"])
        self.assertEqual(active, (self.fx.root / "dependencies" / "active.json").read_bytes())
        self.assertEqual(old, self.fx.maintenance().image_for("python"))
        self.assertEqual(b'{"case":1}', (cases / "one.json").read_bytes())
        self.assertEqual([], list((self.fx.root / "dependencies" / "staging").iterdir()))
        self.assertEqual(1, len(list((self.fx.root / "dependencies" / "sets").iterdir())))

    def test_promotion_flags_restart_for_a_loaded_process_only(self):
        self.m.bootstrap(wait=True)
        loaded = self.fx.maintenance()
        old = loaded.image_for("inspector-worker")
        self.fx.save(self.fx.bumped(**{"inspector-worker": 44}))
        before = loaded.status()
        self.assertEqual("available", before["state"])
        self.assertEqual("outdated", next(c for c in before["components"] if c["name"] == "inspector-worker")["state"])
        result = loaded.update(wait=True)
        self.assertEqual("updated", result["state"], result["message"])
        self.assertEqual(["inspector-worker"], result["restart"])
        self.assertTrue(result["restart_required"])
        self.assertIn("Restart required", result["message"])
        self.assertNotEqual(old, loaded.image_for("inspector-worker"))
        self.assertFalse(self.fx.maintenance().status()["restart_required"])
        self.assertEqual(2, len(list((self.fx.root / "dependencies" / "sets").iterdir())), "active and previous are kept")

    def test_update_never_downgrades_to_an_older_checkout(self):
        self.fx.save(self.fx.bumped(python=22))
        self.m.bootstrap(wait=True)
        installed = self.m.image_for("python")
        self.fx.save(self.fx.base_manifest())  # revision 1 again
        result = self.fx.maintenance().update(wait=True)
        self.assertEqual("current", result["state"], result["message"])
        self.assertEqual(2, result["installed_revision"])
        self.assertEqual(installed, self.m.image_for("python"))

    def test_unreviewed_checkout_manifest_is_not_activated(self):
        self.fx.manifest["reviewed"] = False
        self.fx.save()
        status = self.m.status()
        self.assertEqual("failed", status["state"])
        self.assertEqual("failed", self.m.update(wait=True)["state"])
        self.assertFalse((self.fx.root / "dependencies" / "active.json").exists())
        with self.assertRaises(mt.Unavailable):
            self.m.image_for("python")

    def test_required_pin_absent_fails_and_optional_absent_does_not(self):
        (self.fx.checkout / "Cargo.lock").unlink()
        result = self.m.bootstrap(wait=True)
        self.assertEqual("failed", result["state"])
        self.assertIn("workspace-cargo", result["message"])
        self.assertFalse((self.fx.root / "dependencies" / "active.json").exists())

    def test_operator_asset_present_is_verified_and_wrong_one_is_drift(self):
        data = b"operator-supplied"
        (self.fx.root / "inputs").mkdir()
        (self.fx.root / "inputs" / "reference").write_bytes(data)
        self.fx.component("reference-input")["sha256"] = sha(data)
        self.fx.save()
        m = self.fx.maintenance()
        self.assertEqual("verified", next(c for c in m.status()["components"] if c["name"] == "reference-input")["state"])
        (self.fx.root / "inputs" / "reference").write_bytes(b"changed")
        self.assertEqual("failed", m.update(wait=True)["state"])

    def test_ci_pin_drift_blocks_the_update(self):
        self.fx.write(".github/workflows/ci.yml", b"steps:\n  - uses: actions/checkout@" + b"d" * 40 + b"\n")
        status = self.m.status()
        self.assertEqual("failed", status["state"])
        self.assertIn("ci-actions", status["message"])
        self.assertEqual("failed", self.m.update(wait=True)["state"])
        self.assertFalse((self.fx.root / "dependencies" / "active.json").exists())

    def test_npm_lock_with_a_non_registry_dependency_is_rejected(self):
        self.fx.set_frontend_lock(self.fx.npm_lock("git+https://example.test/x.git#abc"))
        result = self.m.bootstrap(wait=True)
        self.assertEqual("failed", result["state"])
        self.assertIn("frontend", result["message"])
        self.assertEqual([], self.fx.world.commands("ci"))

    def test_changed_lock_without_a_new_pin_is_drift(self):
        self.fx.write("frontend/package-lock.json", b'{"lockfileVersion":3,"packages":{"":{}}}\n')
        self.assertEqual("failed", self.m.status()["state"])

    def test_missing_docker_is_a_prerequisite_failure(self):
        m = mt.Maintenance(self.fx.root, self.fx.checkout, runner=self.fx.world.runner, fetch=self.fx.world.fetch,
                           docker="", upstream=None)
        result = m.bootstrap(wait=True)
        self.assertEqual("failed", result["state"])
        self.assertIn("Docker", result["message"])
        self.assertEqual("missing", result["prerequisites"][0]["state"])

    def test_concurrent_update_is_refused(self):
        with mt._process_lock(self.fx.root / "dependencies" / "update.lock"):
            result = self.m.update(wait=True)
        self.assertEqual("failed", result["state"])
        self.assertIn("Another process", result["message"])

    def test_damaged_set_is_reported_and_repaired_by_update(self):
        self.m.bootstrap(wait=True)
        victim = self.m.asset_path("frontend") / "package.json"
        victim.write_bytes(b"tampered")
        problems = self.m.verify(deep=True)
        self.assertTrue(any("changed after install" in p for p in problems), problems)
        receipt = next((self.fx.root / "dependencies" / "sets").iterdir()) / "receipt.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        self.assertEqual("failed", self.fx.maintenance().status()["state"])
        self.assertEqual(f"docker.io/library/python@{digest(2)}", self.fx.maintenance().image_for("python"))
        self.assertIsNone(self.fx.maintenance().asset_path("frontend"))
        repaired = self.fx.maintenance().update(wait=True)
        self.assertEqual("updated", repaired["state"], repaired["message"])
        self.assertEqual([], self.fx.maintenance().verify(deep=True))

    def test_missing_image_is_reinstalled(self):
        self.m.bootstrap(wait=True)
        self.fx.world.missing.add(self.m.image_for("python"))
        self.assertTrue(any("not present" in p for p in self.m.verify(images=True)))
        result = self.fx.maintenance().update(wait=True)
        self.assertEqual("updated", result["state"], result["message"])
        self.assertEqual([], self.fx.maintenance().verify(images=True))


class Discovery(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.upstream = self.fx.maintenance(UPSTREAM)

    def record(self) -> dict:
        return json.loads((self.fx.root / "dependencies" / "discovery.json").read_text())

    def test_disabled(self):
        result = self.fx.maintenance(None).update(wait=True)
        self.assertEqual("updated", result["state"])
        self.assertEqual("disabled", self.record()["state"])
        self.assertEqual([], [c for c in self.fx.world.calls if c[0] == "fetch" and "github.com/repos" in c[1]])

    def test_unpublished_is_not_an_error(self):
        self.fx.publish(None, raw=mt.NotFound("none"))
        result = self.upstream.update(wait=True)
        self.assertEqual("updated", result["state"], result["message"])
        self.assertEqual("unpublished", self.record()["state"])

    def test_unavailable_bootstraps_from_checkout_when_nothing_is_installed(self):
        result = self.upstream.update(wait=True)  # no fake downloads: lookup fails
        self.assertEqual("updated", result["state"], result["message"])
        self.assertEqual("unavailable", self.record()["state"])
        self.assertIn("Could not check", result["message"])

    def test_unavailable_is_never_reported_as_current_once_installed(self):
        self.fx.maintenance().bootstrap(wait=True)
        result = self.upstream.update(wait=True)
        self.assertEqual("failed", result["state"])
        self.assertIn("Could not check", result["message"])
        self.assertEqual("unavailable", self.record()["state"])

    def test_unavailable_still_installs_a_newer_checkout_manifest(self):
        self.fx.maintenance().bootstrap(wait=True)
        self.fx.save(self.fx.bumped(python=22))
        result = self.upstream.update(wait=True)
        self.assertEqual("updated", result["state"], result["message"])
        self.assertEqual(2, result["installed_revision"])

    def test_malformed_repository_metadata_is_rejected(self):
        self.fx.publish(None, ref=b'{"object":{"type":"tag","sha":"x"}}', raw=b"{}")
        self.upstream.update(wait=True)
        self.assertEqual("rejected", self.record()["state"])

    def test_unreviewed_published_manifest_is_rejected(self):
        remote = self.fx.bumped(python=22)
        remote["reviewed"] = False
        self.fx.publish(remote)
        result = self.upstream.update(wait=True)
        self.assertEqual("rejected", self.record()["state"])
        self.assertEqual(1, result["installed_revision"])

    def test_same_revision_same_bytes_is_current(self):
        self.fx.publish(self.fx.manifest)
        self.upstream.update(wait=True)
        self.assertEqual("current", self.record()["state"])

    def test_older_publication_is_current(self):
        self.fx.save(self.fx.bumped())
        self.fx.publish(self.fx.base_manifest())
        self.upstream.update(wait=True)
        self.assertEqual("current", self.record()["state"])

    def test_same_revision_different_content_is_diverged_and_ignored(self):
        remote = copy.deepcopy(self.fx.manifest)
        self.fx.component("python", remote)["digest"] = digest(22)
        self.fx.publish(remote)
        result = self.upstream.update(wait=True)
        self.assertEqual("diverged", self.record()["state"])
        expected = mt.load_manifest(self.fx.checkout / mt.MANIFEST_NAME).sha256[:12]
        self.assertEqual(f"r1-{expected}", result["set"])

    def test_source_bound_change_needs_a_code_update_and_is_not_installed(self):
        remote = self.fx.bumped(python=22)
        self.fx.component("workspace-cargo", remote)["lock_sha256"] = "9" * 64
        self.fx.publish(remote)
        result = self.upstream.update(wait=True)
        self.assertEqual("needs-code-update", self.record()["state"])
        self.assertIn("workspace code update", self.record()["message"])
        self.assertEqual(1, result["installed_revision"])
        self.assertTrue(self.fx.maintenance().image_for("python").startswith("sha256:"), "installed from the checkout, not the unreviewed candidate")

    def test_newer_reviewed_manifest_is_installed_with_its_origin(self):
        remote = self.fx.bumped(python=22)
        self.fx.publish(remote)
        before = (self.fx.checkout / mt.MANIFEST_NAME).read_bytes()
        result = self.upstream.update(wait=True)
        self.assertEqual("updated", result["state"], result["message"])
        self.assertEqual("newer", self.record()["state"])
        self.assertEqual(2, result["installed_revision"])
        self.assertEqual({"kind": "remote", "repository": "example/kit", "commit": REMOTE_COMMIT}, result["origin"])
        self.assertIn(REMOTE_COMMIT[:12], result["message"])
        self.assertEqual(before, (self.fx.checkout / mt.MANIFEST_NAME).read_bytes(), "the checkout is never edited")
        self.assertEqual(dump(remote), next((self.fx.root / "dependencies" / "sets").iterdir()).joinpath("manifest.json").read_bytes())
        self.assertEqual(self.fx.world.image_id(f"docker.io/library/python@{digest(22)}"), self.upstream.image_for("python"))

    def test_redirect_and_url_shapes_are_fixed(self):
        self.fx.publish(self.fx.bumped(python=22))
        self.upstream.update(wait=True)
        urls = [c[1] for c in self.fx.world.calls if c[0] == "fetch"]
        self.assertEqual([REF_URL, RAW_URL, REA_URL], urls)


if __name__ == "__main__":
    unittest.main()
