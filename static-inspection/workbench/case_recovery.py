"""Offline restore of one saved operator case: the JSON body of the read-only GET /api/state.

Use: ``load_case(path)`` returns a ``Case`` to serve as the initial case before the sockets open.
There is no HTTP import. The caller (an offline --restore-case option) passes a trusted, local,
operator-owned file; nothing here reads from a network, imports modules, or runs file content.

What is refused, with a human-readable CaseRestoreError and nothing loaded:
  * a file that is not a regular, non-symlink, owner-only (no group/other/exec bits) file owned by
    the current user, in a directory nobody else can write to, or that exceeds MAX_CASE_BYTES;
  * invalid UTF-8/JSON, duplicate field names, floats, NaN/Infinity, or any field/type the live
    Case.view() would not have produced (exact schema; unknown fields such as a token are refused);
  * metadata, numbering, event results, counts, skipped checks or critique that disagree with each
    other or with the slots (see _validate);
  * a case with an active agent connection ('ready'/'connected') or an in-flight critique
    ('pending'/'reviewing'). The operator must disconnect/cancel and save the case again.

Credentials are never restored. The file cannot carry a token, session, ticket, lease or deadline;
a restored case always has token/session/ticket empty. 'expired' and 'spent' are kept as inert
status labels; an old grant can never authorize against the restored case.

Limits that cannot be removed offline:
  * The file is not authenticated. Sample bytes are never retained, so size/SHA-256 claims cannot be
    re-derived; only their internal consistency (format, size floor, event results) is checked.
  * Case keeps just the latest HISTORY events. When earlier events were omitted, per-actor totals
    and the mismatch count can only be bounded: actors never below what is retained and summing to
    the total, and mismatches between the retained mismatches and retained plus omitted events.
    Their exact split over the omitted events is therefore preserved as written, not proven.
  * A critique is model text from the operator's own earlier session; it is kept only when ready,
    printable, <= 320 characters and citing retained events. It is data, never markup or authority.
  * Only the immediate parent directory's ownership/write bits are checked, not every ancestor.
"""
import json
import os
import re
import stat
from collections import deque
from pathlib import Path

from case_state import HISTORY, MAX_CALLS, Case
from evidence import MAX_SAMPLE


MAX_CASE_BYTES = 256 * 1024
MAX_REVISION = 10**16 - 1  # The operator API accepts at most 16 revision digits.
MAX_TOTAL = 10**9

_SCHEMA = 'yuzu.report/1'
_TOP = ('schema', 'revision', 'slots', 'events', 'earlier_events_omitted', 'connection', 'remaining', 'advisor')
_ADVISOR = ('total', 'mismatches', 'actors', 'skipped', 'critique')
_ACTORS = ('operator', 'agent', 'skellywix')
_TOOLS = ('identify_file', 'verify_checksum', 'compare_builds', 'export_report')
_FORMATS = ('unknown', 'elf', 'mz', 'pe', 'pdf', 'png', 'zip')
_ARCHES = ('unknown', 'x86', 'arm', 'x86-64', 'arm64')
_MIN_SIZE = {'unknown': 0, 'elf': 4, 'mz': 2, 'pe': 68, 'pdf': 5, 'png': 8, 'zip': 4}
_EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
_DIGEST = re.compile(r'[0-9a-f]{64}')
_CONNECTIONS = ('off', 'ready', 'connected', 'expired', 'spent')
_STATUSES = ('idle', 'pending', 'reviewing', 'ready', 'stale', 'expired', 'failed', 'not_requested', 'cancelled')
_SKIPPED = ([], ['compare_builds'], ['verify_checksum'], ['compare_builds', 'verify_checksum'])
_SAFE_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,31}')


class CaseRestoreError(ValueError):
    """The saved case was refused. Text is fixed wording; file values are never repeated."""

    def __init__(self, reason: str):
        super().__init__(f'Case not restored: {reason.rstrip(".")}. Nothing was loaded.')


def _fail(reason: str):
    raise CaseRestoreError(reason)


def _named(key) -> str:
    return f' "{key}"' if type(key) is str and _SAFE_NAME.fullmatch(key) else ''


