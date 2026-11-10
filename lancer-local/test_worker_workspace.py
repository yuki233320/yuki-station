"""Real bounded file tools and trusted local tests; no model/network calls."""
from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Conflict, Store
from live import Cancelled
from studio import Studio
from worker_policy import bind_role
from worker_workspace import WorkerWorkspace


class WorkerWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.studio = Studio(Store(self.base / 'data', seed=False))
        self.pid = self.studio.create_project('文件工具验证', '冻结副本、范围与真实测试')['id']
        source = {'solution.py': 'VALUE = 0\n', 'docs/unloaded.txt': 'not in model context\na.*b literal\n',
                  'dep.txt': 'old project dependency\n'}
        revision = self.studio.store.current(self.pid)[0]['revision']
        view = self.studio.import_files(self.pid, [{'path': name, 'data': base64.b64encode(text.encode()).decode()}
                                                   for name, text in source.items()], revision)
        self.session = self.studio.create_session(self.pid, '任务', '修改solution.py并测试', 'general')
        self.marker = self.base / 'invocations'
        script = self.base / 'trusted.py'
        script.write_text("import argparse,json,sys\nfrom pathlib import Path\n"
                          "p=argparse.ArgumentParser();p.add_argument('--workspace');a=p.parse_args()\n"
                          + 'counter=Path(' + repr(str(self.marker)) + ')\n'
                          "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n"
                          "passed=(Path(a.workspace)/'solution.py').read_text()=='VALUE = 1\\n'\n"
                          "print(json.dumps({'accepted':passed,'criteria':[{'id':'C1','status':'passed' if passed else 'failed'}]}))\n"
                          "sys.exit(0 if passed else 1)\n")
        entry = self.studio.host_acceptance.register({'id': 'fixed-test', 'name': 'Frozen local test',
            'scriptPath': str(script), 'files': ['solution.py'], 'criteria': [{'id': 'C1', 'description': 'VALUE is one'}], 'timeoutSeconds': 5})
        checks = self.studio.host_acceptance.snapshot([entry['id']])
        self.policy = {'version': 1, 'roleGrants': {'coder': {'enabled': True, 'checkIds': [entry['id']]}}}
        self.module = {'id': 'code', 'roleId': 'coder', 'scope': ['solution.py', 'new/'], 'dependsOn': ['upstream']}
        self.binding = bind_role(self.module, self.policy, checks)
        self.module['workerBinding'] = deepcopy(self.binding)
        self.run = {'id': '123456abcdef', 'projectRevision': view['revision'],
                    'workerToolsVersion': 1, 'workerTools': self.policy, 'hostAcceptanceChecks': checks}
        self.node = {'id': 'build-code'}
        self.contract = {'id': 'fixed-contract', 'moduleId': 'code', 'scope': self.module['scope'],
                         'workerToolsVersion': 1, 'workerBinding': deepcopy(self.binding), 'fingerprint': 'frozen-fingerprint'}
        self.shared = {'module': self.module, 'executionContract': self.contract,
                       'upstream': {'upstream': {'files': [{'path': 'dep.txt', 'content': 'actual direct upstream\n'}]}}}
        self.workspace = self.make()
        self.project_workspace = self.studio.store.revision_dir(self.pid, self.run['projectRevision']) / 'workspace'

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, shared=None, binding=None):
        return WorkerWorkspace(self.studio, self.session, self.run, self.node,
                               self.shared if shared is None else shared, self.binding if binding is None else binding)

    def test_constructor_and_readonly_missing_workspace_do_not_create_files(self):
        before = set(self.studio.store.root.rglob('*'))
        other = self.make({'module': self.module, 'executionContract': self.contract})
        self.assertFalse(other.root.exists())
        with self.assertRaises(ValueError): other.verify_inputs()
        self.assertEqual(set(self.studio.store.root.rglob('*')), before)

    def test_reads_full_frozen_project_and_direct_upstream_without_touching_originals(self):
        initial = self.workspace.prepare()
        names = [row['path'] for row in self.workspace.list()['files']]
        self.assertIn('docs/unloaded.txt', names)
        self.assertIn('not in model context', self.workspace.read('docs/unloaded.txt')['content'])
        self.assertEqual(self.workspace.read('dep.txt')['content'], 'actual direct upstream\n')
        self.assertFalse(self.workspace.read('dep.txt')['writable'])
        self.assertEqual((self.project_workspace / 'dep.txt').read_text(), 'old project dependency\n')
        readonly = self.make({'module': self.module, 'executionContract': self.contract})
        readonly.verify_inputs()
        self.assertEqual(readonly.fingerprint(), initial['workspaceFingerprint'])
        self.assertEqual(readonly.files(), [{'path': 'solution.py', 'content': 'VALUE = 0\n'}])

    def test_private_files_are_filtered_and_non_utf8_is_explicitly_unreadable(self):
        (self.project_workspace / '.env').write_text('private')
        (self.project_workspace / 'credentials.txt').write_text('sk-' + 'x' * 24)
        (self.project_workspace / 'opaque.bin').write_bytes(b'\xff\x00binary')
        self.workspace.prepare()
        rows = {row['path']: row for row in self.workspace.list()['files']}
        self.assertNotIn('.env', rows); self.assertNotIn('credentials.txt', rows)
        self.assertFalse(rows['opaque.bin']['readable'])
        self.assertEqual(self.workspace.read('opaque.bin')['reason'], 'non_utf8')
        self.assertFalse((self.workspace.workspace / 'opaque.bin').exists())
        with self.assertRaises(ValueError): self.workspace.read('.env')

    def test_literal_search_and_pagination_do_not_hide_the_later_directory(self):
        for number in range(230):
            (self.project_workspace / ('item-%03d.txt' % number)).write_text('value')
        self.workspace.prepare()
        all_paths, offset = [], 0
        while offset is not None:
            page = self.workspace.list(offset=offset, limit=100)
            all_paths.extend(row['path'] for row in page['files']); offset = page['nextOffset']
        self.assertIn('item-229.txt', all_paths); self.assertEqual(len(all_paths), len(set(all_paths)))
        matches = self.workspace.search('a.*b')['matches']
        self.assertEqual([(row['path'], row['line']) for row in matches], [('docs/unloaded.txt', 2)])
        self.assertEqual(self.workspace.search('^VALUE')['matches'], [])
        with self.assertRaises(ValueError): self.workspace.list(offset=True)

    def test_cas_write_is_scope_bound_and_old_inputs_and_project_stay_unchanged(self):
        self.workspace.prepare(); before = self.workspace.fingerprint()
        old = self.workspace.read('solution.py')['sha256']
        with self.assertRaises(Conflict): self.workspace.write('solution.py', 'VALUE = 1\n')
        changed = self.workspace.write('solution.py', 'VALUE = 1\n', old)
        self.assertEqual(set(changed), {'path', 'sha256', 'changed', 'revision', 'workspaceFingerprint'})
        self.assertEqual(changed['revision'], 1); self.assertNotEqual(changed['workspaceFingerprint'], before)
        with self.assertRaises(Conflict): self.workspace.write('solution.py', 'VALUE = 2\n', old)
        same = self.workspace.write('solution.py', 'VALUE = 1\n', changed['sha256'])
        self.assertEqual(set(same), set(changed))
        self.assertFalse(same['changed']); self.assertEqual(same['revision'], 1)
        with self.assertRaises(ValueError): self.workspace.write('dep.txt', 'overwrite', self.workspace.read('dep.txt')['sha256'])
        self.assertEqual((self.project_workspace / 'solution.py').read_text(), 'VALUE = 0\n')
        self.assertEqual((self.workspace.root / 'inputs/project/solution.py').read_text(), 'VALUE = 0\n')

    def test_path_aliases_hidden_names_and_credentials_cannot_be_written(self):
        self.workspace.prepare()
        for name in ('../outside', '/absolute', './solution.py', 'new//file.py', 'new/../file.py',
                     'new/.env', 'new/token.key', 'new/cafe\u0301.txt', 'new\\file.py'):
            with self.subTest(name=name), self.assertRaises(ValueError): self.workspace.write(name, 'data')
        self.workspace.write('new/Name.txt', 'first')
        with self.assertRaises(ValueError): self.workspace.write('new/name.txt', 'alias')
        with self.assertRaises(ValueError): self.workspace.write('new/secret.txt', 'sk-' + 'x' * 24)

    def test_project_and_workspace_symlinks_are_rejected(self):
        target = self.base / 'outside.txt'; target.write_text('outside')
        link = self.project_workspace / 'link.txt'; link.symlink_to(target)
        with self.assertRaises(ValueError): self.workspace.prepare()
        link.unlink(); self.workspace.prepare()
        candidate = self.workspace.workspace / 'solution.py'; candidate.unlink(); candidate.symlink_to(target)
        with self.assertRaises(ValueError): self.workspace.read('solution.py')
        self.assertEqual(target.read_text(), 'outside')

    def test_unknown_write_is_retained_and_cannot_be_repaired_by_prepare(self):
        self.workspace.prepare(); old = self.workspace.read('solution.py')['sha256']
        save = self.workspace._save
        def fail_after_replace(path, value):
            if path == self.workspace.state_path and value.get('revision') == 1 and value.get('pendingWrite') is None:
                raise OSError('lost final write state')
            return save(path, value)
        with patch.object(self.workspace, '_save', side_effect=fail_after_replace):
            with self.assertRaises(OSError): self.workspace.write('solution.py', 'VALUE = 1\n', old)
        state = json.loads(self.workspace.state_path.read_text())
        self.assertIsNotNone(state['pendingWrite'])
        with self.assertRaisesRegex(ValueError, '未确认'): self.make().prepare()
        self.assertEqual((self.workspace.workspace / 'solution.py').read_text(), 'VALUE = 1\n')

    def test_forged_or_unselected_role_binding_is_not_execution_authority(self):
        bad = deepcopy(self.binding); bad['enabled'] = False
        with self.assertRaises(ValueError): self.make(binding=bad)
        self.run['workerTools']['roleGrants']['coder']['enabled'] = False
        with self.assertRaises(ValueError): self.make()
        self.assertFalse(self.workspace.root.exists())

    @unittest.skipUnless(os.name == 'posix', 'trusted host runner needs POSIX groups')
    def test_real_failed_test_write_then_success_uses_only_scope_and_separate_namespace(self):
        self.workspace.prepare()
        first = self.workspace.run_test('fixed-test', 'one', threading.Event())
        self.assertEqual(first['status'], 'failed'); self.assertEqual(first['receipt']['exitCode'], 1)
        self.assertTrue(self.workspace.verify_test(first)); self.assertEqual(self.marker.read_text(), '1')
        self.assertEqual([row['path'] for row in first['receipt']['artifactManifest']], ['solution.py'])
        self.assertIn('host-acceptance/worker-test-', first['receipt']['receiptRef'])
        self.assertNotIn('hostAcceptanceHistory', self.run)
        self.assertEqual(self.workspace.run_test('fixed-test', 'two', threading.Event()), first)
        self.assertEqual(self.marker.read_text(), '1')
        self.workspace.write('solution.py', 'VALUE = 1\n', self.workspace.read('solution.py')['sha256'])
        self.assertFalse(self.workspace.verify_test(first))
        last = self.workspace.run_test('fixed-test', 'three', threading.Event())
        self.assertTrue(last['passed']); self.assertEqual(last['receipt']['exitCode'], 0)
        self.assertEqual(self.marker.read_text(), '2'); self.assertTrue(self.workspace.verify_test(last))
        self.assertEqual(self.workspace.files(), [{'path': 'solution.py', 'content': 'VALUE = 1\n'}])

    def test_unknown_test_is_not_replayed_even_after_another_file_write(self):
        self.workspace.prepare()
        class Crash(BaseException): pass
        with patch('host_acceptance.run_check', side_effect=Crash):
            with self.assertRaises(Crash): self.workspace.run_test('fixed-test', 'one', threading.Event())
        self.workspace.write('new/notes.txt', 'new candidate')
        with patch('host_acceptance.run_check', side_effect=AssertionError('must not replay')):
            with self.assertRaisesRegex(ValueError, '未知'): self.workspace.run_test('fixed-test', 'two', threading.Event())

    def test_cancel_preserves_unknown_raw_receipt_and_does_not_execute_script(self):
        self.workspace.prepare(); cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled): self.workspace.run_test('fixed-test', 'one', cancel)
        self.assertFalse(self.marker.exists())
        record = next(iter(json.loads(self.workspace.state_path.read_text())['tests'].values()))
        self.assertEqual(record['status'], 'unknown'); self.assertTrue(record['cancelled'])
        self.assertEqual(record['receipt']['status'], 'unknown')
        self.assertTrue(record['receiptSha256'])

    @unittest.skipUnless(os.name == 'posix', 'trusted host runner needs POSIX groups')
    def test_cached_test_or_input_tampering_is_readonly_failure_never_a_reexecution(self):
        self.workspace.prepare(); record = self.workspace.run_test('fixed-test', 'one', threading.Event())
        receipt = self.workspace.session_root / record['receipt']['receiptRef']
        receipt.write_text('{}')
        before = {p: p.read_bytes() for p in self.workspace.root.rglob('*') if p.is_file()}
        with patch('host_acceptance.run_check', side_effect=AssertionError('must not replay')):
            self.assertFalse(self.workspace.verify_test(record))
            with self.assertRaises(ValueError): self.workspace.run_test('fixed-test', 'two', threading.Event())
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        source = self.workspace.root / 'inputs/upstream/dep.txt'; source.chmod(0o600); source.write_text('tampered')
        with self.assertRaises(ValueError): self.workspace.verify_inputs()


if __name__ == '__main__':
    unittest.main()
