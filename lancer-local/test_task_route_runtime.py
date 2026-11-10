"""Direct routing skips planning calls, never the existing quality boundary."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import Store
from studio import Studio
from test_studio_business import BusinessClient


class DirectRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = BusinessClient()
        self.studio = Studio(Store(Path(self.tmp.name), seed=False), self.client)
        self.pid = self.studio.create_project('Direct task fixture', 'No paid models')['id']
        self.session = self.studio.create_session(self.pid, '明确任务', '整理已有资料，按给定标准输出说明', 'general')
        self.route = {'mode': 'direct', 'contract': {'roleId': 'writing', 'workType': 'writing',
                      'scope': ['direct-task.md'], 'acceptance': ['明确说明交付内容'],
                      'skills': {'writing': 1}, 'requiredCapabilities': ['text'], 'complexity': 2}}

    def tearDown(self):
        for cancel in self.studio.cancels.values():cancel.set()
        for worker in list(self.studio.threads.values()):
            if worker.is_alive():worker.join(10)
        self.tmp.cleanup()

    def execute(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume,
            **({} if resume else {'coordinator_model': 'opus', 'team_models': ['opus', 'sol', 'gemini'], 'task_route': self.route}))
        self.studio.threads[self.session['id']].join(10)
        return self.studio.load(self.pid, self.session['id'])

    def test_direct_skips_model_planner_and_keeps_fixed_contract_and_acceptance(self):
        result = self.execute()
        self.assertEqual(result['status'], 'completed', result.get('error'))
        run = result['runs'][-1]
        self.assertEqual([call['key'][0] for call in self.client.calls], ['build', 'integration'])
        router = next(n for n in run['nodes'] if n['id'] == 'draft')
        self.assertEqual((router['agent'], router['phase'], router['executorId']), ('host', 'routing', 'task_router'))
        self.assertNotIn('responseRef', router)
        self.assertEqual(run['taskContracts']['direct-task']['taskRoutingVersion'], 1)
        self.assertEqual(run['taskContracts']['direct-task']['criteria'][0]['text'], self.route['contract']['acceptance'][0])
        self.assertTrue(run['qualityHistory'][-1]['passed'])

    def test_direct_development_still_requires_actual_independent_review(self):
        self.route['contract'].update(roleId='backend', workType='development', skills={'coding': 1})
        result = self.execute()
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual([call['key'][0] for call in self.client.calls], ['build', 'quality', 'integration'])
        run = result['runs'][-1]
        self.assertEqual(run['plan']['modules'][0]['reviewRequirements']['mode'], 'independent')
        self.assertTrue(run['qualityHistory'][-1]['modules'][0]['reviewEvidence'])

    def test_invalid_direct_contract_does_not_fall_back_to_paid_planning(self):
        del self.route['contract']['acceptance']
        before = self.studio.load(self.pid, self.session['id'])
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.studio.load(self.pid, self.session['id']), before)
        self.assertFalse(self.client.calls)

    def test_changed_route_original_blocks_first_publication_without_more_calls(self):
        actual = self.studio.store.commit
        def stop_before_commit(*args, **kwargs):
            if len(args) > 2 and args[2] == 'studio_delivery':raise RuntimeError('fixture before commit')
            return actual(*args, **kwargs)
        with patch.object(self.studio.store, 'commit', side_effect=stop_before_commit):result = self.execute()
        self.assertEqual(result['status'], 'failed', result.get('error'))
        run = result['runs'][-1]
        proof = self.studio.project_dir(self.pid) / self.session['id'] / run['taskRoutingProof']['ref']
        proof.write_text('{}')
        before = len(self.client.calls)
        result = self.execute(resume=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.client.calls), before)
        self.assertNotIn('mergedRevision', result['runs'][-1])


if __name__ == '__main__':unittest.main()
