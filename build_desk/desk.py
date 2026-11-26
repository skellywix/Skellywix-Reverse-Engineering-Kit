"""Serve software builds and their recorded results on a private operator socket."""
import argparse
import fcntl
import hashlib
import os
import re
import shutil
import socketserver
import stat
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from threading import BoundedSemaphore
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit
from time import monotonic

from . import game_jobs, neverball_archives
from .maintenance import MaintenanceError
from .workspace_http import REPORT_PATH, REVIEW_PATH, WorkspaceRoutes

HERE = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html', 'text/html'), '/desk.css': ('desk.css', 'text/css'),
          '/game.js': ('game.js', 'text/javascript'), '/projects.js': ('projects.js', 'text/javascript'),
          '/feedback.js': ('feedback.js', 'text/javascript')}
REFERENCE_BYTES = 75_802_818


class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Operator(WorkspaceRoutes, BaseHTTPRequestHandler):
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
                return self.data(409, {'error': 'This run has no intact Windows package available for download.'})
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
        except RunnerUnavailable as exc:
            return self.data(503, {'error': str(exc)})
        except (game_jobs.GameJobError, OSError):
            return self.data(503, {'error': 'The installed game build profile could not be verified.'})
        self.data(202, {'id': run})

    def do_GET(self):
        if not self.host_ok():
            return self.data(403, {'error': 'Unexpected operator host.'})
        target = urlsplit(self.path)
        if target.path.startswith('/game/'):
            return self._game_read(target)
        if target.path.startswith(('/project/', '/maintenance/')):
            return self.workspace_get(target)
        saved = target.path == '/' and re.fullmatch(r'(?:game|project)=[0-9a-f]{32}', target.query)
        if target.path in ASSETS and (not target.query or saved):
            name, kind = ASSETS[target.path]
            return self.response(200, (HERE / name).read_bytes(), kind + '; charset=utf-8')
        self.data(404, {'error': 'Unknown build resource.'})

    def do_POST(self):
        if (not self.host_ok() or self.headers.get_all('Origin') != ['http://' + self.server.authority]
                or self.headers.get_all('X-Candidate-Action') != ['1']
                or self.headers.get_all('Sec-Fetch-Site') not in (None, ['same-origin'])):
            return self.data(403, {'error': 'Local operator origin and action header required.'})
        if self.path == '/game/build':
            # The Windows recipe still requires its exact original release.
            return self._game_upload()
        if self.path in ('/project/build', '/maintenance/update') or REPORT_PATH.fullmatch(self.path) or REVIEW_PATH.fullmatch(self.path):
            return self.workspace_post()
        self.data(404, {'error': 'Unknown action endpoint.'})


RUNNER_KEY = 'neverball-runner'
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')


class RunnerUnavailable(game_jobs.GameJobError):
    """The Windows build profile cannot start a run: no usable updater-installed compiler, or no approved dependency archive.

    The message is safe to show.
    """


class Runner(NamedTuple):
    image: str
    recipe_sha256: str


def installed_runner(maintenance, recipe_sha256):
    """The updater-installed Neverball runner, or RunnerUnavailable.

    Image and recipe come from one validated read of the active dependency receipt, so they describe the same installed record.
    The image must be the immutable local ID (`sha256:` plus 64 lowercase hex) of a locally built (`oci-build`) component, and the
    receipt must say it was built from `recipe_sha256`, the recipe the release source carries. A tag, a registry digest, an ID the
    receipt does not hold, a duplicated record or a recipe mismatch is refused. Nothing here reads a request.
    """
    try:
        _, receipt = maintenance._read_active()  # one read; Maintenance has no public typed accessor for a receipt record
    except MaintenanceError:
        raise RunnerUnavailable('The Windows compiler image is not installed. Run Update all dependencies, then try again.') from None
    components = (receipt or {}).get('components')
    records = [r for r in (components.values() if isinstance(components, dict) else ())
              if isinstance(r, dict) and r.get('key') == RUNNER_KEY]
    if len(records) != 1:
        raise RunnerUnavailable('The Windows compiler image is not installed. Run Update all dependencies, then try again.')
    image, recipe = records[0].get('image_id'), records[0].get('recipe_sha256')
    if records[0].get('family') != 'oci-build' or not isinstance(image, str) or not IMAGE_ID.fullmatch(image):
        raise RunnerUnavailable('The Windows compiler is not a locally built immutable image. Run Update all dependencies, then try again.')
    if recipe != recipe_sha256:
        raise RunnerUnavailable('The installed Windows compiler was not built from this profile\'s pinned recipe. '
                                'Run Update all dependencies; if that does not fix it, update this checkout.')
    return Runner(image, recipe)


def release_archive(release, approved):
    """SHA-256 of the release's `dependencies.tgz`, only if it is exactly one of the `approved` digests."""
    path = Path(release) / 'dependencies.tgz'
    if path.is_symlink() or not path.is_file():
        raise RunnerUnavailable('The game release has no dependency archive. Prepare it again with tools/prepare_neverball.py.')
    digest = game_jobs.sha256_file(path)
    if digest not in approved:
        raise RunnerUnavailable('The game release holds a dependency archive that is not an approved one. '
                                'Prepare it again with tools/prepare_neverball.py.')
    return digest


