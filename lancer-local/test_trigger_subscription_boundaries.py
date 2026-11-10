"""Deterministic subscription continuity through the real task authority."""
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch

from trigger_continuation import process_subscriptions, validate_continuation
import test_trigger_service as fixture


class SubscriptionContinuationTests(unittest.TestCase):
    tearDown = fixture.TriggerServiceTests.tearDown
    configure = fixture.TriggerServiceTests.configure
    ingest = fixture.TriggerServiceTests.ingest
    join_task = fixture.TriggerServiceTests.join_task

    def setUp(self):
        fixture.TriggerServiceTests.setUp(self)
        self.source_payload['subscription']={'version':1,'enabled':True}
        self.source_payload['target']['taskRoute']={'mode':'direct','contract':{
            'roleId':'backend','workType':'development','complexity':2,'scope':['direct-task.md'],
            'requiredCapabilities':['text'],'instructions':'Update brief within its fixed scope.',
            'acceptance':['The brief explains the result accurately.'],'skills':{'coding':1}}}
        self.source_payload['target']['allowedPaths']=['direct-task.md']
        self.source=self.configure('auto')

    def first(self):
        self.ingest(self.source)
        self.service.engine.process_due(limit=10)
        effect=self.service.store.effects()[0]
        saved=self.join_task(effect)
        self.assertEqual(saved['status'],'completed',saved.get('error'))
        return effect,saved

    def feedback(self, key='feedback'):
        self.service.ingest_custom(self.source['id'],{'externalId':key,'threadKey':'discussion',
            'title':'Follow up','body':'Please clarify the same brief.', 'occurredAt':'2026-11-09T12:00:00+08:00'})

    def test_completed_owner_continues_same_session_without_new_router_or_worker_selection(self):
        effect, first=self.first()
        root=deepcopy(first['runs'][0]);calls=self.client.evaluations
        self.feedback()
        with patch('business_routing.assign_modules',side_effect=AssertionError('Never reassign Worker')) as assignment:
            outcomes=process_subscriptions(self.service,threading.Event())
            self.assertEqual(outcomes[0]['status'],'continued',outcomes)
            latest=self.join_task(effect)
            self.assertEqual(latest['status'],'completed',latest.get('error'))
            assignment.assert_not_called()
        self.assertEqual(len(latest['runs']),2)
        new=latest['runs'][-1]
        self.assertEqual(self.client.evaluations,calls)
        self.assertEqual(root,latest['runs'][0])
        self.assertEqual(root['plan']['modules'][0]['assignment'],new['plan']['modules'][0]['assignment'])
        old_worker=next(n for n in root['nodes'] if n['phase']=='build')
        worker=next(n for n in new['nodes'] if n['phase']=='build')
        self.assertEqual(old_worker['agentId'],worker['agentId'])
        context=self.studio.context(latest['projectId'],latest['id'],worker['contextRef'])
        self.assertTrue(context['memory']['subscriptionInputs']['events'])
        self.assertGreater(context['memoryPolicy']['ownTurnsIncluded'],0)
        self.assertEqual(new['projectRevision'],root['mergedRevision'])
        validate_continuation(self.studio,latest,new)
        before=len(self.client.calls)
        process_subscriptions(self.service,threading.Event())
        self.feedback()
        process_subscriptions(self.service,threading.Event())
        self.assertEqual(len(self.client.calls),before)
        self.assertEqual(len(self.studio.load(latest['projectId'],latest['id'])['runs']),2)

    def test_failed_owner_keeps_feedback_pending_without_model_or_new_session(self):
        effect, saved=self.first()
        saved['status']=saved['runs'][-1]['status']='failed'
        self.studio.save(saved)
        self.feedback()
        before=(len(self.client.calls),self.client.evaluations)
        process_subscriptions(self.service,threading.Event())
        current=self.studio.load(saved['projectId'],saved['id'])
        self.assertEqual(len(current['runs']),1)
        self.assertEqual((len(self.client.calls),self.client.evaluations),before)
        self.assertTrue(self.service.store.subscription_inputs(effect['origin'],self.source['id'],self.source['revision']))

    def test_same_sender_other_thread_does_not_continue_owner(self):
        effect,saved=self.first()
        self.service.ingest_custom(self.source['id'],{'externalId':'other-thread','threadKey':'another-topic',
            'title':'Same sender','body':'Unrelated request', 'occurredAt':'2026-11-09T12:00:00+08:00'})
        process_subscriptions(self.service,threading.Event())
        self.assertEqual(len(self.studio.load(saved['projectId'],saved['id'])['runs']),1)
        self.assertFalse(self.service.store.subscription_inputs(effect['origin'],self.source['id'],self.source['revision']))

    def test_source_revision_change_blocks_old_owner(self):
        effect,saved=self.first();self.feedback()
        source=self.service.store.get_source(self.source['id']);source['name']='Changed policy version'
        self.service.save_source(source)
        before=len(self.client.calls)
        process_subscriptions(self.service,threading.Event())
        self.assertEqual(len(self.client.calls),before)
        self.assertEqual(len(self.studio.load(saved['projectId'],saved['id'])['runs']),1)

    def test_completed_turn_consumes_active_input_only_after_returned_parent_context(self):
        effect,saved=self.first();self.feedback()
        # A later event with no corresponding returned parent context stays pending.
        self.assertTrue(self.service.store.subscription_inputs(effect['origin'],self.source['id'],self.source['revision']))
        process_subscriptions(self.service,threading.Event());saved=self.join_task(effect)
        process_subscriptions(self.service,threading.Event())
        self.assertFalse(self.service.store.subscription_inputs(effect['origin'],self.source['id'],self.source['revision']))
        self.feedback('late-after-final')
        process_subscriptions(self.service,threading.Event());saved=self.join_task(effect)
        self.assertEqual(saved['status'],'completed',saved.get('error'))
        self.assertEqual(len(saved['runs']),3)


    def test_paused_root_does_not_hide_an_active_alias_for_same_owner(self):
        effect,saved=self.first()
        link=self.service.store.link_for(self.source['id'],'discussion')
        self.service.store.subscribe_thread(self.source['id'],effect['origin'],'z-followup','room',self.source['revision'])
        self.service.store.subscription_action(self.source['id'],'discussion',self.source['revision'],effect['origin'],link['subscription']['version'],'pause')
        self.service.ingest_custom(self.source['id'],{'externalId':'alias-event','threadKey':'z-followup','title':'Follow up',
            'body':'Clarify this same work.','occurredAt':'2026-11-09T12:00:00+08:00'})
        result=process_subscriptions(self.service,threading.Event())
        self.assertEqual(result[0]['status'],'continued',result)
        self.assertEqual(self.join_task(effect)['status'],'completed')

    def test_missing_continuation_marker_cannot_downgrade_saved_lineage(self):
        effect,saved=self.first();self.feedback()
        process_subscriptions(self.service,threading.Event());saved=self.join_task(effect)
        saved['runs'][-1].pop('triggerContinuation')
        with self.assertRaisesRegex(ValueError,'标记遗失'):
            validate_continuation(self.studio,saved,saved['runs'][-1])

    def test_context_memory_tampering_does_not_consume_feedback(self):
        effect,saved=self.first();self.feedback()
        process_subscriptions(self.service,threading.Event());saved=self.join_task(effect)
        run=saved['runs'][-1]
        node=next(n for n in run['nodes'] if n['phase']=='build')
        path=self.studio.project_dir(saved['projectId'])/saved['id']/'contexts'/(node['contextRef']+'.json')
        context=json.loads(path.read_text());context['memory']['subscriptionInputs']['events'][0]['body']='Different body'
        path.write_text(json.dumps(context))
        result=process_subscriptions(self.service,threading.Event())
        self.assertEqual(result[0]['status'],'blocked')
        self.assertTrue(self.service.store.subscription_inputs(effect['origin'],self.source['id'],self.source['revision']))


    def test_restart_preserves_pending_feedback_and_requires_source_resume(self):
        from app import Store
        from studio import Studio
        effect,saved=self.first();self.feedback()
        before=len(self.client.calls)
        self.service.close();self.studio.shutdown_admission()
        root=self.studio.store.root
        self.studio=Studio(Store(root,seed=False),self.client)
        self.service=self.studio.triggers
        self.assertEqual(self.service.store.get_source(self.source['id'])['runtimeState'],'awaiting_resume')
        self.assertEqual(process_subscriptions(self.service,threading.Event()),[])
        self.assertEqual(len(self.client.calls),before)
        with patch.object(self.service,'start_monitor',lambda:None):
            self.service.resume_source(self.source['id'],self.source['revision'])
        outcomes=process_subscriptions(self.service,threading.Event())
        self.assertEqual(outcomes[0]['status'],'continued',outcomes)
        latest=self.join_task(effect)
        self.assertEqual(latest['status'],'completed',latest.get('error'))
        self.assertEqual(len(latest['runs']),2)
        self.assertEqual(self.client.evaluations,1)

    def test_failed_start_keeps_one_intent_and_is_not_automatically_recreated(self):
        effect,saved=self.first();self.feedback()
        before=len(self.client.calls)
        with patch.object(self.studio,'start',side_effect=RuntimeError('Injected admission failure')):
            result=process_subscriptions(self.service,threading.Event())
        self.assertEqual(result[0]['status'],'blocked')
        current=self.studio.load(saved['projectId'],saved['id'])
        self.assertEqual(len(current['subscriptionContinuations']),1)
        self.assertEqual(current['subscriptionContinuations'][0]['status'],'blocked')
        process_subscriptions(self.service,threading.Event())
        self.assertEqual(len(self.client.calls),before)
        self.assertEqual(len(self.studio.load(saved['projectId'],saved['id'])['runs']),1)


    def test_idle_completed_subscription_skips_artifact_and_context_verification(self):
        effect,saved=self.first()
        before=len(self.client.calls)
        with patch('trigger_continuation._verify_completed_delivery', side_effect=AssertionError('Idle proof work')) as verify, \
             patch('trigger_continuation._consumed_ids', side_effect=AssertionError('Idle context scan')) as consumed, \
             patch.object(self.service.engine,'lookup_origin',side_effect=AssertionError('Idle session load')) as lookup:
            for _ in range(3):
                self.assertEqual(process_subscriptions(self.service,threading.Event()),[])
            verify.assert_not_called();consumed.assert_not_called();lookup.assert_not_called()
        self.assertEqual(len(self.client.calls),before)

    def test_after_acknowledgement_idle_ticks_skip_proof_work(self):
        effect,saved=self.first();self.feedback()
        process_subscriptions(self.service,threading.Event());saved=self.join_task(effect)
        process_subscriptions(self.service,threading.Event())
        self.assertFalse(self.service.store.subscription_inputs(effect['origin'],self.source['id'],self.source['revision']))
        with patch('trigger_continuation._verify_completed_delivery',side_effect=AssertionError('Repeated proof')):
            self.assertEqual(process_subscriptions(self.service,threading.Event()),[])


    def test_completed_status_waits_for_worker_handle_to_finish_before_continuing(self):
        from types import SimpleNamespace
        effect,saved=self.first();self.feedback()
        before=self.studio.load(saved['projectId'],saved['id'])
        before_calls=len(self.client.calls)
        real_handle=self.studio.threads[saved['id']]
        finishing=SimpleNamespace(is_alive=lambda:True)
        self.studio.threads[saved['id']]=finishing
        try:
            with patch.object(self.service.store,'subscription_ack',side_effect=AssertionError('Acknowledged before worker drain')):
                self.assertEqual(process_subscriptions(self.service,threading.Event()),[])
            self.assertEqual(self.studio.load(saved['projectId'],saved['id']),before)
            self.assertEqual(len(self.client.calls),before_calls)
            self.assertNotIn('subscriptionContinuations',before)
        finally:
            self.studio.threads[saved['id']]=real_handle
        self.assertFalse(real_handle.is_alive())
        result=process_subscriptions(self.service,threading.Event())
        self.assertEqual(result[0]['status'],'continued',result)
        latest=self.join_task(effect)
        self.assertEqual(latest['status'],'completed',latest.get('error'))
        self.assertEqual(len(latest['runs']),2)


if __name__=='__main__':unittest.main()
