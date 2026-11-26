"""Non-live contract for tools/prepare_fourmilab.py.

No Docker, network, provider, model, or original program is involved: a stand-in file replaces the pinned hash for
the duration of each test, and the Docker answers come from a fake runner.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fourmilab import builder, contract, pipeline, sandbox
from tools import prepare_fourmilab as prepare

CHECKOUT = Path(__file__).resolve().parents[1]
STAND_IN = b'\x7fELF stand-in bytes, not the Fourmilab program\n'
HOST = 'desk-host'


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@unittest.skipUnless(sys.platform.startswith('linux'), 'private /tmp candidate directories and Unix modes are Linux-only')
class PrepareDirectory(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.original = self.scratch / 'supplied'
        self.original.write_bytes(STAND_IN)
        pin = patch.object(contract, 'REFERENCE_SHA256', hashlib.sha256(STAND_IN).hexdigest())
        pin.start()
        self.addCleanup(pin.stop)

    def prepared(self):
        root = prepare.prepare(self.original, HOST, check_runtime=False)['root']
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def refused(self, path, pattern):
        with patch.object(prepare, 'new_root', side_effect=AssertionError('a directory was created before refusal')):
            with self.assertRaisesRegex(ValueError, pattern):
                prepare.prepare(path, HOST, check_runtime=False)

    def test_exact_original_is_installed_read_only_in_a_private_directory(self):
        root = self.prepared()
        self.assertRegex(str(root), r'^/tmp/fourmilab-compat\.[A-Za-z0-9]{8}$')
        self.assertEqual(mode(root), 0o700)
        self.assertEqual(set(os.listdir(root)), {'app', 'connection.json', 'reference'})
        reference = root / 'reference'
        self.assertEqual(reference.read_bytes(), STAND_IN)
        self.assertEqual(mode(reference), 0o555)
        self.assertFalse(reference.is_symlink())
        sandbox.check_reference(reference)  # the desk's own gate

    def test_descriptor_is_what_the_broker_and_companion_validate(self):
        root = self.prepared()
        descriptor, digest = builder.connection(root)
        self.assertEqual(descriptor, {'host': HOST, 'directory': str(root)})
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        self.assertEqual(mode(root / 'connection.json'), 0o400)

    def test_desk_accepts_the_prepared_directory(self):
        root = self.prepared()
        broker = SimpleNamespace(model=builder.MODEL, version='fixed', connection_hash='0' * 64)
        desk = pipeline.Desk(root, broker)
        self.assertIsNone(desk.store.releases()['active'], 'a new directory has no release and no run')
        self.assertEqual(desk.store.summaries(), [])

    def test_code_copy_is_byte_identical_and_runs_from_its_own_directory(self):
        root = self.prepared()
        app = root / 'app'
        for name in prepare.CODE:
            copy = app / 'fourmilab' / name
            self.assertEqual(copy.read_bytes(), (CHECKOUT / 'fourmilab' / name).read_bytes(), name)
            self.assertEqual(mode(copy), 0o444, name)
        tool = app / 'tools' / prepare.TOOL
        self.assertEqual(tool.read_bytes(), (CHECKOUT / 'tools' / prepare.TOOL).read_bytes())
        probe = ('import json; from fourmilab import builder, channel, desk, pipeline; '
                 'print(json.dumps([pipeline.HERE.as_posix(), pipeline.Desk.disk_code()], sort_keys=True))')
        env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        done = subprocess.run([sys.executable, '-B', '-c', probe], cwd=app, env=env, capture_output=True,
                              text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        here, code_hashes = json.loads(done.stdout)
        self.assertEqual(here, (app.resolve() / 'fourmilab').as_posix())
        self.assertEqual(code_hashes, pipeline.Desk.disk_code(), 'the desk would see the same pinned coordinator code')

    def test_every_desk_file_is_copied(self):
        folder = CHECKOUT / 'fourmilab'
        files = {path.name for path in folder.iterdir() if path.is_file()} - {'README.md'}
        self.assertEqual(files, set(prepare.CODE), 'a file added to or removed from fourmilab/ must update the installer')
        self.assertLessEqual({path.name for path in folder.iterdir() if path.is_dir()}, {'__pycache__'})

    def test_each_preparation_is_a_fresh_directory(self):
        first = self.prepared()
        (first / 'runs').mkdir()
        (first / 'runs' / 'stale-receipt').write_text('previous attempt')
        second = self.prepared()
        self.assertNotEqual(first, second)
        self.assertEqual(set(os.listdir(second)), {'app', 'connection.json', 'reference'})

    def test_anything_but_the_pinned_original_is_refused_before_a_directory_exists(self):
        wrong = self.scratch / 'wrong'
        wrong.write_bytes(STAND_IN + b'x')
        empty = self.scratch / 'empty'
        empty.write_bytes(b'')
        link = self.scratch / 'link'
        link.symlink_to(self.original)
        fifo = self.scratch / 'fifo'
        os.mkfifo(fifo)
        for name, path, pattern in [('wrong bytes', wrong, 'not the pinned'), ('empty', empty, 'non-empty'),
                                    ('directory', self.scratch, 'regular file|Cannot open'), ('symlink', link, 'symbolic link'),
                                    ('fifo', fifo, 'non-empty regular'), ('missing', self.scratch / 'absent', 'Cannot open')]:
            with self.subTest(name):
                self.refused(path, pattern)
        with self.subTest('over the size bound'), patch.object(prepare, 'MAX_REFERENCE', len(STAND_IN) - 1):
            self.refused(self.original, 'at most')

    def test_failure_after_creation_removes_the_directory(self):
        created = []
        real = prepare.new_root

        def spy(*args):
            created.append(real(*args))
            return created[-1]

        with patch.object(prepare, 'new_root', spy), self.assertRaisesRegex(ValueError, 'descriptor'):
            prepare.prepare(self.original, 'bad host!', check_runtime=False)
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())

    def test_docker_preflight_failure_stops_before_reading_or_creating_anything(self):
        with patch.object(prepare, 'runtime_problems', return_value=['no docker']), \
                patch.object(prepare, 'new_root', side_effect=AssertionError('created')):
            with self.assertRaisesRegex(ValueError, 'Docker is not ready: no docker'):
                prepare.prepare(self.original, HOST)

    def test_command_line_refuses_and_prepares(self):
        wrong = self.scratch / 'wrong'
        wrong.write_bytes(b'not the original')
        arguments = ['--companion-host', HOST, '--skip-runtime-check']
        with patch.object(prepare.os, 'getuid', return_value=1000):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(prepare.main(['--reference', str(wrong), *arguments]), 1)
            self.assertIn('Not prepared:', err.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(prepare.main(['--reference', str(self.original), *arguments]), 0)
        root = Path(out.getvalue().split()[1])
        self.addCleanup(shutil.rmtree, root, True)
        self.assertTrue((root / 'reference').is_file())
        self.assertIn(f'fourmilab.desk serve {root}', out.getvalue())
        self.assertIn(f'--fourmilab-socket {root}/desk.sock', out.getvalue())
        self.assertIsNone(re.search(r'(?i)token|password|secret', out.getvalue()))


class RuntimePreflight(unittest.TestCase):
    OBSERVER_ID = ('image', 'inspect', '--format', '{{.Id}}', sandbox.PYTHON)
    COMPILER_ID = ('image', 'inspect', '--format', '{{.Id}}', sandbox.RUST)
    RUNTIMES = ('info', '--format', '{{json .Runtimes}}')

    def runner(self, answers):
        def run(argv, **_):
            out = answers.get(tuple(argv[1:]))
            return SimpleNamespace(returncode=0 if out is not None else 1, stdout=out or b'')
        return run

    def good(self):
        return {self.OBSERVER_ID: sandbox.PYTHON.encode() + b'\n', self.COMPILER_ID: sandbox.RUST.encode() + b'\n',
                self.RUNTIMES: b'{"runc":{},"runsc":{}}\n'}

    def problems(self, answers, docker=sys.executable):
        return prepare.runtime_problems(self.runner(answers), docker)

    def test_ready_host_has_no_problems(self):
        self.assertEqual(self.problems(self.good()), [])

    def test_missing_docker_or_wrong_image_id_or_runtime_is_reported(self):
        self.assertIn('not installed', self.problems(self.good(), '/nonexistent/docker')[0])
        wrong = {**self.good(), self.COMPILER_ID: b'sha256:' + b'0' * 64 + b'\n'}
        self.assertEqual(len([p for p in self.problems(wrong) if 'compiler' in p]), 1)
        absent = {key: value for key, value in self.good().items() if key != self.OBSERVER_ID}
        self.assertEqual(len([p for p in self.problems(absent) if 'observer' in p]), 1)
        no_runsc = {**self.good(), self.RUNTIMES: b'{"runc":{}}'}
        self.assertEqual(len([p for p in self.problems(no_runsc) if 'runsc' in p]), 1)


if __name__ == '__main__':
    unittest.main()
