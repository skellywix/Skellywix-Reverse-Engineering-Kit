"""Boundary cases for the generic project build backend (build_desk.project_jobs and its helper modules).

Nothing here needs Docker: `FakeRunner` stands in for the container runner and records exactly what the backend would
send to the sandbox (the stdin tar) so the tests can check inputs, the docker argv (via the real DockerRunner.argv),
failure reporting, packaging and persistence. One optional test drives the real sandbox when
PROJECT_JOBS_COMPILER_IMAGE names a pinned image ID.

Run:  python -m unittest tests.test_project_jobs -v      (from the repository root)
"""
import base64
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from build_desk import project_archive as pa
from build_desk import project_pack as pk
from build_desk import project_scripts as ps
from build_desk.project_container import DockerRunner, Result, Spec
from build_desk.project_jobs import PROFILE_ROWS, ProjectJobs

COMPILER = 'sha256:' + 'a' * 64
NATIVE_COMPILER = 'sha256:' + 'd' * 64
PYTHON = 'sha256:' + 'b' * 64
ELF = b'\x7fELF\x02\x01\x01' + b'\x00' * 9 + b'\x02\x00' + b'\x00' * 64
STATUS_KEYS = {'id', 'profile', 'state', 'message', 'events', 'artifact', 'sha256', 'input_sha256',
               'project_id', 'project_name'}
