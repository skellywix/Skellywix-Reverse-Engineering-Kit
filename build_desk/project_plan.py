"""Decide, from the validated file list alone, how a project is built or packaged and which files take part.

Nothing is executed. Manifests are parsed (TOML) only to refuse projects that cannot build offline, to read a name,
and to describe an entry point. Every decision is derived from the project's own files, never from a title or hash.
"""
import posixpath
import re
import tomllib
from dataclasses import dataclass

from .project_archive import project_name

RUST = 'rust'
NATIVE = 'c-cpp'
PYTHON = 'python-app'
SITE = 'static-site'

MAX_MANIFEST = 1 << 20
SOURCE_SUFFIXES = ('.c', '.cc', '.cpp', '.cxx')
MAKEFILES = ('Makefile', 'makefile', 'GNUmakefile')
CARGO_CONFIG_ALLOWED = ('source', 'net', 'term')
SITE_DIR_PREFERENCE = ('dist', 'build', 'public', 'docs', 'site', '_site', 'www', 'html')
SITE_KEEP_HIDDEN = ('.well-known',)
CPP_HINT = re.compile(
    r'\bstd::|#\s*include\s*<(?:iostream|vector|string|map|set|memory|algorithm|cstdio|cstdlib|cmath|cstring|array)>'
    r'|\bnamespace\b|\bclass\s+\w+|\btemplate\s*<|\bnullptr\b|\busing\s+namespace')


@dataclass(frozen=True)
class Plan:
    profile: str
    mode: str                 # rustc | cargo | cmake | make | configure | direct | python | site
    name: str                 # safe artifact stem
    files: tuple              # ((source member, destination inside the payload), ...)
    notes: tuple = ()         # facts the user is told (ignored files, caveats)
    info: tuple = ()          # ((key, value), ...) JSON-safe description for PACKAGE-INFO.json


def looks_cpp(text):
    return bool(CPP_HINT.search(text))


def _hidden(name, keep=()):
    return any(p.startswith('.') and p not in keep for p in name.split('/'))


