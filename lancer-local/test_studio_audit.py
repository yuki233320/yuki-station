import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from app import Store
from studio import Studio
from studio_client import StudioClient
from studio_audit import DIMS,load_sources,validate_refs,validate_scores
from model_catalog import MODELS

class AuditFixture(StudioClient):
 """Deterministic contract fixture, never a real model receipt."""
 def __init__(self):super().__init__();self.calls=[];self.mutate=None
 def available(self):return list(MODELS)
 def call(self,model,messages,cancel,on_delta=None):
  prompt=next(m['content'] for m in reversed(messages) if '本轮角色任务：\n' in m['content'])
  memory=json.loads(prompt.split('：\n',1)[1].split('\n本轮角色任务：\n',1)[0]);s=memory['sharedEvidence']
  phase='draft' if 'architecture' in s else 'build' if 'module' in s else 'integration' if 'reports' in s else 'decision' if 'evidence' in s else 'review'
  with self.lock:self.calls.append({'phase':phase,'model':model,'memory':copy.deepcopy(memory),'messages':copy.deepcopy(messages)})
  if phase=='draft':
   result={'summary':'两个模块，三轮只读审查','areas':[dict(id=a['id'],title=a['title'],focus='核对调用边界',primary='sol',secondary='codex',files=a['priorityFiles']) for a in s['architecture']['areas']]}
  elif phase in ('review','decision'):
   src=next(f for f in memory['project']['files'] if f['path'].endswith('.ts'));refs=[{'path':src['path'],'line':1,'endLine':2}]
   scores={d:{'score':7,'reason':'静态证据示例','evidenceRefs':copy.deepcopy(refs)} for d in DIMS}
   result={'summary':'模块证据已检查','scores':scores,'limitations':['未运行被审查程序']}
   if phase=='review':
    result['findings']=[{'claim':'测试示例问题','priority':'P2','complexity':2,'evidence':'函数有明确输入','evidenceRefs':refs,'proposal':'增加边界验证'}]
    result['responses']=[{'findingId':f['id'],'verdict':'confirm','reason':'已核对上轮引用','evidenceRefs':refs} for f in (s['previousRounds'][-1]['findings'] if s['previousRounds'] else [])]
   else:
    result['decisions']=[{'findingId':f['id'],'decision':'accept','reason':'只读证据支持建议'} for f in s['evidence']]
    result['recommendations']=[{'id':'bounded-input','priority':'P2','complexity':2,'action':'建议增加边界验证','why':'降低错误输入风险','acceptance':'未来运行对应测试','evidenceRefs':refs}]
  elif phase=='build':result={'summary':'整理既定裁决','files':[{'path':s['module']['scope'][0],'content':'# 审查建议\n评分来自主模型；尚未实施。'}]}
  else:result={'summary':'审查完成，原源码未修改','accepted':True,'files':[{'path':'README.md','content':'# 审查总评\n仅建议。'}],'risks':['未运行被审查系统'],'nextSteps':['由用户确定后续实施']}
  if self.mutate:result=self.mutate(phase,result)
  return result,{'mode':'test-fixture','requestedModel':model,'returnedModel':model,'usage':{}}

class AuditTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name),seed=False);self.client=AuditFixture();self.studio=Studio(self.store,self.client)
  self.pid=self.studio.create_project('只读审查','仅建议','被审查源码不得修改')['id']
  self.areas=[{'id':a,'title':a,'prefixes':[a+'/'],'priorityFiles':[a+'/index.ts']} for a in ('api','web')]
  self.original={f'source/{a}/index.ts':b'export function run(input) {\n  return input;\n}\n' for a in ('api','web')}
  def prepare(state,w,old):
   for name,data in self.original.items():p=w/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
   base={'audit-brief.md':'Only review.','architecture-map.json':json.dumps({'areas':self.areas}),'source-origin.json':json.dumps({'repository':'fixture/private','commit':'pinned','verification':'test only'}),'deployment-evidence.json':'{}','score-rubric.json':'{}','source-manifest.json':json.dumps({'files':[{'path':p[7:],'projectPath':p,'sha256':hashlib.sha256(data).hexdigest()} for p,data in self.original.items()]})}
   for name,data in base.items():(w/name).write_text(data)
   return {'detail':'test fixture'}
  self.store.commit(self.pid,'test input','test',prepare)
 def tearDown(self):
  for t in self.studio.threads.values():t.join(10)
  self.tmp.cleanup()
 def run_audit(self):
  s=self.studio.create_session(self.pid,'只读审查','审查，不修改','audit');self.studio.start(self.pid,s['id']);self.studio.threads[s['id']].join(10)
  self.assertFalse(self.studio.threads[s['id']].is_alive());return self.studio.load(self.pid,s['id'])
 def test_three_rounds_cited_scores_independent_context_and_readonly_delivery(self):
  s=self.run_audit();r=s['runs'][-1];self.assertEqual(s['status'],'completed',s.get('error'));self.assertEqual(r['completedRounds'],3)
  self.assertEqual(len([n for n in r['nodes'] if n['phase']=='review']),6)
  self.assertEqual({n['round'] for n in r['nodes'] if n['phase']=='review'},{1,2,3})
  self.assertTrue(any(c['model']=='codex' for c in self.client.calls));self.assertEqual({c['model'] for c in self.client.calls if c['phase']=='build'},{'deepseek'})
  w=self.store.current(self.pid)[1]
  for p,data in self.original.items():self.assertEqual((w/p).read_bytes(),data)
  self.assertTrue((w/'index.html').exists());self.assertTrue((w/'reports/audit.json').exists())
  first=next(c for c in self.client.calls if c['phase']=='review');self.assertEqual(len(first['messages']),1)
  third=next(c for c in self.client.calls if c['phase']=='review' and len(c['memory']['sharedEvidence']['previousRounds'])==2)
  self.assertEqual(len(third['messages']),3)
  self.assertEqual(len([f for f in third['memory']['project']['files'] if f['path'].endswith('.ts')]),1)
  self.assertTrue(all(not any(f['path'].endswith('.ts') for f in c['memory']['project']['files']) for c in self.client.calls if c['phase']=='build'))
  final=next(c for c in self.client.calls if c['phase']=='integration');self.assertEqual(len(final['memory']['sharedEvidence']['reports']),2)
  for m in s['messages']:
   if m.get('phase')=='review':self.assertTrue(all(f['id'].startswith(str(m['round'])+'-') for f in m['result']['findings']))
 def test_no_source_overwrite_from_writer(self):
  self.client.mutate=lambda phase,r:dict(r,files=[{'path':'source/api/index.ts','content':'replaced'}]) if phase=='build' else r
  s=self.run_audit();self.assertEqual(s['status'],'failed');w=self.store.current(self.pid)[1]
  self.assertEqual((w/'source/api/index.ts').read_bytes(),self.original['source/api/index.ts']);self.assertFalse((w/'reports/audit.json').exists())
 def test_integration_cannot_add_package_or_source_files(self):
  self.client.mutate=lambda phase,r:dict(r,files=[{'path':'package.json','content':'{}'}]) if phase=='integration' else r
  s=self.run_audit();self.assertEqual(s['status'],'failed');self.assertFalse((self.store.current(self.pid)[1]/'package.json').exists())
 def test_cross_review_requires_response_to_each_previous_finding(self):
  self.client.mutate=lambda phase,r:dict(r,responses=[]) if phase=='review' else r
  s=self.run_audit();self.assertEqual(s['status'],'failed');self.assertLess(s['runs'][-1]['completedRounds'],3)
 def test_missing_source_citation_or_out_of_range_line_rejected(self):
  rows=[{'path':'a.ts','endLine':3}]
  for refs in ([],[{'path':'other.ts','line':1}],[{'path':'a.ts','line':4}],[{'path':'a.ts','line':True}]):
   with self.assertRaises(ValueError):validate_refs(refs,rows)
  score={d:{'score':None,'reason':'未加载足够证据','evidenceRefs':[]} for d in DIMS};validate_scores(score,rows)
  score['architecture']['score']=8
  with self.assertRaises(ValueError):validate_scores(score,rows)
 def test_source_manifest_hash_must_match(self):
  state,w=self.store.current(self.pid);(w/'source/api/index.ts').write_text('changed')
  with self.assertRaises(ValueError):load_sources(self.studio,{'projectId':self.pid},{'projectRevision':state['revision']},{'files':['api/index.ts']},self.areas[0])
 def test_loaded_foundations_and_exact_project_path_aliases_are_valid_evidence(self):
  def update(phase,r):
   if phase=='review':
    r['scores']['delivery']['evidenceRefs']=[{'path':'deployment-evidence.json','line':1}]
    r['findings'][0]['evidenceRefs']=[{'path':'source/'+r['findings'][0]['evidenceRefs'][0]['path'],'line':1}]
   return r
  self.client.mutate=update;s=self.run_audit();self.assertEqual(s['status'],'completed',s.get('error'))
  self.assertFalse(s['runs'][0]['nodes'][1]['result']['findings'][0]['evidenceRefs'][0]['path'].startswith('source/'))
 def test_bounded_correction_keeps_receipts_and_correct_context(self):
  bad=[True]
  def update(phase,r):
   if phase=='review' and bad[0]:bad[0]=False;r['scores']['architecture']['evidenceRefs'][0]['line']=999
   return r
  self.client.mutate=update;s=self.run_audit();self.assertEqual(s['status'],'completed',s.get('error'))
  n=next(n for n in s['runs'][0]['nodes'] if len(n.get('attempts',[]))==2)
  self.assertIsNotNone(n['attempts'][0]['validationError']);self.assertIsNone(n['attempts'][1]['validationError'])
  message=next(m for m in s['messages'] if m.get('node')==n['id']);self.assertEqual(message['contextRef'],n['contextRef'])
  context=self.studio.context(self.pid,s['id'],n['contextRef']);self.assertEqual(context['validationRepair'],1)
  self.assertEqual(s['runs'][0]['completedRounds'],3)
 def test_opus_reassigns_failed_provider_and_keeps_completed_review(self):
  first=[True];original=self.client.call
  def update(phase,r):
   if phase=='draft' and first[0]:first[0]=False;r['areas'][0]['primary']='gemini'
   return r
  self.client.mutate=update
  def provider(model,messages,cancel,on_delta=None):
   if model=='gemini':raise ValueError('provider unavailable')
   return original(model,messages,cancel,on_delta)
  self.client.call=provider;s=self.run_audit();self.assertEqual(s['status'],'failed')
  previous=next(n for n in s['runs'][0]['nodes'] if n['id']=='review-1-web')['contextRef']
  self.studio.start(self.pid,s['id'],'live',True);self.studio.threads[s['id']].join(10);s=self.studio.load(self.pid,s['id']);r=s['runs'][0]
  self.assertEqual(s['status'],'completed',s.get('error'));self.assertEqual(r['completedRounds'],3)
  self.assertTrue(any(n['id']=='routing-recovery' and n['status']=='done' for n in r['nodes']))
  self.assertEqual(next(n for n in r['nodes'] if n['id']=='review-1-web')['contextRef'],previous)
  self.assertEqual(next(n for n in r['nodes'] if n['id']=='review-1-api')['agent'],'sol')
 def test_malformed_json_is_repaired_with_receipt_without_extra_review_round(self):
  from studio_client import ModelResponseError
  original=self.client.call;broken=[True]
  def provider(model,messages,cancel,on_delta=None):
   if model=='sol' and broken[0]:
    broken[0]=False;raise ModelResponseError('invalid JSON',{'mode':'test-fixture','returnedModel':'sol','finishReason':'response.completed'},'{"summary":"missing comma" "findings":[]}')
   return original(model,messages,cancel,on_delta)
  self.client.call=provider;s=self.run_audit();self.assertEqual(s['status'],'completed',s.get('error'))
  node=next(n for n in s['runs'][0]['nodes'] if any(a.get('validationError')=='invalid JSON' for a in n.get('attempts',[])))
  context=self.studio.context(self.pid,s['id'],node['contextRef']);self.assertEqual(context['formatRepair'],1);self.assertEqual(s['runs'][0]['completedRounds'],3)

if __name__=='__main__':unittest.main()
