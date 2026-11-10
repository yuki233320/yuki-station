"""Deterministic capacity/fairness barriers; no models, processes or network."""
import threading
import time
import unittest
from unittest.mock import patch

from execution_capacity import CapacityClosed, ExecutionCapacity
from live import Cancelled


class ExecutionCapacityTests(unittest.TestCase):
    def setUp(self):
        self.capacity = ExecutionCapacity()
        self.threads, self.releases, self.cancels = [], [], []

    def tearDown(self):
        self.capacity.close()
        for event in self.cancels: event.set()
        for event in self.releases: event.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'test left a capacity waiter alive')

    def until(self, predicate):
        deadline = time.monotonic() + 3
        pause = threading.Event()
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('condition not reached: ' + str(self.capacity.snapshot()))
            pause.wait(.005)

    def job(self, pool, key, *, priority='normal', release=None, order=None, label=None, body=None):
        entered, ended, cancel = threading.Event(), threading.Event(), threading.Event()
        outcome = {}
        if release is None: release = threading.Event()
        self.releases.append(release); self.cancels.append(cancel)
        def execute():
            try:
                with self.capacity.slot(pool, key, cancel, priority=priority) as lease:
                    outcome['lease'] = lease
                    if order is not None: order.append(label or key)
                    entered.set()
                    if body is not None: body()
                    if not release.wait(3): raise AssertionError('test did not release active job')
            except BaseException as error:
                outcome['error'] = error
            finally:
                ended.set()
        thread = threading.Thread(target=execute, daemon=True)
        self.threads.append(thread); thread.start()
        return {'entered': entered, 'ended': ended, 'cancel': cancel, 'release': release,
                'outcome': outcome, 'thread': thread}

    def waiting(self, pool, count):
        self.until(lambda: self.capacity.snapshot()['pools'][pool]['waiting'] == count)

    def test_default_reserves_one_control_and_two_worker_slots(self):
        snapshot = self.capacity.snapshot()
        self.assertEqual(snapshot['totalLimit'], 3)
        self.assertEqual(snapshot['pools']['control']['limit'], 1)
        self.assertEqual(snapshot['pools']['worker']['limit'], 2)
        self.assertEqual(snapshot['totalActive'], 0)
        snapshot['pools']['worker']['activeByKey']['invented'] = 100
        self.assertEqual(self.capacity.snapshot()['pools']['worker']['activeByKey'], {})

    def test_two_long_workers_do_not_block_new_planning_in_reserved_control(self):
        first = self.job('worker', 'run-A'); second = self.job('worker', 'run-A')
        self.assertTrue(first['entered'].wait(1)); self.assertTrue(second['entered'].wait(1))
        pending = self.job('worker', 'run-A'); self.waiting('worker', 1)
        planning = self.job('control', 'new-session')
        self.assertTrue(planning['entered'].wait(1), 'workers consumed the reserved planning slot')
        snapshot = self.capacity.snapshot()
        self.assertEqual(snapshot['totalActive'], 3)
        self.assertEqual(snapshot['pools']['worker']['activeByKey'], {'run-A': 2})
        self.assertFalse(pending['entered'].is_set())
        planning['release'].set(); self.assertTrue(planning['ended'].wait(1))
        self.assertFalse(first['ended'].is_set()); self.assertFalse(second['ended'].is_set())

    def test_worker_slots_do_not_borrow_control_even_when_control_is_idle(self):
        first = self.job('worker', 'one'); second = self.job('worker', 'two')
        self.assertTrue(first['entered'].wait(1)); self.assertTrue(second['entered'].wait(1))
        third = self.job('worker', 'three'); self.waiting('worker', 1)
        self.assertFalse(third['entered'].is_set())
        self.assertEqual(self.capacity.snapshot()['pools']['control']['active'], 0)

    def test_another_run_gets_next_worker_before_same_run_backlog(self):
        one = self.job('worker', 'run-A'); two = self.job('worker', 'run-A')
        self.assertTrue(one['entered'].wait(1)); self.assertTrue(two['entered'].wait(1))
        order = []
        backlog = self.job('worker', 'run-A', order=order); self.waiting('worker', 1)
        newcomer = self.job('worker', 'run-B', order=order); self.waiting('worker', 2)
        one['release'].set()
        self.assertTrue(newcomer['entered'].wait(1))
        self.assertFalse(backlog['entered'].is_set()); self.assertEqual(order, ['run-B'])
        newcomer['release'].set()
        self.assertTrue(backlog['entered'].wait(1)); self.assertEqual(order, ['run-B', 'run-A'])

    def test_keys_round_robin_and_each_keys_own_waiters_remain_fifo(self):
        self.capacity = ExecutionCapacity(worker=1)
        holder = self.job('worker', 'holder'); self.assertTrue(holder['entered'].wait(1))
        released = threading.Event(); released.set(); order = []; jobs = []
        for number, (key, label) in enumerate((('A', 'A1'), ('A', 'A2'), ('A', 'A3'),
                                               ('B', 'B1'), ('B', 'B2'), ('C', 'C1')), 1):
            jobs.append(self.job('worker', key, release=released, order=order, label=label))
            self.waiting('worker', number)
        holder['release'].set()
        for job in jobs: self.assertTrue(job['ended'].wait(1))
        self.assertEqual(order, ['A1', 'B1', 'C1', 'A2', 'B2', 'A3'])

    def test_probes_are_lower_priority_but_cannot_starve_under_normal_traffic(self):
        self.capacity = ExecutionCapacity(probe_interval=3)
        holder = self.job('control', 'holder'); self.assertTrue(holder['entered'].wait(1))
        released = threading.Event(); released.set(); order = []
        probe = self.job('control', 'probe:model', priority='probe', release=released, order=order)
        self.waiting('control', 1)
        normal = []
        for index in range(5):
            normal.append(self.job('control', 'run-' + str(index), release=released, order=order))
            self.waiting('control', index + 2)
        self.assertEqual(self.capacity.snapshot()['pools']['control']['probeWaiting'], 1)
        holder['release'].set()
        for job in [probe, *normal]: self.assertTrue(job['ended'].wait(1))
        self.assertEqual(order[:4], ['run-0', 'run-1', 'run-2', 'probe:model'])

    def test_waiting_cancellation_never_enters_the_executor_and_does_not_leak(self):
        holder = self.job('control', 'held'); self.assertTrue(holder['entered'].wait(1))
        waiting = self.job('control', 'cancelled'); self.waiting('control', 1)
        waiting['cancel'].set(); self.assertTrue(waiting['ended'].wait(1))
        self.assertIsInstance(waiting['outcome']['error'], Cancelled)
        self.assertFalse(waiting['entered'].is_set()); self.waiting('control', 0)
        next_job = self.job('control', 'next'); self.waiting('control', 1)
        holder['release'].set(); self.assertTrue(next_job['entered'].wait(1))

    def test_already_cancelled_request_never_reserves_a_slot(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled):
            with self.capacity.slot('control', 'never', cancel): self.fail('cancelled body executed')
        self.assertEqual(self.capacity.snapshot()['totalActive'], 0)

    def test_close_wakes_both_pools_but_does_not_forcibly_release_active_work(self):
        self.capacity = ExecutionCapacity(worker=1)
        control = self.job('control', 'active-control'); worker = self.job('worker', 'active-worker')
        self.assertTrue(control['entered'].wait(1)); self.assertTrue(worker['entered'].wait(1))
        c_waiter = self.job('control', 'queued-control'); w_waiter = self.job('worker', 'queued-worker')
        self.waiting('control', 1); self.waiting('worker', 1)
        self.capacity.close(); self.capacity.close()
        for job in (c_waiter, w_waiter):
            self.assertTrue(job['ended'].wait(1)); self.assertIsInstance(job['outcome']['error'], CapacityClosed)
            self.assertFalse(job['entered'].is_set())
        self.assertEqual(self.capacity.snapshot()['totalActive'], 2)
        with self.assertRaises(CapacityClosed):
            with self.capacity.slot('control', 'new', threading.Event()): self.fail('closed body executed')
        control['release'].set(); worker['release'].set()
        self.assertTrue(control['ended'].wait(1)); self.assertTrue(worker['ended'].wait(1))
        self.assertEqual(self.capacity.snapshot()['totalActive'], 0)

    def test_exception_release_and_nonreentrant_guard_keep_capacity_consistent(self):
        cancel = threading.Event()
        with self.assertRaisesRegex(RuntimeError, 'body failed'):
            with self.capacity.slot('worker', 'run', cancel): raise RuntimeError('body failed')
        self.assertEqual(self.capacity.snapshot()['totalActive'], 0)
        with self.capacity.slot('worker', 'run', cancel):
            with self.assertRaisesRegex(RuntimeError, '同线程'):
                with self.capacity.slot('worker', 'nested', cancel): self.fail('nested slot entered')
            self.assertEqual(self.capacity.snapshot()['pools']['worker']['active'], 1)
        self.assertEqual(self.capacity.snapshot()['totalActive'], 0)

    def test_waiting_exception_does_not_leave_a_ghost_admission(self):
        holder = self.job('control', 'held'); self.assertTrue(holder['entered'].wait(1))
        with patch.object(self.capacity._condition, 'wait', side_effect=RuntimeError('wait interrupted')):
            waiter = self.job('control', 'interrupted')
            self.assertTrue(waiter['ended'].wait(1))
        self.assertIsInstance(waiter['outcome']['error'], RuntimeError)
        self.assertFalse(waiter['entered'].is_set()); self.waiting('control', 0)
        next_job = self.job('control', 'next'); self.waiting('control', 1)
        holder['release'].set(); self.assertTrue(next_job['entered'].wait(1))

    def test_close_after_grant_before_waiter_reenters_does_not_start_the_body(self):
        lease = self.capacity.slot('control', 'holder', threading.Event())
        lease.__enter__()
        waiter = self.job('control', 'not-yet-started'); self.waiting('control', 1)
        # Keep the waiter from reentering its condition after the release has
        # reserved it a slot, then close that exact admission window.
        with self.capacity._condition:
            lease.__exit__(None, None, None)
            self.capacity.close()
        self.assertTrue(waiter['ended'].wait(1)); self.assertFalse(waiter['entered'].is_set())
        self.assertIsInstance(waiter['outcome']['error'], CapacityClosed)
        self.assertEqual(self.capacity.snapshot()['totalActive'], 0)

    def test_contention_never_exceeds_the_reserved_limits(self):
        lock = threading.Lock(); active = {'control': 0, 'worker': 0}; maximum = dict(active)
        full, release = threading.Event(), threading.Event(); jobs = []
        def body(pool):
            with lock:
                active[pool] += 1; maximum[pool] = max(maximum[pool], active[pool])
                if sum(active.values()) == 3: full.set()
            if not release.wait(2): raise AssertionError('contention test did not release jobs')
            with lock: active[pool] -= 1
        for pool in ('worker', 'control', 'worker', 'control', 'worker', 'worker', 'control'):
            jobs.append(self.job(pool, 'run-' + str(len(jobs) % 3), release=release,
                                 body=lambda pool=pool: body(pool)))
        self.releases.append(release)
        self.assertTrue(full.wait(1)); release.set()
        for job in jobs:
            self.assertTrue(job['ended'].wait(2)); self.assertNotIn('error', job['outcome'])
        self.assertEqual(maximum, {'control': 1, 'worker': 2})
        self.assertEqual(self.capacity.snapshot()['totalActive'], 0)

    def test_invalid_capacity_and_pool_parameters_fail_before_queueing(self):
        for kwargs in ({'worker': 0}, {'control': True}, {'probe_interval': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): ExecutionCapacity(**kwargs)
        for pool, key, priority in (('other', 'key', 'normal'), ('worker', '', 'normal'), ('worker', 'key', 'probe')):
            with self.subTest(pool=pool, key=key), self.assertRaises(ValueError):
                with self.capacity.slot(pool, key, threading.Event(), priority=priority): pass
        self.assertEqual(self.capacity.snapshot()['totalActive'], 0)


class AuditCapacityIntegrationTests(unittest.TestCase):
    def test_actual_audit_validation_repair_keeps_its_own_control_lease(self):
        import json
        import test_studio_audit as fixtures
        case = fixtures.AuditTests()
        case.setUp()
        observations = []
        try:
            original = case.client.call
            def observed_call(model, messages, cancel, on_delta=None):
                prompt = next(m['content'] for m in reversed(messages) if '本轮角色任务：\n' in m['content'])
                shared = json.loads(prompt.split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])['sharedEvidence']
                expected = 'worker' if 'module' in shared else 'control'
                capacity = case.studio.capacity
                with capacity._condition:
                    own = [name for name, pool in capacity._pools.items()
                           for ticket in pool.active.values() if ticket['thread'] == threading.get_ident()]
                observations.append((expected, own))
                return original(model, messages, cancel, on_delta)
            case.client.call = observed_call
            first_review = [True]
            def invalid_once(phase, result):
                if phase == 'review' and first_review[0]:
                    first_review[0] = False
                    result['scores']['architecture']['evidenceRefs'][0]['line'] = 999
                return result
            case.client.mutate = invalid_once
            result = case.run_audit()
            self.assertEqual(result['status'], 'completed', result.get('error'))
            self.assertTrue(any(len(node.get('attempts', [])) == 2 for node in result['runs'][0]['nodes']))
            self.assertTrue(observations)
            self.assertTrue(all(own == [expected] for expected, own in observations), observations)
            self.assertEqual(case.studio.capacity.snapshot()['totalActive'], 0)
        finally:
            case.studio.capacity.close()
            case.tearDown()


if __name__ == '__main__':
    unittest.main()
