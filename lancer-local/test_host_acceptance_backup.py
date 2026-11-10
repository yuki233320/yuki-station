"""Restored host evidence is history, never restored execution authority."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import Store
from project_backup import backup_project, restore_project
from studio import Studio
from test_studio_business import BusinessClient


SCRIPT = '''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--workspace');a=p.parse_args()
passed=(Path(a.workspace)/'brief.md').is_file()
print(json.dumps({'accepted':passed,'criteria':[{'criterionId':'HC-1','status':'passed' if passed else 'failed'}]}))
'''


@unittest.skipUnless(os.name == 'posix', 'host process-group execution requires POSIX')
class HostAcceptanceBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = BusinessClient()
        self.studio = Studio(Store(self.root / 'original', seed=False), self.client)
        self.studios = [self.studio]
        self.pid = self.studio.create_project('宿主回执恢复', '区分历史证据和本机授权')['id']
        self.script = self.root / 'approved.py'
        self.script.write_text(SCRIPT)
        self.registration = {'id': 'local-check', 'name': 'Explicit local check',
                             'scriptPath': str(self.script), 'files': ['brief.md'],
                             'criteria': [{'id': 'HC-1', 'description': 'Required artifact exists'}],
                             'timeoutSeconds': 5}
        self.entry = self.studio.host_acceptance.register(self.registration)
        self.studio.set_host_acceptance(self.pid, self.studio.meta(self.pid)['version'], [self.entry['id']])
        self.session = self.studio.create_session(self.pid, '任务', '整理资料并交付 brief.md', 'general')

    def tearDown(self):
        for studio in self.studios:
            for thread in studio.threads.values():
                if thread.ident is not None:
                    thread.join(15)
        self.tmp.cleanup()

    def execute(self, studio=None, resume=False):
        studio = studio or self.studio
        studio.start(self.pid, self.session['id'], resume=resume)
        thread = studio.threads[self.session['id']]
        thread.join(15)
        self.assertFalse(thread.is_alive())
        return studio.load(self.pid, self.session['id'])

    def restore(self):
        archive = self.root / 'backup.zip'
        backup_project(self.studio.store.root, self.pid, archive)
        target = self.root / 'restored'
        restore_project(archive, target)
        client = BusinessClient()
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('restore must not execute a checker')):
            fresh = Studio(Store(target, seed=False), client)
        self.studios.append(fresh)
        return fresh, client, archive

    def test_history_exports_and_restores_but_local_registration_and_script_do_not(self):
        completed = self.execute()
        self.assertEqual(completed['status'], 'completed', completed.get('error'))
        old_history = deepcopy(completed['runs'][-1]['hostAcceptanceHistory'])
        fresh, client, archive_path = self.restore()
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertFalse(any('host-acceptance-registry.json' in name or 'host-acceptance-scripts/' in name for name in names))
            self.assertTrue(any('/host-acceptance/' in name and name.endswith('/receipt.json') for name in names))
            self.assertTrue(any('/host-acceptance/' in name and name.endswith('/stdout.log') for name in names))
        restored = fresh.load(self.pid, self.session['id'])
        self.assertEqual(restored['runs'][-1]['hostAcceptanceHistory'], old_history)
        self.assertEqual(fresh.host_acceptance.info(), [])
        self.assertFalse((fresh.store.root / 'host-acceptance-registry.json').exists())
        self.assertFalse((fresh.store.root / 'host-acceptance-scripts').exists())
        self.assertEqual(client.calls, [])
        with zipfile.ZipFile(io.BytesIO(fresh.export(self.pid, self.session['id']))) as archive:
            exported = json.loads(archive.read('会话与执行记录.json'))
            self.assertEqual(exported['runs'][-1]['hostAcceptanceHistory'], old_history)
        next_session = fresh.create_session(self.pid, '新任务', '再次生成项目资料', 'general')
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must require local registration')):
            with self.assertRaises(ValueError):
                fresh.start(self.pid, next_session['id'])
        self.assertEqual(client.calls, [])
        self.assertEqual(fresh.load(self.pid, next_session['id'])['status'], 'ready')

    def test_unpublished_intent_needs_local_reregistration_and_reuses_verified_receipt(self):
        original = self.studio.store.commit
        def fail_before_publication(*args, **kwargs):
            raise OSError('injected before HEAD publication')
        self.studio.store.commit = fail_before_publication
        failed = self.execute()
        self.studio.store.commit = original
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue(failed['runs'][-1]['commitIntent'])
        self.assertNotIn('mergedRevision', failed['runs'][-1])
        fresh, client, _ = self.restore()
        revision = fresh.store.current(self.pid)[0]['revision']
        with patch('host_acceptance.run_check', side_effect=AssertionError('resume may only verify saved receipt')):
            with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not dispatch')):
                rejected = self.execute(fresh, resume=True)
        self.assertEqual(rejected['status'], 'failed')
        self.assertEqual(fresh.store.current(self.pid)[0]['revision'], revision)
        self.assertEqual(client.calls, [])
        # An explicit local registration with the same frozen identity grants
        # permission to verify the existing receipt, not to silently rerun it.
        fresh.host_acceptance.register(self.registration)
        client.available = lambda: []
        with patch('host_acceptance.run_check', side_effect=AssertionError('must not rerun saved grader')):
            with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not dispatch')):
                completed = self.execute(fresh, resume=True)
        self.assertEqual(completed['status'], 'completed', completed.get('error'))
        self.assertNotEqual(fresh.store.current(self.pid)[0]['revision'], revision)
        self.assertEqual(client.calls, [])
        self.assertEqual(completed['runs'][-1]['hostAcceptanceHistory'], failed['runs'][-1]['hostAcceptanceHistory'])

    def test_published_head_recovery_needs_no_registry_checker_or_model_connection(self):
        original = self.studio.store.commit
        published = []
        def lose_return(*args, **kwargs):
            result = original(*args, **kwargs)
            published.append(result['revision'])
            raise OSError('injected after HEAD publication')
        self.studio.store.commit = lose_return
        failed = self.execute()
        self.studio.store.commit = original
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(len(published), 1)
        self.assertNotIn('mergedRevision', failed['runs'][-1])
        fresh, client, _ = self.restore()
        self.assertEqual(fresh.host_acceptance.info(), [])
        client.available = lambda: []
        with patch('host_acceptance.run_check', side_effect=AssertionError('published delivery must not rerun')):
            with patch('host_acceptance.verify_receipt', side_effect=AssertionError('published HEAD already owns delivery fact')):
                with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not dispatch')):
                    completed = self.execute(fresh, resume=True)
        self.assertEqual(completed['status'], 'completed', completed.get('error'))
        self.assertEqual(completed['runs'][-1]['mergedRevision'], published[0])
        self.assertEqual(fresh.store.current(self.pid)[0]['revision'], published[0])
        self.assertEqual(len(fresh.meta(self.pid)['memory']), 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(completed['runs'][-1]['hostAcceptanceHistory'], failed['runs'][-1]['hostAcceptanceHistory'])


if __name__ == '__main__':
    unittest.main()
