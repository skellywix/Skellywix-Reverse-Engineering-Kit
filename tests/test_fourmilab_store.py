import tempfile
import unittest
from pathlib import Path

from fourmilab.store import Store


class ReplayTests(unittest.TestCase):
    def test_staging_does_not_promote_and_retries_never_rebind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidate.sqlite'
            store = Store(path)
            first = store.stage({'kit': 'v1', 'rea': 'v1'})
            store.promote(first)
            a = store.start({'contract': 'x'})
            store.event(a, 1, 'rebuild', 'blocked', {'reason': 'provider offline'})
            second = store.stage({'kit': 'v2', 'rea': 'v2'})
            self.assertEqual(store.run(a)['manifest']['release'], first)
            self.assertEqual(store.releases()['active'], first)
            store.promote(second)
            b = store.start({'contract': 'y'})
            self.assertEqual(store.retry(a), 2)
            self.assertEqual(store.run(a)['manifest']['release'], first)
            store.rollback()
            other = Store(path)
            self.assertEqual(other.releases()['active'], first)
            self.assertEqual(other.run(b)['manifest']['release'], second)
            log = other.run(a)['events']
            self.assertEqual([e['seq'] for e in log], [1, 2, 3])
            self.assertEqual([(e['attempt'], e['status']) for e in log],
                             [(1, 'queued'), (1, 'blocked'), (2, 'queued')])
            with self.assertRaises(ValueError):
                other.event(a, 1, 'verify', 'verified', {})

    def test_empty_bootstrap_and_unknown_releases_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db')
            with self.assertRaises(ValueError):
                store.start({'contract': 'x'})
            with self.assertRaises(ValueError):
                store.promote('b' * 64)
            self.assertIsNone(store.releases()['active'])
