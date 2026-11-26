"""Project grouping, bad-run reports and explicit retest reviews of build_desk.project_jobs (project_feedback store).

Builds use the static-site profile (no sandbox) and a rust build with a failing fake runner for failed runs. Grouping
is only the explicit project id; every package and hash read here is that run's own.
"""
import json
import threading
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from build_desk import project_feedback as fb
from build_desk.project_container import Result
from build_desk.project_jobs import ProjectJobs
from tests.test_project_jobs import FakeRunner, images, zip_bytes

HEX32 = set('0123456789abcdef')
ISSUE_KEYS = {'id', 'run_id', 'input_sha256', 'sha256', 'payload_sha256', 'expected', 'observed', 'state', 'at', 'reviews'}
REVIEW_KEYS = {'run_id', 'input_sha256', 'sha256', 'payload_sha256', 'outcome', 'note', 'at'}
HISTORY_KEYS = {'id', 'name', 'runs', 'issues'}
RUN_KEYS = {'id', 'profile', 'state', 'input_sha256', 'sha256'}


def is_id(value):
    return isinstance(value, str) and len(value) == 32 and set(value) <= HEX32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='projfb-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runner = FakeRunner()
        self.default_responder = self.runner.responder
        self.jobs = self.make(self.runner)

    def make(self, runner=None):
        jobs = ProjectJobs(self.tmp / 'state', images, runner=runner or FakeRunner())
        self.addCleanup(jobs.close)
        return jobs

    @property
    def runs(self):
        return self.tmp / 'state' / 'runs'

    def build(self, content=b'one', jobs=None, **kwargs):
        jobs = jobs or self.jobs
        run = jobs.start('static-site', zip_bytes({'index.html': content}), **kwargs)
        self.assertTrue(jobs.wait(30))
        self.assertEqual(jobs.status(run)['state'], 'built', jobs.status(run))
        return run

    def failed_build(self, **kwargs):
        self.runner.responder = lambda spec, staged: Result(1, b'', b'')
        try:
            run = self.jobs.start('rust', b'fn main(){}', **kwargs)
            self.assertTrue(self.jobs.wait(30))
        finally:
            self.runner.responder = self.default_responder
        self.assertEqual(self.jobs.status(run)['state'], 'failed')
        return run

    def issue_dir(self, issue):
        return self.runs / issue['run_id'] / 'issues' / issue['id']

    def project(self, name='Notes'):
        """A project with a built v1, a built v2 and an issue reported against v1."""
        v1 = self.build(b'v1', project_name=name)
        v2 = self.build(b'v2', project_id=v1)
        issue = self.jobs.report(v1, 'It opens', 'It is blank')
        return v1, v2, issue


