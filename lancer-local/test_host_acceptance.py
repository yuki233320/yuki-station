"""Real local grader processes and durable reuse, without model/network traffic."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from host_acceptance import HostAcceptanceRegistry, MAX_OUTPUT, run_check, verify_receipt
from live import Cancelled


PASS_SCRIPT = '''import argparse,json,os,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--workspace');a=p.parse_args()
marker=Path(os.environ['HOME'])/'invocations'
marker.write_text(str(int(marker.read_text())+1) if marker.exists() else '1')
passed=(Path(a.workspace)/'deliverable.txt').read_bytes()==b'candidate'
print(json.dumps({'accepted':passed,'criteria':[{'criterionId':'C1','status':'passed' if passed else 'failed'}],
                  'flags':{'isolated':sys.flags.isolated,'noBytecode':sys.dont_write_bytecode},'env':dict(os.environ)}))
'''


class HostAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'data'
        self.project = self.data / 'project-sessions' / 'project1'
        self.project.mkdir(parents=True)
        self.studio = SimpleNamespace(store=SimpleNamespace(root=self.data),
                                      project_dir=lambda pid: self.data / 'project-sessions' / pid)
        self.registry = HostAcceptanceRegistry(self.data)
        self.session = {'projectId': 'project1', 'id': 'session1'}
        self.run = {'id': 'run1'}
        self.files = {'deliverable.txt': b'candidate', 'extra/data.bin': b'\x00\x01'}
        self.cancel = threading.Event()
        self.request_number = 0

    def tearDown(self):
        self.cancel.set()
        self.tmp.cleanup()

    def register(self, source=PASS_SCRIPT, **changes):
        self.request_number += 1
        path = self.root / ('grader%d.py' % self.request_number)
        path.write_text(source, encoding='utf8')
        request = {'id': 'grader1', 'name': 'Trusted local grader', 'scriptPath': str(path),
                   'files': ['deliverable.txt'], 'criteria': [{'id': 'C1', 'description': 'Candidate bytes match'}]}
        request.update(changes)
        entry = self.registry.register(request)
        return entry, self.registry.snapshot([entry['id']])[0], path

    def execute(self, check, files=None, round_no=0):
        return run_check(self.studio, self.session, self.run, check,
                         self.files if files is None else files, round_no, self.cancel)

    def job(self, round_no=0):
        return self.project / 'session1' / 'host-acceptance' / 'run1' / ('round-' + str(round_no)) / 'grader1'

    def test_registration_copies_without_execution_and_freezes_public_metadata(self):
        marker = self.root / 'must-not-run'
        entry, check, source = self.register('from pathlib import Path\nPath(%r).write_text("bad")\n' % str(marker))
        self.assertFalse(marker.exists())
        approved = self.registry.authorize(check)
        self.assertEqual(approved.read_bytes(), source.read_bytes())
        self.assertNotIn('scriptPath', entry)
        self.assertNotIn('registeredAt', check)
        self.assertEqual(set(check), {'id', 'name', 'files', 'criteria', 'scriptSha256', 'timeoutSeconds'})
        source.write_text('changed original')
        self.assertEqual(self.registry.authorize(check), approved)
        self.assertNotIn('must-not-run', json.dumps(self.registry.info()))

    def test_registry_revocation_and_modification_are_visible_to_old_instances(self):
        _, check, _ = self.register()
        another = HostAcceptanceRegistry(self.data)
        another.remove(check['id'])
        self.assertEqual(self.registry.info(), [])
        with self.assertRaises(ValueError): self.registry.authorize(check)
        self.assertTrue((self.registry.scripts / (check['scriptSha256'] + '.py')).exists())
        _, renewed, _ = self.register(name='Changed name')
        with self.assertRaises(ValueError): self.registry.authorize(check)
        self.registry.authorize(renewed).write_text('changed approved copy')
        with self.assertRaisesRegex(ValueError, 'hash changed'): self.registry.authorize(renewed)

    def test_validation_rejects_model_commands_traversal_duplicate_and_boolean_timeout(self):
        for change in ({'files': ['../secret']}, {'files': ['/absolute']}, {'files': ['a\\b']},
                       {'files': ['a', 'a']}, {'files': [[]]}, {'timeoutSeconds': True},
                       {'timeoutSeconds': 0}, {'timeoutSeconds': 301}, {'id': '../escape'},
                       {'criteria': [{'id': 'C1', 'description': 'a'}, {'id': 'C1', 'description': 'b'}]},
                       {'command': ['python3', 'model-proposed.py']}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.register(**change)
        _, check, _ = self.register()
        with self.assertRaises(ValueError): self.registry.authorize({**check, 'script': 'model supplied body'})

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_pass_copies_all_artifacts_cleans_credentials_and_reuses_exact_receipt(self):
        _, check, _ = self.register()
        before = deepcopy(self.files)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-secret', 'ANTHROPIC_API_KEY': 'other-secret',
                                    'UNRELATED_CREDENTIAL': 'also-secret'}):
            result = self.execute(check)
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(result['passed']); self.assertTrue(result['workspaceUnchanged'])
        self.assertEqual(self.files, before)
        self.assertEqual([r['path'] for r in result['artifactManifest']], sorted(self.files))
        report = json.loads((self.job() / 'stdout.log').read_text())
        self.assertEqual(report['flags'], {'isolated': 1, 'noBytecode': True})
        self.assertNotIn('OPENAI_API_KEY', report['env'])
        self.assertNotIn('ANTHROPIC_API_KEY', report['env'])
        self.assertNotIn('UNRELATED_CREDENTIAL', report['env'])
        self.assertEqual(report['env']['HOME'], str(self.job() / 'home'))
        self.assertEqual(json.loads((self.job() / 'receipt.json').read_text()), result)
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.execute(check), result)
            self.assertTrue(verify_receipt(self.studio, self.session, self.run, check, self.files, 0, result))
        self.assertEqual((self.job() / 'home/invocations').read_text(), '1')

    def test_missing_declared_file_is_repairable_failure_without_dispatch(self):
        _, check, _ = self.register()
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not start')):
            result = self.execute(check, files={'other.txt': b'other'})
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['executed']); self.assertIsNone(result['exitCode'])
        self.assertEqual(result['basis'], 'preflight_missing_artifacts')
        self.assertEqual(result['missingArtifacts'], ['deliverable.txt'])
        self.assertEqual(result['criteria'][0]['status'], 'failed')
        self.assertTrue(verify_receipt(self.studio, self.session, self.run, check, {'other.txt': b'other'}, 0, result))

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_real_exit_one_failure_is_preserved_and_never_retried(self):
        _, check, _ = self.register("import json,sys\nprint(json.dumps({'accepted':False,'criteria':[{'id':'C1','status':'failed','detail':'Wrong bytes'}]}))\nsys.exit(1)\n")
        result = self.execute(check)
        self.assertEqual((result['status'], result['exitCode'], result['passed']), ('failed', 1, False))
        self.assertEqual(result['criteria'][0]['detail'], 'Wrong bytes')
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not retry')):
            self.assertEqual(self.execute(check), result)

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_invalid_or_inconsistent_reports_cannot_pass(self):
        reports = [({'accepted': 1, 'criteria': [{'id': 'C1', 'status': 'passed'}]}, 0),
                   ({'accepted': True, 'criteria': []}, 0),
                   ({'accepted': True, 'criteria': [{'id': 'C1', 'status': 'passed'}] * 2}, 0),
                   ({'accepted': True, 'criteria': [{'id': 'C1', 'status': 'failed'}]}, 0),
                   ({'accepted': False, 'criteria': [{'id': 'C1', 'status': 'passed'}]}, 0),
                   ({'accepted': True, 'criteria': [{'id': 'C1', 'status': 'passed'}]}, 1),
                   ({'accepted': False, 'criteria': [{'id': 'C1', 'status': 'failed'}]}, 2),
                   ({'accepted': True, 'criteria': [{'id': 'other', 'status': 'passed'}]}, 0)]
        for number, (report, code) in enumerate(reports):
            with self.subTest(report=report, code=code):
                _, check, _ = self.register('import sys\nprint(%r)\nsys.exit(%d)\n' % (json.dumps(report), code))
                result = self.execute(check, round_no=number)
                self.assertEqual(result['status'], 'error', result)
                self.assertFalse(result['passed'])

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_invalid_json_and_workspace_mutation_are_errors(self):
        _, bad_json, _ = self.register('print("not JSON")\n')
        self.assertEqual(self.execute(bad_json)['status'], 'error')
        _, mutation, _ = self.register("import argparse,json\nfrom pathlib import Path\np=argparse.ArgumentParser();p.add_argument('--workspace');a=p.parse_args()\n(Path(a.workspace)/'deliverable.txt').write_bytes(b'changed')\nprint(json.dumps({'accepted':True,'criteria':[{'id':'C1','status':'passed'}]}))\n")
        result = self.execute(mutation, round_no=1)
        self.assertEqual(result['status'], 'error'); self.assertFalse(result['workspaceUnchanged'])
        self.assertEqual(self.files['deliverable.txt'], b'candidate')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_optional_summary_counts_must_be_strict_and_match_criteria(self):
        for number, fields in enumerate(({'passed': True}, {'total': 1.0}, {'passed': 0}, {'total': 2})):
            with self.subTest(fields=fields):
                report = {'accepted': True, 'criteria': [{'id': 'C1', 'status': 'passed'}], **fields}
                _, check, _ = self.register('print(%r)\n' % json.dumps(report))
                self.assertEqual(self.execute(check, round_no=number)['status'], 'error')
        report = {'accepted': True, 'criteria': [{'id': 'C1', 'status': 'passed'}], 'passed': 1, 'total': 1}
        _, check, _ = self.register('print(%r)\n' % json.dumps(report))
        self.assertEqual(self.execute(check, round_no=10)['status'], 'passed')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_launch_failure_does_not_claim_actual_execution(self):
        _, check, _ = self.register()
        with patch('host_acceptance.subprocess.Popen', side_effect=OSError('fixture launch denied')):
            result = self.execute(check)
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['executed'])
        self.assertIsNone(result['exitCode'])

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_concurrent_same_job_observes_unknown_without_duplicate_dispatch(self):
        source = PASS_SCRIPT.replace("passed=(Path", "import time\ntime.sleep(.3)\npassed=(Path")
        _, check, _ = self.register(source)
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.execute(check)))
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not (self.job() / 'home/invocations').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.job() / 'home/invocations').exists())
            original = (self.job() / 'receipt.json').read_bytes()
            with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('duplicate dispatch')):
                self.assertEqual(self.execute(check)['status'], 'unknown')
            self.assertEqual((self.job() / 'receipt.json').read_bytes(), original)
        finally:
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0]['status'], 'passed')
        self.assertEqual((self.job() / 'home/invocations').read_text(), '1')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_stdout_and_stderr_limits_are_bounded_and_fail_closed(self):
        for number, stream in enumerate(('stdout', 'stderr')):
            _, check, _ = self.register('import sys\nsys.%s.write("x" * %d)\nsys.%s.flush()\n' % (stream, MAX_OUTPUT + 100000, stream))
            result = self.execute(check, round_no=number)
            self.assertEqual(result['status'], 'error', result)
            self.assertTrue(result['outputTruncated'])
            self.assertLessEqual((self.job(number) / (stream + '.log')).stat().st_size, MAX_OUTPUT)
            self.assertFalse(result['passed'])

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_timeout_is_durable_and_not_replayed(self):
        _, check, _ = self.register('import time\ntime.sleep(10)\n', timeoutSeconds=1)
        result = self.execute(check)
        self.assertEqual(result['status'], 'timeout', result)
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not retry')):
            self.assertEqual(self.execute(check), result)

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_cancel_stops_process_group_then_saves_unknown_and_raises(self):
        source = '''import os,subprocess,sys,time
from pathlib import Path
home=Path(os.environ['HOME'])
subprocess.Popen([sys.executable,'-I','-B','-c',"import time; from pathlib import Path; time.sleep(1); Path("+repr(str(home/'late-child-write'))+").write_text('bad')"])
(home/'started').write_text('ready')
time.sleep(20)
'''
        _, check, _ = self.register(source, timeoutSeconds=10)
        outcomes = []
        def worker():
            try: outcomes.append(self.execute(check))
            except BaseException as error: outcomes.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not (self.job() / 'home/started').exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue((self.job() / 'home/started').exists())
        finally:
            self.cancel.set(); thread.join(4)
        self.assertFalse(thread.is_alive()); self.assertIsInstance(outcomes[0], Cancelled)
        saved = json.loads((self.job() / 'receipt.json').read_text())
        self.assertEqual(saved['status'], 'unknown'); self.assertTrue(saved['cancelled'])
        self.assertTrue((self.job() / 'receipt.sha256').exists())
        time.sleep(1.1)
        self.assertFalse((self.job() / 'home/late-child-write').exists())
        self.cancel.clear()
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not retry')):
            self.assertEqual(self.execute(check)['status'], 'unknown')

    def test_cancel_before_dispatch_is_durable_without_popen(self):
        _, check, _ = self.register()
        self.cancel.set()
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not run')):
            with self.assertRaises(Cancelled): self.execute(check)
        saved = json.loads((self.job() / 'receipt.json').read_text())
        self.assertEqual(saved['status'], 'unknown'); self.assertTrue(saved['cancelled'])

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_recovery_intent_is_saved_before_launch_and_never_replayed(self):
        _, check, _ = self.register()
        class Crash(BaseException): pass
        def crash(*args, **kwargs):
            saved = json.loads((self.job() / 'receipt.json').read_text())
            self.assertEqual(saved['status'], 'running')
            self.assertTrue((self.job() / 'artifacts.json').is_file())
            raise Crash()
        with patch('host_acceptance.subprocess.Popen', side_effect=crash):
            with self.assertRaises(Crash): self.execute(check)
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.execute(check)['status'], 'unknown')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_missing_corrupt_or_changed_evidence_never_reexecutes(self):
        mutations = [lambda job: (job/'receipt.sha256').unlink(),
                     lambda job: (job/'stdout.log').write_text('changed'),
                     lambda job: (job/'stderr.log').write_text('changed'),
                     lambda job: (job/'workspace/extra/data.bin').write_bytes(b'changed'),
                     lambda job: (job/'artifacts.json').write_text('{}'),
                     lambda job: (job/'receipt.json').write_text('{}')]
        _, check, _ = self.register()
        for round_no, mutate in enumerate(mutations):
            with self.subTest(round=round_no):
                result = self.execute(check, round_no=round_no)
                mutate(self.job(round_no))
                with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not replay')):
                    self.assertEqual(self.execute(check, round_no=round_no)['status'], 'unknown')
                    self.assertFalse(verify_receipt(self.studio, self.session, self.run, check, self.files, round_no, result))
        result = self.execute(check, round_no=10)
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.execute(check, files={'deliverable.txt': b'new'}, round_no=10)['status'], 'unknown')
            self.assertFalse(verify_receipt(self.studio, self.session, self.run, check, self.files, 11, result))

    def test_verify_missing_receipt_is_read_only_and_does_not_dispatch(self):
        _, check, _ = self.register()
        before = {p.relative_to(self.data).as_posix() for p in self.data.rglob('*')}
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not run')):
            self.assertFalse(verify_receipt(self.studio, self.session, self.run, check, self.files, 0, {}))
        self.assertEqual({p.relative_to(self.data).as_posix() for p in self.data.rglob('*')}, before)

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_workspace_root_symlink_cannot_reuse_an_otherwise_matching_receipt(self):
        _, check, _ = self.register()
        result = self.execute(check)
        workspace = self.job() / 'workspace'
        moved = self.job() / 'moved-workspace'
        workspace.rename(moved)
        workspace.symlink_to(moved, target_is_directory=True)
        with patch('host_acceptance.subprocess.Popen', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.execute(check)['status'], 'unknown')
            self.assertFalse(verify_receipt(self.studio, self.session, self.run, check, self.files, 0, result))

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX process-group runner')
    def test_details_are_bounded_with_explicit_truncation_and_raw_output_retained(self):
        _, check, _ = self.register("import json\nprint(json.dumps({'accepted':False,'criteria':[{'id':'C1','status':'failed','detail':'z'*20000}]}))\n")
        result = self.execute(check)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['criteria'][0]['detailTruncated'])
        self.assertLessEqual(len(result['criteria'][0]['detail']), 1000)
        self.assertEqual(len(json.loads((self.job()/'stdout.log').read_text())['criteria'][0]['detail']), 20000)


if __name__ == '__main__':
    unittest.main()
