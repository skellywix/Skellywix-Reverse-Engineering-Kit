"""Acceptance checks for the candidate evidence ledger and the live input contract."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fourmilab import contract
from fourmilab.store import Store


class PackageProof(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'db')
        self.store.promote(self.store.stage({'kit': 'first'}))
        self.run_id = self.store.start({'required_cases': 2})
        self.proof = {'run': self.run_id, 'attempt': 1, 'generation': 1,
                      'manifest_sha256': self.store.run(self.run_id)['manifest_hash'],
                      'required': 2, 'matched': 2, 'artifact_sha256': 'a' * 64,
                      'source_sha256': 'b' * 64, 'reference_rows_sha256': 'c' * 64,
                      'candidate_rows_sha256': 'd' * 64}

    def receipt(self, proof):
        return {'completed': proof['matched'], 'required': proof['required'], 'evidence': proof}

    def package(self, proof, attempt=1):
        self.store.event(self.run_id, attempt, 'package', 'verified',
                         {**self.receipt(proof), 'package_sha256': 'e' * 64})

    def verify(self, proof):
        self.store.event(self.run_id, 1, 'verify', 'running',
                         {'generation': 1, 'artifact_sha256': 'a' * 64, 'completed': 0, 'required': 2})
        self.store.event(self.run_id, 1, 'verify', 'completed', self.receipt(proof))

    def test_package_without_verifier_receipt_never_becomes_downloadable(self):
        with self.assertRaises(ValueError):
            self.package(self.proof)
        self.assertNotEqual('verified', self.store.run(self.run_id)['status'])

    def test_mixed_artifact_is_not_its_own_verification(self):
        self.verify(self.proof)
        with self.assertRaises(ValueError):
            self.package({**self.proof, 'artifact_sha256': 'f' * 64})
        self.package(self.proof)
        self.assertEqual('verified', self.store.run(self.run_id)['status'])

    def test_partial_coverage_cannot_qualify(self):
        bad = {**self.proof, 'matched': 1}
        with self.assertRaises(ValueError):
            self.verify(bad)
            self.package(bad)
        self.assertNotEqual('verified', self.store.run(self.run_id)['status'])

    def test_retry_cannot_package_first_attempts_evidence(self):
        self.verify(self.proof)
        self.store.event(self.run_id, 1, 'package', 'blocked', {'reason': 'disk unavailable'})
        attempt = self.store.retry(self.run_id)
        with self.assertRaises(ValueError):
            self.package(self.proof, attempt)
        self.assertEqual('queued', self.store.run(self.run_id)['status'])


@unittest.skipUnless(sys.platform.startswith('linux'), 'Observer runs in the Linux container')
class LiveInput(unittest.TestCase):
    def observe(self, read):
        def fixture(*_args):
            return subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.' + read + '; sys.stdout.write("M")'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    bufsize=0, start_new_session=True)
        with patch.object(contract, '_spawn', fixture), patch.object(contract, 'LIVE_TIMEOUT', 0.8):
            return contract._run(contract.Case(contract.LIVE_PIPE, ('-d',), b'', live=True), '.', None)

    def test_eof_only_consumer_fails_while_writer_stays_open(self):
        with self.assertRaisesRegex(contract.CaseError, 'timed out'):
            self.observe('read()')

    def test_one_complete_block_is_sufficient_with_writer_open(self):
        self.assertEqual(self.observe('read(256)'), (0, b'M', b''))
