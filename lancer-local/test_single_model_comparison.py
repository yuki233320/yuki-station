"""The comparison-only same-model arm still uses real contracts and quality gates."""
import copy
import tempfile
import unittest
from pathlib import Path

from app import Store
from studio import Studio
from test_studio_business import BusinessClient, module


class SingleModelComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        spec = module('software', 'development', 4)
        spec['scope'] = ['software.py']
        self.client = BusinessClient([spec])
        self.client.mutate = self.code_output
        self.studio = Studio(Store(Path(self.temp.name), seed=False), self.client)
        self.pid = self.studio.create_project('单模型开发对照', '按固定接口交付', '')['id']

    def tearDown(self):
        for thread in self.studio.threads.values():
            thread.join(15)
        self.temp.cleanup()

    def code_output(self, stage, result):
        if stage == 'build':
            result['files'] = [{'path': 'software.py', 'content': 'def identity(value):\n    return value\n'}]
        return result

    def execute(self, arm='single', session=None, resume=False):
        session = session or self.studio.create_session(self.pid, '开发对照', '按接口实现开发模块', 'general')
        self.studio.start(self.pid, session['id'], resume=resume,
                          coordinator_model='opus', team_models=['opus'], comparison_arm=arm)
        self.studio.threads[session['id']].join(15)
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        return self.studio.load(self.pid, session['id'])

    def test_same_model_has_separate_contexts_and_preserves_round_limits(self):
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['comparisonArm'], 'single')
        self.assertEqual({n['agent'] for n in run['nodes']}, {'opus'})
        progress = run['moduleReviews']['software']
        self.assertEqual(progress['models'], ['opus'])
        self.assertEqual(progress['roundLimit'], 2)
        self.assertEqual(progress['completedRounds'], 2)
        self.assertEqual(len(progress['reflection']['answerNodeIds']), 1)
        self.assertEqual(run['repairLimit'], 2)
        expected_agents = ['coordinator', 'worker-software-owner-software-opus',
                           'reviewer-software-opus', 'quality-software-opus']
        self.assertTrue(all(key in session['agents'] for key in expected_agents))
        context_sets = [{turn['contextRef'] for turn in session['agents'][key]['turns']}
                        for key in expected_agents]
        self.assertEqual(sum(map(len, context_sets)), len(set.union(*context_sets)))
        reviewer = session['agents']['quality-software-opus']
        self.assertIn('同模型复核', reviewer['turns'][0]['instruction'])
        self.assertEqual(len(reviewer['turns']), 1)
        row = run['qualityHistory'][0]['modules'][0]
        self.assertTrue(row['gatePassed'])
        self.assertEqual(row['basis'], 'same_model_artifact_review')
        self.assertIsNone(row['independentReview'])
        self.assertIsNotNone(row['sameModelReview'])
        verify = next(n for n in run['nodes'] if n['phase'] == 'verification')
        self.assertIn('同模型复核', verify['title'])
        self.assertEqual((self.studio.store.current(self.pid)[1] / 'software.py').read_text(),
                         'def identity(value):\n    return value\n')

    def test_ordinary_single_model_does_not_silently_bypass_cross_model_review(self):
        self.client.modules[0]['reviewMode'] = 'same_model'
        session = self.execute(arm=None)
        self.assertEqual(session['status'], 'failed')
        self.assertIn('两个', session['error'])
        self.assertFalse(any(call['key'][0] == 'build' for call in self.client.calls))
        self.assertNotIn('comparisonArm', session['runs'][-1])

    def test_same_model_quality_failure_still_requires_repair_and_fixed_contract(self):
        attempts = []
        def first_failure(stage, result):
            self.code_output(stage, result)
            if stage == 'quality':
                attempts.append(copy.deepcopy(result))
                if len(attempts) == 1:
                    result['criteria'][0].update(status='unmet', reason='接口说明尚未满足')
                    result['findings'] = [{'priority': 'P1', 'claim': '接口说明尚未满足', 'proposal': '补齐本模块接口说明',
                                           'evidence': copy.deepcopy(result['criteria'][0]['evidence'])}]
            return result
        self.client.mutate = first_failure
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual([record['passed'] for record in run['qualityHistory']], [False, True])
        self.assertEqual(len(attempts), 2)
        builds = [call for call in self.client.calls if call['key'][0] == 'build']
        self.assertEqual(len(builds), 2)
        self.assertEqual(builds[0]['memory']['sharedEvidence']['executionContract'],
                         builds[1]['memory']['sharedEvidence']['executionContract'])

    def test_same_model_blocker_exhausts_exactly_two_repairs(self):
        def blocker(stage, result):
            self.code_output(stage, result)
            if stage == 'quality':
                result['findings'] = [{'priority': 'P1', 'claim': '未满足接口', 'proposal': '按原接口修复',
                                       'evidence': [{'path': 'software.py', 'startLine': 1, 'endLine': 1}]}]
            return result
        self.client.mutate = blocker
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('两轮', session['error'])
        run = session['runs'][-1]
        self.assertEqual([record['round'] for record in run['qualityHistory']], [0, 1, 2])
        self.assertFalse(any(record['passed'] for record in run['qualityHistory']))
        self.assertFalse((self.studio.store.current(self.pid)[1] / 'software.py').exists())

    def test_resume_retains_comparison_mode_and_finished_discussion(self):
        self.client.fail_once = ('quality', 'software', 'opus', 0)
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        finished_reviews = [copy.deepcopy(call) for call in self.client.calls if call['key'][0] == 'review']
        session = self.execute(arm=None, session=session, resume=True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['comparisonArm'], 'single')
        self.assertEqual([call for call in self.client.calls if call['key'][0] == 'review'], finished_reviews)
        self.assertEqual(run['qualityHistory'][0]['modules'][0]['basis'], 'same_model_artifact_review')


if __name__ == '__main__':
    unittest.main()
