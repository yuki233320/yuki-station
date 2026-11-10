"""Scheduling invariants with synchronized workers; no providers or tools."""
import threading
import unittest

from dag_scheduler import execute_dag
from live import Cancelled


def task(mid, deps=(), priority='P1', complexity=1):
    return {'id': mid, 'dependsOn': list(deps), 'priority': priority, 'complexity': complexity}


class DAGSchedulerTests(unittest.TestCase):
    def background(self, function):
        result, ended = {}, threading.Event()
        def run():
            try:
                result['value'] = function()
            except BaseException as exc:
                result['error'] = exc
            finally:
                ended.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result, ended

    def test_idle_slot_starts_downstream_before_slow_sibling_finishes(self):
        slow_started, child_started = threading.Event(), threading.Event()
        observed = {}
        def execute(module, upstream):
            mid = module['id']
            if mid == 'fast':
                self.assertTrue(slow_started.wait(3))
            elif mid == 'slow':
                slow_started.set()
                observed['early_child'] = child_started.wait(3)
            else:
                self.assertEqual(upstream, {'fast': 'fast-result'})
                child_started.set()
            return mid + '-result'
        result = execute_dag([task('fast'), task('slow'), task('child', ['fast'])],
                             execute, threading.Event(), max_workers=2)
        self.assertTrue(observed['early_child'])
        self.assertEqual(set(result), {'fast', 'slow', 'child'})

    def test_priority_complexity_and_id_order_at_admission(self):
        order = []
        execute_dag([task('last', priority='P2', complexity=5), task('b', complexity=3),
                     task('urgent', priority='P0'), task('a', complexity=3), task('small')],
                    lambda module, upstream: order.append(module['id']), threading.Event(), max_workers=1)
        self.assertEqual(order, ['urgent', 'a', 'b', 'small', 'last'])

    def test_at_most_three_workers_are_admitted(self):
        release, full = threading.Event(), threading.Event()
        lock = threading.Lock()
        active, maximum, started = 0, 0, []
        def execute(module, upstream):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                started.append(module['id'])
                if active == 3:
                    full.set()
            try:
                if not release.wait(3):
                    raise TimeoutError('test worker not released')
            finally:
                with lock:
                    active -= 1
            return module['id']
        thread, result, ended = self.background(lambda: execute_dag(
            [task(str(i)) for i in range(7)], execute, threading.Event()))
        try:
            self.assertTrue(full.wait(3))
            with lock:
                self.assertEqual(len(started), 3)
        finally:
            release.set()
            thread.join(5)
        self.assertTrue(ended.is_set())
        self.assertNotIn('error', result)
        self.assertEqual(maximum, 3)
        self.assertEqual(len(result['value']), 7)

    def test_dependency_snapshots_do_not_share_nested_results(self):
        modified = threading.Event()
        original = {'files': [{'content': 'original'}]}
        def execute(module, upstream):
            if module['id'] == 'root':
                return original
            if module['id'] == 'mutator':
                upstream['root']['files'][0]['content'] = 'changed'
                modified.set()
            else:
                self.assertTrue(modified.wait(3))
                self.assertEqual(upstream['root']['files'][0]['content'], 'original')
            return module['id']
        result = execute_dag([task('root'), task('mutator', ['root']), task('observer', ['root'])],
                             execute, threading.Event())
        self.assertEqual(result['root'], {'files': [{'content': 'original'}]})
        self.assertEqual(original, result['root'])

    def test_failure_stops_admission_and_waits_for_running_evidence(self):
        sibling_started, failing, release = threading.Event(), threading.Event(), threading.Event()
        evidence, started = [], []
        def execute(module, upstream):
            started.append(module['id'])
            if module['id'] == 'bad':
                self.assertTrue(sibling_started.wait(3))
                failing.set()
                raise ValueError('actual worker failure')
            sibling_started.set()
            if not release.wait(3):
                raise TimeoutError('test worker not released')
            evidence.append(module['id'])
            return module['id']
        thread, result, ended = self.background(lambda: execute_dag(
            [task('bad', priority='P0'), task('sibling'), task('never', priority='P2')],
            execute, threading.Event(), max_workers=2))
        try:
            self.assertTrue(failing.wait(3))
            self.assertFalse(ended.wait(.1))
            self.assertNotIn('never', started)
        finally:
            release.set()
            thread.join(5)
        self.assertTrue(ended.is_set())
        self.assertIsInstance(result.get('error'), ValueError)
        self.assertEqual(str(result['error']), 'actual worker failure')
        self.assertEqual(evidence, ['sibling'])
        self.assertNotIn('never', started)

    def test_cancel_stops_admission_and_drains_existing_worker(self):
        started, release, cancel = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def execute(module, upstream):
            calls.append(module['id'])
            started.set()
            if not release.wait(3):
                raise TimeoutError('test worker not released')
            return module['id']
        thread, result, ended = self.background(lambda: execute_dag(
            [task('first'), task('never')], execute, cancel, max_workers=1))
        try:
            self.assertTrue(started.wait(3))
            cancel.set()
            self.assertFalse(ended.wait(.1))
        finally:
            release.set()
            thread.join(5)
        self.assertTrue(ended.is_set())
        self.assertIsInstance(result.get('error'), Cancelled)
        self.assertEqual(calls, ['first'])

    def test_invalid_graph_is_rejected_before_worker_side_effects(self):
        for modules in ([task('a', ['missing'])], [task('a', ['b']), task('b', ['a'])],
                        [task('a'), task('a')]):
            with self.subTest(modules=modules):
                calls = []
                with self.assertRaises(ValueError):
                    execute_dag(modules, lambda module, upstream: calls.append(module['id']), threading.Event())
                self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
