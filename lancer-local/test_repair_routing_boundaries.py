"""SQLite recovery, mixed failures and unchanged legacy semantics (no network)."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import Store
from studio import Studio
from test_studio_business import BusinessClient, module


class BoundaryClient(BusinessClient):
    def __init__(self, mixed=False):
        ids = ['broken', 'uncertain'] if mixed else ['uncertain']
        modules = [module(mid, 'development', 2) for mid in ids]
        for item in modules:
            item['reviewRequirements'] = {'mode': 'independent', 'criterionIds': [item['id'] + '-c1'],
                                          'reason': 'Fixed independent review'}
        super().__init__(modules)

    def call(self, model, messages, cancel, on_delta=None):
        memory = json.loads(messages[-1]['content'].split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])
        shared = memory['sharedEvidence']
        result, receipt = super().call(model, messages, cancel, on_delta)
        if shared['businessStage'] == 'quality':
            if shared['module']['id'] == 'uncertain':
                result['criteria'][0].update(status='unverified', reason='实际证据不足', evidence=[])
            else:
                result['findings'] = [{'priority': 'P1', 'claim': '具体产物缺陷', 'proposal': '修复原有范围内产物',
                                      'evidence': copy.deepcopy(result['criteria'][0]['evidence'])}]
        return result, receipt


class RepairRoutingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yuki-repair-boundaries-')
        self.root = Path(self.tmp.name)
        self.store = Store(self.root, seed=False)
        self.client = BoundaryClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('isolated boundary', 'fixture only')['id']
        self.session = self.studio.create_session(self.pid, 'frozen review', 'original task', 'general')
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network'))
        self.network.start()

    def tearDown(self):
        self.studio.shutdown_admission()
        self.network.stop()
        self.tmp.cleanup()

    def execute(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume,
                          **({} if resume else {'coordinator_model': 'opus', 'team_models': ['opus', 'sol', 'gemini']}))
        thread = self.studio.threads[self.session['id']]
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.session = self.studio.load(self.pid, self.session['id'])
        return self.session['runs'][-1]

    def test_persistent_missing_evidence_reopens_from_sqlite_without_new_calls(self):
        run = self.execute()
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        history = copy.deepcopy(run['qualityHistory'])
        self.assertEqual([h['round'] for h in history], [0, 0])
        before = len(self.client.calls)
        self.studio.shutdown_admission()
        self.store = Store(self.root, seed=False)
        self.studio = Studio(self.store, self.client)
        self.session = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(self.session['runs'][-1]['qualityHistory'], history)
        resumed = self.execute(resume=True)
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        self.assertEqual(resumed['qualityHistory'], history)
        self.assertEqual(len(self.client.calls), before)
        self.assertFalse(any(node.get('repairRound') for node in resumed['nodes']))

    def test_mixed_code_and_persistent_missing_evidence_stops_all_worker_repairs(self):
        self.studio.shutdown_admission()
        self.client = BoundaryClient(mixed=True)
        self.studio = Studio(self.store, self.client)
        run = self.execute()
        self.assertEqual(self.session['status'], 'failed', self.session.get('error'))
        first = {row['moduleId']: row for row in run['qualityHistory'][0]['modules']}
        self.assertEqual(first['broken']['repairRoute']['action'], 'repair')
        self.assertEqual(first['uncertain']['repairRoute']['action'], 'review')
        last = {row['moduleId']: row for row in run['qualityHistory'][-1]['modules']}
        self.assertEqual(last['uncertain']['repairRoute']['action'], 'stop')
        self.assertEqual(len([c for c in self.client.calls if c['key'][0] == 'build']), 2)
        self.assertFalse(any(node.get('repairRound') for node in run['nodes']))
        self.assertNotIn('mergedRevision', run)



if __name__ == '__main__':
    unittest.main()