class GroupingTest(Base):
    def test_same_named_independent_projects_stay_separate(self):
        a = self.build(b'a', project_name='Same name')
        b = self.build(b'b', project_name='Same name')
        self.assertNotEqual(a, b)
        library = self.jobs.library()
        self.assertEqual(set(library), {'projects'})
        self.assertEqual(library['projects'], [{'id': b, 'name': 'Same name'}, {'id': a, 'name': 'Same name'}])
        for run in (a, b):
            history = self.jobs.history(run)
            self.assertEqual(set(history), HISTORY_KEYS)
            self.assertEqual([r['id'] for r in history['runs']], [run])
        issue = self.jobs.report(a, 'x', 'y')
        self.assertEqual(self.jobs.history(a)['issues'], [issue])
        self.assertEqual(self.jobs.history(b)['issues'], [])

    def test_default_project_is_the_new_run_and_two_argument_call_still_works(self):
        run = self.build(b'one')
        status = self.jobs.status(run)
        self.assertEqual(status['project_id'], run)
        self.assertEqual(status['project_name'], 'site')
        self.assertEqual(self.jobs.library()['projects'], [{'id': run, 'name': 'site'}])
        again = self.build(b'one')  # same bytes, same default name: still a separate project
        self.assertEqual(len(self.jobs.library()['projects']), 2)
        self.assertEqual([r['id'] for r in self.jobs.history(again)['runs']], [again])

    def test_second_revision_joins_only_by_explicit_id(self):
        v1 = self.build(b'one', project_name='Notes')
        v2 = self.build(b'two', project_id=v1)
        loose = self.build(b'two', project_name='Notes')
        history = self.jobs.history(v1)
        self.assertEqual(history['name'], 'Notes')
        self.assertEqual([r['id'] for r in history['runs']], [v2, v1])
        for row in history['runs']:
            self.assertEqual(set(row), RUN_KEYS)
            status = self.jobs.status(row['id'])
            self.assertEqual((row['state'], row['sha256'], row['input_sha256'], status['project_id'],
                              status['project_name']),
                             ('built', status['sha256'], status['input_sha256'], v1, 'Notes'))
        self.assertEqual([p['id'] for p in self.jobs.library()['projects']], [loose, v1])
        self.assertEqual([r['id'] for r in self.jobs.history(loose)['runs']], [loose])
        self.assertNotEqual(history['runs'][0]['sha256'], history['runs'][1]['sha256'])

    def test_failed_run_is_a_row_with_no_hash(self):
        v1 = self.build(b'one', project_name='Notes')
        bad = self.failed_build(project_id=v1)
        rows = {r['id']: r for r in self.jobs.history(v1)['runs']}
        self.assertEqual((rows[bad]['state'], rows[bad]['sha256']), ('failed', None))
        self.assertEqual(rows[v1]['state'], 'built')

    def test_bad_project_arguments_are_value_errors_and_start_nothing(self):
        v1 = self.build(b'one', project_name='Notes')
        v2 = self.build(b'two', project_id=v1)
        before = sorted(p.name for p in self.runs.iterdir())
        data = zip_bytes({'index.html': b'x'})
        bad = [dict(project_id='0' * 32), dict(project_id=v2), dict(project_id=v1.upper()), dict(project_id=5),
               dict(project_id='abc'), dict(project_id=v1, project_name='Rename'), dict(project_name=''),
               dict(project_name='  '), dict(project_name='x' * 81), dict(project_name='a\x00b'),
               dict(project_name='a\nb'), dict(project_name='a\u202eb'), dict(project_name=7)]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.jobs.start('static-site', data, **kwargs)
        self.assertEqual(sorted(p.name for p in self.runs.iterdir()), before)
        run = self.build(b'three', project_name='  ' + 'ñ' * 80 + '  ')
        self.assertEqual(self.jobs.history(run)['name'], 'ñ' * 80)

    def test_history_and_library_reject_unknown_ids(self):
        v1 = self.build(b'one')
        v2 = self.build(b'two', project_id=v1)
        for value in ('0' * 32, v2, 'nope', None, v1.upper()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.jobs.history(value)

    def test_name_lives_only_in_the_root_record(self):
        v1 = self.build(b'one', project_name='Notes')
        v2 = self.build(b'two', project_id=v1)
        second = json.loads((self.runs / v2 / 'status.json').read_text())
        self.assertNotIn('project_name', second)
        self.assertEqual(second['project_id'], v1)
        root = json.loads((self.runs / v1 / 'status.json').read_text())
        self.assertEqual((root['project_id'], root['project_name']), (v1, 'Notes'))

    def test_legacy_records_are_independent_and_never_rewritten(self):
        legacy = 'ab' * 16
        directory = self.runs / legacy
        directory.mkdir()
        record = {'format': 1, 'id': legacy, 'profile': 'rust', 'state': 'failed', 'message': 'x', 'events': ['x'],
                  'artifact': None, 'sha256': None, 'input_sha256': '0' * 64, 'bytes': 0}
        (directory / 'status.json').write_text(json.dumps(record))
        original = (directory / 'status.json').read_bytes()
        again = self.make()
        status = again.status(legacy)
        self.assertEqual((status['project_id'], status['project_name']), (legacy, 'Rust project abababab'))
        self.assertEqual(again.library()['projects'], [{'id': legacy, 'name': 'Rust project abababab'}])
        self.assertEqual([r['id'] for r in again.history(legacy)['runs']], [legacy])
        issue = again.report(legacy, 'It builds', 'It did not')
        self.assertEqual((issue['run_id'], issue['sha256'], issue['input_sha256']), (legacy, None, '0' * 64))
        revision = self.build(b'fix', jobs=again, project_id=legacy)
        self.assertEqual([r['id'] for r in again.history(legacy)['runs']], [revision, legacy])
        self.assertEqual(again.review(revision, issue['id'], 'fixed', 'Now builds')['state'], 'fixed')
        self.assertEqual((directory / 'status.json').read_bytes(), original)


class ReportTest(Base):
    def test_built_run_report_records_server_derived_identity(self):
        v1 = self.build(b'one', project_name='Notes')
        status = self.jobs.status(v1)
        issue = self.jobs.report(v1, '  It opens  ', 'It is\nblank \u00e9')
        self.assertEqual(set(issue), ISSUE_KEYS)
        self.assertTrue(is_id(issue['id']))
        self.assertEqual((issue['run_id'], issue['state'], issue['reviews']), (v1, 'open', []))
        self.assertEqual((issue['input_sha256'], issue['sha256']), (status['input_sha256'], status['sha256']))
        self.assertEqual((issue['expected'], issue['observed']), ('It opens', 'It is\nblank \u00e9'))
        self.assertRegex(issue['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$')
        self.assertEqual(self.jobs.history(v1)['issues'], [issue])

    def test_failed_run_report_has_no_output_hash(self):
        bad = self.failed_build()
        issue = self.jobs.report(bad, 'It builds', 'It failed')
        self.assertIsNone(issue['sha256'])
        self.assertEqual(issue['input_sha256'], self.jobs.status(bad)['input_sha256'])

    def test_report_belongs_to_the_run_not_to_the_latest_job(self):
        v1, v2, issue = self.project()
        other = self.build(b'other', project_name='Notes')
        self.assertEqual(issue['sha256'], self.jobs.status(v1)['sha256'])
        self.assertEqual(self.jobs.history(v1)['issues'], [issue])
        self.assertEqual(self.jobs.history(other)['issues'], [])
        second = self.jobs.report(v2, 'b', 'c')
        self.assertEqual(second['sha256'], self.jobs.status(v2)['sha256'])
        self.assertEqual([i['id'] for i in self.jobs.history(v1)['issues']], [second['id'], issue['id']])

    def test_invalid_reports_write_nothing(self):
        v1 = self.build(b'one')
        for expected, observed in (('', 'x'), ('x', ' \n '), (None, 'x'), ('x', 3), ('x' * 1001, 'y'),
                                   ('a\x00b', 'y'), ('x', 'a\x1bb'), ('x', 'a\r b'), ('x', 'a\ud800b')):
            with self.subTest(expected=expected, observed=observed), self.assertRaises(ValueError):
                self.jobs.report(v1, expected, observed)
        for run in ('0' * 32, 'bad', None):
            with self.subTest(run=run), self.assertRaises(ValueError):
                self.jobs.report(run, 'x', 'y')
        self.assertFalse((self.runs / v1 / 'issues').exists())
        self.assertEqual(self.jobs.report(v1, 'x' * 1000, 'y')['expected'], 'x' * 1000)

    def test_unfinished_or_tampered_run_cannot_be_reported(self):
        self.runner.gate = threading.Event()
        run = self.jobs.start('rust', b'fn main(){}')
        with self.assertRaises(RuntimeError):
            self.jobs.report(run, 'x', 'y')
        self.runner.gate.set()
        self.assertTrue(self.jobs.wait(30))
        path = self.jobs.package(run)[0]
        path.write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            self.jobs.report(run, 'x', 'y')
        self.assertFalse((self.runs / run / 'issues').exists())

    def test_issue_limit_is_per_project(self):
        v1 = self.build(b'one')
        other = self.build(b'two')
        with mock.patch.object(fb, 'MAX_ISSUES', 2):
            self.jobs.report(v1, 'a', 'b')
            self.jobs.report(v1, 'a', 'b')
            with self.assertRaises(RuntimeError):
                self.jobs.report(v1, 'a', 'b')
            self.jobs.report(other, 'a', 'b')


class ReviewTest(Base):
    def test_rebuilding_never_resolves_an_issue(self):
        v1, v2, issue = self.project()
        self.build(b'v3', project_id=v1)
        self.assertEqual(self.jobs.history(v1)['issues'], [issue])
        self.assertEqual(self.jobs.history(v1)['issues'][0]['state'], 'open')

    def test_explicit_successful_retest_marks_fixed_and_keeps_the_original(self):
        v1, v2, issue = self.project()
        done = self.jobs.review(v2, issue['id'], 'fixed', 'Opens now.\nChecked by hand.')
        self.assertEqual(set(done), ISSUE_KEYS)
        self.assertEqual(done['state'], 'fixed')
        for key in ('id', 'run_id', 'input_sha256', 'sha256', 'expected', 'observed', 'at'):
            self.assertEqual(done[key], issue[key])
        [review] = done['reviews']
        self.assertEqual(set(review), REVIEW_KEYS)
        status = self.jobs.status(v2)
        self.assertEqual((review['run_id'], review['sha256'], review['input_sha256'], review['outcome'],
                          review['note']),
                         (v2, status['sha256'], status['input_sha256'], 'fixed', 'Opens now.\nChecked by hand.'))
        self.assertEqual(self.jobs.history(v1)['issues'], [done])
        with self.assertRaises(RuntimeError):
            self.jobs.review(self.build(b'v3', project_id=v1), issue['id'], 'still_open', 'again')

    def test_unsuccessful_retests_are_kept_and_do_not_close(self):
        v1, v2, issue = self.project()
        bad = self.failed_build(project_id=v1)
        first = self.jobs.review(bad, issue['id'], 'still_open', 'Build failed')
        self.assertEqual((first['state'], first['reviews'][0]['sha256']), ('open', None))
        second = self.jobs.review(v2, issue['id'], 'still_open', 'Still blank')
        self.assertEqual([r['outcome'] for r in second['reviews']], ['still_open', 'still_open'])
        v3 = self.build(b'v3', project_id=v1)
        third = self.jobs.review(v3, issue['id'], 'fixed', 'Good')
        self.assertEqual([r['run_id'] for r in third['reviews']], [bad, v2, v3])
        self.assertEqual(third['reviews'][:2], second['reviews'])
        self.assertEqual(third['state'], 'fixed')

    def test_rejected_reviews_change_nothing(self):
        v1, v2, issue = self.project()
        bad = self.failed_build(project_id=v1)
        other = self.build(b'other', project_name='Other')
        other_issue = self.jobs.report(other, 'a', 'b')
        files = sorted(p.name for p in self.issue_dir(issue).iterdir())
        cases = [
            (v1, issue['id'], 'fixed'),          # the reported run itself is not later
            (bad, issue['id'], 'fixed'),         # a failed build cannot fix
            (other, issue['id'], 'fixed'),       # another project
            (other, issue['id'], 'still_open'),
            (v2, other_issue['id'], 'fixed'),    # the issue belongs to another project
        ]
        for candidate, issue_id, outcome in cases:
            with self.subTest(candidate=candidate, issue=issue_id, outcome=outcome), self.assertRaises(RuntimeError):
                self.jobs.review(candidate, issue_id, outcome, 'note')
        self.assertEqual(sorted(p.name for p in self.issue_dir(issue).iterdir()), files)
        self.assertEqual(self.jobs.history(v1)['issues'], [issue])

    def test_earlier_build_cannot_review_a_later_report(self):
        v1, v2, _ = self.project()
        late = self.jobs.report(v2, 'a', 'b')
        for outcome in ('fixed', 'still_open'):
            with self.assertRaises(RuntimeError):
                self.jobs.review(v1, late['id'], outcome, 'note')

    def test_repacking_unchanged_software_does_not_fix_a_report(self):
        root, changed, issue = self.project()
        original_again = self.build(b'v1', project_id=root)
        self.assertNotEqual(self.jobs.package(root)[1], self.jobs.package(original_again)[1])
        with self.assertRaises(RuntimeError):
            self.jobs.review(original_again, issue['id'], 'fixed', 'Only the run ID changed')
        self.jobs.review(changed, issue['id'], 'still_open', 'v2 still has the mistake')
        again = self.build(b'v2', project_id=root)
        with self.assertRaises(RuntimeError):
            self.jobs.review(again, issue['id'], 'fixed', 'Same application as the last retest')
        self.assertEqual(self.jobs.history(root)['issues'][0]['state'], 'open')

    def test_reused_output_cannot_be_the_fix(self):
        v1, v2, issue = self.project()

        def use_archived_output(target, original):
            src, digest = self.jobs.package(original)
            dest = self.jobs.package(target)[0]
            shutil.copyfile(src, dest)
            record = self.runs / target / 'status.json'
            data = json.loads(record.read_bytes()) | {'sha256': digest, 'bytes': dest.stat().st_size}
            record.write_text(json.dumps(data))

        repacked = self.build(project_id=v1)
        use_archived_output(repacked, v1)
        with self.assertRaises(RuntimeError):
            self.jobs.review(repacked, issue['id'], 'fixed', 'same bytes')
        self.jobs.review(v2, issue['id'], 'still_open', 'blank')
        v3 = self.build(b'v3', project_id=v1)
        use_archived_output(v3, v2)
        with self.assertRaises(RuntimeError):
            self.jobs.review(v3, issue['id'], 'fixed', 'already judged open')
        self.assertEqual(len(self.jobs.history(v1)['issues'][0]['reviews']), 1)

    def test_review_uses_one_verified_snapshot_of_the_download(self):
        root, changed, issue = self.project()
        unchanged = self.build(b'v1', project_id=root)
        path, _ = self.jobs.package(unchanged)
        replacement = self.jobs.package(changed)[0].read_bytes()
        open_zip = zipfile.ZipFile

        def read_while_replaced(source, *args, **kwargs):
            path.write_bytes(replacement)
            return open_zip(source, *args, **kwargs)

        with mock.patch.object(zipfile, 'ZipFile', side_effect=read_while_replaced):
            with self.assertRaises(RuntimeError):
                self.jobs.review(unchanged, issue['id'], 'fixed', 'Retested the unchanged download')
        self.assertEqual(self.jobs.history(root)['issues'], [issue])

    def test_same_build_cannot_repeat_or_reverse_a_review(self):
        v1, v2, issue = self.project()
        self.jobs.review(v2, issue['id'], 'still_open', 'blank')
        with self.assertRaises(RuntimeError):
            self.jobs.review(v2, issue['id'], 'still_open', 'blank again')
        with self.assertRaises(RuntimeError):  # the output judged still open cannot become the fix
            self.jobs.review(v2, issue['id'], 'fixed', 'on second look')
        self.assertEqual(self.jobs.history(v1)['issues'][0]['state'], 'open')

    def test_tampered_candidate_output_cannot_be_reviewed(self):
        v1, v2, issue = self.project()
        self.jobs.package(v2)[0].write_bytes(b'changed')
        for outcome in ('fixed', 'still_open'):
            with self.assertRaises(RuntimeError):
                self.jobs.review(v2, issue['id'], outcome, 'note')

    def test_bad_review_input_is_value_error(self):
        v1, v2, issue = self.project()
        for args in ((v2, issue['id'], 'closed', 'n'), (v2, issue['id'], None, 'n'), (v2, issue['id'], 'fixed', ''),
                     (v2, issue['id'], 'fixed', ' \n'), (v2, issue['id'], 'fixed', 'x' * 1001),
                     (v2, issue['id'], 'fixed', 'a\x00'), (v2, issue['id'], 'fixed', None),
                     (v2, '0' * 32, 'fixed', 'n'), (v2, 'bad', 'fixed', 'n'), (v2, v2, 'fixed', 'n'),
                     ('0' * 32, issue['id'], 'fixed', 'n'), (None, issue['id'], 'fixed', 'n')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.jobs.review(*args)
        self.assertEqual(self.jobs.history(v1)['issues'], [issue])

    def test_unfinished_candidate_cannot_review(self):
        v1, v2, issue = self.project()
        self.runner.gate = threading.Event()
        run = self.jobs.start('rust', b'fn main(){}', project_id=v1)
        with self.assertRaises(RuntimeError):
            self.jobs.review(run, issue['id'], 'still_open', 'n')
        self.runner.gate.set()
        self.assertTrue(self.jobs.wait(30))

    def test_review_limit(self):
        v1, v2, issue = self.project()
        v3 = self.build(b'v3', project_id=v1)
        with mock.patch.object(fb, 'MAX_REVIEWS', 1):
            self.jobs.review(v2, issue['id'], 'still_open', 'a')
            with self.assertRaises(RuntimeError):
                self.jobs.review(v3, issue['id'], 'still_open', 'b')


class StorageTest(Base):
    def test_history_survives_restart_and_reviews_continue(self):
        v1, v2, issue = self.project()
        bad = self.failed_build(project_id=v1)
        self.jobs.review(bad, issue['id'], 'still_open', 'failed')
        before = (self.jobs.library(), self.jobs.history(v1), self.jobs.status(v2))
        self.jobs.close()
        again = self.make()
        self.assertEqual((again.library(), again.history(v1), again.status(v2)), before)
        self.assertEqual(again.package(v2)[1], before[2]['sha256'])
        done = again.review(v2, issue['id'], 'fixed', 'ok after restart')
        self.assertEqual([r['run_id'] for r in done['reviews']], [bad, v2])
        revision = self.build(b'v4', jobs=again, project_id=v1)
        self.assertEqual(again.history(v1)['runs'][0]['id'], revision)

    def test_events_are_written_once_and_never_rewritten(self):
        v1, v2, issue = self.project()
        directory = self.issue_dir(issue)
        report = (directory / 'report.json').read_bytes()
        self.jobs.review(v2, issue['id'], 'still_open', 'blank')
        first = (directory / 'review-01.json').read_bytes()
        v3 = self.build(b'v3', project_id=v1)
        self.jobs.review(v3, issue['id'], 'fixed', 'good')
        self.assertEqual((directory / 'report.json').read_bytes(), report)
        self.assertEqual((directory / 'review-01.json').read_bytes(), first)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ['report.json', 'review-01.json', 'review-02.json'])
        self.assertTrue(is_id(json.loads(report)['id']))

    def test_unreadable_run_records_are_skipped_and_never_rewritten(self):
        v1 = self.build(b'one', project_name='Notes')
        v2 = self.build(b'two', project_id=v1)
        v3 = self.build(b'three', project_id=v1)
        path = self.runs / v2 / 'status.json'
        path.write_text('{not json')
        self.assertEqual([r['id'] for r in self.jobs.history(v1)['runs']], [v3, v1])
        self.assertEqual([p['id'] for p in self.jobs.library()['projects']], [v1])
        with self.assertRaises(RuntimeError):
            self.jobs.status(v2)
        self.assertEqual(path.read_text(), '{not json')

    def test_corrupt_child_record_does_not_hide_that_corrections_are_missing(self):
        root = self.build()
        child = self.build(b'v2', project_id=root)
        issue = self.jobs.report(child, 'Shows notes', 'Blank')
        unrelated = self.build(b'other')
        path = self.runs / child / 'status.json'
        saved = path.read_bytes()
        path.write_bytes(b'{')
        self.assertIs(self.jobs.history(root).get('incomplete'), True)
        self.assertIs(self.jobs.library().get('incomplete'), True)
        self.assertEqual(self.jobs.history(unrelated)['issues'], [])
        path.write_bytes(saved)
        self.assertEqual(self.jobs.history(root)['issues'], [issue])

    def test_history_rejects_stored_fix_for_identical_files(self):
        root, changed, issue = self.project()
        self.jobs.review(changed, issue['id'], 'still_open', 'v2 still fails')
        retry = self.build(b'v2', project_id=root)
        self.jobs.review(retry, issue['id'], 'still_open', 'Same v2')
        event = self.issue_dir(issue) / 'review-02.json'
        event.write_text(json.dumps(json.loads(event.read_bytes()) | {'outcome': 'fixed'}))
        with self.assertRaises(fb.Unreadable):
            self.jobs.history(root)

    def test_inconsistent_grouping_fields_are_unreadable(self):
        v1 = self.build(b'one', project_name='Notes')
        v2 = self.build(b'two', project_id=v1)
        path = self.runs / v2 / 'status.json'
        good = json.loads(path.read_text())
        orphan = good | {'project_id': 'f' * 32}
        for change in ({'project_name': 'Other'}, {'created_ns': True}, {'created_ns': 0}, {'created_ns': None},
                       {'project_id': 'NOT-HEX'}, {'project_id': 7}):
            with self.subTest(change=change):
                path.write_text(json.dumps(good | change))
                with self.assertRaises(RuntimeError):
                    self.jobs.status(v2)
                self.assertEqual([r['id'] for r in self.jobs.history(v1)['runs']], [v1])
        path.write_text(json.dumps({k: v for k, v in good.items() if k != 'created_ns'}))
        with self.assertRaises(RuntimeError):
            self.jobs.status(v2)
        path.write_text(json.dumps(orphan))  # a project whose root does not exist
        with self.assertRaises(RuntimeError):
            self.jobs.status(v2)
        with self.assertRaises(ValueError):
            self.jobs.history('f' * 32)
        with self.assertRaises(ValueError):
            self.jobs.start('static-site', zip_bytes({'index.html': b'x'}), project_id='f' * 32)
        self.assertEqual([p['id'] for p in self.jobs.library()['projects']], [v1])

    def test_malformed_issue_records_fail_closed(self):
        v1, v2, issue = self.project()
        self.jobs.review(v2, issue['id'], 'still_open', 'blank')
        directory = self.issue_dir(issue)
        saved = {p.name: p.read_bytes() for p in directory.iterdir()}
        report = json.loads(saved['report.json'])
        review = json.loads(saved['review-01.json'])
        mutations = [
            ('report.json', b'{bad'),
            ('report.json', b'[]'),
            ('report.json', json.dumps(report | {'run_id': v2}).encode()),
            ('report.json', json.dumps(report | {'id': 'f' * 32}).encode()),
            ('report.json', json.dumps(report | {'extra': 1}).encode()),
            ('report.json', json.dumps(report | {'sha256': 'XYZ'}).encode()),
            ('report.json', json.dumps(report | {'expected': ''}).encode()),
            ('report.json', json.dumps(report | {'at': 'yesterday'}).encode()),
            ('report.json', b'x' * (fb.MAX_EVENT + 1)),
            ('review-01.json', json.dumps(review | {'outcome': 'done'}).encode()),
            ('review-01.json', json.dumps(review | {'seq': 2}).encode()),
            ('review-01.json', json.dumps(review | {'issue_id': 'f' * 32}).encode()),
            ('review-01.json', json.dumps(review | {'sha256': None, 'outcome': 'fixed'}).encode()),
            ('review-01.json', b'{'),
        ]
        for name, data in mutations:
            with self.subTest(name=name, data=data[:60]):
                (directory / name).write_bytes(data)
                with self.assertRaises(RuntimeError):
                    self.jobs.history(v1)
                with self.assertRaises(RuntimeError):
                    self.jobs.review(self.build(b'n' + name.encode(), project_id=v1), issue['id'], 'still_open', 'n')
                (directory / name).write_bytes(saved[name])
        (directory / 'review-01.json').rename(directory / 'review-02.json')  # a gap in the sequence
        with self.assertRaises(RuntimeError):
            self.jobs.history(v1)
        (directory / 'review-02.json').rename(directory / 'review-01.json')
        (directory / 'notes.txt').write_text('stray')
        with self.assertRaises(RuntimeError):
            self.jobs.history(v1)
        (directory / 'notes.txt').unlink()
        self.assertEqual(self.jobs.history(v1)['issues'][0]['reviews'][0]['note'], 'blank')

    def test_a_report_that_never_reached_the_disk_is_ignored(self):
        v1 = self.build(b'one')
        directory = self.runs / v1 / 'issues' / ('ab' * 16)
        directory.mkdir(parents=True)
        (directory / 'report.json.tmp').write_text('{"partial')
        self.assertEqual(self.jobs.history(v1)['issues'], [])
        issue = self.jobs.report(v1, 'a', 'b')
        self.assertEqual(self.jobs.history(v1)['issues'], [issue])
        with self.assertRaises(ValueError):
            self.jobs.review(v1, 'ab' * 16, 'still_open', 'n')

    def test_issue_files_are_not_run_records(self):
        v1, v2, issue = self.project()
        again = self.make()  # recovery over directories holding issue files must leave them alone
        self.assertEqual(again.history(v1)['issues'], [issue])


class TextRulesTest(unittest.TestCase):
    def test_text(self):
        self.assertEqual(fb.text('  a\r\nb\tc é  ', 'x'), 'a\nb\tc é')
        self.assertEqual(fb.text('n' * 1000, 'x'), 'n' * 1000)
        self.assertEqual(fb.text('ñ' * 80, 'x', name=True), 'ñ' * 80)
        for bad in ('', ' ', 'n' * 1001, '\x00', 'a\x7fb', 'a\u2028b', 'a\u202eb', None, b'x', 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fb.text(bad, 'x')
        for bad in ('n' * 81, 'a\nb', 'a\tb', 'a\u0085b'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fb.text(bad, 'x', name=True)


if __name__ == '__main__':
    unittest.main()
