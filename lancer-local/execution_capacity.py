"""Reserved control/worker capacity with cancellable per-run round-robin queues.

These are concurrent execution slots, not a fee or token budget. Pools do not
borrow each other's reserved slots. Probe traffic uses control capacity at a
lower, bounded priority: while probes wait, at most probe_interval normal
admissions precede the next probe. Running work is never preempted.
"""
from collections import Counter, deque
from contextlib import contextmanager
import threading

from live import Cancelled


class CapacityClosed(Cancelled):
    """No new execution may begin after the capacity owner closes."""


class _Pool:
    def __init__(self, limit):
        self.limit = limit
        self.active = {}
        self.queues = {'normal': {}, 'probe': {}}
        self.rings = {'normal': deque(), 'probe': deque()}
        self.last_key = {'normal': None, 'probe': None}
        self.normal_since_probe = 0


class ExecutionCapacity:
    def __init__(self, control=1, worker=2, probe_interval=4):
        if any(type(value) is not int or value < 1 for value in (control, worker, probe_interval)):
            raise ValueError('执行池容量和probe轮转间隔须为正整数')
        self._condition = threading.Condition(threading.RLock())
        self._pools = {'control': _Pool(control), 'worker': _Pool(worker)}
        self._probe_interval = probe_interval
        self._closed = False
        self._next_id = 0

    @contextmanager
    def slot(self, pool, key, cancel, *, priority='normal'):
        """Wait without holding any Studio/Store lock, then release on all exits.

        All DAG nodes for one run should use the same key. A probe uses
        slot('control', 'probe:<model>', cancel, priority='probe'). Acquisition
        is deliberately non-reentrant; call_model must not nest inside an
        already-pooled Studio.ask. Closing wakes waiters, but the owner must
        separately cancel/join active work through its existing cancel events.
        """
        ticket = self._acquire(pool, key, cancel, priority)
        try:
            yield {'pool': pool, 'key': key, 'priority': priority, 'ticket': ticket['id']}
        finally:
            with self._condition:
                state = self._pools[pool]
                state.active.pop(ticket['id'])
                ticket['state'] = 'released'
                self._dispatch(state)
                self._condition.notify_all()

    def _acquire(self, name, key, cancel, priority):
        if name not in self._pools or not isinstance(key, str) or not key:
            raise ValueError('执行池须为control/worker，任务key须为非空字符串')
        if priority not in ('normal', 'probe') or (priority == 'probe' and name != 'control'):
            raise ValueError('probe仅可使用control池的低优先队列')
        if not callable(getattr(cancel, 'is_set', None)):
            raise ValueError('执行池等待必须提供取消事件')
        with self._condition:
            if self._closed:
                raise CapacityClosed('执行容量已关闭，未启动新工作')
            if cancel.is_set():
                raise Cancelled('等待执行容量前已取消')
            state = self._pools[name]
            owner = threading.get_ident()
            if any(ticket['thread'] == owner for ticket in state.active.values()):
                raise RuntimeError('同线程不能重复获取同一执行池；请只在executor外层持槽')
            self._next_id += 1
            ticket = {'id': self._next_id, 'key': key, 'priority': priority,
                      'cancel': cancel, 'thread': owner, 'state': 'waiting', 'error': None}
            queues, ring = state.queues[priority], state.rings[priority]
            if key not in queues:
                queues[key] = deque()
                ring.append(key)
            queues[key].append(ticket)
            try:
                self._dispatch(state)
                while True:
                    if ticket['error'] is not None:
                        raise ticket['error']
                    if self._closed and ticket['state'] == 'active':
                        state.active.pop(ticket['id'])
                        ticket['state'] = 'cancelled'
                        self._condition.notify_all()
                        raise CapacityClosed('执行容量已关闭，尚未开始的工作已停止')
                    if cancel.is_set():
                        if ticket['state'] == 'active':
                            state.active.pop(ticket['id'])
                        else:
                            self._remove(state, ticket)
                        ticket['state'] = 'cancelled'
                        self._dispatch(state)
                        self._condition.notify_all()
                        raise Cancelled('排队等待执行容量时已取消')
                    if ticket['state'] == 'active':
                        return ticket
                    # threading.Event does not notify a Condition when set. The
                    # bounded wait notices cancellation without a watcher thread.
                    self._condition.wait(timeout=0.05)
            except BaseException:
                # Interruption while waiting must not leave a ghost request
                # that later reserves capacity for a thread which has exited.
                if ticket['state'] == 'active':
                    state.active.pop(ticket['id'], None)
                elif ticket['state'] == 'waiting':
                    self._remove(state, ticket)
                ticket['state'] = 'cancelled'
                self._dispatch(state)
                self._condition.notify_all()
                raise

    @staticmethod
    def _remove(state, ticket):
        priority, key = ticket['priority'], ticket['key']
        queue = state.queues[priority].get(key)
        if queue is None:
            return
        queue.remove(ticket)
        if not queue:
            del state.queues[priority][key]
            state.rings[priority].remove(key)

    def _prune_cancelled(self, state):
        for priority in ('normal', 'probe'):
            for key, queue in list(state.queues[priority].items()):
                retained = deque()
                for ticket in queue:
                    if ticket['cancel'].is_set():
                        ticket.update(state='cancelled', error=Cancelled('排队等待执行容量时已取消'))
                    else:
                        retained.append(ticket)
                if retained:
                    state.queues[priority][key] = retained
                else:
                    del state.queues[priority][key]
                    state.rings[priority].remove(key)

    def _dispatch(self, state):
        if self._closed:
            return
        self._prune_cancelled(state)
        while len(state.active) < state.limit:
            normal, probe = bool(state.rings['normal']), bool(state.rings['probe'])
            if not normal and not probe:
                break
            priority = ('probe' if probe and (not normal or state.normal_since_probe >= self._probe_interval)
                        else 'normal')
            ring, queues = state.rings[priority], state.queues[priority]
            # A burst from the last-served run must not jump ahead of another
            # run that arrived while its earlier workers occupied all slots.
            if len(ring) > 1 and ring[0] == state.last_key[priority]:
                ring.rotate(-1)
            key = ring.popleft()
            ticket = queues[key].popleft()
            if queues[key]:
                ring.append(key)
            else:
                del queues[key]
            state.last_key[priority] = key
            if priority == 'probe':
                state.normal_since_probe = 0
            elif probe:
                state.normal_since_probe += 1
            else:
                state.normal_since_probe = 0
            ticket['state'] = 'active'
            state.active[ticket['id']] = ticket
        self._condition.notify_all()

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for state in self._pools.values():
                for priority in ('normal', 'probe'):
                    for queue in state.queues[priority].values():
                        for ticket in queue:
                            ticket.update(state='cancelled', error=CapacityClosed('执行容量已关闭，排队工作未启动'))
                    state.queues[priority].clear()
                    state.rings[priority].clear()
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            pools = {}
            for name, state in self._pools.items():
                queued = [ticket for queues in state.queues.values() for queue in queues.values() for ticket in queue]
                pools[name] = {'limit': state.limit, 'active': len(state.active), 'waiting': len(queued),
                               'activeByKey': dict(sorted(Counter(t['key'] for t in state.active.values()).items())),
                               'waitingByKey': dict(sorted(Counter(t['key'] for t in queued).items())),
                               'normalWaiting': sum(t['priority'] == 'normal' for t in queued),
                               'probeWaiting': sum(t['priority'] == 'probe' for t in queued)}
            return {'closed': self._closed, 'totalLimit': sum(state.limit for state in self._pools.values()),
                    'totalActive': sum(row['active'] for row in pools.values()), 'pools': pools}
