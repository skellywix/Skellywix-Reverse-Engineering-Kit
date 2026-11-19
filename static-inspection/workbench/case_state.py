"""One in-memory case. The transport serializes changes under its shared lock."""
from collections import deque
from secrets import compare_digest, token_urlsafe
from time import monotonic

from evidence import analyze, validate_arguments


MAX_CALLS = 32
GRANT_SECONDS = 900
HISTORY = 64
CRITIQUE_SECONDS = 120


class CaseError(ValueError):
    def __init__(self, status: int, text: str):
        self.status = status
        super().__init__(text)


def _matches(provided: str | None, expected: str | None) -> bool:
    return bool(expected and isinstance(provided, str) and provided.isascii()
                and compare_digest(provided, expected))


def _brief_event(event: dict) -> dict:
    tool, result = event['tool'], event['result']
    if tool == 'identify_file':
        outcome = 'unclassified' if result['format'] == 'unknown' else 'recognized'
    elif tool == 'verify_checksum':
        outcome = 'match' if result['matches'] else 'mismatch'
    elif tool == 'compare_builds':
        outcome = 'identical' if result['identical'] else 'different'
    elif tool == 'export_report':
        outcome = 'exported'
    else:
        raise ValueError('Unsupported evidence category.')
    row = {'n': event['n'], 'actor': event['actor'], 'check': tool, 'outcome': outcome}
    if 'slot' in event:
        row['slot'] = event['slot']
    return row


