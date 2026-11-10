import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app import Store
from live import LiveManager, BudgetBlocked, PROFILES, NODES

class Response(io.BytesIO):
    status=200

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name),seed=False)
        self.manager=LiveManager(self.store,{'opus':{'key':'session-only-placeholder','base':'https://grooroute.pro/v1'}})
        self.manager.run={'id':'test','startedAt':0,'events':[],'ledger':[],'reserved':0,'status':'running','nodes':[]}
    def tearDown(self):self.temp.cleanup()
    def test_unpriced_request_never_reserves(self):
        self.manager.config=lambda:{'budget':20,'rates':{}}
        with self.assertRaises(BudgetBlocked):self.manager.reserve({'id':'synthesis','agent':'sol','limit':2000},'x')
        self.assertEqual(self.manager.session_reserved,0)
    def test_atomic_budget_refuses_overspend(self):
        self.manager.config=lambda:{'budget':1,'rates':{'opus':{'input':1000000,'output':1000000}}}
        with self.assertRaises(BudgetBlocked):self.manager.reserve({'id':'brief','agent':'opus','limit':2},'x')
        self.assertEqual(self.manager.run['ledger'],[])
    def test_public_state_and_disk_exclude_credentials(self):
        self.manager.event('connected',detail='Upstream echoed session-only-placeholder')
        self.assertNotIn('session-only-placeholder',json.dumps(self.manager.view()))
        self.assertNotIn('session-only-placeholder',(self.manager.dir/'test.json').read_text())
    def test_missing_demo_config_keeps_status_readable_and_prevents_start(self):
        before=json.dumps(self.manager.run)
        with patch('live.HERE',Path(self.temp.name)):
            self.assertEqual(self.manager.config(),{'budget':0,'rates':{},'limits':{}})
            state=self.manager.view()
            self.assertEqual(state['budget'],0)
            self.assertTrue(all(not profile['priced'] for profile in state['profiles'].values()))
            with self.assertRaisesRegex(ValueError,'预算'):
                self.manager.start()
        self.assertIsNone(self.manager.thread)
        self.assertEqual(json.dumps(self.manager.run),before)
        self.assertEqual(self.store.list_projects(),[])
    def test_chat_stream_requires_complete_finish(self):
        n={'id':'brief','agent':'opus','limit':30,'text':''};self.manager.run['nodes']=[n]
        self.manager.request=lambda *args:Response(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
        with self.assertRaisesRegex(ValueError,'未完整结束'):self.manager.stream_text(n,PROFILES['opus'],'brief')
        self.assertEqual(n['text'],'partial')
    def test_chat_stream_keeps_real_usage(self):
        n={'id':'brief','agent':'opus','limit':30,'text':''};self.manager.run['nodes']=[n]
        self.manager.request=lambda *args:Response(b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: {"usage":{"prompt_tokens":9,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
        self.assertEqual(self.manager.stream_text(n,PROFILES['opus'],'brief'),'done')
        self.assertEqual(n['usage']['completion_tokens'],2)
    def test_response_failure_is_not_completion(self):
        n={'id':'synthesis','agent':'sol','limit':30,'text':''};self.manager.run['nodes']=[n]
        self.manager.request=lambda *args:Response(b'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}\n\n')
        with self.assertRaisesRegex(ValueError,'未完整完成'):self.manager.stream_text(n,PROFILES['sol'],'brief')

    def test_plan_persists_without_rewriting_recorded_events(self):
        self.manager.run['nodes']=[dict(NODES[0])]
        before=json.dumps(self.manager.run)
        plan=self.manager.configure({'version':0,'node':'brief','model':'sol','lane':'release'})
        self.assertEqual(json.dumps(self.manager.run),before)
        self.assertEqual(plan['models']['brief'],'sol')
        self.assertEqual(plan['lanes']['brief'],'release')
        reopened=LiveManager(self.store,{})
        self.assertEqual(reopened.workbench,plan)
        with self.assertRaisesRegex(ValueError,'配置已更新'):
            self.manager.configure({'version':0,'node':'brief','model':'opus'})

    def test_plan_rejects_wrong_model_type(self):
        for node,model in [('visual','sol'),('verify','opus'),('brief','image')]:
            with self.assertRaisesRegex(ValueError,'模型类型'):
                self.manager.configure({'version':0,'node':node,'model':model})
        self.assertFalse(self.manager.workbench_path.exists())

    def test_new_run_uses_plan_without_modifying_dependencies(self):
        self.manager.configure({'version':0,'node':'brief','model':'sol','lane':'release'})
        self.manager.launch=lambda:None
        config={'budget':20,'rates':{},'limits':{}}
        (Path(self.temp.name)/'live_config.json').write_text(json.dumps(config))
        with patch('live.HERE',Path(self.temp.name)):
            self.assertEqual(self.manager.config(),config)
            result=self.manager.start()
        brief=result['run']['nodes'][0]
        self.assertEqual(brief['agent'],'sol')
        self.assertEqual(brief['lane'],3)
        self.assertEqual([n['deps'] for n in result['run']['nodes']],[n['deps'] for n in NODES])
        self.assertEqual(result['run']['ledger'],[])

    def test_local_note_is_versioned_without_changing_stages_or_billing(self):
        pid=self.store.create('Note test','A local note')
        before=self.store.view(pid)
        self.manager.run.update(projectId=pid,status='completed',nodes=[dict(NODES[0])])
        result=self.manager.add_note({'node':'brief','text':'引用产品定义，补充验收细节。','revision':before['revision']})
        note=result['workbench']['notes'][0]
        _,workspace=self.store.current(pid)
        self.assertIn('补充验收细节',(workspace/note['path']).read_text())
        self.assertEqual(result['project']['stages'],before['stages'])
        self.assertNotEqual(result['project']['revision'],before['revision'])
        self.assertEqual(self.manager.run['ledger'],[])
        self.assertEqual(self.manager.run['events'],[])
        with self.assertRaises(ValueError):
            self.manager.add_note({'node':'brief','text':'no revision'})

if __name__=='__main__':unittest.main()
