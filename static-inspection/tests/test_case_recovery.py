"""Offline case restore: states come from a real Case.view(), then are mutated, tampered or truncated.

Place beside test_yuzu_case.py (tests/) with case_recovery.py in workbench/, or put both in workbench/.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
for _candidate in (HERE.parent / 'workbench', HERE):
    if (_candidate / 'case_state.py').is_file():
        sys.path.insert(0, str(_candidate))
        break
from case_recovery import MAX_CASE_BYTES, CaseRestoreError, load_case, parse_case, restore_case
from case_state import CRITIQUE_SECONDS, GRANT_SECONDS, HISTORY, MAX_CALLS, Case, CaseError
from evidence import summarize

A_BYTES = b'%PDF-1.7 operator fixture A'
B_BYTES = b'\x89PNG\r\n\x1a\nfixture B'
POSIX = hasattr(os, 'geteuid') and hasattr(os, 'O_NOFOLLOW')


def plain(case):
    """The JSON an operator would save from GET /api/state."""
    return json.loads(json.dumps(case.view(), separators=(',', ':')))


def compact(state):
    return json.dumps(state, separators=(',', ':'))


def two_files():
    case = Case()
    case.admit(0, 'a', summarize(A_BYTES))
    case.admit(1, 'b', summarize(B_BYTES))
    return case


def one_file():
    case = Case()
    case.admit(0, 'a', summarize(A_BYTES))
    return case


def ready_case():
    """Operator, agent and review events, a mismatch, and a ready critique; grant disconnected."""
    case = two_files()
    case.operator_call(2, 'identify_file', {'slot': 'a'})
    case.operator_call(2, 'verify_checksum', {'slot': 'b', 'sha256': '0' * 64})
    token = case.pair(2)
    sid = case.begin(token, '2025-11-25')
    case.initialized(token, sid)
    case.agent_call(token, sid, 'compare_builds', {})
    case.agent_call(token, sid, 'export_report', {})
    case.disconnect(2)
    case.start_review(2, case.slots['a']['sha256'], True)
    job = case.claim_critique(2)
    case.finish_critique(2, job['ticket'], {'text': 'Both samples were identified; the reference matched.', 'cites': [5, 8]})
    return case


def long_case(matching, mismatching):
    case = Case()
    case.admit(0, 'a', summarize(A_BYTES))
    right = case.slots['a']['sha256']
    for _ in range(matching):
        case.operator_call(1, 'verify_checksum', {'slot': 'a', 'sha256': right})
    for _ in range(mismatching):
        case.operator_call(1, 'verify_checksum', {'slot': 'a', 'sha256': '0' * 64})
    return case


def paths(value, path=()):
    yield path
    if isinstance(value, dict):
        for key, item in value.items():
            yield from paths(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from paths(item, path + (index,))


def at(state, path):
    for step in path:
        state = state[step]
    return state


class RestoreBase(unittest.TestCase):
    def refused(self, state, *, contains=None):
        with self.assertRaises(CaseRestoreError) as caught:
            restore_case(state)
        text = str(caught.exception)
        self.assertTrue(text.startswith('Case not restored: '), text)
        self.assertTrue(text.endswith('Nothing was loaded.'), text)
        if contains:
            self.assertIn(contains, text)
        return text

    def restored(self, state):
        case = restore_case(copy.deepcopy(state))
        self.assertEqual(case.view(), state)
        self.assertIsNone(case.token)
        self.assertIsNone(case.sid)
        self.assertEqual(case.remaining, 0)
        return case


class ResumeTests(RestoreBase):
    def test_every_resting_state_round_trips_with_ids_events_and_critique(self):
        def not_requested():
            c = two_files()
            c.start_review(2, None, False)
            return c

        def cancelled():
            c = two_files()
            c.start_review(2, None, True)
            c.cancel_critique(2, c.view()['advisor']['critique']['end'])
            return c

        def failed():
            c = two_files()
            c.start_review(2, None, True)
            job = c.claim_critique(2)
            c.finish_critique(2, job['ticket'], None)
            return c

        def expired():
            c = two_files()
            with patch('case_state.monotonic', return_value=1000.0):
                c.start_review(2, None, True)
            with patch('case_state.monotonic', return_value=1000.0 + CRITIQUE_SECONDS):
                self.assertEqual(c.view()['advisor']['critique']['status'], 'expired')
            return c

        def stale():
            c = two_files()
            c.start_review(2, None, True)
            c.operator_call(2, 'export_report', {})
            return c

        def after_review_then_work():
            c = not_requested()
            c.operator_call(2, 'identify_file', {'slot': 'b'})
            return c

        def reset():
            c = two_files()
            c.reset(2)
            return c

        builders = {'fresh': Case, 'one file': one_file, 'ready': ready_case, 'not_requested': not_requested,
                    'cancelled': cancelled, 'failed': failed, 'expired': expired, 'stale': stale,
                    'review then work': after_review_then_work, 'reset': reset}
        for name, build in builders.items():
            with self.subTest(name):
                case = build()
                state = plain(case)
                loaded = self.restored(state)
                self.assertEqual(loaded.report(), case.report())
                self.assertEqual(loaded.revision, case.revision)
                self.assertEqual(loaded.view()['advisor'], state['advisor'])
        ready = self.restored(plain(ready_case()))
        self.assertEqual(ready.view()['advisor']['critique']['cites'], [5, 8])
        self.assertEqual([e['actor'] for e in ready.events],
                         ['operator', 'operator', 'agent', 'agent', 'skellywix', 'skellywix', 'skellywix', 'skellywix'])

    def test_loaded_case_continues_numbering_and_still_demands_current_revision(self):
        state = plain(ready_case())
        loaded = self.restored(state)
        with self.assertRaises(CaseError) as stale:
            loaded.operator_call(state['revision'] - 1, 'identify_file', {'slot': 'a'})
        self.assertEqual(stale.exception.status, 409)
        loaded.operator_call(state['revision'], 'identify_file', {'slot': 'a'})
        self.assertEqual(loaded.events[-1]['n'], 9)
        self.assertEqual(loaded.view()['advisor']['critique']['status'], 'stale')
        loaded.admit(state['revision'], 'a', summarize(B_BYTES))
        self.assertEqual((loaded.view()['events'], loaded.view()['advisor']['total']), ([], 0))

    def test_ready_critique_text_stays_inert_data(self):
        state = plain(ready_case())
        state['advisor']['critique']['text'] = '<img src=x onerror=alert(1)> & "quoted"'
        self.assertEqual(self.restored(state).view()['advisor']['critique']['text'], state['advisor']['critique']['text'])
        for key in ('html', 'code', 'script', '__class__', 'ticket'):
            tampered = copy.deepcopy(state)
            tampered['advisor']['critique'][key] = '<script>alert(1)</script>'
            self.refused(tampered)


class AuthorityTests(RestoreBase):
    def connected(self, begin=True, initialize=True):
        case = two_files()
        token = case.pair(2)
        sid = case.begin(token, '2025-11-25') if begin else None
        if initialize:
            case.initialized(token, sid)
        return case, token, sid

    def test_active_agent_connection_is_refused_not_downgraded(self):
        for begin, initialize in ((False, False), (True, False), (True, True)):
            with self.subTest(begin=begin, initialize=initialize):
                case, token, sid = self.connected(begin, initialize)
                self.refused(plain(case), contains='Disconnect')
                self.assertTrue(case.view()['connection'] in ('ready', 'connected'))  # The source is untouched.

    def test_old_grant_session_and_ticket_cannot_authorize_after_restore(self):
        case = two_files()
        with patch('case_state.monotonic', return_value=1000.0):
            token = case.pair(2)
            sid = case.begin(token, '2025-11-25')
            case.initialized(token, sid)
            case.agent_call(token, sid, 'identify_file', {'slot': 'a'})
        with patch('case_state.monotonic', return_value=1000.0 + GRANT_SECONDS):
            state = plain(case)
        self.assertEqual(state['connection'], 'expired')
        saved = compact(state)
        self.assertNotIn(token, saved)
        self.assertNotIn(sid, saved)
        loaded = self.restored(state)
        for call in (lambda: loaded.authorize(token), lambda: loaded.begin(token, 'x'),
                     lambda: loaded.agent_call(token, sid, 'identify_file', {'slot': 'a'}),
                     lambda: loaded.session(token, sid)):
            with self.assertRaises(CaseError) as denied:
                call()
            self.assertEqual(denied.exception.status, 401)
        fresh = loaded.pair(state['revision'])
        self.assertNotEqual(fresh, token)
        with self.assertRaises(CaseError):
            loaded.authorize(token)
        loaded.authorize(fresh)

    def test_spent_grant_restores_as_inert_status(self):
        case, token, sid = self.connected()
        for _ in range(MAX_CALLS):
            case.agent_call(token, sid, 'identify_file', {'slot': 'a'})
        state = plain(case)
        self.assertEqual(state['connection'], 'spent')
        loaded = self.restored(state)
        with self.assertRaises(CaseError):
            loaded.agent_call(token, sid, 'identify_file', {'slot': 'a'})
        self.assertEqual(loaded.view()['connection'], 'spent')

    def test_in_flight_critique_is_refused_but_finished_ones_are_kept(self):
        case = two_files()
        case.start_review(2, None, True)
        self.refused(plain(case), contains='in flight')
        case.claim_critique(2)
        self.assertEqual(plain(case)['advisor']['critique']['status'], 'reviewing')
        self.refused(plain(case), contains='in flight')
        case.cancel_critique(2, case.view()['advisor']['critique']['end'])
        self.restored(plain(case))

    def test_file_cannot_carry_credentials_or_authority(self):
        base = plain(ready_case())
        secret = 'SECRET-VALUE-0123456789'
        for key in ('token', 'sid', 'ticket', 'version', 'lease', 'grant', 'expires', 'remaining_calls', 'html', '__class__'):
            state = copy.deepcopy(base)
            state[key] = secret
            self.assertNotIn(secret, self.refused(state))
        state = copy.deepcopy(base)
        state['advisor']['ticket'] = secret
        self.assertNotIn(secret, self.refused(state))
        state = copy.deepcopy(base)
        state['connection'], state['remaining'] = 'connected', 5
        self.refused(state)
        for connection, remaining in (('off', 5), ('expired', 1), ('spent', 3), ('bogus', 0), ('ready', 0)):
            state = copy.deepcopy(base)
            state['connection'], state['remaining'] = connection, remaining
            self.refused(state)


class TamperTests(RestoreBase):
    def setUp(self):
        self.good = plain(ready_case())

    def test_unmutated_state_is_accepted(self):
        self.restored(self.good)

    def test_named_mutations_are_refused(self):
        a, b = self.good['slots']['a'], self.good['slots']['b']
        mutations = [
            (('schema',), 'yuzu.report/2'), (('schema',), None),
            (('revision',), 1), (('revision',), -1), (('revision',), True), (('revision',), 10 ** 16), (('revision',), '2'),
            (('earlier_events_omitted',), 1), (('earlier_events_omitted',), -1),
            (('advisor', 'total'), 9), (('advisor', 'total'), 7), (('advisor', 'total'), True),
            (('advisor', 'actors', 'agent'), 3), (('advisor', 'actors', 'operator'), 1), (('advisor', 'actors', 'robot'), 0),
            (('advisor', 'mismatches'), 0), (('advisor', 'mismatches'), 2), (('advisor', 'mismatches'), False),
            (('advisor', 'skipped'), ['compare_builds']), (('advisor', 'skipped'), ['verify_checksum']),
            (('advisor', 'skipped'), ['verify_checksum', 'compare_builds']), (('advisor', 'skipped'), 'none'),
            (('advisor', 'critique', 'end'), 7), (('advisor', 'critique', 'end'), 9), (('advisor', 'critique', 'end'), 0),
            (('advisor', 'critique', 'status'), 'pending'), (('advisor', 'critique', 'status'), 'bogus'),
            (('advisor', 'critique', 'status'), 'idle'), (('advisor', 'critique', 'status'), 'stale'),
            (('advisor', 'critique', 'cites'), [99]), (('advisor', 'critique', 'cites'), [5, 5]),
            (('advisor', 'critique', 'cites'), []), (('advisor', 'critique', 'cites'), [0]),
            (('advisor', 'critique', 'cites'), [1, 2, 3, 4]), (('advisor', 'critique', 'cites'), ['5']), (('advisor', 'critique', 'cites'), [True]),
            (('advisor', 'critique', 'text'), ''), (('advisor', 'critique', 'text'), '   '), (('advisor', 'critique', 'text'), 'bad\u202etext'),
            (('advisor', 'critique', 'text'), 'x' * 321), (('advisor', 'critique', 'text'), ' padded '), (('advisor', 'critique', 'text'), 'two\nlines'),
            (('advisor', 'critique', 'text'), 5), (('advisor', 'critique', 'text'), 'lone\ud800'),
            (('events', 0, 'actor'), 'robot'), (('events', 0, 'n'), 2), (('events', 0, 'n'), 0), (('events', 0, 'tool'), 'run_shell'),
            (('events', 0, 'slot'), 'b'), (('events', 0, 'slot'), 'c'), (('events', 0, 'extra'), 1),
            (('events', 0, 'result', 'size'), 99999), (('events', 0, 'result', 'format'), 'elf'),
            (('events', 1, 'result', 'matches'), True), (('events', 1, 'result', 'expected'), 'A' * 64),
            (('events', 1, 'result', 'actual'), '1' * 64), (('events', 1, 'slot'), 'a'),
            (('events', 2, 'result', 'size_delta'), 0), (('events', 2, 'result', 'identical'), True),
            (('events', 2, 'result', 'size_delta'), True), (('events', 2, 'slot'), 'a'),
            (('events', 3, 'result', 'files'), 1), (('events', 3, 'actor'), 'skellywix'), (('events', 3, 'result', 'files'), True),
            (('events', 4, 'actor'), 'agent'), (('events', 5, 'slot'), 'a'), (('events', 6, 'tool'), 'export_report'),
            (('events', 7, 'result', 'matches'), False), (('events', 7, 'actor'), 'operator'), (('events',), {}), (('events',), None),
            (('slots', 'a', 'sha256'), a['sha256'].upper()), (('slots', 'a', 'sha256'), a['sha256'][:-1]),
            (('slots', 'a', 'size'), 0), (('slots', 'a', 'size'), 16 * 1024 * 1024 + 1), (('slots', 'a', 'size'), True),
            (('slots', 'a', 'format'), 'exe'), (('slots', 'a', 'architecture'), 'x86'), (('slots', 'b', 'architecture'), 'arm64'),
            (('slots', 'b', 'size'), 3), (('slots', 'c'), dict(a)), (('slots', 'a', 'path'), 'C:/x'), (('slots',), []), (('slots',), {'b': b}),
            (('advisor',), None), (('remaining',), 1), (('connection',), 'on'),
        ]
        for path, value in mutations:
            with self.subTest(path=path, value=value):
                state = copy.deepcopy(self.good)
                parent = at(state, path[:-1])
                parent[path[-1]] = value
                self.refused(state)

    def test_too_many_events_and_wrong_ordering_are_refused(self):
        state = copy.deepcopy(self.good)
        state['events'] = state['events'] * 9
        self.refused(state)
        state = copy.deepcopy(self.good)
        state['events'].reverse()
        self.refused(state)
        state = copy.deepcopy(self.good)
        del state['events'][2]
        self.refused(state)
        state = copy.deepcopy(self.good)
        state['events'].append(copy.deepcopy(state['events'][-1]))
        self.refused(state)

    def test_deleting_or_nulling_any_field_is_refused(self):
        count = 0
        for path in paths(self.good):
            if not path:
                continue
            parent = at(self.good, path[:-1])
            if isinstance(parent, dict):
                state = copy.deepcopy(self.good)
                del at(state, path[:-1])[path[-1]]
                with self.subTest(delete=path):
                    self.refused(state)
                count += 1
            state = copy.deepcopy(self.good)
            at(state, path[:-1])[path[-1]] = None
            with self.subTest(null=path):
                self.refused(state)
            count += 1
        self.assertGreater(count, 150)

    def test_non_objects_are_refused(self):
        for value in (None, [], 'case', 7, [self.good]):
            self.refused(value)

    def test_critique_without_a_review_or_on_other_events_is_refused(self):
        plain_case = plain(two_files())
        plain_case['advisor']['critique'] = {'status': 'not_requested', 'end': 0}
        self.refused(plain_case)
        state = plain(ready_case())
        for index in range(4, 8):  # A review event attributed to the operator cannot support the critique.
            tampered = copy.deepcopy(state)
            tampered['events'][index]['actor'] = 'operator'
            tampered['advisor']['actors'] = {'operator': 3, 'agent': 2, 'skellywix': 3}
            self.refused(tampered)


class TruncationTests(RestoreBase):
    def test_truncated_history_keeps_numbers_and_continues(self):
        case = long_case(10, 64)
        state = plain(case)
        self.assertEqual((state['earlier_events_omitted'], state['advisor']['total'], len(state['events'])), (10, 74, HISTORY))
        loaded = self.restored(state)
        self.assertEqual(loaded.events[0]['n'], 11)
        loaded.operator_call(1, 'identify_file', {'slot': 'a'})
        self.assertEqual((loaded.events[0]['n'], loaded.events[-1]['n'], loaded.view()['earlier_events_omitted']), (12, 75, 11))

    def test_truncated_counts_are_bounded_but_cannot_be_proven(self):
        state = plain(long_case(10, 64))
        # The 64 retained events are all mismatches, so the total cannot be below 64 or above 64 plus 10 omitted.
        for mismatches in (0, 63, 75, 1000):
            tampered = copy.deepcopy(state)
            tampered['advisor']['mismatches'] = mismatches
            self.refused(tampered)
        for mismatches in (64, 70, 74):  # Documented limit: the omitted events are gone, so these cannot be told apart.
            accepted = copy.deepcopy(state)
            accepted['advisor']['mismatches'] = mismatches
            self.assertEqual(self.restored(accepted).view()['advisor']['mismatches'], mismatches)
        for actors in ({'operator': 63, 'agent': 11, 'skellywix': 0}, {'operator': 74, 'agent': 1, 'skellywix': 0},
                       {'operator': 70, 'agent': 0, 'skellywix': 0}):
            tampered = copy.deepcopy(state)
            tampered['advisor']['actors'] = actors
            self.refused(tampered)
        accepted = copy.deepcopy(state)
        accepted['advisor']['actors'] = {'operator': 70, 'agent': 4, 'skellywix': 0}
        self.restored(accepted)

    def test_truncated_numbering_total_and_omitted_must_agree(self):
        state = plain(long_case(10, 64))
        for field, value in (('earlier_events_omitted', 9), ('earlier_events_omitted', 11), ('earlier_events_omitted', 0)):
            tampered = copy.deepcopy(state)
            tampered[field] = value
            self.refused(tampered)
        tampered = copy.deepcopy(state)
        tampered['advisor']['total'] = 75
        self.refused(tampered)
        tampered = copy.deepcopy(state)
        for event in tampered['events']:
            event['n'] -= 1
        self.refused(tampered)
        tampered = copy.deepcopy(state)
        del tampered['events'][0]
        self.refused(tampered)
        tampered = copy.deepcopy(state)
        tampered['events'][5]['n'] = 99
        self.refused(tampered)

    def test_review_after_many_events_is_checked_against_the_retained_tail(self):
        case = two_files()
        for _ in range(62):
            case.operator_call(2, 'export_report', {})
        case.start_review(2, case.slots['a']['sha256'], True)  # 4 review events: 63..66; 1 and 2 are evicted.
        case.claim_critique(2)
        case.cancel_critique(2, case.view()['advisor']['critique']['end'])
        state = plain(case)
        self.assertEqual((state['advisor']['total'], state['earlier_events_omitted']), (66, 2))
        self.restored(state)
        tampered = copy.deepcopy(state)
        tampered['events'][-1]['actor'] = 'operator'
        tampered['advisor']['actors'] = {'operator': 63, 'agent': 0, 'skellywix': 3}
        self.refused(tampered)

    def test_ready_critique_can_cite_the_oldest_retained_event(self):
        case = two_files()
        for _ in range(HISTORY):
            case.operator_call(2, 'export_report', {})
        case.start_review(2, None, True)
        job = case.claim_critique(2)
        first = case.events[0]['n']
        case.finish_critique(2, job['ticket'], {'text': 'Retained history only.', 'cites': [first, case.number]})
        state = plain(case)
        self.restored(state)
        state['advisor']['critique']['cites'] = [first - 1]
        self.refused(state)


class ParseTests(RestoreBase):
    def setUp(self):
        self.text = compact(plain(ready_case()))

    def refused_text(self, text):
        with self.assertRaises(CaseRestoreError) as caught:
            restore_case(parse_case(text.encode('utf-8') if isinstance(text, str) else text))
        self.assertTrue(str(caught.exception).endswith('Nothing was loaded.'))
        return str(caught.exception)

    def test_valid_text_parses(self):
        self.assertEqual(restore_case(parse_case(self.text.encode())).view(), json.loads(self.text))

    def test_duplicate_fields_anywhere_are_refused(self):
        self.assertIn('repeats', self.refused_text('{"revision":2,' + self.text[1:]))
        self.assertIn('repeats', self.refused_text(self.text.replace('"actor":', '"actor":"operator","actor":', 1)))
        self.assertIn('repeats', self.refused_text(self.text.replace('"end":8', '"end":8,"end":8', 1)))
        self.assertIn('repeats', self.refused_text(self.text.replace('"a":{', '"a":{"size":3,', 1)))

    def test_non_json_numbers_and_encodings_are_refused(self):
        for text in (self.text.replace('"revision":2', '"revision":2.0'), self.text.replace('"revision":2', '"revision":2e0'),
                     self.text.replace('"remaining":0', '"remaining":NaN'), self.text.replace('"remaining":0', '"remaining":Infinity'),
                     self.text.replace('"remaining":0', '"remaining":-Infinity'), self.text.replace('"revision":2', '"revision":02'),
                     '', '   ', '{', self.text + '{}', self.text + 'x', 'null', '[]', '"x"', '\ufeff' + self.text,
                     '[' * 200000, '{"a":' * 5000):
            with self.subTest(text[:40]):
                self.refused_text(text)
        self.refused_text(self.text.encode() + b'\xff')
        self.refused_text(b'\xef\xbb\xbf' + self.text.encode())
        self.refused_text(self.text.encode('utf-16'))

    def test_surrounding_whitespace_is_accepted(self):
        restore_case(parse_case(('\n  ' + self.text + '\n').encode()))

    def test_oversize_input_is_refused_before_parsing(self):
        with self.assertRaises(CaseRestoreError):
            parse_case(b' ' * (MAX_CASE_BYTES + 1))


@unittest.skipUnless(POSIX, 'owner and private-mode checks need a POSIX host')
class FileTests(RestoreBase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        os.chmod(self.directory, 0o700)
        self.text = compact(plain(ready_case()))

    def save(self, name='case.json', data=None, mode=0o600):
        path = self.directory / name
        path.write_bytes(self.text.encode() if data is None else data)
        os.chmod(path, mode)
        return path

    def refused_path(self, path, contains=None):
        with self.assertRaises(CaseRestoreError) as caught:
            load_case(path)
        text = str(caught.exception)
        self.assertTrue(text.startswith('Case not restored: ') and text.endswith('Nothing was loaded.'), text)
        if contains:
            self.assertIn(contains, text)

    def test_private_regular_file_loads_the_exact_case(self):
        for mode in (0o600, 0o400):
            path = self.save(mode=mode)
            loaded = load_case(path)
            self.assertEqual(loaded.view(), json.loads(self.text))
            self.assertIsInstance(loaded, Case)
            self.assertIsNone(loaded.token)
            os.chmod(path, 0o600)

    def test_missing_unreadable_or_wrongly_typed_paths_are_refused(self):
        self.refused_path(self.directory / 'missing.json')
        self.refused_path(self.directory, 'regular file')
        os.mkfifo(self.directory / 'pipe')
        self.refused_path(self.directory / 'pipe', 'regular file')

    def test_symlink_is_refused_even_to_a_valid_private_file(self):
        target = self.save('real.json')
        link = self.directory / 'link.json'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('symbolic links unavailable')
        self.refused_path(link, 'symbolic link')

    def test_loose_modes_are_refused(self):
        for mode in (0o644, 0o640, 0o604, 0o660, 0o666, 0o700, 0o610, 0o601):
            with self.subTest(oct(mode)):
                self.refused_path(self.save(mode=mode), 'private')

    def test_other_owner_is_refused(self):
        path = self.save()
        with patch('case_recovery.os.geteuid', return_value=os.geteuid() + 1):
            self.refused_path(path, 'owned')

    def test_shared_directory_is_refused(self):
        path = self.save()
        os.chmod(self.directory, 0o770)
        try:
            self.refused_path(path, 'writable')
        finally:
            os.chmod(self.directory, 0o700)
        load_case(path)

    def test_oversize_file_is_refused(self):
        self.refused_path(self.save('big.json', b' ' * (MAX_CASE_BYTES + 1)), 'larger')
        load_case(self.save('edge.json', self.text.encode() + b' ' * (MAX_CASE_BYTES - len(self.text))))

    def test_bad_content_in_a_private_file_is_refused(self):
        for data in (b'', b'not json', b'\xff\xfe', b'{"revision":0}', self.text.encode()[:-5]):
            with self.subTest(data[:12]):
                self.refused_path(self.save('bad.json', data))
        grant = plain(ready_case())
        grant['token'] = 'SECRET'
        self.refused_path(self.save('grant.json', compact(grant).encode()), 'unexpected field')

    def test_file_content_is_never_executed_or_repeated(self):
        marker = self.directory / 'ran.txt'
        state = plain(ready_case())
        state['__import__'] = f'open({str(marker)!r}, "w")'
        state['advisor']['critique']['text'] = f'__import__("os").remove({str(marker)!r})'
        path = self.save('evil.json', compact(state).encode())
        self.refused_path(path, 'unexpected field')
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
