"""An admitted attempt must not monopolize the desk when startup fails."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fourmilab import pipeline


class StartupRecovery(unittest.TestCase):
    def exercise(self, cause):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, 'check_reference'):
            broker = SimpleNamespace(model='fixed', version='fixed', connection_hash='a' * 64)
            desk = pipeline.Desk(Path(tmp), broker)
            desk.store.promote(desk.store.stage({'kit': 'fixed'}))
            failure = (patch.object(desk, 'view', side_effect=OSError('projection offline'))
                       if cause == 'view' else patch.object(threading.Thread, 'start', side_effect=OSError('worker unavailable')))
            with failure:
                with self.assertRaises((ValueError, OSError)):
                    desk.start()
                [row] = desk.store.summaries()
                self.assertEqual(row['status'], 'blocked')
                self.assertIsNone(desk.current)
                # The same fault can recur. It must still admit a new attempt on the original run.
                with self.assertRaises((ValueError, OSError)):
                    desk.start(retry=row['id'])
            events = desk.store.run(row['id'])['events']
            self.assertEqual([(e['attempt'], e['status']) for e in events],
                             [(1, 'queued'), (1, 'blocked'), (2, 'queued'), (2, 'blocked')])
            self.assertIsNone(desk.current)

    def test_worker_start_failure_releases_slot_and_allows_same_run_retry(self):
        self.exercise('thread')

    def test_view_write_failure_releases_slot_and_allows_same_run_retry(self):
        self.exercise('view')
