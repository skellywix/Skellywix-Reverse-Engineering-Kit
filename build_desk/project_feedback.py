"""Private correction history for project builds: text rules and the append-only issue store.

An issue is one explicit report about a finished run, plus the reviews later runs earned. Each event is its own
file written once and never rewritten:

    runs/<run id>/issues/<issue id>/report.json
    runs/<run id>/issues/<issue id>/review-01.json, review-02.json, ...

The run that was reported is the only place an issue is stored. Its project, and so who may review it, is whatever
that run's own record says; nothing here repeats it. Everything is bounded and written atomically (temporary file,
fsync, replace). `lock` is the owner's lock: only one process owns the store.
"""
import json
import os
import re
import stat
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEX32 = re.compile(r'[0-9a-f]{32}')
HASH = re.compile(r'[0-9a-f]{64}')
OUTCOMES = ('fixed', 'still_open')
MAX_TEXT = 1000
MAX_NAME = 80
MAX_REVIEWS = 50
MAX_ISSUES = 100                    # per project
MAX_EVENT = 64 << 10
ISSUES = 'issues'
REPORT = 'report.json'
REVIEW = re.compile(r'review-(\d{2})\.json')
TEMP_SUFFIX = '.tmp'
ALLOWED_CONTROLS = {'\n', '\t'}
# Bidirectional overrides and isolates reorder what a reader sees; refused like controls.
BIDI = set('\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
REFUSED_CATEGORIES = ('Cc', 'Cs', 'Zl', 'Zp')


def text(value, what, *, name=False):
    """A 1-1000 character (name: 1-80) string without unsupported controls. Returns it stripped. ValueError."""
    if not isinstance(value, str):
        raise ValueError(f'The {what} must be text.')
    value = value.replace('\r\n', '\n').strip()
    limit = MAX_NAME if name else MAX_TEXT
    if not 1 <= len(value) <= limit:
        raise ValueError(f'The {what} must be 1 to {limit} characters.')
    for char in value:
        if char in BIDI or (unicodedata.category(char) in REFUSED_CATEGORIES
                            and (name or char not in ALLOWED_CONTROLS)):
            raise ValueError(f'The {what} contains a character that is not allowed.')
    return value


_clock = threading.Lock()
_last = datetime.min.replace(tzinfo=timezone.utc)


def now():
    """Strictly increasing within the process, so issues and reviews always order the way they were written."""
    global _last
    with _clock:
        _last = max(datetime.now(timezone.utc), _last + timedelta(microseconds=1))
        return _last.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _is_time(value):
    return isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z', value) is not None


def _is_hash(value, *, nullable):
    return (value is None and nullable) or (isinstance(value, str) and HASH.fullmatch(value) is not None)


def _has_payload(event):
    return (_is_hash(event['payload_sha256'], nullable=True)
            and (event['payload_sha256'] is None) == (event['sha256'] is None))


def _is_text(value):
    try:
        return text(value, 'text') == value.replace('\r\n', '\n')
    except ValueError:
        return False


def _review_problem(report, reviews, candidate, digest, payload, outcome):
    """The same transition rules apply to a new review and to the saved history."""
    if reviews and reviews[-1]['outcome'] == 'fixed':
        return 'This issue is already marked fixed.'
    if candidate == report['run_id'] or any(r['run_id'] == candidate for r in reviews):
        return 'That build has already been reported or reviewed on this issue.'
    if outcome == 'fixed':
        if digest is None:
            return 'A build that failed cannot fix an issue.'
        if any(r['sha256'] == digest or r['payload_sha256'] == payload for r in (report, *reviews)):
            return 'The delivered files are unchanged from a reported or reviewed build.'
    return None


class Unreadable(RuntimeError):
    """A stored event is missing, malformed or inconsistent."""


class IssueStore:
    def __init__(self, runs, lock):
        self._runs = Path(runs)
        self._lock = lock

    # ------------------------------------------------------------------ reading

    def _dir(self, run_id, issue_id):
        return self._runs / run_id / ISSUES / issue_id

    def _read(self, path):
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EVENT:
                raise Unreadable('An issue record is unreadable.')
            event = json.loads(path.read_text('utf-8'))
        except Unreadable:
            raise
        except (OSError, ValueError) as error:
            raise Unreadable('An issue record is unreadable.') from error
        if not isinstance(event, dict):
            raise Unreadable('An issue record is unreadable.')
        return event

    def locate(self, issue_id):
        """The run an issue was reported against, or None."""
        if not isinstance(issue_id, str) or not HEX32.fullmatch(issue_id):
            return None
        try:
            names = [p.name for p in self._runs.iterdir()]
        except OSError as error:
            raise OSError('The run store could not be read.') from error
        for name in sorted(names):
            if HEX32.fullmatch(name) and (self._dir(name, issue_id) / REPORT).exists():
                return name
        return None

    def load(self, run_id, issue_id):
        """The validated issue as returned to callers. Raises Unreadable on any inconsistency."""
        directory = self._dir(run_id, issue_id)
        report = self._read(directory / REPORT)
        keys = {'format', 'kind', 'id', 'run_id', 'input_sha256', 'sha256', 'payload_sha256', 'expected', 'observed', 'at'}
        if (set(report) != keys or report['format'] != 1 or report['kind'] != 'report' or report['id'] != issue_id
                or report['run_id'] != run_id or not _is_hash(report['input_sha256'], nullable=False)
                or not _is_hash(report['sha256'], nullable=True) or not _has_payload(report) or not _is_text(report['expected'])
                or not _is_text(report['observed']) or not _is_time(report['at'])):
            raise Unreadable('An issue record is unreadable.')
        try:
            names = sorted(p.name for p in directory.iterdir() if not p.name.endswith(TEMP_SUFFIX))
        except OSError as error:
            raise Unreadable('An issue record is unreadable.') from error
        reviews = []
        for index, name in enumerate(n for n in names if n != REPORT):
            found = REVIEW.fullmatch(name)
            if not found or int(found.group(1)) != index + 1 or index >= MAX_REVIEWS:
                raise Unreadable('An issue record is unreadable.')
            event = self._read(directory / name)
            rkeys = {'format', 'kind', 'issue_id', 'seq', 'run_id', 'input_sha256', 'sha256', 'payload_sha256',
                     'outcome', 'note', 'at'}
            if (set(event) != rkeys or event['format'] != 1 or event['kind'] != 'review'
                    or event['issue_id'] != issue_id or event['seq'] != index + 1
                    or not (isinstance(event['run_id'], str) and HEX32.fullmatch(event['run_id']))
                    or not _is_hash(event['input_sha256'], nullable=False)
                    or not _is_hash(event['sha256'], nullable=True) or not _has_payload(event) or event['outcome'] not in OUTCOMES
                    or not _is_text(event['note']) or not _is_time(event['at'])
                    or _review_problem(report, reviews, event['run_id'], event['sha256'], event['payload_sha256'],
                                       event['outcome'])):
                raise Unreadable('An issue record is unreadable.')
            reviews.append({k: event[k] for k in ('run_id', 'input_sha256', 'sha256', 'payload_sha256', 'outcome', 'note', 'at')})
        fixed = bool(reviews) and reviews[-1]['outcome'] == 'fixed'
        return {'id': issue_id, 'run_id': run_id, 'input_sha256': report['input_sha256'], 'sha256': report['sha256'],
                'payload_sha256': report['payload_sha256'], 'expected': report['expected'], 'observed': report['observed'],
                'state': 'fixed' if fixed else 'open', 'at': report['at'], 'reviews': reviews}

    def for_run(self, run_id):
        """Every issue reported against one run, oldest first. Raises Unreadable."""
        root = self._runs / run_id / ISSUES
        try:
            names = sorted(p.name for p in root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as error:
            raise Unreadable('An issue record is unreadable.') from error
        issues = []
        for name in names:
            if not HEX32.fullmatch(name):
                raise Unreadable('An issue record is unreadable.')
            if not (root / name / REPORT).exists():
                if any(not p.name.endswith(TEMP_SUFFIX) for p in (root / name).iterdir()):
                    raise Unreadable('An issue record is unreadable.')
                continue  # a report that never reached the disk: nothing was ever promised to the reporter
            issues.append(self.load(run_id, name))
        return sorted(issues, key=lambda i: (i['at'], i['id']))

    # ------------------------------------------------------------------ writing

    def _put_new(self, path, event):
        """Write `event` to a file that must not exist. Caller holds the lock."""
        if path.exists():
            raise RuntimeError('This issue changed while it was being updated. Try again.')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + TEMP_SUFFIX)
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(event, stream, ensure_ascii=True, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def create(self, run_id, issue_id, input_sha256, sha256, payload_sha256, expected, observed):
        event = {'format': 1, 'kind': 'report', 'id': issue_id, 'run_id': run_id, 'input_sha256': input_sha256,
                 'sha256': sha256, 'payload_sha256': payload_sha256, 'expected': expected, 'observed': observed, 'at': now()}
        with self._lock:
            self._put_new(self._dir(run_id, issue_id) / REPORT, event)
            return self.load(run_id, issue_id)

    def append_review(self, run_id, issue_id, review_run, input_sha256, sha256, payload_sha256, outcome, note):
        with self._lock:
            issue = self.load(run_id, issue_id)
            problem = _review_problem(issue, issue['reviews'], review_run, sha256, payload_sha256, outcome)
            if problem:
                raise RuntimeError(problem)
            seq = len(issue['reviews']) + 1
            if seq > MAX_REVIEWS:
                raise RuntimeError('This issue has reached its review limit.')
            event = {'format': 1, 'kind': 'review', 'issue_id': issue_id, 'seq': seq, 'run_id': review_run,
                     'input_sha256': input_sha256, 'sha256': sha256, 'payload_sha256': payload_sha256,
                     'outcome': outcome, 'note': note, 'at': now()}
            self._put_new(self._dir(run_id, issue_id) / f'review-{seq:02d}.json', event)
            return self.load(run_id, issue_id)
