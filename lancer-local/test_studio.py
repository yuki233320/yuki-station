import base64,copy,io,json,re,tempfile,threading,unittest,zipfile
from pathlib import Path
from app import Store,Conflict
from studio import Studio,classify,validate_plan,clean_path
from studio_client import StudioClient
from studio_demo import BRIEF,respond,modules
from model_catalog import MODELS,rank_models,reviewers


class FixtureClient(StudioClient):
 """Provider contract fixture; never contacts an external service."""
 def __init__(self):
  super().__init__();self.calls=[];self.fail_once=None;self.before_integrate=None;self.mutate_result=None
 def available(self):return list(MODELS)
 def call(self,model,messages,cancel,on_delta=None):
  content=messages[-1]['content'];memory=json.loads(content.split('：\n',1)[1].split('\n本轮角色任务：\n',1)[0]);instruction=content.split('\n本轮角色任务：\n',1)[1];shared=memory['sharedEvidence']
  phase='build' if 'module' in shared else 'integration' if 'checks' in shared else 'decision' if 'evidence' in shared else 'review' if 'previousRounds' in shared else 'draft'
  number=int(re.search(r'第 (\d) 轮',instruction).group(1)) if phase=='review' else None
  node={'phase':phase,'agent':model,'round':number};key=(phase,model,number,shared.get('module',{}).get('id'))
  with self.lock:
   self.calls.append({'key':key,'messages':copy.deepcopy(messages),'memory':memory})
   if self.fail_once==key:self.fail_once=None;raise ValueError('fixture interrupted')
  if phase=='integration' and self.before_integrate:
   action=self.before_integrate;self.before_integrate=None;action()
  result=respond(node,instruction,shared)
  if self.mutate_result:result=self.mutate_result(phase,result)
  return result,{'mode':'fixture','requestedModel':model,'returnedModel':model,'usage':{}}


class ProjectTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name),seed=False);self.client=FixtureClient();self.studio=Studio(self.store,self.client)
  self.pid=self.studio.create_project('测试项目','验证持久记忆','只修改任务声明范围；交付须保留数据。')['id']
 def tearDown(self):
  for t in self.studio.threads.values():t.join(10)
  self.tmp.cleanup()
 def task(self,text=BRIEF,kind='auto'):
  # These fixtures preserve the v1 execution/replay contract for saved runs.
  # Default v2 business routing has its own mixed-task and acceptance suite.
  s=self.studio.create_session(self.pid,'测试会话',text,kind)
  s['workflowVersion']=1;self.studio.save(s);return s
 def run_task(self,s,resume=False):
  self.studio.start(self.pid,s['id'],'live',resume);self.studio.threads[s['id']].join(10)
  self.assertFalse(self.studio.threads[s['id']].is_alive());return self.studio.load(self.pid,s['id'])
 def test_delivery_classification_does_not_review_incidental_code(self):
  for text in ['制作一个网站','开发一个客户服务agent','创建手机应用','新增登录功能','修复接口错误','开发一个分析数据的网站']:
   self.assertEqual(classify(text)['kind'],'development',text)
  for text in ['用 Python 清洗数据并统计收入','解释这段网站代码','分析代码里的算法','总结智能体原理','不需要开发网站，只比较工具']:
   self.assertEqual(classify(text)['kind'],'general',text)
  self.assertEqual(classify('制作网站','general')['kind'],'general')
 def test_three_rounds_finite_independent_contexts_and_delivery(self):
  s=self.run_task(self.task());r=s['runs'][-1]
  self.assertEqual(s['status'],'completed',s.get('error'));self.assertEqual(r['completedRounds'],3)
  self.assertEqual(len([n for n in r['nodes'] if n['phase']=='review']),9)
  self.assertEqual({n['round'] for n in r['nodes'] if n['phase']=='review'},{1,2,3})
  self.assertEqual(len(r['plan']['decisions']),len(r['evidence']));self.assertEqual(len(s['agents']),7)
  self.assertEqual({m['assignment']['model'] for m in r['plan']['modules']},{'deepseek','gemini','sol'})
  c=next(c for c in self.client.calls if c['key'][:3]==('review','sol',2))
  self.assertEqual(len(c['messages']),3);self.assertEqual(len(c['memory']['sharedEvidence']['previousRounds']),1)
  self.assertIn('稳定任务ID',c['messages'][1]['content']);self.assertNotIn('可访问标签',c['messages'][1]['content'])
  self.assertEqual(len(r['artifacts']),6);self.assertTrue((self.store.current(self.pid)[1]/'web/app.js').exists())
 def test_general_task_never_enters_review(self):
  def general(phase,r):
   if phase=='decision':r['modules']=[{'id':'notes','title':'摘要','priority':'P1','complexity':1,'scope':['notes.md'],'dependsOn':[],'skills':{'writing':1},'instructions':'写摘要','acceptance':['完整摘要']}]
   if phase=='build':return {'summary':'整理完成','files':[{'path':'notes.md','content':'整理的公开资料'}]}
   return r
  original=self.client.call
  # The static fixture understands development module ids; a generic worker is patched here.
  def call(model,messages,cancel,on_delta=None):
   if '"id": "notes"' in messages[-1]['content'] and '"module":' in messages[-1]['content']:
    return {'summary':'整理完成','files':[{'path':'notes.md','content':'整理的公开资料'}]},{'mode':'fixture'}
   return original(model,messages,cancel,on_delta)
  self.client.mutate_result=general;self.client.call=call
  s=self.run_task(self.task('整理公开资料，输出一份摘要','general'));self.assertEqual(s['status'],'completed',s.get('error'))
  self.assertEqual(s['runs'][0]['completedRounds'],0);self.assertFalse(any(n['phase']=='review' for n in s['runs'][0]['nodes']))
 def test_project_memory_crosses_sessions_but_agents_do_not(self):
  first=self.run_task(self.task());second=self.task('解释已有需求看板的数据契约','general')
  rev=self.store.current(self.pid)[0]['revision'];context,_=self.studio.assemble_context(second,{'id':'inspect','request':'数据契约','projectRevision':rev},'coordinator','opus','解释数据契约',{})
  self.assertEqual(len(context['memory']['projectMemory']),1)
  self.assertTrue(any(f['path']=='web/app.js' for f in context['memory']['project']['files']))
  self.assertEqual(context['memoryPolicy']['ownTurnsIncluded'],0);self.assertNotEqual(first['id'],second['id'])
  follow=self.studio.add_message(self.pid,first['id'],'修改按钮配色',first['version'])
  self.assertEqual(follow['classification']['kind'],'development');self.assertEqual(len(follow['agents']),7)
 def test_imports_are_versioned_and_secrets_excluded(self):
  revision=self.store.current(self.pid)[0]['revision'];data=base64.b64encode(b'contract version 2').decode()
  p=self.studio.import_files(self.pid,[{'path':'docs/contract.md','data':data}],revision)
  self.assertNotEqual(p['revision'],revision)
  with self.assertRaises(Conflict):self.studio.import_files(self.pid,[{'path':'docs/next.md','data':data}],revision)
  with self.assertRaises(ValueError):self.studio.import_files(self.pid,[{'path':'secrets.txt','data':base64.b64encode(b'sk-' + b'x'*30).decode()}],p['revision'])
  for path in ['../escape','/abs/path','.env','.ssh/private','src/../../escape','x.pem','node_modules/a.js']:
   with self.assertRaises(ValueError,msg=path):clean_path(path)
 def test_scope_collision_cycle_and_binary_reference_are_rejected(self):
  m=modules();m[1]['scope']=['web/'];
  with self.assertRaises(ValueError):validate_plan({'modules':m},list(MODELS))
  m=modules();m[0]['dependsOn']=['interaction'];
  with self.assertRaises(ValueError):validate_plan({'modules':m},list(MODELS))
  with self.assertRaises(ValueError):self.studio.validate_files({'files':[{'path':'web/a.js','binaryRef':'../../private'}]},['web/'])
  with self.assertRaises(ValueError):self.studio.validate_files({'files':[{'path':'other/a.js','content':'x'}]},['web/'])
 def test_failed_worker_resume_does_not_repeat_completed_requests(self):
  self.client.fail_once=('build','sol',None,'interaction');s=self.run_task(self.task());self.assertEqual(s['status'],'failed')
  count=len([c for c in self.client.calls if c['key'][0]=='review']);self.assertEqual(count,9)
  s=self.run_task(s,True);self.assertEqual(s['status'],'completed',s.get('error'))
  self.assertEqual(len([c for c in self.client.calls if c['key'][0]=='review']),9)
  self.assertEqual(len([c for c in self.client.calls if c['key']==('build','sol',None,'interaction')]),2)
 def test_concurrent_project_update_preserves_new_files_and_candidate(self):
  def update():
   self.store.commit(self.pid,'concurrent','test',lambda s,w,old:(w/'external.md').write_text('external update') and {'detail':'external'})
  self.client.before_integrate=update;s=self.run_task(self.task())
  self.assertEqual(s['status'],'conflict');w=self.store.current(self.pid)[1]
  self.assertEqual((w/'external.md').read_text(),'external update');self.assertFalse((w/'web/app.js').exists())
  self.assertEqual(len(s['runs'][0]['artifacts']),6)
  with self.assertRaises(ValueError):self.studio.start(self.pid,s['id'],'live',True)
 def test_invalid_review_or_decision_never_marks_node_done(self):
  self.client.mutate_result=lambda phase,r:dict(r,decisions=[]) if phase=='decision' else r
  s=self.run_task(self.task());self.assertEqual(s['status'],'failed')
  n=next(n for n in s['runs'][0]['nodes'] if n['id']=='decision');self.assertEqual(n['status'],'failed')
  self.assertFalse(s['runs'][0]['artifacts']);self.assertNotIn('web/app.js',[f['path'] for f in self.studio.project(self.pid)['files']])
 def test_cancel_resume_and_restart_recovery(self):
  entered=threading.Event();original=self.client.call
  def slow(model,messages,cancel,on_delta=None):
   entered.set()
   if cancel.wait(2):
    from live import Cancelled
    raise Cancelled()
   return original(model,messages,cancel,on_delta)
  self.client.call=slow;s=self.task();self.studio.start(self.pid,s['id']);self.assertTrue(entered.wait(2));self.studio.stop(self.pid,s['id']);self.studio.threads[s['id']].join(3)
  s=self.studio.load(self.pid,s['id']);self.assertEqual(s['status'],'stopped');self.client.call=original
  s=self.run_task(s,True);self.assertEqual(s['status'],'completed',s.get('error'))
  # Simulate a process dying after a durable running snapshot.
  s['status']='running';s['runs'][-1]['status']='running';self.studio.save(s)
  restored=Studio(self.store,self.client).load(self.pid,s['id']);self.assertEqual(restored['status'],'interrupted')
 def test_routing_and_export_do_not_invent_model_identity_or_leak_keys(self):
  self.assertNotIn('codex',[r['model'] for r in rank_models({'coding':1})]);self.assertEqual(rank_models({'image':1})[0]['model'],'image')
  self.assertNotIn('image',[r['model'] for r in rank_models({'coding':1})]);self.assertNotIn('opus',reviewers())
  with self.assertRaises(ValueError):rank_models({'image':.5,'coding':.5})
  image_module=copy.deepcopy(modules()[0]);image_module['skills']={'image':1};image_module['scope']=['web/app.js']
  with self.assertRaises(ValueError):validate_plan({'modules':[image_module]},list(MODELS))
  with self.assertRaises(ValueError):reviewers(['opus','sol'])
  secret='sk-'+('private-fixture-'*3);self.client.configure('opus',secret);s=self.run_task(self.task())
  exported=self.studio.export(self.pid,s['id'])
  with zipfile.ZipFile(io.BytesIO(exported)) as z:
   self.assertTrue(any(n.startswith('contexts/') for n in z.namelist()))
   for n in z.namelist():self.assertNotIn(secret.encode(),z.read(n))
  self.assertNotIn(secret,json.dumps(self.studio.index()));self.client.forget();self.assertFalse(self.client.credentials)
 def test_custom_task_cannot_be_silently_replaced_by_demo(self):
  s=self.task('制作一个真正的网站')
  with self.assertRaises(ValueError):self.studio.start(self.pid,s['id'],'demo')


