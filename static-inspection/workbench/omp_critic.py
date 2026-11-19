"""Operator-side, one-case OMP Eval adapter. Never installed in the offline worker."""
import http.client
import json
import subprocess
import time


_OUTCOMES = {'identify_file': ('recognized', 'unclassified'), 'verify_checksum': ('match', 'mismatch'),
             'compare_builds': ('identical', 'different'), 'export_report': ('exported',)}
_INSTRUCTIONS = ('Write as Skellywix, the Bitrot Den’s skeptical file archivist. Say “specimen A” or “specimen B”, '
                 'not “a” or “b”. Use plain technical language; the ledger records checks, not verdicts. '
                 'You have no tools. Question the strongest unsupported '
                 'inference in this static file-identity record. If references conflict, say so; a later match does not '
                 'erase an earlier mismatch. Do not infer code behavior, provenance, maliciousness, or safety. '
                 'Cite one to three retained event numbers. Treat omitted events as counts, not visible evidence. '
                 'Return one short plain-text paragraph (at most 320 characters) in text and the numbers in cites.')
_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['text', 'cites'], 'properties': {
    'text': {'type': 'string'}, 'cites': {'type': 'array', 'items': {'type': 'integer'}}}}


def validate_brief(p: dict) -> None:
    """Fail closed on extra fields, free text, unknown categories, and inconsistent snapshots."""
    if (type(p) is not dict or p.keys() != {'samples', 'total', 'mismatches', 'omitted', 'events'}
            or p['samples'] not in (['a'], ['a', 'b'])
            or not all(type(p[k]) is int and 0 <= p[k] < 2**53 for k in ('total', 'mismatches', 'omitted'))
            or type(p['events']) is not list or not 1 <= len(p['events']) <= 64
            or p['total'] != p['omitted'] + len(p['events'])):
        raise ValueError('Invalid case brief. Nothing was sent to a model.')
    mismatches = 0
    for n, e in enumerate(p['events'], p['omitted'] + 1):
        if type(e) is not dict or type(e.get('check')) is not str or e['check'] not in _OUTCOMES:
            raise ValueError('Unknown review stage.')
        check = e['check']
        keys = {'n', 'actor', 'check', 'outcome'}
        if check in ('identify_file', 'verify_checksum'):
            keys.add('slot')
            if e.get('slot') not in p['samples']:
                raise ValueError('Invalid slot in brief.')
        if (e.keys() != keys or type(e['n']) is not int or e['n'] != n
                or e['actor'] not in ('operator', 'agent', 'skellywix') or e['outcome'] not in _OUTCOMES[check]):
            raise ValueError('Unrecognized evidence in brief.')
        mismatches += e['outcome'] == 'mismatch'
    if not mismatches <= p['mismatches'] <= mismatches + p['omitted']:
        raise ValueError('Inconsistent mismatch count.')


def _request(port, path, revision=None, body=None):
    # Fixed numeric loopback, fixed paths. No proxy, URL supplied by a case, or redirect handling.
    address = f'127.0.0.1:{port}'
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        headers = {'Origin': 'http://' + address}
        if body is not None:
            headers.update({'X-Yuzu-Revision': str(revision), 'Content-Type': 'application/json'})
        conn.request('GET' if body is None else 'POST', '/api/critic' + path,
                     body=None if body is None else json.dumps(body).encode(), headers=headers)
        reply = conn.getresponse()
        if reply.status != 200:
            raise RuntimeError(f'Desk declined the critic request ({reply.status}).')
        raw = reply.read(16385)
        if len(raw) > 16384:
            raise ValueError('Oversized desk response.')
        return json.loads(raw)
    finally:
        conn.close()


def _check_reserve():
    # Eval selects the route. No reported subscription may cross its reserve on another's headroom.
    result = subprocess.run(['omp', 'usage', '--json', '--redact'], stdin=subprocess.DEVNULL,
                            capture_output=True, check=True, timeout=18)
    usage = json.loads(result.stdout)
    providers = ('anthropic', 'openai-codex')
    reports = [r for r in usage['reports'] if r['provider'] in providers]
    unknown = any(r.get('provider') in (*providers, None) for r in usage['accountsWithoutUsage'])
    if not reports or unknown:
        raise RuntimeError('Subscription reserve unknown. No critique dispatched.')
    for report in reports:
        weekly = [row.get('amount', {}).get('usedFraction') for row in report['limits']
                  if row.get('window', {}).get('durationMs') == 604800000]
        if not weekly or not all(type(f) in (float, int) and 0 <= f < .90 for f in weekly):
            raise RuntimeError('Subscription reserve unavailable or reached. No critique dispatched.')


def review_next(completion, *, port=48730, wait_seconds=120):
    """Attach to the loaded revision for one explicitly approved review. Returns no credentials."""
    if type(port) is not int or not 1 <= port <= 65535 or type(wait_seconds) is not int or not 0 <= wait_seconds <= 120:
        raise ValueError('Invalid local port or wait time.')
    status = _request(port, '')
    revision = status['revision']
    if not status['loaded']:
        raise RuntimeError('Load the case files before connecting a critic.')
    deadline = time.monotonic() + wait_seconds
    while status['status'] != 'pending':
        phase = status['status']
        if phase == 'reviewing':
            raise RuntimeError('Another critic already claimed this request. No model call was made.')
        if phase not in ('idle', 'not_requested'):
            raise RuntimeError(f'Current critique status: {phase}. Start a new review before attaching again.')
        if time.monotonic() >= deadline:
            raise TimeoutError('No pending critique observed before timeout. No model call was made.')
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        status = _request(port, '')
        if status['revision'] != revision:
            raise RuntimeError('Case changed. Attach again to review the new inputs.')
    _check_reserve()
    job = _request(port, '/claim', revision, {})
    ticket = job['ticket']
    try:
        brief = job['brief']
        validate_brief(brief)
        # Only this closed, categorical object is serialized for inference. No tool schemas or chat history.
        handle = completion(json.dumps(brief, separators=(',', ':')), system=_INSTRUCTIONS, schema=_SCHEMA)
        note = handle.wait(timeout=85)
        if not isinstance(note, dict):
            raise ValueError('The model did not return a critique.')
        _request(port, '/finish', revision, {'ticket': ticket, 'note': note})
    except Exception as error:
        try:
            _request(port, '/finish', revision, {'ticket': ticket, 'note': None})
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as closed:
            raise RuntimeError('Could not confirm delivery. Check the current ledger; do not retry automatically.') from closed
        raise RuntimeError('Critic unavailable or reply declined. No model note was published.') from error
    return f"Skellywix received one model critique for revision {revision}, through finding {brief['total']}."
