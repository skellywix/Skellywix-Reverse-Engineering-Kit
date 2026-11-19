import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workbench'))
from omp_critic import review_next, validate_brief


class CriticBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.brief = {'samples': ['a'], 'total': 1, 'mismatches': 0, 'omitted': 0, 'events': [
            {'n': 1, 'actor': 'operator', 'check': 'identify_file', 'slot': 'a', 'outcome': 'recognized'}]}

    def test_private_fields_and_free_text_never_become_a_prompt(self):
        validate_brief(self.brief)
        for level, key, value in [('case', 'path', '/private'), ('case', 'total', 'file-name'), ('case', 'samples', ['customer']),
                                  ('row', 'sha256', 'a' * 64), ('row', 'actor', 'instructions'), ('row', 'outcome', 'user-supplied text'),
                                  ('row', 'slot', 'private name'), ('row', 'n', True), ('row', 'check', 'other')]:
            with self.subTest(key=key):
                packet = copy.deepcopy(self.brief)
                target = packet if level == 'case' else packet['events'][0]
                target[key] = value
                with self.assertRaises(ValueError): validate_brief(packet)

    def test_summary_and_event_window_must_agree(self):
        for change in [{'events': []}, {'omitted': 1}, {'mismatches': 2}, {'events': self.brief['events'] * 65}]:
            with self.assertRaises(ValueError): validate_brief(self.brief | change)

    def test_spare_quota_on_one_subscription_cannot_approve_another(self):
        def report(provider, used):
            return {'provider': provider, 'limits': [{'window': {'durationMs': 604800000}, 'amount': {'usedFraction': used}}]}
        low = report('anthropic', .2)
        for other in (report('openai-codex', .95), report('openai-codex', True), {'provider': 'openai-codex', 'limits': []}):
            with self.subTest(other=other):
                usage = {'reports': [low, other], 'accountsWithoutUsage': []}
                status = {'revision': 1, 'loaded': True, 'status': 'pending'}
                model = Mock()
                with patch('omp_critic.subprocess.run', return_value=Mock(stdout=json.dumps(usage))), \
                     patch('omp_critic._request', side_effect=[status, {'ticket': 'r', 'brief': self.brief}, {}, {}]), \
                     self.assertRaisesRegex(RuntimeError, 'reserve'):
                    review_next(model, wait_seconds=0)
                model.assert_not_called()

    def test_another_adapter_claiming_is_not_reported_as_no_request(self):
        status = {'revision': 1, 'loaded': True, 'status': 'idle'}
        model = Mock()
        with patch('omp_critic._check_reserve'), patch('omp_critic.time.sleep'), \
             patch('omp_critic._request', side_effect=[status, status | {'status': 'reviewing'}]), \
             self.assertRaisesRegex(RuntimeError, 'already claimed'):
            review_next(model, wait_seconds=1)
        model.assert_not_called()
