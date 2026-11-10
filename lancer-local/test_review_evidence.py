"""Independent no-host review recovery regressions. All model calls are fixtures."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Store
from live import Cancelled
from studio import Studio
from test_studio_business import BusinessClient, module
from quality_workflow import verify_review_delivery, _review_rows_fingerprint
from review_evidence import review_version


class ReviewClient(BusinessClient):
    def __init__(self):
        task = module('code', 'development', 2)
        task['acceptance'] = ['独立代码复核确认当前交付符合既定接口']
        task['reviewRequirements'] = {'mode': 'independent', 'criterionIds': ['code-c1'],
                                      'reason': '独立验证纯API审查缓存，不依赖宿主检查器'}
        super().__init__([task])
        self.entered = threading.Event()
        self.cancel_review_once = False
        self.seen = []

    def call(self, model, messages, cancel, on_delta=None):
        memory = json.loads(messages[-1]['content'].split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])
        shared = memory['sharedEvidence']
        stage = shared['businessStage']
        self.seen.append((stage, model))
        if stage == 'quality' and self.cancel_review_once:
            self.cancel_review_once = False
            self.entered.set()
            if not cancel.wait(5):
                raise AssertionError('Test failed to cancel pending reviewer')
            raise Cancelled()
        return super().call(model, messages, cancel, on_delta)


class IndependentReviewPaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yuki-review-path-')
        self.store = Store(Path(self.temp.name), seed=False)
        self.client = ReviewClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('纯API独立审查控制流验证', '只有fixture、不联网、不付费')['id']
        self.session = self.studio.create_session(self.pid, '纯API复核', '验证冻结的独立复核要求', 'general')
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No real network allowed'))
        self.network.start()

    def tearDown(self):
        for event in self.studio.cancels.values():
            event.set()
        for worker in self.studio.threads.values():
            worker.join(10)
        self.network.stop()
        self.temp.cleanup()

    def start(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume,
                          **({} if resume else {'coordinator_model': 'opus', 'team_models': ['opus', 'sol', 'gemini']}))
        return self.studio.threads[self.session['id']]

    def result(self):
        worker = self.studio.threads[self.session['id']]
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.session = self.studio.load(self.pid, self.session['id'])
        return self.session['runs'][-1]

    def interrupted_after_review(self):
        self.client.fail_once = ('integration', None, 'opus', 0)
        self.start(); run = self.result()
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        self.assertEqual(run.get('hostAcceptanceChecks', []), [])
        review = next(node for node in run['nodes'] if node['phase'] == 'verification')
        self.assertEqual(review['status'], 'done')
        return run, review

    def test_cancelled_reviewer_resumes_without_rebuilding_or_lowering_contract(self):
        self.client.cancel_review_once = True
        self.start()
        self.assertTrue(self.client.entered.wait(5))
        self.studio.stop(self.pid, self.session['id']); first = self.result()
        self.assertEqual(self.session['status'], 'stopped', self.session.get('error'))
        contract = copy.deepcopy(first['taskContracts']['code'])
        builds = len([row for row in self.client.seen if row[0] == 'build'])
        self.studio = Studio(self.store, self.client)
        self.start(resume=True); run = self.result()
        self.assertEqual(self.session['status'], 'completed', self.session.get('error'))
        self.assertEqual(run['taskContracts']['code'], contract)
        self.assertEqual(len([row for row in self.client.seen if row[0] == 'build']), builds)
        self.assertEqual([row['status'] for row in run['executionAttempts']], ['stopped', 'completed'])
        quality = run['qualityHistory'][-1]['modules'][0]
        self.assertIsNotNone(quality.get('independentReview'))
        self.assertIsNone(quality.get('sameModelReview'))

    def test_no_host_cached_review_rejects_changed_raw_context_before_new_calls(self):
        run, review = self.interrupted_after_review()
        context = self.studio.project_dir(self.pid) / self.session['id'] / 'contexts' / (review['contextRef'] + '.json')
        context.write_text('{}', encoding='utf8')
        seen = len(self.client.seen)
        self.studio = Studio(self.store, self.client)
        self.start(resume=True); self.result()
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        self.assertEqual(len(self.client.seen), seen, 'Cache mismatch must not trigger new paid-equivalent model calls')
        self.assertNotIn('mergedRevision', self.session['runs'][-1])

    def test_no_host_cached_review_rejects_changed_in_memory_result_against_raw_response(self):
        run, review = self.interrupted_after_review()
        review['result']['summary'] = 'changed without matching the sealed raw response'
        self.studio.save(self.session)
        seen = len(self.client.seen)
        self.studio = Studio(self.store, self.client)
        self.start(resume=True); self.result()
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        self.assertEqual(len(self.client.seen), seen, 'Corrupt cached result must not silently become review authority')
        self.assertNotIn('mergedRevision', self.session['runs'][-1])

    def test_unpublished_intent_missing_run_version_cannot_bypass_frozen_review(self):
        commit = self.store.commit
        def stop_before_publish(*args, **kwargs):
            if len(args) > 2 and args[2] == 'studio_delivery':
                raise OSError('stop before first publication')
            return commit(*args, **kwargs)
        self.store.commit = stop_before_publish
        self.start(); run = self.result()
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        self.assertIn('commitIntent', run)
        self.assertEqual(run['taskContracts']['code']['reviewRequirementVersion'], 1)
        review = next(node for node in run['nodes'] if node['phase'] == 'verification')
        raw = self.studio.project_dir(self.pid) / self.session['id'] / 'responses' / (review['responseRef'] + '.json')
        raw.write_text('{}', encoding='utf8')
        run.pop('reviewRequirementVersion')
        self.studio.save(self.session)
        seen = len(self.client.seen)
        self.store.commit = commit
        self.studio = Studio(self.store, self.client)
        self.start(resume=True); self.result()
        self.assertEqual(self.session['status'], 'failed', 'Dropping run version must not downgrade a frozen version-one contract')
        self.assertNotIn('mergedRevision', self.session['runs'][-1])
        self.assertEqual(len(self.client.seen), seen)

    def test_saved_schedule_cannot_reclassify_same_model_as_independent(self):
        run, review = self.interrupted_after_review()
        author = next(node for node in run['nodes'] if node['phase'] == 'build')
        schedule = run['plan']['modules'][0]['reviewSchedule']
        schedule['mode'] = 'same_model'; schedule['reviewer'] = author['agent']
        run['plan']['modules'][0]['reviewMode'] = 'same_model'
        self.studio.save(self.session)
        seen = len(self.client.seen)
        self.studio = Studio(self.store, self.client)
        self.start(resume=True); self.result()
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        self.assertEqual(len(self.client.seen), seen)
        self.assertNotIn('mergedRevision', self.session['runs'][-1])

    def completed(self):
        self.start(); run = self.result()
        self.assertEqual(self.session['status'], 'completed', self.session.get('error'))
        files = {row['path']: (self.store.current(self.pid)[1] / row['path']).read_bytes() for row in run['artifacts']}
        verify_review_delivery(self.studio, self.session, run, files)
        return run, files

    def test_low_complexity_requires_actual_review_proof_before_delivery(self):
        run, _ = self.completed()
        contract = run['taskContracts']['code']
        row = run['qualityHistory'][-1]['modules'][0]
        self.assertEqual(contract['reviewRequirements']['mode'], 'independent')
        self.assertEqual(contract['reviewSchedule']['nodeId'], 'verify-0-code')
        self.assertEqual(row['reviewEvidence']['status'], 'verified')
        self.assertTrue(row['reviewEvidence']['responseSha256'])
        self.assertNotEqual(row['reviewSchedule']['author'], row['reviewer'])
        self.assertEqual([stage for stage, model in self.client.seen], ['plan', 'build', 'quality', 'integration'])

    def test_missing_review_node_or_contract_marker_is_not_a_legacy_fallback(self):
        run, files = self.completed()
        missing = copy.deepcopy(run)
        missing['nodes'] = [node for node in missing['nodes'] if node['phase'] != 'verification']
        with self.assertRaises(ValueError):verify_review_delivery(self.studio, self.session, missing, files)
        missing = copy.deepcopy(run)
        missing['taskContracts']['code'].pop('reviewRequirementVersion')
        with self.assertRaises(ValueError):verify_review_delivery(self.studio, self.session, missing, files)

    def test_first_publication_rejects_missing_or_changed_candidate_files(self):
        run, files = self.completed()
        with self.assertRaises(ValueError):verify_review_delivery(self.studio, self.session, run, {})
        changed = dict(files); changed[next(iter(files))] += b'changed'
        with self.assertRaises(ValueError):verify_review_delivery(self.studio, self.session, run, changed)

    def test_an_arbitrary_verification_node_cannot_replace_the_planned_node_id(self):
        run, files = self.completed()
        row = run['qualityHistory'][-1]['modules'][0]
        node = next(node for node in run['nodes'] if node['id'] == row['reviewNodeId'])
        node['id'] = 'different-review-node'
        row['reviewNodeId'] = row['reviewEvidence']['reviewNodeId'] = node['id']
        run['qualityHistory'][-1]['reviewEvidenceFingerprint'] = _review_rows_fingerprint(run['qualityHistory'][-1]['modules'])
        with self.assertRaises(ValueError):verify_review_delivery(self.studio, self.session, run, files)

    def test_version_consistency_rejects_partial_markers_and_keeps_true_legacy(self):
        self.assertEqual(review_version({'plan': {'modules': [{}]}, 'taskContracts': {}, 'qualityHistory': []}), 0)
        self.assertEqual(review_version({'reviewRequirementVersion': 1, 'plan': None}), 1)
        for run in ({'reviewRequirementVersion': True},
                    {'taskContracts': {'code': {'reviewRequirementVersion': 1}}},
                    {'qualityHistory': [{'reviewRequirementVersion': 1, 'modules': []}]},
                    {'plan': {'modules': [{'reviewRequirementVersion': 1}]}},
                    {'reviewRequirementVersion': 1, 'taskContracts': {'code': {}}}):
            with self.subTest(run=run), self.assertRaises(ValueError):review_version(run)


class SupplementPreservesHostGate(unittest.TestCase):
    """Use the existing native evidence fixture; host results are read only."""
    def setUp(self):
        import test_evidence_review as fixture
        fixture.EvidenceReviewTests.setUp(self)
    def tearDown(self):
        import test_evidence_review as fixture
        fixture.EvidenceReviewTests.tearDown(self)
    def native_execute(self, *args):
        import test_evidence_review as fixture
        return fixture.EvidenceReviewTests.native_execute(self, *args)
    def failed_workflow(self):
        import test_evidence_review as fixture
        return fixture.EvidenceReviewTests.failed_workflow(self)

    def test_supplementing_native_receipts_never_drops_an_existing_failed_host_gate(self):
        from evidence_review import _assess_outputs
        script = Path(self.tmp.name) / 'host-check.py'
        script.write_text("import json\nprint(json.dumps({'accepted':False,'criteria':[{'id':'H1','status':'failed','detail':'Original host failure'}]}))\n")
        entry = self.studio.host_acceptance.register({'name': 'Existing host gate', 'scriptPath': str(script),
            'files': ['verification.md'], 'criteria': [{'id': 'H1', 'description': 'Must remain a hard failure'}]})
        self.studio.set_host_acceptance(self.pid, self.studio.meta(self.pid)['version'], [entry['id']])
        session = self.failed_workflow(); run = session['runs'][-1]
        previous = run['qualityHistory'][-1]
        complete = {'verification': copy.deepcopy(next(node['result'] for node in run['nodes']
                    if node['phase'] == 'build' and node.get('moduleId') == 'verification'))}
        supplement = {'modules': [{'moduleId': 'verification', 'missing': []}]}
        before = copy.deepcopy(run['hostAcceptanceHistory'])
        with patch('host_acceptance.run_check', side_effect=AssertionError('supplement must not execute host checks')):
            rows = _assess_outputs(self.studio, session, run, complete, previous, supplement)
        host = [check for check in rows[0]['checks'] if check.get('type') == 'host_acceptance']
        self.assertEqual(len(host), 1); self.assertFalse(host[0]['pass'])
        self.assertFalse(rows[0]['gatePassed'])
        self.assertEqual(run['hostAcceptanceHistory'], before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
