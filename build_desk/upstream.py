"""Resolve publisher channels into concrete inputs for one transaction. Never modify the checkout."""
from __future__ import annotations

import copy
import re

from tools import dependency_candidates as dep
from . import maintenance as mt

NPM_FLAGS = ['--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org/']


def release(m, c, dest, previous):
    repo = c['repository']
    is_rea = c['family'] == 'reference-archive'
    tag, version = dep.stable_release(repo, dest, m._fetch)
    pattern = dep.REA_TAG if is_rea else dep.VERSION_TAG
    commit = dep.release_commit(repo, tag, dest, m._fetch)
    if old := previous.get('tag'):
        if old == tag and previous.get('commit') != commit:
            raise mt.MaintenanceError(f'{repo}: the published tag moved to a different commit')
        prior = pattern.fullmatch(old)
        if prior and dep.stable(version) < dep.stable(prior[1]):
            raise mt.MaintenanceError(f'{repo}: the published release is older than the installed release')
    c.update(tag=tag, commit=commit, version=tag)
    if not is_rea:
        return
    hashes = {}
    for name in dep.REA_FILES:
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        hashes[name] = m._fetch(f'https://raw.githubusercontent.com/{repo}/{commit}/{name}', path, mt.REFERENCE_FILE_LIMIT)
    package = mt._loads((dest / 'package.json').read_bytes(), 'REA package')
    if not isinstance(package, dict) or package.get('name') != 'rea-agents' or package.get('version') != version:
        raise mt.MaintenanceError('REA package does not match its release tag')
    dep.validate_npm_lock(mt._loads((dest / 'package-lock.json').read_bytes(), 'REA lock'), package)
    c['files'] = hashes


def registry_digest(m, repository, tag, cache):
    ref = f'{repository}:{tag}'
    if ref not in cache:
        description = m.docker._call(['buildx', 'imagetools', 'inspect', '--format', '{{json .Manifest}}', ref], 90)
        descriptor = mt._loads(description.encode(), 'registry manifest descriptor')
        digest = descriptor.get('digest') if isinstance(descriptor, dict) else None
        if not isinstance(digest, str) or not mt.DIGEST.fullmatch(digest):
            raise mt.MaintenanceError(f'{ref}: registry did not return a content digest')
        cache[ref] = digest
    return cache[ref]


def native_packages(m, image):
    """Resolve the recipe's apt install, including transitive packages, without installing any."""
    direct = ('g++', 'make', 'cmake')
    query = '''mkdir -p /tmp/lists/partial
apt() { apt-get -o Dir::State::lists=/tmp/lists -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= "$@"; }
apt -o APT::Update::Error-Mode=any -qq update >&2
apt --simulate --no-remove --no-install-recommends install "$@"'''
    output = m.docker.run(image, ['sh', '-ec', query, 'resolve-apt', *direct], network=True,
                          env={'LC_ALL': 'C'}, timeout=300)
    packages = dict.fromkeys(direct)
    for line in output.splitlines():
        if line.startswith('Inst '):
            match = re.fullmatch(r'Inst ([a-z0-9][a-z0-9.+:-]*) (?:\[[^\]]+\] )?\(([0-9][A-Za-z0-9.+:~-]*) [^)]*\)', line)
            if not match:
                raise mt.MaintenanceError('Unrecognized apt package resolution')
            packages[match[1]] = match[2]
    return ' '.join(f'{name}={version}' if version else name for name, version in sorted(packages.items())).encode()


