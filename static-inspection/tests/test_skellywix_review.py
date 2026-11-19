"""Operator-approved reviews of metadata; a critic is never a case tool caller."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workbench'))
from case_state import Case, CaseError, HISTORY
from evidence import summarize


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = Case()
        self.a = summarize(b'private fixture A; do not disclose')
        self.case.admit(0, 'a', self.a)

    def check(self, digest):
        self.case.operator_call(1, 'verify_checksum', {'slot': 'a', 'sha256': digest})

    def test_single_input_is_not_its_own_reference(self):
        self.case.start_review(1, None, False)
        state = self.case.view()
        self.assertEqual([(e['actor'], e['tool'], e['slot']) for e in state['events']], [('skellywix', 'identify_file', 'a')])
        self.assertEqual(state['advisor']['skipped'], ['compare_builds', 'verify_checksum'])
        self.assertEqual(state['advisor']['critique']['status'], 'not_requested')
        with self.assertRaises(CaseError):
            self.case.claim_critique(1)

    def test_only_fixed_checks_and_redacted_evidence_reach_critic(self):
        self.case.admit(1, 'b', summarize(b'other private fixture'))
        self.case.start_review(2, '0' * 64, True)
        job = self.case.claim_critique(2)
        self.assertEqual(job['brief'], {'samples': ['a', 'b'], 'total': 4, 'mismatches': 1, 'omitted': 0, 'events': [
            {'n': 1, 'actor': 'skellywix', 'check': 'identify_file', 'slot': 'a', 'outcome': 'unclassified'},
            {'n': 2, 'actor': 'skellywix', 'check': 'identify_file', 'slot': 'b', 'outcome': 'unclassified'},
            {'n': 3, 'actor': 'skellywix', 'check': 'compare_builds', 'outcome': 'different'},
            {'n': 4, 'actor': 'skellywix', 'check': 'verify_checksum', 'slot': 'a', 'outcome': 'mismatch'},
        ]})
        with self.assertRaises(CaseError): self.case.claim_critique(2)
        for secret in (job['ticket'], self.a['sha256'], 'private fixture', '0' * 64):
            self.assertNotIn(secret, json.dumps(job['brief']))
        with self.assertRaises(CaseError): self.case.finish_critique(2, job['ticket'], {'text': 'Unsupported.', 'cites': [99]})
        self.case.finish_critique(2, job['ticket'], {'text': 'The reference does not match A.', 'cites': [4]})
        self.assertEqual(self.case.view()['advisor']['critique']['text'], 'The reference does not match A.')
        self.assertEqual(len(self.case.report()['events']), 4)
        self.assertNotIn('reference does not match A', json.dumps(self.case.report()))
        with self.assertRaises(CaseError): self.case.finish_critique(2, job['ticket'], None)

    def test_whole_case_counts_survive_detail_eviction_but_not_replacement(self):
        self.check('0' * 64)
        for _ in range(HISTORY): self.check(self.a['sha256'])
        self.case.start_review(1, None, True)
        brief = self.case.claim_critique(1)['brief']
        self.assertEqual((brief['mismatches'], brief['total'], brief['omitted'], len(brief['events'])), (1, HISTORY + 2, 2, HISTORY))
        self.assertFalse(any(e['outcome'] == 'mismatch' for e in brief['events']))
        self.assertEqual(self.case.view()['advisor']['actors']['operator'], HISTORY + 1)
        self.case.admit(1, 'a', self.a)
        self.assertEqual((self.case.view()['advisor']['mismatches'], self.case.view()['advisor']['critique']['status']), (0, 'idle'))

    def test_stale_cancelled_expired_or_failed_is_never_a_verdict(self):
        for stop in ('event', 'case', 'cancel', 'expiry', 'provider'):
            with self.subTest(stop=stop), patch('case_state.monotonic', return_value=100):
                c = Case(); c.admit(0, 'a', self.a); c.start_review(1, None, True); job = c.claim_critique(1)
                if stop == 'event': c.operator_call(1, 'identify_file', {'slot': 'a'})
                elif stop == 'case': c.reset(1)
                elif stop == 'cancel': c.cancel_critique(1, job['brief']['total'])
                elif stop == 'expiry':
                    with patch('case_state.monotonic', return_value=2000): self.assertEqual(c.view()['advisor']['critique']['status'], 'expired')
                else: c.finish_critique(1, job['ticket'], None)
                with self.assertRaises(CaseError): c.finish_critique(1, job['ticket'], {'text': 'Late.', 'cites': [1]})
                self.assertNotIn('text', c.view()['advisor']['critique'])
                self.assertNotEqual(c.view()['advisor']['critique']['status'], 'ready')

    def test_invalid_review_is_atomic_and_cannot_add_work(self):
        for rev, reference, enabled in [(0, None, True), (1, 'bad', True), (1, None, 'true'), (1, {'tool': 'other'}, False)]:
            with self.assertRaises((ValueError, CaseError)): self.case.start_review(rev, reference, enabled)
        self.assertEqual(self.case.report()['events'], [])

    def test_pending_request_cannot_be_duplicated_or_silently_replaced(self):
        self.case.start_review(1, None, True)
        with self.assertRaises(CaseError): self.case.start_review(1, None, False)
        job = self.case.claim_critique(1)
        with self.assertRaises(CaseError): self.case.start_review(1, None, True)
        self.assertEqual(len(self.case.report()['events']), 1)
        self.case.finish_critique(1, job['ticket'], {'cites': [1], 'text': 'The specimen was not classified.'})

    def test_expiration_precedes_a_later_finding_with_or_without_a_poll(self):
        for poll in (False, True):
            c = Case(); c.admit(0, 'a', self.a)
            with patch('case_state.monotonic', return_value=100):
                c.start_review(1, None, True); c.claim_critique(1)
            with self.subTest(poll=poll), patch('case_state.monotonic', return_value=250):
                if poll: c.view()
                c.operator_call(1, 'identify_file', {'slot': 'a'})
                self.assertEqual(c.view()['advisor']['critique']['status'], 'expired')


if __name__ == '__main__':
    unittest.main()
