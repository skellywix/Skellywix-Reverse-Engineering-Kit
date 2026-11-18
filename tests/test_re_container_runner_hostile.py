"""Runner against a hostile OMP: planted out/ entries never redirect, block, or skip its writes.

OMP runs as the runner's uid, so a prompt-injected model can leave links, directories or FIFOs
at the names the runner writes after OMP exits. The fake OMP below plants them; every run must
still end with a valid status.json and report.md, and nothing outside out/ may change.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

CONTAINER = Path(__file__).resolve().parents[1] / "container"
JOB = "job-20261116-0003"
GUARDED = ("report.md", "status.json", "status.json.tmp")
KINDS = ("directory", "dangling-link", "self-link", "outside-link", "sibling-link", "fifo")
needs_posix = unittest.skipUnless(
    os.name == "posix" and hasattr(os, "mkfifo"), "requires POSIX symlinks and FIFOs")


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "re_container_runner_hostile", CONTAINER / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOSTILE_OMP = textwrap.dedent("""
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    os.makedirs(args[args.index("--session-dir") + 1], exist_ok=True)
    with open(os.path.join(here, "plant.json"), encoding="utf-8") as spec_file:
        plants = json.load(spec_file)
    for plant in plants:
        path = os.path.join("out", plant["name"])
        if os.path.lexists(path):
            os.unlink(path)
        kind = plant["kind"]
        if kind == "directory":
            os.mkdir(path)
            with open(os.path.join(path, "inner.txt"), "w") as inner:
                inner.write("planted")
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "forged-status":
            with open(path, "w") as forged:
                json.dump({"job_id": json.load(open("in/job.json"))["job_id"],
                           "status": "cost-cap", "exit_code": "\\u001b[2J\\u001b]0;pwn\\u0007",
                           "model": "x" * 3000, "started": [], "ended": {}}, forged)
        else:
            os.symlink(plant["target"], path)
    sys.stdout.write("FINAL ANSWER\\n")
