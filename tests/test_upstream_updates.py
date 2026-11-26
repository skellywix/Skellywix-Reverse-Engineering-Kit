"""Upstream channels advance independently of a new Kit checkout. No services or credentials needed."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from build_desk import maintenance as mt
from tests.test_maintenance import Fixture, REPO, dump


@unittest.skipUnless(os.name == 'posix', 'the Linux launcher requires a private data directory')
class BootstrapPermissions(unittest.TestCase):
    def test_cli_creates_a_root_the_launcher_will_accept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'
            # A missing checkout stops before the network, after the CLI creates its first state.
            attempt = subprocess.run(
                [sys.executable, '-B', str(REPO / 'tools' / 'update_workspace.py'), '--root', str(root),
                 '--checkout', directory, '--no-discovery', 'bootstrap'],
                capture_output=True, text=True, umask=0o022, timeout=30,
            )
            self.assertEqual(1, attempt.returncode, attempt.stderr)
            self.assertEqual('failed', json.loads(attempt.stdout)['state'])
            self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))


class WorkspaceBuildAcceptance(unittest.TestCase):
    def setUp(self):
        fx = self.fx = Fixture(self)
        initial = fx.maintenance().bootstrap(wait=True)
        self.assertEqual('updated', initial['state'], initial['message'])
        pointer = fx.root / 'dependencies' / 'active.json'
        self.previous = pointer.read_bytes()
        self.pointer = pointer
        self.initial = initial['set']
        # Upgrade an existing dependency-only installation to a portable distribution.
        manifest = fx.bumped(node=99, compiler=42)
        manifest['distribution'] = {'portable': True}
        inspector = dict(fx.component('frontend', manifest), name='inspector-ui')
        for key, filename in [('manifest', 'package.json'), ('lock', 'package-lock.json')]:
            path = 'static-inspection/workbench/ui/' + filename
            fx.write(path, (fx.checkout / inspector[key]).read_bytes())
            inspector[key] = path
        manifest['components'].append(inspector)
        fx.save(manifest)
        fx.write('src/main.rs', b'fn main() {}\n')
        fx.write('migrations/001.sql', b'SELECT 1;\n')
        for rel in ('frontend', 'static-inspection/workbench/ui'):
            fx.write(rel + '/index.html', b'<main>app</main>\n')
        runner = fx.world.runner
        self.builds = []
        self.fail_inspector = False
        self.edit_frontend = False
        self.check_staging = True
        self.stages = set()
        image_ids = {}
        for name in ('compiler', 'node'):
            image = fx.component(name, manifest)
            image_ids[name] = fx.world.image_id(f"{image['repository']}@{image['digest']}")

        def run(argv, timeout):
            if '--entrypoint' not in argv or argv[argv.index('--entrypoint') + 1] != 'sh':
                return runner(argv, timeout)
            mounts = {a.split('dst=')[1].split(',')[0]: Path(a.split('src=')[1].split(',')[0])
                      for a in argv if a.startswith('type=bind,')}
            if '/out' not in mounts:
                return runner(argv, timeout)
            self.assertEqual('none', argv[argv.index('--network') + 1])
            if '/deps' in mounts:
                label = mounts['/deps'].name
                self.assertIn(image_ids['node'], argv)
                if self.check_staging:
                    relative = mounts['/deps'].relative_to(pointer.parent / 'staging')
                    self.assertEqual(('components', label), relative.parts[1:])
                    self.stages.add(relative.parts[0])
                filename, content = 'index.html', (mounts['/work'] / 'index.html').read_bytes()
                (mounts['/out'] / 'app.js').write_bytes(b'compiled application')
            else:
                self.assertIn(image_ids['compiler'], argv)
                label, filename, content = 'shell', 're-kit-workspace', b'compiled shell'
                if self.edit_frontend:
                    fx.write('frontend/index.html', b'edited during compilation\n')
            self.builds.append(label)
            if self.fail_inspector and label == 'inspector-ui':
                return 1, 'incompatible inspector dependency'
            output = mounts['/out'] / filename
            output.write_bytes(content)
            output.chmod(0o755)
            return 0, ''

        self.m = fx.maintenance()
        self.m.docker.runner = run

    def test_last_interface_build_failure_preserves_installed_set(self):
        self.fail_inspector = True
        result = self.m.update(wait=True)
        self.assertEqual('failed', result['state'], result['message'])
        self.assertIn('incompatible inspector dependency', result['message'])
        self.assertEqual(['shell', 'frontend', 'inspector-ui'], self.builds)
        self.assertEqual(1, len(self.stages))
        self.assertEqual(self.previous, self.pointer.read_bytes())
        self.assertEqual({self.initial}, {p.name for p in (self.pointer.parent / 'sets').iterdir()})

    def install(self):
        result = self.m.update(wait=True)
        self.assertEqual('updated', result['state'], result['message'])
        self.check_staging = False

    def test_build_uses_the_source_bytes_that_selected_the_cache_key(self):
        self.edit_frontend = True
        original = (self.fx.checkout / 'frontend/index.html').read_bytes()
        self.install()
        self.fx.write('frontend/index.html', original)
        built = self.m.installed_application()
        self.assertEqual(original, (built / 'frontend/index.html').read_bytes())

    def test_a_cached_interface_missing_its_javascript_is_not_launched(self):
        self.install()
        build = self.m.installed_application()
        (build / 'inspector-ui/app.js').unlink()
        with self.assertRaises(mt.MaintenanceError):
            self.m.installed_application()

    def test_unused_checkout_lock_does_not_replace_a_working_build(self):
        self.install()
        first = self.m.installed_application()
        self.fx.write('frontend/package-lock.json', b'{}\n')
        self.assertEqual(first, self.m.installed_application())
        self.assertEqual(['shell', 'frontend', 'inspector-ui'], self.builds)

    def test_running_updater_does_not_label_outputs_with_an_unloaded_recipe(self):
        from build_desk import workspace_build

        self.install()
        previous = self.pointer.read_bytes()
        replacement = self.fx.checkout / 'replacement_build.py'
        replacement.write_text('BUILD_COMMAND = "different build"\n', encoding='utf-8')
        with patch.object(workspace_build, '__file__', str(replacement)):
            result = self.m.update(wait=True)
        self.assertEqual('failed', result['state'], result['message'])
        self.assertIn('restart', result['message'].lower())
        self.assertEqual(previous, self.pointer.read_bytes())
        self.assertEqual(['shell', 'frontend', 'inspector-ui'], self.builds)


class UpstreamUpdates(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.release = '1llu50ry/Decompile'
        self.channel = f'https://api.github.com/repos/{self.release}'
        self.fx.manifest['components'].append({
            'name': 'decompile', 'family': 'release-metadata', 'scope': 'runtime', 'version': 'Latest stable release',
            'repository': self.release, 'track': True,
        })
        self.fx.save()
        self.m = self.fx.maintenance()

    def publish(self, tag, commit, prerelease=False):
        self.fx.world.downloads[self.channel + '/releases/latest'] = dump({'tag_name': tag, 'draft': False, 'prerelease': prerelease})
        self.fx.world.downloads[self.channel + '/git/ref/tags/' + tag] = dump({'object': {'type': 'commit', 'sha': commit}})

    def test_new_release_without_checkout_change_then_unchanged_and_failure(self):
        before = (self.fx.checkout / mt.MANIFEST_NAME).read_bytes()
        self.publish('v1.0.0', '1' * 40)
        initial = self.m.bootstrap(wait=True)
        self.assertEqual('updated', initial['state'], initial['message'])
        self.publish('v1.1.0', '2' * 40)
        newer = self.m.update(wait=True)
        self.assertEqual('updated', newer['state'], newer['message'])
        self.assertNotEqual(initial['set'], newer['set'])
        pointer = self.fx.root / 'dependencies' / 'active.json'
        snapshot = pointer.read_bytes()
        record = json.loads((pointer.parent / 'sets' / newer['set'] / 'receipt.json').read_text())['components']['decompile']
        self.assertEqual(('v1.1.0', '2' * 40), (record['tag'], record['commit']))
        self.assertIsNone(self.m.asset_path('decompile'))
        self.assertEqual('current', self.m.update(wait=True)['state'])
        self.assertEqual(snapshot, pointer.read_bytes())
        self.publish('v1.2.0', '3' * 40, prerelease=True)
        refused = self.m.update(wait=True)
        self.assertEqual('failed', refused['state'], refused['message'])
        self.assertEqual(snapshot, pointer.read_bytes())
        self.assertEqual(before, (self.fx.checkout / mt.MANIFEST_NAME).read_bytes())
        urls = {c[1] for c in self.fx.world.calls if c[0] == 'fetch' and self.release in c[1]}
        self.assertEqual({self.channel + tail for tail in ('/releases/latest','/git/ref/tags/v1.0.0','/git/ref/tags/v1.1.0')}, urls)

    def test_moved_release_tag_cannot_replace_the_recorded_artifact(self):
        self.publish('v1.0.0', '1' * 40)
        result = self.m.bootstrap(wait=True)
        self.assertEqual('updated', result['state'], result['message'])
        self.publish('v1.0.0', '2' * 40)
        refused = self.m.update(wait=True)
        self.assertEqual('failed', refused['state'])
        self.assertEqual(result['set'], refused['set'])

    def test_shipped_policy_has_no_fixed_selection_for_ordinary_dependencies(self):
        manifest = mt.load_manifest(REPO / mt.MANIFEST_NAME)
        for name in ('compiler','native-compiler','node','python','inspector-worker','workspace-cargo','inspector-ui','frontend','rea','rust-anything','decompile'):
            with self.subTest(component=name):
                self.assertTrue(manifest.by_name[name].get('track'))
        for name in ('rea','rust-anything','decompile'):
            self.assertNotIn('commit', manifest.by_name[name])
            self.assertNotIn('tag', manifest.by_name[name])
        for name in ('compiler','python','node','inspector-worker'):
            self.assertNotIn('digest', manifest.by_name[name])
        for name in ('workspace-cargo','inspector-ui','frontend'):
            self.assertNotIn('lock_sha256', manifest.by_name[name])


if __name__ == '__main__':
    unittest.main()
