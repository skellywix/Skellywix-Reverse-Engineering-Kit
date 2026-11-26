"""Coordinator for a single, hash-locked public-domain compatibility profile."""
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from . import builder, contract, profile, releases
from .sandbox import Box, Cancelled, HERE, PYTHON, RUST, Unavailable, check_reference, file_hash, freeze
from .store import Store, digest, wire

FILES = ('contract.py', 'examples.py', 'profile.py', 'sandbox.py', 'mcp.py', 'pipeline.py', 'desk.py', 'broker.py',
         'channel.py', 'store.py', 'releases.py', 'model_turn.ts', 'builder.py')
ROUNDS = 3


def put(path, obj):
    path.write_text(wire(obj), encoding='ascii')


def record_rows(path, rows):
    with path.open('x', encoding='ascii', newline='\n') as sink:
        for row in rows:
            sink.write(wire(row) + '\n')


def compact_facts(rows):
    facts = []
    for row in rows:
        name = row['id']
        if name.startswith('pad/') or (name.startswith('args/') and name.endswith('/TQ')):
            facts.append([name, row['exit'], row['stdout']['preview'], row['stderr']['preview'],
                          {k: v['preview'] if v and v['kind'] == 'file' else None for k, v in row['files'].items()}])
    return facts


def source_text(reply):
    src = reply.strip()
    if src.startswith('```rust\n') and src.endswith('\n```'):
        src = src[8:-4]
    if 'fn main(' not in src or src.startswith('```') or '\x00' in src:
        raise Unavailable('The model did not return a Rust program. No automatic resubmission.')
    return src + '\n'


