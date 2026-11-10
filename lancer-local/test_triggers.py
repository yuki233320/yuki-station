"""Pure fixture evaluators/tasks over actual SQLite; no model, PR or HTTP writes."""
from copy import deepcopy
from pathlib import Path
import hashlib
import shutil
import tempfile
import threading
import unittest

from app import Conflict
from live import Cancelled
from trigger_engine import TriggerEngine
from trigger_store import TriggerStore, encoded, fingerprint


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1000.0
        self.store=TriggerStore(Path(self.tmp.name),clock=lambda:self.now)
        self.evaluations=[];self.dispatched=[];self.tasks={}
        self.engine=TriggerEngine(self.store,self.evaluate,self.dispatch,self.tasks.get,clock=lambda:self.now)

    def tearDown(self):self.tmp.cleanup()

    def source(self,mode='observe',**changes):
        value={'name':'Fixture source','kind':'custom','enabled':True,'mode':mode,
               'config':{'actor':'alice','container':'repository','eventType':'custom.request'},
               'policy':{'actors':['alice'],'containers':['repository'],'eventTypes':['custom.request'],
                         'debounceSeconds':10,'maxBatchEvents':100,'cooldownSeconds':120,
                         'rules':[{'id':'requested-report','description':'Implement explicitly requested report changes','enabled':True}]},
               'target':{'projectId':'a'*12,'coordinatorModel':'fixture-worker','teamModels':['fixture-worker'],
                         'allowedPaths':['reports/'],'allowedActions':['create_task'],'taskRoute':{'mode':'team'}},
               'evaluatorModel':'fixture-evaluator'}
        value.update(changes)
        return self.store.save_source(value)

    def event(self,key='event-1',thread='thread-1',**changes):
        value={'externalId':key,'threadKey':thread,'actor':'alice','container':'repository','eventType':'custom.request',
               'isBot':False,'isSelfEcho':False,'title':'Please update the report','body':'Please update reports/result.md',
               'url':'','occurredAt':'2026-11-03T10:00:00+08:00','origin':{'kind':'custom'}}
        value.update(changes);return value

    def decision(self,snapshot):
        return {'action':'accept','title':'Update report','request':'Update reports/result.md within the fixed mandate',
                'reason':'Matches the explicit report rule','matchedRuleIds':['requested-report'],
                'evidenceEventIds':[snapshot['eventIds'][-1]]}

    def evaluate(self,snapshot,cancel):
        self.evaluations.append(deepcopy(snapshot))
        return {'decision':self.decision(snapshot),'receipt':{'mode':'fixture','usage':{'input_tokens':10,'output_tokens':5}}}

    def dispatch(self,effect,cancel):
        self.assertEqual(self.store.get_effect(effect['id'])['status'],'dispatching')
        self.dispatched.append(deepcopy(effect))
        task={'projectId':effect['taskProjectId'],'templateProjectId':effect['target']['projectId'],
              'sessionId':effect['sessionId'],'origin':effect['origin'],'status':'queued'}
        self.tasks[effect['origin']]=task
        return task

    def batch(self,source,event=None):
        result=self.engine.receive(source['id'],event or self.event())
        return result['event']['batchId']

    def test_continuous_messages_cannot_postpone_a_batch_forever(self):
        source=self.source('auto');first=None
        for index in range(20):
            self.now=1000.0+index*5
            batch=self.batch(source,self.event('stream-'+str(index)))
            first=first or batch
            self.assertEqual(batch,first)
        ready=self.store.ready_batches(4)
        self.assertEqual(len(ready),1)
        self.assertEqual(ready[0]['dueAt'],1060.0)
        self.assertEqual(ready[0]['coalesceDeadlineAt'],1060.0)
        self.assertEqual(ready[0]['maxHoldSeconds'],60)
        self.engine.process_due(limit=1)
        self.assertEqual(len(self.evaluations),1)
        self.assertEqual(len(self.dispatched),1)

    def test_linked_inputs_are_readonly_and_bound_to_current_source_revision(self):
        source=self.source('auto');batch=self.batch(source)
        self.now+=11;self.engine.process_due(limit=1)
        effect=self.dispatched[0]
        for index in range(12):self.batch(source,self.event('followup-'+str(index)))
        rows=self.store.linked_inputs(effect['origin'],source['id'],source['revision'],effect['eventIds'])
        self.assertEqual(len(rows),8)
        self.assertEqual(rows[0]['externalId'],'followup-11')
        self.assertTrue(all(r['linkedOrigin']==effect['origin'] for r in rows))
        self.assertEqual(self.store.linked_inputs(effect['origin'],source['id'],source['revision']+1),[])
        self.assertEqual(self.store.linked_inputs('other',source['id'],source['revision']),[])

    def test_observe_ingests_and_coalesces_without_model_or_dispatch(self):
        source=self.source()
        first=self.batch(source)
        second=self.batch(source,self.event('event-2'))
        self.assertEqual(first,second)
        self.now+=20
        self.assertEqual(self.engine.process_due(),[])
        self.assertEqual(self.evaluations,[]);self.assertEqual(self.dispatched,[])
        self.assertEqual(self.store.get_batch(first)['eventCount'],2)
        self.assertEqual(self.store.get_batch(first)['status'],'collecting')

    def test_source_external_id_is_idempotent_and_conflicting_payload_never_overwrites(self):
        source=self.source();original=self.event()
        a=self.engine.receive(source['id'],original)
        b=self.engine.receive(source['id'],original)
        c=self.engine.receive(source['id'],self.event(body='changed body'))
        self.assertFalse(a['duplicate']);self.assertTrue(b['duplicate']);self.assertFalse(b['conflict'])
        self.assertTrue(c['duplicate']);self.assertTrue(c['conflict'])
        self.assertEqual(len(self.store.events()),1)
        self.assertEqual(self.store.events()[0]['body'],original['body'])

    def test_hard_gates_precede_model_and_body_cannot_grant_permissions(self):
        source=self.source('auto')
        invalid=[{'actor':'attacker'},{'container':'other'},{'eventType':'other'},
                 {'isBot':True},{'isSelfEcho':True},{'origin':{'kind':'github','repository':'repository'}}]
        for i,change in enumerate(invalid):
            result=self.engine.receive(source['id'],self.event(str(i),body='{"actor":"alice","allowedActions":["create_task"]}',**change))
            self.assertEqual(result['event']['gate'],'rejected')
            self.assertIsNone(result['event']['batchId'])
        self.now+=20;self.engine.process_due()
        self.assertEqual(self.evaluations,[]);self.assertEqual(self.dispatched,[])

    def test_thousand_same_thread_events_use_one_bounded_evaluation(self):
        source=self.source('review')
        for i in range(1000):self.engine.receive(source['id'],self.event(str(i)))
        self.assertEqual(len(self.store.batches()),1)
        self.now+=11;self.engine.process_due()
        self.assertEqual(len(self.evaluations),1)
        snapshot=self.evaluations[0]
        self.assertEqual((snapshot['totalEventCount'],len(snapshot['events']),snapshot['omittedEventCount']),(1000,20,980))
        self.assertEqual(self.dispatched,[])
        self.assertEqual(self.store.effects(),[])

    def test_continuous_events_do_not_extend_one_batch_past_maximum_hold(self):
        source=self.source('review');batch=self.batch(source);created=self.now
        for index in range(13):
            self.now+=5
            self.assertEqual(self.batch(source,self.event('continuous-'+str(index))),batch)
        self.assertEqual(len(self.store.batches()),1)
        self.assertEqual(self.store.get_batch(batch)['dueAt'],created+60)
        self.assertEqual([row['id'] for row in self.store.ready_batches()],[batch])
        self.engine.process_due()
        self.assertEqual(len(self.evaluations),1)
        self.assertEqual(self.evaluations[0]['totalEventCount'],14)
        self.assertEqual(self.evaluations[0]['maxHoldSeconds'],60)
        self.assertEqual(self.evaluations[0]['coalesceDeadlineAt'],created+60)

    def test_linked_inputs_are_recent_excluded_and_exact_current_authority_only(self):
        source=self.source('auto');self.batch(source);self.now+=11;self.engine.process_due()
        effect=self.store.effects()[0];observed=[]
        for index in range(12):
            self.now+=1
            observed.append(self.engine.receive(source['id'],self.event('attached-'+str(index)))['event'])
        initial=list(effect['eventIds'])
        inputs=self.store.linked_inputs(effect['origin'],source['id'],source['revision'],initial)
        self.assertEqual([row['id'] for row in inputs],[row['id'] for row in reversed(observed[-8:])])
        previous=[row['id'] for row in observed[-4:]]
        more=self.store.linked_inputs(effect['origin'],source['id'],source['revision'],initial+previous)
        self.assertEqual([row['id'] for row in more],[row['id'] for row in reversed(observed[:8])])
        self.assertEqual(self.store.linked_inputs('another-origin',source['id'],source['revision']),[])
        self.assertEqual(self.store.linked_inputs(effect['origin'],'different-source',source['revision']),[])
        changed=deepcopy(source);changed['name']='Updated source policy'
        self.store.save_source(changed)
        self.assertEqual(self.store.linked_inputs(effect['origin'],source['id'],source['revision']),[])
        self.assertTrue(all(self.store.get_event(row['id'])['status']=='attached' for row in observed))

    def test_manual_evaluation_never_auto_dispatches_even_for_auto_source(self):
        source=self.source('auto');batch=self.batch(source)
        decision=self.engine.evaluate(batch,manual=True)
        self.assertEqual(decision['status'],'accept');self.assertEqual(decision['evaluationMode'],'manual')
        self.assertEqual(self.store.effects(),[])
        self.now+=20;self.engine.process_due()
        self.assertEqual(self.dispatched,[])
        effect=self.engine.dispatch_batch(batch)
        self.assertEqual(effect['status'],'dispatched')
        self.assertEqual(len(self.dispatched),1)
        self.assertEqual(effect['mandate']['allowedPaths'],['reports/'])
        self.assertEqual(effect['mandate']['rules'][0]['id'],'requested-report')

    def test_auto_uses_stable_effect_identity_and_linked_active_thread_needs_no_new_model(self):
        source=self.source('auto');batch=self.batch(source)
        self.now+=11;self.engine.process_due()
        self.assertEqual((len(self.evaluations),len(self.dispatched)),(1,1))
        effect=self.store.effects()[0]
        self.assertEqual(effect['origin'],'trigger:'+source['id']+':'+batch)
        result=self.engine.receive(source['id'],self.event('follow-up'))
        self.assertEqual(result['event']['status'],'attached')
        self.now+=20;self.engine.process_due()
        self.assertEqual((len(self.evaluations),len(self.dispatched)),(1,1))

    def test_completed_thread_cooldown_defers_new_batch_without_model_calls(self):
        source=self.source('auto');self.batch(source);self.now+=11;self.engine.process_due()
        task=next(iter(self.tasks.values()));task['status']='completed'
        next_batch=self.batch(source,self.event('after-completion'))
        self.now+=20;self.engine.process_due()
        self.assertEqual(len(self.evaluations),1)
        self.assertEqual(self.store.get_batch(next_batch)['deferredReason'],'cooldown')
        self.now+=121;self.engine.process_due()
        self.assertEqual(len(self.evaluations),2)
        self.assertEqual(len(self.dispatched),2)

    def test_policy_change_during_evaluation_discards_result_before_effect_creation(self):
        source=self.source('auto');batch=self.batch(source)
        entered,release=threading.Event(),threading.Event();outcomes=[]
        def slow(snapshot,cancel):
            entered.set();release.wait(3);return self.evaluate(snapshot,cancel)
        engine=TriggerEngine(self.store,slow,self.dispatch,self.tasks.get)
        thread=threading.Thread(target=lambda:outcomes.append(engine.evaluate(batch,manual=False)))
        self.now+=11;thread.start()
        try:
            self.assertTrue(entered.wait(2))
            changed=deepcopy(source);changed['policy']['rules'][0]['description']='A revised and narrower rule'
            self.store.save_source(changed)
        finally:release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(outcomes[0]['status'],'discarded')
        self.assertEqual(self.store.effects(),[]);self.assertEqual(self.dispatched,[])

    def test_invalid_rule_or_event_references_never_authorize_a_task(self):
        for index,changes in enumerate(({'matchedRuleIds':[]},{'matchedRuleIds':['invented']},
                                       {'evidenceEventIds':['not-provided']},{'allowedPaths':['/']})):
            source=self.source();batch=self.batch(source,self.event(str(index)))
            def invalid(snapshot,cancel):return {**self.decision(snapshot),**changes}
            engine=TriggerEngine(self.store,invalid,self.dispatch,self.tasks.get)
            with self.assertRaises(ValueError):engine.evaluate(batch)
            self.assertEqual(self.store.get_batch(batch)['status'],'unknown')
        self.assertEqual(self.store.effects(),[])

    def test_unknown_evaluation_does_not_retry_on_resume_or_later_tick(self):
        source=self.source('auto');batch=self.batch(source);calls=[]
        def lost(snapshot,cancel):calls.append(snapshot);raise OSError('lost model reply')
        engine=TriggerEngine(self.store,lost,self.dispatch,self.tasks.get)
        with self.assertRaises(OSError):engine.evaluate(batch)
        self.store.recover();self.store.resume_source(source['id'],source['revision'])
        self.now+=30;engine.process_due()
        with self.assertRaises(ValueError):engine.evaluate(batch)
        self.assertEqual(len(calls),1);self.assertEqual(self.dispatched,[])

    def test_lost_dispatch_reply_reconciles_origin_without_repeating_create_start(self):
        source=self.source('auto');self.batch(source)
        def lost(effect,cancel):self.dispatch(effect,cancel);raise OSError('lost task creation reply')
        engine=TriggerEngine(self.store,self.evaluate,lost,self.tasks.get)
        self.now+=11;engine.process_due()
        effect=self.store.effects()[0];self.assertEqual(effect['status'],'unknown')
        engine.process_due();self.assertEqual(len(self.dispatched),1)
        reconciled=engine.reconcile(effect['id'])[0]
        self.assertEqual(reconciled['status'],'reconciled')
        self.assertEqual(reconciled['sessionId'],effect['sessionId'])
        self.assertEqual(len(self.dispatched),1)

    def test_unknown_dispatch_without_origin_stays_unknown_and_does_not_call_bridge(self):
        source=self.source('auto');self.batch(source);calls=[]
        def lost(effect,cancel):calls.append(effect);raise OSError('uncertain before/after boundary')
        engine=TriggerEngine(self.store,self.evaluate,lost,self.tasks.get)
        self.now+=11;engine.process_due();effect=self.store.effects()[0]
        self.store.recover();self.store.resume_source(source['id'],source['revision'])
        engine.process_due();engine.reconcile()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.store.get_effect(effect['id'])['status'],'unknown')

    def test_restart_pauses_known_pending_until_explicit_resume_and_does_not_adopt_old_calls(self):
        source=self.source('auto');batch=self.batch(source)
        recovered=TriggerEngine(TriggerStore(self.store.root,clock=lambda:self.now),self.evaluate,self.dispatch,self.tasks.get,recover=True)
        self.now+=20;recovered.process_due()
        self.assertEqual(self.evaluations,[])
        self.assertEqual(self.store.get_batch(batch)['status'],'awaiting_resume')
        self.store.resume_source(source['id'],source['revision'])
        recovered.process_due()
        self.assertEqual((len(self.evaluations),len(self.dispatched)),(1,1))

    def test_source_cas_and_old_poller_revision_cannot_change_new_policy(self):
        source=self.source();changed=deepcopy(source);changed['name']='Renamed'
        newer=self.store.save_source(changed)
        self.assertEqual(newer['revision'],2)
        with self.assertRaises(Conflict):self.store.save_source(source)
        with self.assertRaises(Conflict):self.engine.receive(source['id'],self.event(),source['revision'])
        self.assertFalse(self.store.save_poll_cursor(source['id'],source['revision'],{'watermark':'stale'}))
        self.assertIsNone(self.store.source_cursor(source['id']))

    def test_poll_status_is_readonly_source_projection_without_policy_revision_change(self):
        source=self.source();errors=[{'category':'permission','httpStatus':403,'feed':'issues'}]
        self.store.save_poll_cursor(source['id'],source['revision'],{'watermark':'known'},errors)
        observed=self.store.get_source(source['id'])
        self.assertEqual(observed['revision'],source['revision'])
        self.assertEqual(observed['pollStatus'],errors)
        observed['name']='Renamed';observed['pollStatus']={'forged':'healthy'}
        updated=self.store.save_source(observed)
        self.assertEqual(updated['revision'],source['revision']+1)
        self.assertEqual(updated['pollStatus'],errors)

    def test_cancellation_before_and_during_evaluation_creates_no_effect(self):
        source=self.source('auto');batch=self.batch(source);cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):self.engine.evaluate(batch,cancel)
        self.assertEqual(self.evaluations,[])
        cancel.clear()
        def cancelled(snapshot,event):event.set();return self.decision(snapshot)
        engine=TriggerEngine(self.store,cancelled,self.dispatch,self.tasks.get)
        with self.assertRaises(Cancelled):engine.evaluate(batch,cancel)
        self.assertEqual(self.store.get_batch(batch)['status'],'unknown')
        self.assertEqual(self.store.effects(),[])

    def test_sample_context_is_bounded_and_originals_stay_complete(self):
        source=self.source('review');body='x'*20000
        for index in range(25):self.batch(source,self.event(str(index),body=body))
        self.now+=11;self.engine.process_due();snapshot=self.evaluations[0]
        self.assertEqual(len(snapshot['events']),20)
        self.assertEqual(sum(len(event['body']) for event in snapshot['events']),24000)
        self.assertTrue(all(event['bodyTruncated'] and event['bodySha256']==hashlib.sha256(body.encode()).hexdigest() for event in snapshot['events']))
        self.assertTrue(all(event['body']==body for event in self.store.events()))

    def test_unknown_thread_batches_do_not_starve_other_ready_threads(self):
        source=self.source('review')
        def lost(snapshot,cancel):raise OSError('unknown evaluator result')
        uncertain=TriggerEngine(self.store,lost,self.dispatch,self.tasks.get)
        for index in range(4):
            thread='blocked-'+str(index)
            batch=self.batch(source,self.event('old-'+str(index),thread))
            with self.assertRaises(OSError):uncertain.evaluate(batch)
            self.batch(source,self.event('new-'+str(index),thread))
        fresh=self.batch(source,self.event('fresh','fresh-thread'));self.now+=11
        self.engine.process_due(limit=4)
        self.assertEqual(self.evaluations,[])
        self.engine.process_due(limit=4)
        self.assertEqual([snapshot['batchId'] for snapshot in self.evaluations],[fresh])

    def test_oversized_decision_is_rejected_before_any_task_effect(self):
        source=self.source('auto');batch=self.batch(source)
        def oversized(snapshot,cancel):return {**self.decision(snapshot),'request':'x'*12001}
        engine=TriggerEngine(self.store,oversized,self.dispatch,self.tasks.get)
        with self.assertRaises(ValueError):engine.evaluate(batch)
        self.assertEqual(self.dispatched,[]);self.assertEqual(self.store.effects(),[])

    def test_missing_database_never_recreates_empty_authority(self):
        self.source();self.store.path.rename(self.store.path.with_suffix('.saved'))
        with self.assertRaises(ValueError):self.store.sources()
        with self.assertRaises(ValueError):self.source()
        with self.assertRaises(ValueError):TriggerStore(self.store.root)
        self.assertFalse(self.store.path.exists())

    def test_replaced_database_and_missing_marker_are_refused(self):
        self.source();other=TriggerStore(self.store.root/'other')
        shutil.copyfile(other.path,self.store.path)
        with self.assertRaises(ValueError):self.store.sources()
        with self.assertRaises(ValueError):TriggerStore(self.store.root)
        self.store.marker.rename(self.store.marker.with_suffix('.saved'))
        with self.assertRaises(ValueError):TriggerStore(self.store.root)

    def test_root_database_marker_and_sidecar_symlinks_are_refused(self):
        outside=self.store.root/'outside';outside.mkdir()
        linked=self.store.root/'linked';linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):TriggerStore(linked)
        self.assertEqual(list(outside.iterdir()),[])
        for name in ('trigger-state.sqlite3','trigger-state.authority.json','trigger-state.sqlite3-wal','trigger-state.sqlite3-shm'):
            with self.subTest(name=name):
                root=self.store.root/name.replace('.','-');test_store=TriggerStore(root)
                path=root/name
                if path.exists():path.rename(root/'original')
                path.symlink_to(outside/'uncreated')
                with self.assertRaises(ValueError):test_store.sources()
                with self.assertRaises(ValueError):TriggerStore(root)
                self.assertFalse((outside/'uncreated').exists())

    def test_new_source_revision_does_not_attach_to_previous_target_active_task(self):
        source=self.source('auto');self.batch(source);self.now+=11;self.engine.process_due()
        previous=self.store.effects()[0]
        changed=deepcopy(source);changed['target']['projectId']='b'*12
        newer=self.store.save_source(changed)
        observed=self.engine.receive(newer['id'],self.event('new-target'))
        self.assertEqual(observed['event']['status'],'pending')
        self.now+=11;self.engine.process_due()
        self.assertEqual(len(self.dispatched),2)
        self.assertEqual(self.dispatched[-1]['target']['projectId'],'b'*12)
        self.assertNotEqual(self.dispatched[-1]['origin'],previous['origin'])
        self.assertEqual(self.store.link_for(source['id'],'thread-1')['sourceRevision'],2)

    def test_old_unknown_evaluation_does_not_block_new_revision_and_stays_unknown(self):
        source=self.source('auto');old=self.batch(source)
        def lost(snapshot,cancel):raise OSError('unknown old evaluation')
        engine=TriggerEngine(self.store,lost,self.dispatch,self.tasks.get)
        with self.assertRaises(OSError):engine.evaluate(old)
        changed=deepcopy(source);changed['policy']['rules'][0]['description']='Changed explicit rule'
        newer=self.store.save_source(changed)
        self.batch(newer,self.event('new-policy'));self.now+=11;self.engine.process_due()
        self.assertEqual(len(self.dispatched),1)
        self.assertEqual(self.dispatched[0]['sourceRevision'],2)
        self.assertEqual(self.store.get_batch(old)['status'],'unknown')
        engine.evaluate(old)
        self.assertEqual(self.store.get_batch(old)['status'],'unknown')

    def test_old_unknown_effect_neither_blocks_new_revision_nor_replaces_new_link_on_reconcile(self):
        source=self.source('auto');self.batch(source)
        def lost(effect,cancel):self.dispatch(effect,cancel);raise OSError('lost previous callback reply')
        engine=TriggerEngine(self.store,self.evaluate,lost,self.tasks.get)
        self.now+=11;engine.process_due();old=self.store.effects()[0]
        self.assertEqual(old['status'],'unknown')
        changed=deepcopy(source);changed['policy']['rules'][0]['description']='Newer policy'
        newer=self.store.save_source(changed);self.batch(newer,self.event('new-revision'))
        self.now+=11;self.engine.process_due()
        self.assertEqual(len(self.dispatched),2)
        current=self.store.link_for(source['id'],'thread-1')
        self.assertEqual(current['sourceRevision'],2)
        self.assertEqual(self.store.get_effect(old['id'])['status'],'unknown')
        engine.reconcile(old['id'])
        self.assertEqual(self.store.link_for(source['id'],'thread-1')['origin'],current['origin'])

    def test_ready_auto_effects_filter_manual_observe_rows_before_limit(self):
        for index in range(3):
            source=self.source();batch=self.batch(source,self.event(str(index)))
            self.engine.evaluate(batch);self.store.create_manual_effect(batch)
        source=self.source('auto');batch=self.batch(source);self.now+=11
        self.engine.evaluate(batch,manual=False)
        self.assertEqual(len(self.store.ready_effects(1)),1)
        self.engine.process_due(limit=1)
        self.assertEqual(len(self.dispatched),1)
        self.assertEqual(self.dispatched[0]['sourceId'],source['id'])
        self.assertEqual(self.store.ready_effects(1),[])

    def test_publication_observations_are_durable_non_authoritative_and_do_not_hide_later_candidates(self):
        source=self.source('auto');self.batch(source);self.now+=11;self.engine.process_due()
        original=self.store.effects()[0];effect_ids=[original['id']]
        with self.store.transaction() as db:
            for index in range(203):
                effect=deepcopy(original);effect['id']=fingerprint(['effect',index])[:12]
                effect['batchId']='fixture-batch-'+str(index);effect['origin']='fixture-origin-'+str(index)
                effect['sessionId']=fingerprint(['session',index])[:12]
                effect_ids.append(effect['id'])
                payload={key:value for key,value in effect.items() if key not in ('status','createdAt')}
                db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (effect['id'],effect['batchId'],effect['origin'],effect['sessionId'],source['id'],
                     effect['threadKey'],source['revision'],'dispatched',self.now,encoded(payload)))
        report={'status':'published','reportRef':'github-deliveries/fixture/receipt.json','reportSha256':'a'*64}
        for effect_id in effect_ids[:202]:self.store.note_publication(effect_id,report)
        observed=self.store.get_effect(original['id'])
        self.assertEqual(observed['status'],original['status'])
        self.assertEqual(observed['task'],original['task'])
        self.assertEqual(observed['publicationObservation']['report'],report)
        recovered=TriggerStore(self.store.root,clock=lambda:self.now)
        page=recovered.publication_candidates(limit=1)
        self.assertEqual([row['id'] for row in page],effect_ids[202:203])
        self.assertEqual([row['id'] for row in recovered.publication_candidates(limit=1,after_id=page[-1]['id'])],effect_ids[203:])
        self.assertEqual(len(recovered.publication_candidates(limit=None)),2)


if __name__=='__main__':unittest.main()
