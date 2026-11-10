import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from package_source import HERE, PREFIX, build_archive


class SourceArchiveTests(unittest.TestCase):
    @unittest.skipUnless((HERE.parent / 'lancer-codex-bridge/bridge.mjs').is_file(),
                         'Optional bridge sources are absent')
    def test_archive_has_verified_sources_and_no_workspace_state(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'source.zip'
            build_archive(target)
            with zipfile.ZipFile(target) as archive:
                manifest = json.loads(archive.read(PREFIX + '/SOURCE_MANIFEST.json'))
                names = archive.namelist()
                self.assertEqual(len(names), len(manifest['files']) + 1)
                self.assertIn(PREFIX + '/lancer-local/app.py', names)
                self.assertIn(PREFIX + '/lancer-codex-bridge/bridge.mjs', names)
                self.assertIn(PREFIX + '/lancer-local/public/icons/LICENSE', names)
                self.assertIn(PREFIX + '/lancer-local/work/reuse_previous_visuals.py', names)
                for row in manifest['files']:
                    content = archive.read(PREFIX + '/' + row['path'])
                    self.assertEqual(len(content), row['bytes'])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), row['sha256'])
                    self.assertNotIn(str(Path.home()).encode(), content)
                for name in names:
                    parts = Path(name).parts
                    self.assertTrue(name.startswith(PREFIX + '/'))
                    if 'work' in parts:
                        self.assertEqual(name, PREFIX + '/lancer-local/work/reuse_previous_visuals.py')
                    self.assertFalse(set(parts) & {'.lancer-data', 'workspace-data',
                                                  '__pycache__', 'receipts', 'outputs',
                                                  'schemas', 'native-project.json',
                                                  'model-registry.json', 'auth.json'})
                self.assertFalse(any(name.endswith(('/research.html', '/comments.html', '/comments.js'))
                                     for name in names))
                for page in ('index.html', 'live.html'):
                    html = archive.read(PREFIX + '/lancer-local/public/' + page)
                    self.assertNotIn(b'href="/research.html"', html)
                    self.assertNotIn(b'href="/comments.html"', html)

    def test_bridge_can_be_omitted_without_removing_the_web_app(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'api.zip'
            build_archive(target, include_bridge=False)
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                self.assertIn(PREFIX + '/lancer-local/studio_native.py', names)
                self.assertIn(PREFIX + '/lancer-local/public/studio.html', names)
                self.assertFalse(any('/lancer-codex-bridge/' in name for name in names))
                manifest = json.loads(archive.read(PREFIX + '/SOURCE_MANIFEST.json'))
                self.assertFalse(manifest['optionalBridge'])


if __name__ == '__main__':
    unittest.main()
