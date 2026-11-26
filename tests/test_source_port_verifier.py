"""Offline tests for the owned-source verifier. No Docker, no container, no original or candidate program runs here.

The sandbox boundary (containers.launch and containers.preflight) is replaced by an inert fake that returns the
independently authored expected observables, so these tests exercise approval, binding, comparison, and recording.
"""
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_PORT = REPOSITORY / 'source-port'
sys.path.insert(0, str(SOURCE_PORT))

entry = importlib.import_module('verifier.__main__')
candidate_image = importlib.import_module('verifier.candidate_image')
containers = importlib.import_module('verifier.containers')
elf = importlib.import_module('verifier.elf')
errors = importlib.import_module('verifier.errors')
screen = importlib.import_module('verifier.screen')
framing = importlib.import_module('verifier.framing')
identity = importlib.import_module('verifier.identity')
profiles = importlib.import_module('verifier.profile')
setup_entry = importlib.import_module('portsetup.__main__')
setup_install = importlib.import_module('portsetup.install')
setup_runtime = importlib.import_module('portsetup.runtime')
REAL_BLOCKER = containers.candidate_runtime_blocker
run = importlib.import_module('verifier.run')

CANDIDATE = b'fn main() {\n    println!("port");\n}\n'
ATTEMPT = 'attempt-1'
PROJECTS = ('line-count', 'status-handler')


def make_elf(program_headers=(1,), kind=2, machine=62, entry_size=56, count=None):
    """A minimal ELF64 little-endian executable image (data, never executed): header plus program-header types."""
    header = bytearray(64)
    header[:6] = containers.ELF64_LE
    header[16:20] = struct.pack('<HH', kind, machine)
    header[32:40] = struct.pack('<Q', 64)
    header[54:58] = struct.pack('<HH', entry_size, len(program_headers) if count is None else count)
    table = b''.join(struct.pack('<I', kind_) + bytes(52) for kind_ in program_headers)
    return bytes(header) + table + bytes(16)


ELF = make_elf()
FIELDS = identity.TARGET_FIELDS


def _force_remove(function, path, _error):
    os.chmod(path, 0o700)
    function(path)


def render(kind, row, case):
    """The program output that corresponds to one independently authored expected observable."""
    stderr = row['stderr_latin1'].encode('latin-1')
    if kind == 'cli':
        return containers.RunOutput(row['exit'], row['stdout_latin1'].encode('latin-1'), stderr)
    raw = b'HTTP/1.1 %d X\r\n' % row['status']
    raw += b''.join(f'{name}: {value}\r\n'.encode('latin-1') for name, value in row['headers']) + b'\r\n'
    if not case.stdin.startswith(b'HEAD '):
        raw += row['body_latin1'].encode('latin-1')
    return containers.RunOutput(row['exit'], raw, stderr)


class FakeSandbox:
    """Stands in for containers.launch: records every run and answers from the expected observables."""

    def __init__(self, project_id):
        self.kind = profiles.SPECS[project_id].kind
        base = SOURCE_PORT / 'profiles' / project_id / 'verifier'
        self.cases = profiles.parse_cases((base / 'corpus.json').read_bytes(), self.kind)
        rows = json.loads((base / 'expected_observables.json').read_text('utf-8'))
        self.rows = {row['id']: row for row in rows}
        self.by_key = {(case.argv, case.stdin): case for case in self.cases}
        assert len(self.by_key) == len(self.cases)
        self.calls = []
        self.mounted = []
        self.counts = {}
        self.build_output = containers.RunOutput(0, ELF, b'')
        self.mutate = None
        self.fail = None

    def __call__(self, spec):
        self.calls.append(spec)
        self.mounted.append({dest: src.read_bytes() for src, dest in spec.mounts})
        if self.fail is not None:
            self.fail(spec, len(self.calls))
        if spec.role == 'build':
            return self.build_output
        skip = len(containers.ORIGINAL_ARGV) if spec.role == 'original' else 1
        case = self.by_key[(tuple(spec.argv[skip:]), spec.stdin)]
        number = self.counts[spec.role, case.id] = self.counts.get((spec.role, case.id), 0) + 1
        output = render(self.kind, self.rows[case.id], case)
        if self.mutate is not None:
            output = self.mutate(spec.role, case.id, number, output) or output
        return output

    def roles(self):
        return [spec.role for spec in self.calls]


class Fixture:
    """A temporary protected root: operator-installed registry and approval, web-owned queue entry."""

    def __init__(self, test, project_id='line-count', attempt=ATTEMPT, candidate=CANDIDATE):
        self.project_id, self.attempt, self.candidate = project_id, attempt, candidate
        self.root = Path(tempfile.mkdtemp(prefix='source-port-test-'))
        test.addCleanup(shutil.rmtree, self.root, onexc=_force_remove)
        shutil.copytree(SOURCE_PORT / 'profiles' / project_id, self.root / 'registry' / project_id,
                        ignore=shutil.ignore_patterns('.gitattributes'))
        self.profile = profiles.load(self.root / 'registry', project_id)
        self.queue(candidate)

    @property
    def registry(self):
        return self.root / 'registry' / self.project_id

    def queue(self, candidate, attempt=None, digest=None):
        attempt = attempt or self.attempt
        directory = self.root / 'queue' / self.project_id / attempt
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'candidate.rs').write_bytes(candidate)
        job = {'schema_version': 1, 'project_id': self.project_id, 'attempt_id': attempt,
               'candidate_sha256': digest or identity.sha256(candidate)}
        (directory / 'job.json').write_bytes(identity.canonical(job))

    def target(self, **changes):
        fields = dict(project_id=self.project_id, attempt_id=self.attempt, source_sha256=self.profile.source_sha256,
                      candidate_sha256=identity.sha256(self.candidate), corpus_sha256=self.profile.corpus_sha256,
                      recipe_sha256=self.profile.recipe_sha256, toolchain_sha256=self.profile.toolchain_sha256)
        fields.update(changes)
        return identity.Target(**fields)

    def approve(self, target=None, name=None):
        target = target or self.target()
        directory = self.root / 'approvals' / self.project_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{name or target.attempt_id}.json').write_bytes(
            identity.canonical({'schema_version': 1, 'target': target.as_dict()}))

    def results(self):
        return self.root / 'results' / self.project_id

    def result(self):
        return json.loads((self.results() / self.attempt / 'result.json').read_text('utf-8'))

    def diagnostic(self):
        return json.loads((self.results() / self.attempt / 'diagnostic.json').read_text('utf-8'))

    def runtime(self):
        return json.loads((self.results() / 'runtime.json').read_text('utf-8'))


