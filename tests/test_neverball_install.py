"""Cases for the optional Windows rebuild profile on a clean host: the updater-installed compiler and the prepared release.

Linux only. Nothing here runs Docker or a compiler. `Maintenance` and the active dependency set are real files on disk;
`Installed` replaces only `_compile` with synthetic PE bytes and records the compiler image the run would have started.
"""
import contextlib
import inspect
import io
import json
import shutil
import tarfile
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from build_desk import desk, game_jobs as gj, maintenance as mt, neverball_archives
from tests.test_neverball_jobs import LINUX, make_pe, make_release, sha, write
from tools import prepare_neverball as prep

CHECKOUT = Path(__file__).resolve().parents[1]
LEGACY = 'sha256:93aac6b24e9536cb987fa1c454b71db35bc423abcb1ea087e19a2a2d65e09b28'
IMAGE_A = 'sha256:' + 'a1' * 32
IMAGE_B = 'sha256:' + 'b2' * 32


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def install_set(data, number, image, recipe, *, previous=None, family='oci-build', key=desk.RUNNER_KEY):
    """Write one complete active dependency set exactly as the updater does (receipt and manifest hashed by active.json)."""
    set_id = f'r{number}-' + f'{number:012x}'
    base = data / mt.STATE_DIR / 'sets' / set_id
    base.mkdir(parents=True)
    receipt = canonical({'set': set_id, 'components': {key: {'key': key, 'family': family, 'image_id': image, 'recipe_sha256': recipe}}})
    manifest = b'{}\n'
    write(base / 'receipt.json', receipt)
    write(base / 'manifest.json', manifest)
    write(data / mt.STATE_DIR / 'active.json', json.dumps({
        'schema': mt.SCHEMA, 'set': set_id, 'previous': previous, 'revision': number, 'manifest_sha256': sha(manifest),
        'receipt_sha256': sha(receipt), 'switched_at': 1}).encode())
    return set_id


class Installed(desk.InstalledRunnerJobs):
    """The real desk coordinator, minus the container: records which compiler image each run would have started."""
    gate = None

    def __init__(self, *args, **kwargs):
        self.compiled, self.commands = [], []
        super().__init__(*args, **kwargs)

    def _compile(self, run, facts):
        self.compiled.append(self.pins.image)
        self.commands.append(self._docker_command(run, self.release / 'source', self.release / 'vendor'))
        if self.gate is not None:
            self.gate.wait(30)
        return make_pe(run.encode())