def _int(value, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(f'{where} must be a whole number from {low} to {high}')
    return value


def _record(value, where: str, required: tuple, optional: tuple = ()) -> dict:
    if type(value) is not dict:
        _fail(f'{where} must be a JSON object')
    for key in value:
        if key not in required and key not in optional:
            _fail(f'{where} has an unexpected field{_named(key)}')
    for key in required:
        if key not in value:
            _fail(f'{where} is missing the required field "{key}"')
    return value


def _meta(value, where: str) -> dict:
    meta = _record(value, where, ('size', 'sha256', 'format', 'architecture'))
    size = _int(meta['size'], f'{where}.size', 0, MAX_SAMPLE)
    digest, fmt, arch = meta['sha256'], meta['format'], meta['architecture']
    if type(digest) is not str or not _DIGEST.fullmatch(digest):
        _fail(f'{where}.sha256 must be 64 lowercase hexadecimal characters')
    if type(fmt) is not str or fmt not in _FORMATS:
        _fail(f'{where}.format is not a recognised format')
    if type(arch) is not str or arch not in _ARCHES:
        _fail(f'{where}.architecture is not a recognised architecture')
    if arch != 'unknown' and fmt not in ('elf', 'pe'):
        _fail(f'{where} names an architecture its format cannot carry')
    floor = _MIN_SIZE[fmt]
    if arch != 'unknown':
        floor = 224 if fmt == 'pe' else 64 if arch in ('x86-64', 'arm64') else 52
    if size < floor:
        _fail(f'{where}.size is too small for the recorded format')
    if (size == 0) != (digest == _EMPTY_SHA256):
        _fail(f'{where}.sha256 does not agree with the recorded size')
    return {'size': size, 'sha256': digest, 'format': fmt, 'architecture': arch}


def _slots(value) -> dict:
    if type(value) is not dict or not set(value) <= {'a', 'b'} or ('b' in value and 'a' not in value):
        _fail('slots must hold file A, or files A and B')
    return {slot: _meta(value[slot], f'slots.{slot}') for slot in ('a', 'b') if slot in value}


def _event(value, where: str, slots: dict) -> dict:
    event = _record(value, where, ('n', 'actor', 'tool', 'result'), ('slot',))
    n = _int(event['n'], f'{where}.n', 1, MAX_TOTAL)
    actor, tool = event['actor'], event['tool']
    if type(actor) is not str or actor not in _ACTORS:
        _fail(f'{where}.actor is not a known actor')
    if type(tool) is not str or tool not in _TOOLS:
        _fail(f'{where}.tool is not a known evidence check')
    if actor == 'skellywix' and tool == 'export_report':
        _fail(f'{where} attributes an export to the review, which never exports')
    per_slot = tool in ('identify_file', 'verify_checksum')
    if per_slot != ('slot' in event):
        _fail(f'{where} has the wrong slot field for its check')
    row = {'n': n, 'actor': actor, 'tool': tool}
    result = event['result']
    if per_slot:
        slot = event['slot']
        if type(slot) is not str or slot not in slots:
            _fail(f'{where}.slot is not a file in this case')
        meta = slots[slot]
    if tool == 'identify_file':
        if _meta(result, f'{where}.result') != meta:
            _fail(f'{where}.result differs from the file it describes')
        result = dict(meta)
    elif tool == 'verify_checksum':
        r = _record(result, f'{where}.result', ('matches', 'actual', 'expected'))
        if (type(r['matches']) is not bool or r['actual'] != meta['sha256'] or type(r['expected']) is not str
                or not _DIGEST.fullmatch(r['expected']) or r['matches'] != (r['actual'] == r['expected'])):
            _fail(f'{where}.result is not a consistent checksum comparison')
        result = dict(r)
    elif tool == 'compare_builds':
        r = _record(result, f'{where}.result', ('identical', 'size_delta', 'format_changed', 'architecture_changed'))
        if 'b' not in slots:
            _fail(f'{where} compares builds but the case has no second file')
        a, b = slots['a'], slots['b']
        want = {'identical': a['sha256'] == b['sha256'] and a['size'] == b['size'], 'size_delta': b['size'] - a['size'],
                'format_changed': a['format'] != b['format'], 'architecture_changed': a['architecture'] != b['architecture']}
        if r != want or any(type(r[k]) is not type(want[k]) for k in want):
            _fail(f'{where}.result disagrees with the two files')
        result = dict(want)
    else:
        r = _record(result, f'{where}.result', ('files',))
        if type(r['files']) is not int or r['files'] != len(slots):
            _fail(f'{where}.result disagrees with the number of files')
        result = {'files': len(slots)}
    row['result'] = result
    if per_slot:
        row['slot'] = slot
    return row


def _critique(value, total: int, omitted: int, slots: dict, events: list, actors: dict, skipped: list) -> dict:
    note = _record(value, 'advisor.critique', ('status', 'end'), ('text', 'cites'))
    status = note['status']
    if type(status) is not str or status not in _STATUSES:
        _fail('advisor.critique.status is not a known state')
    end = _int(note['end'], 'advisor.critique.end', 0, total)
    ready = status == 'ready'
    if ready != ('text' in note) or ready != ('cites' in note):
        _fail('only a ready critique carries text and evidence references')
    if status == 'idle':
        if end or actors['skellywix'] or skipped:
            _fail('advisor records review activity but its critique is idle')
        return {'status': status, 'end': 0}
    if not actors['skellywix']:
        _fail('advisor.critique records a review but no review checks ran')
    if ('compare_builds' in skipped) == ('b' in slots):
        _fail('advisor.skipped disagrees with the files in the case')
    # The latest review is exactly these checks, ending at event number `end` (see Case.start_review).
    plan = [('identify_file', slot) for slot in slots]
    if 'compare_builds' not in skipped:
        plan.append(('compare_builds', None))
    if 'verify_checksum' not in skipped:
        plan.append(('verify_checksum', 'a'))
    if end < len(plan) or actors['skellywix'] < len(plan):
        _fail('the review checks recorded do not fit the advisor counts')
    first = omitted + 1
    for offset, (tool, slot) in enumerate(plan):
        n = end - len(plan) + 1 + offset
        if n >= first:
            event = events[n - first]
            if event['actor'] != 'skellywix' or event['tool'] != tool or event.get('slot') != slot:
                _fail(f'event {n} is not the review check the advisor says ended at {end}')
    if status in ('pending', 'reviewing', 'ready') and end != total:
        _fail('a current critique must end at the latest event')
    if status == 'stale' and end >= total:
        _fail('a stale critique must end before the latest event')
    out = {'status': status, 'end': end}
    if ready:
        text, cites = note['text'], note['cites']
        if type(text) is not str or text != text.strip() or not 1 <= len(text) <= 320 or not text.isprintable():
            _fail('advisor.critique.text must be one printable paragraph of at most 320 characters')
        if (type(cites) is not list or not 1 <= len(cites) <= 3 or len(set(map(repr, cites))) != len(cites)
                or any(type(n) is not int or not first <= n <= total for n in cites)):
            _fail('advisor.critique.cites must name one to three distinct retained events')
        out |= {'text': text, 'cites': list(cites)}
    return out


def _validate(state) -> dict:
    top = _record(state, 'The saved case', _TOP)
    if type(top['schema']) is not str or top['schema'] != _SCHEMA:
        _fail('the file is not a yuzu.report/1 case state')
    revision = _int(top['revision'], 'revision', 0, MAX_REVISION)
    slots = _slots(top['slots'])
    if revision < len(slots):
        _fail('revision is lower than the number of files loaded')
    advisor = _record(top['advisor'], 'advisor', _ADVISOR)
    total = _int(advisor['total'], 'advisor.total', 0, MAX_TOTAL)
    omitted = _int(top['earlier_events_omitted'], 'earlier_events_omitted', 0, MAX_TOTAL)
    kept = min(total, HISTORY)
    if omitted != total - kept:
        _fail(f'earlier_events_omitted must equal advisor.total minus the at most {HISTORY} retained events')
    if type(top['events']) is not list or len(top['events']) != kept:
        _fail(f'events must hold exactly the {kept} most recent event(s) for advisor.total')
    if total and not slots:
        _fail('events exist but no file is loaded')
    events = []
    for index, item in enumerate(top['events']):
        event = _event(item, f'events[{index}]', slots)
        if event['n'] != omitted + 1 + index:
            _fail(f'events[{index}] is not numbered {omitted + 1 + index}; numbering must be consecutive and end at the total')
        events.append(event)
    counted = _record(advisor['actors'], 'advisor.actors', _ACTORS)
    actors = {actor: _int(counted[actor], f'advisor.actors.{actor}', 0, MAX_TOTAL) for actor in _ACTORS}
    if sum(actors.values()) != total:
        _fail('advisor.actors do not add up to advisor.total')
    seen, seen_mismatch = dict.fromkeys(_ACTORS, 0), 0
    for event in events:
        seen[event['actor']] += 1
        seen_mismatch += event['tool'] == 'verify_checksum' and not event['result']['matches']
    for actor in _ACTORS:
        if actors[actor] < seen[actor]:
            _fail(f'advisor.actors.{actor} counts fewer calls than the retained events show')
    mismatches = _int(advisor['mismatches'], 'advisor.mismatches', 0, MAX_TOTAL)
    if not seen_mismatch <= mismatches <= seen_mismatch + omitted:
        _fail('advisor.mismatches cannot be reconciled with the retained events and the omitted count')
    skipped = advisor['skipped']
    if skipped not in _SKIPPED:
        _fail('advisor.skipped must list the skipped review checks in order')
    critique = _critique(advisor['critique'], total, omitted, slots, events, actors, skipped)
    connection = top['connection']
    if type(connection) is not str or connection not in _CONNECTIONS:
        _fail('connection is not a known state')
    remaining = _int(top['remaining'], 'remaining', 0, MAX_CALLS)
    active = connection in ('ready', 'connected')
    if (remaining == 0) == active or not slots and connection != 'off':
        _fail('remaining calls disagree with the connection state')
    if active:
        _fail('an agent connection is still active. Disconnect the agent in the desk, then save the case again')
    if critique['status'] in ('pending', 'reviewing'):
        _fail('a critique is still in flight. Cancel it or let it finish or expire, then save the case again')
    return {'schema': _SCHEMA, 'revision': revision, 'slots': slots, 'events': events,
            'earlier_events_omitted': omitted, 'connection': connection, 'remaining': 0,
            'advisor': {'total': total, 'mismatches': mismatches, 'actors': actors,
                        'skipped': list(skipped), 'critique': critique}}


def restore_case(state) -> Case:
    """Validate a decoded /api/state object and return a fresh Case holding exactly that state."""
    view = _validate(state)
    advisor = view['advisor']
    case = Case()
    case.revision = view['revision']
    case.slots = {slot: dict(meta) for slot, meta in view['slots'].items()}
    case.number = advisor['total']
    case.events = deque(({**event, 'result': dict(event['result'])} for event in view['events']), maxlen=HISTORY)
    case.actors = dict(advisor['actors'])
    case.mismatches = advisor['mismatches']
    case.skipped = list(advisor['skipped'])
    case.critique = {**advisor['critique']}
    if 'cites' in case.critique:
        case.critique['cites'] = list(case.critique['cites'])
    case._drop(view['connection'])  # Clears token, session, version and deadline; keeps only the label.
    if case.token or case.sid or case._ticket or case.view() != view:
        _fail('the restored case does not match the saved state; the inspector and file disagree')
    return case


def _object(pairs: list) -> dict:
    out = dict(pairs)
    if len(out) != len(pairs):
        _fail('the file repeats a field name')
    return out


def _refuse_number(_text: str):
    _fail('the file contains a number that is not a whole number')


def parse_case(raw: bytes) -> dict:
    """Decode bounded UTF-8 JSON strictly: no duplicate keys, floats or NaN/Infinity."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_CASE_BYTES:
        _fail(f'the file is larger than {MAX_CASE_BYTES} bytes')
    try:
        return json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=_object,
                          parse_float=_refuse_number, parse_constant=_refuse_number)
    except CaseRestoreError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail('the file is not valid UTF-8 JSON')


def _check_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        _fail('the saved case path is not a regular file (links, pipes, devices and directories are refused)')
    if info.st_uid != os.geteuid():
        _fail('the saved case file is not owned by the account running the inspector')
    if stat.S_IMODE(info.st_mode) & ~0o600:
        _fail('the saved case file must be private: mode 600 or stricter, with no group, other or execute access')
    if info.st_size > MAX_CASE_BYTES:
        _fail(f'the file is larger than {MAX_CASE_BYTES} bytes')


def _check_directory(directory: Path) -> None:
    try:
        info = os.stat(directory)
    except OSError as exc:
        _fail(f'the saved case directory cannot be inspected ({exc.strerror or "unknown error"})')
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (os.geteuid(), 0):
        _fail('the saved case directory must belong to the current user or root')
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        _fail('the saved case directory must not be writable by group or others')


def _read_private_file(path) -> bytes:
    if not hasattr(os, 'geteuid') or not hasattr(os, 'O_NOFOLLOW'):
        _fail('this host cannot verify file ownership and private permissions')
    path = Path(os.path.abspath(path))
    try:
        link = os.lstat(path)
    except OSError as exc:
        _fail(f'the saved case file cannot be inspected ({exc.strerror or "unknown error"})')
    if stat.S_ISLNK(link.st_mode):
        _fail('the saved case path is a symbolic link; name the real file')
    _check_file(link)
    _check_directory(path.parent)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOCTTY', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _fail(f'the saved case file cannot be opened ({exc.strerror or "unknown error"})')
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (link.st_dev, link.st_ino):
            _fail('the saved case file changed while it was being opened')
        _check_file(info)
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, MAX_CASE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_CASE_BYTES:
                _fail(f'the file is larger than {MAX_CASE_BYTES} bytes')
    finally:
        os.close(fd)
    return b''.join(chunks)


def load_case(path: Path) -> Case:
    """Load a saved operator case from a trusted private local file, or raise CaseRestoreError."""
    return restore_case(parse_case(_read_private_file(path)))
