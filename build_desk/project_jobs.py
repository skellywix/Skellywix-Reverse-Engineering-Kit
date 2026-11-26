"""Generic, serialized build and packaging of an uploaded software project. No title, hash or sample list admits a build.

    jobs = ProjectJobs(root, image_for)          # resolves an installed compiler, native-compiler or python image ID
    jobs.profiles()  -> [{id, label, description, available, reason}]
    jobs.start(profile, data, *, project_id=None, project_name=None) -> 32-hex run id
                                                 # ValueError: bad profile/upload/project; RuntimeError: unavailable or busy
    jobs.status(id)  -> {id, profile, state, message, events:[{message}], artifact, sha256, input_sha256,
                         project_id, project_name}
    jobs.package(id) -> (Path, sha256)           # only this run's own successful output; RuntimeError otherwise
    jobs.open_package(id) -> (name, size, sha256, stream)  # owned, verified snapshot; the caller closes it
    jobs.library()   -> {projects:[{id, name}]}  # newest first
    jobs.history(project_id) -> {id, name, runs:[{id, profile, state, input_sha256, sha256}], issues:[issue]}
    jobs.report(run_id, expected, observed) -> issue
    jobs.review(candidate_run_id, issue_id, outcome, note) -> issue   # outcome: 'fixed' | 'still_open'

A project is the root run (its id is the project id) and every run started with that id; with no id a run is a new
project. Grouping is only ever that explicit id: never a label, profile or digest. Older records have no grouping
fields and read as one-run projects; they are never rewritten. The root run's record is the one place a project's name
lives. Issues and reviews are append-only files of `project_feedback`; running another build never resolves one.
Only an explicit review of a later, successful, distinct build of the same project marks an issue fixed.

Profiles (what the upload may be is decided by its files, never by its name):
    rust         one .rs file, or a Cargo project (workspace, Cargo.lock, vendored crates, offline)
    c-cpp        CMake, Makefile or `configure` projects, or loose C/C++ sources
    python-app   Python project, packaged as data; parsed in the sandbox when a pinned Python image exists
    static-site  a directory holding index.html (GitHub archive prefix, `dist/`, `build/`, ... are recognised)

Compiling runs only in the fresh networkless container of `project_container` with no host mount; the host never
executes uploaded or produced files. Build scripts, procedural macros, `make` rules and `./configure` of the project
run inside that sandbox: that is what compiling means. Output is a deterministic ZIP whose PACKAGE-INFO.json names the
run, the input hash and what was and was not done. Source compilation or packaging is never evidence that a program
behaves like any other program.

State names: queued, building, built, failed. Records live in root/runs/<id>/status.json (atomic writes); a run that was
queued or building when the process stopped becomes `failed` on the next start. Only one run is active at a time.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from . import project_archive as pa
from . import project_feedback as fb
from . import project_pack as pk
from . import project_plan as pl
from . import project_scripts as ps
from .project_container import DOCKER_ENVIRONMENT_ERRORS, IMAGE_ID, DockerRunner, Spec

log = logging.getLogger(__name__)

RUN_ID = re.compile(r'[0-9a-f]{32}')
HASH = re.compile(r'[0-9a-f]{64}')
ARTIFACT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.zip')
MARKER = 'PROJECT-BUILD-ERROR: '
CHECK_MARKER = re.compile(r'PROJECT-CHECK: python=(\d+\.\d+\.\d+) files=(\d+)')
STATES = ('queued', 'building', 'built', 'failed')
ACTIVE = ('queued', 'building')
MAX_EVENTS = 40
MAX_MESSAGE = 1500
MAX_RECORD = 1 << 20
LOG_NAME = 'build.log'
DEFAULT_LIMITS = pa.Limits(max_input=128 << 20, max_members=50_000, max_member=128 << 20, max_expanded=384 << 20)
SUFFIX = {pl.RUST: 'build', pl.NATIVE: 'build', pl.PYTHON: 'app', pl.SITE: 'site'}
SANDBOX_PROFILES = {pl.RUST: 'compiler', pl.NATIVE: 'native-compiler'}
SANDBOX_NOTE = 'The produced files were not run, and no claim is made that they behave like any other program.'

PROFILE_ROWS = {
    pl.RUST: ('Rust project',
              'Compiles one .rs file or a Cargo project (workspace, Cargo.lock, vendored crates) offline in a '
              'networkless sandbox. The Linux binaries are packaged and never run.'),
    pl.NATIVE: ('C / C++ project',
                'Builds a CMake, Makefile or configure project, or loose C/C++ sources, offline in a networkless '
                'sandbox. The Linux binaries are packaged and never run.'),
    pl.PYTHON: ('Python application',
                'Packages a Python project as data. With a pinned Python image the sources are also parsed in the '
                'sandbox; nothing is installed or run.'),
    pl.SITE: ('Static website',
              'Packages a static site (the folder holding index.html) as data. Nothing is built or run.'),
}


class _Failed(Exception):
    """A build ended in a way the user is told about in plain words."""


def clip(text, limit=MAX_MESSAGE):
    text = ''.join(c if c == '\n' or 32 <= ord(c) < 127 else '?' for c in str(text))
    return text if len(text) <= limit else text[:limit - 3] + '...'


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _grouping_ok(record):
    """Grouping fields are optional (older records have none); when present they must agree with each other."""
    if 'project_id' not in record:
        return 'project_name' not in record and 'created_ns' not in record
    project, created = record['project_id'], record.get('created_ns')
    if not (isinstance(project, str) and RUN_ID.fullmatch(project)):
        return False
    if not isinstance(created, int) or isinstance(created, bool) or created < 1:
        return False
    if project != record['id']:
        return 'project_name' not in record
    name = record.get('project_name')
    try:
        return isinstance(name, str) and fb.text(name, 'name', name=True) == name
    except ValueError:
        return False


class ProjectJobs:
    def __init__(self, root, image_for, *, runner=None, limits=None, build_seconds=900):
        self.root = Path(root)
        self.runs = self.root / 'runs'
        self.runs.mkdir(parents=True, exist_ok=True)
        self.limits = limits or DEFAULT_LIMITS
        self.build_seconds = build_seconds
        self._image_for = image_for
        self._runner = runner or DockerRunner()
        self._lock = threading.RLock()
        self._issues = fb.IssueStore(self.runs, self._lock)
        self._stop = threading.Event()
        self._thread = None
        self._active = None
        self._recover()

    # ------------------------------------------------------------------ public

    def profiles(self):
        compilers = {pid: self._sandbox_ready(key) for pid, key in SANDBOX_PROFILES.items()}
        python_ok, python_reason = self._sandbox_ready('python')
        rows = []
        for pid, (label, description) in PROFILE_ROWS.items():
            if pid in SANDBOX_PROFILES:
                ok, reason = compilers[pid]
            elif pid == pl.PYTHON:
                ok, reason = True, '' if python_ok else f'Packaged without the sandbox syntax check: {python_reason}'
            else:
                ok, reason = True, ''
            rows.append({'id': pid, 'label': label, 'description': description, 'available': ok,
                         'reason': reason})
        return rows

    def start(self, profile, data, *, project_id=None, project_name=None):
        if not isinstance(profile, str) or profile not in PROFILE_ROWS:
            raise ValueError('Unknown build profile.')
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError('The upload is empty.')
        if project_id is not None:
            if project_name is not None:
                raise ValueError('A name can only be given to a new project.')
            if not isinstance(project_id, str) or not RUN_ID.fullmatch(project_id):
                raise ValueError('Unknown project.')
        chosen = None if project_name is None else fb.text(project_name, 'project name', name=True)
        tree = self._open(profile, data)
        plan = pl.PLANNERS[profile](tree)
        if profile in SANDBOX_PROFILES:
            ok, reason = self._sandbox_ready(SANDBOX_PROFILES[profile])
            if not ok:
                raise RuntimeError(reason)
        input_sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            latest = 0
            if project_id is not None:
                self._root(project_id)
                members, _ = self._group(project_id)
                latest = max(self._created(r) for r in members)
            if self._active:
                raise RuntimeError('Another build is running. Wait for it to finish.')
            run = secrets.token_hex(16)
            (self.runs / run).mkdir()
            record = {'format': 1, 'id': run, 'profile': profile, 'state': 'queued',
                      'message': 'Queued.', 'events': ['Queued.'], 'artifact': None, 'sha256': None,
                      'input_sha256': input_sha, 'bytes': 0, 'project_id': project_id or run,
                      'created_ns': max(time.time_ns(), latest + 1)}
            if project_id is None:
                record['project_name'] = chosen or (plan.name or 'project')[:fb.MAX_NAME]
            self._write(run, record)
            self._active = run
            self._thread = threading.Thread(target=self._work, args=(run, profile, plan, tree, input_sha), daemon=True)
            self._thread.start()
        return run

    def status(self, run_id):
        return self._view(self._load(run_id))

    def _artifact_path(self, record):
        if record['state'] in ACTIVE:
            raise RuntimeError('This build has not finished yet.')
        if record['state'] != 'built':
            raise RuntimeError('This build failed; it has no output to download.')
        path = self.runs / record['id'] / record['artifact']
        try:
            info = os.lstat(path)
        except OSError as error:
            raise RuntimeError('The stored output for this run is no longer available.') from error
        if not stat.S_ISREG(info.st_mode) or info.st_size != record['bytes'] or info.st_size > pk.MAX_ARTIFACT:
            raise RuntimeError('The stored output for this run is no longer the file that was built.')
        return path

    def package(self, run_id):
        record = self._load(run_id)
        path = self._artifact_path(record)
        digest = sha256_file(path)
        if digest != record['sha256']:
            raise RuntimeError('The stored output for this run changed after it was built.')
        return path, digest

    def open_package(self, run_id):
        record = self._load(run_id)
        return record['artifact'], record['bytes'], record['sha256'], self._snapshot(record)

    def library(self):
        records, incomplete = self._records()
        roots = [r for r in records if self._project_of(r) == r['id']]
        roots.sort(key=lambda r: (self._created(r), r['id']), reverse=True)
        result = {'projects': [{'id': r['id'], 'name': self._name_of(r)} for r in roots]}
        if incomplete:
            result['incomplete'] = True
        return result

    def history(self, project_id):
        root = self._root(project_id)
        members, incomplete = self._group(project_id)
        members.sort(key=lambda r: (self._created(r), r['id']), reverse=True)
        runs = []
        for record in members:
            state, _, digest, _ = self._outcome(record)
            runs.append({'id': record['id'], 'profile': record['profile'], 'state': state,
                         'input_sha256': record['input_sha256'], 'sha256': digest})
        issues = [issue for record in members for issue in self._issues.for_run(record['id'])]
        issues.sort(key=lambda i: (i['at'], i['id']), reverse=True)
        result = {'id': project_id, 'name': self._name_of(root), 'runs': runs, 'issues': issues}
        if incomplete:
            result['incomplete'] = True
        return result

    def report(self, run_id, expected, observed):
        """Record that a finished run did not do what was expected. Original identity is derived here, never given."""
        expected = fb.text(expected, 'expected result')
        observed = fb.text(observed, 'observed result')
        record = self._load(run_id)
        digest, payload = self._finished_identity(record)
        with self._lock:
            self._name_of(record)  # the project's root must still be readable
            members, _ = self._group(self._project_of(record))
            if sum(len(self._issues.for_run(r['id'])) for r in members) >= fb.MAX_ISSUES:
                raise RuntimeError('This project has reached its limit of recorded issues.')
            return self._issues.create(run_id, secrets.token_hex(16), record['input_sha256'], digest, payload,
                                       expected, observed)

    def review(self, candidate_run_id, issue_id, outcome, note):
        """Record what a later run showed about an issue. Only an explicit 'fixed' review closes it, and only for a
        later, successful build of the same project whose verified output differs from every output already judged."""
        if not isinstance(outcome, str) or outcome not in fb.OUTCOMES:
            raise ValueError("The outcome must be 'fixed' or 'still_open'.")
        note = fb.text(note, 'note')
        if not isinstance(issue_id, str) or not RUN_ID.fullmatch(issue_id):
            raise ValueError('Unknown issue.')
        candidate = self._load(candidate_run_id)
        owner = self._issues.locate(issue_id)
        if owner is None:
            raise ValueError('Unknown issue.')
        original = self._load(owner)
        if self._project_of(candidate) != self._project_of(original):
            raise RuntimeError('That build belongs to a different project than the issue.')
        digest, payload = self._finished_identity(candidate)
        if self._created(candidate) <= self._created(original):
            raise RuntimeError('Only a build started after the reported one can review the issue.')
        return self._issues.append_review(owner, issue_id, candidate_run_id, candidate['input_sha256'],
                                         digest, payload, outcome, note)

    def wait(self, timeout=None):
        """Block until no build is active (for tests and shutdown). True when idle."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self._active

    def close(self):
        self._stop.set()
        self.wait(60)

    # ------------------------------------------------------------------ availability

    def _resolve(self, key):
        try:
            image = self._image_for(key)
        except (RuntimeError, ValueError, LookupError, OSError) as error:
            return None, clip(f'No pinned {key} image is available ({error}).', 300)
        if not isinstance(image, str) or not IMAGE_ID.fullmatch(image):
            return None, f'The {key} image is not pinned to an image ID.'
        return image, ''

    def _sandbox_ready(self, key):
        image, reason = self._resolve(key)
        if not image:
            return False, reason
        try:
            self._runner.preflight(image)
        except RuntimeError as error:
            return False, clip(error, 300)
        return True, ''

    # ------------------------------------------------------------------ upload

    def _open(self, profile, data):
        single = None
        if pa.detect(data) == 'file':
            if profile == pl.RUST:
                single = 'main.rs'
            elif profile == pl.PYTHON:
                single = 'main.py'
            elif profile == pl.NATIVE:
                single = 'main.cpp' if pl.looks_cpp(pa.text_source(data, self.limits)) else 'main.c'
        return pa.open_upload(data, self.limits, single)

    # ------------------------------------------------------------------ records

    def _dir(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError('Unknown run.')
        return self.runs / run_id

    def _load(self, run_id):
        path = self._dir(run_id) / 'status.json'
        with self._lock:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                raise ValueError('Unknown run.') from None
            except OSError as error:
                raise RuntimeError('The record for this run is unreadable.') from error
            if size > MAX_RECORD:
                raise RuntimeError('The record for this run is unreadable.')
            try:
                record = json.loads(path.read_text('utf-8'))
            except (OSError, ValueError) as error:
                raise RuntimeError('The record for this run is unreadable.') from error
        if not self._valid(record, run_id):
            raise RuntimeError('The record for this run is unreadable.')
        return record

    @staticmethod
    def _valid(record, run_id):
        if not isinstance(record, dict) or record.get('id') != run_id or record.get('profile') not in PROFILE_ROWS:
            return False
        if record.get('state') not in STATES or not isinstance(record.get('message'), str):
            return False
        events = record.get('events')
        if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
            return False
        artifact, digest, size = record.get('artifact'), record.get('sha256'), record.get('bytes')
        if artifact is not None and not (isinstance(artifact, str) and ARTIFACT_NAME.fullmatch(artifact)):
            return False
        if digest is not None and not (isinstance(digest, str) and HASH.fullmatch(digest)):
            return False
        if not (isinstance(record.get('input_sha256'), str) and HASH.fullmatch(record['input_sha256'])):
            return False
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
        if not _grouping_ok(record):
            return False
        return record['state'] != 'built' or (artifact is not None and digest is not None)

    def _write(self, run_id, record):
        directory = self.runs / run_id
        temporary = directory / 'status.json.tmp'
        with self._lock:
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=True, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / 'status.json')

    def _update(self, run_id, state=None, message=None, event=None, **fields):
        with self._lock:
            record = self._load(run_id)
            if state is not None:
                record['state'] = state
            if message is not None:
                record['message'] = clip(message)
            if event is not None:
                record['events'] = (record['events'] + [clip(event)])[-MAX_EVENTS:]
            record.update(fields)
            self._write(run_id, record)

    def _outcome(self, record):
        """(state, artifact, sha256, message) as shown: a built run whose file is gone reads as failed."""
        artifact, digest, state, message = record['artifact'], record['sha256'], record['state'], record['message']
        if state == 'built':
            path = self.runs / record['id'] / artifact
            try:
                info = os.lstat(path)
                present = stat.S_ISREG(info.st_mode) and info.st_size == record['bytes']
            except OSError:
                present = False
            if not present:
                state, artifact, digest = 'failed', None, None
                message = 'The stored output for this run is no longer available.'
        else:
            artifact = digest = None
        return state, artifact, digest, message

    def _view(self, record):
        state, artifact, digest, message = self._outcome(record)
        return {'id': record['id'], 'profile': record['profile'], 'state': state, 'message': message,
                'events': [{'message': e} for e in record['events']], 'artifact': artifact, 'sha256': digest,
                'input_sha256': record['input_sha256'], 'project_id': self._project_of(record),
                'project_name': self._name_of(record)}

    # ------------------------------------------------------------------ projects

    @staticmethod
    def _project_of(record):
        return record.get('project_id', record['id'])

    @staticmethod
    def _created(record):
        return record.get('created_ns', 0)

    def _root(self, project_id):
        """The root record of a project. ValueError: no such project; RuntimeError: its record is unreadable."""
        if not isinstance(project_id, str) or not RUN_ID.fullmatch(project_id):
            raise ValueError('Unknown project.')
        try:
            root = self._load(project_id)
        except ValueError:
            raise ValueError('Unknown project.') from None
        if self._project_of(root) != project_id:
            raise ValueError('Unknown project.')
        return root

    def _name_of(self, record):
        """The project's name: held by its root record, or derived for a root with no grouping fields."""
        if 'project_name' in record:
            return record['project_name']
        project = self._project_of(record)
        if project == record['id']:
            return f'{PROFILE_ROWS[record["profile"]][0]} {project[:8]}'
        try:
            return self._name_of(self._root(project))
        except ValueError as error:
            raise RuntimeError('The project record for this run is unreadable.') from error

    def _records(self):
        """Return readable records and whether any run could not be assigned to a project."""
        records, incomplete = [], False
        for entry in sorted(self.runs.iterdir()):
            if not RUN_ID.fullmatch(entry.name) or not entry.is_dir():
                continue
            try:
                records.append(self._load(entry.name))
            except (ValueError, RuntimeError):
                log.warning('skipping unreadable project run record %s', entry.name)
                incomplete = True
        return records, incomplete

    def _group(self, project_id):
        records, incomplete = self._records()
        return [r for r in records if self._project_of(r) == project_id], incomplete

    def _finished_identity(self, record):
        """Bind feedback to the verified ZIP and its software, not to a new run ID in the ZIP manifest."""
        if record['state'] in ACTIVE:
            raise RuntimeError('This build has not finished yet.')
        if record['state'] == 'failed':
            return None, None
        with self._snapshot(record) as snapshot:
            return record['sha256'], pk.payload_sha256(snapshot)

    def _snapshot(self, record):
        """Copy and verify one bounded archive. The caller owns the rewound stream."""
        path = self._artifact_path(record)
        size, digest = 0, hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        try:
            with ExitStack() as cleanup:
                snapshot = cleanup.enter_context(tempfile.SpooledTemporaryFile(max_size=1 << 20))
                with os.fdopen(os.open(path, flags), 'rb') as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size != record['bytes']:
                        raise RuntimeError('The stored output for this run is no longer the file that was built.')
                    while block := source.read(1 << 20):
                        size += len(block)
                        if size > record['bytes']:
                            break
                        digest.update(block)
                        snapshot.write(block)
                if size != record['bytes'] or digest.hexdigest() != record['sha256']:
                    raise RuntimeError('The stored output for this run changed after it was built.')
                snapshot.seek(0)
                cleanup.pop_all()
                return snapshot
        except OSError as error:
            raise RuntimeError('The delivered files could not be verified.') from error

    def _recover(self):
        """A restart abandons any queued or building run: mark it failed and try to stop its container."""
        for entry in sorted(self.runs.iterdir()):
            if not RUN_ID.fullmatch(entry.name) or not entry.is_dir():
                continue
            for leftover in ('status.json.tmp', 'scratch.zip'):
                (entry / leftover).unlink(missing_ok=True)
            try:
                record = self._load(entry.name)
            except (ValueError, RuntimeError):
                continue  # an unreadable record is reported by status(); it is never rewritten here
            if record['state'] not in ACTIVE:
                continue
            note = 'The build was interrupted when the desk stopped. Start a new build.'
            discard = getattr(self._runner, 'discard', None)
            if discard is not None and record['state'] == 'building':
                try:
                    discard('projbuild-' + entry.name)
                except RuntimeError as error:
                    note += ' ' + clip(error, 300)
            self._update(entry.name, state='failed', message=note, event=note, artifact=None, sha256=None, bytes=0)

    # ------------------------------------------------------------------ worker

    def _work(self, run, profile, plan, tree, input_sha):
        scratch = self.runs / run / 'scratch.zip'
        try:
            self._update(run, state='building', message='Building.', event='Building.')
            for note in plan.notes:
                self._update(run, event=note)
            if profile in SANDBOX_PROFILES:
                outputs, base = self._compile(run, plan, tree)
            elif profile == pl.PYTHON:
                outputs, base = self._python(run, plan, tree)
            else:
                outputs, base = self._site(plan, tree)
            source_tree, members = outputs
            info = {'format': 1, 'run': run, 'profile': profile, 'mode': plan.mode, 'name': plan.name,
                    'input_sha256': input_sha, 'notes': list(plan.notes), 'details': dict(plan.info), **base}
            self._update(run, event='Packaging the output.')
            pk.write_zip(scratch, source_tree, members, info)
            size = scratch.stat().st_size
            if size > pk.MAX_ARTIFACT:
                raise _Failed('The packaged output is larger than the output limit.')
            digest = sha256_file(scratch)
            name = f'{plan.name}-{SUFFIX[profile]}.zip'
            os.replace(scratch, self.runs / run / name)
            tail = SANDBOX_NOTE if profile in SANDBOX_PROFILES else 'Packaged as data; nothing was built or run.'
            self._update(run, state='built', message=f'Built {name} ({size} bytes). {tail}', event=f'Built {name}.',
                         artifact=name, sha256=digest, bytes=size)
        except _Failed as error:
            self._fail(run, str(error))
        except ValueError as error:
            self._fail(run, str(error))
        except RuntimeError as error:
            self._fail(run, str(error))
        except Exception:
            log.exception('project build %s stopped unexpectedly', run)
            self._fail(run, 'The build stopped unexpectedly. Start a new build.')
        finally:
            scratch.unlink(missing_ok=True)
            with self._lock:
                self._active = None

    def _fail(self, run, message):
        try:
            self._update(run, state='failed', message=message, event=message, artifact=None, sha256=None, bytes=0)
        except (OSError, ValueError, RuntimeError):
            log.exception('could not record the failure of project build %s', run)

    def _site(self, plan, tree):
        members = [(src, 'site/' + dest, 0o644) for src, dest in plan.files]
        return (tree, members), {'builder': None}

    def _python(self, run, plan, tree):
        members = [(src, 'app/' + dest, 0o755 if tree.get(src).executable else 0o644) for src, dest in plan.files]
        image, reason = self._resolve('python')
        if image:
            try:
                self._runner.preflight(image)
            except RuntimeError as error:
                image, reason = None, clip(error, 300)
        if not image:
            self._update(run, event=f'Syntax check skipped: {reason}')
            return (tree, members), {'builder': None, 'syntax_check': 'not run'}
        self._update(run, event=f'Parsing the Python files in the sandbox ({image[:19]}).')
        result = self._sandbox(run, image, plan, tree, 'python', 4096)
        text = result.stderr.decode('utf-8', 'replace')
        found = CHECK_MARKER.search(text)
        check = {'python': found.group(1), 'files': int(found.group(2))} if found else 'not reported'
        return (tree, members), {'builder': {'image': image, 'runtime': getattr(self._runner, 'runtime', None)},
                                 'syntax_check': check}

    def _compile(self, run, plan, tree):
        image, reason = self._resolve(SANDBOX_PROFILES[plan.profile])
        if not image:
            raise _Failed(reason)
        self._update(run, event=f'Compiling offline in the sandbox ({image[:19]}); no network, no host files.')
        result = self._sandbox(run, image, plan, tree, plan.mode, pk.MAX_OUTPUT + (4 << 20))
        out, binaries, toolchain = pk.read_output(result.stdout)
        members = [(e.name, e.name, 0o755 if e.executable else 0o644) for e in binaries]
        self._update(run, event=f'The sandbox produced {len(binaries)} file(s).')
        return (out, members), {'builder': {'image': image, 'runtime': getattr(self._runner, 'runtime', None),
                                            'network': 'none', 'toolchain': toolchain}}

    def _sandbox(self, run, image, plan, tree, script, max_stdout):
        """One container run; returns the Result of a clean exit or raises _Failed with the reason."""
        if self._stop.is_set():
            raise _Failed('The build was stopped because the desk is shutting down.')
        helpers = ps.HELPERS.get(script, {})
        spec = Spec(image, 'projbuild-' + run, ps.BOOTSTRAP, (('PROJECT_NAME', plan.name),),
                    lambda stream: pk.write_stage(stream, ps.SCRIPTS[script], helpers, tree, plan),
                    self.build_seconds, max_stdout)
        result = self._runner.run(spec, self._stop)
        text = result.stderr.decode('utf-8', 'replace')
        (self.runs / run / LOG_NAME).write_text(clip(text, 256 << 10), 'ascii')
        if result.stopped:
            raise _Failed('The build was stopped because the desk is shutting down.')
        if result.feed_error:
            raise _Failed(result.feed_error)
        if result.timed_out:
            raise _Failed(f'The build exceeded its {int(self.build_seconds)}-second limit and was stopped.')
        if result.overflow:
            raise _Failed('The build output exceeded its size limit.')
        if result.status:
            raise _Failed(self._reason(run, result.status, text))
        return result

    def _reason(self, run, status, text):
        lines = [line for line in text.splitlines() if line.strip()]
        marked = [line[len(MARKER):] for line in lines if line.startswith(MARKER)]
        if lines:
            self._update(run, event='Last build output: ' + clip('\n'.join(lines[-12:]), 1200))
        if marked:
            return clip(marked[-1], 400)
        if status in DOCKER_ENVIRONMENT_ERRORS:
            return (f'The sandbox could not run the build (exit {status}); the pinned image may lack a shell or '
                    f'tar, or the container could not start.')
        return f'The build failed (exit {status}). See the last build output.'