class ProviderTests(unittest.TestCase):
 def invoke(self,model,events,**kwargs):
  client=StudioClient();client.configure('deepseek' if model=='deepseek' else 'openai' if model=='sol' else 'opus','test-key-not-a-real-secret')
  class Opener:
   def open(self,request,timeout):
    self.payload=json.loads(request.data);return io.BytesIO(b''.join(b'data: '+json.dumps(e).encode()+b'\n\n' for e in events)+b'data: [DONE]\n\n')
  client.opener=Opener();result=client.call(model,[{'role':'user','content':'Return JSON'}],threading.Event(),**kwargs);return result,client.opener.payload
 def test_controlled_output_allowance_reaches_protocol_payload(self):
  chat=[{'model':'deepseek-flash','choices':[{'delta':{'content':'{"summary":"ok"}'},'finish_reason':'stop'}]}]
  responses=[{'type':'response.output_text.delta','delta':'{"summary":"ok"}'},{'type':'response.completed','response':{}}]
  anthropic=[{'type':'content_block_delta','delta':{'type':'text_delta','text':'{"summary":"ok"}'}},{'type':'message_delta','delta':{'stop_reason':'end_turn'}},{'type':'message_stop'}]
  for model,events,field in [('deepseek',chat,'max_tokens'),('sol',responses,'max_output_tokens'),('opus',anthropic,'max_tokens')]:
   with self.subTest(model=model):
    _,payload=self.invoke(model,events,max_output_tokens=2048)
    self.assertEqual(payload[field],2048)
  for invalid in (True,0,-1,2.5,393217):
   with self.assertRaises(ValueError):self.invoke('deepseek',chat,max_output_tokens=invalid)
 def test_responses_requires_terminal_completion_and_uses_no_remote_session(self):
  events=[{'type':'response.output_text.delta','delta':'{"summary":"ok"}'},{'type':'response.completed','response':{'model':'gpt-6.1-sol','usage':{'output_tokens':5}}}]
  (result,receipt),payload=self.invoke('sol',events);self.assertEqual(result['summary'],'ok');self.assertFalse(payload['store']);self.assertNotIn('previous_response_id',payload)
  self.assertEqual(receipt['returnedModel'],'gpt-6.1-sol')
  events[-1]['type']='response.incomplete'
  with self.assertRaises(ValueError):self.invoke('sol',events)
 def test_chat_rejects_truncated_output(self):
  events=[{'model':'claude-opus-5-5','choices':[{'delta':{'content':'{"summary":"ok"}'},'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':'stop'}]}]
  (result,_),payload=self.invoke('gemini',events);self.assertEqual(result['summary'],'ok');self.assertEqual(payload['messages'][0]['role'],'system')
  events[-1]['choices'][0]['finish_reason']='length'
  with self.assertRaises(ValueError):self.invoke('gemini',events)
 def test_native_claude_requires_message_stop_and_preserves_usage(self):
  events=[{'type':'message_start','message':{'model':'claude-opus-5-5','usage':{'input_tokens':10}}},{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'private intermediate'}},{'type':'content_block_delta','delta':{'type':'text_delta','text':'{"summary":"ok"}'}},{'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':5}},{'type':'message_stop'}]
  (result,receipt),payload=self.invoke('opus',events)
  self.assertEqual(result,{'summary':'ok'});self.assertEqual(receipt['usage'],{'input_tokens':10,'output_tokens':5})
  self.assertEqual(payload['output_config'],{'effort':'medium'});self.assertIn('system',payload)
  with self.assertRaises(ValueError):self.invoke('opus',events[:-1])
  events[-2]['delta']['stop_reason']='max_tokens'
  with self.assertRaises(ValueError):self.invoke('opus',events)
 def test_deepseek_budget_keeps_reasoning_out_of_answer(self):
  events=[{'choices':[{'delta':{'reasoning_content':'private intermediate'},'finish_reason':None}]},{'model':'deepseek-flash','choices':[{'delta':{'content':'{"summary":"ok"}'},'finish_reason':'stop'}]}]
  (result,receipt),payload=self.invoke('deepseek',events)
  self.assertEqual(result,{'summary':'ok'});self.assertEqual(receipt['returnedModel'],'deepseek-flash')
  self.assertEqual(payload['reasoning_effort'],'low');self.assertEqual(payload['max_tokens'],32768)
  self.assertEqual(payload['response_format'],{'type':'json_object'})

if __name__=='__main__':unittest.main()
