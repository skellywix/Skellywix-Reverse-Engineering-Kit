"""Private Linux operator socket and separate, read-only, per-run worker MCP launcher."""
import argparse
import fcntl
import json
import os
import re
import socketserver
import stat
import subprocess
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import builder, profile
from .broker import Broker
from .pipeline import Desk
from .sandbox import DOCKER, HERE, PYTHON
from .store import wire

RUN = re.compile(r'[0-9a-f]{32}')
HASH = re.compile(r'[0-9a-f]{64}')
ASSETS = {'/': ('index.html', 'text/html'), '/desk.css': ('desk.css', 'text/css'), '/app.js': ('app.js', 'text/javascript')}


class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Operator(BaseHTTPRequestHandler):
    server_version = 'Fourmilab/1'

    def log_message(self, fmt, *args):
        # No request/body logging; the durable event stream is the user-visible audit trail.
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def response(self, status, body, kind='application/json', download=False):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="fourmilab-base64-verified.zip"')
        self.end_headers()
        self.wfile.write(body)

    def data(self, status, body):
        self.response(status, wire(body).encode('ascii'))

    def host_ok(self):
        return self.headers.get_all('Host') == [self.server.authority]

    def local_read(self):
        """The private socket admits local clients. A browser must not read data for another origin."""
        return (self.headers.get_all('Origin') in (None, ['http://' + self.server.authority])
                and self.headers.get_all('Sec-Fetch-Site') in (None, ['none'], ['same-origin']))

    def do_GET(self):
        if not self.host_ok():
            return self.data(403, {'error': 'Unexpected operator host.'})
        desk = self.server.desk
        target = urlsplit(self.path)
        if (target.path == '/state' or target.path.startswith('/package/')) and not self.local_read():
            return self.data(403, {'error': 'Open this resource from the local desk.'})
        try:
            if not target.query and target.path in ASSETS:
                name, kind = ASSETS[target.path]
                return self.response(200, (HERE / name).read_bytes(), kind + '; charset=utf-8')
            if target.path == '/state':
                params = parse_qs(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
                if set(params) - {'run'} or ('run' in params and not RUN.fullmatch(params['run'][0])):
                    raise ValueError('Invalid run selector.')
                rows = desk.store.summaries()
                selected = params['run'][0] if 'run' in params else rows[0]['id'] if rows else None
                running = desk.current
                return self.data(200, {'runs': rows, 'run': desk.store.run(selected) if selected else None,
                                       'releases': desk.store.releases(), 'catalog': profile.CATALOG,
                                       'busy': desk.update_lock.locked(), 'running': running[0] if running else None,
                                       'update_error': desk.update_error, 'builder_connected': desk.broker.peer is not None})
            if not target.query and (match := re.fullmatch(r'/package/([0-9a-f]{32})', target.path)):
                return self.response(200, desk.package(match[1]).read_bytes(), 'application/zip', True)
            self.data(404, {'error': 'Unknown candidate resource.'})
        except ValueError as exc:
            self.data(400, {'error': str(exc)[:400]})

    def do_POST(self):
        if (not self.host_ok() or self.headers.get_all('Origin') != ['http://' + self.server.authority]
                or self.headers.get_all('X-Candidate-Action') != ['1']):
            return self.data(403, {'error': 'Local operator origin and action header required.'})
        if self.path != '/action':
            return self.data(404, {'error': 'Unknown action endpoint.'})
        if self.headers.get_content_type() != 'application/json' or 'Transfer-Encoding' in self.headers:
            return self.data(415, {'error': 'A bounded JSON body is required.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024 or len(self.headers.get_all('Content-Length', [])) != 1:
                raise ValueError('Invalid request size.')
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict) or not isinstance(req.get('action'), str):
                raise ValueError('Expected an operator action.')
            action = req['action']
            keys = {'promote': {'digest'}, 'retry': {'run'}, 'cancel': {'run'}, 'start': set(), 'stage': set(), 'rollback': set()}
            if action not in keys or set(req) != keys[action] | {'action'}:
                raise ValueError('Unexpected action or field.')
            if 'run' in req and (not isinstance(req['run'], str) or not RUN.fullmatch(req['run'])):
                raise ValueError('Invalid run ID.')
            if 'digest' in req and (not isinstance(req['digest'], str) or not HASH.fullmatch(req['digest'])):
                raise ValueError('Invalid release digest.')
            desk = self.server.desk
            result = {'ok': True}
            if action in ('stage', 'promote', 'rollback'):
                desk.update(action, req.get('digest'))
            elif action == 'cancel':
                desk.cancel(req['run'])
            else:
                result['run'] = desk.start(req.get('run'))
            self.data(200, result)
        except (ValueError, UnicodeError) as exc:
            self.data(400, {'error': str(exc)[:420]})


def serve(root, port):
    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged local forwarding port.')
    os.umask(0o077)
    with (root / 'candidate.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in ('desk.sock', 'builder.sock'):
            path = root / name
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                    raise ValueError('Candidate socket path is occupied.')
                path.unlink()
        desk = Desk(root, Broker(root, builder.MODEL))
        with UnixHTTP(str(root / 'desk.sock'), Operator) as server:
            server.desk = desk
            server.authority = f'127.0.0.1:{port}'
            print(f'Candidate ready on {root / "desk.sock"}; forward to {server.authority}', flush=True)
            server.serve_forever()


def mcp(root, run):
    if not RUN.fullmatch(run) or not (root / 'runs' / run / 'view' / 'run.json').is_file():
        raise ValueError('Select an existing candidate run.')
    cmd = [DOCKER, 'run', '--rm', '-i', '--pull=never', '--runtime=runsc', '--network=none', '--read-only',
           '--user=65534:65534', '--security-opt=no-new-privileges', '--cap-drop=ALL', '--memory=128m',
           '--cpus=0.5', '--pids-limit=64', '--ulimit=nproc=8:8', '--log-driver=none', '--mount', f'type=bind,src={root}/runs/{run}/view,dst=/view,readonly',
           '--mount', f'type=bind,src={HERE}/mcp.py,dst=/mcp.py,readonly', '--entrypoint=python3', PYTHON, '-IB', '/mcp.py']
    return subprocess.call(cmd)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('serve', 'mcp'))
    p.add_argument('directory', type=Path)
    p.add_argument('--port', type=int, default=48784)
    p.add_argument('--run', default='')
    args = p.parse_args()
    root = args.directory.absolute()
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or ',' in str(root):
        p.error('Use a private, operator-owned, mode-0700 candidate directory.')
    if args.mode == 'mcp':
        raise SystemExit(mcp(root, args.run))
    serve(root, args.port)
