"""Cases for game_jobs: real release verification, PE checks, ZIP assembly, journal, recovery and receipt validation.

Linux only (flock, O_NOFOLLOW, uids). Nothing here runs Docker or a compiler: `FakeJobs` replaces only `_compile` with
synthetic PE bytes; release verification, PE validation, packaging, journaling and qualification run for real.
"""
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from build_desk import game_jobs as gj

LINUX = sys.platform.startswith('linux')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_pe(tag=b'', size=70000, machine=0x14C, subsystem=2, dll=False, entry=0x1000, opt_magic=0x10B):
    data = bytearray(size)
    data[0:2] = b'MZ'
    struct.pack_into('<I', data, 0x3C, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    flags = 0x0002 | 0x0100 | (0x2000 if dll else 0)
    struct.pack_into('<HHIIIHH', data, 0x84, machine, 1, 0, 0, 0, 224, flags)
    opt = 0x98
    struct.pack_into('<H', data, opt, opt_magic)
    struct.pack_into('<I', data, opt + 16, entry)
    struct.pack_into('<II', data, opt + 56, 0x3000, 0x200)
    struct.pack_into('<H', data, opt + 68, subsystem)
    table = opt + 224
    struct.pack_into('<8sIIII', data, table, b'.text', 0x1000, 0x1000, 0x1000, 0x400)
    struct.pack_into('<I', data, table + 36, 0x60000020)
    data[0x1000:0x1000 + len(tag)] = tag
    return bytes(data)


def write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def fix_modes(base):
    for current, dirs, files in os.walk(base):
        os.chmod(current, 0o755)
        for name in files:
            if not os.path.islink(os.path.join(current, name)):
                os.chmod(os.path.join(current, name), 0o644)


def write_manifest(release):
    lines = []
    for top in ('source', 'vendor'):
        for current, _, files in os.walk(release / top):
            for name in files:
                path = Path(current) / name
                lines.append(f'{sha(path.read_bytes())}  {path.relative_to(release).as_posix()}')
    write(release / 'source.sha256', ('\n'.join(sorted(lines)) + '\n').encode())


def make_release(release):
    source = {'Cargo.toml': b'[package]\nname="neverball"\n', 'desktop/Cargo.toml': b'[package]\nname="neverball-desktop"\n',
              'desktop/Cargo.lock': b'# frozen lock\n', 'desktop/src/main.rs': b'fn main() {}\n', 'src/lib.rs': b'// lib\n',
              'build/miniquad-i686.patch': b'fixture: pinned Win32 window bindings fix\n',
              'build/windows-i686.Dockerfile': b'FROM rust\n', 'COPYING': b'GPL text\n', 'NOTICE.md': b'notice\n'}
    vendor = {'crate-a/Cargo.toml': b'[package]\nname="a"\n', 'crate-a/src/lib.rs': b'// a\n',
              'crate-b/.cargo-checksum.json': b'{"files":{}}\n'}
    for rel, data in source.items():
        write(release / 'source' / rel, data)
    for rel, data in vendor.items():
        write(release / 'vendor' / rel, data)
    with tarfile.open(release / 'dependencies.tgz', 'w:gz') as tar:
        for rel, data in vendor.items():
            info = tarfile.TarInfo('vendor/' + rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    original_exe = make_pe(b'original')
    pk3 = b'pk3-data-' * 5000
    top = 'neverball-1.6.0/'
    with zipfile.ZipFile(release / 'original.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(top + 'neverball.exe', original_exe)
        archive.writestr(top + 'SDL.dll', b'MZdll')
        archive.writestr(top + 'data/data-1.6.0.pk3', pk3)
        archive.writestr(top + 'LICENSE.md', b'GPL notice\n')
        archive.writestr(top + 'doc/legal/license-GPL-2.0.txt', b'GPL 2\n')
        archive.writestr(top + 'README.txt', b'readme\n')
    write_manifest(release)
    fix_modes(release)
    pins = replace(gj.PINS, vendor_tgz_sha256=sha((release / 'dependencies.tgz').read_bytes()),
                   lock_sha256=sha(source['desktop/Cargo.lock']), recipe_sha256=sha(source['build/windows-i686.Dockerfile']),
                   original_zip_sha256=sha((release / 'original.zip').read_bytes()), pk3_sha256=sha(pk3))
    return pins, {'exe_sha256': sha(original_exe), 'pk3': pk3, 'source': source}


class FakeJobs(gj.GameJobs):
    """Real GameJobs except the compiler: `output` stands in for the pinned image's stdout."""
    output = None
    error = None
    gate = None

    def _compile(self, run, facts):
        if self.gate is not None:
            self.gate.wait(30)
        if self.error is not None:
            raise self.error
        return self.output if self.output is not None else make_pe(run.encode())


class NoDockerJobs(FakeJobs):
    docker = '/nonexistent/docker'


@unittest.skipUnless(LINUX, 'game_jobs targets Linux')
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='gamejobs-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.release = self.tmp / 'release'
        self.release.mkdir()
        self.pins, self.fx = make_release(self.release)
        self.root = self.tmp / 'root'
        self.opened = []

    def make(self, cls=FakeJobs):
        jobs = cls(self.root, self.release, pins=self.pins)
        self.opened.append(jobs)
        self.addCleanup(jobs.close)
        return jobs

    def run_job(self, jobs):
        run = jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        jobs._thread.join(60)
        return run

    def waiting(self):
        jobs = self.make()
        run = self.run_job(jobs)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'waiting_windows', view['message'])
        return jobs, run

    def receipt(self, jobs, run):
        """A complete, honest-shaped receipt and its evidence, bound to this candidate. Returns (receipt, files)."""
        cand = jobs.status(run)['candidate']
        receipt = {'version': 1, 'run': run, 'reference_sha256': self.pins.original_zip_sha256,
                   'artifact_sha256': cand['artifact_sha256'], 'zip_sha256': cand['zip_sha256'], 'binding': cand['binding'],
                   'checks': []}
        files = {}
        for case in gj.REQUIRED:
            item = {'name': case.name, 'kind': case.kind}
            if case.kind == 'continuous':
                base = [index * 0.01 for index in range(80)]
                values = {'original': base, 'candidate': [v + case.tolerance / 10 for v in base]}
                item['comparison'] = {'metric': 'max_abs_error', 'tolerance': case.tolerance,
                                      'observed': max(abs(a - b) for a, b in zip(*values.values())), 'accepted': True}
            for label, subject in (('original', self.fx['exe_sha256']), ('candidate', cand['artifact_sha256'])):
                name = f'{case.name}-{label}.log'
                data = (f'run={run} subject={subject} binding={cand["binding"]} case={case.name} side={label}\n'
                        'recorded observation lines\n').encode()
                files[name] = data
                side = {'method': 'windows_run', 'subject_sha256': subject, 'evidence': name, 'evidence_sha256': sha(data)}
                if case.kind == 'discrete':
                    side['outcome'] = f'{case.name}: observed state 4 of 4'
                else:
                    side['values'] = values[label]
                item[label] = side
            receipt['checks'].append(item)
        return receipt, files

    def install(self, jobs, run, receipt, files, mode=0o600, name=gj.RECEIPT):
        folder = jobs.evidence_dir(run)
        for fname, data in files.items():
            write(folder / fname, data, 0o600)
        write(jobs.root / run / name, json.dumps(receipt).encode(), mode)

    def check_unqualified(self, jobs, run):
        with self.assertRaises(gj.Unqualified):
            jobs.package(run)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'waiting_windows')
        self.assertNotIn('verified_sha256', view)

    def check_rejected(self, jobs, run):
        with self.assertRaises(gj.Rejected):
            jobs.package(run)
        self.assertEqual(jobs.status(run)['state'], 'failed')
        with self.assertRaises(gj.GameJobError):
            jobs.package(run)

    def prove(self, mutate=None, files_mutate=None):
        jobs, run = self.waiting()
        receipt, files = self.receipt(jobs, run)
        if mutate:
            mutate(receipt)
        if files_mutate:
            files_mutate(files)
        self.install(jobs, run, receipt, files)
        return jobs, run


class ReleaseCases(Base):
    def test_valid_release_is_verified(self):
        facts = gj.verify_release(self.release, self.pins)
        self.assertEqual(facts['original']['exe_sha256'], self.fx['exe_sha256'])
        self.assertIn('source/desktop/Cargo.lock', facts['source'])
        self.assertEqual([n for n, _ in facts['original']['notices']].count('LICENSE.md'), 1)
        self.assertNotIn('SDL.dll', [n for n, _ in facts['original']['notices']])

    def assertRefused(self, message=None):
        with self.assertRaises(gj.ReleaseError) as caught:
            gj.verify_release(self.release, self.pins)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_extra_source_file(self):
        write(self.release / 'source' / 'extra.rs', b'// extra\n')
        self.assertRefused('extra or missing')

    def test_missing_source_file(self):
        (self.release / 'source' / 'src' / 'lib.rs').unlink()
        self.assertRefused('extra or missing')

    def test_modified_source_file(self):
        write(self.release / 'source' / 'src' / 'lib.rs', b'// changed\n')
        self.assertRefused('differs from source.sha256')

    def test_symlink_member(self):
        os.symlink('Cargo.toml', self.release / 'source' / 'link.toml')
        self.assertRefused('symlink')

    def test_c_source_refused_even_when_listed(self):
        write(self.release / 'source' / 'src' / 'game.c', b'int main(void){return 0;}\n')
        write_manifest(self.release)
        fix_modes(self.release)
        self.assertRefused('forbidden member')

    def test_build_script_refused(self):
        write(self.release / 'source' / 'build.rs', b'fn main() {}\n')
        write_manifest(self.release)
        fix_modes(self.release)
        self.assertRefused('forbidden member')

    def test_wrong_lock_pin(self):
        self.pins = replace(self.pins, lock_sha256='0' * 64)
        self.assertRefused('Cargo.lock')

    def test_wrong_recipe_pin(self):
        self.pins = replace(self.pins, recipe_sha256='0' * 64)
        self.assertRefused('recipe')

    def test_vendor_must_match_dependencies_tarball(self):
        write(self.release / 'vendor' / 'crate-a' / 'src' / 'lib.rs', b'// tampered\n')
        write_manifest(self.release)
        fix_modes(self.release)
        self.assertRefused('does not match vendor')

    def test_dependencies_tarball_pin(self):
        self.pins = replace(self.pins, vendor_tgz_sha256='1' * 64)
        self.assertRefused('dependencies.tgz')

    def test_extra_top_level_entry(self):
        write(self.release / 'surprise.txt', b'x')
        self.assertRefused('exactly')

    def test_group_writable_is_untrusted(self):
        os.chmod(self.release / 'source' / 'Cargo.toml', 0o664)
        self.assertRefused('writable')

    def test_original_pin(self):
        self.pins = replace(self.pins, original_zip_sha256='2' * 64)
        self.assertRefused('original.zip')

    def test_pk3_pin(self):
        self.pins = replace(self.pins, pk3_sha256='3' * 64)
        self.assertRefused('data-1.6.0.pk3')

    def test_manifest_shape(self):
        for bad in (b'', b'not a manifest\n', b'%s  ../escape\n' % (b'a' * 64), b'%s  other/file\n' % (b'a' * 64)):
            with self.assertRaises(gj.ReleaseError):
                gj._parse_manifest(bad)
        line = b'%s  source/a\n' % (b'a' * 64)
        with self.assertRaises(gj.ReleaseError):
            gj._parse_manifest(line + line)


class PeCases(unittest.TestCase):
    def test_valid_gui_pe32(self):
        self.assertEqual(gj.check_pe(make_pe())['subsystem'], 'windows-gui')

    def test_refusals(self):
        for label, data in {'x64': make_pe(machine=0x8664), 'console': make_pe(subsystem=3), 'dll': make_pe(dll=True),
                            'pe32plus': make_pe(opt_magic=0x20B), 'entry outside code': make_pe(entry=0x9000),
                            'truncated': make_pe()[:300], 'elf': b'\x7fELF' + bytes(70000), 'empty': b''}.items():
            with self.subTest(label), self.assertRaises(gj.GameJobError):
                gj.check_pe(data)


class StartCases(Base):
    def test_fixed_prompt_and_reference(self):
        jobs = self.make()
        for prompt, ref in (('Rebuild Neverball 1.6.0 in Rust for Windows', self.pins.original_zip_sha256),
                            (gj.PROMPT + ' ', self.pins.original_zip_sha256), (None, self.pins.original_zip_sha256),
                            (gj.PROMPT, '0' * 64), (gj.PROMPT, self.pins.original_zip_sha256.upper()), (gj.PROMPT, 'abc'),
                            (gj.PROMPT, None)):
            with self.subTest(prompt=prompt, ref=ref), self.assertRaises(ValueError):
                jobs.start(prompt, ref)
        self.assertEqual(jobs.status()['state'], 'idle')
        self.assertFalse(jobs.busy())

    def test_prompt_digest_constant(self):
        self.assertEqual(hashlib.sha256(gj.PROMPT.encode()).hexdigest(), gj.PROMPT_SHA256)

    def test_invalid_release_blocks_start_without_a_run(self):
        write(self.release / 'source' / 'extra.rs', b'// extra\n')
        jobs = self.make()
        with self.assertRaises(gj.ReleaseError):
            jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        self.assertFalse(jobs.busy())
        self.assertEqual(jobs.status()['state'], 'idle')

    def test_one_attempt_at_a_time(self):
        jobs = self.make()
        jobs.gate = threading.Event()
        run = jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        self.assertRegex(run, r'[0-9a-f]{32}')
        self.assertTrue(jobs.busy())
        with self.assertRaises(gj.Busy):
            jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        jobs.gate.set()
        jobs._thread.join(60)
        self.assertFalse(jobs.busy())
        self.assertEqual(jobs.status(run)['state'], 'waiting_windows')
        self.assertNotEqual(jobs.start(gj.PROMPT, self.pins.original_zip_sha256), run)

    def test_second_coordinator_is_excluded(self):
        self.make()
        with self.assertRaises(gj.GameJobError):
            gj.GameJobs(self.root, self.release, pins=self.pins)

    def test_root_must_be_private(self):
        shared = self.tmp / 'shared'
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        with self.assertRaises(gj.GameJobError):
            gj.GameJobs(shared, self.release, pins=self.pins)

    def test_unknown_run(self):
        jobs = self.make()
        for bad in ('x', 'A' * 32, '0' * 32):
            with self.assertRaises(ValueError):
                jobs.status(bad)
            with self.assertRaises(ValueError):
                jobs.package(bad)

    def test_docker_command_is_fixed_and_confined(self):
        jobs = self.make()
        cmd = jobs._docker_command('a' * 32, self.release / 'source', self.release / 'vendor')
        joined = ' '.join(cmd)
        for flag in ('--runtime=runsc', '--pull=never', '--network=none', '--read-only', '--user=65534:65534',
                     '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=', '--memory=', '--cpus='):
            self.assertIn(flag, joined)
        self.assertEqual(cmd[0], '/usr/bin/docker')
        self.assertEqual(cmd[-1], self.pins.image)
        self.assertNotIn('privileged', joined)
        self.assertEqual([c for c in cmd if c.startswith('type=bind')],
                         [f'type=bind,src={self.release / "source"},dst=/src,readonly',
                          f'type=bind,src={self.release / "vendor"},dst=/vendor,readonly'])
        self.assertIn('/work:rw,nosuid,nodev,exec', joined)
        self.assertEqual(gj.GameJobs.docker, '/usr/bin/docker')
        with self.assertRaises(gj.ReleaseError):
            jobs._docker_command('a' * 32, self.release / 'missing', self.release / 'vendor')


class PipelineCases(Base):
    def test_candidate_reaches_waiting_with_pins_and_exact_zip(self):
        jobs, run = self.waiting()
        pe = make_pe(run.encode())
        view = jobs.status(run)
        self.assertEqual([e['state'] for e in view['events']], ['queued', 'build', 'package', 'waiting_windows'])
        self.assertEqual(view['candidate']['artifact_sha256'], sha(pe))
        archive_path = self.root / run / gj.CANDIDATE
        self.assertEqual(view['candidate']['zip_sha256'], sha(archive_path.read_bytes()))
        self.assertEqual(stat.S_IMODE(archive_path.stat().st_mode), 0o400)
        queued = view['events'][0]['data']
        self.assertEqual(queued['compiler_image'], self.pins.image)
        self.assertEqual(queued['lock_sha256'], self.pins.lock_sha256)
        self.assertEqual(queued['vendor_tgz_sha256'], self.pins.vendor_tgz_sha256)
        self.assertEqual(queued['original_zip_sha256'], self.pins.original_zip_sha256)
        self.assertEqual(view['events'][2]['data']['pe']['subsystem'], 'windows-gui')
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            self.assertEqual(archive.read('neverball.exe'), pe)
            self.assertEqual(archive.read('data/data-1.6.0.pk3'), self.fx['pk3'])
            self.assertEqual(archive.read('source/desktop/Cargo.lock'), self.fx['source']['desktop/Cargo.lock'])
            self.assertEqual(sha(archive.read('build/dependencies.tgz')), self.pins.vendor_tgz_sha256)
            provenance = json.loads(archive.read('provenance.json'))
        self.assertEqual(provenance['artifact_sha256'], sha(pe))
        for expected in ('notices/LICENSE.md', 'notices/doc/legal/license-GPL-2.0.txt', 'notices/README.txt',
                         'source/desktop/src/main.rs', 'source/COPYING', 'source/build/windows-i686.Dockerfile'):
            self.assertIn(expected, names)
        self.assertEqual([n for n in names if n.lower().endswith(('.exe', '.dll', '.c', '.h'))], ['neverball.exe'])
        self.assertNotIn(self.fx['exe_sha256'], [sha(archive_path.read_bytes())])
        self.assertNotIn('verified_sha256', view)
        self.assertIn('qualification', view)

    def test_waiting_run_cannot_be_packaged(self):
        jobs, run = self.waiting()
        self.check_unqualified(jobs, run)

    def test_compiler_failure_is_recorded(self):
        jobs = self.make()
        jobs.error = gj.GameJobError('The compiler exited 101: error[E0308]')
        run = self.run_job(jobs)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'failed')
        self.assertIn('E0308', view['message'])
        self.assertEqual([e['state'] for e in view['events']], ['queued', 'build', 'failed'])
        self.assertFalse(jobs.busy())
        with self.assertRaises(gj.GameJobError):
            jobs.package(run)

    def test_non_pe_output_fails(self):
        for label, output in {'elf': b'\x7fELF' + bytes(70000), 'console': make_pe(subsystem=3), 'tiny': b'MZ'}.items():
            with self.subTest(label):
                jobs = self.make()
                jobs.output = output
                run = self.run_job(jobs)
                self.assertEqual(jobs.status(run)['state'], 'failed')
                self.assertFalse((self.root / run / gj.CANDIDATE).exists())
                jobs.close()
                self.opened.clear()
                shutil.rmtree(self.root)

    def test_byte_copy_of_original_exe_fails(self):
        jobs = self.make()
        jobs.output = make_pe(b'original')
        run = self.run_job(jobs)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'failed')
        self.assertIn('byte copy', view['message'])
        self.assertFalse((self.root / run / gj.CANDIDATE).exists())

    def test_release_changed_after_start_fails(self):
        class Tamper(FakeJobs):
            def _build(self, run, facts):
                write(self.release / 'source' / 'src' / 'lib.rs', b'// changed after the release was accepted\n')
                return super()._build(run, facts)

        jobs = self.make(Tamper)
        run = self.run_job(jobs)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'failed')
        self.assertIn('differs from source.sha256', view['message'])
        self.assertFalse((self.root / run / gj.CANDIDATE).exists())

    def test_runtime_error_is_not_trusted_as_message(self):
        jobs = self.make()
        jobs.error = OSError('disk exploded')
        run = self.run_job(jobs)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'failed')
        self.assertIn('Internal error (OSError)', view['message'])
        self.assertNotIn('disk exploded', view['message'])

    def test_candidate_not_servable_by_tampering_after_packaging(self):
        jobs, run = self.prove()
        path = self.root / run / gj.CANDIDATE
        os.chmod(path, 0o600)
        with path.open('ab') as stream:
            stream.write(b'x')
        with self.assertRaises(gj.Rejected):
            jobs.package(run)
        self.assertEqual(jobs.status(run)['state'], 'failed')


