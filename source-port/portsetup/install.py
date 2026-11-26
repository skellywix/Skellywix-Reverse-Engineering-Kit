"""Install the two fixed profiles into a protected root, verify an existing root, and print approval review details.

The layout is source-port/verifier/LAYOUT.md. Everything installed is verified by the verifier's own loader before it
becomes visible, and nothing already installed is ever replaced.
"""
import json
import os
import shutil
import uuid
from pathlib import Path

from verifier import containers, screen
from verifier import profile as profiles
from verifier import run
from verifier.errors import Refused
from verifier.identity import Target, canonical, sha256, valid_attempt
from verifier.store import read_regular

SOURCE_PROFILES = Path(__file__).resolve().parents[1] / 'profiles'
DIRECTORY_MODE = 0o750  # operator:verifier; the verifier refuses group- or other-writable protected files
FILE_MODE = 0o640
WORK_MODE = 0o700
SOURCE_LIMIT = 1 << 20


def _make(path, mode=DIRECTORY_MODE):
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise Refused(f'{path.name} must be a plain directory.')


def _copy_profile(spec, stage):
    """Copy exactly the fixed profile files, never `.gitattributes` or anything else, into stage/<id>.

    The portable layout: the one authoritative expected.json is written at gate/expected.json, not at
    verifier/expected.json, and an empty gate/approval directory is created as the mount point for approvals.
    """
    source = SOURCE_PROFILES / spec.id
    target = stage / spec.id
    for name in (*profiles.files(spec), 'manifest.json'):
        data = read_regular(source / name, SOURCE_LIMIT)
        destination = target / (profiles.PORTABLE_EXPECTED if name == profiles.EXPECTED else name)
        destination.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        with destination.open('xb') as stream:
            stream.write(data)
        destination.chmod(FILE_MODE)
    (target / profiles.MOUNT_POINT).mkdir(mode=DIRECTORY_MODE)
    for directory, _subdirectories, _names in os.walk(target):
        Path(directory).chmod(DIRECTORY_MODE)


def install_registry(root, spec):
    """Install one profile. Returns 'installed', or 'present' for an identical, verified profile already there.

    An installed profile in the original layout stays valid and is left alone; it is never migrated or replaced.
    """
    registry = root / 'registry'
    _make(registry)
    destination = registry / spec.id
    if destination.exists() or destination.is_symlink():
        profiles.load(registry, spec.id)  # Refused unless it is exactly this reviewed profile; never replaced.
        return 'present' if profiles.physical_names(destination, spec)[1] == 'portable' else 'present (original layout)'
    stage = root / f'.stage-{uuid.uuid4().hex}'
    stage.mkdir(mode=DIRECTORY_MODE)
    try:
        _copy_profile(spec, stage)
        profiles.load(stage, spec.id)  # The verifier's loader, on the staged bytes, before they become visible.
        os.rename(stage / spec.id, destination)
    except OSError as error:
        raise Refused(f'Could not install {spec.id}: {error.strerror or error}') from error
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    profiles.load(registry, spec.id)
    return 'installed'


def install(root):
    """Create the protected root's directories and install both profiles. Returns a list of report lines."""
    root = Path(root)
    if root.is_symlink():
        raise Refused('The root must not be a symbolic link.')
    _make(root)
    for name in ('approvals', 'queue', 'results'):
        _make(root / name)
    lines = []
    for spec in profiles.SPECS.values():
        state = install_registry(root, spec)
        for name in ('approvals', 'queue', 'results'):
            _make(root / name / spec.id)
        lines.append(f'{spec.id}: registry {state}; approvals, queue, results ready')
    _make(root / 'work', WORK_MODE)
    return lines


def check(root):
    """Verify an installed root without writing. Returns (ok, report lines)."""
    root = Path(root)
    lines, ok = [], True
    for spec in profiles.SPECS.values():
        problems, layout = [], 'unknown'
        base = root / 'registry' / spec.id
        try:
            layout = profiles.physical_names(base, spec)[1]
            profiles.load(root / 'registry', spec.id)
        except Refused as error:
            problems.append(str(error))
        for name in ('approvals', 'queue', 'results'):
            if not (root / name / spec.id).is_dir():
                problems.append(f'{name}/{spec.id} is missing')
        mount_point = base / profiles.MOUNT_POINT
        if layout == 'portable' and (mount_point.is_symlink() or not mount_point.is_dir()):
            problems.append(f'registry/{spec.id}/{profiles.MOUNT_POINT} mount point is missing')
        approval_directory = root / 'approvals' / spec.id
        approvals = sorted(p.name for p in approval_directory.glob('*.json')) if approval_directory.is_dir() else []
        ok = ok and not problems
        note = {'portable': 'portable layout', 'original': 'original layout, no web gate projection'}.get(layout, '')
        lines.append(f'{spec.id}: ' + (f'ok ({note})' if not problems else 'PROBLEM: ' + '; '.join(problems))
                     + f'; approvals present: {len(approvals)}')
    if not (root / 'work').is_dir():
        ok = False
        lines.append('work/ is missing')
    return ok, lines


def approval_review(root, project_id, attempt_id):
    """What an operator must read before writing an approval by hand. Reads only; writes nothing, approves nothing."""
    if not valid_attempt(attempt_id):
        raise Refused('The attempt ID is not valid.')
    layout = run.Layout(Path(root))
    profile = profiles.load(layout.registry, project_id)
    candidate = run.read_candidate(layout, project_id, attempt_id)
    target = Target(project_id, attempt_id, profile.source_sha256, sha256(candidate), profile.corpus_sha256,
                    profile.recipe_sha256, profile.toolchain_sha256)
    approval = canonical({'schema_version': 1, 'target': target.as_dict()})
    path = layout.approvals / project_id / f'{attempt_id}.json'
    try:
        existing = 'matches' if read_regular(path, 4096, protected=True) == approval else 'differs'
    except Refused:
        existing = 'absent'
    blocker = containers.candidate_runtime_blocker()
    return {
        'notice': 'Review only. No approval was written and none is implied; the operator writes the file by hand.',
        'target': target.as_dict(),
        'candidate': {'bytes': len(candidate), 'sha256': target.candidate_sha256,
                      'matches_fallback_screen': screen.used_fallback(candidate),
                      'source_ascii_escaped': candidate.decode('utf-8')},
        'approval_file': f'approvals/{project_id}/{attempt_id}.json',
        'approval_file_content': approval.decode('ascii'),
        'existing_approval': existing,
        'receipt_possible_now': blocker is None,
        'runtime_blocker': blocker,
    }


def render_review(review):
    """Printable, ASCII-only text: the candidate is untrusted and may hold terminal control characters."""
    return json.dumps(review, indent=2, sort_keys=True, ensure_ascii=True)
