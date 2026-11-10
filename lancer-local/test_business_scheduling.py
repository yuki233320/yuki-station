"""Exercise the real business workflow with deterministic model responses."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import Store
from studio import Studio
from test_studio_business import BusinessClient, module


class SchedulingClient(BusinessClient):
    def __init__(self):
        super().__init__([module('fast'), module('slow'), module('downstream', deps=['fast'])])
        self.slow_started = threading.Event()
        self.downstream_started = threading.Event()
        self.downstream_before_slow = False
        self.downstream_upstream = None

    def call(self, model, messages, cancel, on_delta=None):
        memory = json.loads(messages[-1]['content'].split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])
        shared = memory['sharedEvidence']
        if shared['businessStage'] == 'build':
            mid = shared['module']['id']
            if mid == 'fast':
                if not self.slow_started.wait(3):
                    raise AssertionError('slow sibling did not start concurrently')
            elif mid == 'slow':
                self.slow_started.set()
                self.downstream_before_slow = self.downstream_started.wait(3)
            elif mid == 'downstream':
                self.downstream_upstream = shared['upstream']
                self.downstream_started.set()
        return super().call(model, messages, cancel, on_delta)


class BusinessSchedulingTests(unittest.TestCase):
    def test_fast_downstream_runs_early_and_still_passes_contract_and_delivery_gates(self):
        with tempfile.TemporaryDirectory() as folder:
            client = SchedulingClient()
            store = Store(Path(folder), seed=False)
            studio = Studio(store, client)
            pid = studio.create_project('调度验证', '独立分支完成后立即交接', '保持真实验收和文件交接')['id']
            session = studio.create_session(pid, '分支任务', '完成两个独立模块及快速模块的下游', 'general')
            session['workflowVersion'] = 2
            studio.save(session)
            studio.start(pid, session['id'], 'live')
            worker = studio.threads[session['id']]
            worker.join(15)
            if worker.is_alive():
                studio.stop(pid, session['id'])
                worker.join(5)
            self.assertFalse(worker.is_alive())
            saved = studio.load(pid, session['id'])
            self.assertEqual(saved['status'], 'completed', saved.get('error'))
            self.assertTrue(client.downstream_before_slow,
                            'downstream waited for the unrelated slow sibling')
            self.assertEqual(set(client.downstream_upstream), {'fast'})
            run = saved['runs'][-1]
            self.assertTrue(run['qualityHistory'][-1]['passed'])
            self.assertEqual(set(run['taskContracts']), {'fast', 'slow', 'downstream'})
            self.assertEqual([call['key'][1] for call in client.calls if call['key'][0] == 'build'].count('fast'), 1)
            packet = next(row for row in run['handoffs'] if row['target']['moduleId'] == 'downstream')
            source = packet['sources'][0]
            self.assertEqual(source['moduleId'], 'fast')
            content = client.downstream_upstream['fast']['files'][0]['content'].encode()
            self.assertEqual(source['files'][0]['sha256'], hashlib.sha256(content).hexdigest())
            state, workspace = store.current(pid)
            self.assertEqual(run['mergedRevision'], state['revision'])
            self.assertEqual((workspace / 'fast.md').read_bytes(), content)
            self.assertTrue(all(item['state'] == 'accepted' for item in run['artifacts']))


if __name__ == '__main__':
    unittest.main()
