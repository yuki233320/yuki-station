"""Fixture API decisions plus actual local file/grader tools, no paid calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Store, stamp, uid
from live import Cancelled
from real_task_benchmark import measure_run
from review_evidence import fingerprint
from studio import Studio
from studio_client import StudioClient
from worker_execution import execute_worker, verify_worker_result, WorkerExecutionError, _observation_view, MAX_REPLY
from worker_policy import bind_role, normalize_policy
from worker_workspace import WorkerWorkspace


def tool(name, **arguments):
    return {'tool': {'name': name, 'arguments': arguments}}


def final():
    return {'summary': 'Produced actual candidate files; prechecks completed', 'notes': [], 'checks': [], 'questions': []}


GRADER = '''import argparse,json,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--workspace');a=p.parse_args()
path=Path(a.workspace)/'answer.txt'
ok=path.is_file() and path.read_text()=='good'
print(json.dumps({'accepted':ok,'passed':int(ok),'total':1,'criteria':[{'criterionId':'C1','status':'passed' if ok else 'failed','detail':'answer.txt must contain good'}]}))
sys.exit(0 if ok else 1)
'''


class SequenceClient(StudioClient):
    def __init__(self):
        super().__init__()
        self.answers = []
        self.calls = []

    def available(self):
        return ['deepseek']

    def native_info(self):
        return {'available': False, 'enabled': False, 'capabilities': []}

    def call(self, model, messages, cancel, on_delta=None, **kwargs):
        self.calls.append({'model': model, 'messages': deepcopy(messages),
                           'scope': deepcopy(getattr(self._request_local, 'profiles', None))})
        if not self.answers: raise AssertionError('Fixture answer sequence exhausted')
        value = self.answers.pop(0)
        if callable(value): value = value()
        if isinstance(value, BaseException): raise value
        return deepcopy(value), {'requestedModel': 'fixture-model', 'returnedModel': 'fixture-model',
                                'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
                                'protocol': 'chat', 'finishReason': 'stop'}


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.client = SequenceClient()
        self.studio = Studio(Store(self.root / 'data', seed=False), self.client)
        self.pid = self.studio.create_project('Workspace fixture', 'Use explicit local tools')['id']
        self.session = self.studio.create_session(self.pid, 'Worker task', 'Create the requested answer', 'general')
        script = self.root / 'grader.py'; script.write_text(GRADER)
        entry = self.studio.host_acceptance.register({'id': 'answer-check', 'name': 'Answer check',
            'scriptPath': str(script), 'files': ['answer.txt'], 'criteria': [{'id': 'C1', 'description': 'Answer is good'}]})
        self.check = self.studio.host_acceptance.snapshot([entry['id']])[0]
        self.policy = normalize_policy({'version': 1, 'roleGrants': {'builder': {'enabled': True, 'checkIds': [entry['id']]}}})
        self.module = {'id': 'answer', 'roleId': 'builder', 'scope': ['answer.txt'], 'dependsOn': [],
                       'requiredCapabilities': ['text', 'workspace'], 'assignment': {'model': 'deepseek'}}
        self.module['workerBinding'] = bind_role(self.module, self.policy, [self.check], self.studio.host_acceptance)
        run_id = uid()
        self.contract = {'schema': 'yuki-task-contract/v1', 'id': run_id + ':answer', 'runId': run_id,
            'projectId': self.pid, 'sessionId': self.session['id'], 'moduleId': 'answer', 'roleId': 'builder',
            'model': 'deepseek', 'scope': ['answer.txt'], 'workerToolsVersion': 1,
            'workerBinding': deepcopy(self.module['workerBinding'])}
        self.contract['fingerprint'] = fingerprint(self.contract)
        self.run = {'id': run_id, 'mode': 'live', 'status': 'running', 'qualityVersion': 1,
            'workflowVersion': 2, 'projectRevision': self.studio.store.current(self.pid)[0]['revision'],
            'workerToolsVersion': 1, 'workerTools': self.policy, 'hostAcceptanceChecks': [self.check],
            'plan': {'modules': [self.module]}, 'taskContracts': {'answer': self.contract}, 'nodes': [],
            'modelRequestProfiles': self.client.snapshot_profiles(['deepseek'])}
        self.node = self.studio.node(self.run, 'build-answer', 'Write answer', 'deepseek', 'build')
        self.node.update(moduleId='answer', roleId='builder', agentId='worker-builder', executorId='workspace')
        self.session['runs'] = [self.run]; self.session['status'] = 'running'
        self.shared = {'module': self.module, 'executionContract': self.contract, 'upstream': {}, 'businessStage': 'build'}
        self.context = {'id': uid(), 'created': stamp(), 'runId': run_id, 'model': 'deepseek',
                        'agentId': 'worker-builder', 'memory': {}, 'messages': [{'role':'user','content':'Create answer.txt using the allowed tools'}]}
        self.session_root = self.studio.project_dir(self.pid) / self.session['id']
        self.studio._write(self.session_root / 'contexts' / (self.context['id'] + '.json'), self.context)
        self.studio.save(self.session)
        self.cancel = threading.Event()
        self.job = self.session_root / 'worker-executions' / run_id / self.node['id']

    def tearDown(self):
        self.cancel.set()
        self.temp.cleanup()

    def execute(self, answers=None):
        if answers is not None: self.client.answers = list(answers)
        return execute_worker(self.studio, self.session, self.run, self.node, self.context,
                              self.context['messages'], self.shared, self.cancel, None)

    def workspace(self):
        return WorkerWorkspace(self.studio, self.session, self.run, self.node, self.shared, self.module['workerBinding'])

    def test_real_write_failed_test_repair_and_pass_then_final_preserves_every_call(self):
        bad_hash = hashlib.sha256(b'bad').hexdigest()
        answers = [tool('list', offset=0, limit=20), tool('write', path='answer.txt', content='bad'),
                   tool('test', checkId='answer-check'), tool('read', path='answer.txt'),
                   tool('write', path='answer.txt', content='good', expectedSha256=bad_hash),
                   tool('test', checkId='answer-check'), final()]
        result, receipt, context = self.execute(answers)
        self.assertEqual(result['files'], [{'path':'answer.txt','content':'good'}])
        proof = result['workerEvidence']
        self.assertEqual(proof['status'], 'completed')
        self.assertEqual(proof['nodeId'], self.node['id'])
        self.assertEqual(proof['toolCounts']['write'], 2)
        tests = [row for row in proof['tools'] if row['name']=='test']
        self.assertEqual([row['testStatus'] for row in tests], ['failed','passed'])
        self.assertEqual([row['testPassed'] for row in tests], [False, True])
        self.assertFalse(self.run.get('qualityHistory'), 'Worker prechecks are not the formal quality gate')
        self.assertTrue(all(row['workspaceFingerprint']==proof['workspaceFingerprint'] for row in proof['tests']))
        self.assertEqual(len(self.client.calls), len(answers))
        self.assertEqual({call['model'] for call in self.client.calls}, {'deepseek'})
        self.assertTrue(all(call['scope'] == self.run['modelRequestProfiles'] for call in self.client.calls))
        calls = self.run['callMetrics']
        self.assertEqual(len({call['contextRef'] for call in calls}), len(answers))
        rows = [json.loads(path.read_text()) for path in (self.session_root/'responses').glob('*.json')]
        metrics = measure_run(self.run, rows)
        self.assertEqual(metrics['callCount'], len(answers))
        self.assertTrue(metrics['usage']['complete'])
        self.assertEqual(metrics['usage']['inputTokens'], 10*len(answers))
        self.assertEqual(metrics['usage']['outputTokens'], 5*len(answers))
        self.assertIsNone(metrics['actualPaid']['amount'])
        self.assertEqual(receipt['returnedModel'], 'fixture-model')
        self.assertEqual(context['id'], calls[-1]['contextRef'])

    def test_final_runs_missing_prechecks_and_cached_completion_never_replays(self):
        result, receipt, context = self.execute([tool('write',path='answer.txt',content='good'),final()])
        self.assertEqual(result['workerEvidence']['toolCounts']['test'], 1)
        count = len(self.client.calls)
        with patch.object(self.studio, 'request_model_response', side_effect=AssertionError('model replay')), \
             patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('test replay')):
            repeated = self.execute()
            checks = verify_worker_result(self.studio,self.session,self.run,self.node,
                                          {'module':self.module,'executionContract':self.contract},result)
        self.assertEqual(repeated, (result,receipt,context))
        self.assertEqual(len(self.client.calls), count)
        self.assertTrue(all(check['pass'] for check in checks))

    def test_inline_model_files_are_rejected_not_written_and_model_can_use_write(self):
        forged = {'summary':'fake','files':[{'path':'answer.txt','content':'evil'}]}
        result, _, _ = self.execute([forged,tool('write',path='answer.txt',content='good'),final()])
        self.assertEqual(result['files'][0]['content'], 'good')
        self.assertEqual(result['workerEvidence']['toolCounts']['write'], 1)

    def test_write_invalidates_old_pass_and_final_rechecks_current_bytes(self):
        good = hashlib.sha256(b'good').hexdigest(); bad = hashlib.sha256(b'bad').hexdigest()
        answers=[tool('write',path='answer.txt',content='good'),tool('test',checkId='answer-check'),
                 tool('write',path='answer.txt',content='bad',expectedSha256=good),final(),
                 tool('write',path='answer.txt',content='good',expectedSha256=bad),final()]
        result, _, _ = self.execute(answers)
        self.assertEqual([row['testStatus'] for row in result['workerEvidence']['tools'] if row['name']=='test'],
                         ['passed','failed','passed'])

    def test_repeated_reads_stop_without_external_actions_or_total_budget(self):
        self.client.answers=[tool('read',path='brief.md') for _ in range(8)]
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('unexpected test')):
            with self.assertRaisesRegex(WorkerExecutionError,'重复或无进展'):
                self.execute()
        self.assertLessEqual(len(self.client.calls), 5)
        self.assertFalse(self.workspace().list('answer.txt')['files'])
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(journal['status'], 'failed')
        self.assertIsNone(journal['pending'])

    def test_unknown_test_intent_and_model_failure_are_not_replayed(self):
        answers=[tool('write',path='answer.txt',content='good'),tool('test',checkId='answer-check')]
        with patch.object(WorkerWorkspace,'run_test',side_effect=OSError('lost test reply')):
            with self.assertRaises(OSError): self.execute(answers)
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(journal['status'], 'unknown')
        self.assertEqual(journal['pending']['kind'], 'tool')
        before=(self.job/'receipt.json').read_bytes()
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('model replay')), \
             patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('test replay')):
            with self.assertRaisesRegex(WorkerExecutionError,'不自动重放'): self.execute()
        self.assertEqual((self.job/'receipt.json').read_bytes(), before)

    def test_cancel_is_persisted_and_resume_does_not_replay_pending_model(self):
        def cancel_request():
            self.cancel.set()
            raise Cancelled()
        with self.assertRaises(Cancelled): self.execute([cancel_request])
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertTrue(journal['cancelled'])
        self.assertEqual(journal['pending']['kind'], 'model')
        self.cancel.clear()
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('model replay')):
            with self.assertRaises(WorkerExecutionError): self.execute()

    def test_corrupt_model_or_tool_originals_and_altered_output_fail_readonly_verification(self):
        result,_,_=self.execute([tool('write',path='answer.txt',content='good'),final()])
        changed=deepcopy(result);changed['files'][0]['content']='changed'
        with self.assertRaises(WorkerExecutionError):
            verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,changed)
        journal=json.loads((self.job/'receipt.json').read_text())
        original=self.session_root/journal['modelCalls'][0]['responseRef']
        original.write_text('{}')
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('model replay')), \
             patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('test replay')):
            with self.assertRaisesRegex(WorkerExecutionError,'原件已变化'):
                verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
            with self.assertRaises(WorkerExecutionError): self.execute()

    def test_missing_readonly_cache_does_not_prepare_or_dispatch(self):
        before=set(self.session_root.rglob('*'))
        with patch.object(WorkerWorkspace,'prepare',side_effect=AssertionError('prepare')), \
             patch.object(self.studio,'request_model_response',side_effect=AssertionError('request')):
            with self.assertRaises(WorkerExecutionError):
                verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,{})
        self.assertEqual(set(self.session_root.rglob('*')), before)

    def test_frozen_request_policy_change_cannot_reuse_completed_worker_result(self):
        result,_,_=self.execute([tool('write',path='answer.txt',content='good'),final()])
        self.run['modelRequestProfiles']['deepseek']['requestPolicy']['maxOutputTokens'] += 1
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('request')):
            with self.assertRaises(WorkerExecutionError):
                verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
            with self.assertRaisesRegex(WorkerExecutionError,'发生变化'):
                self.execute()

    def test_execution_recovery_rechecks_supplied_upstream_without_dispatch(self):
        self.module['dependsOn'] = ['upstream']
        self.shared['upstream'] = {'upstream': {'files': [{'path':'source.txt','content':'original input'}]}}
        self.execute([tool('write',path='answer.txt',content='good'),final()])
        self.shared['upstream']['upstream']['files'][0]['content'] = 'changed input'
        before = (self.job/'receipt.json').read_bytes()
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('request')), \
             patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('test')):
            with self.assertRaisesRegex(ValueError,'冻结输入已经变化'):
                self.execute()
        self.assertEqual((self.job/'receipt.json').read_bytes(), before)

    def test_session_root_symlink_is_rejected_before_any_external_write(self):
        moved = self.session_root.with_name(self.session_root.name + '-saved')
        self.session_root.rename(moved)
        external = self.root / 'outside-session'; external.mkdir()
        self.session_root.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(WorkerExecutionError, '符号链接'):
            self.execute([final()])
        with self.assertRaisesRegex(WorkerExecutionError, '符号链接'):
            verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,{})
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(self.client.calls, [])


class WorkerObservationViewTests(unittest.TestCase):
    def encoded(self, value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def test_default_200_file_list_retains_valid_paging_without_skipping_names(self):
        files = [{'path': 'folder-%03d/' % i + 'long-name-' * 8 + '.py', 'bytes': 100,
                  'sha256': 'a' * 64, 'readable': True, 'writable': False} for i in range(200)]
        offset, seen, pages = 0, [], 0
        while offset is not None:
            raw = {'files': files[offset:offset+200], 'total': len(files), 'offset': offset,
                   'limit': 200, 'nextOffset': None, 'truncated': False}
            view = _observation_view('list', raw, 'steps/list.json')
            self.assertLessEqual(len(self.encoded(view)), MAX_REPLY)
            self.assertTrue(view['files'])
            self.assertEqual(view['files'], files[offset:offset+len(view['files'])])
            self.assertEqual(view['total'], 200)
            expected_next = offset+len(view['files'])
            self.assertEqual(view['nextOffset'], expected_next if expected_next < 200 else None)
            seen.extend(row['path'] for row in view['files']); offset=view['nextOffset']; pages+=1
        self.assertGreater(pages, 1)
        self.assertEqual(seen, [row['path'] for row in files])

    def test_long_read_and_search_preserve_addressing_metadata_and_errors(self):
        read = {'path':'code.py','sha256':'b'*64,'startLine':1,'endLine':200,'lineCount':300,
                'content':'quoted "text"\\n'*3000,'readable':True,'truncated':True}
        view = _observation_view('read', read, 'steps/read.json')
        self.assertLessEqual(len(self.encoded(view)), MAX_REPLY)
        for key in ('path','sha256','startLine','endLine','lineCount'):
            self.assertEqual(view[key], read[key])
        self.assertTrue(view['contentTruncated'])
        self.assertTrue(read['content'].startswith(view['content']))
        matches=[{'path':'code.py','line':i+1,'text':'x'*2000,'lineTruncated':True} for i in range(50)]
        view = _observation_view('search', {'matches':matches,'truncated':False}, 'steps/search.json')
        self.assertLessEqual(len(self.encoded(view)), MAX_REPLY)
        self.assertTrue(view['truncated']);self.assertTrue(view['matches'])
        self.assertEqual(view['matches'], matches[:len(view['matches'])])
        for kind in ('list','search'):
            error=_observation_view(kind, {'status':'error','error':'path not found'}, 'steps/error.json')
            self.assertEqual(error['status'], 'error')
            self.assertNotIn('files' if kind=='list' else 'matches', error)

    def test_test_projection_prioritizes_failure_criteria_over_large_manifests(self):
        criteria=[{'criterionId':'C'+str(i),'status':'passed','detail':'normal'} for i in range(100)]
        criteria.append({'criterionId':'C-failed','status':'failed','detail':'expected good but observed bad'})
        raw={'checkId':'check-1','status':'failed','passed':False,'workspaceFingerprint':'c'*64,
             'revision':3,'receiptSha256':'d'*64,'receipt':{'exitCode':1,'receiptRef':'host-acceptance/receipt.json',
                'criteria':criteria,'artifactManifest':[{'path':'p'+str(i),'sha256':'f'*64} for i in range(1000)]}}
        view=_observation_view('test',raw,'steps/test.json')
        self.assertLessEqual(len(self.encoded(view)),MAX_REPLY)
        self.assertEqual((view['checkId'],view['status'],view['passed'],view['exitCode']),('check-1','failed',False,1))
        self.assertEqual(view['criteria'][0]['criterionId'],'C-failed')
        self.assertIn('observed bad',view['criteria'][0]['detail'])
        self.assertEqual(view['receiptRef'],'host-acceptance/receipt.json')
        self.assertEqual(view['rawRef'],'steps/test.json')
        self.assertNotIn('artifactManifest',view)
        self.assertEqual(view['failedCriteriaTotal'],1)


if __name__=='__main__': unittest.main()
