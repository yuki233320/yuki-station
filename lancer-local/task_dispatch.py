"""Bounded task admission over existing SQLite intents, not a second task store.

Only attempts explicitly submitted to this instance receive runtime handles.
The database decides eligibility and claim; this dispatcher never adopts old
queued rows after restart. Priority is uniformly normal: projects rotate, each
project remains FIFO, and an old waiting head takes precedence after 30 seconds.
"""
from datetime import datetime, timezone
import re
import threading
import time


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def timestamp(value):
    if not isinstance(value, str): return None
    value = value.replace('Z', '+00:00')
    value = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value)
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError): return None


def wait_ms(start, end):
    a, b = timestamp(start), timestamp(end)
    return max(0, round((b-a)*1000, 3)) if a is not None and b is not None else None


def _choose(rows, last_project=None, now=None, aging_seconds=30, allow_aging=True):
    """Project round robin, with persistent insertion order breaking clock ties."""
    if not rows: return None, False
    now = time.time() if now is None else now
    def order(row):
        created = timestamp(row.get('queuedAt') or row.get('createdAt'))
        return (created if created is not None else float('inf'), row.get('queueOrder', 0))
    heads = {}
    for row in sorted(rows, key=order): heads.setdefault(row['projectId'], row)
    oldest = min(heads.values(), key=order)
    began = timestamp(oldest.get('queuedAt') or oldest.get('createdAt'))
    if allow_aging and began is not None and now - began >= aging_seconds: return oldest, True
    projects = sorted(heads)
    project = next((p for p in projects if last_project is None or p > last_project), projects[0])
    return heads[project], False


def select_next(rows, last_project=None, now=None, aging_seconds=30, allow_aging=True):
    return _choose(rows, last_project, now, aging_seconds, allow_aging)[0]


class JobHandle:
    """Thread-like lifecycle handle; queued jobs own no waiting OS thread."""
    def __init__(self, attempt_id, cancel):
        self.attempt_id, self.cancel_event = attempt_id, cancel
        self._done = threading.Event()
        self.error = None
        self.ident = id(self)  # logical handle identity, not an OS worker claim
        self.name = 'queued-task-' + attempt_id

    def is_alive(self): return not self._done.is_set()
    def join(self, timeout=None): self._done.wait(timeout)
    def _finish(self, error=None):
        # Tracebacks retain callback frames and whole session graphs. Keep only
        # bounded diagnostics on the lightweight handle after terminal work.
        self.error = None if error is None else {'type':type(error).__name__, 'message':str(error)[:2000]}
        self._done.set()


