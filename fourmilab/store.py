"""Private candidate ledger. The SQLite transaction is the append/publish boundary."""
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def wire(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(wire(value).encode('ascii')).hexdigest()


TERMINAL = {'blocked', 'failed', 'cancelled', 'verified'}
STAGES = ('intake', 'analyze', 'rebuild', 'verify', 'package')
SHA256 = re.compile(r'[0-9a-f]{64}')
RECEIPT_HASHES = ('artifact_sha256', 'source_sha256', 'reference_rows_sha256', 'candidate_rows_sha256')
RECEIPT_KEYS = {'run', 'attempt', 'generation', 'manifest_sha256', 'required', 'matched', *RECEIPT_HASHES}


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS releases (digest TEXT PRIMARY KEY, info TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS defaults (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    active TEXT REFERENCES releases, previous TEXT REFERENCES releases, staged TEXT REFERENCES releases);
                INSERT OR IGNORE INTO defaults(singleton) VALUES(1);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (run TEXT NOT NULL REFERENCES runs, seq INTEGER NOT NULL,
                    attempt INTEGER NOT NULL, at TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, PRIMARY KEY(run,seq));
            ''')

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def stage(self, info):
        key = digest(info)
        with self.db(True) as db:
            db.execute('INSERT OR IGNORE INTO releases VALUES(?,?)', (key, wire(info)))
            db.execute('UPDATE defaults SET staged=? WHERE singleton=1', (key,))
        return key

    def release(self, key):
        with self.db() as db:
            row = db.execute('SELECT info FROM releases WHERE digest=?', (key,)).fetchone()
            if row is None or digest(info := json.loads(row[0])) != key:
                raise ValueError('Pinned release unavailable or corrupt.')
            return info

    def releases(self):
        with self.db() as db:
            out = dict(db.execute('SELECT active,previous,staged FROM defaults').fetchone())
            out['items'] = [{'digest': row[0], 'info': json.loads(row[1])} for row in
                            db.execute('SELECT digest,info FROM releases ORDER BY rowid DESC')]
            return out

    def promote(self, key):
        with self.db(True) as db:
            state = db.execute('SELECT staged,active FROM defaults').fetchone()
            if not key or key != state['staged']:
                raise ValueError('Select the validated staged revision.')
            if key != state['active']:
                db.execute('UPDATE defaults SET previous=active,active=?', (key,))

    def rollback(self):
        with self.db(True) as db:
            if not db.execute('UPDATE defaults SET active=previous,previous=active WHERE previous IS NOT NULL').rowcount:
                raise ValueError('No previous active revision.')

    @staticmethod
    def _append(db, run, seq, attempt, stage, status, detail):
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)', (run, seq, attempt,
                   datetime.now(timezone.utc).isoformat(timespec='milliseconds'), stage, status, wire(detail)))

    @staticmethod
    def _last(db, run):
        row = db.execute('SELECT seq,attempt,stage,status,detail FROM events WHERE run=? ORDER BY seq DESC LIMIT 1',
                         (run,)).fetchone()
        if row is None:
            raise ValueError('Unknown candidate run.')
        return row

    def start(self, pins):
        run = uuid.uuid4().hex
        with self.db(True) as db:
            active = db.execute('SELECT active FROM defaults').fetchone()[0]
            if not active:
                raise ValueError('Stage and promote a validated candidate first.')
            manifest = {**pins, 'release': active}
            db.execute('INSERT INTO runs VALUES(?,?,?)', (run, digest(manifest), wire(manifest)))
            self._append(db, run, 1, 1, 'intake', 'queued', {'reason': 'Queued on the pinned release.'})
        return run

    def retry(self, run):
        with self.db(True) as db:
            last = self._last(db, run)
            if last['status'] not in ('failed', 'blocked', 'cancelled'):
                raise ValueError('Only a stopped, unverified run can retry.')
            attempt = last['attempt'] + 1
            self._append(db, run, last['seq'] + 1, attempt, 'intake', 'queued', {'reason': 'Original manifest retained.'})
            return attempt

    @staticmethod
    def _verify_package(db, run, attempt, last, detail):
        if (last['stage'], last['status']) != ('verify', 'completed'):
            raise ValueError('No completed verification in this attempt.')
        row = db.execute('SELECT manifest_hash,manifest FROM runs WHERE id=?', (run,)).fetchone()
        manifest = json.loads(row['manifest'])
        prior = json.loads(last['detail'])
        proof = detail.get('evidence')
        required = manifest.get('required_cases')
        if (not isinstance(proof, dict) or proof.keys() != RECEIPT_KEYS or proof != prior.get('evidence')
                or proof['run'] != run or proof['attempt'] != attempt or type(proof['attempt']) is not int
                or type(proof['generation']) is not int or proof['generation'] < 1
                or digest(manifest) != row['manifest_hash'] or proof['manifest_sha256'] != row['manifest_hash']
                or type(required) is not int or required < 1):
            raise ValueError('Verification receipt does not belong to this attempt and manifest.')
        counts = (proof['matched'], proof['required'], prior.get('completed'), prior.get('required'),
                  detail.get('completed'), detail.get('required'))
        hashes = (detail.get('package_sha256'), *(proof[k] for k in RECEIPT_HASHES))
        if (any(type(x) is not int or x != required for x in counts)
                or any(not isinstance(h, str) or not SHA256.fullmatch(h) for h in hashes)):
            raise ValueError('A verified package needs full coverage and matching artifact evidence.')

    def event(self, run, attempt, stage, status, detail):
        if stage not in STAGES or status not in {'running', 'completed', *TERMINAL}:
            raise ValueError('Invalid event.')
        if status == 'verified' and (stage != 'package' or not isinstance(detail, dict)):
            raise ValueError('No verified package receipt.')
        with self.db(True) as db:
            last = self._last(db, run)
            if last['attempt'] != attempt or last['status'] in TERMINAL:
                raise ValueError('Run attempt already closed.')
            if status == 'verified':
                self._verify_package(db, run, attempt, last, detail)
            self._append(db, run, last['seq'] + 1, attempt, stage, status, detail)

    def run(self, run):
        with self.db() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
            if row is None:
                raise ValueError('Unknown candidate run.')
            result = dict(row)
            result['manifest'] = json.loads(result['manifest'])
            if digest(result['manifest']) != result['manifest_hash']:
                raise ValueError('Run manifest digest mismatch.')
            events = [dict(e) for e in db.execute('SELECT seq,attempt,at,stage,status,detail FROM events '
                                                  'WHERE run=? ORDER BY seq', (run,))]
            for e in events:
                e['detail'] = json.loads(e['detail'])
            return {**result, 'attempt': events[-1]['attempt'], 'status': events[-1]['status'], 'events': events}

    def summaries(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT r.id,e.attempt,e.status FROM runs r JOIN events e '
                        'ON e.run=r.id AND e.seq=(SELECT max(seq) FROM events WHERE run=r.id) ORDER BY r.rowid DESC')]

    def recover(self):
        # A process lock excludes a second coordinator; never resume unobserved side effects.
        for row in self.summaries():
            if row['status'] not in TERMINAL:
                last = self.run(row['id'])['events'][-1]
                self.event(row['id'], row['attempt'], last['stage'], 'blocked',
                           {'reason': 'Coordinator restarted. Retry with the original manifest.'})
