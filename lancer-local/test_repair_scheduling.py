"""Completion-driven repair through the real workflow with local model fixtures."""
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import Store
from live import Cancelled
from studio import Studio
from test_studio_business import BusinessClient, module


class RepairSchedulingClient(BusinessClient):
    def __init__(self):
        super().__init__([module('fast'), module('slow'), module('stable'),
                          module('child', deps=['fast', 'stable']),
                          module('leaf', deps=['child']), module('unrelated')])
        self.requests = []
        self.repair_hook = None

    def call(self, model, messages, cancel, on_delta=None):
        memory = json.loads(messages[-1]['content'].split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])
        shared = memory['sharedEvidence']
        if shared['businessStage'] == 'build':
            with self.lock:
                self.requests.append(copy.deepcopy(shared))
            if shared.get('repair') and self.repair_hook:
                self.repair_hook(shared, cancel)
        result, receipt = super().call(model, messages, cancel, on_delta)
        if shared['businessStage'] == 'integration' and shared.get('qualityRound') == 0:
            result['accepted'] = False
            for row in result['moduleAcceptance']:
                if row['moduleId'] in {'fast', 'slow'}:
                    row.update(accepted=False, unmet=['补齐本模块说明'], repairAction='repair')
                    for criterion in row['criteria']:
                        criterion['status'] = 'unmet'
        if shared['businessStage'] == 'build' and shared.get('repair'):
            result['files'][0]['content'] += '\n修复第 %d 轮。' % shared['repair']['round']
        return result, receipt

    def builds(self, mid, repair=False):
        return [row for row in self.requests
                if row['module']['id'] == mid and bool(row.get('repair')) == repair]


class RepairSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = RepairSchedulingClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('返工调度', '保留证据并按依赖修复', '固定标准不可降低')['id']
        self.session = self.studio.create_session(self.pid, '返工任务', '完成并验收本轮文件', 'general')
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for cancel in self.studio.cancels.values():
            cancel.set()
        for thread in self.studio.threads.values():
            thread.join(10)
        self.tmp.cleanup()

    def start(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume)
        return self.studio.threads[self.session['id']]

    def result(self):
        worker = self.studio.threads[self.session['id']]
        worker.join(10)
        self.assertFalse(worker.is_alive())
        return self.studio.load(self.pid, self.session['id'])

    def node(self, saved, mid):
        return next(node for node in saved['runs'][-1]['nodes'] if node['id'] == 'repair-1-' + mid)

    def test_fast_repair_releases_descendants_and_keeps_unaffected_upstream(self):
        slow_started, child_started = threading.Event(), threading.Event()
        early_child = []
        def hook(shared, cancel):
            mid = shared['module']['id']
            if mid == 'fast':
                self.assertTrue(slow_started.wait(3))
            elif mid == 'slow':
                slow_started.set()
                early_child.append(child_started.wait(3))
            elif mid == 'child':
                self.assertEqual(set(shared['upstream']), {'fast', 'stable'})
                self.assertIn('修复第 1 轮', shared['upstream']['fast']['files'][0]['content'])
                self.assertNotIn('修复第', shared['upstream']['stable']['files'][0]['content'])
                child_started.set()
        self.client.repair_hook = hook
        self.start()
        saved = self.result()
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        self.assertEqual(early_child, [True], 'fast repair descendants waited for slow sibling')
        for mid in ('fast', 'slow', 'stable', 'child', 'leaf', 'unrelated'):
            self.assertEqual(len(self.client.builds(mid)), 1)
            self.assertEqual(len(self.client.builds(mid, True)), int(mid not in {'stable', 'unrelated'}))
        run = saved['runs'][-1]
        self.assertEqual([record['passed'] for record in run['qualityHistory']], [False, True])
        self.assertEqual(run['repairLimit'], 2)
        for mid in ('fast', 'slow', 'child', 'leaf'):
            first, repair = self.client.builds(mid)[0], self.client.builds(mid, True)[0]
            self.assertEqual(first['executionContract'], repair['executionContract'])
            self.assertNotIn('修复第', repair['repair']['previousFiles'][0]['content'])
        child = self.client.builds('child', True)[0]
        sources = {row['moduleId']: row for row in child['handoff']['sources']}
        self.assertEqual(sources['fast']['nodeId'], 'repair-1-fast')
        self.assertEqual(sources['stable']['nodeId'], 'build-stable')
        self.assertEqual(child['module']['dependsOn'], ['fast', 'stable'])
        for mid, source in sources.items():
            content = child['upstream'][mid]['files'][0]['content'].encode()
            self.assertEqual(source['files'][0]['sha256'], hashlib.sha256(content).hexdigest())
        leaf = self.client.builds('leaf', True)[0]
        self.assertIn('修复第 1 轮', leaf['upstream']['child']['files'][0]['content'])
        self.assertTrue(all(row['state'] == 'accepted' for row in run['artifacts']))

    def test_failed_repair_drains_sibling_and_resume_reuses_its_saved_evidence(self):
        slow_started, failing, release = threading.Event(), threading.Event(), threading.Event()
        self.release_events.append(release)
        def hook(shared, cancel):
            mid = shared['module']['id']
            if mid == 'fast':
                self.assertTrue(slow_started.wait(3))
                failing.set()
                raise ValueError('repair fixture failed')
            if mid == 'slow':
                slow_started.set()
                self.assertTrue(release.wait(3))
        self.client.repair_hook = hook
        worker = self.start()
        self.assertTrue(failing.wait(3))
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.studio.load(self.pid, self.session['id'])['status'], 'running')
        self.assertEqual(self.client.builds('child', True), [])
        release.set()
        failed = self.result()
        self.assertEqual(failed['status'], 'failed', failed.get('error'))
        self.assertIn('repair fixture failed', failed['error'])
        retained = copy.deepcopy(self.node(failed, 'slow'))
        quality = copy.deepcopy(failed['runs'][-1]['qualityHistory'][0])
        self.assertEqual(retained['status'], 'done')
        self.assertEqual(self.client.builds('child', True), [])
        self.client.repair_hook = None
        self.start(resume=True)
        saved = self.result()
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        self.assertEqual(len(self.client.builds('fast', True)), 2)
        self.assertEqual(len(self.client.builds('slow', True)), 1)
        self.assertEqual(len(self.client.builds('child', True)), 1)
        self.assertEqual(len(self.client.builds('leaf', True)), 1)
        self.assertEqual(self.node(saved, 'slow')['receipt'], retained['receipt'])
        self.assertEqual(self.node(saved, 'slow')['result'], retained['result'])
        self.assertEqual(saved['runs'][-1]['qualityHistory'][0], quality)
        self.assertEqual([item['status'] for item in saved['runs'][-1]['executionAttempts']], ['failed', 'completed'])
        for mid in ('fast', 'slow', 'stable', 'child', 'leaf', 'unrelated'):
            self.assertEqual(len(self.client.builds(mid)), 1)

    def test_cancelled_repair_waits_for_worker_and_resume_does_not_repeat_done_parent(self):
        slow_started, child_started, release = threading.Event(), threading.Event(), threading.Event()
        self.release_events.append(release)
        def hook(shared, cancel):
            mid = shared['module']['id']
            if mid == 'fast':
                self.assertTrue(slow_started.wait(3))
            elif mid == 'slow':
                slow_started.set()
                self.assertTrue(cancel.wait(3))
                raise Cancelled()
            elif mid == 'child':
                child_started.set()
                self.assertTrue(release.wait(3))
        self.client.repair_hook = hook
        worker = self.start()
        self.assertTrue(child_started.wait(3))
        before = self.studio.load(self.pid, self.session['id'])
        retained = copy.deepcopy(self.node(before, 'fast'))
        self.assertEqual(retained['status'], 'done')
        self.studio.stop(self.pid, self.session['id'])
        self.assertTrue(worker.is_alive())
        release.set()
        stopped = self.result()
        self.assertEqual(stopped['status'], 'stopped', stopped.get('error'))
        self.assertEqual(self.client.builds('leaf', True), [])
        self.client.repair_hook = None
        self.start(resume=True)
        saved = self.result()
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        self.assertEqual(len(self.client.builds('fast', True)), 1)
        self.assertEqual(len(self.client.builds('slow', True)), 2)
        self.assertEqual(len(self.client.builds('child', True)), 2)
        self.assertEqual(len(self.client.builds('leaf', True)), 1)
        self.assertEqual(self.node(saved, 'fast')['receipt'], retained['receipt'])
        self.assertEqual(self.node(saved, 'fast')['result'], retained['result'])
        self.assertEqual([item['status'] for item in saved['runs'][-1]['executionAttempts']], ['stopped', 'completed'])
        self.assertEqual([item['round'] for item in saved['runs'][-1]['qualityHistory']], [0, 1])
        for mid in ('fast', 'slow', 'stable', 'child', 'leaf', 'unrelated'):
            self.assertEqual(len(self.client.builds(mid)), 1)


if __name__ == '__main__':
    unittest.main()
