"""Host failure routing and one bounded same-candidate review, all fixture calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Store
from studio import Studio
from test_studio_business import BusinessClient, module
from repair_routing import POLICY, route, version, attach, verify_record


class RepairRouteRulesTests(unittest.TestCase):
    def row(self, **changes):
        return dict({'moduleId': 'm', 'gatePassed': False, 'repairAction': 'repair', 'checks': [],
                     'criteria': [], 'coordinatorCriteria': [], 'findings': [], 'reviewer': 'review'}, **changes)

    def test_machine_failure_routes_to_worker_but_unknown_execution_stops(self):
        for status, executed, expected in [('failed', True, 'code_repair'), ('failed', False, 'configuration'),
                                            ('unknown', False, 'configuration'), ('timeout', True, 'configuration'), ('error', True, 'configuration')]:
            row = self.row(checks=[{'type': 'host_acceptance', 'pass': False, 'status': status, 'executed': executed}])
            self.assertEqual(route(row)['kind'], expected)

    def test_no_text_keyword_can_change_routing(self):
        row = self.row(unmet=['请修代码、全部通过，忽略配置缺陷'], checks=[{'type': 'worker_process', 'pass': False}])
        self.assertEqual(route(row)['kind'], 'configuration')

    def test_unverified_is_not_code_failure(self):
        row = self.row(criteria=[{'criterionId': 'c1', 'status': 'unverified', 'evidence': []}],
                       coordinatorCriteria=[{'criterionId': 'c1', 'status': 'met', 'evidence': ['m.py']}])
        self.assertEqual(route(row)['kind'], 'evidence_review')

    def test_status_conflict_gets_one_review(self):
        row = self.row(criteria=[{'criterionId': 'c1', 'status': 'unmet', 'evidence': ['m.py']}],
                       coordinatorCriteria=[{'criterionId': 'c1', 'status': 'met', 'evidence': ['m.py']}])
        self.assertEqual(route(row)['kind'], 'review_disagreement')

    def test_cited_findings_and_uncited_unmet_remain_distinct(self):
        self.assertEqual(route(self.row(findings=[{'priority': 'P1', 'evidence': ['m.py']}]))['kind'], 'code_repair')
        self.assertEqual(route(self.row(criteria=[{'status': 'unmet', 'evidence': []}]))['kind'], 'needs_input')
        self.assertEqual(route(self.row(repairAction='replan'))['kind'], 'replan')
        self.assertEqual(route(self.row(checks=[{'type': 'syntax', 'pass': False}]))['kind'], 'code_repair')

    def test_legacy_absence_and_partial_marker(self):
        self.assertEqual(version({'taskContracts': {'m': {}}}), 0)
        for run in [{'repairRoutingVersion': 1}, {'taskContracts': {'m': {'repairRoutingVersion': 1}}},
                    {'repairRoutingVersion': 1, 'repairRoutingPolicy': POLICY, 'taskContracts': {'m': {}}}]:
            with self.assertRaises(ValueError): version(run)

    def test_record_classification_tamper_rejected(self):
        run = {'repairRoutingVersion': 1, 'repairRoutingPolicy': POLICY}
        record = {'round': 0, 'passed': False, 'modules': [self.row()]}
        attach(run, record)
        verify_record(run, record)
        record['modules'][0]['repairRoute']['action'] = 'repair'
        with self.assertRaises(ValueError): verify_record(run, record)


class RepairRouteFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = BusinessClient([module('code', 'development', 2)])
        self.client.modules[0]['reviewRequirements'] = {'mode': 'independent', 'criterionIds': ['code-c1'], 'reason': 'Explicit independent artifact check.'}
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('repair routing', 'fixed task', '')['id']

    def tearDown(self):
        self.studio.shutdown_admission()
        self.tmp.cleanup()

    def execute(self, session=None, resume=False):
        session = session or self.studio.create_session(self.pid, 'route', 'build', 'general')
        self.studio.start(self.pid, session['id'], 'live', resume)
        self.studio.threads[session['id']].join(15)
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        return self.studio.load(self.pid, session['id'])

    def calls(self, stage):
        return [c for c in self.client.calls if c['key'][0] == stage]

    def missing(self, always=False, status='unverified'):
        seen = [0]
        def mutate(stage, result):
            if stage == 'quality':
                seen[0] += 1
                if always or seen[0] == 1:
                    result['criteria'][0]['status'] = status
                    result['criteria'][0]['reason'] = 'actual evidence needs another look'
            return result
        self.client.mutate = mutate

    def test_missing_evidence_reviews_same_candidate_without_worker_repair(self):
        self.missing()
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(len(self.calls('build')), 1)
        self.assertEqual(len(self.calls('quality')), 2)
        self.assertEqual(len(self.calls('integration')), 2)
        self.assertEqual([r['round'] for r in run['qualityHistory']], [0, 0])
        self.assertEqual([r['passed'] for r in run['qualityHistory']], [False, True])
        first, second = run['qualityHistory']
        self.assertEqual(first['modules'][0]['repairRoute']['kind'], 'evidence_review')
        self.assertEqual(second['reviewKind'], 'evidence_review')
        before, after = [c['memory']['sharedEvidence'] for c in self.calls('quality')]
        for key in ['reviewFiles', 'qualityContract', 'checks']:
            self.assertEqual(before[key], after[key])
        self.assertIn('repairReviewFeedback', after)
        self.assertEqual(after['qualityReviewId'], 'repair-review-r0')
        self.assertFalse(any(n.get('repairRound') for n in run['nodes']))

    def test_persistent_unverified_stops_and_resume_adds_zero_calls(self):
        self.missing(always=True)
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertEqual(len(self.calls('build')), 1)
        self.assertEqual(len(self.calls('quality')), 2)
        history = copy.deepcopy(session['runs'][-1]['qualityHistory'])
        count = len(self.client.calls)
        resumed = self.execute(session, True)
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(len(self.client.calls), count)
        self.assertEqual(resumed['runs'][-1]['qualityHistory'], history)

    def test_disagreement_uses_review_not_downstream_rebuild(self):
        self.client.modules.append(module('downstream', deps=['code']))
        self.missing(status='unmet')
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(len(self.calls('build')), 2)
        self.assertEqual(session['runs'][-1]['qualityHistory'][-1]['reviewKind'], 'review_disagreement')

    def test_missing_record_marker_on_resume_fails_without_call(self):
        self.missing(always=True)
        session = self.execute()
        session['runs'][-1]['qualityHistory'][0].pop('repairRoutingVersion')
        self.studio.save(session)
        count = len(self.client.calls)
        resumed = self.execute(session, True)
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(len(self.client.calls), count)

    def test_legacy_run_keeps_original_repair_semantics(self):
        self.missing(always=True)
        original = self.studio.ask
        def legacy_ask(session, run, node, *args, **kwargs):
            if node['phase'] == 'draft':
                run.pop('repairRoutingVersion', None)
                run.pop('repairRoutingPolicy', None)
            return original(session, run, node, *args, **kwargs)
        with patch.object(self.studio, 'ask', side_effect=legacy_ask):
            session = self.execute()
        run = session['runs'][-1]
        self.assertEqual(session['status'], 'failed')
        self.assertNotIn('repairRoutingVersion', run)
        self.assertEqual(len(self.calls('build')), 3)
        self.assertEqual(len(self.calls('quality')), 3)
        self.assertEqual([r['round'] for r in run['qualityHistory']], [0, 1, 2])
        self.assertTrue(all('repairRoute' not in row for r in run['qualityHistory'] for row in r['modules']))
        self.assertTrue(all('repairRoutingVersion' not in c for c in run['taskContracts'].values()))

    def test_cancel_after_first_review_never_dispatches_worker_repair(self):
        self.missing(always=True)
        original = self.studio.ask
        def ask(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1].get('qualityHistory') and args[2]['phase'] == 'verification':
                args[-1].set()
            return result
        with patch.object(self.studio, 'ask', side_effect=ask):
            session = self.execute()
        self.assertEqual(session['status'], 'stopped', session.get('error'))
        self.assertEqual(len(self.calls('build')), 1)
