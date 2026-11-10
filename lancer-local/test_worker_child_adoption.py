"""Parent acceptance has separate CAS, tests and evidence from a child proposal."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from app import Conflict
from review_evidence import fingerprint
import test_worker_execution as fixture
from test_worker_execution import tool, final
from worker_execution import WorkerExecutionError, verify_worker_result
from worker_policy import bind_role
from worker_workspace import _atomic


def child_final():return {'summary':'Fixed answer in isolated child workspace','findings':['answer-check passed']}


class WorkerChildAdoptionTests(unittest.TestCase):
    setUp=fixture.WorkerExecutionTests.setUp
    tearDown=fixture.WorkerExecutionTests.tearDown
    execute=fixture.WorkerExecutionTests.execute
    workspace=fixture.WorkerExecutionTests.workspace

    def enable(self):
        self.policy['roleGrants']['builder']['childExecution']=True
        self.module['workerBinding']=bind_role(self.module,self.policy,[self.check],self.studio.host_acceptance)
        self.contract['workerBinding']=deepcopy(self.module['workerBinding'])
        self.contract.pop('fingerprint');self.contract['fingerprint']=fingerprint(self.contract)

    def delegate(self):return tool('delegate',task='Fix answer.txt and test it',mode='execute',scope=['answer.txt'],checkIds=['answer-check'])

    def adopt(self):
        child=json.loads((self.job/'receipt.json').read_text())['subagents'][-1]
        return tool('accept_child',childId=child['id'],expectedFingerprint=child['proposalFingerprint'])

    def inspect(self):
        child=json.loads((self.job/'receipt.json').read_text())['subagents'][-1]
        return tool('inspect_child',childId=child['id'],path='answer.txt')

    def test_parent_reads_actual_candidate_before_adopting(self):
        self.enable()
        result,_,_=self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),child_final(),self.inspect,self.adopt,final()])
        journal=json.loads((self.job/'receipt.json').read_text())
        step=next(r for r in journal['steps'] if r['name']=='inspect_child')
        raw=json.loads((self.session_root/step['rawRef']).read_text())['result']
        self.assertEqual(raw['content'],'good');self.assertFalse(raw['writable'])
        self.assertEqual(raw['proposalFingerprint'],result['workerEvidence']['subagents'][0]['proposalFingerprint'])

    def test_partial_loss_of_child_mode_or_version_cannot_downgrade_evidence(self):
        from worker_execution import _load_journal,_save_journal
        self.enable()
        result,_,_=self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),child_final(),self.adopt,final()])
        path=self.job/'receipt.json';original=path.read_bytes()
        for remove in ('version','mode'):
            journal=_load_journal(path)
            if remove=='version':journal.pop('childExecutionVersion')
            else:
                journal['subagents'][0].pop('mode');journal['subagents'][0].pop('executionVersion')
            _save_journal(path,journal)
            with self.assertRaises(WorkerExecutionError):verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
            path.write_bytes(original)

    def test_child_failure_repair_acceptance_and_distinct_parent_retest(self):
        self.enable()
        answers=[self.delegate(),tool('write',path='answer.txt',content='bad'),tool('test',checkId='answer-check'),
                 tool('read',path='answer.txt'),tool('write',path='answer.txt',content='good',expectedSha256=hashlib.sha256(b'bad').hexdigest()),
                 tool('test',checkId='answer-check'),child_final(),self.adopt,final()]
        result,_,_=self.execute(answers)
        evidence=result['workerEvidence'];self.assertEqual(result['files'],[{'path':'answer.txt','content':'good'}])
        self.assertEqual(len(evidence['childAdoptions']),1);self.assertEqual(evidence['toolCounts']['accept_child'],1)
        child=evidence['subagents'][0];self.assertEqual(child['mode'],'execute')
        raw=json.loads((self.session_root/child['rawRef']).read_text())
        self.assertEqual([r['result']['status'] for r in raw['tools'] if r['action']['name']=='test'],['failed','passed'])
        self.assertNotEqual(raw['tests']['answer-check']['testRunId'],evidence['tests'][0]['testRunId'])
        self.assertEqual(evidence['toolCounts']['test'],1)
        self.assertTrue(all(r['model']=='deepseek' for r in self.client.calls))
        self.assertEqual(len(self.client.calls),len(answers))
        verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
        before=len(self.client.calls);self.execute();self.assertEqual(len(self.client.calls),before)

    def test_parent_changes_make_child_proposal_conflict_without_overwrite(self):
        self.enable()
        answers=[self.delegate(),tool('write',path='answer.txt',content='good'),child_final(),
                 tool('write',path='answer.txt',content='parent edit'),self.adopt,
                 tool('write',path='answer.txt',content='good',expectedSha256=hashlib.sha256(b'parent edit').hexdigest()),final()]
        result,_,_=self.execute(answers)
        self.assertEqual(result['workerEvidence']['childAdoptions'],[])
        journal=json.loads((self.job/'receipt.json').read_text())
        row=next(r for r in journal['steps'] if r['name']=='accept_child')
        self.assertEqual(row['status'],'error');self.assertEqual(row['beforeFingerprint'],row['afterFingerprint'])

    def test_ungranted_execution_is_rejected_before_child_or_extra_model_call(self):
        result,_,_=self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),final()])
        self.assertEqual(result['workerEvidence']['subagents'],[])
        self.assertEqual(len(self.client.calls),3)
        self.assertNotIn('childExecutionVersion',json.loads((self.job/'receipt.json').read_text()))

    def test_repeat_acceptance_never_reapplies_or_copies_child_tests(self):
        self.enable()
        result,_,_=self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),child_final(),self.adopt,self.adopt,final()])
        self.assertEqual(len(result['workerEvidence']['childAdoptions']),1)
        self.assertEqual(self.workspace()._view()['revision'],1)
        journal=json.loads((self.job/'receipt.json').read_text())
        adopted=[json.loads((self.session_root/r['rawRef']).read_text())['result']['status'] for r in journal['steps'] if r['name']=='accept_child']
        self.assertEqual(adopted,['accepted','already_accepted'])
        self.assertEqual(result['workerEvidence']['toolCounts']['test'],1)

    def test_acceptance_partial_file_write_stays_unknown_and_never_replays(self):
        self.enable();original=_atomic
        def fail_parent(path,raw,*args,**kwargs):
            if str(path).endswith('/workspace/answer.txt') and '/children/' not in str(path):
                original(path,raw,*args,**kwargs);raise OSError('simulated reply loss after parent write')
            return original(path,raw,*args,**kwargs)
        with patch('worker_workspace._atomic',side_effect=fail_parent):
            with self.assertRaises((WorkerExecutionError,ValueError,OSError)):
                self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),child_final(),self.adopt])
        journal=json.loads((self.job/'receipt.json').read_text());self.assertEqual(journal['status'],'unknown')
        before=len(self.client.calls)
        with self.assertRaises((WorkerExecutionError,ValueError)):self.execute()
        self.assertEqual(len(self.client.calls),before)
        self.assertIsNotNone(json.loads(self.workspace().state_path.read_text())['pendingWrite'])

    def test_atomic_adoption_validates_all_files_before_first_write(self):
        self.enable();ws=self.workspace();ws.prepare();before=ws.fingerprint()
        with self.assertRaises(ValueError):
            ws.accept_changes([{'path':'answer.txt','content':'good','beforeSha256':None},
                               {'path':'outside.txt','content':'bad','beforeSha256':None}],before)
        self.assertEqual(ws.fingerprint(),before)
        self.assertFalse((ws.workspace/'answer.txt').exists())

    def test_corrupted_child_workspace_blocks_completed_parent_quality_reuse(self):
        self.enable()
        result,_,_=self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),child_final(),self.adopt,final()])
        child=result['workerEvidence']['subagents'][0]
        path=self.workspace().root/'children'/child['id']/'workspace/answer.txt';path.write_text('tampered')
        with self.assertRaisesRegex(WorkerExecutionError,'子任务证据'):
            verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)


if __name__=='__main__':unittest.main()