class Case:
    def __init__(self):
        self.revision = 0
        self.slots: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=HISTORY)
        self.number = 0
        self._clear_advisor()
        self._drop('off')

    def _drop(self, status: str) -> None:
        self.token: str | None = None
        self.sid: str | None = None
        self.version: str | None = None
        self.remaining = 0
        self.expires = 0.0
        self.status = status

    def _expire(self) -> None:
        if self.token and monotonic() >= self.expires:
            self._drop('expired')

    def check_revision(self, revision: int) -> None:
        if type(revision) is not int or revision != self.revision:
            raise CaseError(409, 'The files changed. Refresh the case and try again.')

    def _new_inputs(self) -> None:
        self._drop('off')
        self.number = 0
        self.events.clear()
        self.revision += 1
        self._clear_advisor()

    def reset(self, revision: int) -> None:
        self.check_revision(revision)
        self.slots.clear()
        self._new_inputs()

    def admit(self, revision: int, slot: str, metadata: dict) -> None:
        self.check_revision(revision)
        if slot not in ('a', 'b') or (slot == 'b' and 'a' not in self.slots):
            raise CaseError(400, 'Add file A first.')
        if slot == 'a':
            self.slots.clear()
        self.slots[slot] = metadata
        self._new_inputs()

    def report(self) -> dict:
        return {'schema': 'yuzu.report/1', 'revision': self.revision, 'slots': dict(self.slots),
                'events': list(self.events), 'earlier_events_omitted': self.number - len(self.events)}

    def view(self) -> dict:
        self._expire()
        self._expire_critique()
        advisor = {'total': self.number, 'mismatches': self.mismatches, 'actors': self.actors.copy(),
                   'skipped': self.skipped, 'critique': self.critique.copy()}
        return self.report() | {'connection': self.status, 'remaining': self.remaining, 'advisor': advisor}

    def _clear_advisor(self) -> None:
        self.actors = dict.fromkeys(('operator', 'agent', 'skellywix'), 0)
        self.mismatches = 0
        self.skipped = []
        self._set_critique('idle')

    def _set_critique(self, status: str) -> None:
        self.critique = {'status': status, 'end': self.number}
        self._ticket: str | None = None
        self._deadline = 0.0

    def _expire_critique(self) -> None:
        if self._deadline and monotonic() >= self._deadline:
            self._set_critique('expired')

    def critic_status(self) -> dict:
        self._expire_critique()
        return {'revision': self.revision, 'loaded': bool(self.slots), 'status': self.critique['status']}

    def start_review(self, revision: int, reference: str | None, critique: bool) -> None:
        self.check_revision(revision)
        if not self.slots or type(critique) is not bool:
            raise CaseError(400, 'Load A and choose whether to request a critique.')
        if self.critic_status()['status'] in ('pending', 'reviewing'):
            raise CaseError(409, 'Cancel or finish the current critique before starting a new pass.')
        if reference is not None:
            validate_arguments('verify_checksum', {'slot': 'a', 'sha256': reference})
        # One atomic pass over retained metadata. Neither the model nor a request provides a work list.
        skipped = []
        for slot in self.slots:
            self._call('skellywix', 'identify_file', {'slot': slot})
        if 'b' in self.slots:
            self._call('skellywix', 'compare_builds', {})
        else:
            skipped.append('compare_builds')
        if reference is not None:
            self._call('skellywix', 'verify_checksum', {'slot': 'a', 'sha256': reference})
        else:
            skipped.append('verify_checksum')
        self.skipped = skipped
        self._set_critique('pending' if critique else 'not_requested')
        if critique:
            self._deadline = monotonic() + CRITIQUE_SECONDS

    def claim_critique(self, revision: int) -> dict:
        self.check_revision(revision)
        if self.critic_status()['status'] != 'pending':
            raise CaseError(409, 'No current review is waiting for a critic.')
        brief = {'samples': list(self.slots), 'total': self.number, 'mismatches': self.mismatches,
                 'omitted': self.number - len(self.events), 'events': [_brief_event(e) for e in self.events]}
        self._set_critique('reviewing')
        self._ticket = token_urlsafe(24)
        self._deadline = monotonic() + CRITIQUE_SECONDS
        return {'brief': brief, 'ticket': self._ticket}

    def finish_critique(self, revision: int, ticket: str, note: dict | None) -> None:
        self.check_revision(revision)
        if self.critic_status()['status'] != 'reviewing' or not _matches(ticket, self._ticket):
            raise CaseError(409, 'The critique is no longer current.')
        if note is None:
            self._set_critique('failed')
            return
        if not isinstance(note, dict) or set(note) != {'text', 'cites'}:
            raise CaseError(400, 'A critique needs text and evidence references.')
        text, cites = note['text'], note['cites']
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 320 or len(text) > 320
                or not text.isprintable() or not isinstance(cites, list) or not 1 <= len(cites) <= 3
                or any(type(n) is not int or not self.events[0]['n'] <= n <= self.number for n in cites)
                or len(set(cites)) != len(cites)):
            raise CaseError(400, 'The critique must cite retained findings and fit in one paragraph.')
        self._set_critique('ready')
        self.critique.update(text=text.strip(), cites=cites.copy())

    def cancel_critique(self, revision: int, end: int) -> None:
        self.check_revision(revision)
        if type(end) is not int or end < 1:
            raise CaseError(400, 'A review snapshot is required.')
        if self.critic_status()['status'] not in ('pending', 'reviewing'):
            raise CaseError(409, 'There is no pending critique.')
        if end != self.critique['end']:
            raise CaseError(409, 'A different critique is now pending.')
        self._set_critique('cancelled')

    def pair(self, revision: int) -> str:
        self.check_revision(revision)
        if 'a' not in self.slots:
            raise CaseError(400, 'Add file A before connecting an agent.')
        self._drop('ready')
        self.token = token_urlsafe(32)
        self.expires = monotonic() + GRANT_SECONDS
        self.remaining = MAX_CALLS
        return self.token

    def disconnect(self, revision: int) -> None:
        self.check_revision(revision)
        self._drop('off')

    def authorize(self, token: str | None) -> None:
        self._expire()
        if not _matches(token, self.token):
            raise CaseError(401, 'Case access is unavailable. Pair again from the desk.')

    def begin(self, token: str, version: str) -> str:
        self.authorize(token)
        if self.sid:
            raise CaseError(409, 'This key is already paired. Create a new one to reconnect.')
        self.sid = token_urlsafe(24)
        self.version = version
        return self.sid

    def session(self, token: str, sid: str | None, *, active: bool = True) -> None:
        self.authorize(token)
        if not _matches(sid, self.sid):
            raise CaseError(404, 'Unknown case session.')
        if active and self.status != 'connected':
            raise CaseError(400, 'Initialize the agent connection first.')

    def initialized(self, token: str, sid: str) -> None:
        self.session(token, sid, active=False)
        self.status = 'connected'

    def _call(self, actor: str, tool: str, args: dict) -> dict:
        if not self.slots:
            raise CaseError(400, 'Add file A to start a case.')
        if tool == 'export_report':
            if not isinstance(args, dict) or args:
                raise ValueError('Export takes no arguments.')
            result = {'files': len(self.slots)}
        else:
            result = analyze(tool, args, self.slots)
        self._expire_critique()
        if self.critique['status'] in ('pending', 'reviewing', 'ready'):
            self._set_critique('stale')
        self.actors[actor] += 1
        if tool == 'verify_checksum' and not result['matches']:
            self.mismatches += 1
        self.number += 1
        record = {'n': self.number, 'actor': actor, 'tool': tool, 'result': result}
        if tool in ('identify_file', 'verify_checksum'):
            record['slot'] = args['slot']
        self.events.append(record)
        return self.report() if tool == 'export_report' else result

    def operator_call(self, revision: int, tool: str, args: dict) -> dict:
        self.check_revision(revision)
        return self._call('operator', tool, args)

    def agent_call(self, token: str, sid: str, tool: str, args: dict) -> dict:
        self.session(token, sid)
        self.remaining -= 1
        if self.remaining == 0:
            self._drop('spent')
        return self._call('agent', tool, args)
