"""Real local host scripts run before deterministic model quality reviews."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import Store
from studio import Studio
from test_studio_business import BusinessClient, module


CHECKER = '''import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--workspace',required=True); args=p.parse_args()
good='GOOD' in (Path(args.workspace)/'brief.md').read_text()
print(json.dumps({'accepted':good,'criteria':[{'criterionId':'business-1','status':'passed' if good else 'failed','detail':'Candidate must contain GOOD'}]}))
raise SystemExit(0 if good else 1)
'''


class HostAwareClient(BusinessClient):
    def __init__(self, development=False, good_first=True, good_repair=True, obey_host=True):
        super().__init__([module('brief', 'development' if development else 'writing', 3 if development else 1)])
        self.good_first, self.good_repair, self.obey_host = good_first, good_repair, obey_host
        self.host_contexts = []

    def call(self, model, messages, cancel, on_delta=None):
        memory = json.loads(messages[-1]['content'].split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])
        shared = memory['sharedEvidence']
        result, receipt = super().call(model, messages, cancel, on_delta)
        stage = shared['businessStage']
        if stage == 'build':
            good = self.good_repair if shared.get('repair') else self.good_first
            result['files'][0]['content'] += '\nGOOD' if good else '\nNEEDS_FIX'
        if stage in ('quality', 'integration'):
            host = shared.get('hostAcceptance', [])
            self.host_contexts.append(copy.deepcopy(shared))
            proved = bool(host) and all(row['passed'] and row['executed'] is True and row['receiptRef'] for row in host)
            if self.obey_host:
                if stage == 'quality':
                    report = next((f for f in shared['reviewFiles'] if f['path'].startswith('host-acceptance-evidence/')), None)
                    for criterion in result['criteria']:
                        criterion['status'] = 'met' if proved else 'unverified'
                        criterion['reason'] = '已核对本轮真实宿主回执' if proved else '缺少通过的本轮宿主执行证据'
                        if report:
                            criterion['evidence'] = [{'path': report['path'], 'startLine': 1, 'endLine': 1}]
                else:
                    result['accepted'] = proved
                    for row in result['moduleAcceptance']:
                        row.update(accepted=proved, unmet=[] if proved else ['宿主业务检查尚未通过'])
                        paths = shared.get('hostAcceptanceEvidencePathsByModule', {}).get(row['moduleId'], [])
                        row['evidence'] += paths
                        for criterion in row['criteria']:
                            criterion['status'] = 'met' if proved else 'unverified'
                            if paths:
                                criterion['evidence'] = paths
        return result, receipt


class HostQualityFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'data', seed=False)
        self.client = HostAwareClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('宿主验收前置', '产物实际检查后再审查', '不把模型判断冒充运行成功')['id']
        self.session = self.studio.create_session(self.pid, '固定业务检查', '交付brief并满足已登记业务标准', 'general')

    def tearDown(self):
        for cancel in self.studio.cancels.values():
            cancel.set()
        for worker in self.studio.threads.values():
            worker.join(10)
        self.tmp.cleanup()

    def register(self, script=CHECKER):
        path = self.root / 'checker.py'; path.write_text(script, encoding='utf8')
        self.studio.host_acceptance.register({'id': 'fixed-check', 'name': '登记业务标准',
            'scriptPath': str(path), 'files': ['brief.md'],
            'criteria': [{'id': 'business-1', 'description': '候选文本含业务标志GOOD'}], 'timeoutSeconds': 5})
        self.studio.set_host_acceptance(self.pid, self.studio.meta(self.pid)['version'], ['fixed-check'])

    def start(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume)
        return self.studio.threads[self.session['id']]

    def result(self):
        worker = self.studio.threads[self.session['id']]
        worker.join(15)
        self.assertFalse(worker.is_alive())
        return self.studio.load(self.pid, self.session['id'])

    def test_actual_host_receipt_reaches_reviewer_and_coordinator_before_delivery(self):
        self.client.modules = [module('brief', 'development', 3)]
        self.register()
        self.start(); saved = self.result()
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        run = saved['runs'][-1]
        self.assertEqual([row['round'] for row in run['hostAcceptanceHistory']], [0])
        evidence = run['hostAcceptanceHistory'][0]['checks'][0]
        self.assertEqual(evidence['status'], 'passed')
        self.assertTrue(evidence['receipt']['executed'])
        self.assertEqual(evidence['receipt']['exitCode'], 0)
        self.assertEqual({row['businessStage'] for row in self.client.host_contexts}, {'quality', 'integration'})
        for shared in self.client.host_contexts:
            self.assertTrue(shared['hostAcceptance'][0]['passed'])
            self.assertEqual(shared['hostAcceptance'][0]['receiptRef'], evidence['receipt']['receiptRef'])
        self.assertTrue(run['qualityHistory'][0]['passed'])
        self.assertEqual([check['id'] for check in run['taskContracts']['brief']['hostAcceptanceChecks']], ['fixed-check'])
        self.assertEqual([a['path'] for a in run['artifacts']], ['brief.md'])
        self.assertFalse((self.store.current(self.pid)[1] / evidence['evidenceFile']['path']).exists())

    def test_failed_assertion_repairs_candidate_and_checks_new_hash_before_acceptance(self):
        self.client.good_first = False
        self.register()
        self.start(); saved = self.result()
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        run = saved['runs'][-1]
        histories = run['hostAcceptanceHistory']
        self.assertEqual([row['checks'][0]['status'] for row in histories], ['failed', 'passed'])
        self.assertNotEqual(histories[0]['artifactFingerprint'], histories[1]['artifactFingerprint'])
        self.assertEqual([row['passed'] for row in run['qualityHistory']], [False, True])
        self.assertEqual(len([call for call in self.client.calls if call['key'][0] == 'build']), 2)
        self.assertIn('GOOD', (self.store.current(self.pid)[1] / 'brief.md').read_text())

    def test_model_acceptance_cannot_override_failed_host_check_or_repair_limit(self):
        self.client.good_first = self.client.good_repair = self.client.obey_host = False
        self.register()
        self.start(); saved = self.result()
        self.assertEqual(saved['status'], 'failed')
        run = saved['runs'][-1]
        self.assertEqual(len(run['qualityHistory']), 3)
        self.assertTrue(all(not row['passed'] for row in run['qualityHistory']))
        self.assertTrue(run['final']['coordinatorAccepted'])
        self.assertFalse(run['final']['accepted'])
        self.assertNotIn('mergedRevision', run)
        self.assertEqual(len([call for call in self.client.calls if call['key'][0] == 'build']), 3)
        repairs = [call['memory']['sharedEvidence']['repair'] for call in self.client.calls
                   if call['key'][0] == 'build' and call['memory']['sharedEvidence'].get('repair')]
        self.assertEqual(len(repairs), 2)
        for repair in repairs:
            check = next(check for check in repair['feedback']['checks'] if check.get('type') == 'host_acceptance')
            self.assertTrue(check['executed'])
            self.assertIn('business-1', check['detail'])
            self.assertIn('Candidate must contain GOOD', check['detail'])
            self.assertEqual(check['criteria'][0]['criterionId'], 'business-1')
            self.assertEqual(check['criteria'][0]['status'], 'failed')
            self.assertIn('Candidate must contain GOOD', check['criteria'][0]['detail'])

    def test_runner_error_needs_input_and_resume_does_not_repeat_calls(self):
        self.client.obey_host = False
        self.register('print("not an acceptance report")\n')
        self.start(); failed = self.result()
        self.assertEqual(failed['status'], 'failed')
        record = failed['runs'][-1]['qualityHistory'][0]
        self.assertEqual(record['repairBlocked'][0]['action'], 'needs_input')
        self.assertEqual(failed['runs'][-1]['hostAcceptanceHistory'][0]['checks'][0]['status'], 'error')
        count = len(self.client.calls)
        with patch('host_acceptance._execute', side_effect=AssertionError('must reuse error receipt')):
            self.start(resume=True); resumed = self.result()
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(len(self.client.calls), count)
        self.assertEqual(len(resumed['runs'][-1]['qualityHistory']), 1)

    def test_tampered_raw_output_after_model_review_blocks_commit_without_rerun(self):
        self.register()
        def tamper():
            paths = list((self.studio.project_dir(self.pid) / self.session['id']).glob('host-acceptance/*/round-0/fixed-check/stdout.log'))
            self.assertEqual(len(paths), 1)
            paths[0].write_text('changed after review', encoding='utf8')
        self.client.before_final = tamper
        self.start(); failed = self.result()
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('只读核验', failed['error'])
        self.assertNotIn('mergedRevision', failed['runs'][-1])
        count = len(self.client.calls)
        with patch('host_acceptance._execute', side_effect=AssertionError('commit recovery cannot run checker')):
            self.start(resume=True); resumed = self.result()
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(len(self.client.calls), count)

    def test_cancelled_host_check_and_restart_preserve_unknown_without_rerun(self):
        self.client.obey_host = False
        self.register('import time\ntime.sleep(30)\n')
        self.start()
        receipt_path = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            paths = list((self.studio.project_dir(self.pid) / self.session['id']).glob('host-acceptance/*/round-0/fixed-check/receipt.json'))
            if paths:
                receipt_path = paths[0]
                break
            time.sleep(.01)
        self.assertIsNotNone(receipt_path)
        self.studio.stop(self.pid, self.session['id'])
        stopped = self.result()
        self.assertEqual(stopped['status'], 'stopped', stopped.get('error'))
        raw = receipt_path.read_bytes()
        self.assertEqual(json.loads(raw)['status'], 'unknown', raw.decode())
        # Simulate a fresh host reading persisted session/attempt and runner state.
        self.studio = Studio(self.store, self.client)
        with patch('host_acceptance._execute', side_effect=AssertionError('unknown work must not repeat')):
            self.start(resume=True); resumed = self.result()
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(resumed['runs'][-1]['qualityHistory'][0]['repairBlocked'][0]['action'], 'needs_input')
        self.assertEqual(receipt_path.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
