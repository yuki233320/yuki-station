import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import Store
from studio import Studio
from project_backup import backup_project, restore_project
from service_lock import DataDirectoryInUseError, ServiceDataLock
from session_state import read_dispatches
from test_studio_business import BusinessClient


class ProjectBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'original'
        self.studio = Studio(Store(self.root, seed=False))
        self.pid = self.studio.create_project('恢复验收', '保存项目、历史、岗位和证据', '保留真实证据')['id']
        self.session = self.studio.create_session(self.pid, '已有会话', '继续独立上下文', 'general')
        self.archive = self.base / 'backup.zip'

    def test_full_restore_preserves_history_context_and_export_without_native_authority(self):
        store = self.studio.store
        before, _ = store.current(self.pid)
        first = before['revision']
        store.save_file(self.pid, 'deliverable.txt', b'actual artifact', first)
        context = self.studio.project_dir(self.pid) / self.session['id'] / 'contexts' / 'aabbccddeeff.json'
        self.studio._write(context, {'id': 'aabbccddeeff', 'messages': [{'role': 'user', 'content': 'retained context'}]})
        self.studio.client.configure('deepseek', 'sk-' + 'x' * 30)
        (self.root / 'native-project.json').write_text('{"projectId":"machine-only"}')
        (self.root / 'work-policy.json').write_text('{"version":1}')
        expected_export = self.studio.export(self.pid, self.session['id'])
        manifest = backup_project(self.root, self.pid, self.archive)
        restored = self.base / 'restored'
        restore_project(self.archive, restored)
        fresh = Studio(Store(restored, seed=False))
        self.assertEqual(fresh.load(self.pid, self.session['id']), self.studio.load(self.pid, self.session['id']))
        self.assertEqual(fresh.meta(self.pid), self.studio.meta(self.pid))
        self.assertEqual(fresh.store.view(self.pid)['revision'], store.view(self.pid)['revision'])
        self.assertTrue(fresh.store.revision_dir(self.pid, first).exists())
        self.assertEqual(fresh.store.current(self.pid)[1].joinpath('deliverable.txt').read_bytes(), b'actual artifact')
        self.assertFalse((restored / 'native-project.json').exists())
        self.assertEqual(fresh.client.available(), [])
        for row in manifest['files']:
            self.assertEqual(hashlib.sha256((restored / row['path']).read_bytes()).hexdigest(), row['sha256'])
        with zipfile.ZipFile(io.BytesIO(expected_export)) as old, zipfile.ZipFile(io.BytesIO(fresh.export(self.pid, self.session['id']))) as new:
            self.assertEqual({n: old.read(n) for n in old.namelist()}, {n: new.read(n) for n in new.namelist()})

    def test_active_task_and_existing_destination_are_rejected(self):
        self.session['status'] = 'running'
        self.studio.save(self.session)
        with self.assertRaisesRegex(ValueError, 'active tasks'):
            backup_project(self.root, self.pid, self.archive)
        self.assertFalse(self.archive.exists())
        self.session['status'] = 'cancelled'
        self.studio.save(self.session)
        backup_project(self.root, self.pid, self.archive)
        with self.assertRaisesRegex(ValueError, 'never overwritten'):
            restore_project(self.archive, self.root)

    def test_empty_stage_directories_survive_and_restored_project_can_continue(self):
        store = self.studio.store
        pid = store.create('新工作流', '恢复后实际交接')
        backup_project(self.root, pid, self.archive)
        restore_project(self.archive, self.base / 'continued')
        fresh = Store(self.base / 'continued', seed=False)
        state = fresh.view(pid)
        self.assertTrue(Path(state['cwd']).is_dir())
        state = fresh.complete(pid, state['revision'])
        state = fresh.handoff(pid, state['revision'])
        state = fresh.complete(pid, state['revision'])
        self.assertEqual(state['active'], 1)
        self.assertEqual(state['stages'][1]['status'], 'done')

    def test_tampering_and_traversal_do_not_publish_partial_restore(self):
        backup_project(self.root, self.pid, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            entries = {n: source.read(n) for n in source.namelist()}
        for mode in ('tampered', 'traversal'):
            changed = dict(entries)
            if mode == 'tampered':
                changed[self.pid + '/HEAD'] = b'000000000000'
            else:
                manifest = json.loads(changed['BACKUP_MANIFEST.json'])
                manifest['files'][0]['path'] = self.pid + '/../escape'
                changed['BACKUP_MANIFEST.json'] = json.dumps(manifest).encode()
            path = self.base / (mode + '.zip')
            with zipfile.ZipFile(path, 'w') as target:
                for name, data in changed.items():
                    target.writestr(name, data)
            destination = self.base / mode
            with self.assertRaises(ValueError):
                restore_project(path, destination)
            self.assertFalse(destination.exists())
        self.assertFalse(list(self.base.glob('.yuki-restore-*')))

    def test_backup_exports_authoritative_sessions_without_stale_json_or_other_projects(self):
        authoritative = self.studio.load(self.pid, self.session['id'])
        stale = dict(authoritative, title='obsolete import original', status='running')
        original = self.studio.path(self.pid, self.session['id'])
        original.write_text(json.dumps(stale))
        other_pid = self.studio.create_project('another project', 'other-project-private-marker')['id']
        other = self.studio.create_session(other_pid, 'private session', 'other-project-private-marker', 'general')
        manifest = backup_project(self.root, self.pid, self.archive)
        name = 'project-sessions/' + self.pid + '/' + self.session['id'] + '.json'
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(json.loads(archive.read(name)), authoritative)
            self.assertEqual(archive.namelist().count(name), 1)
            self.assertNotIn(other['id'], '\n'.join(archive.namelist()))
            self.assertFalse(any(b'other-project-private-marker' in archive.read(item) for item in archive.namelist()))
            # No global database/WAL is copied; only this project's logical
            # session records and original evidence enter the portable package.
            self.assertTrue(all(item == 'BACKUP_MANIFEST.json' or item.startswith((self.pid + '/', 'project-sessions/' + self.pid + '/'))
                                or item in ('model-registry.json', 'work-policy.json') for item in archive.namelist()))
        self.assertEqual(json.loads(original.read_text()), stale)
        self.assertEqual(manifest['sessionFormat'], 'yuki-session-logical-export/v1')
        restored = self.base / 'logical-restore'
        restore_project(self.archive, restored)
        fresh = Studio(Store(restored, seed=False))
        self.assertEqual(fresh.load(self.pid, self.session['id']), authoritative)
        self.assertEqual([s['id'] for s in fresh.list_sessions()], [self.session['id']])

    def test_backup_rejects_live_service_directory_even_with_no_running_session(self):
        with ServiceDataLock(self.root):
            with self.assertRaises(DataDirectoryInUseError):
                backup_project(self.root, self.pid, self.archive)
        self.assertFalse(self.archive.exists())

    def test_running_nested_run_cannot_hide_behind_terminal_session(self):
        self.session.update(status='completed', runs=[{'id': 'run', 'status': 'running', 'nodes': []}])
        self.studio.save(self.session)
        with self.assertRaisesRegex(ValueError, 'active tasks'):
            backup_project(self.root, self.pid, self.archive)
        self.assertFalse(self.archive.exists())

    def test_unsettled_dispatch_blocks_backup_even_if_session_is_idle(self):
        with patch('project_backup.pending_dispatches', return_value=[{'status': 'queued'}]):
            with self.assertRaisesRegex(ValueError, 'Unsettled dispatches'):
                backup_project(self.root, self.pid, self.archive)
        self.assertFalse(self.archive.exists())

    def test_dispatch_audit_survives_logical_restore_without_execution_authority(self):
        self.studio.client = BusinessClient()
        self.studio.start(self.pid, self.session['id'])
        worker = self.studio.threads[self.session['id']]
        worker.join(10)
        self.assertFalse(worker.is_alive())
        saved = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        dispatches = read_dispatches(self.studio.root, self.pid)
        self.assertTrue(dispatches)
        self.assertFalse(any(item['status'] in ('queued', 'running') for item in dispatches))
        backup_project(self.root, self.pid, self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            exported = json.loads(archive.read('project-sessions/' + self.pid + '/dispatch-state.json'))
            self.assertEqual(exported['schema'], 'yuki-dispatch-export/v1')
            self.assertEqual(exported['dispatches'], dispatches)
        restored = self.base / 'restored-dispatch'
        restore_project(self.archive, restored)
        fresh = Studio(Store(restored, seed=False))
        self.assertEqual(fresh.load(self.pid, self.session['id']), saved)
        self.assertEqual(read_dispatches(fresh.root, self.pid), [dict(item, owner=None) for item in dispatches])
        self.assertEqual(fresh.threads, {})
        self.assertEqual(fresh.client.available(), [])

    def test_restore_rejects_active_or_misbound_dispatch_before_publication(self):
        self.session.update(status='completed', runs=[{
            'id': '111111111111', 'status': 'completed', 'nodes': [],
            'executionAttempts': [{'id': '222222222222', 'number': 1, 'reason': 'start',
                                   'status': 'completed', 'startedAt': 't1', 'endedAt': 't2'}],
        }])
        self.studio.save(self.session)
        backup_project(self.root, self.pid, self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        state_name = 'project-sessions/' + self.pid + '/dispatch-state.json'
        for case, change in (
            ('active', {'status': 'queued'}),
            ('other_project', {'projectId': '333333333333'}),
            ('other_session', {'sessionId': '444444444444'}),
            ('missing_attempt', {'attemptId': '555555555555'}),
        ):
            with self.subTest(case=case):
                contents = dict(entries)
                state = json.loads(contents[state_name])
                self.assertEqual(len(state['dispatches']), 1)
                state['dispatches'][0].update(change)
                data = json.dumps(state).encode()
                contents[state_name] = data
                manifest = json.loads(contents['BACKUP_MANIFEST.json'])
                row = next(item for item in manifest['files'] if item['path'] == state_name)
                row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                contents['BACKUP_MANIFEST.json'] = json.dumps(manifest).encode()
                archive_path = self.base / (case + '.zip')
                with zipfile.ZipFile(archive_path, 'w') as archive:
                    for name, content in contents.items():
                        archive.writestr(name, content)
                target = self.base / ('restore-' + case)
                with self.assertRaises(ValueError):
                    restore_project(archive_path, target)
                self.assertFalse(target.exists())
                self.assertFalse(list(self.base.glob('.yuki-restore-*')))

    def test_legacy_backup_is_read_only_and_restores_through_import(self):
        legacy_root = self.base / 'legacy'
        store = Store(legacy_root, seed=False)
        pid = store.create('legacy project', 'read-only import original')
        sid = 'aabbccddeeff'
        directory = legacy_root / 'project-sessions' / pid
        directory.mkdir(parents=True)
        legacy = {'id': sid, 'projectId': pid, 'status': 'ready', 'runs': [],
                  'version': 7, 'title': 'legacy record', 'updated': 'original-time'}
        path = directory / (sid + '.json')
        path.write_text(json.dumps(legacy))
        before = path.read_bytes()
        manifest = backup_project(legacy_root, pid, self.archive)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set((legacy_root / 'project-sessions').iterdir()), {directory})
        restored = self.base / 'restored-legacy'
        restore_project(self.archive, restored)
        self.assertEqual(Studio(Store(restored, seed=False)).load(pid, sid), legacy)
        self.assertTrue(any(row['path'].endswith(sid + '.json') for row in manifest['files']))


if __name__ == '__main__':
    unittest.main()
