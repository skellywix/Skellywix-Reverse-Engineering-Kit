"""One fixed-recipe verification: check approval, build the exact candidate bytes, compare, and record what was observed.

Nothing here is a command surface. The candidate is data (one Rust source file); every argv comes from containers.py.
"""
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import containers, elf, framing, screen
from . import profile as profiles
from .errors import MeasurementFailed, Refused, ResultLost, Unavailable
from .identity import Target, canonical, exact_keys, parse_json, sha256, valid_attempt
from .store import read_regular, replace_file, write_once

MAX_CANDIDATE = 256 << 10
DIAGNOSTIC_LIMIT = 2000


@dataclass(frozen=True)
class Layout:
    """See LAYOUT.md. registry/ and approvals/ are operator-owned, queue/ is web-owned, results/ and work/ verifier-only."""
    root: Path

    @property
    def registry(self):
        return self.root / 'registry'

    @property
    def approvals(self):
        return self.root / 'approvals'

    @property
    def queue(self):
        return self.root / 'queue'

    @property
    def results(self):
        return self.root / 'results'

    @property
    def work(self):
        return self.root / 'work'


def read_candidate(layout, project_id, attempt_id):
    """Read the exact candidate bytes once. Everything after this hashes and builds these bytes, never the path."""
    base = layout.queue / project_id / attempt_id
    job = exact_keys(parse_json(read_regular(base / 'job.json', 4096), 'job.json'),
                     ('schema_version', 'project_id', 'attempt_id', 'candidate_sha256'), 'job.json')
    if (type(job['schema_version']) is not int or job['schema_version'] != 1
            or job['project_id'] != project_id or job['attempt_id'] != attempt_id):
        raise Refused('The queue job does not name this project and attempt.')
    data = read_regular(base / 'candidate.rs', MAX_CANDIDATE)
    if not data or b'\0' in data:
        raise Refused('The candidate must be one nonempty Rust source file without NUL bytes.')
    try:
        data.decode('utf-8')
    except UnicodeDecodeError as error:
        raise Refused('The candidate is not UTF-8 text.') from error
    if job['candidate_sha256'] != sha256(data):
        raise Refused('The candidate bytes differ from the queued digest.')
    return data


def check_approval(layout, target):
    """The operator's approval must name the complete target for these exact bytes and this attempt."""
    raw = read_regular(layout.approvals / target.project_id / f'{target.attempt_id}.json', 4096, protected=True)
    approval = exact_keys(parse_json(raw, 'approval'), ('schema_version', 'target'), 'approval')
    if type(approval['schema_version']) is not int or approval['schema_version'] != 1:
        raise Refused('Unsupported approval schema.')
    approved = Target.from_wire(approval['target'])
    if approved != target:
        raise Refused('No operator approval names this exact target; differing fields: '
                      + ', '.join(target.differences(approved)) + '.')


def record_runtime(layout, profile, attempt_id, state, detail):
    directory = layout.results / profile.spec.id
    directory.mkdir(parents=True, exist_ok=True)
    replace_file(directory / 'runtime.json', canonical({
        'schema_version': 1, 'project_id': profile.spec.id, 'attempt_id': attempt_id,
        'recipe_sha256': profile.recipe_sha256, 'toolchain_sha256': profile.toolchain_sha256,
        'state': state, 'detail': detail[:DIAGNOSTIC_LIMIT]}))


def claim(layout, project_id, attempt_id):
    """An attempt directory exists at most once: a second run of the same attempt is refused."""
    parent = layout.results / project_id
    parent.mkdir(parents=True, exist_ok=True)
    try:
        (parent / attempt_id).mkdir(mode=0o750)
    except FileExistsError as error:
        raise Refused('This attempt already has a verifier record; a new attempt needs a new approval.') from error
    return parent / attempt_id


def freeze(data, path):
    """Materialize verified bytes once in the private work directory and confirm what will be mounted."""
    with path.open('xb') as stream:
        stream.write(data)
    path.chmod(0o444)
    if sha256(path.read_bytes()) != sha256(data):
        raise MeasurementFailed(f'{path.name} changed after it was frozen.')
    return path


def _observe(profile, case, output):
    head_only = profile.spec.kind == 'http' and case.stdin.startswith(b'HEAD ')
    return framing.observed_digest(profile.spec.kind, output.status, output.stdout, output.stderr, head_only)


