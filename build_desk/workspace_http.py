"""Browser transport for product-independent builds and operator-owned dependency updates."""
import json
import re
import shutil
from time import monotonic
from urllib.parse import unquote_to_bytes


MAX_UPLOAD = 64 << 20
RUN_PATH = re.compile(r'/project/(status|package)/([a-f0-9]{32})')
PROJECT_PATH = re.compile(r'/project/library/([a-f0-9]{32})')
REPORT_PATH = re.compile(r'/project/reports/([a-f0-9]{32})')
REVIEW_PATH = re.compile(r'/project/reviews/([a-f0-9]{32})/([a-f0-9]{32})')
MAX_FEEDBACK = 16 << 10


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Use each feedback field only once.')
    return result


def _project_selector(headers):
    ids, names = headers.get_all('X-Project-Id'), headers.get_all('X-Project-Name')
    if ids is not None:
        if names is not None or len(ids) != 1 or not re.fullmatch(r'[0-9a-f]{32}', ids[0]):
            raise ValueError('Select one existing project, or name a new project.')
        return {'project_id': ids[0]}
    if names is None:
        return {}
    if (len(names) != 1 or not 1 <= len(names[0]) <= 960
            or not re.fullmatch(r"(?:[A-Za-z0-9_.!~*'()-]|%[0-9A-Fa-f]{2})+", names[0])):
        raise ValueError('The project name is not valid UTF-8 percent-encoded text.')
    try:
        return {'project_name': unquote_to_bytes(names[0]).decode('utf-8')}
    except UnicodeDecodeError:
        raise ValueError('The project name is not UTF-8.') from None


class WorkspaceRoutes:
    def workspace_get(self, target):
        if not self.local_read():
            return self.data(403, {'error': 'Open this resource from the local workspace.'})
        if target.query:
            return self.data(400, {'error': 'Use the run ID in the resource path.'})
        try:
            if target.path == '/maintenance/status':
                return self.data(200, self.server.maintenance.status())
            jobs = self.server.projects
            if target.path == '/project/library':
                return self.data(200, jobs.library())
            if project := PROJECT_PATH.fullmatch(target.path):
                return self.data(200, jobs.history(project[1]))
            if target.path == '/project/profiles':
                return self.data(200, {'profiles': jobs.profiles(), 'max_bytes': MAX_UPLOAD,
                                       'workspace': getattr(self.server, 'paired', False)})
            match = RUN_PATH.fullmatch(target.path)
            if not match:
                return self.data(404, {'error': 'No such project resource.'})
            if match[1] == 'status':
                return self.data(200, jobs.status(match[2]))
            name, size, digest, source = jobs.open_package(match[2])
        except ValueError:
            return self.data(404, {'error': 'No such project run.'})
        except RuntimeError:
            return self.data(409, {'error': 'This operation has no available result.'})
        except OSError:
            return self.data(503, {'error': 'The local workspace store is unavailable.'})
        with source:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', name) or not re.fullmatch('[a-f0-9]{64}', digest):
                return self.data(503, {'error': 'The stored artifact identity is invalid.'})
            self._headers(200, size, 'application/octet-stream', filename=name, sha256=digest)
            shutil.copyfileobj(source, self.wfile, 128 << 10)

    def _feedback(self, size, report, review):
        if self.headers.get_all('Content-Type') != ['application/json']:
            return self.data(415, {'error': 'Send feedback as a JSON object.'})
        if not 0 < size <= MAX_FEEDBACK:
            return self.data(413, {'error': 'Send a nonempty feedback record within 16 KiB.'})
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                return self.data(400, {'error': 'The feedback record ended early.'})
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_fields)
        except TimeoutError:
            return self.data(408, {'error': 'The feedback upload timed out.'})
        except (ValueError, RecursionError):
            return self.data(400, {'error': 'The feedback record is not a valid JSON object.'})
        fields = ('expected', 'observed') if report else ('outcome', 'note')
        if not isinstance(value, dict) or value.keys() != set(fields) or not all(isinstance(value[k], str) for k in fields):
            return self.data(422, {'error': 'Supply only these text fields: ' + ', '.join(fields) + '.'})
        jobs = self.server.projects
        try:
            result = (jobs.report(report[1], value['expected'], value['observed']) if report else
                      jobs.review(review[1], review[2], value['outcome'], value['note']))
        except ValueError as error:
            return self.data(422, {'error': str(error)[:300]})
        except RuntimeError as error:
            return self.data(409, {'error': str(error)[:300]})
        except OSError:
            return self.data(503, {'error': 'The local feedback store is unavailable.'})
        return self.data(201, result)

    def workspace_post(self):
        if 'Transfer-Encoding' in self.headers or self.headers.get_all('Content-Encoding') not in (None, ['identity']):
            return self.data(400, {'error': 'Encoded request bodies are not supported.'})
        lengths = self.headers.get_all('Content-Length')
        if not lengths or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            return self.data(411, {'error': 'A bounded content length is required.'})
        size = int(lengths[0])
        if self.path == '/maintenance/update':
            if size:
                return self.data(400, {'error': 'Update accepts no URLs, commands or options.'})
            try:
                report = self.server.maintenance.update()
            except RuntimeError:
                return self.data(409, {'error': 'Another update is active. Check update status.'})
            except (OSError, ValueError):
                return self.data(503, {'error': 'The dependency set could not be opened.'})
            return self.data(202 if report['state'] not in ('current', 'updated', 'failed') else 200, report)
        report, review = REPORT_PATH.fullmatch(self.path), REVIEW_PATH.fullmatch(self.path)
        if report or review:
            return self._feedback(size, report, review)
        if self.headers.get_all('Content-Type') != ['application/octet-stream']:
            return self.data(415, {'error': 'Choose a project source file or ZIP.'})
        profiles = self.headers.get_all('X-Project-Profile')
        if not profiles or len(profiles) != 1 or not re.fullmatch('[a-z][a-z0-9-]{0,62}', profiles[0]):
            return self.data(422, {'error': 'Choose one of the installed build profiles.'})
        if not 0 < size <= MAX_UPLOAD:
            return self.data(413, {'error': 'Choose a nonempty project no larger than 64 MiB.'})
        if not self.server.project_upload.acquire(blocking=False):
            return self.data(409, {'error': 'Another project upload is in progress.'})
        try:
            selector = _project_selector(self.headers)
            data = bytearray(size)
            view = memoryview(data)
            offset = 0
            deadline = monotonic() + 90
            while offset < size:
                left = deadline - monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(min(10, left))
                n = self.rfile.readinto1(view[offset:])
                if not n:
                    return self.data(400, {'error': 'The project upload ended early.'})
                offset += n
            job = self.server.projects.start(profiles[0], bytes(data), **selector)
        except TimeoutError:
            return self.data(408, {'error': 'The upload timed out.'})
        except ValueError as exc:
            return self.data(422, {'error': str(exc)[:300]})
        except RuntimeError as exc:
            return self.data(409, {'error': str(exc)[:300]})
        except OSError:
            return self.data(503, {'error': 'The project could not be stored.'})
        finally:
            self.server.project_upload.release()
        self.data(202, {'id': job})
