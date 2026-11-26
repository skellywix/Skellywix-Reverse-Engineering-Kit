"""The two compile-time-known profiles, and verification of an operator-installed registry copy against their pins.

A profile cannot be uploaded or registered through this code. An installed directory is accepted only when its
manifest.json hashes to the value pinned below and every listed file hashes to the manifest.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from . import containers, framing, screen
from .errors import Refused
from .identity import CASE, SHA256, canonical, exact_keys, parse_json, sha256
from .store import read_regular

MAX_FILE = 2 << 20
LANGUAGE = 'python'
MAX_CASE_ARGS = 8
EXPECTED = 'verifier/expected.json'  # logical name, pinned in every manifest
PORTABLE_EXPECTED = 'gate/expected.json'  # its only other permitted physical path
MOUNT_POINT = 'gate/approval'  # portable layout only: an empty directory onto which approvals/<id> is mounted


@dataclass(frozen=True)
class Spec:
    id: str
    name: str
    kind: str
    source: str


SPECS = {spec.id: spec for spec in (
    Spec('line-count', 'Line count', 'cli', 'line_count.py'),
    Spec('status-handler', 'Status handler', 'http', 'status_handler.py'),
)}

# sha256 of each profile's manifest.json. Changing any profile file means changing the pin in a reviewed commit.
PINNED_MANIFEST_SHA256 = {
    'line-count': 'b610e0c74f6a17c0dff0c456de7c5a0a8dd4c137b6d6a9d2a0b3789a813af8ca',
    'status-handler': 'ec5f7a66ab98b735d4e4bfe495a90dd65cbe91fdf84b2d9011995f67aedeeea9',
}


@dataclass(frozen=True)
class Case:
    id: str
    held_out: bool
    argv: tuple
    stdin: bytes


@dataclass(frozen=True)
class Profile:
    spec: Spec
    source: bytes
    cases: tuple
    expected: dict  # case id -> expected_sha256
    source_sha256: str
    corpus_sha256: str
    recipe_sha256: str
    toolchain_sha256: str

    def identity(self):
        return identity(self.spec, self.source_sha256, self.corpus_sha256, len(self.cases))


def files(spec):
    return ('public/profile.json', 'public/brief.md', 'public/framing.md', f'public/original/{spec.source}',
            'verifier/corpus.json', 'verifier/expected.json', 'verifier/expected_observables.json')


def recipe_sha256(spec):
    return sha256(canonical({'schema_version': 1, 'project_id': spec.id, 'kind': spec.kind,
                             'framing': framing.FRAMING[spec.kind], 'recipe': containers.recipe(),
                             'fallback_screen': screen.PATTERN}))


def toolchain_sha256():
    return sha256(canonical({'schema_version': 1, **containers.toolchain()}))


def corpus_sha256(data):
    names = ('public/framing.md', 'verifier/corpus.json', 'verifier/expected.json')
    return sha256(canonical({'schema_version': 1, 'files': {name: sha256(data[name]) for name in names}}))


def identity(spec, source_sha, corpus_sha, required):
    """The public profile.json content. The web reads it; the verifier recomputes and requires equality."""
    return {'schema_version': 1, 'id': spec.id, 'name': spec.name, 'language': LANGUAGE, 'kind': spec.kind,
            'source_sha256': source_sha, 'corpus_sha256': corpus_sha, 'recipe_sha256': recipe_sha256(spec),
            'toolchain_sha256': toolchain_sha256(), 'required_cases': required}


def parse_cases(raw, kind):
    rows = parse_json(raw, 'corpus.json')
    if not isinstance(rows, list) or not rows:
        raise Refused('corpus.json must list at least one case.')
    cases = []
    for row in rows:
        exact_keys(row, ('id', 'held_out', 'argv', 'stdin'), 'A corpus case')
        argv = row['argv']
        if not (isinstance(row['id'], str) and CASE.fullmatch(row['id'])) or type(row['held_out']) is not bool:
            raise Refused('A corpus case has a bad id or held_out flag.')
        if (not isinstance(argv, list) or len(argv) > MAX_CASE_ARGS
                or not all(isinstance(a, str) and len(a) <= 64 and '\0' not in a for a in argv)
                or (kind == 'http' and argv)):
            raise Refused(f'Case {row["id"]} has bad argv.')
        cases.append(Case(row['id'], row['held_out'], tuple(argv), framing.expand(row['stdin'])))
    ids = [case.id for case in cases]
    if ids != sorted(set(ids)):
        raise Refused('Corpus case IDs must be unique and sorted.')
    if not any(case.held_out for case in cases):
        raise Refused('The corpus needs at least one held-out case.')
    return tuple(cases)


def parse_expected(raw, cases):
    rows = parse_json(raw, 'expected.json')
    if not isinstance(rows, list) or len(rows) != len(cases):
        raise Refused('expected.json must have exactly one row per corpus case.')
    expected = {}
    for row, case in zip(rows, cases):
        exact_keys(row, ('id', 'held_out', 'expected_sha256'), 'An expected row')
        if row['id'] != case.id or row['held_out'] is not case.held_out or not framing.valid_digest(row['expected_sha256']):
            raise Refused(f'Expected row for {case.id} does not match the corpus.')
        expected[case.id] = row['expected_sha256']
    return expected


def expected_rows(kind, cases, raw):
    """Derive expected.json rows from the hand-authored observables (used by `describe` and the tests)."""
    observed = parse_json(raw, 'expected_observables.json')
    if not isinstance(observed, list) or [row.get('id') if isinstance(row, dict) else None for row in observed] != [
            case.id for case in cases]:
        raise Refused('expected_observables.json must have one row per corpus case, in order.')
    rows = []
    for row, case in zip(observed, cases):
        if row.get('held_out') is not case.held_out:
            raise Refused(f'Observable for {case.id} disagrees about held_out.')
        rows.append({'id': case.id, 'held_out': case.held_out, 'expected_sha256': framing.expected_digest(kind, row)})
    return rows


def physical_names(base, spec):
    """Map each logical profile file to its path under `base`, and name the layout.

    The pinned logical list is the same in both layouts. Only expected.json has two possible physical homes: the
    original verifier/expected.json, or the portable gate/expected.json (the one file the web is given, so that the
    web can be shown a directory holding nothing else). Both present, or either one a symlink, is refused later.
    """
    names = {name: name for name in files(spec)}
    original, portable = os.path.lexists(base / EXPECTED), os.path.lexists(base / PORTABLE_EXPECTED)
    if original and portable:
        raise Refused('The profile holds expected.json in both the original and the portable layout.')
    if portable:
        names[EXPECTED] = PORTABLE_EXPECTED
    return names, 'portable' if portable else 'original'


def _read_all(base, spec, protected, physical=None):
    """Read every logical file once from its physical path; the digests are taken over exactly these bytes."""
    physical = physical or physical_names(base, spec)[0]
    return {name: read_regular(base / path, MAX_FILE, protected=protected) for name, path in physical.items()}


def load(registry, project_id):
    """Load and fully verify an installed profile. Any drift from the compiled pins is Refused."""
    spec = SPECS.get(project_id) if isinstance(project_id, str) else None
    if spec is None:
        raise Refused('Only the compiled-in fixed profiles can run.')
    base = Path(registry) / spec.id
    manifest_bytes = read_regular(base / 'manifest.json', MAX_FILE, protected=True)
    if sha256(manifest_bytes) != PINNED_MANIFEST_SHA256[spec.id]:
        raise Refused(f'The installed {spec.id} manifest differs from the verifier pin.')
    manifest = exact_keys(parse_json(manifest_bytes, 'manifest.json'), ('schema_version', 'project_id', 'files'),
                          'manifest.json')
    listed = manifest['files']
    if manifest['schema_version'] != 1 or manifest['project_id'] != spec.id or not isinstance(listed, dict):
        raise Refused('The manifest does not describe this profile.')
    names = files(spec)
    if set(listed) != set(names) or not all(isinstance(v, str) and SHA256.fullmatch(v) for v in listed.values()):
        raise Refused('The manifest does not list exactly the fixed profile files.')
    physical, layout = physical_names(base, spec)
    present, directories = set(), set()
    for directory, subdirectories, filenames in os.walk(base):
        for subdirectory in subdirectories:
            path = os.path.join(directory, subdirectory)
            if os.path.islink(path):
                raise Refused('The installed profile contains a symbolic link.')
            directories.add(Path(path).relative_to(base).as_posix())
        present.update(Path(directory, name).relative_to(base).as_posix() for name in filenames)
    if present != set(physical.values()) | {'manifest.json'}:
        raise Refused('The installed profile has missing or unlisted files.')
    allowed = {parent.as_posix() for name in physical.values() for parent in Path(name).parents if parent.name}
    if layout == 'portable':
        allowed.add(MOUNT_POINT)  # The empty mount point for the approvals directory; any file in it is unlisted.
    if directories - allowed:
        raise Refused('The installed profile has an unlisted directory.')
    data = _read_all(base, spec, True, physical)
    for name in names:
        if sha256(data[name]) != listed[name]:
            raise Refused(f'{name} differs from its manifest digest.')
    cases = parse_cases(data['verifier/corpus.json'], spec.kind)
    expected = parse_expected(data['verifier/expected.json'], cases)
    source = data[f'public/original/{spec.source}']
    profile = Profile(spec, source, cases, expected, sha256(source), corpus_sha256(data),
                      recipe_sha256(spec), toolchain_sha256())
    if parse_json(data['public/profile.json'], 'profile.json') != profile.identity():
        raise Refused('public/profile.json differs from the recomputed identities.')
    return profile


def describe(project_id, directory):
    """Print-only helper for building a profile directory: the identities, expected rows, and manifest for its files."""
    spec = SPECS.get(project_id)
    if spec is None:
        raise Refused('Only the compiled-in fixed profiles can be described.')
    data = _read_all(Path(directory), spec, False)
    cases = parse_cases(data['verifier/corpus.json'], spec.kind)
    manifest = {'schema_version': 1, 'project_id': spec.id, 'files': {n: sha256(data[n]) for n in files(spec)}}
    return {
        'profile': identity(spec, sha256(data[f'public/original/{spec.source}']), corpus_sha256(data), len(cases)),
        'expected': expected_rows(spec.kind, cases, data['verifier/expected_observables.json']),
        'manifest': manifest,
        'manifest_sha256': sha256(canonical(manifest)),
    }