HEX32 = set('0123456789abcdef')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def zip_bytes(files, prefix='', dirs=(), compress=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name in dirs:
            info = zipfile.ZipInfo(prefix + name + '/', (2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o40755 << 16) | 0x10
            archive.writestr(info, b'')
        for name, data in files.items():
            info = zipfile.ZipInfo(prefix + name, (2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = compress
            archive.writestr(info, data)
    return buffer.getvalue()


def raw_zip(members):
    """members: (filename, data, external_attr_high16, flag_bits). The filename is set after construction so Windows
    does not rewrite separators."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data, attr, flags in members:
                info = zipfile.ZipInfo('placeholder', (2020, 1, 1, 0, 0, 0))
                info.filename = name
                info.create_system = 3
                info.external_attr = attr << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                info.flag_bits |= flags
                archive.writestr(info, data)
    return buffer.getvalue()


def tar_bytes(members, gz=False):
    """members: (name, type, data, linkname, mode)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz' if gz else 'w') as archive:
        for name, kind, data, link, mode in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = mode
            info.linkname = link
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return buffer.getvalue()


def reg(name, data=b'x', mode=0o644):
    return (name, tarfile.REGTYPE, data, '', mode)


def output_tar(files=None, extra=()):
    files = {'bin/hello': ELF} if files is None else files
    members = [reg(name, data, 0o755) for name, data in files.items()]
    members.append(reg('toolchain.txt', b'arch: x86_64\nrustc: rustc 1.90.0 (fake)\n'))
    members.extend(extra)
    return tar_bytes(members)


def staged_files(blob):
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        return {m.name: (archive.extractfile(m).read(), m.mode) for m in archive if m.isreg()}


class FakeRunner:
    runtime = 'runsc'

    def __init__(self):
        self.calls = []
        self.discarded = []
        self.preflight_error = None
        self.responder = lambda spec, staged: Result(0, output_tar(), b'', False)
        self.gate = None
        self.error = None

    def preflight(self, image):
        if self.preflight_error:
            raise RuntimeError(self.preflight_error)

    def discard(self, name):
        self.discarded.append(name)

    def run(self, spec, stop):
        sink = io.BytesIO()
        try:
            spec.feed(sink)
        except ValueError as error:
            return Result(None, b'', b'', feed_error=str(error))
        staged = staged_files(sink.getvalue())
        self.calls.append((spec, staged))
        if self.error:
            raise self.error
        if self.gate is not None:
            until = time.monotonic() + 10
            while not self.gate.is_set():
                if stop.is_set():
                    return Result(None, b'', b'', stopped=True)
                if time.monotonic() > until:
                    break
                time.sleep(0.01)
        return self.responder(spec, staged)


def images(key):
    return {'compiler': COMPILER, 'native-compiler': NATIVE_COMPILER, 'python': PYTHON}[key]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='projjobs-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runner = FakeRunner()
        self.jobs = self.make()

    def make(self, **kwargs):
        jobs = ProjectJobs(self.tmp / 'state', kwargs.pop('image_for', images), runner=kwargs.pop('runner', self.runner),
                           **kwargs)
        self.addCleanup(jobs.close)
        return jobs

    def run_to_end(self, profile, data, jobs=None):
        jobs = jobs or self.jobs
        run = jobs.start(profile, data)
        self.assertTrue(jobs.wait(30))
        return run, jobs.status(run)

    def built(self, profile, data, jobs=None):
        run, status = self.run_to_end(profile, data, jobs)
        self.assertEqual(status['state'], 'built', status)
        return run, status

    def failed(self, profile, data):
        run, status = self.run_to_end(profile, data)
        self.assertEqual(status['state'], 'failed', status)
        self.assertIsNone(status['artifact'])
        self.assertIsNone(status['sha256'])
        with self.assertRaises(RuntimeError):
            self.jobs.package(run)
        return status

    def members(self, run):
        path, digest = self.jobs.package(run)
        with zipfile.ZipFile(path) as archive:
            return {n: archive.read(n) for n in archive.namelist()}, path, digest

    def no_runs(self):
        self.assertEqual(list((self.tmp / 'state' / 'runs').iterdir()), [])


SITE_2048 = {
    'index.html': b'<!DOCTYPE html><title>2048</title><script src="js/game_manager.js"></script>',
    'style/main.css': b'body{margin:0}',
    'style/fonts/clear-sans.woff': b'wOFF' + b'\x00' * 32,
    'js/game_manager.js': b'function GameManager(){}',
    'meta/apple-touch-icon.png': b'\x89PNG' + b'\x00' * 16,
    'favicon.ico': b'\x00\x00\x01\x00',
    'README.md': b'# 2048',
    'LICENSE.txt': b'MIT',
    'Rakefile': b'task :default',
    '.gitignore': b'*.swp',
    '.jshintrc': b'{}',
    '.travis.yml': b'language: node_js',
}


class ProfileListTest(Base):
    def test_every_profile_is_listed_with_exact_keys(self):
        rows = self.jobs.profiles()
        self.assertEqual([r['id'] for r in rows], ['rust', 'c-cpp', 'python-app', 'static-site'])
        self.assertEqual(set(PROFILE_ROWS), {r['id'] for r in rows})
        for row in rows:
            self.assertEqual(set(row), {'id', 'label', 'description', 'available', 'reason'})
            self.assertTrue(row['available'], row)
            self.assertEqual(row['reason'], '')

    def test_missing_compiler_image_is_unavailable_not_silently_absent(self):
        for missing, affected, unaffected in (('compiler', 'rust', 'c-cpp'), ('native-compiler', 'c-cpp', 'rust')):
            with self.subTest(missing=missing):
                def supplier(key):
                    if key == missing:
                        raise RuntimeError('image not loaded')
                    return images(key)

                jobs = self.make(image_for=supplier)
                rows = {r['id']: r for r in jobs.profiles()}
                self.assertFalse(rows[affected]['available'])
                self.assertIn(missing, rows[affected]['reason'])
                self.assertTrue(all(rows[p]['available'] for p in (unaffected, 'static-site', 'python-app')))
                with self.assertRaises(RuntimeError):
                    jobs.start(affected, b'fn main() {}' if affected == 'rust' else b'int main(void) {return 0;}')
        self.no_runs()

    def test_unpinned_or_malformed_image_is_unavailable(self):
        for value in ('rust:bookworm', '', None, 'sha256:' + 'A' * 64, 'sha256:abc'):
            jobs = self.make(image_for=lambda key, v=value: v)
            row = {r['id']: r for r in jobs.profiles()}['rust']
            self.assertFalse(row['available'], value)
            self.assertIn('pinned', row['reason'])

    def test_runner_preflight_failure_is_the_reason(self):
        self.runner.preflight_error = 'The runsc container runtime is not registered with Docker.'
        rows = {r['id']: r for r in self.jobs.profiles()}
        self.assertFalse(rows['c-cpp']['available'])
        self.assertIn('runsc', rows['c-cpp']['reason'])
        self.assertTrue(rows['python-app']['available'])
        self.assertIn('syntax check', rows['python-app']['reason'])
        self.assertTrue(rows['static-site']['available'])


class RequestValidationTest(Base):
    def test_bad_profile_or_data_is_value_error(self):
        for profile in ('neverball', '', None, 7, 'RUST'):
            with self.assertRaises(ValueError):
                self.jobs.start(profile, b'x')
        for data in (b'', None, 'text', 12):
            with self.assertRaises(ValueError):
                self.jobs.start('static-site', data)
        self.no_runs()

    def test_run_ids_are_validated_before_any_path_is_built(self):
        for bad in ('', 'x', 'A' * 32, 'g' * 32, '../' * 11 + 'ab', '0' * 31, '0' * 33, None, 5, ('a' * 32,)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.jobs.status(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.jobs.package(bad)
        with self.assertRaises(ValueError):
            self.jobs.status('0' * 32)
        with self.assertRaises(ValueError):
            self.jobs.package('0' * 32)


class StaticSiteTest(Base):
    def test_upstream_2048_shaped_github_zip_packages_as_data(self):
        data = zip_bytes(SITE_2048, prefix='2048-master/', dirs=('', 'js', 'style', 'style/fonts', 'meta'))
        run, status = self.built('static-site', data)
        self.assertEqual(set(status), STATUS_KEYS)
        self.assertEqual(len(run), 32)
        self.assertTrue(set(run) <= HEX32)
        self.assertEqual(status['profile'], 'static-site')
        self.assertEqual(status['artifact'], '2048-master-site.zip')
        self.assertEqual(status['input_sha256'], sha(data))
        self.assertTrue(all(isinstance(e, dict) and set(e) == {'message'} for e in status['events']))
        files, path, digest = self.members(run)
        self.assertEqual(path.name, status['artifact'])
        self.assertEqual(digest, status['sha256'])
        self.assertEqual(digest, sha(path.read_bytes()))
        self.assertIn('site/index.html', files)
        self.assertIn('site/style/main.css', files)
        self.assertIn('site/js/game_manager.js', files)
        self.assertIn('site/LICENSE.txt', files)
        self.assertEqual(files['site/js/game_manager.js'], SITE_2048['js/game_manager.js'])
        self.assertFalse([n for n in files if '/.' in n], files.keys())
        info = json.loads(files['PACKAGE-INFO.json'])
        self.assertEqual(info['run'], run)
        self.assertEqual(info['input_sha256'], sha(data))
        self.assertIn('No claim is made', info['claims'])
        self.assertEqual(self.runner.calls, [], 'a static site never starts a container')

    def test_the_output_is_deterministic_apart_from_the_run_id(self):
        data = zip_bytes(SITE_2048)
        first, _ = self.built('static-site', data)
        second, _ = self.built('static-site', data)
        one, two = self.members(first)[0], self.members(second)[0]
        self.assertEqual({k: v for k, v in one.items() if k != 'PACKAGE-INFO.json'},
                         {k: v for k, v in two.items() if k != 'PACKAGE-INFO.json'})

    def test_unprefixed_zip_and_tar_gz_and_build_folder(self):
        run, _ = self.built('static-site', zip_bytes({'index.html': b'<p>hi</p>', 'a.js': b'1'}))
        self.assertIn('site/index.html', self.members(run)[0])
        archive = tar_bytes([reg('site-main/index.html', b'<p>hi</p>'), reg('site-main/x/y.css', b'b{}')], gz=True)
        run, status = self.built('static-site', archive)
        self.assertEqual(status['artifact'], 'site-main-site.zip')
        self.assertIn('site/x/y.css', self.members(run)[0])
        nested = zip_bytes({'package.json': b'{}', 'src/app.js': b'1', 'dist/index.html': b'x', 'dist/a.css': b'y'})
        run, status = self.built('static-site', nested)
        files = self.members(run)[0]
        self.assertIn('site/a.css', files)
        self.assertNotIn('site/src/app.js', files)
        self.assertTrue(any('no npm build' in e['message'] for e in status['events']))

    def test_macos_resource_forks_do_not_defeat_prefix_removal(self):
        data = zip_bytes({'game/index.html': b'x', '__MACOSX/game/._index.html': b'junk', 'game/a.js': b'1'})
        run, _ = self.built('static-site', data)
        files = self.members(run)[0]
        self.assertIn('site/a.js', files)
        self.assertFalse([n for n in files if 'MACOSX' in n])

    def test_sites_without_a_single_clear_index_are_refused(self):
        for data in (zip_bytes({'readme.md': b'x'}), zip_bytes({'a/index.html': b'x', 'b/index.html': b'y'}),
                     b'<html>just text</html>', zip_bytes({'.hidden/index.html': b'x', 'other.txt': b'y'})):
            with self.assertRaises(ValueError):
                self.jobs.start('static-site', data)
        self.no_runs()

    def test_corrupt_member_data_fails_the_run_instead_of_packaging_it(self):
        payload = b'AAAA-unique-payload-AAAA'
        data = zip_bytes({'index.html': payload}, compress=zipfile.ZIP_STORED)
        data = data.replace(payload, payload.replace(b'AAAA', b'BBBB'))
        status = self.failed('static-site', data)
        self.assertIn('cannot be read', status['message'])


class HostileArchiveTest(Base):
    def refused(self, data, *, jobs=None, text=None):
        jobs = jobs or self.jobs
        with self.assertRaises(ValueError) as caught:
            jobs.start('static-site', data)
        if text:
            self.assertIn(text, str(caught.exception))
        self.assertEqual(list((jobs.runs).iterdir()), [])

    def test_zip_names_that_escape_are_refused(self):
        for name in ('../evil.html', 'a/../../evil', '/abs/index.html', 'C:/x/index.html', '..\\evil',
                     'a/./index.html', 'x:y/index.html'):
            self.refused(raw_zip([('ok/index.html', b'x', 0o100644, 0), (name, b'x', 0o100644, 0)]))

    def test_zip_links_devices_and_encryption_are_refused(self):
        self.refused(raw_zip([('index.html', b'x', 0o100644, 0), ('link', b'/etc/passwd', 0o120777, 0)]),
                     text='symbolic link')
        self.refused(raw_zip([('index.html', b'x', 0o100644, 0), ('dev', b'', 0o020666, 0)]), text='device')
        self.refused(raw_zip([('index.html', b'x', 0o100644, 0), ('fifo', b'', 0o010644, 0)]), text='device')
        # libarchive-produced ZipCrypto archive; Python's ZIP writer clears caller-provided encryption flags.
        encrypted = base64.b64decode(
            'UEsDBBQACQAIAHSIeV0AAAAAAAAAAAAAAAAKACAAaW5kZXguaHRtbHV4CwABBAAAAAAEAAAAAFVUDQAHTGkHa0xp'
            'B2tMaQdrzeC4Pr9RIdAdfoumx/DW91BLBwgfCOpGEAAAAAIAAABQSwECFAMUAAkACAB0iHldHwjqRhAAAAAC'
            'AAAACgAYAAAAAAAAAAAAtoEAAAAAaW5kZXguaHRtbHV4CwABBAAAAAAEAAAAAFVUBQABTGkHa1BLBQYAAAAAAQAB'
            'AFAAAABoAAAAAAA=')
        with zipfile.ZipFile(io.BytesIO(encrypted)) as archive:
            self.assertEqual(archive.read('index.html', pwd=b'fixture'), b'x\n')
        self.refused(encrypted, text='encrypted')

    def test_duplicates_and_file_directory_clashes_are_refused(self):
        self.refused(raw_zip([('index.html', b'a', 0o100644, 0), ('index.html', b'b', 0o100644, 0)]))
        self.refused(raw_zip([('index.html', b'a', 0o100644, 0), ('INDEX.HTML', b'b', 0o100644, 0)]))
        self.refused(raw_zip([('index.html', b'a', 0o100644, 0), ('x', b'b', 0o100644, 0),
                              ('x/y', b'c', 0o100644, 0)]), text='file and a directory')

    def test_compression_bomb_is_refused(self):
        bomb = raw_zip([('index.html', b'x', 0o100644, 0), ('big.bin', b'\0' * (8 << 20), 0o100644, 0)])
        self.assertLess(len(bomb), 100_000)
        self.refused(bomb, text='compression ratio')

    def test_counts_and_sizes_are_bounded(self):
        small = self.make(limits=pa.Limits(max_input=10_000, max_members=3, max_member=500, max_expanded=900))
        many = zip_bytes({f'f{i}.txt': b'x' for i in range(5)} | {'index.html': b'x'})
        self.refused(many, jobs=small, text='more than 3')
        self.refused(zip_bytes({'index.html': b'x' * 600}), jobs=small, text='per-file')
        self.refused(zip_bytes({'index.html': b'x' * 400, 'b.js': b'y' * 400, 'c.js': b'z' * 400}), jobs=small)
        self.refused(os.urandom(20_000), jobs=small, text='exceeds')
        slim = self.make(limits=pa.Limits(max_expanded=1 << 20))
        self.refused(tar_bytes([reg('p/index.html', b'\0' * (4 << 20))], gz=True), jobs=slim)

    def test_truncated_and_garbage_archives_are_refused(self):
        good = zip_bytes(SITE_2048)
        self.refused(good[:len(good) // 2], text='corrupt')
        self.refused(b'PK\x03\x04' + b'garbage' * 10)
        self.refused(b'\x1f\x8b' + b'notgzip' * 10)

    def test_tar_links_devices_and_traversal_are_refused(self):
        index = reg('p/index.html')
        self.refused(tar_bytes([index, ('p/l', tarfile.SYMTYPE, b'', '/etc/passwd', 0o777)]), text='link')
        self.refused(tar_bytes([index, ('p/h', tarfile.LNKTYPE, b'', 'p/index.html', 0o644)]), text='link')
        self.refused(tar_bytes([index, ('p/c', tarfile.CHRTYPE, b'', '', 0o666)]), text='special')
        self.refused(tar_bytes([index, ('p/f', tarfile.FIFOTYPE, b'', '', 0o644)]), text='special')
        self.refused(tar_bytes([index, reg('../evil')]))
        self.refused(tar_bytes([index, reg('/etc/cron.d/x')]))
        self.refused(tar_bytes([index], gz=True)[:-9])


class RustTest(Base):
    def test_single_source_file(self):
        source = b'fn main() { println!("hi"); }\n'
        run, status = self.built('rust', source)
        (spec, staged), = self.runner.calls
        self.assertEqual(spec.image, COMPILER)
        self.assertEqual(spec.name, 'projbuild-' + run)
        self.assertEqual(dict(spec.env), {'PROJECT_NAME': 'project'})
        self.assertEqual(staged['src/main.rs'][0], source)
        self.assertEqual(staged['run.sh'][0].decode(), ps.RUSTC)
        self.assertEqual(sorted(staged), ['run.sh', 'src/main.rs'])
        self.assertEqual(status['artifact'], 'project-build.zip')
        files, path, digest = self.members(run)
        self.assertEqual(files['bin/hello'], ELF)
        self.assertEqual(digest, status['sha256'])
        info = json.loads(files['PACKAGE-INFO.json'])
        self.assertEqual(info['builder']['image'], COMPILER)
        self.assertEqual(info['builder']['network'], 'none')
        self.assertEqual(info['builder']['toolchain'], ['arch: x86_64', 'rustc: rustc 1.90.0 (fake)'])
        self.assertIn('never run', info['claims'])
        self.assertIn('not run', status['message'])
        with zipfile.ZipFile(path) as archive:
            mode = archive.getinfo('bin/hello').external_attr >> 16
        self.assertEqual(stat.S_IMODE(mode), 0o755)

    def test_cargo_workspace_with_lock_and_vendor_stays_offline(self):
        files = {
            'Cargo.toml': b'[package]\nname = "hello-app"\nversion = "0.1.0"\nedition = "2021"\n[workspace]\n'
                          b'members = ["crates/a"]\n[dependencies]\nserde = "1"\n',
            'Cargo.lock': b'version = 3\n[[package]]\nname = "serde"\nversion = "1.0.0"\n'
                          b'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "x"\n',
            'src/main.rs': b'fn main() {}\n',
            'crates/a/Cargo.toml': b'[package]\nname = "a"\nversion = "0.1.0"\n',
            'crates/a/src/lib.rs': b'pub fn a() {}\n',
            '.cargo/config.toml': b'[source.crates-io]\nreplace-with = "vendored-sources"\n'
                                  b'[source.vendored-sources]\ndirectory = "vendor"\n',
            'vendor/serde/Cargo.toml': b'[package]\nname = "serde"\nversion = "1.0.0"\n',
            'vendor/serde/.cargo-checksum.json': b'{"files":{}}',
            'rust-toolchain.toml': b'[toolchain]\nchannel = "nightly"\n',
            'target/debug/prebuilt': ELF,
            '.git/config': b'[core]',
        }
        run, status = self.built('rust', zip_bytes(files, prefix='hello-main/'))
        (spec, staged), = self.runner.calls
        self.assertEqual(status['artifact'], 'hello-app-build.zip')
        self.assertEqual(staged['run.sh'][0].decode(), ps.CARGO)
        self.assertIn('src/Cargo.lock', staged)
        self.assertIn('src/vendor/serde/Cargo.toml', staged)
        self.assertIn('src/.cargo/config.toml', staged)
        self.assertFalse([n for n in staged if n.startswith(('src/target', 'src/.git'))], sorted(staged))
        self.assertIn('--frozen', ps.CARGO)
        self.assertTrue(any('rust-toolchain' in e['message'] for e in status['events']))
        self.assertIn('rm -f rust-toolchain rust-toolchain.toml', ps.CARGO)

    def test_lock_needing_a_registry_without_vendor_config_is_refused(self):
        lock = (b'version = 3\n[[package]]\nname = "serde"\nversion = "1.0.0"\n'
                b'source = "registry+https://github.com/rust-lang/crates.io-index"\n')
        data = zip_bytes({'Cargo.toml': b'[package]\nname = "x"\nversion = "0.1.0"\n', 'Cargo.lock': lock,
                          'src/main.rs': b'fn main(){}'})
        with self.assertRaises(ValueError) as caught:
            self.jobs.start('rust', data)
        self.assertIn('vendor', str(caught.exception))
        self.assertEqual(self.runner.calls, [])
        self.no_runs()

    def test_cargo_config_may_only_describe_vendored_sources(self):
        base = {'Cargo.toml': b'[package]\nname = "x"\nversion = "0.1.0"\n', 'src/main.rs': b'fn main(){}'}
        for config in (b'[build]\nrustc-wrapper = "/bin/sh"\n', b'[target.x86_64-unknown-linux-gnu]\nlinker = "x"\n',
                       b'[alias]\nb = "build"\n', b'[source.v]\ndirectory = "/etc"\n',
                       b'[source.v]\ndirectory = "../outside"\n', b'[source.v]\ndirectory = "vendor"\n',
                       b'not = = toml'):
            with self.assertRaises(ValueError, msg=config):
                self.jobs.start('rust', zip_bytes(base | {'.cargo/config.toml': config}))
        with self.assertRaises(ValueError):
            self.jobs.start('rust', zip_bytes({'Cargo.toml': b'nonsense = ='}))
        with self.assertRaises(ValueError):
            self.jobs.start('rust', zip_bytes({'Cargo.toml': b'[profile.release]\nlto = true\n'}))
        self.assertEqual(self.runner.calls, [])
        self.no_runs()

    def test_projects_without_a_manifest_need_exactly_one_rs_file(self):
        run, _ = self.built('rust', zip_bytes({'src/lonely.rs': b'fn main(){}'}))
        self.assertEqual(self.runner.calls[-1][1]['src/main.rs'][0], b'fn main(){}')
        for data in (zip_bytes({'a.rs': b'1', 'b.rs': b'2'}), zip_bytes({'readme.md': b'x'})):
            with self.assertRaises(ValueError):
                self.jobs.start('rust', data)

    def test_manifest_without_dependencies_needs_no_lock(self):
        data = zip_bytes({'Cargo.toml': b'[package]\nname = "tiny"\nversion = "0.1.0"\n', 'src/main.rs': b'fn main(){}'})
        run, status = self.built('rust', data)
        self.assertEqual(status['artifact'], 'tiny-build.zip')
        self.assertIn('--offline', ps.CARGO)


class NativeTest(Base):
    def staged(self, files, prefix='proj-1.0/'):
        run, status = self.built('c-cpp', zip_bytes(files, prefix=prefix))
        return self.runner.calls[-1][1], status

    def test_makefile_cmake_and_configure_are_chosen_from_the_files(self):
        staged, status = self.staged({'Makefile': b'all:\n', 'main.c': b'int main(){}'})
        self.assertEqual(staged['run.sh'][0].decode(), ps.MAKE)
        self.assertEqual(status['artifact'], 'proj-1.0-build.zip')
        staged, _ = self.staged({'CMakeLists.txt': b'project(x)', 'Makefile': b'all:\n', 'main.c': b'int main(){}'})
        self.assertEqual(staged['run.sh'][0].decode(), ps.CMAKE)
        staged, _ = self.staged({'configure': b'#!/bin/sh\n', 'Makefile.in': b'all:\n'})
        self.assertEqual(staged['run.sh'][0].decode(), ps.CONFIGURE)
        self.assertEqual(staged['src/configure'][1], 0o755, 'a shebang file is made executable in the sandbox')

    def test_loose_sources_are_compiled_directly_and_git_metadata_is_left_out(self):
        staged, _ = self.staged({'main.c': b'int main(){}', 'util.h': b'', '.git/HEAD': b'ref'})
        self.assertEqual(staged['run.sh'][0].decode(), ps.DIRECT)
        self.assertEqual(sorted(staged), ['run.sh', 'src/main.c', 'src/util.h'])
        staged, _ = self.staged({'src/a.cpp': b'int main(){}'})
        self.assertEqual(staged['run.sh'][0].decode(), ps.DIRECT)

    def test_single_text_file_is_c_or_cpp_by_content(self):
        self.built('c-cpp', b'#include <stdio.h>\nint main(void){return 0;}\n')
        self.assertIn('src/main.c', self.runner.calls[-1][1])
        self.built('c-cpp', b'#include <iostream>\nint main(){std::cout << 1;}\n')
        self.assertIn('src/main.cpp', self.runner.calls[-1][1])

    def test_unbuildable_trees_are_refused_with_a_reason(self):
        for files, word in (({'readme.md': b'x'}, 'No CMakeLists'), ({'configure.ac': b'AC_INIT', 'Makefile.am': b''},
                                                                    'autoreconf'),
                            ({'deep/x/main.c': b'int main(){}'}, 'No CMakeLists')):
            with self.assertRaises(ValueError) as caught:
                self.jobs.start('c-cpp', zip_bytes(files))
            self.assertIn(word, str(caught.exception))
        self.no_runs()

    def test_project_name_reaches_the_sandbox_only_as_a_safe_environment_value(self):
        self.built('c-cpp', zip_bytes({'Makefile': b'all:\n'}, prefix='we ird;$(rm -rf x)`/'))
        spec = self.runner.calls[-1][0]
        self.assertRegex(dict(spec.env)['PROJECT_NAME'], r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class SandboxResultTest(Base):
    def respond(self, **kwargs):
        stdout = kwargs.pop('stdout', output_tar())
        stderr = kwargs.pop('stderr', b'')
        status = kwargs.pop('status', 0)
        self.runner.responder = lambda spec, staged: Result(status, stdout, stderr, **kwargs)

    def test_missing_tool_is_reported_accurately(self):
        self.respond(status=64, stdout=b'', stderr=b"cmake noise\nPROJECT-BUILD-ERROR: The pinned image has no 'cmake', "
                                                   b"which this project needs.\n")
        status = self.failed('c-cpp', zip_bytes({'CMakeLists.txt': b'project(x)'}))
        self.assertEqual(status['message'], "The pinned image has no 'cmake', which this project needs.")
        self.assertTrue(any('cmake noise' in e['message'] for e in status['events']))

    def test_other_failures_name_their_cause(self):
        cases = (
            (dict(status=101, stdout=b'', stderr=b'error[E0308]: mismatched types\n'), 'exit 101'),
            (dict(status=127, stdout=b''), 'could not run'),
            (dict(status=None, stdout=b'', timed_out=True), 'limit'),
            (dict(status=None, stdout=b'', overflow=True), 'size limit'),
            (dict(status=None, stdout=b'', feed_error='Archive member x is truncated.'), 'truncated'),
            (dict(status=None, stdout=b'', stopped=True), 'shutting down'),
        )
        for kwargs, word in cases:
            self.respond(**kwargs)
            status = self.failed('rust', b'fn main(){}')
            self.assertIn(word, status['message'], kwargs)

    def test_runner_errors_fail_the_run_and_free_the_slot(self):
        self.runner.error = RuntimeError('Docker did not answer.')
        self.assertEqual(self.failed('rust', b'fn main(){}')['message'], 'Docker did not answer.')
        self.runner.error = KeyError('boom: /secret/host/path')
        status = self.failed('rust', b'fn main(){}')
        self.assertNotIn('secret', status['message'])
        self.runner.error = None
        self.built('rust', b'fn main(){}')

    def test_hostile_or_oversized_output_is_never_packaged(self):
        bad = (
            b'not a tar at all' * 100,
            output_tar(extra=[('bin/link', tarfile.SYMTYPE, b'', '/etc/passwd', 0o777)]),
            output_tar(extra=[reg('../escape')]),
            output_tar(extra=[('bin/dev', tarfile.CHRTYPE, b'', '', 0o666)]),
            output_tar(files={}),
            output_tar(files={'bin/bad name': ELF, 'other/x': ELF}),
            zip_bytes({'bin/hello': ELF}),
        )
        for stdout in bad:
            self.respond(stdout=stdout)
            self.failed('rust', b'fn main(){}')
        self.respond()
        with mock.patch.object(pk, 'OUTPUT_LIMITS', pa.Limits(max_expanded=1000, max_member=1000, max_input=1 << 20)):
            self.respond(stdout=output_tar(files={'bin/big': ELF + b'0' * 5000}))
            self.failed('rust', b'fn main(){}')
        with mock.patch.object(pk, 'MAX_ARTIFACT', 10):
            self.respond()
            self.failed('rust', b'fn main(){}')

    def test_files_outside_bin_in_the_output_are_ignored(self):
        self.respond(stdout=output_tar(extra=[reg('etc/passwd', b'root')]))
        run, _ = self.built('rust', b'fn main(){}')
        self.assertEqual(sorted(self.members(run)[0]), ['PACKAGE-INFO.json', 'bin/hello'])


class PythonTest(Base):
    APP = {
        'pyproject.toml': b'[project]\nname = "My_App"\nversion = "1"\n[project.scripts]\nrun-it = "my_app:main"\n',
        'my_app/__init__.py': b'def main(): pass\n',
        'my_app/__pycache__/x.cpython-313.pyc': b'\x00',
        '__main__.py': b'import my_app\n',
        'requirements.txt': b'requests\n',
        '.env': b'SECRET=1',
        '.venv/lib/x.py': b'x = 1\n',
    }

    def test_package_with_sandbox_syntax_check(self):
        self.runner.responder = lambda spec, staged: Result(0, b'', b'PROJECT-CHECK: python=3.13.1 files=2\n')
        run, status = self.built('python-app', zip_bytes(self.APP, prefix='my_app-main/'))
        (spec, staged), = self.runner.calls
        self.assertEqual(spec.image, PYTHON)
        self.assertEqual(staged['run.sh'][0].decode(), ps.PYTHON_RUN)
        self.assertEqual(staged['check.py'][0].decode(), ps.PYTHON_CHECK)
        self.assertNotIn('src/.env', staged)
        self.assertNotIn('src/.venv/lib/x.py', staged)
        files = self.members(run)[0]
        self.assertEqual(sorted(n for n in files if n.startswith('app/')),
                         ['app/__main__.py', 'app/my_app/__init__.py', 'app/pyproject.toml', 'app/requirements.txt'])
        info = json.loads(files['PACKAGE-INFO.json'])
        self.assertEqual(info['syntax_check'], {'python': '3.13.1', 'files': 2})
        self.assertEqual(info['details']['console_scripts'], ['run-it'])
        self.assertFalse(info['details']['dependencies_included'])
        self.assertEqual(status['artifact'], 'My_App-app.zip')

    def test_syntax_error_fails_the_run(self):
        self.runner.responder = lambda spec, staged: Result(
            65, b'', b'PROJECT-BUILD-ERROR: Python 3.13.1 cannot parse a.py line 1: invalid syntax\n')
        status = self.failed('python-app', zip_bytes({'a.py': b'print "hi"\n'}))
        self.assertIn('cannot parse a.py', status['message'])

    def test_without_a_python_image_it_still_packages_and_says_the_check_did_not_run(self):
        jobs = self.make(image_for=lambda key: (_ for _ in ()).throw(KeyError(key)))
        run, status = self.built('python-app', zip_bytes({'main.py': b'print(1)\n'}), jobs)
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(any('Syntax check skipped' in e['message'] for e in status['events']))
        path, _ = jobs.package(run)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(json.loads(archive.read('PACKAGE-INFO.json'))['syntax_check'], 'not run')

    def test_input_needs_python_sources(self):
        with self.assertRaises(ValueError):
            self.jobs.start('python-app', zip_bytes({'readme.md': b'x'}))
        self.runner.responder = lambda spec, staged: Result(0, b'', b'')
        self.built('python-app', b'print("single file")\n')
        self.assertIn('src/main.py', self.runner.calls[-1][1])


class DockerBoundaryTest(unittest.TestCase):
    def argv(self, **env):
        spec = Spec(COMPILER, 'projbuild-' + 'c' * 32, ps.BOOTSTRAP, tuple(env.items()), lambda s: None, 5, 1)
        return DockerRunner(docker='docker', runtime='runsc').argv(spec)

    def test_no_mount_socket_network_or_privilege_is_requested(self):
        argv = self.argv(PROJECT_NAME='demo')
        self.assertIn('--network=none', argv)
        self.assertIn('--runtime=runsc', argv)
        self.assertIn('--read-only', argv)
        self.assertIn('--cap-drop=ALL', argv)
        self.assertIn('--security-opt=no-new-privileges', argv)
        self.assertIn('--user=65534:65534', argv)
        self.assertIn('--pull=never', argv)
        self.assertIn('--env=PROJECT_NAME=demo', argv)
        self.assertTrue(any(a.startswith('--memory=') for a in argv))
        self.assertTrue(any(a.startswith('--pids-limit=') for a in argv))
        for arg in argv:
            self.assertNotIn(arg, ('-v', '--volume', '--mount', '--privileged', '--network=host', '-p', '--publish',
                                   '--device', '--pid=host', '--ipc=host', '--cap-add'))
            self.assertNotRegex(arg, r'docker\.sock|^--volume|^--mount|^--cap-add|^--device|^--network=(?!none)')
        tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == '--tmpfs']
        self.assertEqual(sorted(t.split(':')[0] for t in tmpfs), ['/tmp', '/work'])
        self.assertTrue(all('size=' in t for t in tmpfs))
        self.assertEqual(argv[-5:-2], ['--entrypoint', '/bin/sh', COMPILER])
        self.assertEqual(argv[-2:], ['-c', ps.BOOTSTRAP])

    def test_spec_values_are_validated(self):
        runner = DockerRunner(docker='docker')
        for image in ('rust:bookworm', 'sha256:abc', COMPILER + 'x'):
            with self.assertRaises(ValueError):
                runner.argv(Spec(image, 'projbuild-' + 'c' * 32, 'x', (), lambda s: None, 1, 1))
        for name in ('x', 'projbuild-../..', 'projbuild-' + 'C' * 32):
            with self.assertRaises(ValueError):
                runner.argv(Spec(COMPILER, name, 'x', (), lambda s: None, 1, 1))
        for env in ((('PROJECT_NAME', 'a b'),), (('PROJECT_NAME', 'a;b'),), (('project', 'x'),), (('A', '$(x)'),)):
            with self.assertRaises(ValueError):
                runner.argv(Spec(COMPILER, 'projbuild-' + 'c' * 32, 'x', env, lambda s: None, 1, 1))
        with self.assertRaises(ValueError):
            DockerRunner(runtime='')
        with self.assertRaises(ValueError):
            runner.discard('not-a-build')

    def test_scripts_use_no_network_tools_or_embedded_urls(self):
        for name, script in {**ps.SCRIPTS, 'bootstrap': ps.BOOTSTRAP}.items():
            for word in ('curl', 'wget', 'http://', 'https://', 'pip install', 'npm ', 'git clone', 'apt-get', 'ssh '):
                self.assertNotIn(word, script, f'{name}: {word}')
        self.assertIn('cargo build --release', ps.CARGO)
        self.assertNotIn('cargo test', ps.CARGO)

    @unittest.skipUnless(shutil.which('sh'), 'no POSIX sh on PATH for a syntax-only check')
    def test_scripts_parse(self):
        for name, script in ps.SCRIPTS.items():
            done = subprocess.run(['sh', '-n'], input=script.encode(), capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0, f'{name}: {done.stderr.decode(errors="replace")}')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'the collector needs a POSIX userland (GNU find, od, tr)')
    def test_collector_takes_only_new_executables_libraries_and_static_archives(self):
        work = Path(tempfile.mkdtemp(prefix='collect-'))
        self.addCleanup(shutil.rmtree, work, True)
        root, out, stamp = work / 'tree', work / 'out', work / 'stamp'
        (root / 'sub').mkdir(parents=True)
        (root / 'CMakeFiles').mkdir()
        elf = lambda kind: b'\x7fELF\x02\x01\x01' + b'\x00' * 9 + bytes([kind, 0]) + b'\x00' * 40
        stamp.write_bytes(b'')
        os.utime(stamp, (1_700_000_000, 1_700_000_000))
        files = {
            'old.so': (elf(3), 1_699_999_000), 'app': (elf(2), 1_700_000_500), 'sub/lib.so': (elf(3), 1_700_000_500),
            'obj.o': (elf(1), 1_700_000_500), 'libx.a': (b'!<arch>\n' + b'\0' * 20, 1_700_000_500),
            'libx.rlib': (b'!<arch>\n' + b'\0' * 20, 1_700_000_500), 'script.sh': (b'#!/bin/sh\n' + b'\0' * 30, 1_700_000_500),
            'CMakeFiles/probe': (elf(2), 1_700_000_500),
        }
        for name, (data, mtime) in files.items():
            (root / name).write_bytes(data)
            os.utime(root / name, (mtime, mtime))
        script = (f'set -eu\nOUT={out}\nmkdir -p "$OUT/bin"\n' + ps._COLLECT.replace('/work/stamp', str(stamp))
                  + f'collect_from {root}\n')
        done = subprocess.run(['sh', '-c', script], capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors='replace'))
        self.assertEqual(sorted(p.name for p in (out / 'bin').iterdir()), ['app', 'libx.a', 'sub_lib.so'])


class PersistenceTest(Base):
    def record(self, run, state, **extra):
        directory = self.tmp / 'state' / 'runs' / run
        directory.mkdir(parents=True, exist_ok=True)
        data = {'format': 1, 'id': run, 'profile': 'rust', 'state': state, 'message': 'x', 'events': ['x'],
                'artifact': None, 'sha256': None, 'input_sha256': '0' * 64, 'bytes': 0} | extra
        (directory / 'status.json').write_text(json.dumps(data))
        return directory

    def test_built_runs_survive_a_restart_and_interrupted_runs_are_failed(self):
        run, before = self.built('static-site', zip_bytes(SITE_2048))
        stale = 'ab' * 16
        self.record(stale, 'building')
        (self.tmp / 'state' / 'runs' / stale / 'scratch.zip').write_bytes(b'partial')
        queued = 'cd' * 16
        self.record(queued, 'queued')
        again = self.make(runner=FakeRunner())
        self.assertEqual(again.status(run), before)
        self.assertEqual(again.package(run)[1], before['sha256'])
        for item in (stale, queued):
            status = again.status(item)
            self.assertEqual(status['state'], 'failed')
            self.assertIn('interrupted', status['message'])
            self.assertIsNone(status['artifact'])
            with self.assertRaises(RuntimeError):
                again.package(item)
        self.assertFalse((self.tmp / 'state' / 'runs' / stale / 'scratch.zip').exists())
        self.assertEqual(again.wait(1), True)
        self.built('static-site', zip_bytes(SITE_2048), again)

    def test_interrupted_build_container_is_discarded_on_restart(self):
        stale = 'ef' * 16
        self.record(stale, 'building')
        runner = FakeRunner()
        self.make(runner=runner)
        self.assertEqual(runner.discarded, ['projbuild-' + stale])

    def test_changed_or_missing_output_is_never_downloadable(self):
        run, status = self.built('static-site', zip_bytes(SITE_2048))
        path = self.jobs.package(run)[0]
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaises(RuntimeError):
            self.jobs.package(run)
        self.assertEqual(self.jobs.status(run)['state'], 'built', 'same size: only the digest check can tell')
        path.write_bytes(original + b'x')
        with self.assertRaises(RuntimeError):
            self.jobs.package(run)
        path.unlink()
        view = self.jobs.status(run)
        self.assertEqual((view['state'], view['artifact'], view['sha256']), ('failed', None, None))
        with self.assertRaises(RuntimeError):
            self.jobs.package(run)

    def test_unreadable_records_are_reported_not_guessed(self):
        run = '12' * 16
        directory = self.record(run, 'built', artifact='x.zip', sha256='1' * 64, bytes=3)
        for text in ('{not json', json.dumps({'id': run}), json.dumps([1])):
            (directory / 'status.json').write_text(text)
            with self.assertRaises(RuntimeError):
                self.jobs.status(run)
        self.record(run, 'built', artifact='../x.zip', sha256='1' * 64, bytes=3)
        with self.assertRaises(RuntimeError):
            self.jobs.package(run)
        self.record(run, 'built', artifact=None, sha256=None)
        with self.assertRaises(RuntimeError):
            self.jobs.status(run)

    def test_each_run_packages_only_its_own_output(self):
        one, _ = self.built('static-site', zip_bytes({'index.html': b'one'}))
        self.runner.responder = lambda spec, staged: Result(1, b'', b'')
        two = self.failed('rust', b'fn main(){}')
        failed_run = self.jobs.start('rust', b'fn main(){}')
        self.jobs.wait(30)
        with self.assertRaises(RuntimeError):
            self.jobs.package(failed_run)
        path, _ = self.jobs.package(one)
        self.assertEqual(path.parent.name, one)
        self.assertEqual(two['artifact'], None)


class BusyTest(Base):
    def test_only_one_build_at_a_time(self):
        self.runner.gate = threading.Event()
        first = self.jobs.start('rust', b'fn main(){}')
        for _ in range(500):
            if self.runner.calls or self.jobs.status(first)['state'] == 'building':
                break
            time.sleep(0.01)
        self.assertIn(self.jobs.status(first)['state'], ('queued', 'building'))
        with self.assertRaises(RuntimeError) as caught:
            self.jobs.start('static-site', zip_bytes(SITE_2048))
        self.assertIn('running', str(caught.exception))
        with self.assertRaises(RuntimeError):
            self.jobs.package(first)
        self.runner.gate.set()
        self.assertTrue(self.jobs.wait(30))
        self.assertEqual(self.jobs.status(first)['state'], 'built')
        self.built('static-site', zip_bytes(SITE_2048))

    def test_close_stops_a_running_build_and_records_the_failure(self):
        self.runner.gate = threading.Event()
        run = self.jobs.start('rust', b'fn main(){}')
        for _ in range(500):
            if self.runner.calls:
                break
            time.sleep(0.01)
        self.jobs.close()
        status = self.jobs.status(run)
        self.assertEqual(status['state'], 'failed')
        self.assertIn('shutting down', status['message'])


@unittest.skipUnless(os.environ.get('PROJECT_JOBS_COMPILER_IMAGE'),
                     'set PROJECT_JOBS_COMPILER_IMAGE to a pinned image ID (sha256:...) to exercise real Docker + runsc')
class RealSandboxTest(unittest.TestCase):
    def test_compiles_a_single_rust_file_in_the_real_sandbox(self):
        tmp = Path(tempfile.mkdtemp(prefix='projreal-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        image = os.environ['PROJECT_JOBS_COMPILER_IMAGE']
        jobs = ProjectJobs(tmp, lambda key: image, build_seconds=600)
        self.addCleanup(jobs.close)
        row = {r['id']: r for r in jobs.profiles()}['rust']
        self.assertTrue(row['available'], row['reason'])
        run = jobs.start('rust', b'fn main() { println!("hello"); }\n')
        self.assertTrue(jobs.wait(900))
        status = jobs.status(run)
        self.assertEqual(status['state'], 'built', status)
        with zipfile.ZipFile(jobs.package(run)[0]) as archive:
            binary = archive.read('bin/project')
        self.assertEqual(binary[:4], b'\x7fELF')


if __name__ == '__main__':
    unittest.main()
