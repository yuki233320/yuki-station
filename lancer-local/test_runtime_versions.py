"""Version provenance uses real source files and local workflow fixtures only."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import Store
from runtime_versions import SERVICE_BUILD, build_reference, source_matches, source_snapshot
from studio import Studio
from test_studio_business import BusinessClient


class SourceVersionTests(unittest.TestCase):
    def test_source_snapshot_is_portable_and_detects_code_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'service.py').write_text('answer = 1\n')
            before = source_snapshot(root)
            (root / 'test_service.py').write_text('a test is not runtime code')
            (root / 'README.md').write_text('documentation only')
            self.assertEqual(source_snapshot(root), before)
            self.assertEqual(list(before['files']), ['service.py'])
            (root / 'service.py').write_text('answer = 2\n')
            self.assertFalse(source_matches(before, root))
            self.assertNotEqual(source_snapshot(root)['files']['service.py'], before['files']['service.py'])

    def test_running_service_keeps_startup_snapshot_until_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(Path(__file__).with_name('runtime_versions.py'), root / 'runtime_versions.py')
            (root / 'service.py').write_text('answer = 1\n')
            script = '''
import json
from pathlib import Path
import runtime_versions as versions
before = versions.SERVICE_BUILD.copy()
Path('service.py').write_text('answer = 2\\n')
print(json.dumps({'before': before, 'after': versions.SERVICE_BUILD,
                  'matches': versions.source_matches(versions.SERVICE_BUILD),
                  'disk': versions.source_snapshot()}))
'''
            first = subprocess.run([sys.executable, '-B', '-c', script], cwd=root,
                                   capture_output=True, text=True, check=True, timeout=10)
            record = json.loads(first.stdout)
            self.assertEqual(record['before'], record['after'])
            self.assertFalse(record['matches'])
            second = subprocess.run([sys.executable, '-B', '-c',
                                     'import json,runtime_versions as v;print(json.dumps(v.SERVICE_BUILD))'],
                                    cwd=root, capture_output=True, text=True, check=True, timeout=10)
            restarted = json.loads(second.stdout)
            self.assertEqual(restarted['fingerprint'], record['disk']['fingerprint'])
            self.assertNotEqual(restarted['fingerprint'], record['before']['fingerprint'])


class RunVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = BusinessClient()
        self.studio = Studio(Store(Path(self.temp.name), seed=False), self.client)
        self.pid = self.studio.create_project('版本追溯', '验证真实存档', '')['id']
        self.session = self.studio.create_session(self.pid, '报告', '整理一份简报', 'general')

    def tearDown(self):
        for thread in self.studio.threads.values():
            thread.join(10)
        self.temp.cleanup()

    def execute(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume)
        self.studio.threads[self.session['id']].join(10)
        self.assertFalse(self.studio.threads[self.session['id']].is_alive())
        return self.studio.load(self.pid, self.session['id'])

    def test_run_context_and_export_preserve_same_startup_build(self):
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][0]
        self.assertEqual(run['runtimeBuild'], SERVICE_BUILD)
        self.assertIs(type(run['sourceMatchesRuntimeBuild']), bool)
        self.assertEqual(self.studio.index()['runtimeBuild'], SERVICE_BUILD)
        with zipfile.ZipFile(io.BytesIO(self.studio.export(self.pid, session['id']))) as archive:
            saved = json.loads(archive.read('会话与执行记录.json'))
            self.assertEqual(saved['runs'][0]['runtimeBuild'], run['runtimeBuild'])
            contexts = [json.loads(archive.read(name)) for name in archive.namelist()
                        if name.startswith('contexts/') and name.endswith('.json')]
        self.assertTrue(contexts)
        for context in contexts:
            provenance = context['memory']['runtimeProvenance']
            self.assertEqual(provenance['runBuild'], build_reference(run['runtimeBuild']))
            self.assertEqual(provenance['serviceBuild'], build_reference(SERVICE_BUILD))
            self.assertEqual(provenance['runSourceMatchesRuntimeBuild'], run['sourceMatchesRuntimeBuild'])

    def test_resumed_historical_run_does_not_receive_current_version(self):
        self.client.fail_once = ('plan', None, 'opus', 0)
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        run = session['runs'][0]
        del run['runtimeBuild']
        del run['sourceMatchesRuntimeBuild']
        self.studio.save(session)
        resumed = self.execute(resume=True)
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        self.assertNotIn('runtimeBuild', resumed['runs'][0])
        self.assertNotIn('sourceMatchesRuntimeBuild', resumed['runs'][0])
        for call in self.client.calls[1:]:
            provenance = call['memory']['runtimeProvenance']
            self.assertIsNone(provenance['runBuild'])
            self.assertIsNone(provenance['runSourceMatchesRuntimeBuild'])
            self.assertEqual(provenance['serviceBuild'], build_reference(SERVICE_BUILD))

    def test_service_restart_marks_active_evidence_review_interrupted(self):
        session = self.execute()
        run = session['runs'][0]
        previous = {'id': 'previous-review', 'status': 'failed', 'error': 'missing result'}
        run['evidenceReviews'] = [previous, {'id': 'active-review', 'status': 'running'}]
        session['status'] = run['status'] = 'running'
        self.studio.save(session)
        restored = Studio(self.studio.store, self.client).load(self.pid, session['id'])
        self.assertEqual(restored['status'], 'interrupted')
        reviews = restored['runs'][0]['evidenceReviews']
        self.assertEqual(reviews[0], previous)
        self.assertEqual(reviews[1]['status'], 'interrupted')
        self.assertEqual(reviews[1]['error'], restored['error'])
        self.assertTrue(reviews[1]['interruptedAt'])


if __name__ == '__main__':
    unittest.main()
