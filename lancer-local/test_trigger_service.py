"""Real local task dispatch from events; all model replies are fixtures."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Store, Conflict
from studio import Studio
from test_studio_business import BusinessClient


class AdmissionClient(BusinessClient):
    def __init__(self):
        super().__init__()
        self.evaluations = 0
        self.on_evaluation = None

    def call(self, model, messages, cancel, on_delta=None):
        text = messages[-1]['content']
        if text.startswith('判断这些外部事件'):
            view = json.loads(text.split('\n', 1)[1])
            self.evaluations += 1
            if self.on_evaluation:self.on_evaluation()
            result = {'action':'accept','title':'Update the report','request':'整理brief.md中的项目说明，不改变其他文件。',
                      'reason':'符合已授权的报告规则','evidenceEventIds':[view['events'][0]['id']],
                      'matchedRuleIds':[view['rules'][0]['id']]}
            return result, {'mode':'fixture','model':model,'protocol':'chat','usage':{'prompt_tokens':40,'completion_tokens':10},
                            'responseComplete':True,'jsonValidated':True}
        return super().call(model,messages,cancel,on_delta)


class TriggerServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = AdmissionClient()
        self.studio = Studio(Store(Path(self.tmp.name)/'data', seed=False),self.client)
        self.service = self.studio.triggers
        self.project = self.studio.create_project('Source template', 'No real calls')
        self.pid = self.project['id']
        self.monitor = patch.object(self.service,'start_monitor',lambda:None)
        self.monitor.start()
        self.network = patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No real network allowed'))
        self.network.start()
        self.source_payload = {'name':'Controlled custom source','kind':'custom','enabled':True,'mode':'observe',
            'config':{'actor':'alice','container':'room','eventType':'custom.request'},
            'policy':{'actors':['alice'],'containers':['room'],'eventTypes':['custom.request'],
                      'debounceSeconds':0,'cooldownSeconds':0,
                      'rules':[{'id':'report','description':'整理明确请求的项目说明','enabled':True}]},
            'target':{'projectId':self.pid,'coordinatorModel':'opus','teamModels':['opus','sol','gemini'],
                      'allowedPaths':['brief.md'],'allowedActions':['create_task'],'taskRoute':{'mode':'team'}},
            'evaluatorModel':'opus'}

    def tearDown(self):
        self.service.close()
        self.studio.shutdown_admission()
        for event in self.studio.cancels.values():event.set()
        for worker in list(self.studio.threads.values()):
            if worker.is_alive():worker.join(10)
        self.network.stop();self.monitor.stop();self.tmp.cleanup()

    def configure(self, mode='observe'):
        request=copy.deepcopy(self.source_payload);request['mode']=mode
        snapshot=self.service.save_source(request)
        return snapshot['sources'][0]

    def ingest(self, source, key='one'):
        self.service.ingest_custom(source['id'], {'externalId':key,'threadKey':'discussion',
            'title':'项目说明好像过期了','body':'请整理本项目brief.md里的说明。','occurredAt':'2026-11-03T09:00:00+08:00'})
        return self.service.store.batches(source['id'])[0]

    def join_task(self, effect):
        task=self.service.engine.lookup_origin(effect['origin'])
        self.studio.threads[task['sessionId']].join(10)
        return self.studio.load(task['projectId'],task['sessionId'])

    def test_observe_is_zero_model_and_manual_assessment_never_auto_dispatches(self):
        source=self.configure();batch=self.ingest(source)
        self.service.engine.process_due(limit=10)
        state=self.service.snapshot(self.pid)
        self.assertEqual((state['stats']['received'],state['stats']['modelCalls'],state['stats']['inputTokens']), (1,0,0))
        self.service.engine.evaluate(batch['id'],manual=True)
        state=self.service.snapshot(self.pid)
        self.assertEqual((state['stats']['modelCalls'],state['stats']['inputTokens'],state['stats']['outputTokens']), (1,40,10))
        self.assertFalse(state['effects'])
        self.assertFalse(self.studio.list_sessions(self.pid))

    def test_image_model_cannot_be_saved_as_paid_event_evaluator(self):
        payload=copy.deepcopy(self.source_payload);payload.update(mode='review',evaluatorModel='image')
        with self.assertRaisesRegex(ValueError,'文字模型'):self.service.save_source(payload)
        self.assertFalse(self.service.store.sources())
        self.assertEqual(self.client.evaluations,0)

    def test_manual_dispatch_runs_once_in_isolated_project_and_keeps_template(self):
        source=self.configure();batch=self.ingest(source)
        original=self.studio.store.current(self.pid)[0]['revision']
        self.service.engine.evaluate(batch['id'],manual=True)
        effect=self.service.engine.dispatch_batch(batch['id'])
        session=self.join_task(effect)
        self.assertEqual(session['status'],'completed',session.get('error'))
        self.assertNotEqual(session['projectId'],self.pid)
        self.assertEqual(self.studio.store.current(self.pid)[0]['revision'],original)
        run=session['runs'][-1]
        self.assertEqual(run['triggerMandate']['target']['projectId'],self.pid)
        self.assertEqual(run['taskContracts']['brief']['triggerMandateFingerprint'],run['triggerMandate']['fingerprint'])
        before=len(self.client.calls)
        repeated=self.service.engine.dispatch_batch(batch['id'])
        self.assertEqual(repeated['task']['sessionId'],session['id'])
        self.assertEqual(len(self.client.calls),before)

    def test_auto_without_mention_reaches_normal_quality_pipeline(self):
        source=self.configure('auto');batch=self.ingest(source)
        self.service.engine.process_due(limit=1)
        effect=self.service.store.effects()[0]
        session=self.join_task(effect)
        self.assertEqual(session['status'],'completed',session.get('error'))
        self.assertTrue(session['runs'][-1]['qualityHistory'][-1]['passed'])
        self.assertEqual(self.client.evaluations,1)

    def test_changed_source_during_model_reply_discards_decision_without_task(self):
        source=self.configure('auto');batch=self.ingest(source)
        def change():
            replacement=copy.deepcopy(source);replacement['enabled']=False
            self.service.save_source(replacement)
        self.client.on_evaluation=change
        outcome=self.service.engine.evaluate(batch['id'],manual=False)
        self.assertEqual(outcome['status'],'discarded')
        self.assertFalse(self.service.store.effects())

    def test_planner_cannot_expand_trigger_scope(self):
        source=self.configure('auto');batch=self.ingest(source)
        def outside(stage, result):
            if stage=='plan':result['modules'][0]['scope']=['outside.md']
            return result
        self.client.mutate=outside
        self.service.engine.process_due(limit=1)
        effect=self.service.store.effects()[0];session=self.join_task(effect)
        self.assertEqual(session['status'],'failed')
        self.assertIn('超出来源',session['error'])
        self.assertFalse(any(call['key'][0]=='build' for call in self.client.calls))

    def test_loss_after_start_is_reconciled_without_a_second_task(self):
        source=self.configure();batch=self.ingest(source)
        self.service.engine.evaluate(batch['id'],manual=True)
        callback=self.service.engine.dispatch_callback
        def lose(effect,cancel):
            callback(effect,cancel)
            raise OSError('fixture lost return after local start')
        self.service.engine.dispatch_callback=lose
        with self.assertRaises(OSError):self.service.engine.dispatch_batch(batch['id'])
        effect=self.service.store.effects()[0]
        session=self.join_task(effect)
        self.assertEqual(session['status'],'completed',session.get('error'))
        before=len(self.client.calls)
        result=self.service.engine.reconcile(effect['id'])[0]
        self.assertEqual(result['status'],'reconciled')
        self.assertEqual(len(self.client.calls),before)
        self.assertEqual(len(self.studio.list_sessions()),1)

    def test_active_followup_reaches_next_model_context_without_new_task_or_authority(self):
        source=self.configure('auto');self.ingest(source)
        entered,release=threading.Event(),threading.Event()
        def pause(stage,result):
            if stage=='build':
                entered.set()
                if not release.wait(5):raise AssertionError('fixture release timed out')
            return result
        self.client.mutate=pause
        try:
            self.service.engine.process_due(limit=1)
            self.assertTrue(entered.wait(5))
            effect=self.service.store.effects()[0]
            self.ingest(source,'followup')
            self.assertEqual(len(self.service.store.effects()),1)
            release.set();session=self.join_task(effect)
            self.assertEqual(session['status'],'completed',session.get('error'))
            final=next(c for c in self.client.calls if c['key'][0]=='integration')
            self.assertEqual(len(final['memory']['triggerInputs']['events']),1)
            self.assertEqual(session['runs'][-1]['taskContracts']['brief']['scope'],['brief.md'])
            self.assertEqual(self.client.evaluations,1)
        finally:release.set()


if __name__=='__main__':unittest.main()