""")


def snapshot(root, skip):
    """Every entry under `root` except the `skip` subtrees: kind plus content or link target."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        relative_dir = os.path.relpath(dirpath, root)
        kept = []
        for name in sorted(dirnames + filenames):
            relative = os.path.normpath(os.path.join(relative_dir, name))
            if relative in skip:
                continue
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                entries[relative] = ("link", os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = ("file", Path(path).read_bytes())
            elif stat.S_ISDIR(info.st_mode):
                entries[relative] = ("dir",)
                kept.append(name)
            else:
                entries[relative] = ("other", stat.S_IFMT(info.st_mode))
        dirnames[:] = [name for name in dirnames if name in kept]
    return entries


@needs_posix
class HostileOutputTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(stack.close)
        self.runner = load_runner()

    def make_tree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        job = root / "job"
        for name in ("in/target", "out", "session"):
            (job / name).mkdir(parents=True)
        (job / "in" / "target" / "sample.bin").write_bytes(b"sample")
        (job / "in" / "question.md").write_text("Analyse it.", encoding="utf-8")
        (job / "in" / "job.json").write_text(json.dumps({"job_id": JOB}), encoding="utf-8")
        (root / "outside").mkdir()
        (root / "outside" / "existing.txt").write_text("ORIGINAL", encoding="utf-8")
        (root / "fake_omp.py").write_text(HOSTILE_OMP, encoding="utf-8")
        (root / "prompt.md").write_text("SYSTEM PROMPT", encoding="utf-8")
        profile = root / "profile-src"
        profile.mkdir()
        (profile / "config.yml").write_text("update:\n  auto: false\n", encoding="utf-8")
        (profile / "mcp.json").write_text("{}", encoding="utf-8")
        return root, job

    def plant_target(self, root, name, kind):
        return {
            "dangling-link": str(root / "outside" / "dangling-target"),
            "self-link": name,
            "outside-link": str(root / "outside" / "existing.txt"),
            "sibling-link": "omp.stdout",
        }.get(kind)

    def run_planted(self, plants):
        root, job = self.make_tree()
        specs = [{"name": name, "kind": kind, "target": self.plant_target(root, name, kind)}
                 for name, kind in plants]
        (root / "plant.json").write_text(json.dumps(specs), encoding="utf-8")
        skip = {os.path.join("job", "out"), os.path.join("job", "session"), "home"}
        before = snapshot(root, skip)
        result = self.runner.run_job(
            job, JOB, omp_cmd=(sys.executable, str(root / "fake_omp.py")),
            prompt_path=root / "prompt.md", profile_src=root / "profile-src",
            home=root / "home")
        self.assertEqual(snapshot(root, skip), before, "nothing outside out/ may change")
        return root, job, specs, result

    def assert_valid_outputs(self, job, result):
        out = job / "out"
        status = out / "status.json"
        self.assertTrue(stat.S_ISREG(os.lstat(status).st_mode))
        body = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(body, result)
        self.assertEqual((body["job_id"], body["status"], body["exit_code"]), (JOB, "done", 0))
        self.assertNotIn("error", body)
        report = out / "report.md"
        self.assertTrue(stat.S_ISREG(os.lstat(report).st_mode))
        text = report.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(
            "# Report: incomplete\nThe agent did not write out/report.md; status: done.\n"))
        self.assertIn("FINAL ANSWER", text)
        self.assertEqual((out / "omp.stdout").read_bytes(), b"FINAL ANSWER\n",
                         "nothing is written through a link to OMP's own output")

    def assert_quarantined(self, job, spec):
        pattern = re.compile(re.escape(f".quarantine-{spec['name']}-") + r"[0-9a-f]{32}")
        aside = [entry for entry in os.listdir(job / "out") if pattern.fullmatch(entry)]
        self.assertEqual(len(aside), 1, os.listdir(job / "out"))
        info = os.lstat(job / "out" / aside[0])
        if spec["kind"] == "directory":
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual((job / "out" / aside[0] / "inner.txt").read_text(), "planted")
        elif spec["kind"] == "fifo":
            self.assertTrue(stat.S_ISFIFO(info.st_mode))
        else:
            self.assertTrue(stat.S_ISLNK(info.st_mode), "the link itself is moved, not followed")
            self.assertEqual(os.readlink(job / "out" / aside[0]), spec["target"])

    def test_each_planted_entry_at_each_guarded_name(self):
        for name in GUARDED:
            for kind in KINDS:
                with self.subTest(name=name, kind=kind):
                    _root, job, specs, result = self.run_planted([(name, kind)])
                    self.assert_valid_outputs(job, result)
                    self.assert_quarantined(job, specs[0])

    def test_every_guarded_name_planted_at_once(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                _root, job, specs, result = self.run_planted([(name, kind) for name in GUARDED])
                self.assert_valid_outputs(job, result)
                for spec in specs:
                    self.assert_quarantined(job, spec)

    def test_forged_status_and_blocked_temporary_cannot_survive_a_clean_exit(self):
        _root, job, _specs, result = self.run_planted(
            [("status.json", "forged-status"), ("status.json.tmp", "directory")])
        self.assert_valid_outputs(job, result)
        self.assertNotIn("\x1b", (job / "out" / "status.json").read_text(encoding="utf-8"))

    def test_replaced_stdout_path_neither_hides_nor_redirects_the_final_output(self):
        for kind in ("outside-link", "dangling-link", "directory"):
            with self.subTest(kind=kind):
                _root, job, _specs, result = self.run_planted([("omp.stdout", kind)])
                self.assertEqual(result["status"], "done")
                report = (job / "out" / "report.md").read_text(encoding="utf-8")
                self.assertIn("## Final agent output (unstructured)\n\nFINAL ANSWER\n", report)

    def test_stale_irregular_status_before_launch_is_moved_aside(self):
        root, job = self.make_tree()
        (job / "out" / "status.json").mkdir()
        os.symlink(root / "outside" / "existing.txt", job / "out" / "report.md")
        (root / "plant.json").write_text("[]", encoding="utf-8")
        result = self.runner.run_job(
            job, JOB, omp_cmd=(sys.executable, str(root / "fake_omp.py")),
            prompt_path=root / "prompt.md", profile_src=root / "profile-src",
            home=root / "home")
        self.assertEqual(result["status"], "done")
        self.assertEqual((root / "outside" / "existing.txt").read_text(), "ORIGINAL")
        self.assertTrue(stat.S_ISREG(os.lstat(job / "out" / "status.json").st_mode))
        self.assertTrue(stat.S_ISREG(os.lstat(job / "out" / "report.md").st_mode))


if __name__ == "__main__":
    unittest.main()
