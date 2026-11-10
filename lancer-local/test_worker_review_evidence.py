"""Real Worker process receipts become reviewer-citable, never invented evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Store
from studio import Studio
from quality_workflow import verify_review_delivery
import test_studio_business as business
import test_worker_execution as worker_fixture

CANARY = 'UNRELATED_PRIVATE_SOURCE_BODY_MUST_NOT_ENTER_REVIEW_REPORT'
AUTHOR_CLAIM = 'MODEL_AUTHORED_CHILD_SUMMARY_IS_NOT_HOST_PROCESS_EVIDENCE'


def tool(name, **arguments):return {'tool':{'name':name,'arguments':arguments}}


class ProcessReviewClient(business.BusinessClient):
    def __init__(self):
        super().__init__()
        self.studio=None
        self.reproduce=True
        self.parent_count=self.child_count=0
        self.all_calls=[]
        self.review_shared=[]
        self.claimed_process_paths=[]

    def call(self,model,messages,cancel,on_delta=None):
        child=messages[0]['content'].startswith('你是父Worker派生的执行子智能体')
        parent=not child and any('这是已授权的workspace执行闭环' in m['content'] for m in messages)
        stage='child' if child else 'parent' if parent else 'business'
        self.all_calls.append({'stage':stage,'model':model})
        receipt={'mode':'fixture','requestedModel':model,'returnedModel':model,'protocol':'chat',
                 'usage':{'prompt_tokens':20,'completion_tokens':10},'responseComplete':True,'jsonValidated':True}
        if child:
            answers=[tool('read',path='answer.txt'),
                tool('write',path='answer.txt',content='good',expectedSha256=hashlib.sha256(b'bad').hexdigest()),
                tool('test',checkId='process-check'),{'summary':AUTHOR_CLAIM,'findings':['Fixed in own child workspace']}]
            if self.child_count>=len(answers):raise AssertionError('Unexpected child replay')
            result=answers[self.child_count];self.child_count+=1
            return result,receipt
        if parent:
            step=self.parent_count;self.parent_count+=1
            if self.reproduce:
                if step==0:return tool('test',checkId='process-check'),receipt
                step-=1
            if step==0:
                return tool('delegate',task='Repair answer.txt using the approved fixed check.',mode='execute',scope=['answer.txt'],checkIds=['process-check']),receipt
            paths=list((Path(self.studio.store.root)/'project-sessions').glob('*/*/worker-executions/*/*/receipt.json'))
            assert len(paths)==1
            child_row=json.loads(paths[0].read_text())['subagents'][0]
            if step==1:return tool('inspect_child',childId=child_row['id'],path='answer.txt'),receipt
            if step==2:return tool('accept_child',childId=child_row['id'],expectedFingerprint=child_row['proposalFingerprint']),receipt
            if step!=3:raise AssertionError('Unexpected parent replay')
            return {'summary':'Actual files and tool receipts collected','notes':[],'checks':[],'questions':[]},receipt
        memory=json.loads(messages[-1]['content'].split('：\n',1)[1].split('\n本轮角色任务：\n',1)[0])
        shared=memory['sharedEvidence']
        result,original=super().call(model,messages,cancel,on_delta)
        if shared['businessStage']=='quality':
            self.review_shared.append(deepcopy(shared))
            reports=[r for r in shared['reviewFiles'] if r['path'].startswith('worker-execution-evidence/')]
            if reports:
                report=reports[0];parsed=json.loads(report['content'])
                criterion=result['criteria'][-1]
                if self.reproduce:
                    line=next((i+1 for i,text in enumerate(report['content'].splitlines()) if '"inspect_child"' in text),1)
                    criterion.update(reason='Actual host process report records the requested Worker path',
                        evidence=[{'path':report['path'],'startLine':line,'endLine':line}])
                    self.claimed_process_paths.append(report['path'])
                elif not parsed['facts']['initialWorkspaceTests']:
                    criterion.update(status='unverified',reason='No actual initial parent test receipt is present',evidence=[])
        if shared['businessStage']=='integration' and not self.reproduce:
            result['accepted']=False
            row=result['moduleAcceptance'][0]
            row.update(accepted=False,unmet=['No actual initial parent failure receipt'],repairAction='needs_input')
            row['criteria'][-1].update(status='unverified',evidence=[])
        return result,{**original,**receipt}


class WorkerReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='worker-review-proof-')
        self.root=Path(self.temp.name);self.store=Store(self.root/'data',seed=False)
        self.client=ProcessReviewClient();self.studio=Studio(self.store,self.client);self.client.studio=self.studio
        self.pid=self.studio.create_project('Worker review proof fixture','No external requests')['id']
        revision=self.store.current(self.pid)[0]['revision']
        revision=self.store.save_file(self.pid,'answer.txt',b'bad',revision)['revision']
        self.store.save_file(self.pid,'unrelated.txt',CANARY.encode(),revision)
        checker=self.root/'fixed-check.py';checker.write_text(worker_fixture.GRADER)
        check=self.studio.host_acceptance.register({'id':'process-check','name':'Actual answer check','scriptPath':str(checker),
            'files':['answer.txt'],'criteria':[{'id':'C1','description':'answer.txt must contain the required text'}],'timeoutSeconds':5})
        project=self.studio.project(self.pid);project=self.studio.set_host_acceptance(self.pid,project['version'],[check['id']])
        config=deepcopy(project['teamConfig']);config['workerTools']={'version':1,'roleGrants':{
            'backend':{'enabled':True,'checkIds':[check['id']],'childExecution':True}}}
        self.studio.update_project(self.pid,'Require actual tool receipts and independent review.',project['version'],config)
        self.session=self.studio.create_session(self.pid,'Worker process evidence','Repair only answer.txt and preserve actual process proof.','general')
        self.route={'mode':'direct','contract':{'roleId':'backend','workType':'development','scope':['answer.txt'],
            'instructions':'Reproduce the failure, delegate to a same-model execution child, inspect and accept its change, then retest.',
            'acceptance':['answer.txt contains the required text.',
                          'Actual parent failure, execution child, candidate inspection, acceptance and parent retest are independently reviewed.'],
            'complexity':2,'skills':{'coding':1},'requiredCapabilities':['text','workspace']}}
        self.network=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No real network allowed'));self.network.start()

    def tearDown(self):
        self.studio.triggers.close();self.studio.shutdown_admission()
        for event in self.studio.cancels.values():event.set()
        for thread in list(self.studio.threads.values()):
            if thread.is_alive():thread.join(10)
        self.network.stop();self.temp.cleanup()

    def start(self,resume=False):
        self.studio.start(self.pid,self.session['id'],resume=resume,
            **({} if resume else {'coordinator_model':'opus','team_models':['opus','sol','gemini'],'task_route':self.route}))
        thread=self.studio.threads[self.session['id']];thread.join(15)
        self.assertFalse(thread.is_alive())
        self.session=self.studio.load(self.pid,self.session['id']);return self.session['runs'][-1]

    def root_for(self):return self.studio.project_dir(self.pid)/self.session['id']

    def completed(self):
        run=self.start();self.assertEqual(self.session['status'],'completed',self.session.get('error'))
        files={r['path']:(self.store.current(self.pid)[1]/r['path']).read_bytes() for r in run['artifacts']}
        verify_review_delivery(self.studio,self.session,run,files)
        return run,files

    def journal(self,run):
        node=next(n for n in run['nodes'] if n['phase']=='build')
        path=self.root_for()/node['result']['workerEvidence']['receiptRef']
        return node,json.loads(path.read_text())

    def process_report(self):
        shared=self.client.review_shared[-1]
        reports=[f for f in shared['reviewFiles'] if f['path'].startswith('worker-execution-evidence/')]
        self.assertEqual(len(reports),1)
        return reports[0],json.loads(reports[0]['content'])

    def test_actual_child_inspection_adoption_and_retest_are_citable_review_inputs(self):
        run,files=self.completed();report,parsed=self.process_report();node,journal=self.journal(run)
        self.assertEqual(run['workerReviewEvidenceVersion'],2)
        self.assertEqual(parsed['schema'],'yuki-worker-review-evidence/v2')
        self.assertEqual(parsed['source']['nodeId'],node['id'])
        self.assertEqual(parsed['source']['receiptSha256'],node['result']['workerEvidence']['receiptSha256'])
        self.assertIn(report['path'],self.client.claimed_process_paths)
        counts=parsed['facts']['completedToolCounts']
        self.assertEqual((counts.get('delegate'),counts.get('inspect_child'),counts.get('accept_child')),(1,1,1))
        self.assertEqual(len(parsed['facts']['initialWorkspaceTests']),1)
        self.assertFalse(parsed['facts']['initialWorkspaceTests'][0]['passed'])
        self.assertNotIn(report['path'],files)
        self.assertNotIn(CANARY,report['content']);self.assertNotIn(AUTHOR_CLAIM,report['content'])
        row=run['qualityHistory'][-1]['modules'][0]
        self.assertEqual(row['reviewEvidence']['binding']['workerReviewEvidenceVersion'],2)
        bound=next(f for f in row['reviewEvidence']['binding']['reviewFiles'] if f['path']==report['path'])
        self.assertEqual(bound['sha256'],hashlib.sha256(report['content'].encode()).hexdigest())
        refs=row['independentReview']['criteria'][-1]['evidence']
        self.assertEqual(refs[0]['path'],report['path'])

    def test_missing_original_parent_test_is_empty_and_remains_unverified(self):
        self.client.reproduce=False
        run=self.start();self.assertEqual(self.session['status'],'failed',self.session.get('error'))
        report,parsed=self.process_report()
        self.assertEqual(parsed['facts']['initialWorkspaceTests'],[])
        self.assertEqual(run['qualityHistory'][0]['modules'][0]['independentReview']['criteria'][-1]['status'],'unverified')
        self.assertNotIn('mergedRevision',run)

    def test_child_receipts_and_model_context_damage_blocks_delivery_verification(self):
        run,files=self.completed();node,journal=self.journal(run);child=journal['subagents'][0]
        initial_step=json.loads((self.root_for()/journal['steps'][0]['rawRef']).read_text())
        self.assertFalse(initial_step['result']['passed'])
        self.assertTrue(initial_step['result']['receipt']['executed'])
        initial_failed_stdout=self.root_for()/initial_step['result']['receipt']['stdoutRef']
        targets=[initial_failed_stdout,
                 self.root_for()/journal['steps'][0]['rawRef'],
                 self.root_for()/child['rawRef'],
                 self.root_for()/child['tests']['process-check']['receipt']['receiptRef'],
                 self.root_for()/'contexts'/(child['contextRefs'][0]+'.json'),
                 self.root_for()/'worker-workspaces'/run['id']/node['id']/'children'/child['id']/'binding.json']
        for path in targets:
            with self.subTest(path=path.name):
                raw=path.read_bytes();calls=len(self.client.all_calls)
                try:
                    path.write_text('{}')
                    with self.assertRaises(ValueError):verify_review_delivery(self.studio,self.session,run,files)
                    self.assertEqual(len(self.client.all_calls),calls)
                finally:path.write_bytes(raw)

    def test_review_context_report_cannot_be_replaced_without_rebinding_originals(self):
        run,files=self.completed();row=run['qualityHistory'][-1]['modules'][0]
        path=self.root_for()/'contexts'/(row['reviewEvidence']['contextRef']+'.json')
        data=json.loads(path.read_text());report=next(f for f in data['memory']['sharedEvidence']['reviewFiles'] if f['path'].startswith('worker-execution-evidence/'))
        report['content']='{"schema":"forged"}'
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):verify_review_delivery(self.studio,self.session,run,files)

    def test_corrupt_worker_process_proof_blocks_cached_review_without_more_models(self):
        self.client.fail_once=('integration',None,'opus',0)
        run=self.start();self.assertEqual(self.session['status'],'failed')
        node,journal=self.journal(run);(self.root_for()/journal['subagents'][0]['rawRef']).write_text('{}')
        calls=len(self.client.all_calls)
        self.start(resume=True)
        self.assertEqual(self.session['status'],'failed');self.assertEqual(len(self.client.all_calls),calls)
        self.assertNotIn('mergedRevision',self.session['runs'][-1])

    def test_lost_new_marker_cannot_downgrade_contract_or_review_binding(self):
        from worker_review_evidence import version
        run,files=self.completed()
        for kind in ('run','contract','binding'):
            changed=deepcopy(run)
            if kind=='run':changed.pop('workerReviewEvidenceVersion')
            elif kind=='contract':changed['taskContracts']['direct-task'].pop('workerReviewEvidenceVersion')
            else:changed['qualityHistory'][-1]['modules'][0]['reviewEvidence']['binding'].pop('workerReviewEvidenceVersion')
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):version(changed)
                with self.assertRaises(ValueError):verify_review_delivery(self.studio,self.session,changed,files)

    def test_already_published_head_finishes_without_replaying_or_requiring_old_process_files(self):
        from studio_business import _commit_delivery
        run,files=self.completed();before=self.store.current(self.pid)[0]['revision'];calls=len(self.client.all_calls)
        node,journal=self.journal(run);(self.root_for()/journal['subagents'][0]['rawRef']).write_text('{}')
        run.pop('workerReviewEvidenceVersion')
        _commit_delivery(self.studio,self.session,run)
        self.assertEqual(self.store.current(self.pid)[0]['revision'],before)
        self.assertEqual(len(self.client.all_calls),calls)
        self.assertEqual(self.session['status'],'completed')

    def test_wholly_legacy_worker_report_version_remains_zero_and_readonly(self):
        from worker_review_evidence import version,evidence_files
        module={'id':'legacy-worker','requiredCapabilities':['text','workspace']}
        contract={'id':'legacy-contract','workerBinding':{'enabled':True}}
        legacy={'plan':{'modules':[module]},'taskContracts':{'legacy-worker':contract},'nodes':[],'qualityHistory':[]}
        self.assertEqual(version(legacy,module,contract),0)
        before=deepcopy(legacy)
        self.assertEqual(evidence_files(self.studio,self.session,legacy,module,{}),[])
        self.assertEqual(legacy,before)
        self.assertEqual(self.client.all_calls,[])


if __name__=='__main__':unittest.main()
