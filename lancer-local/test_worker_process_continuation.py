"""Follow-ups retain original failure proof while each turn keeps its own tools."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app import uid
from review_evidence import fingerprint
from worker_execution import WorkerExecutionError
from worker_process import _continuation_baseline
import test_worker_process as process_fixture
from test_worker_execution import tool, final


class ContinuationBaselineTests(unittest.TestCase):
    def setUp(self):
        self.case=process_fixture.WorkerProcessTests('test_initial_failure_precedes_first_model_write_and_final_retests')
        self.case.setUp();self.addCleanup(self.case.tearDown)
        f=self.case;f.seed();f.freeze(baseline=True,child=False)
        output,_,_=f.execute([tool('write',path='answer.txt',content='good',expectedSha256=process_fixture.sha('bad')),final()])
        f.node.update(status='done',result=output)
        self.original=deepcopy(f.run)
        self.run=deepcopy(f.run);self.run['id']=uid();self.run['nodes']=[]
        self.run['triggerContinuation']={'rootRunId':self.original['id'],'rootNodeId':f.node['id'],
            'rootContractFingerprint':f.contract['fingerprint']}
        self.journal=deepcopy(f.journal());self.journal.pop('processBaseline',None)
        self.journal['steps']=[];self.journal['modelCalls']=[]
        self.journal['binding']['runId']=self.run['id']
        self.journal['binding']['contract']=deepcopy(f.contract)
        self.journal['binding']['contract'].update(id=self.run['id']+':answer',runId=self.run['id'])
        self.journal['binding']['contract']['fingerprint']=fingerprint(self.journal['binding']['contract'])
        self.lineage=patch('trigger_continuation.validate_continuation',return_value={'rootRun':self.original})
        self.lineage.start();self.addCleanup(self.lineage.stop)

    def read(self,create=False):
        f=self.case
        return _continuation_baseline(f.studio,f.session,self.run,f.node,self.journal,f.session_root,create)

    def test_inherits_real_original_proof_without_executing_test(self):
        with patch('worker_workspace.WorkerWorkspace.run_test',side_effect=AssertionError('replayed test')):
            summary=self.read(True)
            self.journal['processBaseline']=deepcopy(summary)
            self.assertEqual(summary,self.read())
        self.assertTrue(summary['reused']);self.assertEqual(summary['inheritedFromRunId'],self.original['id'])
        self.assertEqual(summary['actualFailedCriterionIds'],['C1'])
        self.assertNotEqual(summary['ref'],summary['originalBaseline']['ref'])

    def test_original_baseline_missing_and_changed_requirement_block(self):
        f=self.case;summary=self.read(True)
        original=f.session_root/summary['originalBaseline']['ref'];raw=original.read_bytes();original.unlink()
        try:
            with self.assertRaises(WorkerExecutionError):self.read()
        finally:original.write_bytes(raw)
        self.journal['processRequirements']['initialFailure']['criterionIds']=['another']
        with self.assertRaises(WorkerExecutionError):self.read()

    def test_missing_inherited_proof_after_first_call_is_not_recreated(self):
        summary=self.read(True)
        (self.case.session_root/summary['ref']).unlink()
        self.journal['modelCalls']=[{'contextRef':'already_called'}]
        with self.assertRaises(WorkerExecutionError):self.read(True)

    def test_changed_continuation_and_recursive_root_rejected(self):
        self.read(True)
        self.run['triggerContinuation']['unexpected']='changed'
        with self.assertRaises(WorkerExecutionError):self.read()
        self.original['triggerContinuation']={'rootRunId':self.run['id']}
        with self.assertRaises(WorkerExecutionError):self.read()
