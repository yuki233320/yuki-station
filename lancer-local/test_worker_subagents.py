"""Real local workspaces/graders with fixture parent/child API responses only."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from live import Cancelled
from real_task_benchmark import measure_run
from studio_client import ModelResponseError
import test_worker_execution as fixture
from worker_execution import verify_worker_result, WorkerExecutionError
from worker_workspace import WorkerWorkspace
import worker_execution


def child_final():return {'summary':'Read-only analysis finished','findings':['The parent must write answer.txt and run its registered check.']}
tool,final=fixture.tool,fixture.final


class WorkerSubagentTests(unittest.TestCase):
    setUp=fixture.WorkerExecutionTests.setUp
    tearDown=fixture.WorkerExecutionTests.tearDown
    execute=fixture.WorkerExecutionTests.execute
    workspace=fixture.WorkerExecutionTests.workspace

    def test_child_has_separate_context_same_frozen_model_and_complete_usage(self):
        self.context['messages'].append({'role':'assistant','content':'PARENT_PRIVATE_HISTORY_SENTINEL'})
        answers=[tool('delegate',task='Read the project instructions and suggest the answer file'),
                 tool('list',limit=20),tool('read',path='brief.md'),child_final(),
                 tool('write',path='answer.txt',content='good'),final()]
        result,_,_=self.execute(answers)
        proof=result['workerEvidence'];child=proof['subagents'][0]
        self.assertEqual((child['status'],child['model'],child['depth'],child['callCount']),('completed','deepseek',1,3))
        self.assertEqual(proof['toolCounts']['delegate'],1)
        self.assertEqual(proof['toolCounts']['write'],1)
        self.assertFalse(self.run.get('qualityHistory'))
        self.assertEqual({call['model'] for call in self.client.calls},{'deepseek'})
        self.assertTrue(all(call['scope']==self.run['modelRequestProfiles'] for call in self.client.calls))
        for ref in child['contextRefs']:
            context=json.loads((self.session_root/'contexts'/(ref+'.json')).read_text())
            self.assertEqual(context['workerSubagentId'],child['id'])
            self.assertEqual(context['parentNodeId'],self.node['id'])
            self.assertNotIn('PARENT_PRIVATE_HISTORY_SENTINEL',json.dumps(context['messages']))
        rows=[json.loads(path.read_text()) for path in (self.session_root/'responses').glob('*.json')]
        metrics=measure_run(self.run,rows)
        self.assertEqual(metrics['callCount'],len(answers))
        self.assertTrue(metrics['usage']['complete'])
        self.assertEqual(metrics['usage']['inputTokens'],10*len(answers))
        self.assertEqual(metrics['usage']['outputTokens'],5*len(answers))
        self.assertIsNone(metrics['actualPaid']['amount'])
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('model replay')):
            self.assertEqual(self.execute()[0],result)
            self.assertTrue(verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result))

    def test_child_cannot_write_test_recurse_or_select_a_new_model(self):
        answers=[]
        for forbidden in (tool('write',path='answer.txt',content='evil'),tool('test',checkId='answer-check'),
                          tool('delegate',task='Create another child')):
            answers.extend([tool('delegate',task='Analyze existing files only'),forbidden])
        answers.extend([tool('delegate',task='analysis',model='other'),
                        tool('write',path='answer.txt',content='good'),final()])
        with patch.object(WorkerWorkspace,'run_test',autospec=True,side_effect=WorkerWorkspace.run_test) as run_test:
            result,_,_=self.execute(answers)
        self.assertEqual(result['files'],[{'path':'answer.txt','content':'good'}])
        self.assertEqual([row['status'] for row in result['workerEvidence']['subagents']],['failed']*3)
        self.assertEqual(run_test.call_count,1)
        self.assertEqual(result['workerEvidence']['toolCounts']['write'],1)

    def test_child_count_is_four_and_fifth_does_not_create_model_context(self):
        answers=[]
        for index in range(4):answers.extend([tool('delegate',task='Analyze section '+str(index)),child_final()])
        answers.extend([tool('delegate',task='Fifth child'),tool('write',path='answer.txt',content='good'),final()])
        result,_,_=self.execute(answers)
        self.assertEqual(len(result['workerEvidence']['subagents']),4)
        self.assertEqual(sum(row['callCount'] for row in result['workerEvidence']['subagents']),4)
        self.assertEqual(result['workerEvidence']['toolCounts']['delegate'],5)

    def test_unknown_child_request_has_originals_and_never_replays(self):
        with self.assertRaises(OSError):self.execute([tool('delegate',task='Inspect files'),OSError('lost child reply')])
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(journal['status'],'unknown')
        self.assertEqual(journal['pending']['kind'],'subagent_model')
        self.assertEqual(journal['subagents'][0]['status'],'unknown')
        self.assertEqual(journal['subagents'][0]['callCount'],1)
        self.assertTrue((self.session_root/journal['subagents'][0]['rawRef']).is_file())
        self.assertEqual(len(journal['modelCalls']),2)
        before=(self.job/'receipt.json').read_bytes()
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('model replay')):
            with self.assertRaises(WorkerExecutionError):self.execute()
        self.assertEqual((self.job/'receipt.json').read_bytes(),before)

    def test_cancelled_child_is_unknown_and_parent_does_not_write(self):
        def cancelled():self.cancel.set();raise Cancelled()
        with self.assertRaises(Cancelled):self.execute([tool('delegate',task='Inspect files'),cancelled])
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(journal['status'],'unknown')
        self.assertTrue(journal['subagents'][0]['cancelled'])
        self.assertEqual(journal['steps'],[])
        self.cancel.clear()
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('replay')):
            with self.assertRaises(WorkerExecutionError):self.execute()

    def test_child_original_corruption_blocks_completed_result_reuse(self):
        result,_,_=self.execute([tool('delegate',task='Inspect files'),child_final(),
                                tool('write',path='answer.txt',content='good'),final()])
        child=result['workerEvidence']['subagents'][0]
        path=self.session_root/child['rawRef'];original=path.read_bytes();path.write_text('{}')
        with self.assertRaisesRegex(WorkerExecutionError,'子任务原件'):
            verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
        path.write_bytes(original)
        journal=json.loads((self.job/'receipt.json').read_text())
        child_call=next(row for row in journal['modelCalls'] if row.get('subagentId')==child['id'])
        (self.session_root/child_call['responseRef']).write_text('{}')
        with self.assertRaisesRegex(WorkerExecutionError,'模型调用原件'):
            verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)

    def test_child_json_format_repair_preserves_both_model_calls(self):
        receipt={'requestedModel':'fixture-model','returnedModel':'fixture-model',
                 'usage':{'prompt_tokens':10,'completion_tokens':5},'protocol':'chat','finishReason':'stop'}
        bad=ModelResponseError('invalid child JSON',deepcopy(receipt),'not-json')
        answers=[tool('delegate',task='Inspect files'),bad,child_final(),
                 tool('write',path='answer.txt',content='good'),final()]
        result,_,_=self.execute(answers)
        child=result['workerEvidence']['subagents'][0]
        self.assertEqual(child['callCount'],2)
        self.assertEqual(len(child['contextRefs']),2)
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(len(journal['modelCalls']),5)
        metrics=measure_run(self.run,[json.loads(path.read_text()) for path in (self.session_root/'responses').glob('*.json')])
        self.assertEqual(metrics['callCount'],5);self.assertTrue(metrics['usage']['complete'])
        self.assertEqual(metrics['usage']['inputTokens'],50)

    def test_completed_child_response_that_cannot_be_journaled_stays_unknown(self):
        original=worker_execution._write
        def failing(path,value):
            if value.get('purpose')=='worker_subagent':raise OSError('fixture evidence write failed')
            return original(path,value)
        with patch.object(worker_execution,'_write',side_effect=failing):
            with self.assertRaises(OSError):
                self.execute([tool('delegate',task='Analyze files'),child_final()])
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(journal['status'],'unknown')
        self.assertEqual(journal['subagents'][0]['status'],'unknown')
        self.assertEqual(journal['pending']['kind'],'subagent_model')
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('replay')):
            with self.assertRaises(WorkerExecutionError):self.execute()

    def test_legacy_unfinished_journal_cannot_gain_delegate_during_resume(self):
        # Stop immediately after genuine workspace preparation, before any
        # model request; represent the older journal's five-tool capability.
        with patch.object(worker_execution,'_loop',return_value=(None,None,None)):
            self.execute([])
        path=self.job/'receipt.json'
        journal=worker_execution._load_journal(path)
        del journal['subagents']
        journal['baseMessages'][-1]['content']='Only list/read/search/write/test are available; final files come from the workspace.'
        worker_execution._save_journal(path,journal)
        with patch('worker_subagents.execute_subagent',side_effect=AssertionError('legacy child call')):
            result,_,_=self.execute([tool('delegate',task='Analyze files'),
                                    tool('write',path='answer.txt',content='good'),final()])
        self.assertNotIn('subagents',result['workerEvidence'])
        self.assertNotIn('delegate',result['workerEvidence']['toolCounts'])
        self.assertEqual(len(self.client.calls),3)
        self.assertTrue(all('workerSubagentId' not in json.loads(path.read_text())
                            for path in (self.session_root/'contexts').glob('*.json')))


class ExecutionChildTests(unittest.TestCase):
    tearDown=fixture.WorkerExecutionTests.tearDown
    execute=fixture.WorkerExecutionTests.execute
    workspace=fixture.WorkerExecutionTests.workspace
    def setUp(self):
        fixture.WorkerExecutionTests.setUp(self)
        from worker_policy import normalize_policy,bind_role
        from review_evidence import fingerprint
        self.policy=normalize_policy({'version':1,'roleGrants':{'builder':{
            'enabled':True,'checkIds':['answer-check'],'childExecution':True}}})
        self.module['workerBinding']=bind_role(self.module,self.policy,[self.check],self.studio.host_acceptance)
        self.run['workerTools']=self.policy
        self.contract['workerBinding']=deepcopy(self.module['workerBinding'])
        self.contract.pop('fingerprint');self.contract['fingerprint']=fingerprint(self.contract)
        self.studio.save(self.session)

    def delegate(self,**extra):
        return tool('delegate',task='Implement answer.txt and prove it with the registered checker',
                    mode='execute',scope=['answer.txt'],checkIds=['answer-check'],**extra)

    def adopt(self):
        journal=json.loads((self.job/'receipt.json').read_text());child=journal['subagents'][-1]
        self.assertFalse((self.workspace().workspace/'answer.txt').exists(),'child must not mutate parent')
        return tool('accept_child',childId=child['id'],expectedFingerprint=child['proposalFingerprint'])

    def test_execute_child_repairs_real_failure_then_parent_adopts_and_retests(self):
        import hashlib
        answers=[self.delegate(),tool('write',path='answer.txt',content='bad'),
                 tool('test',checkId='answer-check'),tool('read',path='answer.txt'),
                 tool('write',path='answer.txt',content='good',expectedSha256=hashlib.sha256(b'bad').hexdigest()),
                 tool('test',checkId='answer-check'),child_final(),self.adopt,final()]
        result,_,_=self.execute(answers)
        journal=json.loads((self.job/'receipt.json').read_text());child=journal['subagents'][0]
        self.assertEqual(child['status'],'completed');self.assertEqual(child['mode'],'execute')
        self.assertIn('+good',child['changes'][0]['diff'])
        self.assertFalse(child['changes'][0]['diffTruncated'])
        self.assertEqual([r['result']['passed'] for r in child['tools'] if r['action']['name']=='test'],[False,True])
        self.assertEqual(result['files'],[{'path':'answer.txt','content':'good'}])
        self.assertEqual(len(journal['childAdoptions']),1)
        self.assertNotEqual(child['tests']['answer-check']['testRunId'],journal['tests']['answer-check']['testRunId'])
        self.assertEqual(len(journal['modelCalls']),len(answers));self.assertEqual({r['model'] for r in self.client.calls},{'deepseek'})
        self.assertFalse(self.run.get('qualityHistory'))
        self.assertTrue(verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result))
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('replay')):
            self.assertEqual(self.execute()[0],result)

    def test_child_does_not_need_parent_to_adopt_and_cannot_replace_parent_result(self):
        answers=[self.delegate(),tool('write',path='answer.txt',content='good'),child_final(),
                 tool('write',path='answer.txt',content='good'),final()]
        result,_,_=self.execute(answers)
        journal=json.loads((self.job/'receipt.json').read_text())
        self.assertEqual(journal['childAdoptions'],[])
        self.assertTrue(verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result))
        from worker_subagents import verify_execution_child
        child=journal['subagents'][0]
        self.assertEqual(verify_execution_child(self.workspace(),child,self.session_root),
                         [{'path':'answer.txt','content':'good','beforeSha256':None}])

    def test_execute_child_scope_and_checks_cannot_expand_parent_authority(self):
        answers=[tool('delegate',mode='execute',task='escape',scope=['outside.txt'],checkIds=['answer-check']),
                 tool('delegate',mode='execute',task='escape',scope=['answer.txt'],checkIds=['unknown']),
                 self.delegate(),tool('write',path='outside.txt',content='bad'),
                 tool('write',path='answer.txt',content='good'),child_final(),self.adopt,final()]
        result,_,_=self.execute(answers);child=json.loads((self.job/'receipt.json').read_text())['subagents'][0]
        self.assertEqual(len(result['workerEvidence']['subagents']),1)
        self.assertEqual(child['tools'][0]['result']['status'],'error')
        self.assertFalse((self.workspace().workspace/'outside.txt').exists())

    def test_execute_child_test_lost_result_blocks_parent_and_never_replays(self):
        from child_workspace import ChildWorkspace
        with patch.object(ChildWorkspace,'run_test',side_effect=OSError('child grader reply lost')):
            with self.assertRaises(OSError):self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),
                                                        tool('test',checkId='answer-check')])
        journal=json.loads((self.job/'receipt.json').read_text());self.assertEqual(journal['status'],'unknown')
        self.assertEqual(journal['subagents'][0]['status'],'unknown')
        self.assertEqual(journal['pending']['kind'],'subagent_tool')
        self.assertFalse((self.workspace().workspace/'answer.txt').exists())
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('replay')):
            with self.assertRaises(WorkerExecutionError):self.execute()

    def test_execute_child_test_receipt_corruption_blocks_reuse(self):
        result,_,_=self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),
                                child_final(),self.adopt,final()])
        child=json.loads((self.job/'receipt.json').read_text())['subagents'][0]
        path=self.session_root/child['tests']['answer-check']['receipt']['receiptRef'];path.write_text('{}')
        with self.assertRaises(WorkerExecutionError):
            verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)

    def test_execute_child_cannot_recurse(self):
        result,_,_=self.execute([self.delegate(),tool('delegate',task='recurse'),
                                tool('write',path='answer.txt',content='good'),final()])
        self.assertEqual(result['workerEvidence']['subagents'][0]['status'],'failed')
        self.assertEqual(len(result['workerEvidence']['subagents']),1)

    def test_large_child_diff_is_explicitly_truncated_but_full_candidate_is_preserved(self):
        from child_workspace import ChildWorkspace
        from worker_subagents import _proposal
        from worker_workspace import _sha
        parent=self.workspace();parent.prepare();base=parent.fingerprint()
        workspace=ChildWorkspace(parent,'longdiff',['answer.txt'],['answer-check'],base)
        workspace.prepare();content='a'*8000
        workspace.write('answer.txt',content)
        child={'id':'longdiff','model':'deepseek','workspaceFingerprint':base,
               'workspaceBindingSha256':_sha(workspace.meta_path.read_bytes()),
               'scope':['answer.txt'],'checkIds':['answer-check'],'tests':{}}
        proposal=_proposal(workspace,child);change=proposal['changes'][0]
        self.assertEqual(len(change['diff']),4000);self.assertTrue(change['diffTruncated'])
        self.assertEqual(workspace.changes()[0]['content'],content)
        self.assertEqual(change['afterSha256'],_sha(content.encode()))
        self.assertFalse((parent.workspace/'answer.txt').exists())


    def test_execute_child_unknown_model_preserves_every_call_and_parent_copy(self):
        with self.assertRaises(OSError):
            self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),OSError('lost model reply')])
        journal=json.loads((self.job/'receipt.json').read_text());child=journal['subagents'][0]
        self.assertEqual(child['status'],'unknown');self.assertEqual(child['callCount'],2)
        self.assertEqual(len(journal['modelCalls']),3)
        self.assertFalse((self.workspace().workspace/'answer.txt').exists())
        with patch.object(self.studio,'request_model_response',side_effect=AssertionError('replay')):
            with self.assertRaises(WorkerExecutionError):self.execute()

    def test_execute_child_non_assertion_test_outcome_stops_unknown(self):
        from child_workspace import ChildWorkspace
        for status in ('error','timeout','unknown'):
            with self.subTest(status=status), patch.object(ChildWorkspace,'run_test',return_value={'status':status,'passed':False}):
                with self.assertRaises(WorkerExecutionError):
                    self.execute([self.delegate(),tool('write',path='answer.txt',content='good'),tool('test',checkId='answer-check')])
            journal=json.loads((self.job/'receipt.json').read_text())
            self.assertEqual(journal['status'],'unknown')
            self.assertEqual(journal['subagents'][0]['status'],'unknown')
            # A fresh independent fixture for each outcome; never reset journals.
            if status!='unknown':self.tearDown();self.setUp()

    def test_child_grader_reads_unchanged_parent_scope_without_child_write_authority(self):
        from worker_policy import bind_role
        from review_evidence import fingerprint
        self.studio.host_acceptance.register({'id':'answer-check','name':'Answer and reference',
            'scriptPath':str(self.root/'grader.py'),'files':['answer.txt','reference.txt'],
            'criteria':[{'id':'C1','description':'Answer is good'}]})
        self.check=self.studio.host_acceptance.snapshot(['answer-check'])[0]
        self.module['scope']=['answer.txt','reference.txt']
        self.module['workerBinding']=bind_role(self.module,self.policy,[self.check],self.studio.host_acceptance)
        self.contract.update(scope=list(self.module['scope']),workerBinding=deepcopy(self.module['workerBinding']))
        self.contract.pop('fingerprint');self.contract['fingerprint']=fingerprint(self.contract)
        self.run['hostAcceptanceChecks']=[self.check];self.studio.save(self.session)
        answers=[tool('write',path='reference.txt',content='readonly reference'),self.delegate(),
                 tool('read',path='reference.txt'),tool('write',path='reference.txt',content='bad'),
                 tool('write',path='answer.txt',content='good'),child_final(),self.adopt,final()]
        result,_,_=self.execute(answers);child=json.loads((self.job/'receipt.json').read_text())['subagents'][0]
        self.assertEqual(child['tools'][1]['result']['status'],'error')
        self.assertEqual({r['path']:r['content'] for r in result['files']},
                         {'answer.txt':'good','reference.txt':'readonly reference'})
        self.assertTrue(child['tests']['answer-check']['passed'])
        self.assertTrue(verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result))


if __name__=='__main__':unittest.main()
