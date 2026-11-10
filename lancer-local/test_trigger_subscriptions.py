"""Durable thread ownership and FIFO inputs without model evaluation for followups."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest

from app import Conflict
from trigger_policy import normalize_source
from trigger_store import TriggerStore
import test_triggers as fixture


class TriggerSubscriptionTests(unittest.TestCase):
    setUp = fixture.TriggerTests.setUp
    tearDown = fixture.TriggerTests.tearDown
    source = fixture.TriggerTests.source
    event = fixture.TriggerTests.event
    decision = fixture.TriggerTests.decision
    evaluate = fixture.TriggerTests.evaluate
    dispatch = fixture.TriggerTests.dispatch
    batch = fixture.TriggerTests.batch

    def subscribed(self):
        target={'projectId':'a'*12,'coordinatorModel':'fixture-worker','teamModels':['fixture-worker'],
                'allowedPaths':['reports/'],'allowedActions':['create_task'],
                'taskRoute':{'mode':'direct','contract':{'roleId':'writer','workType':'writing','complexity':1,
                    'scope':['reports/'],'requiredCapabilities':['text'],'skills':{'writing':1},
                    'instructions':'Update the agreed report only','acceptance':['The fixed report is complete']}}}
        source=self.source('auto',target=target,subscription={'version':1,'enabled':True})
        self.batch(source);self.now+=11;self.engine.process_due()
        link=self.store.subscription_links()[0]
        return source,link

    def test_missing_flag_keeps_legacy_and_enabled_requires_auto_direct(self):
        source=self.source('observe');self.assertNotIn('subscription',source)
        for mode in ('observe','review'):
            with self.assertRaises(ValueError):self.source(mode,subscription={'version':1,'enabled':True})
        with self.assertRaises(ValueError):self.source('auto',subscription={'version':1,'enabled':True})

    def test_completed_owner_receives_followup_without_second_eval_or_task(self):
        source,link=self.subscribed();self.tasks[link['origin']]['status']='completed'
        later=self.engine.receive(source['id'],self.event('later'))['event']
        self.assertEqual(later['status'],'subscribed');self.now+=11;self.engine.process_due()
        self.assertEqual(len(self.evaluations),1);self.assertEqual(len(self.dispatched),1)
        rows=self.store.subscription_inputs(link['origin'],source['id'],source['revision'])
        self.assertEqual([r['id'] for r in rows],[later['id']])
        self.store.subscription_ack(link['origin'],[later['id']],'finished-run')
        self.assertEqual(self.store.subscription_inputs(link['origin'],source['id'],source['revision']),[])
        self.assertEqual(self.store.subscription_snapshot()[0]['consumedCount'],1)

    def test_failure_stays_owned_and_does_not_reassess(self):
        source,link=self.subscribed();self.tasks[link['origin']]['status']='failed'
        self.engine.receive(source['id'],self.event('failure-feedback'));self.now+=11;self.engine.process_due()
        self.assertEqual(len(self.evaluations),1)
        self.assertEqual(self.store.subscription_snapshot()[0]['pendingCount'],1)

    def test_deduplication_fifo_and_bounded_projection_keep_every_unread_event(self):
        source,link=self.subscribed()
        ids=[]
        for i in range(12):
            observed=self.engine.receive(source['id'],self.event('m'+str(i)))
            ids.append(observed['event']['id'])
            self.assertTrue(self.engine.receive(source['id'],self.event('m'+str(i)))['duplicate'])
        self.now+=11
        rows=self.store.subscription_inputs(link['origin'],source['id'],source['revision'])
        self.assertEqual([r['id'] for r in rows],ids[:8])
        self.store.subscription_ack(link['origin'],ids[:8],'run1')
        self.assertEqual([r['id'] for r in self.store.subscription_inputs(link['origin'],source['id'],source['revision'])],ids[8:])
        self.assertEqual(self.store.subscription_snapshot()[0]['pendingCount'],4)

    def test_oversized_input_blocks_without_consuming_or_truncating(self):
        source,link=self.subscribed();body='x'*12001
        observed=self.engine.receive(source['id'],self.event('large',body=body));self.now+=11
        self.assertEqual(self.store.subscription_inputs(link['origin'],source['id'],source['revision']),[])
        row=self.store.subscription_snapshot()[0];self.assertEqual(row['pendingCount'],1);self.assertIn('12000',row['blockedReason'])
        self.assertEqual(self.store.get_event(observed['event']['id'])['body'],body)

    def test_pause_cas_and_restart_preserve_unread_and_owner(self):
        source,link=self.subscribed();self.engine.receive(source['id'],self.event('new'));self.now+=11
        self.store.subscription_action(source['id'],link['threadKey'],source['revision'],link['origin'],1,'pause')
        self.assertEqual(self.store.subscription_inputs(link['origin'],source['id'],source['revision']),[])
        with self.assertRaises(Conflict):self.store.subscription_action(source['id'],link['threadKey'],source['revision'],link['origin'],1,'resume')
        self.store.subscription_action(source['id'],link['threadKey'],source['revision'],link['origin'],2,'resume')
        reopened=TriggerStore(self.store.root,clock=lambda:self.now);reopened.recover()
        self.assertEqual(reopened.subscription_inputs(link['origin'],source['id'],source['revision']),[])
        self.assertEqual(reopened.subscription_snapshot()[0]['pendingCount'],1)
        reopened.resume_source(source['id'],source['revision'])
        self.assertEqual(len(reopened.subscription_inputs(link['origin'],source['id'],source['revision'])),1)
        self.assertEqual(reopened.subscription_links()[0]['origin'],link['origin'])

    def test_alias_and_conflict_never_choose_another_owner(self):
        source,link=self.subscribed()
        self.store.subscribe_thread(source['id'],link['origin'],'issue:22','repository',source['revision'])
        row=self.engine.receive(source['id'],self.event('pr',thread='issue:22'))['event']
        self.assertEqual(row['linkedOrigin'],link['origin'])
        with self.assertRaises(ValueError):self.store.subscribe_thread(source['id'],'unrelated','issue:22','repository',source['revision'])
        self.assertEqual(len(self.evaluations),1)

    def test_source_revision_change_cannot_consume_old_subscription(self):
        source,link=self.subscribed();self.engine.receive(source['id'],self.event('old'));self.now+=11
        changed={k:deepcopy(v) for k,v in source.items() if k not in ('runtimeState','updatedAt','pollStatus')}
        changed['name']='New source policy';new=self.store.save_source(changed)
        self.assertEqual(self.store.subscription_inputs(link['origin'],source['id'],source['revision']),[])
        self.assertEqual(self.store.subscription_inputs(link['origin'],source['id'],new['revision']),[])
        self.assertEqual(self.store.subscription_snapshot()[0]['pendingCount'],1)


class SubscriptionIdentityTests(unittest.TestCase):
    def test_explicit_thread_claim_rejects_another_source_on_same_template(self):
        import test_trigger_subscription_boundaries as bridge
        fixture = bridge.SubscriptionContinuationTests()
        fixture.setUp()
        try:
            effect, saved = fixture.first()
            request = deepcopy(fixture.source_payload)
            request['name'] = 'Another source on the same project'
            request['mode'] = 'auto'
            other = fixture.service.save_source(request)['sources']
            source = next(row for row in other if row['id'] != fixture.source['id'])
            fixture.service.ingest_custom(source['id'], {'externalId':'claim-target', 'threadKey':'handoff',
                'title':'Existing thread', 'body':'Follow up', 'occurredAt':'2026-11-09T12:00:00+08:00'})
            before = fixture.service.store.subscription_links()
            with self.assertRaisesRegex(ValueError, '当前冻结授权'):
                fixture.service.subscribe_thread(source['id'], effect['origin'], 'handoff', 'room', source['revision'])
            self.assertEqual(before, fixture.service.store.subscription_links())
        finally:
            fixture.tearDown()


if __name__=='__main__':unittest.main()