def measure(profile, candidate, work, progress):
    """Baseline twice, build once, candidate once per case. Returns result rows only when every run completed."""
    original = freeze(profile.source, work / 'main.py')
    baseline = []
    for case in profile.cases:
        spec = containers.RunSpec('original', (*containers.ORIGINAL_ARGV, *case.argv),
                                  ((original, '/app/main.py'),), case.stdin)
        first = _observe(profile, case, containers.launch(spec))
        replay = _observe(profile, case, containers.launch(spec))
        if first != replay:
            raise MeasurementFailed(f'The original is unstable on case {case.id}.')
        if first != profile.expected[case.id]:
            raise MeasurementFailed(f'The original disagrees with the independent expected result on case {case.id}.')
        baseline.append((first, replay))
    source = freeze(candidate, work / 'source.rs')
    built = containers.launch(containers.RunSpec('build', containers.BUILD_ARGV, ((source, '/source.rs'),)))
    progress['build_exit'] = built.status
    if built.status:
        raise MeasurementFailed(f'Rust compilation failed (exit {built.status}).')  # Compiler text echoes candidate code; never shown.
    if not built.stdout.startswith(containers.ELF64_LE):
        raise MeasurementFailed('The compiler exited 0 but produced no little-endian ELF64 program.')
    problem = elf.static_problem(built.stdout)
    if problem:  # Before the program is written anywhere: the candidate runtime cannot start a loader-dependent program.
        raise MeasurementFailed(problem)
    binary = work / 'port'
    with binary.open('xb') as stream:
        stream.write(built.stdout)
    binary.chmod(0o555)
    program_sha256 = progress['program_sha256'] = sha256(built.stdout)
    rows, fallback = [], screen.used_fallback(candidate)  # Advisory: a hit can only add used_fallback true.
    for case, (first, replay) in zip(profile.cases, baseline):
        if sha256(binary.read_bytes()) != program_sha256:
            raise MeasurementFailed('The candidate program changed after it was built.')
        spec = containers.RunSpec('candidate', (containers.CANDIDATE_ENTRYPOINT, *case.argv),
                                  ((binary, containers.CANDIDATE_ENTRYPOINT),), case.stdin)
        rows.append({'id': case.id, 'original_sha256': first, 'replay_sha256': replay,
                     'candidate_output_sha256': _observe(profile, case, containers.launch(spec)),
                     'used_fallback': fallback})
    return rows


def _make_removable(function, path, _error):
    """Frozen files are read-only; make them removable where the host requires it, then retry once. Best effort."""
    try:
        os.chmod(path, 0o700)
        function(path)
    except OSError:
        pass  # Scratch under the verifier-private work directory; the result record does not depend on it.


def execute(layout, profile, target, candidate, attempt_dir):
    progress = {'build_exit': None}
    rows, state, detail = [], 'exercised', ''
    work = Path(tempfile.mkdtemp(prefix='attempt-', dir=layout.work)).resolve()
    try:
        rows = measure(profile, candidate, work, progress)
        matching = sum(row['candidate_output_sha256'] == profile.expected[row['id']] for row in rows)
        detail = f'{matching} of {len(rows)} cases match the expected results.'
        differing = [row['id'] for row in rows if row['candidate_output_sha256'] != profile.expected[row['id']]]
        if differing:
            detail += ' Differing: ' + ', '.join(differing) + '.'
        if any(row['used_fallback'] for row in rows):
            detail += ' The candidate source matched the fallback screen (used_fallback true); it cannot be exported.'
        detail += f' Candidate program sha256 {progress["program_sha256"]}.'
    except Unavailable as error:
        state, detail = 'unavailable', str(error)
    except MeasurementFailed as error:
        state, detail = 'failed', str(error)
    except Exception as error:  # Any other verifier fault is recorded as a failed measurement; success is never inferred.
        state, detail = 'failed', f'Verifier error {type(error).__name__}.'  # Exception text can carry host paths; keep it off the browser.
    finally:
        shutil.rmtree(work, onexc=_make_removable)
    result = {'schema_version': 1, 'target': target.as_dict(), 'build_exit': progress['build_exit'], 'cases': rows}
    write_once(attempt_dir / 'result.json', canonical(result))
    write_once(attempt_dir / 'diagnostic.json',
               canonical({'schema_version': 1, 'state': state, 'detail': detail[:DIAGNOSTIC_LIMIT]}))
    record_runtime(layout, profile, target.attempt_id, state, detail)
    return {'state': state, 'detail': detail, 'result': result}


def record_blocked(layout, profile, target, reason):
    """An explicit blocked result: no case rows, no build, no run. It is never a receipt and the gate cannot accept it."""
    attempt_dir = claim(layout, target.project_id, target.attempt_id)
    result = {'schema_version': 1, 'target': target.as_dict(), 'build_exit': None, 'cases': []}
    write_once(attempt_dir / 'result.json', canonical(result))
    write_once(attempt_dir / 'diagnostic.json',
               canonical({'schema_version': 1, 'state': 'blocked', 'detail': reason[:DIAGNOSTIC_LIMIT]}))
    record_runtime(layout, profile, target.attempt_id, 'blocked', reason)
    return {'state': 'blocked', 'detail': reason, 'result': result}


def verify(root, project_id, attempt_id):
    """Verify one queued, operator-approved attempt. Raises Refused or Unavailable before claiming the attempt."""
    layout = Layout(Path(root))
    if not valid_attempt(attempt_id):
        raise Refused('The attempt ID is not valid.')
    profile = profiles.load(layout.registry, project_id)
    candidate = read_candidate(layout, project_id, attempt_id)
    target = Target(project_id, attempt_id, profile.source_sha256, sha256(candidate), profile.corpus_sha256,
                    profile.recipe_sha256, profile.toolchain_sha256)
    check_approval(layout, target)
    blocker = containers.candidate_runtime_blocker()
    if blocker:  # Before preflight and before anything runs: the fixed environment cannot support a receipt.
        try:
            return record_blocked(layout, profile, target, blocker)
        except ResultLost:
            record_runtime(layout, profile, attempt_id, 'failed', 'The verifier could not record its result.')
            raise
    try:
        containers.preflight()
    except Unavailable as error:
        record_runtime(layout, profile, None, 'unavailable', str(error))
        raise
    attempt_dir = claim(layout, project_id, attempt_id)
    os.makedirs(layout.work, mode=0o700, exist_ok=True)
    try:
        return execute(layout, profile, target, candidate, attempt_dir)
    except ResultLost:
        record_runtime(layout, profile, attempt_id, 'failed', 'The verifier could not record its result.')
        raise