def resolve_lock(m, c, dest, image):
    manifest = dest / c['manifest']
    manifest.parent.mkdir(parents=True, exist_ok=True)
    declaration = mt._lf((m.checkout / c['manifest']).read_bytes())
    if mt._sha(declaration) != c['manifest_sha256']:
        raise mt.MaintenanceError('Package declaration changed after the update started')
    manifest.write_bytes(declaration)
    m._writable(manifest.parent)
    if c['family'] == 'npm-lock':
        package = mt._loads(manifest.read_bytes(), 'npm declaration')
        for group in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
            for name, spec in package.get(group, {}).items():
                if not re.fullmatch(r'(?:@[\w.-]+/)?[\w.-]+', name) or not re.fullmatch(r'[~^]\d+\.\d+\.\d+', spec):
                    raise mt.MaintenanceError(f'{name}: expected a compatible npm registry range')
        m.docker.run(image, ['npm', 'install', '--package-lock-only', *NPM_FLAGS], network=True,
                     mounts=((manifest.parent, '/work', False),), workdir='/work', timeout=600,
                     env={'HOME': '/tmp', 'npm_config_cache': '/tmp/npm-cache', 'npm_config_update_notifier': 'false'})
        lock = mt._read_limited(dest / c['lock'], 64 << 20, 'resolved npm lock')
        mt._check_npm_lock(lock)
    else:
        # Only the Kit workspace is resolved here. Qualification profiles keep their independently signed inputs.
        if c['context'] != '.' or c['manifest'] != 'Cargo.toml' or c['lock'] != 'Cargo.lock':
            raise mt.MaintenanceError('Unknown rolling Cargo workspace')
        # The workspace declares its lib and bin targets explicitly. Resolution needs no application source.
        m.docker.run(image, ['cargo', 'generate-lockfile', '--manifest-path', '/work/Cargo.toml'], network=True,
                     mounts=((dest, '/work', False),), workdir='/work', timeout=600,
                     env={'HOME': '/tmp', 'CARGO_HOME': '/tmp/cargo', 'CARGO_TERM_COLOR': 'never'})
        lock = mt._read_limited(dest / c['lock'], 8 << 20, 'resolved Cargo lock')
        mt._check_cargo_lock(lock.decode())
    c['lock_sha256'] = mt._sha(mt._lf(lock))
    c['version'] = f"{c['family'].split('-')[0]} lock {c['lock_sha256'][:12]}"


def resolve(m, policy, plan, scratch, receipt):
    """Return the concrete manifest, per-component source directories, and already checked resolver images."""
    if not any(c.get('track') for c in policy.components):
        return policy, {}, {}
    raw = copy.deepcopy(policy.raw)
    parts = {c['name']: c for c in raw['components']}
    roots, ready, digests = {}, {}, {}
    planned = {c['name'] for c in plan}
    for original in policy.ordered():
        name = original['name']
        if not original.get('track'):
            continue
        if name not in planned:
            raise mt.MaintenanceError(f'{name}: rolling source is missing')
        m._note('Checking upstream: ' + name)
        c = parts[name]
        del c['track']
        dest = scratch / name
        dest.mkdir()
        try:
            family = c['family']
            if family in ('release-metadata', 'reference-archive'):
                release(m, c, dest, (receipt or {}).get('components', {}).get(name, {}))
                if family == 'reference-archive':
                    roots[name] = dest
            elif family == 'oci-image':
                c['digest'] = registry_digest(m, c['repository'], c['tag'], digests)
                c['version'] = f"{c['tag']} @ {c['digest'][7:19]}"
            elif family == 'oci-build':
                base = c['base']
                ref = base['repository'] + ':' + base['tag']
                digest = registry_digest(m, base['repository'], base.pop('tag'), digests)
                base['digest'] = digest
                recipe = mt._lf((m.checkout / c['recipe']).read_bytes())
                spec = ('FROM ' + ref).encode()
                if recipe.splitlines().count(spec) != 1 or mt._sha(recipe) != c['recipe_sha256']:
                    raise mt.MaintenanceError('The local recipe no longer matches its source channel')
                compiler = parts['compiler']
                if compiler['repository'] != base['repository'] or compiler['digest'] != digest:
                    raise mt.MaintenanceError('Native recipe must use the resolved compiler base')
                if 'compiler' not in ready:
                    ready['compiler'] = m._install_image(compiler, {})
                marker = b'--no-install-recommends g++ make cmake '
                if recipe.count(marker) != 1:
                    raise mt.MaintenanceError('Native recipe does not match its apt declaration')
                packages = native_packages(m, ready['compiler']['image_id'])
                recipe = recipe.replace(marker, b'--no-install-recommends ' + packages + b' ', 1)
                recipe = recipe.replace(spec + b'\n', ('FROM ' + base['repository'] + '@' + digest + '\n').encode(), 1)
                path = dest / c['recipe']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(recipe)
                roots[name] = dest
                c['recipe_sha256'] = mt._sha(recipe)
                c['version'] = f"{ref} @ {digest[7:19]}"
            elif family in ('cargo-lock', 'npm-lock'):
                tool = c['image']
                if tool not in ready:
                    ready[tool] = m._install_image(parts[tool], {})
                resolve_lock(m, c, dest, ready[tool]['image_id'])
                roots[name] = dest
            else:
                raise mt.MaintenanceError(f'{name} does not support a rolling source')
        except (ValueError, TypeError, KeyError, OSError, mt.MaintenanceError) as exc:
            detail = str(exc) if isinstance(exc, mt.MaintenanceError) else 'invalid or unavailable publisher data'
            raise mt.MaintenanceError(f'{name}: {detail}') from exc
    data = mt._canonical(raw)
    return mt.validate_manifest(raw, data), roots, ready
