"""Two owner-private Unix sockets. The operator owns files; the agent has case tools only."""
import argparse
import json
import os
import re
import socketserver
import stat
import sys
from contextlib import ExitStack
from functools import partial
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock, Thread
from time import monotonic

# The launcher mounts this trusted, dependency-free application read-only, separately from its IPC.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from case_state import Case, CaseError
from evidence import MAX_SAMPLE, summarize
from protocol import rpc

MAX_JSON = 8192
HOST = re.compile(r'(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?\Z', re.ASCII)
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
TEAM_HOME = 'http://127.0.0.1:48784/'


def _object(pairs: list) -> dict:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError('duplicate key')
    return out


def static_assets(directory: Path) -> dict:
    assets = {}
    for path in (directory / 'index.html', directory / 'skelly.webp', directory / 'skellywix-mark.svg', *sorted((directory / 'assets').glob('*'))):
        rel = path.relative_to(directory).as_posix()
        if rel not in ('index.html', 'skelly.webp', 'skellywix-mark.svg') and not re.fullmatch(r'assets/[a-zA-Z0-9_.-]+\.(js|css)', rel):
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise RuntimeError('Missing or invalid built workbench asset.')
        mime = {'.js':'application/javascript','.css':'text/css','.html':'text/html','.webp':'image/webp','.svg':'image/svg+xml'}[path.suffix]
        assets['/' + rel] = (mime, path.read_bytes())
    assets['/'] = assets['/index.html']
    return assets


