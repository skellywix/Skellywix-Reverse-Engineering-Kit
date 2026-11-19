import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workbench'))
from case_state import Case, CaseError


A = {'size': 3, 'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad', 'format':'unknown','architecture':'unknown'}
B = {'size': 0, 'sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'format':'unknown','architecture':'unknown'}


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.case = Case()
        self.case.admit(0, 'a', A)
        self.case.admit(1, 'b', B)

    def connect(self):
        token = self.case.pair(2)
        self.assertEqual(self.case.view()['connection'], 'ready')
        sid = self.case.begin(token, '2025-11-25')
        self.assertEqual(self.case.view()['connection'], 'ready')
        self.case.initialized(token, sid)
        self.assertEqual(self.case.view()['connection'], 'connected')
        return token, sid

    def test_capability_is_not_an_operator_session_or_part_of_report(self):
        token, sid = self.connect()
        for cred, sess in [(sid, token), (token, 'other'), ('other',sid)]:
            with self.assertRaises(CaseError):
                self.case.agent_call(cred, sess, 'identify_file', {'slot': 'a'})
        diff = self.case.agent_call(token, sid, 'compare_builds', {})
        self.assertFalse(diff['identical'])
        report = self.case.agent_call(token, sid, 'export_report', {})
        self.assertEqual(report['slots'], {'a': A, 'b': B})
        self.assertEqual(report['events'][0]['actor'], 'agent')
        self.assertNotIn(token, json.dumps([report, self.case.view()]))
        self.assertNotIn(sid, json.dumps([report, self.case.view()]))

    def test_mutation_revokes_and_stale_revision_cannot_change_case(self):
        token,sid = self.connect()
        self.case.agent_call(token,sid,'identify_file',{'slot':'a'})
        self.assertEqual(self.case.view()['events'][0]['n'], 1)
        with self.assertRaises(CaseError): self.case.admit(1,'a',B)
        self.assertEqual(self.case.view()['slots']['a'], A)
        self.case.admit(2,'a',B)
        with self.assertRaises(CaseError): self.case.agent_call(token,sid,'identify_file',{'slot':'a'})
        self.assertEqual(self.case.view()['slots'], {'a':B})
        self.assertEqual(self.case.view()['events'], [])
        self.case.operator_call(3,'identify_file',{'slot':'a'})
        self.assertEqual(self.case.view()['events'][0]['n'], 1)

    def test_revocation_expiry_and_maximum_calls(self):
        with patch('case_state.monotonic', return_value=1000): token,sid = self.connect()
        with patch('case_state.monotonic', return_value=1900):
            with self.assertRaises(CaseError): self.case.agent_call(token,sid,'identify_file',{'slot':'a'})
            self.assertEqual(self.case.view()['connection'],'expired')
        token,sid = self.connect()
        for _ in range(32): self.case.agent_call(token,sid,'identify_file',{'slot':'a'})
        with self.assertRaises(CaseError): self.case.agent_call(token,sid,'identify_file',{'slot':'a'})
        self.assertEqual(self.case.view()['connection'], 'spent')
        token,sid = self.connect(); self.case.disconnect(2)
        with self.assertRaises(CaseError): self.case.agent_call(token,sid,'identify_file',{'slot':'a'})
        self.assertEqual(self.case.view()['connection'], 'off')

    def test_second_handshake_and_mutable_tool_arguments_are_refused(self):
        token,sid=self.connect()
        with self.assertRaises(CaseError): self.case.begin(token,'2025-11-25')
        with self.assertRaises(ValueError): self.case.agent_call(token,sid,'export_report',{'path':'not-allowed'})
        self.assertNotIn('not-allowed', json.dumps(self.case.report()))