class TaskDispatcher:
    def __init__(self, state, execute, fail, active_limit=4, aging_seconds=30):
        if type(active_limit) is not int or active_limit < 1: raise ValueError('active_limit must be positive')
        self.state, self.execute, self.fail = state, execute, fail
        self.active_limit, self.aging_seconds = active_limit, aging_seconds
        self._condition = threading.Condition()
        self._jobs, self._active = {}, {}
        self._thread = None
        self._closed = False
        self._last_project = None
        self._aging_turn = False

    @property
    def closed(self):
        with self._condition: return self._closed

    def submit(self, attempt_id, project_id, session_id, cancel, session, run, callback, args):
        with self._condition:
            if self._closed: raise RuntimeError('Task admission is closed')
            if attempt_id in self._jobs: raise ValueError('Attempt already registered')
            handle = JobHandle(attempt_id, cancel)
            self._jobs[attempt_id] = {'handle':handle, 'attemptId':attempt_id, 'projectId':project_id,
                'sessionId':session_id, 'cancel':cancel, 'session':session, 'run':run, 'callback':callback, 'args':args}
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._dispatch, name='yuki-task-dispatcher', daemon=True)
                try: self._thread.start()
                except BaseException as error:
                    self._jobs.pop(attempt_id); self._thread=None; handle._finish(error)
                    raise
            self._condition.notify_all()
            return handle

    def cancel(self, session_id):
        with self._condition:
            for aid, job in list(self._jobs.items()):
                if job['sessionId'] == session_id:
                    job['cancel'].set()
                    if aid not in self._active:
                        job['handle']._finish()
                        self._jobs.pop(aid, None)
            self._condition.notify_all()

    def close(self):
        """No callbacks while holding the condition; caller settles queued DB rows."""
        with self._condition:
            self._closed = True
            jobs = [job for job in self._jobs.values() if job['handle'].is_alive()]
            for job in jobs: job['cancel'].set()
            self._condition.notify_all()
            return [(job['attemptId'],job['projectId'],job['sessionId']) for job in jobs]

    def join(self, timeout=None):
        with self._condition: thread=self._thread; handles=[job['handle'] for job in self._jobs.values()]
        for handle in handles: handle.join(timeout)
        if thread is not None and thread is not threading.current_thread(): thread.join(timeout)

    def _run(self, aid, job):
        error = None
        try: self.execute(aid, job)
        except BaseException as caught:
            error = caught
            try: self.fail(aid, caught)
            except BaseException: pass  # durable intent remains recoverable, never auto-replayed
        finally:
            with self._condition:
                self._active.pop(aid, None)
                self._jobs.pop(aid, None)
                job['handle']._finish(error)
                self._condition.notify_all()

    def _dispatch(self):
        while True:
            with self._condition:
                pending = {aid for aid, job in self._jobs.items() if job['handle'].is_alive() and aid not in self._active}
                if self._closed or not pending:
                    if not self._active:
                        self._thread = None
                        return
                    self._condition.wait(.05); continue
                if len(self._active) >= self.active_limit:
                    self._condition.wait(.05); continue
                last_project = self._last_project
                aging_turn = self._aging_turn
            # SQLite reads and every Studio callback happen outside our lock.
            try: rows = self.state.queue_rows(pending)
            except BaseException as error:
                for aid in pending:
                    try: self.fail(aid, error)
                    except BaseException: pass
                    with self._condition:
                        if aid in self._jobs:
                            self._jobs.pop(aid)['handle']._finish(error)
                continue
            by_id = {row['attemptId']: row for row in rows}
            with self._condition:
                for aid in pending:
                    if aid not in by_id or by_id[aid]['status'] != 'queued':
                        if aid in self._jobs: self._jobs.pop(aid)['handle']._finish()
                active_projects = {self._jobs[aid]['projectId'] for aid in self._active if aid in self._jobs}
                candidates = [row for row in rows if row['status']=='queued' and not row['cancelRequested']
                              and row['projectId'] not in active_projects
                              and row['attemptId'] in self._jobs and self._jobs[row['attemptId']]['handle'].is_alive()]
                if self._closed: continue
                selected, aged = _choose(candidates,last_project,aging_seconds=self.aging_seconds,allow_aging=aging_turn)
                if selected is None:
                    self._condition.wait(.05); continue
                aid = selected['attemptId']; job=self._jobs[aid]
                # Aging gets at most alternating admissions and never resets
                # the round-robin cursor; an old project's backlog cannot keep
                # later projects from advancing through the regular turns.
                if not aged: self._last_project = selected['projectId']
                self._aging_turn = not aging_turn
                job['admissionReason'] = 'aging' if aged else 'project_round_robin'
                worker = threading.Thread(target=self._run,args=(aid,job),name='yuki-task-'+aid,daemon=True)
                self._active[aid] = worker
            try: worker.start()
            except BaseException as error:
                try: self.fail(aid,error)
                finally:
                    with self._condition:
                        self._active.pop(aid,None);self._jobs.pop(aid,None);job['handle']._finish(error);self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            ids={aid for aid,job in self._jobs.items() if job['handle'].is_alive()}
            active=len(self._active); closed=self._closed; last=self._last_project; aging=self._aging_turn
            active_ids=set(self._active)
            active_projects={self._jobs[aid]['projectId'] for aid in active_ids if aid in self._jobs}
        rows=self.state.queue_rows(ids) if ids else []
        pending=[row for row in rows if row['status']=='queued' and not row['cancelRequested']]
        positions={}; remaining=[row for row in pending if row['attemptId'] not in active_ids and row['projectId'] not in active_projects]
        while remaining:
            selected,aged=_choose(remaining,last,aging_seconds=self.aging_seconds,allow_aging=aging)
            positions[selected['attemptId']]=len(positions)+1
            if not aged:last=selected['projectId']
            aging=not aging;remaining.remove(selected)
        fields=('projectId','sessionId','runId','attemptId','status','queuedAt','admittedAt','queueWaitMs','kind')
        return {'activeLimit':self.active_limit,'active':active,'queued':len(pending),'closed':closed,
                'activePerProjectLimit':1,
                'policy':'project_round_robin_fifo_with_bounded_aging','agingSeconds':self.aging_seconds,
                'jobs':[{**{key:row.get(key) for key in fields},'position':positions.get(row['attemptId']),
                         'waitingFor':('admission' if row['attemptId'] in active_ids else
                                       'project' if row['projectId'] in active_projects else 'capacity')
                                      if row['status']=='queued' else None} for row in rows]}