class JournalCases(Base):
    def test_hash_chain_detects_edits(self):
        jobs, run = self.waiting()
        journal = self.root / run / 'journal.jsonl'
        os.chmod(journal, 0o600)
        text = journal.read_text().replace('Compiling', 'Compilinq', 1)
        journal.write_text(text)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'failed')
        self.assertIn('Journal integrity', view['message'])
        with self.assertRaises(gj.JournalError):
            jobs.package(run)

    def test_torn_tail_is_ignored_then_truncated(self):
        jobs, run = self.prove()
        journal = self.root / run / 'journal.jsonl'
        with journal.open('ab') as stream:
            stream.write(b'{"seq":9,"torn')
        self.assertEqual(jobs.status(run)['state'], 'proven')  # settles to proven, truncating the torn tail first
        events, committed = jobs._load(run)
        self.assertEqual([e['state'] for e in events][-1], 'proven')
        self.assertEqual(journal.stat().st_size, committed)

    def test_transition_order_is_enforced(self):
        jobs, run = self.waiting()
        with self.assertRaises(gj.GameJobError):
            jobs._append(run, 'package', 'again')
        with self.assertRaises(gj.GameJobError):
            jobs._append(run, 'queued', 'again')


class RecoveryCases(Base):
    def test_interrupted_build_is_failed_not_resumed(self):
        jobs = self.make()
        run = '0123456789abcdef0123456789abcdef'
        (self.root / run).mkdir(mode=0o700)
        jobs._append(run, 'queued', 'q')
        jobs._append(run, 'build', 'b')
        write(self.root / run / (gj.CANDIDATE + '.part'), b'partial')
        jobs.close()
        again = self.make(NoDockerJobs)
        view = again.status(run)
        self.assertEqual(view['state'], 'failed')
        self.assertIn('while build', view['message'])
        self.assertIn('not resumed', view['message'])
        self.assertIn('could not be confirmed', view['message'])
        self.assertEqual(view['events'][-1]['data'], {'interrupted_state': 'build'})
        self.assertFalse((self.root / run / (gj.CANDIDATE + '.part')).exists())
        self.assertFalse(again.busy())

    def test_waiting_and_proven_survive_restart(self):
        jobs, run = self.prove()
        jobs.package(run)
        jobs.close()
        again = self.make(NoDockerJobs)
        self.assertEqual(again.status(run)['state'], 'proven')
        path, digest = again.package(run)
        self.assertEqual(digest, sha(path.read_bytes()))

    def test_waiting_survives_restart_and_stays_unproven(self):
        jobs, run = self.waiting()
        jobs.close()
        again = self.make(NoDockerJobs)
        self.assertEqual(again.status(run)['state'], 'waiting_windows')
        self.check_unqualified(again, run)


