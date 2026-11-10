"""Durable task admission and fair scheduling, no paid providers or network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import Conflict, Store, stamp
from contribution_tracking import verify_contribution
from execution_attempts import begin_attempt, finish_attempt
from live import Cancelled
from studio import Studio
from task_dispatch import JobHandle, _choose, wait_ms
from test_studio_business import BusinessClient


class FairSelectionTests(unittest.TestCase):
    def test_project_round_robin_with_bounded_aging_does_not_reset_the_cursor(self):
        now = datetime.now(timezone.utc)
        rows = [{'attemptId':'a'+str(i),'projectId':'A','queueOrder':i,
                 'queuedAt':(now-timedelta(hours=1)).isoformat()} for i in range(100)]
        rows += [{'attemptId':p,'projectId':p,'queueOrder':200+i,'queuedAt':now.isoformat()}
                 for i,p in enumerate(('B','C'))]
        last, aging, chosen = None, False, []
        for _ in range(6):
            item, aged = _choose(rows,last,now.timestamp(),30,aging)
            chosen.append(item['attemptId']);rows.remove(item)
            if not aged:last=item['projectId']
            aging=not aging
        self.assertIn('B',chosen[:4]);self.assertIn('C',chosen[:6])
        self.assertEqual([x for x in chosen if x.startswith('a')],['a0','a1','a2','a3'])

    def test_fifo_uses_persistent_order_for_equal_timestamp_and_old_offset_parsing(self):
        rows=[{'attemptId':'a','projectId':'P','queueOrder':2,'queuedAt':'2026-11-03T10:00:00+0800'},
              {'attemptId':'z','projectId':'P','queueOrder':1,'queuedAt':'2026-11-03T10:00:00+0800'}]
        self.assertEqual(_choose(rows,allow_aging=False)[0]['attemptId'],'z')
        self.assertEqual(wait_ms('2026-11-03T10:00:00+0800','2026-11-03T02:00:01+00:00'),1000)
        self.assertIsNone(wait_ms('legacy unknown','2026-11-03T02:00:01+00:00'))


class TaskAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.client=BusinessClient()
        self.studio=Studio(Store(Path(self.temp.name),seed=False),self.client)
        self.releases=[]

    def tearDown(self):
        for event in self.releases:event.set()
        self.studio.shutdown_admission()
        self.studio.task_dispatcher.join(10)
        for handle in self.studio.threads.values():handle.join(10)
        self.temp.cleanup()

    def task(self,pid=None,title='Queued fixture'):
        pid=pid or self.studio.create_project(title,'Verify durable queue')['id']
        return pid,self.studio.create_session(pid,title,'整理资料并明确交付','general')

    def start_blocked(self):
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        def wait():
            entered.set()
            if not release.wait(10):raise AssertionError('Test did not release fixture')
        self.client.before_final=wait
        pid,session=self.task()
        initial=self.studio.start(pid,session['id'])
        self.assertEqual(initial['status'],'queued')
        self.assertTrue(entered.wait(5))
        return pid,session,release

    def join(self,pid,session):
        handle=self.studio.threads[session['id']];self.assertIsNotNone(handle.ident)
        handle.join(10);self.assertFalse(handle.is_alive())
        return self.studio.load(pid,session['id'])

    def test_more_than_active_limit_are_durable_queued_without_per_job_threads(self):
        releases={}; entered=[];guard=threading.Lock()
        def fake_execute(session,run,cancel):
            gate=releases[session['id']]
            with guard:entered.append(session['id'])
            while not gate.wait(.01):
                if cancel.is_set():break
            with self.studio.lock:
                session['status']=run['status']='stopped' if cancel.is_set() else 'completed'
                run['ended']=stamp();finish_attempt(run,stamp());self.studio.save(session)
        with patch.object(self.studio,'execute',side_effect=fake_execute):
            tasks=[]
            for i in range(7):
                pid,session=self.task(title='P'+str(i));gate=threading.Event()
                releases[session['id']]=gate;self.releases.append(gate);tasks.append((pid,session))
                self.studio.start(pid,session['id'])
            deadline=time.monotonic()+5
            while len(entered)<4 and time.monotonic()<deadline:time.sleep(.01)
            snapshot=self.studio.queue_snapshot()['taskQueue']
            self.assertEqual(snapshot['active'],4);self.assertEqual(snapshot['queued'],3)
            self.assertEqual(sum(isinstance(h,JobHandle) for h in self.studio.threads.values()),7)
            actual=[t for t in threading.enumerate() if t.name.startswith('yuki-task-') and t.name!='yuki-task-dispatcher']
            self.assertLessEqual(len(actual),4)
            self.assertEqual(sum(self.studio.load(pid,s['id'])['status']=='queued' for pid,s in tasks),3)
            for event in releases.values():event.set()
            for pid,session in tasks:self.assertEqual(self.join(pid,session)['status'],'completed')
        self.assertEqual(self.studio.task_dispatcher._jobs,{})
        self.assertFalse(self.client.calls)

    def test_queued_cancel_and_mutation_guards_issue_no_model_calls(self):
        self.studio.task_dispatcher.active_limit=1
        _,_,release=self.start_blocked()
        pid,session=self.task();self.studio.start(pid,session['id'])
        saved=self.studio.load(pid,session['id']);calls=len(self.client.calls)
        attempt_id=saved['runs'][-1]['executionAttempts'][-1]['id']
        stale_job=self.studio.task_dispatcher._jobs[attempt_id]
        self.assertEqual(saved['status'],'queued')
        with self.assertRaises(ValueError):self.studio.add_message(pid,session['id'],'new task',saved['version'])
        with self.assertRaises(ValueError):self.studio.set_team(pid,session['id'],'opus',['opus'],saved['version'])
        with self.assertRaises(ValueError):self.studio.set_task_route_selection(pid,session['id'],{'mode':'team'},saved['version'])
        with self.assertRaises(ValueError):self.studio.start(pid,session['id'])
        request={k:'fixture' for k in ('runId','recordId','evidenceFingerprint','beforeObservation','afterObservation','method')}
        request.update(version=saved['version'],verdict='rejected')
        with self.assertRaisesRegex(ValueError,'排队或执行'):verify_contribution(self.studio,pid,session['id'],request)
        stopped=self.studio.stop(pid,session['id'])
        self.assertEqual(stopped['status'],'stopped')
        self.assertEqual(len(self.client.calls),calls)
        self.assertEqual(stopped['runs'][-1]['nodes'],[])
        self.assertIsNone(stopped['runs'][-1]['admittedAt'])
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        row=self.studio.state.dispatches(pid)[0]
        self.assertEqual((row['status'],row['outcome'],row['cancelRequested']),('settled','stopped',True))
        # Even a late callback with a lost in-memory cancel flag cannot revive
        # the cancelled durable intent.
        stale_job['cancel'].clear()
        self.studio._dispatch_queued(attempt_id,stale_job)
        self.assertEqual(self.studio.load(pid,session['id']),stopped)
        self.assertEqual(len(self.client.calls),calls)
        release.set()

    def test_same_project_waits_then_conflicts_before_any_new_model_call(self):
        pid,first,release=self.start_blocked()
        _,second=self.task(pid,'Same project later task')
        self.studio.start(pid,second['id'])
        queued=self.studio.load(pid,second['id'])
        self.assertEqual(queued['status'],'queued')
        queued_row=next(row for row in self.studio.queue_snapshot()['taskQueue']['jobs'] if row['sessionId']==second['id'])
        self.assertEqual(queued_row['waitingFor'],'project')
        self.assertIsNone(queued_row['position'])
        calls=len(self.client.calls)
        release.set()
        self.assertEqual(self.join(pid,first)['status'],'completed')
        result=self.join(pid,second)
        self.assertEqual(result['status'],'conflict')
        self.assertIn('项目版本已变化',result['error'])
        self.assertEqual(result['runs'][-1]['nodes'],[])
        self.assertEqual(len(self.client.calls),calls)
        self.assertEqual(result['runs'][-1]['projectRevision'],queued['runs'][-1]['projectRevision'])

    def test_queued_preflight_rechecks_current_model_availability(self):
        self.studio.task_dispatcher.active_limit=1
        _,_,release=self.start_blocked()
        pid,session=self.task();self.studio.start(pid,session['id'])
        calls=len(self.client.calls)
        self.client.available=lambda: []
        release.set()
        result=self.join(pid,session)
        self.assertEqual(result['status'],'failed')
        self.assertIn('不可派工',result['error'])
        self.assertEqual(len(self.client.calls),calls)
        self.assertIsNone(result['runs'][-1]['admittedAt'])

    def test_claim_and_admission_payload_commit_atomically(self):
        pid,session=self.task();seen=[];original=self.client.call
        def call(*args,**kwargs):
            saved=self.studio.load(pid,session['id']);row=self.studio.state.dispatches(pid)[0]
            seen.append(row)
            self.assertEqual(row['status'],'running')
            self.assertEqual(saved['status'],'running')
            self.assertIsNotNone(saved['runs'][-1]['admittedAt'])
            self.assertGreaterEqual(saved['runs'][-1]['queueWaitMs'],0)
            self.assertEqual(saved['runs'][-1]['executionAttempts'][-1]['admissionState'],'running')
            return original(*args,**kwargs)
        with patch.object(self.client,'call',side_effect=call):
            self.studio.start(pid,session['id']);result=self.join(pid,session)
        self.assertTrue(seen);self.assertEqual(result['status'],'completed')
        self.assertEqual(result['runs'][-1]['executionAttempts'][-1]['admissionState'],'completed')

    def test_shutdown_settles_waiters_before_finishing_handles_and_rejects_new_work(self):
        self.studio.task_dispatcher.active_limit=1
        _,_,release=self.start_blocked()
        pid,session=self.task();self.studio.start(pid,session['id'])
        handle=self.studio.threads[session['id']]
        self.studio.shutdown_admission()
        self.assertFalse(handle.is_alive())
        saved=self.studio.load(pid,session['id'])
        self.assertEqual(saved['status'],'stopped')
        self.assertEqual(self.studio.state.dispatches(pid)[0]['status'],'settled')
        fresh_pid,fresh=self.task()
        with self.assertRaisesRegex(ValueError,'退出'):self.studio.start(fresh_pid,fresh['id'])
        self.assertEqual(self.studio.load(fresh_pid,fresh['id'])['runs'],[])
        release.set();self.studio.task_dispatcher.join(10)
        self.assertEqual(self.studio.queue_snapshot()['taskQueue']['active'],0)

    def test_route_selection_cas_is_private_and_cleared_on_new_request(self):
        pid,session=self.task()
        route={'mode':'direct','contract':{'roleId':'writer','workType':'writing','scope':['report.md'],
                                          'acceptance':['说明完整'],'instructions':'写说明'}}
        selected=self.studio.set_task_route_selection(pid,session['id'],route,session['version'])
        self.assertEqual(selected['taskRouteSelection']['mode'],'direct')
        with self.assertRaises(Conflict):self.studio.set_task_route_selection(pid,session['id'],route,session['version'])
        self.client.credentials['opus']='private-nonstandard-key'
        bad=deepcopy(route);bad['contract']['instructions']='private-nonstandard-key'
        with self.assertRaisesRegex(ValueError,'凭据'):
            self.studio.set_task_route_selection(pid,session['id'],bad,selected['version'])
        self.assertEqual(self.studio.load(pid,session['id']),selected)
        changed=self.studio.add_message(pid,session['id'],'另一份说明',selected['version'])
        self.assertNotIn('taskRouteSelection',changed)

    def test_restart_marks_queued_intent_for_explicit_resume_without_adopting_it(self):
        pid,session=self.task()
        run={'id':'queued-run','status':'queued','mode':'live','nodes':[],'admissionVersion':1,
             'queuedAt':'2026-11-03T10:00:00+0800','admittedAt':None,'queueWaitMs':None}
        attempt=begin_attempt(run,'start',run['queuedAt'],'queued-attempt',{})
        attempt.update(admissionVersion=1,admissionState='queued',queuedAt=run['queuedAt'])
        session.update(status='queued',runs=[run]);self.studio.save(session)
        restored=Studio(self.studio.store,self.client)
        result=restored.load(pid,session['id'])
        self.assertEqual(result['status'],'interrupted')
        self.assertTrue(result['recovery']['requiresExplicitResume'])
        self.assertEqual(result['runs'][0]['executionAttempts'][0]['admissionState'],'interrupted')
        self.assertEqual(restored.state.dispatches(pid)[0]['status'],'awaiting_resume')
        self.assertEqual(restored.queue_snapshot()['taskQueue']['queued'],0)
        self.assertEqual(restored.threads,{})
        self.assertEqual(self.client.calls,[])
        restored.shutdown_admission();restored.task_dispatcher.join(2)


if __name__=='__main__':unittest.main()