class InstalledRunnerJobs(game_jobs.GameJobs):
    """GameJobs whose compiler is the updater-installed runner image and whose dependency archive is an approved exact digest.

    Both are chosen by this host, never by a request, and are resolved at `start` while no run is active:

    * compiler: the immutable image ID recorded for the runner. The journal, the ZIP provenance and the docker command all name
      it. An update that lands mid-run applies to the next run. The legacy built-in image ID is never used: until a run starts
      the compiler is empty and the compile step refuses it.
    * archive: the SHA-256 of the release's `dependencies.tgz`, accepted only if it is in `approved` (portable or historical).
      `verify_release` then compares the file to that one digest, and the journal and ZIP provenance record it.

    A run keeps its own archive binding: status and package check an existing candidate against the digest its journal recorded
    (when that is still approved), on the calling thread only, so a run built under one approved archive stays verifiable after
    the release is re-prepared with the other. `approved` and `pins` are injectable so fixtures can use their own digests.
    """

    def __init__(self, root, release, maintenance, *, pins=game_jobs.PINS, approved=neverball_archives.APPROVED):
        approved = tuple(approved)
        if not approved or not all(isinstance(d, str) and game_jobs.HASH.fullmatch(d) for d in approved):
            raise ValueError('Approved dependency archives must be lowercase SHA-256 digests.')
        self._maintenance = maintenance
        self._approved = approved
        self._starting = threading.Lock()
        self._local = threading.local()
        super().__init__(root, release, pins=replace(pins, image=''))

    @property
    def pins(self):
        return getattr(self._local, 'pins', None) or self._current

    @pins.setter
    def pins(self, value):
        self._current = value

    def start(self, prompt, reference_sha256):
        with self._starting:
            if self.busy():
                raise game_jobs.Busy('A game job is already queued or running.')
            runner = installed_runner(self._maintenance, self._current.recipe_sha256)
            archive = release_archive(self.release, self._approved)
            self._current = replace(self._current, image=runner.image, vendor_tgz_sha256=archive)
            return super().start(prompt, reference_sha256)

    @contextmanager
    def _bound(self, run):
        """For this thread only, check `run` against the archive digest its own journal recorded, if that is still approved."""
        archive = None
        if isinstance(run, str) and game_jobs.RUN.fullmatch(run):
            try:
                first = self._load(run)[0][0]
            except (game_jobs.GameJobError, ValueError):
                first = None
            recorded = first['data'].get('vendor_tgz_sha256') if first and first['state'] == 'queued' else None
            archive = recorded if recorded in self._approved else None
        previous = getattr(self._local, 'pins', None)
        if archive is not None:
            self._local.pins = replace(self._current, vendor_tgz_sha256=archive)
        try:
            yield
        finally:
            self._local.pins = previous

    def status(self, run=None):
        if run is None:
            with self._lock:
                ids = self._run_ids()
            run = ids[-1] if ids else None
        with self._bound(run):
            return super().status(run)

    def package(self, run):
        with self._bound(run):
            return super().package(run)


def open_games(root, release, maintenance):
    """The one way the desk opens the Windows game profile."""
    return InstalledRunnerJobs(root / 'games', release, maintenance)


def serve(root, port, *, game_release=None, socket_directory=None):
    from .maintenance import Maintenance
    from .project_jobs import ProjectJobs

    if not 1024 <= port <= 65535:
        raise ValueError('Use an unprivileged local forwarding port.')
    os.umask(0o077)
    with (root / 'candidate.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = (socket_directory or root) / 'desk.sock'
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise ValueError('Desk socket path is occupied.')
            path.unlink()
        with UnixHTTP(str(path), Operator) as server, ExitStack() as cleanup:
            server.games = None
            server.maintenance = Maintenance(root, HERE.parent)
            server.projects = ProjectJobs(root / 'projects', server.maintenance.image_for)
            cleanup.callback(server.projects.close)
            server.project_upload = BoundedSemaphore(1)
            server.paired = os.environ.get('RE_KIT_WORKSPACE') == '1'
            if game_release is not None:
                server.games = cleanup.enter_context(open_games(root, game_release, server.maintenance))
            server.authority = f'127.0.0.1:{port}'
            print(f'Build service ready on {path}; local authority {server.authority}', flush=True)
            server.serve_forever()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--port', type=int, default=48784)
    p.add_argument('--game-release', type=Path, help='Admin-installed, fixed Neverball source and dependency release.')
    p.add_argument('--socket-directory', type=Path, help='Separate private directory containing only the web-facing socket.')
    args = p.parse_args()
    root = args.directory.absolute()
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or ',' in str(root):
        p.error('Use a private, operator-owned, mode-0700 candidate directory.')
    if args.socket_directory is not None:
        info = args.socket_directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            p.error('Use a private, operator-owned, mode-0700 socket directory.')
    serve(root, args.port, game_release=args.game_release, socket_directory=args.socket_directory)