class Desk:
    def __init__(self, root, broker):
        self.code = self.disk_code()
        self.root = root
        self.original = root / 'reference'
        check_reference(self.original)
        self.store = Store(root / 'ledger.sqlite')
        self.broker = broker
        self.run_lock, self.update_lock = threading.Lock(), threading.Lock()
        self.current = None
        self.update_error = None
        for name in ('snapshots', 'runs'):
            (root / name).mkdir(mode=0o700, exist_ok=True)
        self.store.recover()
        for summary in self.store.summaries():
            self.view(summary['id'])

    def pins(self):
        return {'reference': contract.REFERENCE_SHA256, 'profile': 'Fourmilab Base64 1.5, Linux amd64, public domain',
                'required_cases': len(contract.case_ids()), 'case_ids_sha256': digest(contract.case_ids()),
                'images': {'observer': PYTHON, 'rustc': RUST}, 'runtime': 'runsc, network=none, LC_ALL=C',
                'roles': digest(profile.CATALOG), 'builder_skill': digest(profile.SKILL), 'generation_limit': ROUNDS,
                'requested_model': self.broker.model, 'requested_effort': builder.EFFORT, 'omp': self.broker.version,
                'builder_connection_sha256': self.broker.connection_hash, **self.code}

    @staticmethod
    def disk_code():
        return {'coordinator_files': {name: file_hash(HERE / name) for name in FILES},
                'dependency_reader': file_hash(HERE.parent / 'tools' / 'dependency_candidates.py')}

    def check_code(self):
        if self.disk_code() != self.code:
            raise Unavailable('Coordinator files changed. Restart the candidate before starting a run.')

    def snapshot(self, key):
        info = self.store.release(key)
        directory = self.root / 'snapshots' / key
        releases.check_snapshot(directory, info)
        return directory, info

    def update(self, action, key=None):
        if not self.update_lock.acquire(blocking=False):
            raise ValueError('An update is already in progress.')
        try:
            if action == 'stage':
                with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                    tmp = Path(tmp)
                    dest = tmp / 'snapshot'
                    dest.mkdir()
                    receipt = releases.fetch_snapshot(dest)
                    probe = tmp / 'inspect.py'
                    shutil.copyfile(dest / releases.INSPECTOR, probe)
                    probe.chmod(0o444)
                    Box(threading.Event()).inspect(self.original, probe)
                    final = self.root / 'snapshots' / digest(receipt)
                    if final.exists():
                        releases.check_snapshot(final, receipt)
                    else:
                        os.rename(dest, final)
                    self.store.stage(receipt)
            elif action == 'promote':
                self.snapshot(key)
                self.store.promote(key)
            elif action == 'rollback':
                previous = self.store.releases()['previous']
                if not previous:
                    raise ValueError('No previous release to restore.')
                self.snapshot(previous)
                self.store.rollback()
            else:
                raise ValueError('Unknown release action.')
            self.update_error = None
        except Exception as exc:
            self.update_error = f'Update not applied: {type(exc).__name__}: {str(exc)[:320]}'
            raise ValueError(self.update_error) from exc
        finally:
            self.update_lock.release()

    def view(self, run):
        data = self.store.run(run)
        view = self.root / 'runs' / run / 'view'
        view.mkdir(parents=True, exist_ok=True)
        view.chmod(0o755)
        tmp = view / 'next'
        put(tmp, {'run': data, 'catalog': profile.CATALOG})
        tmp.chmod(0o444)
        tmp.replace(view / 'run.json')
        return data

    def start(self, retry=None):
        with self.run_lock:
            if self.current:
                raise ValueError('Only one run executes at a time.')
            try:
                self.check_code()
            except (OSError, Unavailable) as exc:
                raise ValueError(str(exc)) from exc
            if retry is None:
                run, attempt = self.store.start(self.pins()), 1
            else:
                run, attempt = retry, self.store.retry(retry)
            stop = threading.Event()
            self.current = (run, stop)
            try:
                self.view(run)
                threading.Thread(target=self.execute, args=(run, attempt, stop), daemon=True).start()
            except Exception as exc:
                self.current = None
                reason = f'Run could not start ({type(exc).__name__}).'
                self.store.event(run, attempt, 'intake', 'blocked', {'reason': reason})
                try:
                    self.view(run)
                except OSError as projection:
                    raise ValueError(reason + ' The worker view is also unavailable; see the operator ledger.') from projection
                raise ValueError(reason) from exc
            return run

    def cancel(self, run):
        with self.run_lock:
            if not self.current or self.current[0] != run:
                raise ValueError('That run is not executing.')
            self.current[1].set()

    def execute(self, run, attempt, stop):
        stage = 'intake'
        def event(status, **detail):
            if stop.is_set() and status != 'cancelled':
                raise Cancelled('Stopped by operator; no verified package.')
            self.store.event(run, attempt, stage, status, detail)
            self.view(run)
        try:
            self.check_code()
            job = self.store.run(run)
            manifest = job['manifest']
            if manifest != {**self.pins(), 'release': manifest['release']}:
                raise Unavailable('Pinned worker, configuration or verifier differs. Restore the original versions to retry.')
            snap, release = self.snapshot(manifest['release'])
            work = self.root / 'runs' / run / str(attempt)
            work.mkdir()
            pins = manifest['coordinator_files']
            checker, script = work / 'contract.py', work / 'examples.py'
            freeze(HERE / 'contract.py', checker, pins['contract.py'])
            freeze(HERE / 'examples.py', script, pins['examples.py'])

            def trusted(path):
                if file_hash(path) != pins[path.name]:
                    raise Unavailable(f'{path.name} no longer matches the attempt manifest.')
                return path

            ids = contract.case_ids()
            event('running', worker='File analyst', skill='Pinned ELF identification', tool='inspect_file.py',
                  deterministic=True, image=PYTHON, evidence=release['files'][releases.INSPECTOR])
            inspector = work / 'inspect.py'
            freeze(snap / releases.INSPECTOR, inspector, release['files'][releases.INSPECTOR])
            box = Box(stop)
            identified = box.inspect(self.original, inspector)
            put(work / 'intake.json', identified)
            put(work / 'manifest.json', manifest)
            put(work / 'release.json', release)
            put(work / 'required.json', ids)
            event('completed', worker='File analyst', deterministic=True, evidence=identified)
            stage = 'analyze'
            def progress(n):
                event('running', worker='Behavior analyst' if stage == 'analyze' else 'Verifier', deterministic=True,
                      completed=n, required=len(ids), image=PYTHON, tool='contract.py')
            progress(0)
            examples = box.examples(self.original, trusted(script))
            reference = box.observe(self.original, original=True, progress=progress, checker=trusted(checker))
            contract.match(ids, reference, reference)
            pipe = next(r for r in reference if r['id'] == contract.LIVE_PIPE)
            if (pipe['exit'], pipe['stdout']['preview'], pipe['stderr']['len']) != (0, '4d', 0):
                raise Unavailable('Reference failed the predeclared live-pipe outcome.')
            record_rows(work / 'reference.jsonl', reference)
            put(work / 'examples.json', examples)
            event('completed', worker='Behavior analyst', deterministic=True, completed=len(ids), required=len(ids),
                  skill='Fourmilab CLI observation', evidence=file_hash(work / 'reference.jsonl'))
            specification = profile.SKILL + '\nFresh CLI examples (output strings encode bytes as Latin-1):\n' + wire(examples) + '\nObserved option/padding rows [id,exit,outHex,errHex,fileHex]:\n' + wire(compact_facts(reference))
            feedback = ''
            for generation in range(1, ROUNDS + 1):
                stage = 'rebuild'
                build = work / str(generation)
                build.mkdir()
                event('running', worker='Rust builder', skill='Fourmilab Base64 compatibility', tool='tool-free OMP turn',
                      generation=generation, requested_model=manifest['requested_model'], requested_effort=builder.EFFORT)
                result = self.broker.generate(specification + feedback, stop)
                src = build / 'main.rs'
                src.write_text(source_text(result['source']), encoding='utf-8', newline='\n')
                src.chmod(0o444)
                put(build / 'model.json', {k: v for k, v in result.items() if k != 'source'})
                event('completed', worker='Rust builder', actual_model=result['actual_model'], generation=generation,
                      source_sha256=file_hash(src), evidence=result['id'])
                exe = build / 'base64'
                event('running', worker='Compiler', image=RUST, tool='rustc', deterministic=True, generation=generation)
                try:
                    box.compile(src, exe)
                except ValueError as exc:
                    if generation == ROUNDS:
                        raise
                    feedback = '\nPrevious source in this run:\n' + src.read_text(encoding='utf-8') + '\nCompiler diagnostic:\n' + str(exc)
                    event('completed', worker='Compiler', deterministic=True, generation=generation, reason=str(exc), outcome='rejected')
                    continue
                artifact = file_hash(exe)
                event('completed', worker='Compiler', deterministic=True, artifact_sha256=artifact, generation=generation)
                stage = 'verify'
                event('running', worker='Verifier', skill='Named-case comparison', deterministic=True, image=PYTHON,
                      completed=0, required=len(ids), artifact_sha256=artifact, generation=generation)
                actual = box.observe(exe, original=False, progress=progress, checker=trusted(checker))
                record_rows(build / 'candidate.jsonl', actual)
                try:
                    passed = contract.match(ids, reference, actual)
                except ValueError:
                    by_id = {row['id']: row for row in actual}
                    differences = [(r['id'], r, by_id.get(r['id'])) for r in reference if r != by_id.get(r['id'])]
                    if not differences:
                        raise
                    rejection = [r[0] for r in differences]
                    put(build / 'mismatches.json', rejection)
                    reason = f'{len(rejection)} case(s) differed: ' + ', '.join(rejection[:8])
                    if generation == ROUNDS:
                        raise ValueError(reason) from None
                    feedback = '\nPrevious source in this run:\n' + src.read_text(encoding='utf-8') + '\nFirst differing [id, expected, actual] observations:\n' + wire(differences[:8])
                    event('completed', worker='Verifier', deterministic=True, outcome='rejected', reason=reason,
                          artifact_sha256=artifact, generation=generation, matched=len(ids)-len(rejection), required=len(ids))
                    continue
                if file_hash(exe) != artifact:
                    raise ValueError('Artifact changed during verification.')
                result_receipt = {'run': run, 'attempt': attempt, 'generation': generation, 'manifest_sha256': job['manifest_hash'],
                                  'required': len(ids), 'matched': passed, 'artifact_sha256': artifact,
                                  'source_sha256': file_hash(src), 'reference_rows_sha256': file_hash(work / 'reference.jsonl'),
                                  'candidate_rows_sha256': file_hash(build / 'candidate.jsonl')}
                event('completed', worker='Verifier', deterministic=True, completed=passed, required=len(ids), evidence=result_receipt)
                stage = 'package'
                put(build / 'receipt.json', result_receipt)
                target = work / 'verified.zip'
                with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
                    for name in ('manifest.json','release.json','required.json','intake.json','reference.jsonl','examples.json'):
                        archive.write(work / name, 'evidence/' + name)
                    for name in ('receipt.json','candidate.jsonl','model.json'):
                        archive.write(build / name, 'evidence/' + name)
                    archive.write(src, 'src/main.rs')
                    archive.write(exe, 'bin/base64')
                    self.check_code()
                    archive.write(trusted(checker), 'evidence/contract.py')
                    archive.writestr('Cargo.toml', '[package]\nname="base64"\nversion="1.5.0"\nedition="2021"\n')
                event('verified', worker='Packager', deterministic=True, completed=passed, required=len(ids),
                      package_sha256=file_hash(target), evidence=result_receipt)
                return
        except Exception as exc:
            status = 'cancelled' if isinstance(exc, Cancelled) or stop.is_set() else 'blocked' if isinstance(exc, (OSError, Unavailable, releases.SnapshotError)) else 'failed'
            self.store.event(run, attempt, stage, status, {'reason': str(exc)[:1600] or type(exc).__name__})
            self.view(run)
        finally:
            try:
                self.broker.finish()
            finally:
                with self.run_lock:
                    self.current = None

    def package(self, run):
        job = self.store.run(run)
        if job['status'] != 'verified':
            raise ValueError('No verified package for this run.')
        path = self.root / 'runs' / run / str(job['attempt']) / 'verified.zip'
        if file_hash(path) != job['events'][-1]['detail']['package_sha256']:
            raise ValueError('Package digest mismatch.')
        return path
