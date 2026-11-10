"""Versioned host reports preserve V1 while binding enforced V2 process proofs."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_worker_process as process_fixture
from test_worker_execution import tool, final
from review_evidence import fingerprint
from worker_review_evidence import evidence_files, integration_paths, requirements_fingerprint, version


class WorkerProcessEvidenceTests(unittest.TestCase):
    def fixture(self):
        case=process_fixture.WorkerProcessTests('test_initial_failure_precedes_first_model_write_and_final_retests')
        case.setUp();self.addCleanup(case.tearDown)
        return case

    def complete(self, required=True):
        f=self.fixture();f.seed();f.freeze(baseline=required,child=required)
        choices=([f.delegate(),*f.child_answers(),f.inspect,f.adopt,final()] if required else
                 [tool('write',path='answer.txt',content='good',expectedSha256=process_fixture.sha('bad')),final()])
        output,_,_=f.execute(choices)
        f.node.update(status='done',result=output)
        return f,output

    def shared(self,f,output,reports):
        return {'taskContracts':f.run['taskContracts'],'modules':{f.module['id']:output},
                'workerExecutionReports':reports,
                'workerExecutionEvidencePathsByModule':{f.module['id']:[r['path'] for r in reports]}}

    def test_required_process_is_bound_into_real_quality_and_cached_delivery(self):
        import test_worker_review_evidence as review_fixture
        from quality_workflow import verify_review_delivery
        f=review_fixture.WorkerReviewEvidenceTests('test_actual_child_inspection_adoption_and_retest_are_citable_review_inputs')
        f.setUp();self.addCleanup(f.tearDown)
        project=f.studio.project(f.pid);config=deepcopy(project['teamConfig'])
        requirement={'version':1,'initialFailure':{'checkId':'process-check','criterionIds':['C1']},'requireExecutionChild':True}
        config['workerTools']['roleGrants']['backend']['processRequirements']=requirement
        f.studio.update_project(f.pid,project['instructions'],project['version'],config)
        run,files=f.completed();report,body=f.process_report()
        self.assertEqual(body['schema'],'yuki-worker-review-evidence/v2')
        self.assertEqual(body['process']['requirements'],requirement)
        self.assertTrue(body['process']['verification']['passed'])
        row=run['qualityHistory'][-1]['modules'][0]
        self.assertTrue(any(c['type']=='worker_process' and c['pass'] is True for c in row['checks']))
        binding=row['reviewEvidence']['binding']
        self.assertEqual(binding['workerReviewEvidenceVersion'],2)
        self.assertEqual(binding['workerProcessVersion'],1)
        self.assertEqual(binding['workerProcessRequirementsFingerprint'],requirements_fingerprint(requirement))
        count=len(f.client.all_calls)
        with patch.object(f.studio,'ask',side_effect=AssertionError('model replay')):
            verify_review_delivery(f.studio,f.session,run,files)
        self.assertEqual(len(f.client.all_calls),count)
        damaged=deepcopy(run)
        damaged['qualityHistory'][-1]['modules'][0]['reviewEvidence']['binding']['workerProcessRequirementsFingerprint']='0'*64
        with self.assertRaises(ValueError):verify_review_delivery(f.studio,f.session,damaged,files)

    def test_v2_contains_host_baseline_and_enforced_complete_child_proof(self):
        f,output=self.complete()
        with patch.object(f.studio.client,'call',side_effect=AssertionError('model replay')):
            reports=evidence_files(f.studio,f.session,f.run,f.module,output)
            paths=integration_paths(self.shared(f,output,reports))
        self.assertEqual(paths[f.module['id']],[reports[0]['path']])
        body=json.loads(reports[0]['content']);proof=body['process']['verification']
        self.assertEqual(body['schema'],'yuki-worker-review-evidence/v2')
        self.assertEqual(body['process']['requirements'],f.contract['workerProcessRequirements'])
        self.assertEqual(proof,output['workerEvidence']['processEvidence'])
        self.assertEqual(body['source']['processEvidenceFingerprint'],requirements_fingerprint(proof))
        self.assertEqual(proof['baseline']['initiator'],'host_requirement')
        self.assertEqual(proof['baseline']['actualFailedCriterionIds'],['C1'])
        self.assertFalse(proof['baseline']['reused'])
        self.assertTrue(proof['children'][0]['inspectionComplete'])
        self.assertTrue(proof['children'][0]['adopted'])
        self.assertTrue(proof['children'][0]['parentRetested'])
        first=body['parent']['steps'][0]
        self.assertEqual(first['name'],'test');self.assertEqual(first['initiator'],'host_requirement')
        self.assertEqual(first['purpose'],'initial_failure')
        self.assertTrue(first['automatic'])
        self.assertFalse(first['result']['test']['passed'])
        self.assertNotIn('"content":',reports[0]['content'])

    def test_v2_unrequired_process_stays_explicitly_null(self):
        f,output=self.complete(False)
        reports=evidence_files(f.studio,f.session,f.run,f.module,output)
        body=json.loads(reports[0]['content'])
        self.assertEqual(body['process'],{'version':1,'requirements':None,'verification':None})
        self.assertEqual(body['facts']['initialWorkspaceTests'],[])
        integration_paths(self.shared(f,output,reports))
        body['process']['verification']={'passed':True}
        altered=[{**reports[0],'content':json.dumps(body)}]
        with self.assertRaisesRegex(ValueError,'回执不一致'):
            integration_paths(self.shared(f,output,altered))

    def test_v2_process_summary_cannot_be_invented_or_mapped_to_another_requirement(self):
        f,output=self.complete();reports=evidence_files(f.studio,f.session,f.run,f.module,output)
        body=json.loads(reports[0]['content'])
        for kind in ('requirements','verification','fingerprint','output'):
            with self.subTest(kind=kind):
                changed=deepcopy(body);out=deepcopy(output)
                if kind=='requirements':changed['process']['requirements']['initialFailure']['criterionIds']=['invented']
                elif kind=='verification':changed['process']['verification']['baseline']['actualFailedCriterionIds']=[]
                elif kind=='fingerprint':changed['source']['processEvidenceFingerprint']='0'*64
                else:out['workerEvidence'].pop('processEvidence')
                changed_reports=[{**reports[0],'content':json.dumps(changed)}]
                with self.assertRaises(ValueError):integration_paths(self.shared(f,out,changed_reports))

    def test_v2_run_contract_and_review_binding_cannot_lose_or_downgrade_process_markers(self):
        f,output=self.complete()
        bound={'contractId':f.contract['id'],'workerReviewEvidenceVersion':2,'workerProcessVersion':1,
               'workerProcessRequirementsFingerprint':requirements_fingerprint(f.contract['workerProcessRequirements'])}
        f.run['nodes'].append({'id':'fake-review-binding','artifactReviewBinding':deepcopy(bound)})
        self.assertEqual(version(f.run,f.module,f.contract),2)
        cases=[('run','workerProcessVersion'),('contract','workerProcessRequirements'),
               ('binding','workerProcessVersion'),('binding','workerProcessRequirementsFingerprint')]
        for where,key in cases:
            with self.subTest(where=where,key=key):
                run=deepcopy(f.run);contract=run['taskContracts'][f.module['id']]
                target=run if where=='run' else contract if where=='contract' else run['nodes'][-1]['artifactReviewBinding']
                target.pop(key)
                with self.assertRaises(ValueError):version(run,f.module,contract)
        run=deepcopy(f.run);run['workerReviewEvidenceVersion']=1
        with self.assertRaises(ValueError):version(run)

    def test_published_v1_renderer_is_byte_identical_for_legacy_process_records(self):
        # The baseline module is the shipped source snapshot, not an imitation of
        # its formatter. Neither old contract nor execution journal is upgraded.
        f=self.fixture();f.seed();f.freeze(baseline=False,child=False,new=False)
        f.run['workerReviewEvidenceVersion']=1
        f.contract['workerReviewEvidenceVersion']=1
        f.contract.pop('fingerprint');f.contract['fingerprint']=fingerprint(f.contract)
        output,_,_=f.execute([tool('write',path='answer.txt',content='good',expectedSha256=process_fixture.sha('bad')),final()])
        f.node.update(status='done',result=output)
        baseline=Path(__file__).resolve().parents[2]/'baseline/lancer-local/worker_review_evidence.py'
        if not baseline.is_file():
            # Standalone source packages deliberately contain no development
            # baseline. The immutable V1 schema is still checked there.
            body=json.loads(evidence_files(f.studio,f.session,f.run,f.module,output)[0]['content'])
            self.assertEqual(body['schema'],'yuki-worker-review-evidence/v1')
            self.assertNotIn('process',body)
            return
        spec=importlib.util.spec_from_file_location('shipped_worker_review_v1',baseline)
        shipped=importlib.util.module_from_spec(spec);spec.loader.exec_module(shipped)
        expected=shipped.evidence_files(f.studio,f.session,f.run,f.module,output)
        actual=evidence_files(f.studio,f.session,f.run,f.module,output)
        self.assertEqual(actual,expected)
        self.assertNotIn('"process":',actual[0]['content'])


if __name__=='__main__':unittest.main()