@unittest.skipUnless(LINUX, 'game_jobs targets Linux')
class RunnerBinding(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='gameinstall-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.release = self.tmp / 'release'
        self.release.mkdir()
        self.pins, self.fx = make_release(self.release)
        self.data = self.tmp / 'data'
        self.data.mkdir()
        self.root = self.tmp / 'games'

    def maintenance(self):
        return mt.Maintenance(self.data, CHECKOUT, upstream=None)

    def jobs(self, *, pins=None, approved=None, maintenance=None):
        """A fixture coordinator. The fixture release's archive is the one approved digest unless `approved` says otherwise."""
        jobs = Installed(self.root, self.release, maintenance or self.maintenance(), pins=pins or self.pins,
                         approved=(self.pins.vendor_tgz_sha256,) if approved is None else approved)
        self.addCleanup(jobs.close)
        return jobs

    def run_job(self, jobs):
        run = jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        jobs._thread.join(60)
        return run

    def events(self, jobs, run):
        return {e['state']: e['data'] for e in jobs._load(run)[0]}

    def assertUnavailable(self, jobs=None):
        jobs = jobs or self.jobs()
        with self.assertRaises(desk.RunnerUnavailable):
            jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        self.assertFalse(jobs.busy())
        self.assertEqual([p.name for p in self.root.iterdir() if p.is_dir()], [], 'a refused start must leave no run directory')
        self.assertEqual(jobs.compiled, [])
        self.assertEqual(jobs.pins.image, '')

    # ----------------------------------------------------------------- the installed image is the compiler

    def test_run_uses_the_updater_installed_image_everywhere(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        jobs = self.jobs()
        self.assertEqual(jobs.pins.image, '', 'before a run the compiler is empty, never the legacy built-in image')
        run = self.run_job(jobs)
        view = jobs.status(run)
        self.assertEqual(view['state'], 'built', view['message'])
        events = self.events(jobs, run)
        self.assertEqual(events['queued']['compiler_image'], IMAGE_A)
        self.assertEqual(events['build']['compiler_image'], IMAGE_A)
        self.assertEqual(events['queued']['recipe_sha256'], self.pins.recipe_sha256)
        with zipfile.ZipFile(self.root / run / gj.CANDIDATE) as archive:
            provenance = json.loads(archive.read('provenance.json'))
        self.assertEqual(provenance['compiler_image'], IMAGE_A)
        self.assertEqual(provenance['recipe_sha256'], self.pins.recipe_sha256)
        self.assertEqual(provenance['source_manifest_sha256'], events['queued']['source_manifest_sha256'])
        self.assertEqual(jobs.compiled, [IMAGE_A])
        self.assertEqual(jobs.commands[0][-1], IMAGE_A)
        self.assertNotIn(LEGACY, [part for command in jobs.commands for part in command])
        self.assertNotIn(LEGACY, json.dumps(jobs._load(run)[0]))

    def test_the_legacy_image_id_is_not_a_default_anywhere(self):
        self.assertEqual(replace(self.pins, image='').image, '')
        first = self.jobs()
        self.assertEqual(first.pins.image, '')
        first.close()  # release this fixture's coordinator lock on the shared test root before opening the same root again
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        jobs = self.jobs()
        self.run_job(jobs)
        self.assertNotEqual(jobs.pins.image, LEGACY)
        self.assertEqual(jobs.pins.image, IMAGE_A)

    def test_a_request_cannot_choose_the_compiler(self):
        self.assertEqual(list(inspect.signature(desk.InstalledRunnerJobs.start).parameters), ['self', 'prompt', 'reference_sha256'])
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        jobs = self.jobs()
        with self.assertRaises(ValueError):
            jobs.start(gj.PROMPT, LEGACY.removeprefix('sha256:'))
        with self.assertRaises(ValueError):
            jobs.start(gj.PROMPT + ' --image evil', self.pins.original_zip_sha256)
        self.assertEqual(jobs.compiled, [])

    # ----------------------------------------------------------------- the dependency archive is an exact approved digest

    def test_the_approved_archives_are_exactly_the_two_reviewed_digests(self):
        self.assertEqual(neverball_archives.APPROVED, ('22e12932d194701a8652d4baa28af6513d77289aae4bed2aa06fd9f75c553d1f',
                                                       '88a960b6c75d5b303ac2b636b77681cfd1e4f4782434c5a498c1c2c9b5ce8b84'))
        self.assertEqual(neverball_archives.HISTORICAL_SHA256, gj.PINS.vendor_tgz_sha256)
        self.assertEqual(inspect.signature(desk.InstalledRunnerJobs.__init__).parameters['approved'].default, neverball_archives.APPROVED)
        self.assertEqual(inspect.signature(prep.prepare).parameters['approved'].default, neverball_archives.APPROVED)
        with self.assertRaises(ValueError):
            desk.InstalledRunnerJobs(self.root, self.release, self.maintenance(), pins=self.pins, approved=('not-a-digest',))

    def test_a_run_binds_the_release_archive_when_it_is_the_portable_or_the_historical_digest(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        archive = self.pins.vendor_tgz_sha256
        unrelated = replace(self.pins, vendor_tgz_sha256='ee' * 32)  # the coordinator's own default is not what binds the run
        for label, approved in (('portable', (archive, 'ff' * 32)), ('historical', ('ff' * 32, archive))):
            with self.subTest(label):
                shutil.rmtree(self.root, ignore_errors=True)
                jobs = self.jobs(pins=unrelated, approved=approved)
                run = self.run_job(jobs)
                self.assertEqual(jobs.status(run)['state'], 'built')
                self.assertEqual(self.events(jobs, run)['queued']['vendor_tgz_sha256'], archive)
                with zipfile.ZipFile(self.root / run / gj.CANDIDATE) as zipped:
                    self.assertEqual(json.loads(zipped.read('provenance.json'))['vendor_tgz_sha256'], archive)
                    self.assertEqual(sha(zipped.read('build/dependencies.tgz')), archive)

    def test_an_archive_that_is_not_an_approved_digest_is_refused_even_with_the_same_files(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        self.assertUnavailable(self.jobs(approved=('11' * 32,)))  # the coordinator's own pin does not approve anything

    def test_a_missing_or_linked_release_archive_is_refused(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        link = self.tmp / 'elsewhere.tgz'
        (self.release / 'dependencies.tgz').rename(link)
        (self.release / 'dependencies.tgz').symlink_to(link)
        self.assertUnavailable()

    def test_a_run_stays_checked_against_the_archive_its_journal_recorded(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        archive = self.pins.vendor_tgz_sha256
        first = self.jobs()
        run = self.run_job(first)
        artifact = self.events(first, run)['package']['artifact_sha256']
        candidate = self.root / run / gj.CANDIDATE
        first.close()
        jobs = self.jobs(pins=replace(self.pins, vendor_tgz_sha256='ee' * 32), approved=(archive, 'ff' * 32))
        with jobs._bound(run):
            self.assertEqual(jobs.pins.vendor_tgz_sha256, archive)
            seen = []
            other = threading.Thread(target=lambda: seen.append(jobs.pins.vendor_tgz_sha256))
            other.start()
            other.join()
            self.assertEqual(seen, ['ee' * 32], 'the binding is for the calling thread only')
            gj.verify_candidate_zip(candidate, artifact, jobs.pins)
        self.assertEqual(jobs.pins.vendor_tgz_sha256, 'ee' * 32)
        with self.assertRaises((gj.GameJobError, zipfile.BadZipFile)):
            gj.verify_candidate_zip(candidate, artifact, jobs.pins)
        self.assertEqual(jobs.status(run)['state'], 'built')
        self.assertEqual(jobs.package(run)[1], jobs.status(run)['download_sha256'], 'checked against the journaled archive binding')
        self.assertEqual(jobs.status()['id'], run)
        jobs.close()  # one coordinator per root: release this fixture's lock before opening the same root again
        unapproved = self.jobs(pins=replace(self.pins, vendor_tgz_sha256='ee' * 32), approved=('ff' * 32,))
        with unapproved._bound(run):
            self.assertEqual(unapproved.pins.vendor_tgz_sha256, 'ee' * 32, 'a digest that is no longer approved is not honoured')

    # ----------------------------------------------------------------- one consistent receipt read

    def test_image_and_recipe_come_from_one_receipt_read(self):
        class Spy(mt.Maintenance):
            def __init__(self, *args, **kwargs):
                self.reads = 0
                super().__init__(*args, **kwargs)

            def _read_active(self):
                self.reads += 1
                return super()._read_active()

            def image_for(self, key):
                raise AssertionError('image and recipe must not be read separately')

        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        spy = Spy(self.data, CHECKOUT, upstream=None)
        spy.reads = 0
        self.assertEqual(desk.installed_runner(spy, self.pins.recipe_sha256), desk.Runner(IMAGE_A, self.pins.recipe_sha256))
        self.assertEqual(spy.reads, 1)
        jobs = self.jobs(maintenance=spy)
        spy.reads = 0
        self.run_job(jobs)
        self.assertEqual(spy.reads, 1)

    def test_two_records_for_the_runner_are_ambiguous_and_refused(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        set_dir = self.data / mt.STATE_DIR / 'sets' / ('r1-' + f'{1:012x}')
        record = {'key': desk.RUNNER_KEY, 'family': 'oci-build', 'image_id': IMAGE_B, 'recipe_sha256': self.pins.recipe_sha256}
        receipt = canonical({'set': set_dir.name, 'components': {'a': dict(record, image_id=IMAGE_A), 'b': record}})
        write(set_dir / 'receipt.json', receipt)
        active = json.loads((self.data / mt.STATE_DIR / 'active.json').read_bytes())
        active['receipt_sha256'] = sha(receipt)
        write(self.data / mt.STATE_DIR / 'active.json', json.dumps(active).encode())
        self.assertUnavailable()

    # ----------------------------------------------------------------- only an immutable, locally built, pinned-recipe image

    def test_no_installed_runner_refuses_before_any_run(self):
        self.assertUnavailable()

    def test_the_committed_pin_is_not_an_installed_runner(self):
        manifest = mt.load_manifest(CHECKOUT / mt.MANIFEST_NAME)
        component = manifest.images[desk.RUNNER_KEY]
        self.assertEqual(component['family'], 'oci-build', 'the runner is built locally; there is no registry digest to fall back to')
        self.assertEqual(component['recipe_sha256'], gj.PINS.recipe_sha256, 'the stock recipe must be the one the release pins')
        with self.assertRaises(mt.Unavailable):
            self.maintenance().image_for(desk.RUNNER_KEY)

    def test_mutable_or_foreign_identifiers_are_refused(self):
        refused = {'tag': 'neverball-runner:latest', 'workspace tag': 'workspace-neverball-runner:2c197c645731',
                   'registry digest': 'docker.io/library/rust@sha256:' + 'a1' * 32, 'bare hex': 'a1' * 32,
                   'uppercase': 'sha256:' + 'A1' * 32, 'short': 'sha256:' + 'a1' * 31, 'padded': ' ' + IMAGE_A, 'empty': ''}
        for label, image in refused.items():
            with self.subTest(label):
                shutil.rmtree(self.data / mt.STATE_DIR, ignore_errors=True)
                install_set(self.data, 1, image, self.pins.recipe_sha256)
                self.assertUnavailable()
                shutil.rmtree(self.root, ignore_errors=True)

    def test_a_runner_not_built_from_the_pinned_recipe_is_refused(self):
        install_set(self.data, 1, IMAGE_A, '0' * 64)
        self.assertUnavailable()

    def test_a_pulled_image_is_not_the_locally_built_runner(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256, family='oci-image')
        self.assertUnavailable()

    def test_the_wrong_component_is_not_the_runner(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256, key='generic-compiler')
        self.assertUnavailable()

    def test_a_damaged_active_set_is_refused(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        (self.data / mt.STATE_DIR / 'sets' / ('r1-' + f'{1:012x}') / 'receipt.json').write_bytes(b'{"set":"r1-000000000001","components":{}}')
        self.assertUnavailable()

    # ----------------------------------------------------------------- a run keeps its compiler

    def test_an_update_during_a_run_applies_to_the_next_run_only(self):
        first = install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        jobs = self.jobs()
        jobs.gate = threading.Event()
        run1 = jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        install_set(self.data, 2, IMAGE_B, self.pins.recipe_sha256, previous=first)
        with self.assertRaises(gj.Busy):
            jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        self.assertEqual(jobs.pins.image, IMAGE_A, 'a refused start must not change the running job\'s compiler')
        jobs.gate.set()
        jobs._thread.join(60)
        run2 = self.run_job(jobs)
        self.assertEqual(self.events(jobs, run1)['queued']['compiler_image'], IMAGE_A)
        self.assertEqual(self.events(jobs, run1)['build']['compiler_image'], IMAGE_A)
        self.assertEqual(self.events(jobs, run2)['queued']['compiler_image'], IMAGE_B)
        self.assertEqual(jobs.compiled, [IMAGE_A, IMAGE_B])
        self.assertEqual([c[-1] for c in jobs.commands], [IMAGE_A, IMAGE_B])
        with zipfile.ZipFile(self.root / run1 / gj.CANDIDATE) as archive:
            self.assertEqual(json.loads(archive.read('provenance.json'))['compiler_image'], IMAGE_A)

    def test_removing_the_runner_stops_new_runs_but_not_old_ones_from_being_read(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        jobs = self.jobs()
        run = self.run_job(jobs)
        shutil.rmtree(self.data / mt.STATE_DIR)
        with self.assertRaises(desk.RunnerUnavailable):
            jobs.start(gj.PROMPT, self.pins.original_zip_sha256)
        self.assertEqual(jobs.status(run)['state'], 'built')
        self.assertEqual(jobs.package(run)[1], jobs.status(run)['download_sha256'], 'an old, intact build stays downloadable')

    # ----------------------------------------------------------------- the desk opens the profile only this way

    def test_the_desk_opens_games_through_the_installed_runner(self):
        install_set(self.data, 1, IMAGE_A, self.pins.recipe_sha256)
        jobs = desk.open_games(self.data, self.release, self.maintenance())
        self.addCleanup(jobs.close)
        self.assertIsInstance(jobs, desk.InstalledRunnerJobs)
        self.assertEqual(jobs.root, self.data / 'games')
        self.assertEqual(desk.installed_runner(self.maintenance(), self.pins.recipe_sha256).image, IMAGE_A)
        source = inspect.getsource(desk.serve)
        self.assertIn('open_games(', source)
        self.assertNotIn('GameJobs(', source)

    def test_start_is_serialised_with_pin_selection(self):
        source = inspect.getsource(desk.InstalledRunnerJobs.start)
        self.assertIn('self._starting', source)
        self.assertLess(source.index('self.busy()'), source.index('installed_runner('), 'a busy job must keep its compiler')


# ====================================================================== the prepared release

LOCK_SUM = 'ab' * 32
LOCK = ('[[package]]\nname = "neverball"\nversion = "0.1.0"\n\n[[package]]\nname = "miniquad"\nversion = "0.4.0"\n'
        f'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "{LOCK_SUM}"\n').encode()
CRATE_LIB = b'keep\nold\ntail\n'
PATCH = b'--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n'
DOCKERFILE = b'FROM rust\n'


def make_inputs(tmp, *, lib=CRATE_LIB, patch=PATCH, extra_crate=False, crlf=False):
    """A fake checkout, the original ZIP, an installed crate tree and a supplied dependency archive; `approved` holds its digest."""
    reference = tmp / 'reference'
    reference.mkdir()
    base_pins, _ = make_release(reference)
    source = {'Cargo.toml': b'[package]\nname="neverball"\n', 'desktop/Cargo.toml': b'[package]\nname="neverball-desktop"\n',
              'desktop/Cargo.lock': LOCK, 'desktop/src/main.rs': b'fn main() {}\n', 'src/lib.rs': b'// lib\n',
              'build/miniquad-i686.patch': patch, 'build/windows-i686.Dockerfile': DOCKERFILE, 'COPYING': b'GPL\n',
              'NOTICE.md': b'notice\n'}
    for rel, data in source.items():
        write(tmp / 'checkout/examples/neverball' / rel, data.replace(b'\n', b'\r\n') if crlf else data)
    vendor = tmp / 'installed'
    crates = {'miniquad': (lib, LOCK_SUM)}
    if extra_crate:
        crates['other'] = (b'x\n', 'cd' * 32)
    for name, (data, package) in crates.items():
        write(vendor / name / 'src/lib.rs', data)
        write(vendor / name / '.cargo-checksum.json', json.dumps({'files': {'src/lib.rs': sha(CRATE_LIB if name == 'miniquad' else data)},
                                                                   'package': package}).encode())
    archive = tmp / 'dependencies.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted(p for p in vendor.rglob('*') if p.is_file()):
            tar.add(path, 'vendor/' + path.relative_to(vendor).as_posix())
    pins = replace(gj.PINS, vendor_tgz_sha256=sha(archive.read_bytes()), lock_sha256=sha(LOCK), recipe_sha256=sha(DOCKERFILE),
                   original_zip_sha256=base_pins.original_zip_sha256, pk3_sha256=base_pins.pk3_sha256)
    return SimpleNamespace(checkout=tmp / 'checkout', original=reference / 'original.zip', archive=archive, vendor=vendor,
                           pins=pins, approved=(pins.vendor_tgz_sha256, 'cd' * 32), output=tmp / 'out' / 'release')


@unittest.skipUnless(LINUX, 'game_jobs targets Linux')
class PrepareRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='gameprepare-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def refused(self, inputs, pattern, **kwargs):
        kwargs.setdefault('archive', inputs.archive)
        kwargs.setdefault('approved', inputs.approved)
        with self.assertRaisesRegex(prep.PrepareError, pattern):
            prep.prepare(inputs.checkout, inputs.output, inputs.original, pins=inputs.pins, **kwargs)
        self.assertFalse(inputs.output.exists(), 'a refused preparation must leave no release')
        if inputs.output.parent.exists():
            self.assertEqual(list(inputs.output.parent.iterdir()), [], 'a refused preparation must leave no staging directory')

    def test_an_approved_archive_makes_a_release_the_desk_verifies(self):
        inputs = make_inputs(self.tmp)
        facts = prep.prepare(inputs.checkout, inputs.output, inputs.original, archive=inputs.archive, vendor=inputs.vendor,
                             pins=inputs.pins, approved=inputs.approved)
        self.assertEqual((facts['archive_sha256'], facts['archive_source'], facts['crates'], facts['patch_hunks'],
                          facts['installed_crates_differ_in_files']), (inputs.pins.vendor_tgz_sha256, 'supplied archive', 1, 1, 0))
        self.assertEqual(gj.verify_release(inputs.output, inputs.pins)['manifest_sha256'], facts['manifest_sha256'])
        self.assertEqual(sha((inputs.output / 'dependencies.tgz').read_bytes()), inputs.pins.vendor_tgz_sha256)
        self.assertEqual([p.name for p in inputs.output.parent.iterdir()], ['release'])

    def test_a_crlf_checkout_is_hashed_as_the_committed_lf_files(self):
        inputs = make_inputs(self.tmp, crlf=True)
        prep.prepare(inputs.checkout, inputs.output, inputs.original, archive=inputs.archive, pins=inputs.pins, approved=inputs.approved)
        self.assertEqual((inputs.output / 'source/build/windows-i686.Dockerfile').read_bytes(), DOCKERFILE)

    def test_installed_crates_make_the_release_only_when_their_canonical_archive_is_approved(self):
        inputs = make_inputs(self.tmp)
        scratch = self.tmp / 'regenerated.tgz'
        prep.deterministic_archive(inputs.vendor, scratch)
        regenerated = sha(scratch.read_bytes())
        self.assertNotEqual(regenerated, inputs.pins.vendor_tgz_sha256)
        self.refused(inputs, regenerated, archive=None, vendor=inputs.vendor)  # the message names the canonical digest, not a pass
        again = self.tmp / 'again.tgz'
        prep.deterministic_archive(inputs.vendor, again)
        self.assertEqual(sha(again.read_bytes()), regenerated, 'the canonical archive must be byte-reproducible')
        facts = prep.prepare(inputs.checkout, inputs.output, inputs.original, vendor=inputs.vendor, pins=inputs.pins,
                             approved=(regenerated, 'cd' * 32))
        self.assertEqual((facts['archive_sha256'], facts['archive_source']), (regenerated, 'installed crates'))
        bound = replace(inputs.pins, vendor_tgz_sha256=regenerated)
        self.assertEqual(gj.verify_release(inputs.output, bound)['manifest_sha256'], facts['manifest_sha256'])
        self.assertEqual(sha((inputs.output / 'dependencies.tgz').read_bytes()), regenerated)

    def test_the_same_files_in_an_unapproved_archive_are_refused(self):
        inputs = make_inputs(self.tmp)
        self.refused(inputs, 'not an approved dependency archive', approved=neverball_archives.APPROVED)

    def test_a_different_dependency_archive_is_refused(self):
        inputs = make_inputs(self.tmp)
        other = self.tmp / 'other.tgz'
        other.write_bytes(inputs.archive.read_bytes() + b'\0')
        self.refused(inputs, 'not an approved dependency archive', archive=other)

    def test_a_different_original_release_is_refused(self):
        inputs = make_inputs(self.tmp)
        other = self.tmp / 'neverball.zip'
        with zipfile.ZipFile(other, 'w') as archive:
            archive.writestr('neverball-1.6.0/neverball.exe', make_pe(b'not the original'))
        inputs.original = other
        self.refused(inputs, 'is not the original Neverball 1.6.0 Windows release')

    def test_there_is_nothing_to_vendor_from(self):
        inputs = make_inputs(self.tmp)
        self.refused(inputs, 'no vendored crates', archive=None, vendor=None)

    def test_a_crate_that_does_not_match_its_checksum_is_refused(self):
        inputs = make_inputs(self.tmp, lib=b'keep\nold\ntail\nEVIL\n')
        self.refused(inputs, 'does not match its crate checksum')

    def test_crates_that_cargo_lock_does_not_name_are_refused(self):
        inputs = make_inputs(self.tmp, extra_crate=True)
        self.refused(inputs, 'not exactly the crates Cargo.lock names')

    def test_a_patch_that_no_longer_applies_is_refused(self):
        inputs = make_inputs(self.tmp, patch=PATCH.replace(b'-old', b'-different'))
        self.refused(inputs, 'no longer applies')

    def test_an_existing_release_is_never_replaced(self):
        inputs = make_inputs(self.tmp)
        write(inputs.output / 'precious', b'keep me')
        with self.assertRaisesRegex(prep.PrepareError, 'already exists'):
            prep.prepare(inputs.checkout, inputs.output, inputs.original, archive=inputs.archive, pins=inputs.pins, approved=inputs.approved)
        self.assertEqual((inputs.output / 'precious').read_bytes(), b'keep me')
        self.assertEqual(sorted(p.name for p in inputs.output.parent.iterdir()), ['release'])

    def test_command_line_refuses_without_dependencies(self):
        inputs = make_inputs(self.tmp)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            code = prep.main(['--root', str(self.tmp / 'data'), '--original', str(inputs.original)])
        self.assertEqual(code, 2)
        self.assertIn('Not prepared', errors.getvalue())
        self.assertFalse((self.tmp / 'data' / 'neverball').exists())


if __name__ == '__main__':
    unittest.main()