def _toml(tree, name, what):
    entry = tree.get(name)
    if entry is None:
        return None
    try:
        return tomllib.loads(tree.read(entry, MAX_MANIFEST).decode('utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f'{what} is not valid TOML.') from error


def _names(tree):
    return [e.name for e in tree.entries()]


def _skipped_note(count, what):
    return f'{count} {what} left out.' if count else None


def plan_rust(tree):
    names = _names(tree)
    kept, dropped = [], 0
    for name in names:
        if name.startswith(('.git/', 'target/')):
            dropped += 1
        else:
            kept.append(name)
    notes = [n for n in (_skipped_note(dropped, 'version-control or previous build file(s)'),) if n]
    if tree.has('Cargo.toml'):
        manifest = _toml(tree, 'Cargo.toml', 'Cargo.toml')
        if 'package' not in manifest and 'workspace' not in manifest:
            raise ValueError('Cargo.toml has neither a [package] nor a [workspace] section.')
        _check_vendoring(tree, names)
        if tree.has('rust-toolchain') or tree.has('rust-toolchain.toml'):
            notes.append('The rust-toolchain file was ignored: the build uses the sandbox toolchain.')
        package = manifest.get('package')
        stem = project_name(package.get('name') if isinstance(package, dict) and isinstance(package.get('name'), str)
                            else tree.prefix, 'project')
        locked = tree.has('Cargo.lock')
        return Plan(RUST, 'cargo', stem, tuple((n, n) for n in kept), tuple(notes), (('locked', locked),))
    sources = [n for n in kept if n.endswith('.rs')]
    if len(sources) == 1:
        return Plan(RUST, 'rustc', project_name(tree.prefix, 'project'), ((sources[0], 'main.rs'),), tuple(notes),
                    (('locked', False),))
    if not sources:
        raise ValueError('No Rust source was found: expected a Cargo.toml project or a single .rs file.')
    raise ValueError('This project has several .rs files but no Cargo.toml at its top level. Add a Cargo.toml.')


def _check_vendoring(tree, names):
    config = None
    for candidate in ('.cargo/config.toml', '.cargo/config'):
        if tree.has(candidate):
            config = _toml(tree, candidate, candidate)
            break
    replaced = False
    if config is not None:
        extra = sorted(set(config) - set(CARGO_CONFIG_ALLOWED))
        if extra:
            raise ValueError(f'The Cargo config may only configure vendored sources; it also sets: {", ".join(extra)}.')
        sources = config.get('source', {})
        if not isinstance(sources, dict):
            raise ValueError('The Cargo config [source] section is malformed.')
        for key, table in sources.items():
            if not isinstance(table, dict):
                raise ValueError('The Cargo config [source] section is malformed.')
            replaced = replaced or 'replace-with' in table
            for field in ('directory', 'local-registry'):
                if field in table:
                    path = table[field]
                    clean = posixpath.normpath(path) if isinstance(path, str) else ''
                    if (not clean or clean.startswith(('/', '..')) or '\\' in clean or re.match(r'^[A-Za-z]:', clean)
                            or clean == '.'):
                        raise ValueError(f'Source "{key}" must name a vendor directory inside the project.')
                    if not any(n.startswith(clean + '/') for n in names):
                        raise ValueError(f'The vendor directory "{clean}" named by the Cargo config is not in the upload.')
    lock = tree.get('Cargo.lock')
    if lock is not None:
        text = tree.read(lock, 64 << 20).decode('utf-8', 'replace')
        if not replaced and re.search(r'^source = "(registry|sparse|git)\+', text, re.MULTILINE):
            raise ValueError('The Cargo.lock needs crates from a registry or git, and no vendored sources are '
                             'configured. Run `cargo vendor`, then include vendor/ and .cargo/config.toml.')


def plan_native(tree):
    names = _names(tree)
    kept = [n for n in names if not n.startswith('.git/')]
    notes = [n for n in (_skipped_note(len(names) - len(kept), 'version-control file(s)'),) if n]
    stem = project_name(tree.prefix, 'project')
    stage = tuple((n, n) for n in kept)
    if tree.has('CMakeLists.txt'):
        return Plan(NATIVE, 'cmake', stem, stage, tuple(notes), (('build', 'cmake'),))
    for make in MAKEFILES:
        if tree.has(make):
            return Plan(NATIVE, 'make', stem, stage, tuple(notes), (('build', 'make'),))
    if tree.has('configure'):
        return Plan(NATIVE, 'configure', stem, stage, tuple(notes), (('build', 'configure + make'),))
    if tree.has('configure.ac') or tree.has('Makefile.am'):
        raise ValueError('This is an unconfigured autotools tree (configure.ac without configure). Upload a release '
                         'tarball or run autoreconf first.')
    loose = [n for n in kept if posixpath.dirname(n) in ('', 'src') and n.endswith(SOURCE_SUFFIXES)]
    if loose:
        return Plan(NATIVE, 'direct', stem, stage, tuple(notes), (('build', 'direct compile'),))
    raise ValueError('No CMakeLists.txt, Makefile or configure script was found, and there are no C or C++ source '
                     'files at the top level or in src/.')


def plan_python(tree):
    names = _names(tree)
    kept = [n for n in names if not _hidden(n) and '__pycache__' not in n.split('/') and not n.endswith(('.pyc', '.pyo'))]
    if not any(n.endswith('.py') for n in kept):
        raise ValueError('No Python source (.py) files were found.')
    notes = [n for n in (_skipped_note(len(names) - len(kept), 'hidden, cache or bytecode file(s)'),) if n]
    project = None
    pyproject = _toml(tree, 'pyproject.toml', 'pyproject.toml')
    if pyproject is not None and isinstance(pyproject.get('project'), dict):
        project = pyproject['project']
    stem = project_name(project.get('name') if project and isinstance(project.get('name'), str) else tree.prefix, 'app')
    entry = [n for n in ('__main__.py', 'main.py', 'app.py') if n in kept]
    scripts = sorted(project['scripts']) if project and isinstance(project.get('scripts'), dict) else []
    info = (('entry_files', entry), ('console_scripts', scripts),
            ('requirements_file', 'requirements.txt' in kept),
            ('dependencies_included', False))
    notes.append('Dependencies are not installed or bundled; the package is the project files only.')
    return Plan(PYTHON, 'python', stem, tuple((n, n) for n in kept), tuple(notes), info)


def plan_site(tree):
    names = _names(tree)
    visible = [n for n in names if not _hidden(n, SITE_KEEP_HIDDEN) and 'node_modules' not in n.split('/')]
    roots = sorted({posixpath.dirname(n) for n in visible if posixpath.basename(n) == 'index.html'},
                   key=lambda d: (d.count('/') + 1 if d else 0, d))
    if not roots:
        raise ValueError('No index.html was found: a static site needs one at its top level or in a build folder.')
    shallow = [r for r in roots if (r.count('/') + 1 if r else 0) == (roots[0].count('/') + 1 if roots[0] else 0)]
    if len(shallow) == 1:
        root = shallow[0]
    else:
        ranked = [r for r in shallow if posixpath.basename(r) in SITE_DIR_PREFERENCE]
        if len(ranked) != 1:
            raise ValueError('Several folders contain an index.html; upload the one site folder you want packaged.')
        root = ranked[0]
    cut = len(root) + 1 if root else 0
    files = tuple((n, n[cut:]) for n in visible if not root or n.startswith(root + '/'))
    notes = [n for n in (_skipped_note(len(names) - len(visible), 'hidden or node_modules file(s)'),
                         _skipped_note(len(visible) - len(files), 'file(s) outside the site folder')) if n]
    if tree.has('package.json'):
        notes.append('package.json is present: the files are packaged as found; no npm build was run.')
    stem = project_name(tree.prefix or posixpath.basename(root), 'site')
    return Plan(SITE, 'site', stem, files, tuple(notes), (('site_root', root or '.'),))


PLANNERS = {RUST: plan_rust, NATIVE: plan_native, PYTHON: plan_python, SITE: plan_site}