class VerifierCase(unittest.TestCase):
    project_id = 'line-count'

    def setUp(self):
        self.fixture = Fixture(self, self.project_id)
        self.sandbox = FakeSandbox(self.project_id)
        self.preflight = mock.Mock()
        # The fake models a pinned candidate-only runtime, which the fixed environment does not have (BlockedRuntimeTests).
        for name, replacement in (('launch', self.sandbox), ('preflight', self.preflight),
                                  ('candidate_runtime_blocker', lambda: None)):
            patcher = mock.patch.object(containers, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def verify(self, project_id=None, attempt=None):
        return run.verify(self.fixture.root, project_id or self.project_id, attempt or ATTEMPT)

    def assertNothingWritten(self):
        self.assertFalse((self.fixture.root / 'results').exists())
        self.assertEqual(self.sandbox.calls, [])


class HappyPathTests(VerifierCase):
    def test_every_case_is_measured_and_recorded(self):
        self.fixture.approve()
        outcome = self.verify()
        self.assertEqual(outcome['state'], 'exercised')
        result = self.fixture.result()
        self.assertEqual(set(result), {'schema_version', 'target', 'build_exit', 'cases'})
        self.assertEqual(result['target'], self.fixture.target().as_dict())
        self.assertEqual(result['build_exit'], 0)
        ids = [case['id'] for case in result['cases']]
        self.assertEqual(ids, sorted(case.id for case in self.sandbox.cases))
        for row in result['cases']:
            self.assertEqual(set(row), {'id', 'original_sha256', 'replay_sha256', 'candidate_output_sha256',
                                        'used_fallback'})
            self.assertIs(row['used_fallback'], False)
            expected = self.fixture.profile.expected[row['id']]
            self.assertEqual((row['original_sha256'], row['replay_sha256'], row['candidate_output_sha256']),
                             (expected,) * 3)
        self.assertEqual(self.fixture.diagnostic()['state'], 'exercised')
        runtime = self.fixture.runtime()
        self.assertEqual((runtime['state'], runtime['attempt_id']), ('exercised', ATTEMPT))

    def test_run_order_and_fixed_argv(self):
        self.fixture.approve()
        self.verify()
        count = len(self.sandbox.cases)
        self.assertEqual(self.sandbox.roles(), ['original'] * (2 * count) + ['build'] + ['candidate'] * count)
        for spec, mounted in zip(self.sandbox.calls, self.sandbox.mounted):
            if spec.role == 'build':
                self.assertEqual(spec.argv, containers.BUILD_ARGV)
                self.assertEqual(mounted, {'/source.rs': CANDIDATE})
            elif spec.role == 'original':
                self.assertEqual(spec.argv[:len(containers.ORIGINAL_ARGV)], containers.ORIGINAL_ARGV)
                self.assertEqual(mounted, {'/app/main.py': self.fixture.profile.source})
            else:
                self.assertEqual(spec.argv[0], containers.CANDIDATE_ENTRYPOINT)
                self.assertEqual(mounted, {containers.CANDIDATE_ENTRYPOINT: ELF})

    def test_status_handler_profile_runs_the_same_way(self):
        fixture = Fixture(self, 'status-handler')
        sandbox = FakeSandbox('status-handler')
        fixture.approve()
        with mock.patch.object(containers, 'launch', sandbox):
            outcome = run.verify(fixture.root, 'status-handler', ATTEMPT)
        self.assertEqual(outcome['state'], 'exercised')
        self.assertEqual(len(fixture.result()['cases']), 12)
        self.assertTrue(all(row['candidate_output_sha256'] == fixture.profile.expected[row['id']]
                            for row in fixture.result()['cases']))

    def test_work_directory_is_removed(self):
        self.fixture.approve()
        self.verify()
        self.assertEqual(list((self.fixture.root / 'work').iterdir()), [])

    def test_the_exact_built_program_is_bound_by_digest_and_every_candidate_run_uses_it(self):
        self.fixture.approve()
        outcome = self.verify()
        self.assertIn(f'Candidate program sha256 {identity.sha256(ELF)}.', outcome['detail'])
        self.assertEqual(self.fixture.diagnostic()['detail'], outcome['detail'])
        candidate_runs = [mounted for spec, mounted in zip(self.sandbox.calls, self.sandbox.mounted)
                          if spec.role == 'candidate']
        self.assertEqual(len(candidate_runs), len(self.sandbox.cases))
        self.assertEqual({identity.sha256(mounted[containers.CANDIDATE_ENTRYPOINT]) for mounted in candidate_runs},
                         {identity.sha256(ELF)})

    def test_a_program_that_changes_between_runs_is_a_failed_measurement(self):
        self.fixture.approve()

        def swap(spec, number):
            if spec.role == 'candidate' and not self.sandbox.counts.get('swapped'):
                self.sandbox.counts['swapped'] = 1
                path = spec.mounts[0][0]
                path.chmod(0o600)
                path.write_bytes(ELF + b'x')
        self.sandbox.fail = swap
        self.assertEqual(self.verify()['state'], 'failed')
        self.assertIn('changed after it was built', self.fixture.diagnostic()['detail'])
        self.assertEqual(self.fixture.result()['cases'], [])


class BlockedRuntimeTests(VerifierCase):
    """The fixed environment has no candidate-only image: the candidate role shares the original's interpreter image."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(containers, 'candidate_runtime_blocker', REAL_BLOCKER)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fixture.approve()

    def test_the_fixed_environment_returns_a_blocked_result_and_runs_nothing(self):
        outcome = self.verify()
        self.assertEqual(outcome['state'], 'blocked')
        self.assertIn('candidate-only', outcome['detail'])
        self.assertEqual(self.sandbox.calls, [])
        self.preflight.assert_not_called()
        result = self.fixture.result()
        self.assertEqual((result['cases'], result['build_exit']), ([], None))
        self.assertEqual(result['target'], self.fixture.target().as_dict())
        self.assertEqual(self.fixture.diagnostic()['state'], 'blocked')
        self.assertEqual(self.fixture.runtime()['state'], 'blocked')

    def test_a_clean_source_scan_never_turns_a_blocked_result_into_success(self):
        self.assertFalse(screen.used_fallback(CANDIDATE))
        self.assertNotEqual(self.verify()['state'], 'exercised')
        self.assertEqual(self.fixture.result()['cases'], [])

    def test_blocked_exits_nonzero_from_the_command_line(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = entry.main(['run', '--root', str(self.fixture.root), 'line-count', ATTEMPT])
        self.assertEqual(code, 1)
        self.assertIn('blocked', out.getvalue())

    def test_an_unreviewed_third_image_stays_blocked_and_only_a_reviewed_candidate_only_image_clears_it(self):
        self.assertIn('same pinned image', REAL_BLOCKER())
        other = 'sha256:' + 'c' * 64
        self.assertEqual(containers.REVIEWED_CANDIDATE_ONLY_IMAGES, frozenset())
        with mock.patch.dict(containers.IMAGES, {'candidate': other}):
            self.assertIn('no reviewed candidate-only', REAL_BLOCKER())
        with mock.patch.dict(containers.IMAGES, {'candidate': other}), \
                mock.patch.object(containers, 'REVIEWED_CANDIDATE_ONLY_IMAGES', frozenset({other})):
            self.assertIsNone(REAL_BLOCKER())
        with mock.patch.dict(containers.IMAGES, {'candidate': containers.COMPILER_IMAGE}), \
                mock.patch.object(containers, 'REVIEWED_CANDIDATE_ONLY_IMAGES',
                                  frozenset({containers.COMPILER_IMAGE})):
            self.assertIn('same pinned image', REAL_BLOCKER())

    def test_the_candidate_role_has_no_original_program_and_no_network_in_its_recipe(self):
        flags = containers.recipe()['docker']['candidate']
        self.assertIn('--network=none', flags)
        self.assertIn('--runtime=runsc', flags)
        self.assertIn('--read-only', flags)
        self.assertNotIn('/app/main.py', ' '.join(flags))

    def test_the_blocked_state_is_documented_and_the_gate_has_no_rows_to_accept(self):
        self.assertIn('`blocked`', (SOURCE_PORT / 'verifier' / 'LAYOUT.md').read_text('utf-8'))
        self.verify()
        self.assertEqual(self.fixture.result()['cases'], [])


class ApprovalTests(VerifierCase):
    def test_missing_approval_is_refused_and_nothing_runs(self):
        with self.assertRaises(errors.Refused):
            self.verify()
        self.assertNothingWritten()
        self.preflight.assert_not_called()

    def test_each_target_field_must_match(self):
        changed = {'source_sha256': '1' * 64, 'candidate_sha256': '2' * 64, 'corpus_sha256': '3' * 64,
                   'recipe_sha256': '4' * 64, 'toolchain_sha256': '5' * 64, 'attempt_id': 'attempt-2'}
        for field, value in changed.items():
            with self.subTest(field=field):
                self.fixture.approve(self.fixture.target(**{field: value}), name=ATTEMPT)
                with self.assertRaisesRegex(errors.Refused, field):
                    self.verify()
                self.assertNothingWritten()

    def test_approval_for_another_attempt_does_not_transfer(self):
        self.fixture.approve(self.fixture.target(attempt_id='attempt-2'), name='attempt-2')
        with self.assertRaises(errors.Refused):
            self.verify()
        self.assertNothingWritten()

    def test_malformed_approvals_are_refused(self):
        directory = self.fixture.root / 'approvals' / self.project_id
        directory.mkdir(parents=True)
        target = self.fixture.target().as_dict()
        bodies = [b'not json', b'[]', identity.canonical({'schema_version': 1}),
                  identity.canonical({'schema_version': 2, 'target': target}),
                  identity.canonical({'schema_version': 1, 'target': target, 'approved': True}),
                  identity.canonical({'schema_version': 1, 'target': {**target, 'extra': 'x'}}),
                  identity.canonical({'schema_version': 1, 'target': {**target, 'candidate_sha256': 'AB' * 32}})]
        for body in bodies:
            with self.subTest(body=body[:40]):
                (directory / f'{ATTEMPT}.json').write_bytes(body)
                with self.assertRaises(errors.Refused):
                    self.verify()
                self.assertNothingWritten()

    def test_group_writable_approval_is_refused(self):
        if os.name != 'posix':
            self.skipTest('Permission bits are only enforced on POSIX hosts.')
        self.fixture.approve()
        path = self.fixture.root / 'approvals' / self.project_id / f'{ATTEMPT}.json'
        path.chmod(0o664)
        with self.assertRaisesRegex(errors.Refused, 'writable'):
            self.verify()

    def test_approval_is_single_use(self):
        self.fixture.approve()
        self.verify()
        calls = len(self.sandbox.calls)
        with self.assertRaisesRegex(errors.Refused, 'already has a verifier record'):
            self.verify()
        self.assertEqual(len(self.sandbox.calls), calls)

    def test_environment_cannot_approve_or_change_containment(self):
        variables = {name: '1' for name in ('SOURCE_PORT_APPROVE', 'SOURCE_PORT_TRUST', 'VERIFIER_APPROVE',
                                            'VERIFIER_NO_SANDBOX', 'APPROVE', 'CI', 'FORCE', 'DOCKER')}
        with mock.patch.dict(os.environ, variables):
            with self.assertRaises(errors.Refused):
                self.verify()
        self.assertNothingWritten()

    def test_unknown_project_and_bad_attempt_are_refused(self):
        for project, attempt in (('fourmilab', ATTEMPT), ('../line-count', ATTEMPT), ('line-count', '../x'),
                                 ('line-count', ''), ('line-count', 'a' * 65)):
            with self.subTest(project=project, attempt=attempt):
                with self.assertRaises(errors.Refused):
                    run.verify(self.fixture.root, project, attempt)
        self.assertNothingWritten()


class BindingTests(VerifierCase):
    def test_candidate_that_differs_from_the_queued_digest_is_refused(self):
        self.fixture.queue(CANDIDATE + b'// drift\n', digest=identity.sha256(CANDIDATE))
        self.fixture.approve()
        with self.assertRaisesRegex(errors.Refused, 'differ'):
            self.verify()
        self.assertNothingWritten()

    def test_approval_for_other_bytes_is_refused_even_with_a_consistent_queue(self):
        self.fixture.approve()
        self.fixture.queue(CANDIDATE + b'// swapped\n')
        with self.assertRaisesRegex(errors.Refused, 'candidate_sha256'):
            self.verify()
        self.assertNothingWritten()

    def test_bad_candidate_shapes_are_refused(self):
        for body in (b'', b'fn main() {}\0', b'\xff\xfe', b'x' * (run.MAX_CANDIDATE + 1)):
            with self.subTest(size=len(body)):
                self.fixture.queue(body)
                self.fixture.approve(self.fixture.target(candidate_sha256=identity.sha256(body)))
                with self.assertRaises(errors.Refused):
                    self.verify()
                self.assertNothingWritten()

    def test_job_must_name_this_project_and_attempt(self):
        directory = self.fixture.root / 'queue' / self.project_id / ATTEMPT
        digest = identity.sha256(CANDIDATE)
        for job in ({'schema_version': 1, 'project_id': 'status-handler', 'attempt_id': ATTEMPT,
                     'candidate_sha256': digest},
                    {'schema_version': 1, 'project_id': self.project_id, 'attempt_id': 'other',
                     'candidate_sha256': digest},
                    {'schema_version': 1, 'project_id': self.project_id, 'attempt_id': ATTEMPT,
                     'candidate_sha256': digest, 'command': 'sh'}):
            with self.subTest(job=job):
                (directory / 'job.json').write_bytes(identity.canonical(job))
                self.fixture.approve()
                with self.assertRaises(errors.Refused):
                    self.verify()
                self.assertNothingWritten()

    def test_cargo_manifest_text_is_just_candidate_data(self):
        body = b'// [package]\n// build = "build.rs"\nfn main() {}\n'
        self.fixture.candidate = body
        self.fixture.queue(body)
        self.fixture.approve()
        self.verify()
        build = [spec for spec in self.sandbox.calls if spec.role == 'build']
        self.assertEqual(len(build), 1)
        self.assertEqual(build[0].argv, containers.BUILD_ARGV)
        self.assertEqual(self.sandbox.mounted[self.sandbox.calls.index(build[0])], {'/source.rs': body})

    def test_symbolic_link_candidate_is_refused(self):
        self.fixture.approve()
        path = self.fixture.root / 'queue' / self.project_id / ATTEMPT / 'candidate.rs'
        real = path.with_name('real.rs')
        real.write_bytes(CANDIDATE)
        path.unlink()
        try:
            path.symlink_to(real)
        except OSError as error:
            self.skipTest(f'This host cannot create symbolic links: {error}')
        with self.assertRaises(errors.Refused):
            self.verify()
        self.assertNothingWritten()


class DigestDriftTests(VerifierCase):
    def approve_and_expect_refusal(self, pattern='.'):
        self.fixture.approve()
        with self.assertRaisesRegex(errors.Refused, pattern):
            self.verify()
        self.assertNothingWritten()

    def test_changed_profile_files_are_refused(self):
        for name in profiles.files(profiles.SPECS[self.project_id]):
            with self.subTest(name=name):
                path = self.fixture.registry / name
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                try:
                    self.approve_and_expect_refusal()
                finally:
                    path.write_bytes(original)

    def test_changed_manifest_is_refused_by_the_pin(self):
        path = self.fixture.registry / 'manifest.json'
        manifest = json.loads(path.read_text('utf-8'))
        manifest['files']['verifier/corpus.json'] = '0' * 64
        path.write_bytes(identity.canonical(manifest))
        self.approve_and_expect_refusal('verifier pin')

    def test_unlisted_file_and_missing_file_are_refused(self):
        extra = self.fixture.registry / 'verifier' / 'extra.json'
        extra.write_bytes(b'{}\n')
        self.approve_and_expect_refusal('unlisted')
        extra.unlink()
        missing = self.fixture.registry / 'public' / 'brief.md'
        missing.unlink()
        self.approve_and_expect_refusal()

    def test_symbolic_link_inside_the_profile_is_refused(self):
        real = self.fixture.registry / 'verifier' / 'corpus.json'
        moved = self.fixture.root / 'moved.json'
        shutil.move(real, moved)
        try:
            real.symlink_to(moved)
        except OSError as error:
            self.skipTest(f'This host cannot create symbolic links: {error}')
        self.approve_and_expect_refusal()

    def test_pins_cover_exactly_the_two_profiles(self):
        self.assertEqual(set(profiles.SPECS), set(PROJECTS))
        self.assertEqual(set(profiles.PINNED_MANIFEST_SHA256), set(PROJECTS))
        self.assertTrue(all(re.fullmatch('[0-9a-f]{64}', v) and v != '0' * 64
                            for v in profiles.PINNED_MANIFEST_SHA256.values()))


class FailureTests(VerifierCase):
    def setUp(self):
        super().setUp()
        self.fixture.approve()

    def assertRecordedFailure(self, state, build_exit):
        result, diagnostic = self.fixture.result(), self.fixture.diagnostic()
        self.assertEqual(result['cases'], [])
        self.assertEqual(result['build_exit'], build_exit)
        self.assertEqual(result['target'], self.fixture.target().as_dict())
        self.assertEqual(diagnostic['state'], state)
        self.assertEqual(self.fixture.runtime()['state'], state)
        return diagnostic['detail']

    def test_missing_build_is_a_failed_run_with_the_real_exit(self):
        self.sandbox.build_output = containers.RunOutput(101, b'', b'error[E0601]: `main` function not found')
        outcome = self.verify()
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(self.assertRecordedFailure('failed', 101), 'Rust compilation failed (exit 101).')
        self.assertNotIn('candidate', self.sandbox.roles())

    def test_browser_visible_detail_never_carries_compiler_docker_or_exception_text(self):
        self.sandbox.build_output = containers.RunOutput(1, b'', b'error: /srv/secret/path/source.rs secret-token')
        self.assertNotIn('secret', self.verify()['detail'])
        done = mock.Mock(returncode=1, stderr=b'permission denied at /var/run/docker.sock secret-token')
        with mock.patch.object(containers.subprocess, 'run', return_value=done):
            with self.assertRaises(errors.Unavailable) as caught:
                containers.docker_query(['info'])
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('docker.sock', str(caught.exception))

    def test_compiler_success_without_a_program_is_failed(self):
        self.sandbox.build_output = containers.RunOutput(0, b'#!/bin/sh\n', b'')
        self.assertEqual(self.verify()['state'], 'failed')
        self.assertRecordedFailure('failed', 0)
        self.assertNotIn('candidate', self.sandbox.roles())

    def test_unstable_original_is_failed_before_any_build(self):
        def mutate(role, case_id, number, output):
            if role == 'original' and case_id == 'two-lines' and number == 2:
                return containers.RunOutput(0, b'3\n', b'')
        self.sandbox.mutate = mutate
        self.assertEqual(self.verify()['state'], 'failed')
        self.assertIn('unstable', self.assertRecordedFailure('failed', None))
        self.assertNotIn('build', self.sandbox.roles())

    def test_original_that_disagrees_with_the_expected_result_is_failed(self):
        def mutate(role, case_id, number, output):
            if role == 'original' and case_id == 'two-lines':
                return containers.RunOutput(0, b'3\n', b'')
        self.sandbox.mutate = mutate
        self.assertEqual(self.verify()['state'], 'failed')
        self.assertIn('independent expected', self.assertRecordedFailure('failed', None))
        self.assertNotIn('build', self.sandbox.roles())

    def test_late_timeout_leaves_no_partial_rows(self):
        def mutate(role, case_id, number, output):
            if role == 'candidate' and case_id == 'empty-input':
                raise errors.MeasurementFailed('Fixed sandbox deadline exceeded.')
        self.sandbox.mutate = mutate
        self.assertEqual(self.verify()['state'], 'failed')
        self.assertIn('deadline', self.assertRecordedFailure('failed', 0))
        self.assertEqual(self.sandbox.roles().count('candidate'), 3)

    def test_docker_unavailable_mid_run_is_reported_as_unavailable(self):
        def fail(spec, number):
            if spec.role == 'build':
                raise errors.Unavailable('Fixed sandbox exited 125: no such image')
        self.sandbox.fail = fail
        self.assertEqual(self.verify()['state'], 'unavailable')
        self.assertRecordedFailure('unavailable', None)

    def test_unexpected_verifier_fault_is_a_failure_never_a_success(self):
        def fail(spec, number):
            if spec.role == 'candidate':
                raise RuntimeError('boom')
        self.sandbox.fail = fail
        self.assertEqual(self.verify()['state'], 'failed')
        self.assertIn('RuntimeError', self.assertRecordedFailure('failed', 0))

    def test_failed_run_exits_nonzero_from_the_command_line(self):
        self.sandbox.build_output = containers.RunOutput(1, b'', b'error')
        with contextlib.redirect_stdout(io.StringIO()):
            code = entry.main(['run', '--root', str(self.fixture.root), 'line-count', ATTEMPT])
        self.assertEqual(code, 1)


class DiscrepancyTests(VerifierCase):
    def test_a_differing_candidate_is_recorded_not_hidden(self):
        self.fixture.approve()

        def mutate(role, case_id, number, output):
            if role == 'candidate' and case_id == 'crlf-lines':
                return containers.RunOutput(0, b'3\n', b'')
        self.sandbox.mutate = mutate
        outcome = self.verify()
        self.assertEqual(outcome['state'], 'exercised')
        self.assertIn('Differing: crlf-lines', outcome['detail'])
        rows = {row['id']: row for row in self.fixture.result()['cases']}
        expected = self.fixture.profile.expected
        self.assertNotEqual(rows['crlf-lines']['candidate_output_sha256'], expected['crlf-lines'])
        self.assertEqual(rows['crlf-lines']['original_sha256'], expected['crlf-lines'])
        self.assertEqual(rows['two-lines']['candidate_output_sha256'], expected['two-lines'])

    def test_exit_status_and_stderr_are_part_of_the_comparison(self):
        self.fixture.approve()

        def mutate(role, case_id, number, output):
            if role == 'candidate' and case_id == 'two-lines':
                return containers.RunOutput(output.status, output.stdout, b'warning\n')
            if role == 'candidate' and case_id == 'empty-input':
                return containers.RunOutput(1, output.stdout, output.stderr)
        self.sandbox.mutate = mutate
        outcome = self.verify()
        self.assertIn('empty-input, two-lines', outcome['detail'])


class ResultRecordingTests(VerifierCase):
    def test_lost_result_is_reported_and_recorded_as_failed(self):
        self.fixture.approve()
        with mock.patch.object(run, 'write_once', side_effect=errors.ResultLost('disk full')):
            with self.assertRaises(errors.ResultLost):
                self.verify()
        self.assertFalse((self.fixture.results() / ATTEMPT / 'result.json').exists())
        self.assertEqual(self.fixture.runtime()['state'], 'failed')

    def test_a_result_that_does_not_read_back_is_lost(self):
        store = importlib.import_module('verifier.store')
        target = self.fixture.root / 'x.json'
        with mock.patch.object(store.Path, 'read_bytes', return_value=b'different'):
            with self.assertRaises(errors.ResultLost):
                store.write_once(target, b'data')

    def test_records_are_never_replaced(self):
        store = importlib.import_module('verifier.store')
        target = self.fixture.root / 'x.json'
        store.write_once(target, b'first')
        with self.assertRaises(errors.Refused):
            store.write_once(target, b'second')
        self.assertEqual(target.read_bytes(), b'first')

    def test_preflight_failure_records_runtime_only_and_does_not_claim_the_attempt(self):
        self.fixture.approve()
        self.preflight.side_effect = errors.Unavailable('Docker is not installed.')
        with self.assertRaises(errors.Unavailable):
            self.verify()
        runtime = self.fixture.runtime()
        self.assertEqual((runtime['state'], runtime['attempt_id']), ('unavailable', None))
        self.assertFalse((self.fixture.results() / ATTEMPT).exists())
        self.assertEqual(self.sandbox.calls, [])
        self.preflight.side_effect = None
        self.assertEqual(self.verify()['state'], 'exercised')

    def test_only_the_protected_result_directory_is_written(self):
        self.fixture.approve()
        before = {p for p in self.fixture.root.rglob('*') if p.is_file()}
        self.verify()
        after = {p for p in self.fixture.root.rglob('*') if p.is_file()}
        for path in after - before:
            self.assertEqual(path.relative_to(self.fixture.root).parts[0], 'results', path)
        for path in before:
            self.assertTrue(path.exists())


class CommandLineTests(VerifierCase):
    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = entry.main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_exit_zero_only_when_exercised(self):
        self.fixture.approve()
        code, out, _ = self.invoke('run', '--root', str(self.fixture.root), 'line-count', ATTEMPT)
        self.assertEqual((code, out.split(':')[1].strip()), (0, 'exercised'))

    def test_refusal_exits_one_with_a_plain_message(self):
        code, out, err = self.invoke('run', '--root', str(self.fixture.root), 'line-count', ATTEMPT)
        self.assertEqual((code, out), (1, ''))
        self.assertIn('attempt-1.json', err)

    def test_no_command_approval_or_profile_options_exist(self):
        for argv in (['run', '--root', 'x', 'line-count', ATTEMPT, '--approve'],
                     ['run', '--root', 'x', '--command', 'sh', 'line-count', ATTEMPT],
                     ['run', '--root', 'x', '--no-sandbox', 'line-count', ATTEMPT],
                     ['serve'], ['exec', 'id'], ['register', 'profile']):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(*argv)[0], 2)
        self.assertEqual(self.sandbox.calls, [])


class ProfileTests(unittest.TestCase):
    def read_public(self, project_id):
        base = SOURCE_PORT / 'profiles' / project_id / 'public'
        return {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}

    def test_shipped_profiles_load_and_describe_themselves(self):
        for project_id in PROJECTS:
            with self.subTest(project=project_id), tempfile.TemporaryDirectory() as root:
                registry = Path(root) / project_id
                shutil.copytree(SOURCE_PORT / 'profiles' / project_id, registry,
                                ignore=shutil.ignore_patterns('.gitattributes'))
                profile = profiles.load(Path(root), project_id)
                self.assertEqual(identity.sha256((registry / 'manifest.json').read_bytes()),
                                 profiles.PINNED_MANIFEST_SHA256[project_id])
                self.assertEqual(json.loads((registry / 'public' / 'profile.json').read_text('utf-8')),
                                 profile.identity())
                self.assertEqual(profile.identity()['required_cases'], len(profile.cases))
                self.assertGreaterEqual(sum(case.held_out for case in profile.cases), 3)

    def test_expected_results_are_recomputed_from_the_hand_authored_observables(self):
        for project_id in PROJECTS:
            with self.subTest(project=project_id):
                base = SOURCE_PORT / 'profiles' / project_id / 'verifier'
                spec = profiles.SPECS[project_id]
                cases = profiles.parse_cases((base / 'corpus.json').read_bytes(), spec.kind)
                rows = profiles.expected_rows(spec.kind, cases, (base / 'expected_observables.json').read_bytes())
                self.assertEqual(json.loads((base / 'expected.json').read_text('utf-8')), rows)

    def test_held_out_values_are_not_in_the_public_files(self):
        for project_id in PROJECTS:
            with self.subTest(project=project_id):
                spec = profiles.SPECS[project_id]
                base = SOURCE_PORT / 'profiles' / project_id
                cases = profiles.parse_cases((base / 'verifier' / 'corpus.json').read_bytes(), spec.kind)
                expected = json.loads((base / 'verifier' / 'expected.json').read_text('utf-8'))
                public = b'\n'.join(self.read_public(project_id).values())
                for case in cases:
                    if not case.held_out:
                        continue
                    self.assertRegex(case.id, r'^held-[0-9]{2}$')
                    needle = case.stdin.split(b'\r\n')[0] if spec.kind == 'http' else case.stdin
                    if len(needle) >= 6:
                        self.assertNotIn(needle, public, case.id)
                for row in expected:
                    self.assertNotIn(row['expected_sha256'].encode(), public)

    def test_public_files_carry_no_verifier_only_paths(self):
        for project_id in PROJECTS:
            with self.subTest(project=project_id):
                names = {p.relative_to(SOURCE_PORT / 'profiles' / project_id).as_posix()
                         for p in (SOURCE_PORT / 'profiles' / project_id / 'public').rglob('*') if p.is_file()}
                self.assertEqual(names, {'public/profile.json', 'public/brief.md', 'public/framing.md',
                                         f'public/original/{profiles.SPECS[project_id].source}'})

    def test_profile_trees_keep_bytes_exact(self):
        for project_id in PROJECTS:
            self.assertEqual((SOURCE_PORT / 'profiles' / project_id / '.gitattributes').read_text(), '* -text\n')
        self.assertEqual((SOURCE_PORT / 'verifier' / '.gitattributes').read_text(), '* -text\n')

    def test_the_http_profile_is_labeled_as_a_stdio_transport(self):
        text = (SOURCE_PORT / 'profiles' / 'status-handler' / 'public' / 'framing.md').read_text('utf-8')
        self.assertIn('stdio-http1', text)
        self.assertIn('no socket', text.lower())


class ContainmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='source-port-mount-')).resolve()
        self.addCleanup(shutil.rmtree, self.directory, onexc=_force_remove)
        self.file = self.directory / 'file'
        self.file.write_bytes(b'x')

    def spec(self, role, *mounts):
        return containers.RunSpec(role, ('/bin/true',), tuple(mounts))

    def test_every_role_has_the_full_containment_flags(self):
        for role in containers.ROLES:
            with self.subTest(role=role):
                argv = containers.docker_argv(self.spec(role, (self.file, '/mnt')), 'name')
                self.assertEqual(argv[:2], ['/usr/bin/docker', 'run'])
                for flag in ('--rm', '--pull=never', '--runtime=runsc', '--network=none', '--read-only',
                             '--cap-drop=ALL', '--security-opt=no-new-privileges', '--log-driver=none',
                             '--user', '65534:65534'):
                    self.assertIn(flag, argv)
                self.assertTrue(any(a.startswith('--pids-limit=') for a in argv))
                self.assertIn('--entrypoint', argv)
                self.assertEqual(argv[argv.index('--entrypoint') + 2], containers.IMAGES[role])
                mounts = [a for a in argv if a.startswith('type=bind')]
                self.assertEqual(mounts, [f'type=bind,src={self.file},dst=/mnt,readonly'])
                self.assertEqual([a for a in argv if a in ('--privileged', '-v', '--volume', '--device', '--cap-add',
                                                          '--network=host', '--pid=host', '--ipc=host')], [])
                self.assertFalse(any('docker.sock' in a for a in argv))

    def test_candidate_has_no_room_for_a_second_process(self):
        self.assertIn('--pids-limit=1', containers.docker_argv(self.spec('candidate'), 'n'))
        self.assertNotIn('--pids-limit=1', containers.docker_argv(self.spec('original'), 'n'))
        self.assertIn('--interactive', containers.docker_argv(self.spec('candidate'), 'n'))
        self.assertNotIn('--interactive', containers.docker_argv(self.spec('build'), 'n'))

    def test_roles_use_the_pinned_image_ids(self):
        self.assertEqual(containers.IMAGES, {'build': containers.COMPILER_IMAGE, 'original': containers.OBSERVER_IMAGE,
                                             'candidate': containers.OBSERVER_IMAGE})
        for image in (containers.COMPILER_IMAGE, containers.OBSERVER_IMAGE):
            self.assertRegex(image, r'^sha256:[0-9a-f]{64}$')

    def test_image_ids_equal_the_fourmilab_preloaded_images(self):
        text = (REPOSITORY / 'fourmilab' / 'sandbox.py').read_text('utf-8')
        python = re.search(r"^PYTHON = '(sha256:[0-9a-f]{64})'$", text, re.M)
        rust = re.search(r"^RUST = '(sha256:[0-9a-f]{64})'$", text, re.M)
        self.assertIsNotNone(python)
        self.assertIsNotNone(rust)
        self.assertEqual(containers.OBSERVER_IMAGE, python.group(1))
        self.assertEqual(containers.COMPILER_IMAGE, rust.group(1))

    def test_unsafe_mounts_and_roles_are_unavailable(self):
        comma = self.directory / 'a,b'
        comma.write_bytes(b'x')
        for bad in ((Path('relative'), '/m'), (self.directory / 'absent', '/m'), (self.directory, '/m'), (comma, '/m')):
            with self.subTest(mount=bad):
                with self.assertRaises(errors.Unavailable):
                    containers.docker_argv(self.spec('build', bad), 'n')
        for role in ('shell', ''):
            with self.assertRaises(errors.Unavailable):
                containers.docker_argv(containers.RunSpec(role, ('/bin/true',), ()), 'n')
        with self.assertRaises(errors.Unavailable):
            containers.docker_argv(containers.RunSpec('build', (), ()), 'n')

    def test_a_symbolic_link_mount_is_unavailable(self):
        link = self.directory / 'link'
        try:
            link.symlink_to(self.file)
        except OSError as error:
            self.skipTest(f'This host cannot create symbolic links: {error}')
        with self.assertRaises(errors.Unavailable):
            containers.docker_argv(self.spec('build', (link, '/m')), 'n')

    def test_the_recipe_digest_covers_every_flag(self):
        line = profiles.SPECS['line-count']
        base = profiles.recipe_sha256(line)
        with mock.patch.object(containers, 'PIDS', {**containers.PIDS, 'candidate': 8}):
            self.assertNotEqual(profiles.recipe_sha256(line), base)
        with mock.patch.object(containers, 'BUILD_ARGV', (*containers.BUILD_ARGV[:2], 'true')):
            self.assertNotEqual(profiles.recipe_sha256(line), base)
        self.assertEqual(profiles.recipe_sha256(line), base)
        self.assertNotEqual(base, profiles.recipe_sha256(profiles.SPECS['status-handler']))


class FailClosedTests(unittest.TestCase):
    def test_a_non_linux_host_is_unavailable_and_starts_nothing(self):
        spec = containers.RunSpec('build', containers.BUILD_ARGV, ())
        with mock.patch.object(containers.sys, 'platform', 'win32'), \
                mock.patch.object(containers.subprocess, 'Popen', side_effect=AssertionError('started a process')):
            with self.assertRaisesRegex(errors.Unavailable, 'Linux'):
                containers.preflight()
            with self.assertRaisesRegex(errors.Unavailable, 'Linux'):
                containers.launch(spec)

    def test_docker_environment_exit_statuses_never_echo_sandbox_stderr(self):
        """Exit 125-127 can come from Docker or from the candidate itself; its stderr must not reach diagnostics."""
        streams = {1: [b''], 2: [b'/srv/secret/path secret-token\n']}

        class Selector:
            def __init__(self):
                self.keys = {}
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def register(self, stream, events, data):
                self.keys[stream.fileno()] = types.SimpleNamespace(fd=stream.fileno(), data=data)
            def unregister(self, fd):
                del self.keys[fd]
            def get_map(self):
                return self.keys
            def select(self, timeout):
                return [(key, 1) for key in list(self.keys.values())]

        class Pipe:
            def __init__(self, fd):
                self.fd = fd
            def fileno(self):
                return self.fd

        class Process:
            stdin, stdout, stderr = None, Pipe(1), Pipe(2)
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def wait(self, timeout=None):
                return 125
            def poll(self):
                return 125

        spec = containers.RunSpec('build', containers.BUILD_ARGV, ())
        with mock.patch.object(containers.sys, 'platform', 'linux'), \
                mock.patch.object(containers, 'docker_argv', return_value=['docker']), \
                mock.patch.object(containers.subprocess, 'Popen', return_value=Process()), \
                mock.patch.object(containers.selectors, 'DefaultSelector', Selector), \
                mock.patch.object(containers.os, 'read', side_effect=lambda fd, size: streams[fd].pop(0) if streams[fd] else b''):
            with self.assertRaises(errors.Unavailable) as caught:
                containers.launch(spec)
        self.assertEqual(str(caught.exception), 'Fixed sandbox exited 125.')

    def test_missing_prerequisites_have_plain_reasons(self):
        platform = mock.patch.object(containers.sys, 'platform', 'linux')
        with platform, mock.patch.object(containers.os.path, 'isfile', return_value=False):
            with self.assertRaisesRegex(errors.Unavailable, 'not installed'):
                containers.preflight()
        installed = mock.patch.object(containers.os.path, 'isfile', return_value=True)
        with platform, installed, mock.patch.object(containers, 'docker_query', return_value=b'sha256:other\n'):
            with self.assertRaisesRegex(errors.Unavailable, 'not preloaded'):
                containers.preflight()

        def query(args):
            return containers.OBSERVER_IMAGE.encode() if args[0] == 'image' else b'{"runc":{}}'
        with platform, installed, mock.patch.object(containers, 'docker_query', side_effect=query):
            with mock.patch.object(containers, 'COMPILER_IMAGE', containers.OBSERVER_IMAGE):
                with self.assertRaisesRegex(errors.Unavailable, 'runsc'):
                    containers.preflight()

    def test_the_verifier_has_no_network_listener_eval_shell_or_environment_input(self):
        forbidden = (r'\bos\.environ\b', r'\bgetenv\b', r'\beval\(', r'\bexec\(', r'shell\s*=\s*True', r'\bsocket\b',
                     r'os\.system', r'\bhttp\.server\b', r'(?<!re\.)\bcompile\(', r'__import__', r'\bpickle\b')
        for path in (SOURCE_PORT / 'verifier').glob('*.py'):
            text = path.read_text('utf-8')
            for pattern in forbidden:
                self.assertIsNone(re.search(pattern, text), f'{path.name}: {pattern}')
            if path.name != 'containers.py':
                self.assertNotIn('subprocess', text, path.name)

    def test_the_verifier_never_imports_the_fourmilab_executor(self):
        for path in (SOURCE_PORT / 'verifier').glob('*.py'):
            self.assertNotIn('fourmilab', re.sub(r'#.*|""".*?"""', '', path.read_text('utf-8'), flags=re.S), path.name)


class FallbackScreenTests(VerifierCase):
    """The b'...' strings below are Rust candidate source fixtures (data), never executed by these tests."""

    def run_candidate(self, body):
        self.fixture.candidate = body
        self.fixture.queue(body)
        self.fixture.approve()
        outcome = self.verify()
        self.assertEqual(outcome['state'], 'exercised')
        return outcome, self.fixture.result()['cases']

    def test_a_clean_source_is_recorded_without_fallback(self):
        outcome, rows = self.run_candidate(CANDIDATE)
        self.assertTrue(rows)
        self.assertEqual({row['used_fallback'] for row in rows}, {False})
        self.assertNotIn('fallback', outcome['detail'])

    def test_sources_that_name_a_way_to_start_another_program_are_recorded_as_fallback(self):
        sources = (b'use std::process::Command;\nfn main() { let _ = Command::new("python3").status(); }\n',
                   b'use std::*;\nfn main() { process::exit(0); }\n',
                   b'use std::os::unix::process::CommandExt;\nfn main() {}\n',
                   b'fn main() { unsafe { core::arch::asm!("syscall"); } }\n',
                   b'extern "C" { fn execve(); }\nfn main() {}\n',
                   b'fn main() { let r#process = 1; let _ = r#process; }\n',
                   b'// calls exec later\nfn main() {}\n')
        for body in sources:
            with self.subTest(body=body[:40]):
                self.setUp()
                outcome, rows = self.run_candidate(body)
                self.assertEqual({row['used_fallback'] for row in rows}, {True})
                self.assertIn('used_fallback true', outcome['detail'])

    def test_ordinary_identifiers_are_not_flagged(self):
        self.assertFalse(screen.used_fallback(b'fn execute(count: u64) { let linked = count; let _ = linked; }\n'))
        self.assertTrue(screen.used_fallback(b'fn f() { x.exec(); }\n'))

    def test_the_build_forbids_unsafe_code_and_the_screen_is_part_of_the_recipe(self):
        self.assertIn('-F unsafe_code', containers.BUILD_SCRIPT)
        line = profiles.SPECS['line-count']
        base = profiles.recipe_sha256(line)
        with mock.patch.object(screen, 'PATTERN', screen.PATTERN + '|extra'):
            self.assertNotEqual(profiles.recipe_sha256(line), base)


class FramingTests(unittest.TestCase):
    RAW = b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nok\n'

    def digest(self, raw, head_only=False, stderr=b''):
        return framing.observed_digest('http', 0, raw, stderr, head_only)

    def test_header_order_and_name_case_are_not_observable(self):
        other = b'HTTP/1.1 200 OK\r\nconnection: close\r\nCONTENT-LENGTH: 3\r\n\r\nok\n'
        self.assertEqual(self.digest(self.RAW), self.digest(other))

    def test_value_status_body_and_stderr_are_observable(self):
        base = self.digest(self.RAW)
        for raw in (self.RAW.replace(b'ok\n', b'no\n'), self.RAW.replace(b'200', b'201'),
                    self.RAW.replace(b'close', b'keep-alive')):
            self.assertNotEqual(base, self.digest(raw))
        self.assertNotEqual(base, self.digest(self.RAW, stderr=b'x'))

    def test_malformed_and_truncated_responses_never_equal_a_well_formed_one(self):
        base = self.digest(self.RAW)
        for raw in (self.RAW[:-1], self.RAW + b'extra', self.RAW.replace(b'Content-Length: 3\r\n', b''),
                    self.RAW.replace(b'\r\n\r\n', b'\r\n'), self.RAW.replace(b'HTTP/1.1', b'HTTP/1.0'),
                    self.RAW.replace(b'Content-Length: 3', b'Content-Length: 3\r\nContent-Length: 3')):
            self.assertIsNone(framing.parse_response(raw, False), raw)
            self.assertNotEqual(base, self.digest(raw))

    def test_head_responses_carry_no_body(self):
        head = self.RAW[:-3]
        self.assertIsNotNone(framing.parse_response(head, True))
        self.assertIsNone(framing.parse_response(self.RAW, True))

    def test_cli_frame_separates_the_streams(self):
        self.assertNotEqual(framing.observed_digest('cli', 0, b'ab', b'c'), framing.observed_digest('cli', 0, b'a', b'bc'))
        self.assertNotEqual(framing.observed_digest('cli', 0, b'a', b''), framing.observed_digest('cli', 1, b'a', b''))

    def test_case_inputs_are_bounded_and_exact(self):
        self.assertEqual(framing.expand({'latin1': '\xff\x00'}), b'\xff\x00')
        self.assertEqual(framing.expand({'repeat_latin1': 'ab', 'times': 3}), b'ababab')
        for bad in ({'latin1': '\u0100'}, {'latin1': 'x', 'extra': 1}, {'repeat_latin1': 'x', 'times': 0},
                    {'repeat_latin1': 'x', 'times': framing.MAX_INPUT + 1}, 'text'):
            with self.assertRaises(errors.Refused):
                framing.expand(bad)


class StaticProgramTests(VerifierCase):
    """The candidate-only runtime has no loader, so a compiled candidate must be static; the verifier checks that."""

    def test_the_build_script_asks_for_a_static_non_pie_program(self):
        self.assertIn('-C target-feature=+crt-static', containers.BUILD_SCRIPT)
        self.assertIn('-C relocation-model=static', containers.BUILD_SCRIPT)
        self.assertIn('-F unsafe_code', containers.BUILD_SCRIPT)
        for project_id in PROJECTS:
            brief = (SOURCE_PORT / 'profiles' / project_id / 'public' / 'brief.md').read_text('utf-8')
            self.assertIn('-C target-feature=+crt-static -C relocation-model=static', brief, project_id)
            self.assertIn('no shell, interpreter, or other program', brief, project_id)

    def test_a_static_executable_and_a_static_pie_pass_the_structural_check(self):
        self.assertIsNone(elf.static_problem(make_elf()))
        self.assertIsNone(elf.static_problem(make_elf((6, 1, 2), kind=3)))

    def test_a_program_that_names_a_loader_is_rejected(self):
        self.assertIn('dynamically linked', elf.static_problem(make_elf((6, 3, 1))))

    def test_other_ELF_shapes_are_rejected_with_fixed_phrases(self):
        cases = {
            'no loadable segment': (make_elf((6,)), 'no loadable segment'),
            'not x86-64': (make_elf(machine=183), 'not an x86-64 executable'),
            'relocatable object': (make_elf(kind=1), 'not an x86-64 executable'),
            'bad entry size': (make_elf(entry_size=32), 'malformed program-header table'),
            'no headers': (make_elf(count=0), 'malformed program-header table'),
            'table past the end': (make_elf(count=5), 'malformed program-header table'),
            'truncated': (make_elf()[:40], 'no little-endian ELF64'),
            'empty': (b'', 'no little-endian ELF64'),
            'big endian': (b'\x7fELF\x02\x02' + bytes(120), 'no little-endian ELF64'),
            'ELF32': (b'\x7fELF\x01\x01' + bytes(120), 'no little-endian ELF64'),
            'script': (b'#!/bin/sh\n' + bytes(120), 'no little-endian ELF64'),
        }
        for label, (data, phrase) in cases.items():
            with self.subTest(label):
                self.assertIn(phrase, elf.static_problem(data) or '')

    def test_a_dynamically_linked_build_is_failed_before_any_candidate_runs(self):
        self.fixture.approve()
        self.sandbox.build_output = containers.RunOutput(0, make_elf((6, 3, 1)), b'')
        outcome = self.verify()
        self.assertEqual(outcome['state'], 'failed')
        self.assertIn('dynamically linked', outcome['detail'])
        self.assertEqual(self.fixture.result()['cases'], [])
        self.assertEqual(self.fixture.result()['build_exit'], 0)
        self.assertNotIn('candidate', self.sandbox.roles())

    def test_every_candidate_run_has_only_the_program_and_no_interpreter_or_original(self):
        self.fixture.approve()
        self.assertEqual(self.verify()['state'], 'exercised')
        runs = [spec for spec in self.sandbox.calls if spec.role == 'candidate']
        self.assertEqual(len(runs), len(self.sandbox.cases))
        for spec in runs:
            self.assertEqual([dest for _src, dest in spec.mounts], [containers.CANDIDATE_ENTRYPOINT])
            self.assertEqual(spec.argv[0], containers.CANDIDATE_ENTRYPOINT)
            joined = ' '.join(spec.argv)
            for forbidden in ('python', '/app/main.py', '/bin/sh', 'sh -c'):
                self.assertNotIn(forbidden, joined)


class CandidateImageTests(unittest.TestCase):
    """The generated runtime image: metadata only, byte-for-byte reproducible, never a pinned or reviewed image."""

    def test_the_archive_is_deterministic_and_matches_the_generator_digest(self):
        self.assertEqual(candidate_image.archive(), candidate_image.archive())
        self.assertEqual(len(candidate_image.archive()) % candidate_image.RECORD, 0)
        self.assertEqual(candidate_image.archive_sha256(), candidate_image.ARCHIVE_SHA256)
        self.assertRegex(candidate_image.ARCHIVE_SHA256, r'^[0-9a-f]{64}$')

    @staticmethod
    def raw_members(data):
        """(header block, size) of every member, read from the wire bytes, then the all-zero remainder."""
        offset, found = 0, []
        while data[offset:offset + 512] != bytes(512):
            block = data[offset:offset + 512]
            size = int(block[124:135], 8)
            found.append((block, size, data[offset + 512:offset + 512 + size]))
            offset += 512 + -(-size // 512) * 512
        return found, data[offset:]

    def test_the_archive_has_exactly_a_manifest_and_a_configuration_and_no_layer(self):
        data = candidate_image.archive()
        config_name = f'{candidate_image.configuration_sha256()}.json'
        members, remainder = self.raw_members(data)
        self.assertEqual([block[:100].rstrip(b'\0').decode() for block, _size, _body in members],
                         [config_name, 'manifest.json'])
        self.assertEqual(remainder, bytes(len(remainder)))
        for block, size, body in members:
            name = block[:100].rstrip(b'\0')
            with self.subTest(name):
                self.assertEqual(len(body), size)
                self.assertEqual(block[100:124], b'0000644\0' + b'0000000\0' + b'0000000\0')  # mode, uid, gid
                self.assertEqual(block[136:148], b'00000000000\0')  # mtime 0
                self.assertEqual(block[156:157], b'0')  # a regular file
                self.assertEqual(block[157:257], bytes(100))  # no link name
                self.assertEqual(block[257:263], b'ustar\0')
                self.assertEqual(block[263:265], b'00')
                self.assertEqual(block[265:329], bytes(64))  # no owner or group names
                self.assertEqual(block[329:345], b'0000000\0' + b'0000000\0')  # device numbers
                self.assertEqual(block[345:512], bytes(167))  # no prefix
                summed = sum(block[:148]) + 8 * 32 + sum(block[156:])
                self.assertEqual((int(block[148:154], 8), block[154:156]), (summed, b'\0 '))
        with tarfile.open(fileobj=io.BytesIO(data), mode='r') as tar:
            manifest = json.loads(tar.extractfile('manifest.json').read())
            config_bytes = tar.extractfile(config_name).read()
        self.assertEqual(manifest, [{'Config': config_name, 'RepoTags': None, 'Layers': []}])
        self.assertEqual(identity.sha256(config_bytes), candidate_image.configuration_sha256())
        self.assertEqual([body for _block, _size, body in members], [config_bytes, candidate_image.manifest_bytes()])

    def test_the_configuration_defines_nothing_to_run_or_reach(self):
        config = candidate_image.configuration()
        self.assertEqual(config, json.loads(candidate_image.configuration_bytes()))
        self.assertEqual(config['config'], {})
        self.assertEqual(config['rootfs'], {'type': 'layers', 'diff_ids': []})
        self.assertEqual((config['os'], config['architecture'], config['created']),
                         ('linux', 'amd64', '1970-01-01T00:00:00Z'))
        for forbidden in ('Entrypoint', 'Cmd', 'Env', 'User', 'ExposedPorts', 'Volumes', 'history'):
            self.assertNotIn(forbidden, config)
            self.assertNotIn(forbidden, config['config'])

    def test_the_archive_names_no_shell_interpreter_loader_or_original(self):
        data = candidate_image.archive()
        for needle in (b'/bin/sh', b'busybox', b'python', b'rustc', b'ld-linux', b'/lib', b'main.py', b'line_count',
                       b'status_handler', b'.tar'):
            self.assertNotIn(needle, data, needle)

    def test_describe_accepts_only_the_generated_bytes(self):
        facts = candidate_image.describe(candidate_image.archive())
        self.assertEqual((facts['layers'], facts['matches_pinned_generator_digest']), (0, True))
        self.assertEqual([name for name, _size, _regular in facts['members']],
                         [f'{candidate_image.configuration_sha256()}.json', 'manifest.json'])
        altered = bytearray(candidate_image.archive())
        altered[600] ^= 1
        with self.assertRaises(ValueError):
            candidate_image.describe(bytes(altered))

    def test_the_live_image_shape_check(self):
        problem = candidate_image.shape_problem
        self.assertIsNone(problem(None, None, None, None, 'linux/amd64'))
        self.assertIsNone(problem([], [], [], [], 'linux/amd64'))
        for label, arguments in {
            'layers': (['sha256:' + 'a' * 64], None, None, None, 'linux/amd64'),
            'entrypoint': (None, ['/bin/sh'], None, None, 'linux/amd64'),
            'command': (None, None, ['sh'], None, 'linux/amd64'),
            'environment': (None, None, None, ['PATH=/bin'], 'linux/amd64'),
            'platform': (None, None, None, None, 'linux/arm64'),
        }.items():
            with self.subTest(label):
                self.assertIsNotNone(problem(*arguments))


def genuine_image_document(image):
    """What `docker image inspect` reports for a loaded zero-layer image: no RootFS.Layers key at all."""
    return {'Id': image, 'Os': 'linux', 'Architecture': 'amd64', 'RootFS': {'Type': 'layers'},
            'Config': {'Cmd': None, 'Entrypoint': None, 'Env': None}}


class CandidateRuntimeBoundaryTests(unittest.TestCase):
    other = 'sha256:' + 'c' * 64

    def body(self, mutate=None, raw=None):
        if raw is not None:
            return raw
        document = genuine_image_document(self.other)
        if mutate:
            mutate(document)
        return json.dumps([document]).encode()

    def query(self, body):
        calls = []

        def query(args):
            calls.append(list(args))
            if args[0] == 'info':
                return b'{"runsc":{}}'
            if '--format' in args:  # preflight's ID lookup
                return args[-1].encode() + b'\n'
            return body
        query.calls = calls
        return query

    def preflight(self, mutate=None, raw=None):
        query = self.query(self.body(mutate, raw))
        with mock.patch.object(containers.sys, 'platform', 'linux'), \
                mock.patch.object(containers.os.path, 'isfile', return_value=True), \
                mock.patch.dict(containers.IMAGES, {'candidate': self.other}), \
                mock.patch.object(containers, 'docker_query', side_effect=query):
            containers.preflight()
        return query.calls

    def inspect(self, mutate=None, raw=None):
        with mock.patch.object(containers, 'docker_query', side_effect=self.query(self.body(mutate, raw))) as query:
            facts = containers.inspect_image(self.other)
        return facts, query.call_args_list

    def test_a_distinct_image_of_the_generated_shape_passes_preflight(self):
        calls = self.preflight()
        self.assertIn(['image', 'inspect', self.other], calls)  # the fixed full-JSON inspect, no Go template

    def test_the_genuine_zero_layer_image_has_no_layers_key_and_means_zero_layers(self):
        facts, calls = self.inspect()
        self.assertEqual(facts, {'id': self.other, 'layers': [], 'entrypoint': None, 'command': None, 'env': None,
                                 'platform': 'linux/amd64'})
        self.assertEqual(calls[0].args[0], ['image', 'inspect', self.other])

    def test_other_spellings_of_no_layers_and_absent_config_lists_are_accepted(self):
        for label, mutate in {
            'null': lambda d: d['RootFS'].update(Layers=None),
            'empty': lambda d: d['RootFS'].update(Layers=[]),
            'bare config': lambda d: d.update(Config={}),
        }.items():
            with self.subTest(label):
                facts, _calls = self.inspect(mutate)
                self.assertEqual((facts['layers'], facts['entrypoint'], facts['command'], facts['env']),
                                 ([], None, None, None))

    def test_a_distinct_image_with_layers_a_command_the_wrong_platform_or_id_is_unavailable(self):
        for label, mutate in {
            'layers': lambda d: d['RootFS'].update(Layers=['sha256:aa']),
            'entrypoint': lambda d: d['Config'].update(Entrypoint=['/bin/sh']),
            'command': lambda d: d['Config'].update(Cmd=['sh']),
            'env': lambda d: d['Config'].update(Env=['PATH=/bin']),
            'platform': lambda d: d.update(Architecture='arm64'),
            'different image': lambda d: d.update(Id='sha256:' + 'd' * 64),
        }.items():
            with self.subTest(label), self.assertRaises(errors.Unavailable):
                self.preflight(mutate)

    def test_a_malformed_or_incomplete_description_fails_closed(self):
        raws = {'not json': b'not json', 'not utf-8': b'\xff\xfe', 'object': b'{}', 'empty array': b'[]',
                'two images': json.dumps([genuine_image_document(self.other)] * 2).encode(),
                'not an object': b'["x"]'}
        mutations = {
            'no RootFS': lambda d: d.pop('RootFS'),
            'RootFS not an object': lambda d: d.update(RootFS='layers'),
            'RootFS type': lambda d: d['RootFS'].update(Type='other'),
            'no RootFS type': lambda d: d['RootFS'].pop('Type'),
            'Layers a string': lambda d: d['RootFS'].update(Layers='sha256:aa'),
            'Layers not strings': lambda d: d['RootFS'].update(Layers=[1]),
            'no Config': lambda d: d.pop('Config'),
            'Config a list': lambda d: d.update(Config=[]),
            'Config null': lambda d: d.update(Config=None),
            'Entrypoint a string': lambda d: d['Config'].update(Entrypoint='sh'),
            'Cmd not strings': lambda d: d['Config'].update(Cmd=[1]),
            'Env an object': lambda d: d['Config'].update(Env={}),
            'no Id': lambda d: d.pop('Id'),
            'empty Os': lambda d: d.update(Os=''),
            'no Os': lambda d: d.pop('Os'),
            'no Architecture': lambda d: d.pop('Architecture'),
        }
        for label, raw in raws.items():
            with self.subTest(label), self.assertRaisesRegex(errors.Unavailable, 'did not describe'):
                self.preflight(raw=raw)
        for label, mutate in mutations.items():
            with self.subTest(label), self.assertRaisesRegex(errors.Unavailable, 'did not describe'):
                self.preflight(mutate)

    def test_a_docker_failure_while_inspecting_is_unavailable(self):
        with mock.patch.object(containers, 'docker_query', side_effect=errors.Unavailable('Docker query failed.')):
            with self.assertRaises(errors.Unavailable):
                containers.check_candidate_image(self.other)

    def test_the_candidate_role_mounts_only_the_program_and_starts_it_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory).resolve() / 'port'
            program.write_bytes(ELF)
            spec = containers.RunSpec('candidate', (containers.CANDIDATE_ENTRYPOINT,),
                                      ((program, containers.CANDIDATE_ENTRYPOINT),))
            with mock.patch.dict(containers.IMAGES, {'candidate': self.other}):
                argv = containers.docker_argv(spec, 'name')
        self.assertEqual([a for a in argv if a.startswith('type=bind')],
                         [f'type=bind,src={program},dst={containers.CANDIDATE_ENTRYPOINT},readonly'])
        self.assertEqual(argv[-3:], ['--entrypoint', containers.CANDIDATE_ENTRYPOINT, self.other])
        for flag in ('--network=none', '--runtime=runsc', '--read-only', '--pids-limit=1'):
            self.assertIn(flag, argv)
        self.assertEqual([a for a in argv if 'python' in a or 'main.py' in a or '/bin/sh' in a], [])

    def test_the_blocker_stays_and_no_image_id_is_invented_or_marked_reviewed(self):
        derived = 'sha256:' + candidate_image.configuration_sha256()
        self.assertEqual(containers.CANDIDATE_IMAGE, containers.OBSERVER_IMAGE)
        self.assertEqual(containers.IMAGES['candidate'], containers.OBSERVER_IMAGE)
        self.assertEqual(containers.REVIEWED_CANDIDATE_ONLY_IMAGES, frozenset())
        self.assertNotIn(derived, containers.IMAGES.values())
        self.assertIn('same pinned image', REAL_BLOCKER())
        source = (SOURCE_PORT / 'verifier' / 'containers.py').read_text('utf-8')
        self.assertNotIn(candidate_image.configuration_sha256(), source)
        self.assertNotIn(candidate_image.ARCHIVE_SHA256, source)


class PortsetupTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='source-port-setup-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, onexc=_force_remove)
        self.root = self.base / 'convert'

    def tree(self):
        return sorted((p.relative_to(self.root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in self.root.rglob('*'))

    def queue(self, project_id, candidate=CANDIDATE, attempt=ATTEMPT):
        directory = self.root / 'queue' / project_id / attempt
        directory.mkdir(parents=True)
        (directory / 'candidate.rs').write_bytes(candidate)
        (directory / 'job.json').write_bytes(identity.canonical({
            'schema_version': 1, 'project_id': project_id, 'attempt_id': attempt,
            'candidate_sha256': identity.sha256(candidate)}))

    def command(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = setup_entry.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_install_puts_both_verified_profiles_and_every_directory_in_place(self):
        setup_install.install(self.root)
        for project_id in PROJECTS:
            with self.subTest(project=project_id):
                profile = profiles.load(self.root / 'registry', project_id)
                self.assertEqual(profile.identity()['required_cases'], len(profile.cases))
                for name in ('approvals', 'queue', 'results'):
                    self.assertEqual(list((self.root / name / project_id).iterdir()), [])
                registry = self.root / 'registry' / project_id
                self.assertEqual(list((registry / 'gate' / 'approval').iterdir()), [])
                installed = {p.relative_to(registry).as_posix() for p in registry.rglob('*') if p.is_file()}
                expected = {profiles.PORTABLE_EXPECTED if name == profiles.EXPECTED else name
                            for name in profiles.files(profile.spec)}
                self.assertEqual(installed, {*expected, 'manifest.json'})
                self.assertFalse((registry / 'verifier' / 'expected.json').exists())
        self.assertTrue((self.root / 'work').is_dir())
        self.assertFalse((self.root / 'gate').exists())  # No separate gate tree and no hard link.
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.stage')], [])

    def test_the_one_expected_file_is_a_single_physical_regular_file_with_the_reviewed_bytes(self):
        setup_install.install(self.root)
        for project_id in PROJECTS:
            projected = self.projected(project_id)
            self.assertTrue(projected.is_file() and not projected.is_symlink())
            self.assertEqual(projected.stat().st_nlink, 1)
            self.assertEqual(projected.read_bytes(),
                             (SOURCE_PORT / 'profiles' / project_id / 'verifier' / 'expected.json').read_bytes())

    def test_install_writes_no_approval_and_no_queue_entry(self):
        setup_install.install(self.root)
        self.assertEqual([p for p in self.root.rglob('*.json') if p.parent.name == 'approval'], [])
        for project_id in PROJECTS:
            self.assertEqual(list((self.root / 'approvals' / project_id).iterdir()), [])
            self.assertEqual(list((self.root / 'queue' / project_id).iterdir()), [])

    def test_install_does_not_create_profiles_through_a_linked_parent(self):
        self.root.mkdir()
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'queue').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(errors.Refused):
            setup_install.install(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_install_is_repeatable_and_changes_nothing(self):
        setup_install.install(self.root)
        before = self.tree()
        lines = setup_install.install(self.root)
        self.assertEqual(self.tree(), before)
        self.assertTrue(all('registry present' in line for line in lines))

    def test_a_tampered_source_profile_is_never_made_visible(self):
        source = self.base / 'profiles'
        shutil.copytree(SOURCE_PORT / 'profiles', source)
        brief = source / 'line-count' / 'public' / 'brief.md'
        brief.write_bytes(brief.read_bytes() + b'extra\n')
        with mock.patch.object(setup_install, 'SOURCE_PROFILES', source):
            with self.assertRaises(errors.Refused):
                setup_install.install(self.root)
        self.assertFalse((self.root / 'registry' / 'line-count').exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.stage')], [])

    def test_an_installed_profile_that_differs_is_never_overwritten(self):
        setup_install.install(self.root)
        brief = self.root / 'registry' / 'line-count' / 'public' / 'brief.md'
        brief.chmod(0o644)
        brief.write_bytes(brief.read_bytes() + b'changed\n')
        changed = brief.read_bytes()
        with self.assertRaises(errors.Refused):
            setup_install.install(self.root)
        self.assertEqual(brief.read_bytes(), changed)

    def test_check_reports_a_good_root_and_each_kind_of_damage(self):
        self.assertFalse(setup_install.check(self.root)[0])
        setup_install.install(self.root)
        ok, lines = setup_install.check(self.root)
        self.assertTrue(ok, lines)
        self.assertIn('portable layout', ' '.join(lines))
        mount_point = self.root / 'registry' / 'line-count' / 'gate' / 'approval'
        mount_point.rmdir()
        ok, lines = setup_install.check(self.root)
        self.assertFalse(ok)
        self.assertIn('mount point is missing', ' '.join(lines))
        mount_point.mkdir()
        self.assertTrue(setup_install.check(self.root)[0])
        projected = self.projected('line-count')
        content = projected.read_bytes()
        projected.chmod(0o640)
        projected.write_bytes(content + b' ')
        ok, lines = setup_install.check(self.root)
        self.assertFalse(ok)
        self.assertIn('line-count: PROBLEM', ' '.join(lines))
        projected.write_bytes(content)
        self.assertTrue(setup_install.check(self.root)[0])

    def projected(self, project_id):
        return self.root / 'registry' / project_id / 'gate' / 'expected.json'

    def assert_installed_root_is_refused(self, project_id='line-count'):
        """Every loader path refuses, nothing is written to results, the queue entry is untouched, and check fails."""
        self.queue(project_id)
        before = self.tree()
        with self.assertRaises(errors.Refused):
            profiles.load(self.root / 'registry', project_id)
        with self.assertRaises(errors.Refused):
            run.verify(self.root, project_id, ATTEMPT)
        results = self.root / 'results'
        self.assertEqual(sorted(p.relative_to(results).as_posix() for p in results.rglob('*')), sorted(PROJECTS))
        self.assertEqual(self.tree(), before)
        self.assertFalse(setup_install.check(self.root)[0])

    def test_a_replaced_gate_expected_file_is_refused_everywhere_and_nothing_is_written(self):
        setup_install.install(self.root)
        profiles.load(self.root / 'registry', 'line-count')
        projected = self.projected('line-count')
        projected.chmod(0o640)
        projected.write_bytes(b'[]')
        with self.assertRaisesRegex(errors.Refused, 'differs from its manifest digest'):
            profiles.load(self.root / 'registry', 'line-count')
        self.assert_installed_root_is_refused()

    def test_a_removed_gate_expected_file_is_refused_everywhere_and_nothing_is_written(self):
        setup_install.install(self.root)
        self.projected('line-count').unlink()
        self.assert_installed_root_is_refused()

    def test_a_second_load_after_the_file_is_replaced_detects_the_change(self):
        setup_install.install(self.root)
        profiles.load(self.root / 'registry', 'status-handler')
        projected = self.projected('status-handler')
        original = projected.read_bytes()
        projected.chmod(0o640)
        projected.write_bytes(original + b'\n')
        with self.assertRaises(errors.Refused):
            profiles.load(self.root / 'registry', 'status-handler')
        projected.write_bytes(original)
        profiles.load(self.root / 'registry', 'status-handler')

    def test_a_symbolic_link_for_the_gate_expected_file_is_refused(self):
        setup_install.install(self.root)
        projected = self.projected('line-count')
        copy = self.base / 'elsewhere.json'
        copy.write_bytes(projected.read_bytes())
        projected.unlink()
        projected.symlink_to(copy)
        self.assert_installed_root_is_refused()

    def test_an_extra_file_or_directory_in_the_portable_profile_is_refused(self):
        for label, make in {
            'file in gate': lambda gate: (gate / 'extra.json').write_bytes(b'{}'),
            'file in the mount point': lambda gate: (gate / 'approval' / 'a.json').write_bytes(b'{}'),
            'directory in gate': lambda gate: (gate / 'extra').mkdir(),
            'directory in the mount point': lambda gate: (gate / 'approval' / 'extra').mkdir(),
        }.items():
            with self.subTest(label):
                root = self.base / label.replace(' ', '-')
                setup_install.install(root)
                make(root / 'registry' / 'line-count' / 'gate')
                with self.assertRaises(errors.Refused):
                    profiles.load(root / 'registry', 'line-count')
                self.assertFalse(setup_install.check(root)[0])

    def test_the_original_layout_stays_valid_and_is_left_alone(self):
        for project_id in PROJECTS:
            shutil.copytree(SOURCE_PORT / 'profiles' / project_id, self.root / 'registry' / project_id,
                            ignore=shutil.ignore_patterns('.gitattributes'))
            profiles.load(self.root / 'registry', project_id)
        before = self.tree()
        lines = setup_install.install(self.root)
        self.assertTrue(all('registry present (original layout)' in line for line in lines), lines)
        for project_id in PROJECTS:
            self.assertFalse((self.root / 'registry' / project_id / 'gate').exists())
            self.assertTrue((self.root / 'registry' / project_id / 'verifier' / 'expected.json').is_file())
        ok, report = setup_install.check(self.root)
        self.assertTrue(ok, report)
        self.assertIn('original layout', ' '.join(report))
        self.assertEqual([entry for entry in self.tree() if entry[0].startswith('registry')],
                         [entry for entry in before if entry[0].startswith('registry')])

    def test_both_layouts_together_neither_layout_and_a_mount_point_in_the_original_layout_are_refused(self):
        for label in ('both', 'neither', 'original with a mount point'):
            with self.subTest(label):
                root = self.base / label.replace(' ', '-')
                registry = root / 'registry' / 'line-count'
                shutil.copytree(SOURCE_PORT / 'profiles' / 'line-count', registry,
                                ignore=shutil.ignore_patterns('.gitattributes'))
                if label == 'both':
                    (registry / 'gate').mkdir()
                    shutil.copyfile(registry / 'verifier' / 'expected.json', registry / 'gate' / 'expected.json')
                elif label == 'neither':
                    (registry / 'verifier' / 'expected.json').unlink()
                else:
                    (registry / 'gate' / 'approval').mkdir(parents=True)
                with self.assertRaises(errors.Refused):
                    profiles.load(root / 'registry', 'line-count')
                self.assertFalse(setup_install.check(root)[0])
                with self.assertRaises(errors.Refused):
                    setup_install.install(root)

    def test_describe_gives_the_same_answer_for_either_layout(self):
        setup_install.install(self.root)
        for project_id in PROJECTS:
            with self.subTest(project=project_id):
                self.assertEqual(profiles.describe(project_id, self.root / 'registry' / project_id),
                                 profiles.describe(project_id, SOURCE_PORT / 'profiles' / project_id))

    def test_approval_review_prints_what_to_read_writes_nothing_and_approves_nothing(self):
        setup_install.install(self.root)
        self.queue('line-count', CANDIDATE + b'// \x1b[31m\n')
        before = self.tree()
        review = setup_install.approval_review(self.root, 'line-count', ATTEMPT)
        self.assertEqual(self.tree(), before)
        profile = profiles.load(self.root / 'registry', 'line-count')
        self.assertEqual(review['target'], {
            'project_id': 'line-count', 'attempt_id': ATTEMPT, 'source_sha256': profile.source_sha256,
            'candidate_sha256': identity.sha256(CANDIDATE + b'// \x1b[31m\n'), 'corpus_sha256': profile.corpus_sha256,
            'recipe_sha256': profile.recipe_sha256, 'toolchain_sha256': profile.toolchain_sha256})
        self.assertEqual(json.loads(review['approval_file_content']),
                         {'schema_version': 1, 'target': review['target']})
        self.assertEqual((review['existing_approval'], review['receipt_possible_now']), ('absent', False))
        self.assertIn('candidate-only', review['runtime_blocker'])
        self.assertEqual(list((self.root / 'approvals' / 'line-count').iterdir()), [])
        rendered = setup_install.render_review(review)
        self.assertTrue(rendered.isascii())
        self.assertNotIn('\x1b', rendered)
        approval = self.root / 'approvals' / 'line-count' / f'{ATTEMPT}.json'
        approval.write_bytes(review['approval_file_content'].encode('ascii'))
        self.assertEqual(setup_install.approval_review(self.root, 'line-count', ATTEMPT)['existing_approval'],
                         'matches')

    def test_approval_review_refuses_an_unknown_project_a_bad_attempt_and_a_missing_queue_entry(self):
        setup_install.install(self.root)
        for project_id, attempt in (('line-count', ATTEMPT), ('unknown', ATTEMPT), ('line-count', '../x')):
            with self.subTest(project=project_id, attempt=attempt), self.assertRaises(errors.Refused):
                setup_install.approval_review(self.root, project_id, attempt)

    def test_the_command_line_has_no_approve_command_and_reports_exit_codes(self):
        with self.assertRaises(SystemExit):
            self.command('approve', '--root', str(self.root), 'line-count', ATTEMPT)
        self.assertEqual(self.command('check', '--root', str(self.root))[0], 1)
        self.assertEqual(self.command('install', '--root', str(self.root))[0], 0)
        self.assertEqual(self.command('check', '--root', str(self.root))[0], 0)
        self.queue('status-handler')
        code, out, _err = self.command('approval-review', '--root', str(self.root), 'status-handler', ATTEMPT)
        self.assertEqual(code, 0)
        self.assertIn('No approval was written', out)
        self.assertEqual(self.command('approval-review', '--root', str(self.root), 'line-count', ATTEMPT)[0], 1)

    def test_the_runtime_archive_is_written_once_and_matches_the_generator(self):
        output = self.base / 'candidate-runtime.tar'
        code, out, _err = self.command('runtime-archive', '--output', str(output))
        self.assertEqual(code, 0)
        self.assertEqual(output.read_bytes(), candidate_image.archive())
        self.assertIn(candidate_image.ARCHIVE_SHA256, out)
        self.assertEqual(self.command('runtime-archive', '--output', str(output))[0], 1)
        self.assertEqual(output.read_bytes(), candidate_image.archive())
        with mock.patch.object(candidate_image, 'ARCHIVE_SHA256', '0' * 64):
            with self.assertRaises(errors.Refused):
                setup_runtime.write_archive(self.base / 'other.tar')
        self.assertFalse((self.base / 'other.tar').exists())

    def test_runtime_review_prints_facts_and_pins_nothing(self):
        archive = self.base / 'candidate-runtime.tar'
        archive.write_bytes(candidate_image.archive())
        report = setup_runtime.review(archive=str(archive))
        self.assertIn('NOT REVIEWED', report['notice'])
        self.assertEqual(report['pinned']['reviewed_candidate_only_images'], [])
        self.assertIn('candidate-only', report['pinned']['runtime_blocker'])
        self.assertEqual(report['generator']['archive_sha256'], candidate_image.ARCHIVE_SHA256)
        self.assertEqual(report['generator']['expected_classic_image_id'],
                         'sha256:' + candidate_image.configuration_sha256())
        self.assertTrue(report['archive_file']['identical_to_generator'])
        altered = bytearray(candidate_image.archive())
        altered[600] ^= 1
        archive.write_bytes(bytes(altered))
        self.assertFalse(setup_runtime.review(archive=str(archive))['archive_file']['identical_to_generator'])
        self.assertEqual(containers.REVIEWED_CANDIDATE_ONLY_IMAGES, frozenset())
        self.assertEqual(containers.CANDIDATE_IMAGE, containers.OBSERVER_IMAGE)

    def test_runtime_review_reports_what_docker_says_about_a_loaded_image(self):
        image = 'sha256:' + candidate_image.configuration_sha256()
        facts = {'id': image, 'layers': None, 'entrypoint': None, 'command': None, 'env': None,
                 'platform': 'linux/amd64'}
        with mock.patch.object(containers, 'inspect_image', return_value=facts):
            shown = setup_runtime.review(image=image)['image']
        self.assertEqual((shown['shape_problem'], shown['id_equals_requested'], shown['id_equals_expected_classic_id']),
                         (None, True, True))
        with mock.patch.object(containers, 'inspect_image', return_value={**facts, 'layers': ['sha256:aa']}):
            self.assertIsNotNone(setup_runtime.review(image=image)['image']['shape_problem'])
        for bad in ('latest', '--all', 'sha256:abc', image.upper()):
            with self.subTest(image=bad), self.assertRaises(errors.Refused):
                setup_runtime.review(image=bad)

    def test_portsetup_has_no_environment_shell_network_or_subprocess(self):
        forbidden = (r'\bos\.environ\b', r'\bgetenv\b', r'\beval\(', r'\bexec\(', r'shell\s*=\s*True', r'\bsocket\b',
                     r'os\.system', r'\bsubprocess\b', r'__import__', r'\bpickle\b', r'\bfourmilab\b')
        for path in (SOURCE_PORT / 'portsetup').glob('*.py'):
            text = path.read_text('utf-8')
            for pattern in forbidden:
                self.assertIsNone(re.search(pattern, text), f'{path.name}: {pattern}')


if __name__ == '__main__':
    unittest.main()
