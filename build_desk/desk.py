"""Serve the fixed Neverball build profile over a private Linux operator socket."""
import argparse
import fcntl
import hashlib
import os
import re
import shutil
import socketserver
import stat
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
from time import monotonic

from . import game_jobs

HERE = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html', 'text/html'), '/desk.css': ('desk.css', 'text/css'),
          '/game.js': ('game.js', 'text/javascript')}
REFERENCE_BYTES = 75_802_818


class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Operator(BaseHTTPRequestHandler):
    server_version = 'BuildDesk/1'

    def log_message(self, fmt, *args):
        # No request/body logging; the durable event stream is the user-visible audit trail.
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _headers(self, status, length, kind, *, filename=None, sha256=None):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        if sha256:
            self.send_header('ETag', f'"{sha256}"')
        self.end_headers()

    def response(self, status, body, kind='application/json'):
        self._headers(status, len(body), kind)
        self.wfile.write(body)

    def data(self, status, body):
        self.response(status, game_jobs.wire(body).encode('ascii'))

    def host_ok(self):
        return self.headers.get_all('Host') == [self.server.authority]

    def local_read(self):
        """The private socket admits local clients. A browser must not read data for another origin."""
        return (self.headers.get_all('Origin') in (None, ['http://' + self.server.authority])
                and self.headers.get_all('Sec-Fetch-Site') in (None, ['none'], ['same-origin']))

    def _game_read(self, target):
        jobs = self.server.games
        if not self.local_read():
            return self.data(403, {'error': 'Open this resource from the local desk.'})
        if not jobs:
            return self.data(503, {'error': 'This desk has no installed Windows game build profile.'})
        if target.query:
            return self.data(400, {'error': 'Invalid game selector.'})
        if match := re.fullmatch(r'/game/status(?:/([0-9a-f]{32}))?', target.path):
            try:
                run = jobs.status(match[1])
            except ValueError:
                return self.data(404, {'error': 'No such game build.'})
            except (game_jobs.GameJobError, OSError):
                return self.data(503, {'error': 'The game journal is unavailable.'})
            return self.data(200, {'job': run, 'prompt': game_jobs.PROMPT, 'reference_bytes': REFERENCE_BYTES})
        if match := re.fullmatch(r'/game/package/([0-9a-f]{32})', target.path):
            try:
                path, digest = jobs.package(match[1])
            except ValueError:
                return self.data(404, {'error': 'No such game build.'})
            except game_jobs.GameJobError:
                return self.data(409, {'error': 'There is no verified Windows download for this run.'})
            except OSError:
                return self.data(503, {'error': 'The stored package could not be read.'})
            with path.open('rb') as src:
                self._headers(200, os.fstat(src.fileno()).st_size, 'application/zip',
                              filename='neverball-windows.zip', sha256=digest)
                shutil.copyfileobj(src, self.wfile, 128 * 1024)
            return
        self.data(404, {'error': 'No such game resource.'})

    def _game_upload(self):
        # HTTP/1.0 closes after this request, including refusals before the body was read.
        if (self.headers.get_all('Content-Type') != ['application/zip'] or 'Transfer-Encoding' in self.headers
                or self.headers.get_all('Content-Encoding') not in (None, ['identity'])):
            return self.data(415, {'error': 'Choose the unmodified Neverball 1.6.0 Windows ZIP.'})
        jobs = self.server.games
        if not jobs:
            return self.data(503, {'error': 'This desk has no installed Windows game build profile.'})
        if self.headers.get_all('X-Game-Request') != [game_jobs.PROMPT]:
            return self.data(422, {'error': 'This profile supports only the displayed Windows rebuild request.'})
        if self.headers.get_all('Content-Length') != [str(REFERENCE_BYTES)]:
            return self.data(422, {'error': 'The file size does not match the original Windows release.'})
        digest = hashlib.sha256()
        remaining = REFERENCE_BYTES
        end = monotonic() + 120
        try:
            while remaining:
                seconds = end - monotonic()
                if seconds <= 0:
                    raise TimeoutError
                self.connection.settimeout(min(10, seconds))
                block = self.rfile.read1(min(remaining, 128 * 1024))
                if not block:
                    return self.data(400, {'error': 'The upload ended before the whole file arrived.'})
                digest.update(block)
                remaining -= len(block)
        except TimeoutError:
            return self.data(408, {'error': 'The upload timed out. No build was started.'})
        try:
            run = jobs.start(game_jobs.PROMPT, digest.hexdigest())
        except ValueError as exc:
            return self.data(422, {'error': str(exc)[:300]})
        except game_jobs.Busy:
            return self.data(409, {'error': 'A build is already running. Wait for it to finish.'})
        except (game_jobs.GameJobError, OSError):
            return self.data(503, {'error': 'The installed game build profile could not be verified.'})
        self.data(202, {'id': run})

    def do_GET(self):
        if not self.host_ok():
            return self.data(403, {'error': 'Unexpected operator host.'})
        target = urlsplit(self.path)
        if target.path.startswith('/game/'):
            return self._game_read(target)
        saved_game = target.path == '/' and re.fullmatch(r'game=[0-9a-f]{32}', target.query)
        if target.path in ASSETS and (not target.query or saved_game):
            name, kind = ASSETS[target.path]
            return self.response(200, (HERE / name).read_bytes(), kind + '; charset=utf-8')
        self.data(404, {'error': 'Unknown build resource.'})

    def do_POST(self):
        if (not self.host_ok() or self.headers.get_all('Origin') != ['http://' + self.server.authority]
                or self.headers.get_all('X-Candidate-Action') != ['1']):
            return self.data(403, {'error': 'Local operator origin and action header required.'})
        if self.path == '/game/build':
            if self.headers.get_all('Sec-Fetch-Site') not in (None, ['same-origin']):
                return self.data(403, {'error': 'Start the build from the local desk.'})
            return self._game_upload()
        self.data(404, {'error': 'Unknown action endpoint.'})


def serve(root, port, *, game_release=None):
    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged local forwarding port.')
    os.umask(0o077)
    with (root / 'candidate.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / 'desk.sock'
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise ValueError('Desk socket path is occupied.')
            path.unlink()
        with UnixHTTP(str(path), Operator) as server, ExitStack() as cleanup:
            server.games = None
            if game_release is not None:
                server.games = cleanup.enter_context(game_jobs.GameJobs(root / 'games', game_release))
            server.authority = f'127.0.0.1:{port}'
            print(f'Candidate ready on {root / "desk.sock"}; forward to {server.authority}', flush=True)
            server.serve_forever()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--port', type=int, default=48784)
    p.add_argument('--game-release', type=Path, help='Admin-installed, fixed Neverball source and dependency release.')
    args = p.parse_args()
    root = args.directory.absolute()
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or ',' in str(root):
        p.error('Use a private, operator-owned, mode-0700 candidate directory.')
    serve(root, args.port, game_release=args.game_release)
