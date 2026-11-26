import hashlib
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import workspace_release as releases


class WorkspaceReleaseTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.release_root = self.scratch / "release"

    @staticmethod
    def _identity(path):
        content = path.read_bytes()
        return {
            "path": path.name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def candidate(self, name, *, state_files=None):
        directory = self.scratch / name
        directory.mkdir()
        files = {
            "shell_binary": directory / "workspace-shell",
            "frontend_manifest": directory / "frontend-manifest.json",
            "capability_image": directory / "capability-image.oci",
        }
        files["shell_binary"].write_bytes(f"shell:{name}".encode())
        files["frontend_manifest"].write_bytes(f'{{"release":"{name}"}}\n'.encode())
        files["capability_image"].write_bytes(f"image:{name}".encode())

        state_files = state_files or {
            "database": [(directory / "workspace.sqlite", f"database:{name}".encode())],
            "artifacts": [(directory / "artifact.bin", f"artifact:{name}".encode())],
            "runtime_state": [(directory / "runtime.state", f"runtime:{name}".encode())],
        }
        state_snapshot = {}
        for group, entries in state_files.items():
            state_snapshot[group] = []
            for path, content in entries:
                path.write_bytes(content)
                state_snapshot[group].append(self._identity(path))

        manifest = {
            "schema_version": 1,
            **{key: self._identity(path) for key, path in files.items()},
            "legacy_engines": {
                "fourmilab": f"fourmilab:{name}",
                "inspector": f"inspector:{name}",
            },
            "state_snapshot": state_snapshot,
        }
        manifest_path = directory / "candidate.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path, files, state_files

    def stage(self, name):
        candidate, _, state = self.candidate(name)
        return releases.stage(self.release_root, candidate), state

    def probe(self, target_id, *, steps=None):
        return releases.probe_plan(
            self.release_root,
            target_id,
            {
                "result": "succeeded",
                "steps": steps or [{"action": "probe", "component": "workspace-shell"}],
            },
        )

    @staticmethod
    def run_cli(*arguments):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(releases.__file__)),
                *(str(argument) for argument in arguments),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    @staticmethod
    def receipt_snapshot(root):
        snapshot = {}
        for directory_name in ("targets", "probes", "backups"):
            directory = root / directory_name
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*.json")):
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
        return snapshot

    def promoted_fixture(self, name):
        root = self.scratch / f"{name}-release"
        candidate, _, _ = self.candidate(name)
        target = releases.stage(root, candidate)
        releases.probe_plan(
            root,
            target["target_id"],
            {"result": "succeeded", "steps": [{"action": "probe", "component": "shell"}]},
        )
        releases.promote(root, target["target_id"])
        self.assertEqual(releases.status(root)["ledger_records"], 4)
        return root, candidate, target

    def test_pristine_writer_lock_blocks_cli_stage_without_initializing_layout(self):
        candidate, _, _ = self.candidate("bootstrap")

        with releases.release_lock(self.release_root):
            blocked = self.run_cli("stage", self.release_root, candidate)
            self.assertEqual(blocked.returncode, releases.EXIT_LOCKED, blocked.stderr)
            self.assertEqual(
                {path.name for path in self.release_root.iterdir()},
                {"release.lock"},
            )
            self.assertFalse((self.release_root / "active").exists())
            self.assertFalse((self.release_root / "previous").exists())

        staged = self.run_cli("stage", self.release_root, candidate)
        self.assertEqual(staged.returncode, releases.EXIT_OK, staged.stderr)
        self.assertTrue((self.release_root / "active").is_file())
        self.assertTrue((self.release_root / "previous").is_file())
        self.assertEqual(len(list((self.release_root / "targets").glob("*.json"))), 1)

    def test_precreated_empty_root_is_read_only_until_stage(self):
        root = self.scratch / "empty-release"
        absent = self.run_cli("status", root)
        self.assertEqual(absent.returncode, releases.EXIT_OK, absent.stderr)
        self.assertFalse(root.exists())
        root.mkdir()

        status_result = self.run_cli("status", root)

        self.assertEqual(status_result.returncode, releases.EXIT_OK, status_result.stderr)
        self.assertEqual(list(root.iterdir()), [])

        candidate, _, _ = self.candidate("empty-candidate")
        stage_result = self.run_cli("stage", root, candidate)
        self.assertEqual(stage_result.returncode, releases.EXIT_OK, stage_result.stderr)
        self.assertEqual(len(list((root / "targets").glob("*.json"))), 1)

    def test_status_and_stage_do_not_recreate_missing_initialized_selectors(self):
        for selector_name in releases.SELECTORS:
            with self.subTest(selector=selector_name):
                root = self.scratch / f"missing-{selector_name}"
                candidate, _, _ = self.candidate(f"candidate-missing-{selector_name}")
                target = releases.stage(root, candidate)
                receipt_path = root / "targets" / f"{target['target_id']}.json"
                receipt_bytes = receipt_path.read_bytes()
                missing = root / selector_name
                missing.unlink()

                status_result = self.run_cli("status", root)
                stage_result = self.run_cli("stage", root, candidate)

                self.assertEqual(status_result.returncode, releases.EXIT_CONFLICT)
                self.assertEqual(stage_result.returncode, releases.EXIT_CONFLICT)
                self.assertFalse(missing.exists())
                self.assertEqual(receipt_path.read_bytes(), receipt_bytes)

    def test_history_loss_never_recreates_ledger_or_marker(self):
        for damaged_name in ("ledger.jsonl", releases.LAYOUT_MARKER):
            with self.subTest(damaged=damaged_name):
                fixture = damaged_name.removesuffix(".jsonl").removesuffix(".json")
                root, candidate, target = self.promoted_fixture(f"lost-{fixture}")
                selectors = {
                    name: (root / name).read_bytes()
                    for name in releases.SELECTORS
                }
                receipts = self.receipt_snapshot(root)
                damaged = root / damaged_name
                saved = self.scratch / f"{fixture}.saved"
                damaged.replace(saved)

                status_result = self.run_cli("status", root)
                stage_result = self.run_cli("stage", root, candidate)

                self.assertEqual(
                    (status_result.returncode, stage_result.returncode),
                    (releases.EXIT_CONFLICT, releases.EXIT_CONFLICT),
                )
                self.assertFalse(damaged.exists())
                self.assertEqual(
                    {name: (root / name).read_bytes() for name in releases.SELECTORS},
                    selectors,
                )
                self.assertEqual(self.receipt_snapshot(root), receipts)

                saved.replace(damaged)
                releases.rollback(
                    root,
                    target["target_id"],
                    confirm_target=target["target_id"],
                )
                self.assertIsNone(releases.status(root)["active"])

    def test_missing_writer_lock_never_creates_a_second_lock_domain(self):
        root, candidate, target = self.promoted_fixture("lost-lock")
        selectors = {name: (root / name).read_bytes() for name in releases.SELECTORS}
        receipts = self.receipt_snapshot(root)
        lock_path = root / "release.lock"
        saved_lock = self.scratch / "release.lock.saved"
        lock_path.replace(saved_lock)

        status_result = self.run_cli("status", root)
        self.assertEqual(status_result.returncode, releases.EXIT_CONFLICT)
        self.assertFalse(lock_path.exists())

        stage_result = self.run_cli("stage", root, candidate)

        self.assertEqual(stage_result.returncode, releases.EXIT_CONFLICT)
        self.assertFalse(lock_path.exists())
        self.assertEqual(
            {name: (root / name).read_bytes() for name in releases.SELECTORS},
            selectors,
        )
        self.assertEqual(self.receipt_snapshot(root), receipts)
        saved_lock.replace(lock_path)
        releases.rollback(
            root,
            target["target_id"],
            confirm_target=target["target_id"],
        )


    def test_stage_records_exact_candidate_identity(self):
        candidate, files, state = self.candidate("green")

        receipt = releases.stage(self.release_root, candidate)

        target_path = self.release_root / "targets" / f"{receipt['target_id']}.json"
        self.assertEqual(json.loads(target_path.read_text(encoding="utf-8")), receipt)
        self.assertEqual(receipt["legacy_engines"]["fourmilab"], "fourmilab:green")
        self.assertEqual(receipt["legacy_engines"]["inspector"], "inspector:green")
        for name, path in files.items():
            self.assertEqual(receipt[name]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        for group, entries in state.items():
            self.assertEqual(
                [entry["sha256"] for entry in receipt["state_snapshot"][group]],
                [hashlib.sha256(path.read_bytes()).hexdigest() for path, _ in entries],
            )

    def test_stage_refuses_recorded_digest_that_does_not_match_file(self):
        candidate, files, _ = self.candidate("bad-digest")
        files["shell_binary"].write_bytes(b"substituted after receipt")

        with self.assertRaisesRegex(releases.DigestMismatchError, "shell_binary") as raised:
            releases.stage(self.release_root, candidate)

        self.assertEqual(raised.exception.exit_code, releases.EXIT_DIGEST_MISMATCH)
        targets = self.release_root / "targets"
        self.assertFalse(targets.exists() and any(targets.iterdir()))

    def test_promote_requires_successful_probe_for_exact_target(self):
        target, _ = self.stage("unprobed")

        with self.assertRaisesRegex(releases.ProbeRequiredError, target["target_id"]) as raised:
            releases.promote(self.release_root, target["target_id"])

        self.assertEqual(raised.exception.exit_code, releases.EXIT_PROBE_REQUIRED)
        self.assertIsNone(releases.status(self.release_root)["active"])

    def test_atomic_switch_crash_between_selectors_leaves_valid_active_selector(self):
        blue, _ = self.stage("blue")
        self.probe(blue["target_id"])
        releases.promote(self.release_root, blue["target_id"])
        green, _ = self.stage("green")
        self.probe(green["target_id"])

        def crash(point):
            if point == "after_previous":
                raise RuntimeError("simulated power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            releases.promote(self.release_root, green["target_id"], fault_hook=crash)

        interrupted = releases.status(self.release_root)
        self.assertEqual(interrupted["active"], blue["target_id"])
        self.assertEqual(interrupted["previous"], blue["target_id"])
        self.assertTrue((self.release_root / "targets" / f"{interrupted['active']}.json").is_file())

        releases.promote(self.release_root, green["target_id"])
        releases.rollback(
            self.release_root,
            green["target_id"],
            confirm_target=green["target_id"],
        )
        restored = releases.status(self.release_root)
        self.assertEqual(restored["active"], blue["target_id"])
        self.assertIsNone(restored["previous"])

    def test_promote_and_rollback_lock_contention_fail_closed(self):
        target, _ = self.stage("locked")
        self.probe(target["target_id"])

        with releases.release_lock(self.release_root):
            with self.assertRaises(releases.LockContendedError) as promote_error:
                releases.promote(self.release_root, target["target_id"])
            with self.assertRaises(releases.LockContendedError) as rollback_error:
                releases.rollback(
                    self.release_root,
                    target["target_id"],
                    confirm_target=target["target_id"],
                )

        self.assertEqual(promote_error.exception.exit_code, releases.EXIT_LOCKED)
        self.assertEqual(rollback_error.exception.exit_code, releases.EXIT_LOCKED)

    def test_rollback_requires_exact_active_target_confirmation(self):
        blue, _ = self.stage("blue-confirm")
        self.probe(blue["target_id"])
        releases.promote(self.release_root, blue["target_id"])
        green, _ = self.stage("green-confirm")
        self.probe(green["target_id"])
        releases.promote(self.release_root, green["target_id"])

        with self.assertRaisesRegex(releases.ConfirmationError, "exact target") as raised:
            releases.rollback(
                self.release_root,
                green["target_id"],
                confirm_target=blue["target_id"],
            )

        self.assertEqual(raised.exception.exit_code, releases.EXIT_CONFIRMATION)
        self.assertEqual(releases.status(self.release_root)["active"], green["target_id"])
        releases.rollback(
            self.release_root,
            green["target_id"],
            confirm_target=green["target_id"],
        )
        self.assertEqual(releases.status(self.release_root)["active"], blue["target_id"])

    def test_rollback_preserves_data_created_after_promotion(self):
        blue, _ = self.stage("blue-data")
        self.probe(blue["target_id"])
        releases.promote(self.release_root, blue["target_id"])
        green, state = self.stage("green-data")
        self.probe(green["target_id"])
        database = state["database"][0][0]
        before_promotion = database.read_bytes()
        promotion = releases.promote(self.release_root, green["target_id"])
        backup_root = self.release_root / "backups" / promotion["operation_id"]
        backup_manifest = json.loads((backup_root / "manifest.json").read_text(encoding="utf-8"))
        database_backup = backup_manifest["state_snapshot"]["database"][0]
        self.assertEqual(database_backup["path"], str(database))
        self.assertEqual(database_backup["size"], len(before_promotion))
        self.assertEqual(
            database_backup["sha256"],
            hashlib.sha256(before_promotion).hexdigest(),
        )
        self.assertEqual(
            (backup_root / database_backup["backup_path"]).read_bytes(),
            before_promotion,
        )
        database.write_bytes(before_promotion + b":post-promotion")
        new_case = database.parent / "case-created-after-promotion.bin"
        new_case.write_bytes(b"new case")

        releases.rollback(
            self.release_root,
            green["target_id"],
            confirm_target=green["target_id"],
        )

        self.assertTrue(database.read_bytes().endswith(b":post-promotion"))
        self.assertEqual(new_case.read_bytes(), b"new case")

    def test_repeated_promote_is_idempotent(self):
        target, _ = self.stage("repeat")
        self.probe(target["target_id"])
        first = releases.promote(self.release_root, target["target_id"])
        backups_before = sorted(path.name for path in (self.release_root / "backups").iterdir())
        ledger_before = (self.release_root / "ledger.jsonl").read_bytes()

        second = releases.promote(self.release_root, target["target_id"])

        self.assertEqual(second["operation_id"], first["operation_id"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(
            sorted(path.name for path in (self.release_root / "backups").iterdir()),
            backups_before,
        )
        self.assertEqual((self.release_root / "ledger.jsonl").read_bytes(), ledger_before)

    def test_probe_plan_refuses_legacy_engine_restart_steps(self):
        target, _ = self.stage("legacy")
        for engine in ("fourmilab", "inspector"):
            with self.subTest(engine=engine):
                with self.assertRaisesRegex(releases.LegacyRestartError, engine) as raised:
                    self.probe(
                        target["target_id"],
                        steps=[{"action": "restart", "engine": engine}],
                    )
                self.assertEqual(raised.exception.exit_code, releases.EXIT_LEGACY_RESTART)

        probes = self.release_root / "probes" / target["target_id"]
        self.assertFalse(probes.exists())

    def test_cli_status_prints_json_and_specific_errors_return_specific_codes(self):
        candidate, files, _ = self.candidate("cli-bad")
        files["frontend_manifest"].write_bytes(b"changed")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = releases.main(["status", str(self.release_root)])
        self.assertEqual(code, releases.EXIT_OK)
        self.assertIsNone(json.loads(stdout.getvalue())["active"])

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = releases.main(["stage", str(self.release_root), str(candidate)])
        self.assertEqual(code, releases.EXIT_DIGEST_MISMATCH)
        self.assertIn("frontend_manifest", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
