"""The fixed Windows game route must not inherit broader build authority."""
import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from build_desk.desk import Operator
from build_desk.game_jobs import Unqualified


class GameHTTP(unittest.TestCase):
    def setUp(self):
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Operator)
        self.http.authority = '%s:%d' % self.http.server_address
        self.http.games = Mock()
        self.job = 'f' * 32
        self.http.games.status.return_value = {'id': self.job, 'state': 'waiting_windows'}
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.thread.join(2)
        self.http.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.http.server_address, timeout=2)
        try:
            conn.request(method, path, body, headers or {})
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    def test_game_desk_has_no_generic_run_or_package_endpoints(self):
        for path in ('/app.js', '/state', '/package/' + self.job):
            self.assertEqual(self.request('GET', path)[0], 404, path)
        headers = {'Origin': 'http://' + self.http.authority, 'Content-Type': 'application/json',
                   'X-Candidate-Action': '1'}
        self.assertEqual(self.request('POST', '/action', b'{"action":"start"}', headers)[0], 404)
        self.http.games.start.assert_not_called()

    def test_saved_game_url_reopens_desk_without_starting_a_new_run(self):
        status, html = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertEqual(self.request('GET', '/?game=' + self.job), (200, html))
        for query in ('?game=missing', '?game=' + self.job + '&game=' + self.job):
            self.assertEqual(self.request('GET', '/' + query)[0], 404)
        self.http.games.start.assert_not_called()

    def test_cross_origin_cannot_read_even_one_job_or_attempt_download(self):
        for path in ('/game/status/' + self.job, '/game/package/' + self.job):
            self.assertEqual(self.request('GET', path, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.http.games.status.assert_not_called()
        self.http.games.package.assert_not_called()
        self.assertEqual(self.request('GET', '/game/status/' + self.job)[0], 200)
        self.http.games.status.assert_called_once_with(self.job)

    def test_unqualified_game_cannot_be_downloaded(self):
        self.http.games.package.side_effect = Unqualified('Windows comparison incomplete')
        status, body = self.request('GET', '/game/package/' + self.job)
        self.assertEqual(status, 409)
        self.assertNotIn(b'PK\x03\x04', body)
        self.http.games.package.assert_called_once_with(self.job)

    def test_upload_requires_origin_fixed_request_and_reference_release(self):
        headers = {'Origin': 'http://' + self.http.authority, 'Content-Type': 'application/zip',
                   'X-Candidate-Action': '1', 'X-Game-Request': 'Rebuild Neverball 1.6.0 in Rust for Windows.'}
        for change, expected in [({'Origin': 'http://unrelated.invalid'}, 403),
                                 ({'X-Candidate-Action': '0'}, 403),
                                 ({'Content-Type': 'application/json'}, 415),
                                 ({'X-Game-Request': 'Use a different program'}, 422), ({}, 422)]:
            with self.subTest(change=change):
                self.assertEqual(self.request('POST', '/game/build', b'not the release', headers | change)[0], expected)
        self.http.games.start.assert_not_called()

    def test_missing_profile_is_not_an_empty_queue_or_a_ready_builder(self):
        self.http.games = None
        self.assertEqual(self.request('GET', '/game/status')[0], 503)
