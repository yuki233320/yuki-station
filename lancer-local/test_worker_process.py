"""Frozen process obligations are proven by actual local tools, not model claims."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_worker_execution as fixture
from test_worker_execution import tool, final
from review_evidence import fingerprint
from worker_policy import bind_role
from worker_execution import WorkerExecutionError, verify_worker_result
from worker_workspace import WorkerWorkspace


def child_final():return {'summary':'Actual child candidate checked','findings':[]}
def sha(text):return hashlib.sha256(text.encode()).hexdigest()


class WorkerProcessTests(unittest.TestCase):
    setUp=fixture.WorkerExecutionTests.setUp
    tearDown=fixture.WorkerExecutionTests.tearDown
    execute=fixture.WorkerExecutionTests.execute
    workspace=fixture.WorkerExecutionTests.workspace

    def seed(self, text='bad'):
        current=self.studio.store.current(self.pid)[0]['revision']
        state=self.studio.store.save_file(self.pid,'answer.txt',text.encode(),current)
        self.run['projectRevision']=state['revision']
        self.studio.save(self.session)

    def replace_checker(self, content, criteria=None):
        script=self.root/'process-grader.py';script.write_text(content)
        entry=self.studio.host_acceptance.register({'id':'process-check','name':'Process check',
            'scriptPath':str(script),'files':['answer.txt'],
            'criteria':criteria or [{'id':'C1','description':'Required original defect and final result'}]})
        self.check=self.studio.host_acceptance.snapshot([entry['id']])[0]
        self.run['hostAcceptanceChecks']=[self.check]
        self.policy['roleGrants']['builder']['checkIds']=[entry['id']]

    def freeze(self, baseline=True, child=False, new=True):
        requirement={'version':1,'initialFailure':{'checkId':self.check['id'],'criterionIds':['C1']} if baseline else None,
                     'requireExecutionChild':child}
        grant=self.policy['roleGrants']['builder'];grant['childExecution']=child
        if baseline or child:grant['processRequirements']=requirement
        self.module['workerBinding']=bind_role(self.module,self.policy,[self.check],self.studio.host_acceptance)
        self.contract['workerBinding']=deepcopy(self.module['workerBinding'])
        if new:
            self.run.update(workerProcessVersion=1,workerReviewEvidenceVersion=2)
            self.contract.update(workerProcessVersion=1,workerProcessRequirements=requirement if baseline or child else None,
                                 workerReviewEvidenceVersion=2)
        self.contract.pop('fingerprint',None);self.contract['fingerprint']=fingerprint(self.contract)
        self.studio.save(self.session)

    def journal(self):return json.loads((self.job/'receipt.json').read_text())
    def child(self):return self.journal()['subagents'][-1]
    def inspect(self, path='answer.txt', **args):return tool('inspect_child',childId=self.child()['id'],path=path,**args)
    def adopt(self):
        child=self.child();return tool('accept_child',childId=child['id'],expectedFingerprint=child['proposalFingerprint'])
    def delegate(self):return tool('delegate',task='Fix the frozen original failure in own copy',mode='execute',
                                    scope=['answer.txt'],checkIds=[self.check['id']])
    def child_answers(self, content='good'):
        return [tool('read',path='answer.txt'),tool('write',path='answer.txt',content=content,expectedSha256=sha('bad')),
                tool('test',checkId=self.check['id']),child_final()]
    def assert_initial_failed_before_model(self):
        journal=self.journal();tests=[r for r in journal['steps'] if r['name']=='test']
        self.assertTrue(tests, 'The host must test before the first model response is consumed')
        raw=json.loads((self.session_root/tests[0]['rawRef']).read_text())
        self.assertIs(raw['result']['passed'],False)
        self.assertEqual(raw['beforeFingerprint'],journal['initialWorkspace']['workspaceFingerprint'])
        self.assertEqual(self.workspace().read('answer.txt')['content'],'bad')
        self.assertFalse(any(r['name'] in ('write','delegate','accept_child') for r in journal['steps']))

    def test_initial_failure_precedes_first_model_write_and_final_retests(self):
        self.seed();self.freeze()
        def first():
            self.assert_initial_failed_before_model()
            return tool('write',path='answer.txt',content='good',expectedSha256=sha('bad'))
        result,_,_=self.execute([first,final()])
        self.assertEqual(result['files'],[{'path':'answer.txt','content':'good'}])
        tests=[r for r in self.journal()['steps'] if r['name']=='test']
        self.assertEqual([r['testPassed'] for r in tests],[False,True])
        verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
        count=len(self.client.calls)
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('test replay')):self.execute()
        self.assertEqual(len(self.client.calls),count)

    def test_initial_failure_precedes_first_delegate_then_full_child_path(self):
        self.seed();self.freeze(child=True)
        def first():self.assert_initial_failed_before_model();return self.delegate()
        result,_,_=self.execute([first,*self.child_answers(),self.inspect,self.adopt,final()])
        journal=self.journal();names=[r['name'] for r in journal['steps']]
        self.assertLess(names.index('test'),names.index('delegate'))
        self.assertLess(names.index('inspect_child'),names.index('accept_child'))
        self.assertEqual(journal['tests'][self.check['id']]['passed'],True)
        self.assertNotEqual(journal['tests'][self.check['id']]['testRunId'],journal['subagents'][0]['tests'][self.check['id']]['testRunId'])
        verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)

    def test_first_final_cannot_skip_original_failure_or_current_checks(self):
        self.seed();self.freeze()
        def first():self.assert_initial_failed_before_model();return final()
        result,_,_=self.execute([first,tool('write',path='answer.txt',content='good',expectedSha256=sha('bad')),final()])
        self.assertEqual(result['files'][0]['content'],'good')
        self.assertGreaterEqual(len(self.client.calls),3)

    def test_already_passing_baseline_blocks_before_model_can_write_bad(self):
        self.seed('good');self.freeze()
        with self.assertRaises(WorkerExecutionError):
            self.execute([tool('write',path='answer.txt',content='bad',expectedSha256=sha('good'))])
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.workspace().read('answer.txt')['content'],'good')
        self.assertNotEqual(self.journal()['status'],'completed')
        self.assertFalse(any(r['name']=='write' for r in self.journal()['steps']))

    def test_other_failed_criterion_is_not_the_required_failure(self):
        self.seed()
        self.replace_checker("import json\nprint(json.dumps({'accepted':False,'criteria':[{'criterionId':'C1','status':'passed','detail':'target is already correct'},{'criterionId':'C2','status':'failed','detail':'unrelated defect'}]}))\nraise SystemExit(1)\n",
                             [{'id':'C1','description':'Target defect'},{'id':'C2','description':'Other defect'}])
        self.freeze()
        with self.assertRaises(WorkerExecutionError):self.execute([final()])
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.workspace().read('answer.txt')['content'],'bad')

    def test_grader_error_is_not_failure_reproduction_and_is_not_replayed(self):
        self.seed();self.replace_checker("raise RuntimeError('broken checker fixture')\n");self.freeze()
        with self.assertRaises((WorkerExecutionError,ValueError)):self.execute([final()])
        self.assertEqual(self.client.calls,[])
        before=(self.job/'receipt.json').read_bytes()
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('unknown test replay')):
            with self.assertRaises((WorkerExecutionError,ValueError)):self.execute()
        self.assertEqual((self.job/'receipt.json').read_bytes(),before)

    def test_lost_baseline_reply_remains_unknown_and_blocks_replay(self):
        self.seed();self.freeze();actual=WorkerWorkspace.run_test;calls=[]
        def lost(workspace,*args,**kwargs):
            value=actual(workspace,*args,**kwargs);calls.append(value);raise OSError('fixture lost baseline reply')
        with patch.object(WorkerWorkspace,'run_test',lost):
            with self.assertRaises((WorkerExecutionError,ValueError,OSError)):self.execute([final()])
        self.assertEqual(len(calls),1);self.assertFalse(calls[0]['passed']);self.assertEqual(self.client.calls,[])
        self.assertEqual(self.journal()['status'],'unknown')
        pending=self.session_root/'worker-process'/self.run['id']/self.module['id']/'baseline.json'
        self.assertEqual(json.loads(pending.read_text())['status'],'pending')
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('replayed baseline')):
            with self.assertRaises((WorkerExecutionError,ValueError)):self.execute()

    def test_accept_without_inspection_is_denied_before_parent_write(self):
        self.seed();self.freeze(child=True)
        def inspect_after_denial():
            self.assertEqual(self.workspace().read('answer.txt')['content'],'bad')
            self.assertEqual(self.journal()['childAdoptions'],[])
            return self.inspect()
        result,_,_=self.execute([self.delegate(),*self.child_answers(),self.adopt,inspect_after_denial,self.adopt,final()])
        self.assertEqual(len(result['workerEvidence']['childAdoptions']),1)
        accepted=[r for r in self.journal()['steps'] if r['name']=='accept_child' and r['status']=='completed']
        self.assertEqual(len(accepted),1)

    def test_error_inspection_does_not_authorize_acceptance(self):
        self.seed();self.freeze(child=True)
        def wrong():return tool('inspect_child',childId=self.child()['id'],path='not-a-changed-file.txt')
        def correct_after_denial():
            self.assertEqual(self.workspace().read('answer.txt')['content'],'bad');return self.inspect()
        result,_,_=self.execute([self.delegate(),*self.child_answers(),wrong,self.adopt,correct_after_denial,self.adopt,final()])
        self.assertEqual(result['files'][0]['content'],'good')
        self.assertTrue(any(r['name']=='inspect_child' and r['status']=='error' for r in self.journal()['steps']))

    def test_truncated_model_view_requires_complete_smaller_pages(self):
        self.seed();large=''.join('good-line-%03d:'%i+'x'*220+'\n' for i in range(100))
        self.replace_checker("import argparse,json,hashlib\nfrom pathlib import Path\np=argparse.ArgumentParser();p.add_argument('--workspace');a=p.parse_args()\nok=hashlib.sha256((Path(a.workspace)/'answer.txt').read_bytes()).hexdigest()=="+repr(sha(large))+"\nprint(json.dumps({'accepted':ok,'criteria':[{'criterionId':'C1','status':'passed' if ok else 'failed','detail':'Frozen complete text hash'}]}))\nraise SystemExit(0 if ok else 1)\n")
        self.freeze(child=True)
        def first_page_after_denial():
            self.assertEqual(self.workspace().read('answer.txt')['content'],'bad');return self.inspect(startLine=1,endLine=30)
        result,_,_=self.execute([self.delegate(),*self.child_answers(large),self.inspect,self.adopt,first_page_after_denial,
                                lambda:self.inspect(startLine=31,endLine=60),lambda:self.inspect(startLine=61,endLine=90),
                                lambda:self.inspect(startLine=91,endLine=100),self.adopt,final()])
        self.assertEqual(result['files'][0]['content'],large)
        self.assertEqual(len(result['workerEvidence']['childAdoptions']),1)

    def test_required_execution_child_cannot_be_replaced_by_parent_direct_write(self):
        self.seed();self.freeze(child=True)
        with self.assertRaisesRegex(WorkerExecutionError,'重复|无进展'):
            self.execute([tool('write',path='answer.txt',content='good',expectedSha256=sha('bad')),*[final() for _ in range(4)]])
        self.assertNotEqual(self.journal()['status'],'completed')
        self.assertFalse(self.journal()['childAdoptions'])

    def test_inspection_of_identical_bytes_from_another_proposal_does_not_count(self):
        self.seed();self.freeze(child=True)
        first_id=[]
        def inspect_first():first_id.append(self.child()['id']);return self.inspect()
        def inspect_second_after_denial():
            self.assertNotEqual(self.child()['id'],first_id[0])
            self.assertEqual(self.workspace().read('answer.txt')['content'],'bad')
            self.assertEqual(self.journal()['childAdoptions'],[])
            return self.inspect()
        result,_,_=self.execute([self.delegate(),*self.child_answers(),inspect_first,
                                self.delegate(),*self.child_answers(),self.adopt,inspect_second_after_denial,self.adopt,final()])
        self.assertEqual(len(result['workerEvidence']['childAdoptions']),1)
        self.assertEqual(result['workerEvidence']['childAdoptions'][0]['childId'],self.child()['id'])

    def test_every_changed_file_must_be_inspected_before_acceptance(self):
        self.seed();current=self.studio.store.current(self.pid)[0]['revision']
        self.run['projectRevision']=self.studio.store.save_file(self.pid,'extra.txt',b'old extra',current)['revision']
        self.module['scope']=['answer.txt','extra.txt'];self.contract['scope']=list(self.module['scope'])
        self.freeze(child=True)
        delegate=tool('delegate',task='Update both frozen files',mode='execute',scope=['answer.txt','extra.txt'],checkIds=[self.check['id']])
        def inspect_extra_after_denial():
            self.assertEqual(self.workspace().read('answer.txt')['content'],'bad')
            self.assertEqual(self.workspace().read('extra.txt')['content'],'old extra')
            return self.inspect('extra.txt')
        result,_,_=self.execute([delegate,tool('write',path='answer.txt',content='good',expectedSha256=sha('bad')),
            tool('write',path='extra.txt',content='new extra',expectedSha256=sha('old extra')),child_final(),
            self.inspect,self.adopt,inspect_extra_after_denial,self.adopt,final()])
        self.assertEqual({f['path']:f['content'] for f in result['files']},{'answer.txt':'good','extra.txt':'new extra'})
        self.assertEqual(len(result['workerEvidence']['childAdoptions']),1)

    def test_a_parent_test_before_adoption_cannot_be_relabelled_as_post_adoption(self):
        from worker_execution import _load_journal
        from worker_process import verify
        self.seed();self.freeze(child=True)
        self.execute([self.delegate(),*self.child_answers(),self.inspect,self.adopt,final()])
        journal=_load_journal(self.job/'receipt.json')
        last_test=max(i for i,r in enumerate(journal['steps']) if r['name']=='test')
        test=journal['steps'].pop(last_test)
        adoption=next(i for i,r in enumerate(journal['steps']) if r['name']=='accept_child')
        journal['steps'].insert(adoption,test)
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('verification reran test')):
            with self.assertRaises(ValueError):
                verify(self.studio,self.session,self.run,self.node,self.workspace(),journal,self.session_root,final=True)

    def test_repair_reuses_verified_original_failure_without_rerunning_it(self):
        from app import uid
        self.seed();self.freeze()
        result,_,_=self.execute([tool('write',path='answer.txt',content='good',expectedSha256=sha('bad')),final()])
        baseline=self.session_root/'worker-process'/self.run['id']/self.module['id']/'baseline.json'
        old_proof=baseline.read_bytes();original_node=self.node
        original_node.update(status='done',result=result)
        self.node=self.studio.node(self.run,'repair-1-answer','Repair answer','deepseek','build')
        self.node.update(moduleId='answer',roleId='builder',agentId='worker-builder',executorId='workspace',repairRound=1)
        self.shared={**self.shared,'repair':{'previousFiles':deepcopy(result['files']),'feedback':[]}}
        self.context=deepcopy(self.context);self.context['id']=uid()
        self.studio._write(self.session_root/'contexts'/(self.context['id']+'.json'),self.context)
        self.job=self.session_root/'worker-executions'/self.run['id']/self.node['id']
        actual=WorkerWorkspace.run_test;checks=[]
        def counted(workspace,*args,**kwargs):
            checks.append((workspace.node['id'],workspace.fingerprint()));return actual(workspace,*args,**kwargs)
        with patch.object(WorkerWorkspace,'run_test',counted):repaired,_,_=self.execute([final()])
        self.assertEqual(baseline.read_bytes(),old_proof)
        self.assertEqual(len(checks),1,'Only the repair candidate final check may execute')
        self.assertEqual(checks[0][0],self.node['id'])
        inherited=self.journal()['processBaseline'];self.assertEqual(inherited['sourceNodeId'],original_node['id'])
        self.assertTrue(inherited['reused'])
        verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,repaired)

    def test_repair_candidate_without_original_baseline_cannot_fake_reproduction(self):
        self.seed();self.freeze();self.node['repairRound']=1
        self.shared['repair']={'previousFiles':[{'path':'answer.txt','content':'good'}],'feedback':[]}
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('No baseline re-creation in repair')):
            with self.assertRaises(WorkerExecutionError):self.execute([final()])
        self.assertEqual(self.client.calls,[])

    def test_baseline_proof_and_failed_test_originals_are_rechecked_without_execution(self):
        self.seed();self.freeze()
        result,_,_=self.execute([tool('write',path='answer.txt',content='good',expectedSha256=sha('bad')),final()])
        journal=self.journal();first=json.loads((self.session_root/journal['steps'][0]['rawRef']).read_text())
        targets=[self.session_root/journal['processBaseline']['ref'],
                 self.session_root/first['result']['receipt']['stdoutRef'],
                 self.session_root/journal['steps'][0]['rawRef']]
        for path in targets:
            with self.subTest(path=path.name):
                original=path.read_bytes();before=len(self.client.calls)
                try:
                    path.write_text('{}')
                    with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('verification executed test')):
                        with self.assertRaises((WorkerExecutionError,ValueError)):
                            verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
                finally:path.write_bytes(original)
                self.assertEqual(len(self.client.calls),before)

    def test_lost_process_markers_cannot_weaken_a_completed_required_contract(self):
        from worker_process import requirements
        self.seed();self.freeze(child=True)
        result,_,_=self.execute([self.delegate(),*self.child_answers(),self.inspect,self.adopt,final()])
        for container,key in ((self.run,'workerProcessVersion'),(self.contract,'workerProcessVersion'),
                              (self.contract,'workerProcessRequirements')):
            with self.subTest(key=key):
                original=container.pop(key)
                try:
                    with self.assertRaises((WorkerExecutionError,ValueError)):requirements(self.run,self.module,self.contract)
                    with self.assertRaises((WorkerExecutionError,ValueError)):
                        verify_worker_result(self.studio,self.session,self.run,self.node,self.shared,result)
                finally:container[key]=original

    def test_persisted_tool_start_without_baseline_proof_blocks_reexecution(self):
        self.seed();self.freeze()
        self.studio.event(self.session,'worker_tool_started','Interrupted baseline start',self.node['id'],name='test')
        self.studio.save(self.session)
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('started tool replay')):
            with self.assertRaises(WorkerExecutionError):self.execute([final()])
        self.assertEqual(self.client.calls,[])

    def test_missing_existing_worker_job_cannot_be_recreated_for_a_required_process(self):
        import shutil
        self.seed();self.freeze()
        self.execute([tool('write',path='answer.txt',content='good',expectedSha256=sha('bad')),final()])
        self.assertTrue(self.node.get('workerEvidence'))
        before=len(self.client.calls);shutil.rmtree(self.job)
        with patch.object(WorkerWorkspace,'run_test',side_effect=AssertionError('deleted job replay')):
            with self.assertRaises(WorkerExecutionError):self.execute()
        self.assertEqual(len(self.client.calls),before)
        self.assertFalse((self.job/'receipt.json').exists())

    def test_legacy_and_explicit_no_requirements_do_not_gain_baseline_obligation(self):
        for new in (False,True):
            with self.subTest(new=new):
                if new:
                    # Separate fresh job for the explicit new-version/no-requirements case.
                    self.tearDown();self.setUp()
                self.seed();self.freeze(baseline=False,child=False,new=new)
                def first():
                    self.assertEqual(self.journal()['steps'],[])
                    return tool('write',path='answer.txt',content='good',expectedSha256=sha('bad'))
                result,_,_=self.execute([first,final()])
                self.assertEqual(result['files'][0]['content'],'good')


if __name__=='__main__':unittest.main()