class ReceiptCases(Base):
    def test_cannot_skip_live_play_audio_or_persistence(self):
        jobs, run = self.waiting()
        required = {'startup_menu', 'level_load', 'level_catalog', 'coin_collection', 'goal_completion',
                    'timeout_failure', 'fall_out_failure', 'recorded_replay', 'pause_resume',
                    'challenge_progress', 'score_after_relaunch', 'audio_output', 'ball_motion', 'camera_motion'}
        self.assertEqual(required, {case.name for case in gj.REQUIRED})
        self.check_unqualified(jobs, run)

    def test_valid_receipt_releases_exactly_the_candidate(self):
        jobs, run = self.prove()
        path, digest = jobs.package(run)
        self.assertEqual(path, self.root / run / gj.CANDIDATE)
        self.assertEqual(digest, sha(path.read_bytes()))
        view = jobs.status(run)
        self.assertEqual(view['state'], 'proven')
        self.assertEqual(view['verified_sha256'], digest)
        self.assertEqual(view['events'][-1]['data']['artifact_sha256'], view['candidate']['artifact_sha256'])
        self.assertEqual(len(view['events'][-1]['data']['evidence_sha256']), 2 * len(gj.REQUIRED))
        self.assertEqual(jobs.package(run), (path, digest))

    def test_status_alone_proves_a_valid_receipt(self):
        jobs, run = self.prove()
        self.assertEqual(jobs.status(run)['state'], 'proven')

    def test_no_receipt(self):
        jobs, run = self.waiting()
        self.check_unqualified(jobs, run)

    def top_mutations(self):
        yield 'run', lambda r: r.update(run='f' * 32)
        yield 'zip', lambda r: r.update(zip_sha256='e' * 64)
        yield 'artifact', lambda r: r.update(artifact_sha256='d' * 64)
        yield 'binding', lambda r: r.update(binding='c' * 64)
        yield 'reference', lambda r: r.update(reference_sha256='b' * 64)
        yield 'version', lambda r: r.update(version=2)
        yield 'extra field', lambda r: r.update(bonus=1)
        yield 'missing field', lambda r: r.pop('binding')
        yield 'missing check', lambda r: r['checks'].pop()
        yield 'extra check', lambda r: r['checks'].append(copy.deepcopy(r['checks'][0]) | {'name': 'bonus_check'})
        yield 'duplicate check', lambda r: r['checks'].__setitem__(1, copy.deepcopy(r['checks'][0]))
        yield 'no checks', lambda r: r.update(checks=[])
        yield 'checks not list', lambda r: r.update(checks={})
        yield 'wrong kind', lambda r: r['checks'][0].update(kind='continuous')
        yield 'source-only method', lambda r: r['checks'][0]['original'].update(method='source_review')
        yield 'original is candidate', lambda r: r['checks'][0]['original'].update(subject_sha256=r['artifact_sha256'])
        yield 'candidate is original', lambda r: r['checks'][0]['candidate'].update(subject_sha256='a' * 64)
        yield 'unknown side field', lambda r: r['checks'][0]['candidate'].update(note='x')
        yield 'evidence traversal', lambda r: r['checks'][0]['candidate'].update(evidence='../journal.jsonl')
        yield 'evidence missing', lambda r: r['checks'][0]['candidate'].update(evidence='absent.log')
        yield 'evidence reused across sides', lambda r: r['checks'][0]['candidate'].update(
            evidence=r['checks'][0]['original']['evidence'], evidence_sha256=r['checks'][0]['original']['evidence_sha256'])
        yield 'weak outcome', lambda r: [r['checks'][0][s].update(outcome='pass') for s in ('original', 'candidate')]
        yield 'bool outcome', lambda r: [r['checks'][0][s].update(outcome=True) for s in ('original', 'candidate')]
        yield 'empty outcome', lambda r: [r['checks'][0][s].update(outcome='') for s in ('original', 'candidate')]
        yield 'short series', lambda r: [r['checks'][-1][s].update(values=[0.0, 1.0]) for s in ('original', 'candidate')]
        yield 'constant original series', lambda r: r['checks'][-1]['original'].update(values=[1.0] * 80)
        yield 'nonfinite series', lambda r: r['checks'][-1]['candidate'].update(values=[1e999] * 80)
        yield 'comparison tolerance loosened', lambda r: r['checks'][-1]['comparison'].update(tolerance=10.0)
        yield 'comparison observed lies', lambda r: r['checks'][-1]['comparison'].update(observed=0.0)
        yield 'comparison missing', lambda r: r['checks'][-1].pop('comparison')
        yield 'comparison accepted not bool', lambda r: r['checks'][-1]['comparison'].update(accepted=1)

    def test_malformed_or_unbound_receipts_never_release(self):
        for label, mutate in self.top_mutations():
            with self.subTest(label):
                self.setUp()
                jobs, run = self.prove(mutate)
                self.check_unqualified(jobs, run)

    def test_empty_evidence(self):
        jobs, run = self.waiting()
        receipt, files = self.receipt(jobs, run)
        name = receipt['checks'][0]['original']['evidence']
        files[name] = b''
        receipt['checks'][0]['original']['evidence_sha256'] = sha(b'')
        self.install(jobs, run, receipt, files)
        self.check_unqualified(jobs, run)

    def test_evidence_hash_mismatch(self):
        jobs, run = self.prove(files_mutate=lambda f: f.__setitem__(next(iter(f)), b'run mentioned elsewhere\n'))
        self.check_unqualified(jobs, run)

    def test_evidence_from_another_run_or_executable(self):
        for needle in ('run', 'subject', 'binding'):
            with self.subTest(needle):
                self.setUp()
                jobs, run = self.waiting()
                receipt, files = self.receipt(jobs, run)
                name = receipt['checks'][0]['candidate']['evidence']
                cand = receipt['checks'][0]['candidate']
                token = {'run': run, 'subject': cand['subject_sha256'], 'binding': receipt['binding']}[needle]
                files[name] = files[name].replace(token.encode(), b'0' * 64 if needle != 'run' else b'0' * 32)
                cand['evidence_sha256'] = sha(files[name])
                self.install(jobs, run, receipt, files)
                self.check_unqualified(jobs, run)

    def test_duplicate_evidence_content(self):
        jobs, run = self.waiting()
        receipt, files = self.receipt(jobs, run)
        first, second = receipt['checks'][0], receipt['checks'][1]
        files[second['original']['evidence']] = files[first['original']['evidence']]
        second['original']['evidence_sha256'] = first['original']['evidence_sha256']
        self.install(jobs, run, receipt, files)
        self.check_unqualified(jobs, run)

    def test_stale_evidence(self):
        jobs, run = self.waiting()
        receipt, files = self.receipt(jobs, run)
        self.install(jobs, run, receipt, files)
        stale = time.time_ns() - 3600 * 10**9
        old = jobs.evidence_dir(run) / receipt['checks'][0]['original']['evidence']
        os.utime(old, ns=(stale, stale))
        self.check_unqualified(jobs, run)

    def test_stale_receipt(self):
        jobs, run = self.waiting()
        receipt, files = self.receipt(jobs, run)
        self.install(jobs, run, receipt, files)
        stale = time.time_ns() - 3600 * 10**9
        os.utime(self.root / run / gj.RECEIPT, ns=(stale, stale))
        self.check_unqualified(jobs, run)

    def test_receipt_file_hygiene(self):
        for label in ('symlink', 'group writable', 'world writable', 'empty', 'directory'):
            with self.subTest(label):
                self.setUp()
                jobs, run = self.waiting()
                receipt, files = self.receipt(jobs, run)
                target = self.root / run / gj.RECEIPT
                if label == 'symlink':
                    self.install(jobs, run, receipt, files, name='real.json')
                    os.symlink('real.json', target)
                elif label == 'directory':
                    self.install(jobs, run, receipt, files, name='real.json')
                    target.mkdir()
                elif label == 'empty':
                    self.install(jobs, run, receipt, files)
                    target.write_bytes(b'')
                else:
                    self.install(jobs, run, receipt, files, mode=0o664 if label == 'group writable' else 0o666)
                self.check_unqualified(jobs, run)

    def test_non_strict_json(self):
        for label, text in {'duplicate key': '{"version":1,"version":1}', 'nan': '{"version":NaN}', 'array': '[]',
                            'garbage': 'not json'}.items():
            with self.subTest(label):
                self.setUp()
                jobs, run = self.waiting()
                write(self.root / run / gj.RECEIPT, text.encode(), 0o600)
                self.check_unqualified(jobs, run)

    def test_receipt_replayed_from_a_proven_run_is_refused(self):
        jobs, first = self.prove()
        jobs.package(first)
        second = jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        jobs._thread.join(60)
        shutil.copytree(jobs.evidence_dir(first), jobs.evidence_dir(second), dirs_exist_ok=True)
        shutil.copy(self.root / first / gj.RECEIPT, self.root / second / gj.RECEIPT)
        self.check_unqualified(jobs, second)

    def test_discrete_disagreement_fails_the_candidate(self):
        def mutate(receipt, files=None):
            side = receipt['checks'][3]['candidate']
            side['outcome'] = 'goal_completion: observed state 1 of 4'
        jobs, run = self.prove(mutate)
        self.check_rejected(jobs, run)

    def test_discrete_type_difference_fails(self):
        def mutate(receipt):
            receipt['checks'][0]['original']['outcome'] = 7
            receipt['checks'][0]['candidate']['outcome'] = '7'
        jobs, run = self.prove(mutate)
        self.check_rejected(jobs, run)

    def test_continuous_out_of_tolerance_fails(self):
        def mutate(receipt):
            item = receipt['checks'][-1]
            item['candidate']['values'] = [v + 1.0 for v in item['original']['values']]
            item['comparison']['observed'] = 1.0
        jobs, run = self.prove(mutate)
        self.check_rejected(jobs, run)

    def test_continuous_declared_not_accepted_fails(self):
        jobs, run = self.prove(lambda r: r['checks'][-1]['comparison'].update(accepted=False))
        self.check_rejected(jobs, run)

    def test_exposed_contract(self):
        contract = gj.qualification_contract()
        names = [c['name'] for c in contract['checks']]
        self.assertEqual(names, [c.name for c in gj.REQUIRED])
        self.assertEqual(len(set(names)), len(names))
        self.assertIn('continuous', {c['kind'] for c in contract['checks']})
        self.assertIn('discrete', {c['kind'] for c in contract['checks']})
        jobs, run = self.waiting()
        self.assertEqual(jobs.status(run)['qualification'], contract)
        self.assertEqual(jobs.receipt_path(run), self.root / run / 'qualification.json')

    def test_package_api_has_no_receipt_writer(self):
        self.assertFalse([n for n in dir(gj.GameJobs) if n.startswith(('write_receipt', 'qualify', 'attest', 'approve'))])


if __name__ == '__main__':
    unittest.main()
