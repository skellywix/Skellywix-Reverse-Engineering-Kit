"""Real HTTP requests over separate owner-local sockets; no Docker or model needed."""
import http.client
import json
import socket
import socketserver
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from threading import Lock, Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workbench'))
from case_state import Case
from server import Handler


class SocketClient(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(3)
        self.sock.connect(self.path)



@unittest.skipUnless(hasattr(socketserver, 'UnixStreamServer'), 'requires Unix sockets')
class WorkspaceNavigationTests(unittest.TestCase):
    def test_standalone_and_paired_operator_addresses(self):
        workbench = Path(__file__).resolve().parents[1] / 'workbench'
        for paired in (False, True):
            with self.subTest(paired=paired), tempfile.TemporaryDirectory() as tmp:
                desk = str(Path(tmp) / 'desk.sock')
                args = [sys.executable, '-IB', str(workbench / 'server.py'), '--ipc', tmp,
                        '--web', str(workbench / 'ui' / 'dist')]
                if paired:
                    args.append('--paired')
                with subprocess.Popen(args, stderr=subprocess.PIPE) as proc:
                    try:
                        deadline = time.monotonic() + 5
                        while not Path(desk).is_socket():
                            if proc.poll() is not None:
                                self.fail(proc.stderr.read().decode())
                            self.assertLess(time.monotonic(), deadline, 'desk socket did not appear')
                            time.sleep(.01)

                        def fetch(method, target, **headers):
                            conn = SocketClient(desk)
                            try:
                                conn.request(method, target, headers=headers)
                                res = conn.getresponse()
                                return res.status, res.getheader('Location'), res.read()
                            finally:
                                conn.close()

                        for method in ('GET', 'HEAD'):
                            status, location, _ = fetch(method, '/')
                            self.assertEqual((status, location), (302, 'http://127.0.0.1:48784/') if paired else (200, None))
                        status, location, page = fetch('GET', '/index.html')
                        self.assertEqual((status, location), (200, None))
                        self.assertEqual(b'<meta name="skellywix-workspace" content="paired">' in page, paired)
                        self.assertEqual(fetch('GET', '/', Host='wrong.example')[:2], (403, None))
                        self.assertEqual(fetch('GET', '/api/state', Origin='http://127.0.0.1:48784')[:2], (403, None))
                    finally:
                        proc.terminate()

@unittest.skipUnless(hasattr(socketserver, 'UnixStreamServer'), 'requires Unix sockets')
class TransportTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        ipc = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.paths = {k: str(ipc / k) for k in ('desk', 'agent')}
        case, lock = Case(), Lock()
        for name, path in self.paths.items():
            handle = partial(Handler, case=case, lock=lock, desk=name == 'desk', assets={})
            listener = stack.enter_context(socketserver.UnixStreamServer(path, handle))
            thread = Thread(target=listener.serve_forever, kwargs={'poll_interval': .02})
            thread.start()
            stack.callback(thread.join)
            stack.callback(listener.shutdown)

    def request(self, channel, path, body=None, **headers):
        conn = SocketClient(self.paths[channel])
        try:
            if isinstance(body, dict):
                body = json.dumps(body).encode()
                headers['Content-Type'] = 'application/json'
            write_error = None
            try:
                conn.request('GET' if body is None else 'POST', path, body, headers)
            except BrokenPipeError as exc:
                if body is None:
                    raise
                # The handler can send its rejection before the client writes the body.
                write_error = exc
            with conn.getresponse() as res:
                if write_error is not None and not 400 <= res.status < 600:
                    raise write_error
                data = res.read()
                return res.status, json.loads(data) if data else None, dict(res.getheaders())
        finally:
            conn.close()

    def test_upload_and_scope_rejections(self):
        api = {'Origin': 'http://localhost', 'Content-Type': 'application/octet-stream', 'X-Yuzu-Revision': '0'}
        self.assertEqual(self.request('desk','/api/sample/a',b'abc',**(api | {'Origin':'https://wrong.example'}))[0],403)
        self.assertEqual(self.request('desk','/api/sample/a',b'abc',**(api | {'Host':'wrong.example'}))[0],403)
        self.assertEqual(self.request('agent','/api/sample/a',b'abc',**api)[0],404)
        code, state, _ = self.request('desk','/api/sample/a',b'abc',**api)
        self.assertEqual(code,200)
        self.assertEqual(state['slots']['a']['sha256'],'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
        self.assertEqual(self.request('desk','/api/sample/a',b'other',**api)[0],409)
        self.assertEqual(self.request('desk','/mcp',{},Origin='http://localhost')[0],404)
        self.assertEqual(self.request('agent','/mcp',{})[0],401)

    def test_native_lifecycle_and_revocation(self):
        origin = {'Origin':'http://localhost','X-Yuzu-Revision':'0','Content-Type':'application/octet-stream'}
        self.request('desk','/api/sample/a',b'fixture-private-content',**origin)
        op = {'Origin':'http://localhost','X-Yuzu-Revision':'1'}
        code, row, _ = self.request('desk','/api/pair',{},**op)
        self.assertEqual(code,200)
        self.assertEqual(row['state']['connection'],'ready')
        cred = {'Authorization':'Bearer '+row['token']}
        init = {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','clientInfo':{'name':'test','version':'1'},'capabilities':{}}}
        code, result, hdr = self.request('agent','/mcp',init,**cred)
        self.assertEqual((code, result['result']['capabilities']), (200, {'tools':{}}))
        auth = cred | {'Mcp-Session-Id': hdr['Mcp-Session-Id']}
        listing = {'jsonrpc':'2.0','id':2,'method':'tools/list'}
        self.assertEqual(self.request('agent','/mcp',listing,**auth)[0],400)
        self.assertEqual(self.request('agent','/mcp',{'jsonrpc':'2.0','method':'notifications/initialized'},**auth)[0],202)
        code, data, _ = self.request('agent','/mcp',listing,**auth)
        self.assertEqual(code,200)
        self.assertEqual({t['name'] for t in data['result']['tools']},{'identify_file','verify_checksum','compare_builds','export_report'})
        call = {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'identify_file','arguments':{'slot':'a'}}}
        code, data, _ = self.request('agent','/mcp',call,**auth)
        self.assertEqual((code,data['result']['structuredContent']['size']), (200,23))
        code, report, _ = self.request('desk','/api/report')
        self.assertEqual((code,report['events'][0]['actor']),(200,'agent'))
        for private in ('fixture-private-content',row['token'],hdr['Mcp-Session-Id']):
            self.assertNotIn(private,json.dumps(report))
        self.assertEqual(self.request('desk','/api/disconnect',{},**op)[0],200)
        self.assertEqual(self.request('agent','/mcp',call,**auth)[0],401)

    def test_only_an_operator_approved_snapshot_can_receive_a_critic_note(self):
        op = {'Origin': 'http://localhost', 'X-Yuzu-Revision': '0', 'Content-Type': 'application/octet-stream'}
        _, state, _ = self.request('desk', '/api/sample/a', b'private specimen', **op)
        op = {'Origin': 'http://localhost', 'X-Yuzu-Revision': str(state['revision'])}
        choice = {'reference': None, 'critique': True}
        self.assertEqual(self.request('agent', '/api/review', choice, **op)[0], 404)
        self.assertEqual(self.request('agent', '/api/critic/claim', {}, **op)[0], 404)
        self.assertEqual(self.request('desk', '/api/review', choice, **(op | {'Origin': 'http://outside.example'}))[0], 403)
        self.assertEqual(self.request('desk', '/api/critic/claim', {}, **op)[0], 409)
        self.assertEqual(self.request('desk', '/api/review', choice, **op)[0], 200)
        code, job, _ = self.request('desk', '/api/critic/claim', {}, **op)
        self.assertEqual(code, 200)
        self.assertNotIn(state['slots']['a']['sha256'], json.dumps(job['brief']))
        wrong = {'ticket': 'wrong-receipt', 'note': {'text': 'No note.', 'cites': [1]}}
        self.assertEqual(self.request('desk', '/api/critic/finish', wrong, **op)[0], 409)
        self.assertEqual(self.request('desk', '/api/critic/cancel', {'end': job['brief']['total']}, **op)[0], 200)
        self.assertEqual(self.request('desk', '/api/critic/finish', wrong | {'ticket': job['ticket']}, **op)[0], 409)
        code, report, _ = self.request('desk', '/api/report')
        self.assertEqual((code, len(report['events'])), (200, 1))
        self.assertNotIn('No note.', json.dumps(report))

    def test_cancel_applies_only_to_the_displayed_review(self):
        headers = {'Origin': 'http://localhost', 'X-Yuzu-Revision': '0', 'Content-Type': 'application/octet-stream'}
        _, state, _ = self.request('desk', '/api/sample/a', b'two reviews, same specimen', **headers)
        op = {'Origin': 'http://localhost', 'X-Yuzu-Revision': str(state['revision'])}
        choice = {'reference': None, 'critique': True}
        _, first, _ = self.request('desk', '/api/review', choice, **op)
        _, job, _ = self.request('desk', '/api/critic/claim', {}, **op)
        self.assertEqual(self.request('desk', '/api/critic/finish', {'ticket': job['ticket'], 'note': None}, **op)[0], 200)
        _, second, _ = self.request('desk', '/api/review', choice, **op)
        previous = first['advisor']['critique']['end']
        current = second['advisor']['critique']
        self.assertGreater(current['end'], previous)
        for payload, expected in (({}, 400), ({'end': previous}, 409), ({'end': True}, 400)):
            with self.subTest(payload=payload):
                self.assertEqual(self.request('desk', '/api/critic/cancel', payload, **op)[0], expected)
                self.assertEqual(self.request('desk', '/api/state')[1]['advisor']['critique'], current)
        code, receipt, _ = self.request('desk', '/api/critic/cancel', {'end': current['end']}, **op)
        self.assertEqual(code, 200)
        self.assertEqual(receipt['advisor']['critique']['status'], 'cancelled')