class Handler(BaseHTTPRequestHandler):
    server_version = 'Skellywix-RE-Kit'
    sys_version = ''

    def __init__(self, *args, case, lock, desk, assets, paired=False, **kwargs):
        self.paired = paired
        self.case, self.lock, self.desk, self.assets = case, lock, desk, assets
        super().__init__(*args, **kwargs)

    def setup(self):
        self.request.settimeout(8)
        super().setup()

    def log_message(self, *_):
        # Request URLs, headers, tool arguments, and files never enter access logs.
        pass

    def _respond(self, code, body=b'', mime='application/json', headers=None):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def _json(self, code, obj=None, headers=None):
        self._respond(code, b'' if obj is None else json.dumps(obj, separators=(',', ':')).encode(), headers=headers)

    def send_error(self, code, message=None, explain=None):
        self._json(code, {'error':'Unsupported request.'})

    def _single(self, field):
        vals = self.headers.get_all(field) or []
        if len(vals) > 1:
            raise CaseError(400, 'Duplicate request header.')
        return vals[0] if vals else None

    def _origin(self):
        host = self._single('Host') or ''
        if not HOST.fullmatch(host):
            raise CaseError(403, 'Use the local workbench address.')
        origin = self._single('Origin')
        if origin is not None and origin != 'http://' + host:
            raise CaseError(403, 'Cross-origin requests are not allowed.')
        if self.desk and (self.headers.get('Sec-Fetch-Site') == 'cross-site' or self.command == 'POST' and origin is None):
            raise CaseError(403, 'Use this workbench tab for operator actions.')

    def _body(self, mime, limit):
        if self._single('Transfer-Encoding') is not None or self._single('Content-Encoding') not in (None, 'identity'):
            raise CaseError(400, 'Unsupported body encoding.')
        if (self._single('Content-Type') or '').split(';')[0].strip().lower() != mime:
            raise CaseError(415, 'Unsupported content type.')
        rawlen = self._single('Content-Length')
        if not rawlen or not re.fullmatch('[0-9]{1,10}', rawlen):
            raise CaseError(411, 'A bounded content length is required.')
        size = int(rawlen)
        if size > limit:
            raise CaseError(413, 'Input exceeds the size limit.')
        end = monotonic() + 25
        data = bytearray(size)
        view = memoryview(data)
        done = 0
        while done < size:
            remaining = end - monotonic()
            if remaining <= 0:
                raise CaseError(408, 'Input timed out.')
            self.connection.settimeout(min(8, remaining))
            n = self.rfile.readinto1(view[done:])
            if not n:
                raise CaseError(400, 'Incomplete input.')
            done += n
        return data

    def _read_json(self):
        try:
            obj = json.loads(self._body('application/json', MAX_JSON).decode('utf-8'), object_pairs_hook=_object)
        except (UnicodeError, ValueError, RecursionError) as e:
            if isinstance(e, CaseError):
                raise
            raise CaseError(400, 'Invalid JSON request.') from None
        if not isinstance(obj, dict):
            raise CaseError(400, 'Expected a JSON object.')
        return obj

    def _operator(self):
        if self.command in ('GET','HEAD'):
            if self.paired and self.path == '/':
                return self._respond(302, mime='text/plain', headers={'Location': TEAM_HOME})
            if self.path in self.assets:
                mime, body = self.assets[self.path]
                return self._respond(200, body, mime)
            with self.lock:
                if self.path == '/api/state':
                    result, headers = self.case.view(), {}
                elif self.path == '/api/critic':
                    result, headers = self.case.critic_status(), {}
                elif self.path == '/api/report' and self.case.slots:
                    result, headers = self.case.report(), {'Content-Disposition':'attachment; filename="skellywix-re-kit-evidence.json"'}
                else:
                    raise CaseError(404, 'No such resource.')
            return self._json(200, result, headers)
        if self.command != 'POST':
            raise CaseError(405, 'Method not allowed.')
        if self.path not in ('/api/sample/a','/api/sample/b','/api/reset','/api/pair','/api/disconnect','/api/run',
                             '/api/review', '/api/critic/claim', '/api/critic/finish', '/api/critic/cancel'):
            raise CaseError(404, 'No such operation.')
        revision = self._single('X-Yuzu-Revision') or ''
        if not re.fullmatch('[0-9]{1,16}', revision):
            raise CaseError(400, 'A case revision is required.')
        rev = int(revision)
        if self.path.startswith('/api/sample/'):
            # Discard submitted bytes immediately after extracting the closed metadata projection.
            meta = summarize(self._body('application/octet-stream', MAX_SAMPLE))
            with self.lock:
                self.case.admit(rev, self.path[-1], meta)
                result = self.case.view()
        else:
            payload = self._read_json()
            with self.lock:
                if self.path == '/api/run':
                    if set(payload) != {'tool','arguments'} or not isinstance(payload['tool'], str) or not isinstance(payload['arguments'], dict):
                        raise CaseError(400, 'Choose a workflow and its inputs.')
                    evidence = self.case.operator_call(rev,payload['tool'],payload['arguments'])
                    result = {'result':evidence,'state':self.case.view()}
                elif self.path == '/api/review':
                    if set(payload) != {'reference', 'critique'}:
                        raise CaseError(400, 'Provide the optional reference and critique choice.')
                    self.case.start_review(rev, payload['reference'], payload['critique'])
                    result = self.case.view()
                elif self.path == '/api/critic/finish':
                    if set(payload) != {'ticket', 'note'}:
                        raise CaseError(400, 'Invalid critique receipt.')
                    self.case.finish_critique(rev, payload['ticket'], payload['note'])
                    result = self.case.critic_status()
                elif self.path == '/api/critic/cancel':
                    if set(payload) != {'end'}:
                        raise CaseError(400, 'Choose the review snapshot to cancel.')
                    self.case.cancel_critique(rev, payload['end'])
                    result = self.case.view()
                else:
                    if payload:
                        raise CaseError(400, 'This operation takes no arguments.')
                    if self.path == '/api/pair':
                        token = self.case.pair(rev)
                        result = {'token':token,'state':self.case.view()}
                    elif self.path == '/api/critic/claim':
                        result = self.case.claim_critique(rev)
                    else:
                        if self.path == '/api/reset':
                            self.case.reset(rev)
                        else:
                            self.case.disconnect(rev)
                        result = self.case.view()
        self._json(200, result)

    def _agent(self):
        if self.path != '/mcp':
            raise CaseError(404, 'No such resource.')
        auth = self._single('Authorization') or ''
        token = auth.removeprefix('Bearer ') if auth.startswith('Bearer ') else ''
        sid = self._single('Mcp-Session-Id')
        with self.lock:
            self.case.authorize(token)
            if self.command == 'DELETE':
                self.case.session(token, sid, active=False)
                self.case.disconnect(self.case.revision)
                response = (200, None, {})
            elif self.command != 'POST':
                raise CaseError(405, 'Use a JSON-RPC POST.')
            else:
                response = None
        if response is None:
            packet = self._read_json()
            with self.lock:
                response = rpc(self.case, token, sid, self._single('MCP-Protocol-Version'), packet)
        self._json(*response)

    def _dispatch(self):
        try:
            self._origin()
            self._operator() if self.desk else self._agent()
        except CaseError as exc:
            self._json(exc.status, {'error':str(exc)})
        except ValueError as exc:
            # Evidence functions raise only fixed, artifact-independent descriptions.
            self._json(400, {'error':str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    do_GET = do_HEAD = do_DELETE = do_POST = _dispatch


def serve(ipc: Path, web: Path, *, paired: bool = False, case: Case | None = None):
    info = ipc.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit('IPC must be a private directory (0700) owned by the current user.')
    os.umask(0o077)
    case = Case() if case is None else case
    lock, assets = Lock(), static_assets(web)
    if paired:
        mime, page = assets['/index.html']
        if page.count(b'</head>') != 1:
            raise RuntimeError('The file inspection page needs one document head.')
        marker = b'<meta name="skellywix-workspace" content="paired">'
        assets['/index.html'] = (mime, page.replace(b'</head>', marker + b'</head>'))
    with ExitStack() as cleanup:
        servers = []
        for name in ('desk','agent'):
            address = ipc / (name + '.sock')
            desk = name == 'desk'
            handler = partial(Handler, case=case, lock=lock, desk=desk,
                              assets=assets if desk else {}, paired=paired and desk)
            server = socketserver.UnixStreamServer(str(address), handler)
            cleanup.callback(address.unlink, missing_ok=True)
            cleanup.enter_context(server)
            servers.append(server)
        Thread(target=servers[1].serve_forever, daemon=True).start()
        cleanup.callback(servers[1].shutdown)
        servers[0].serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ipc', required=True, type=Path)
    parser.add_argument('--web', required=True, type=Path)
    parser.add_argument('--paired', action='store_true', help='Use the fixed loopback team workflow as the workspace home.')
    parser.add_argument('--restore-case', type=Path, help='Start from an offline private operator state export.')
    opts = parser.parse_args()
    initial = None
    if opts.restore_case is not None:
        from case_recovery import CaseRestoreError, load_case
        try:
            initial = load_case(opts.restore_case)
        except CaseRestoreError as exc:
            parser.exit(1, f'{exc}\n')
    serve(opts.ipc, opts.web, paired=opts.paired, case=initial)
