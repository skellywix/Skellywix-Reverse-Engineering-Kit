"""Exercise the public feedback boundary with real stored software projects."""
import hashlib
import http.client
import io
import json
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote

from build_desk.desk import Operator
from build_desk.project_jobs import ProjectJobs
from build_desk.project_plan import SITE


class FeedbackHTTP(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.store = Path(tmp, 'projects')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Operator)
        self.server.authority = '%s:%d' % self.server.server_address
        self.server.projects = ProjectJobs(self.store, lambda _: None)
        self.server.project_upload = threading.BoundedSemaphore(1)
        self.origin = {'Origin': 'http://' + self.server.authority, 'X-Candidate-Action': '1'}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server.projects.close()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request(method, path, body, headers or {})
            r = conn.getresponse()
            data = r.read()
            return r.status, json.loads(data) if r.getheader('Content-Type', '').startswith('application/json') else data
        finally:
            conn.close()

    def build(self, text, project=None):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('index.html', '<title>' + text + '</title>')
        identity = {'X-Project-Id': project} if project else {'X-Project-Name': quote('Résumé')}
        status, result = self.req('POST', '/project/build', archive.getvalue(), self.origin | identity | {
            'X-Project-Profile': SITE, 'Content-Type': 'application/octet-stream'})
        self.assertEqual(status, 202, result)
        self.assertTrue(self.server.projects.wait(5))
        return result['id']

    def post(self, path, obj):
        return self.req('POST', path, json.dumps(obj).encode(), self.origin | {'Content-Type': 'application/json'})

    def test_report_retest_and_restart_keep_the_correct_two_artifacts(self):
        first = self.build('Untitled')
        other = self.build('An unrelated program')
        status, library = self.req('GET', '/project/library')
        self.assertEqual(status, 200, library)
        self.assertEqual({(row['id'], row['name']) for row in library['projects']}, {(first, 'Résumé'), (other, 'Résumé')})
        status, issue = self.post('/project/reports/' + first, {'expected': 'Page title is Notes', 'observed': 'Title is Untitled'})
        self.assertEqual(status, 201, issue)
        wrong_hash = hashlib.sha256(self.req('GET', '/project/package/' + first)[1]).hexdigest()
        self.assertEqual((issue['run_id'], issue['sha256'], issue['state']), (first, wrong_hash, 'open'))
        fix = self.build('Notes', first)
        review = {'outcome': 'fixed', 'note': 'Opened the rebuilt page; the title reads Notes.'}
        self.assertIn(self.post(f'/project/reviews/{other}/{issue["id"]}', review)[0], (404, 409, 422))
        self.assertEqual(self.req('GET', '/project/library/' + other)[1]['issues'], [])
        self.assertEqual(self.req('GET', '/project/library/' + first)[1]['issues'][0]['state'], 'open')
        status, closed = self.post(f'/project/reviews/{fix}/{issue["id"]}', review)
        self.assertEqual(status, 201, closed)
        self.server.projects.close()
        self.server.projects = ProjectJobs(self.store, lambda _: None)
        fixed_hash = hashlib.sha256(self.req('GET', '/project/package/' + fix)[1]).hexdigest()
        after = self.req('GET', '/project/library/' + first)[1]
        self.assertEqual([r['id'] for r in after['runs']], [fix, first])
        stored = after['issues'][0]
        self.assertEqual((stored['state'], stored['sha256'], stored['reviews'][0]['run_id'], stored['reviews'][0]['sha256']),
                         ('fixed', wrong_hash, fix, fixed_hash))

    def test_download_sends_the_bytes_identified_by_its_receipt(self):
        first = self.build('Original')
        artifact, digest = self.server.projects.package(first)
        expected = artifact.read_bytes()
        original_headers = Operator._headers

        def change_after_verification(handler, *args, **kwargs):
            artifact.write_bytes(b'x' * len(expected))
            return original_headers(handler, *args, **kwargs)

        with patch.object(Operator, '_headers', change_after_verification):
            status, body = self.req('GET', '/project/package/' + first)
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(body).hexdigest(), digest)
        self.assertEqual(body, expected)
        self.assertEqual(self.req('GET', '/project/package/' + first)[0], 409)

    def test_feedback_requires_the_same_origin_and_rejects_forged_identity(self):
        first = self.build('Notes')
        route = '/project/reports/' + first
        valid = {'expected': 'Red', 'observed': 'Blue'}
        self.assertEqual(self.req('POST', route, json.dumps(valid), {'Content-Type': 'application/json',
                         'Origin': 'http://unrelated.invalid', 'X-Candidate-Action': '1'})[0], 403)
        self.assertIn(self.post(route, valid | {'sha256': 'a' * 64})[0], (400, 422))
        self.assertEqual(self.req('GET', '/project/library/' + first, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.req('GET', '/project/library/' + first)[1]['issues'], [])
