"""Handler-level acceptance for the private forwarded desk. No job or release is run."""
import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

from fourmilab.desk import Operator


class OperatorAccess(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Operator)
        self.server.authority = '%s:%d' % self.server.server_address
        self.stages = []
        self.package = Mock(return_value=SimpleNamespace(read_bytes=lambda: b'verified package fixture'))
        self.server.desk = SimpleNamespace(update=lambda *args: self.stages.append(args),
            store=SimpleNamespace(summaries=lambda: [], releases=lambda: {}), current=None,
            update_lock=threading.Lock(), update_error=None, broker=SimpleNamespace(peer=None), package=self.package)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def request(self, path, *, action=False, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            conn.request('POST' if action else 'GET', path, b'{"action":"stage"}' if action else None, headers or {})
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    def action_headers(self):
        return {'Origin': 'http://' + self.server.authority, 'Content-Type': 'application/json', 'X-Candidate-Action': '1'}

    def test_opens_and_reads_private_resources_without_credentials(self):
        self.assertEqual(self.request('/state')[0], 200)
        status, shell = self.request('/')
        self.assertEqual(status, 200)
        self.assertNotIn(b'type="password"', shell)
        self.assertNotIn(b'"builder_connected":', shell)
        run_id = 'a' * 32
        status, package = self.request('/package/' + run_id, headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual((status, package), (200, b'verified package fixture'))
        self.package.assert_called_once_with(run_id)

    def test_browser_origin_and_forwarded_host_still_bound_private_reads(self):
        for path in ('/state', '/package/' + 'a' * 32):
            for headers in ({'Host': 'other.invalid'}, {'Origin': 'http://other.invalid'},
                            {'Sec-Fetch-Site': 'same-site'}, {'Sec-Fetch-Site': 'cross-site'}):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.request(path, headers=headers)[0], 403)
        self.package.assert_not_called()

    def test_only_an_explicit_same_origin_json_action_reaches_the_desk(self):
        expected = self.action_headers()
        for headers, status in [
            ({k: v for k, v in expected.items() if k != 'Origin'}, 403),
            ({k: v for k, v in expected.items() if k != 'X-Candidate-Action'}, 403),
            (expected | {'Origin': 'http://other.invalid'}, 403),
            (expected | {'Host': 'other.invalid'}, 403),
            (expected | {'Content-Type': 'text/plain'}, 415),
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/action', action=True, headers=headers)[0], status)
        self.assertEqual(self.stages, [])
        self.assertEqual(self.request('/action', action=True, headers=expected)[0], 200)
        self.assertEqual(self.stages, [('stage', None)])
