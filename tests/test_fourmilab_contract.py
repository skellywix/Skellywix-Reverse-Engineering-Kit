import unittest

from fourmilab import contract


class NamedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.expected = [{'id': 'encode/3', 'exit': 0, 'out': 'YWJj'},
                         {'id': 'decode/live-pipe', 'exit': 0, 'out': 'M'}]

    def reject(self, observed, case):
        with self.assertRaisesRegex(ValueError, case):
            contract.match(['encode/3', 'decode/live-pipe'], self.expected, observed)

    def test_missing_result_cannot_be_filled_by_a_repeat(self):
        self.reject([self.expected[0], self.expected[0]], 'decode/live-pipe')

    def test_one_byte_difference_prevents_verification(self):
        self.reject([self.expected[0], {**self.expected[1], 'out': 'N'}], 'decode/live-pipe')

    def test_required_cases_and_evidence_can_arrive_in_different_orders(self):
        self.assertEqual(contract.match(['encode/3', 'decode/live-pipe'], self.expected,
                                        self.expected[::-1]), 2)

    def test_live_pipe_is_one_of_the_unique_declared_cases(self):
        ids = contract.case_ids()
        self.assertEqual(len(set(ids)), 2557)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('decode/live-pipe', ids)
