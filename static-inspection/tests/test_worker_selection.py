"""A rejected standalone image must not replace the last usable offline worker."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'workbench' / 'worker-image.sh'
A = 'sha256:' + 'a1' * 32
B = 'sha256:' + 'b2' * 32


@unittest.skipUnless(sys.platform == 'linux' and os.getuid() != 0, 'requires a non-root Linux account')
class WorkerSelection(unittest.TestCase):
    def test_rejected_refresh_preserves_offline_selection_until_another_candidate_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            docker = Path(temp) / 'docker'
            docker.write_text('''#!/bin/sh
case "$1" in
  pull) test "$REGISTRY" = online ;;
  image) for ref do :; done; case "$ref" in sha256:*) echo "$ref";; python:*) echo "$CANDIDATE";; *) exit 2;; esac ;;
  run) test "$PROBE" = ok ;;
  *) exit 2 ;;
esac
''', encoding='utf-8')
            docker.chmod(0o700)

            def select(*args, candidate=A, registry='online', probe='ok'):
                env = dict(os.environ, PATH=temp + os.pathsep + os.environ['PATH'], XDG_STATE_HOME=temp,
                           CANDIDATE=candidate, REGISTRY=registry, PROBE=probe)
                return subprocess.run(['bash', str(SCRIPT), *args], env=env, text=True, capture_output=True, timeout=10)

            initial = select()
            self.assertEqual((0, A), (initial.returncode, initial.stdout.strip()), initial.stderr)
            self.assertNotEqual(0, select('--refresh', candidate=B, probe='broken').returncode)
            self.assertNotEqual(0, select('--refresh', candidate=B, registry='offline').returncode)
            cached = select(candidate=B, registry='offline')
            self.assertEqual((0, A), (cached.returncode, cached.stdout.strip()), cached.stderr)
            updated = select('--refresh', candidate=B)
            self.assertEqual((0, B), (updated.returncode, updated.stdout.strip()), updated.stderr)
            self.assertEqual(B, select(candidate=A, registry='offline').stdout.strip())
