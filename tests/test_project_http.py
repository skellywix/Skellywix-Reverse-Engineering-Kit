"""Public build and maintenance contracts, independent of a compiler or registry."""
import hashlib
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from build_desk.desk import Operator


class ProjectHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory())
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Operator)
        self.http.authority = '%s:%d' % self.http.server_address
        self.http.games = None
        self.http.projects = Mock()
        self.http.project_upload = threading.BoundedSemaphore(1)
        self.http.maintenance = Mock()
        self.id = 'c' * 32
        self.http.projects.profiles.return_value = [{'id': 'web', 'available': True}]
        self.http.projects.start.return_value = self.id
        self.http.maintenance.update.return_value = {'state': 'updating', 'components': []}
        self.origin = {'Origin': 'http://' + self.http.authority, 'X-Candidate-Action': '1'}
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()

    def req(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection(*self.http.server_address, timeout=3)
        try:
            conn.request(method, path, data, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_title_independent_upload_and_result_are_bound_to_one_job(self):
        self.assertEqual(json.loads(self.req('GET', '/project/profiles')[2])['profiles'][0]['id'], 'web')
        headers = self.origin | {'X-Project-Profile': 'web', 'Content-Type': 'application/octet-stream'}
        status, _, response = self.req('POST', '/project/build', b'project data', headers)
        self.assertEqual((status, json.loads(response)), (202, {'id': self.id}))
        self.http.projects.start.assert_called_once_with('web', b'project data')
        artifact = Path(self.tmp, 'site.zip')
        artifact.write_bytes(b'example')
        digest = hashlib.sha256(b'example').hexdigest()
        self.http.projects.open_package.return_value = artifact.name, 7, digest, artifact.open('rb')
        status, headers, body = self.req('GET', '/project/package/' + self.id)
        self.assertEqual((status, body), (200, b'example'))
        self.assertIn('site.zip', headers['Content-Disposition'])
        self.assertEqual(headers['ETag'], f'"{digest}"')
        self.http.projects.open_package.assert_called_once_with(self.id)
        self.http.projects.open_package.side_effect = RuntimeError('still building')
        self.assertEqual(self.req('GET', '/project/package/' + self.id)[0], 409)

    def test_update_has_no_remote_arguments_and_no_cross_origin_authority(self):
        self.assertEqual(self.req('POST', '/maintenance/update', b'', {'Origin': 'http://other.invalid'})[0], 403)
        self.assertEqual(self.req('POST', '/maintenance/update', b'{"url":"example.invalid"}', self.origin)[0], 400)
        self.http.maintenance.update.assert_not_called()
        self.assertIn(self.req('POST', '/maintenance/update', b'', self.origin)[0], (200, 202))
        self.http.maintenance.update.assert_called_once_with()

    def test_saved_links_do_not_create_jobs_or_disclose_across_origins(self):
        self.assertEqual(self.req('GET', '/?project=' + self.id)[0], 200)
        self.assertEqual(self.req('GET', '/?project=nope')[0], 404)
        self.assertEqual(self.req('GET', '/project/status/' + self.id, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.http.projects.start.assert_not_called()
        self.http.projects.status.assert_not_called()
