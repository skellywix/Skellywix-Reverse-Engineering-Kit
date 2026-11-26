"""Linux observer lifetime: a corpus must not depend on cyclic garbage collection."""
import gc
import os
import subprocess
import time
import unittest

from fourmilab.contract import _pump


class ObserverLifetime(unittest.TestCase):
    def test_completed_cases_release_poll_descriptors(self):
        def pollers():
            names = []
            for fd in os.listdir('/proc/self/fd'):
                try:
                    names.append(os.readlink('/proc/self/fd/' + fd))
                except FileNotFoundError:
                    # listdir's own directory handle has already closed.
                    continue
            return names.count('anon_inode:[eventpoll]')
        before = pollers()
        enabled = gc.isenabled()
        gc.disable()
        try:
            for _ in range(8):
                with subprocess.Popen(['/bin/cat'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                    self.assertEqual(_pump(proc, b'abc', time.monotonic() + 2), (b'abc', b''))
            self.assertEqual(pollers(), before)
        finally:
            if enabled:
                gc.enable()
