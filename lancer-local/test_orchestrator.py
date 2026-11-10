import copy,json,tempfile,unittest
from pathlib import Path
from app import Store
from orchestrator import Orchestrator, BUSINESS, LANE_META, catalog_nodes,validate_plan,business_checks,parse_json,normalize_result

def plan():
 return {'summary':'规划测试','lanes':copy.deepcopy(LANE_META),'nodes':[{'id':n['id'],'agent':n['agent'],'lane':n['laneId'],'deps':n['deps'],'slot':n['slot'],'branchOf':n['branchOf'],'approach':n['intent']} for n in catalog_nodes()],'reserves':[]}
def option(which):
 c=copy.deepcopy(BUSINESS['baselineCosts'])
 if which=='rain_a':c['marketing']=1000;return {'visitorsPerSession':60,'sessionsPerDay':3,'booths':12,'staff':12,'costs':c}
 c.update(booths=2400,staff=3000)
 return {'visitorsPerSession':90,'sessionsPerDay':2,'booths':8,'staff':10,'costs':c}

class OrchestrationTests(unittest.TestCase):
 def test_gateway_suffix_preserves_object_but_rejects_extra_result(self):
  self.assertEqual(parse_json('{"summary":"完整"}"}'),{"summary":"完整"})
  with self.assertRaises(ValueError):parse_json('{"summary":"one"}{"summary":"two"}')

 def test_nested_control_fields_keep_real_review_decision(self):
  self.assertTrue(normalize_result({"metrics":{"needsChanges":True}})["needsChanges"])
  self.assertFalse(normalize_result({"metrics":{"accepted":False}})["accepted"])
  with self.assertRaises(ValueError):normalize_result({"accepted":"true"})

 def test_plan_rejects_cycles_and_missing_inputs(self):
  p=plan();p['nodes'][0]['deps']=['package']
  with self.assertRaisesRegex(ValueError,'循环'):validate_plan(p)
  p=plan();p['nodes'][6]['deps']=['rain_a']
  with self.assertRaisesRegex(ValueError,'遗漏'):validate_plan(p)
 def test_options_count_every_person_and_cost(self):
  self.assertEqual([r['actual'] for r in business_checks(option('rain_a'))],[96,180,17800.0])
  self.assertEqual([r['actual'] for r in business_checks(option('rain_b'))],[116,180,17400.0])
  p=option('rain_a');p['visitorsPerSession']=100
  self.assertFalse(business_checks(p)[0]['pass'])
  del p['costs']['rain']
  with self.assertRaises(ValueError):business_checks(p)
 def test_complete_dag_handoffs_fork_merge_and_saved_review(self):
  with tempfile.TemporaryDirectory() as d:
   creds={a:{'base':base,'key':'test-memory-key'} for a,base in [('opus','https://grooroute.pro/v1'),('openai','https://api.bang.wiki/v1'),('deepseek','https://api.deepseek.com')]}
   m=Orchestrator(Store(Path(d),seed=False),creds)
   acceptance_calls=[]
   def respond(n,prompt):
    if n['id']=='plan':return json.dumps(plan())
    if n['id'] in ('prototype','revision','final_copy'):return '<html><body>合成演示 员工<img src="hero.png">'+''.join('<table><caption>业务表</caption><thead><tr><th scope="col">项目</th></tr></thead><tbody><tr><td>示例</td></tr></tbody></table>' for _ in range(3))+'</body></html>'
    v={'summary':'result','body':'work output'}
    if n['id'] in ('rain_a','rain_b'):v['metrics']=option(n['id'])
    if n['id']=='decision':v.update(chosen='rain_a',metrics=option('rain_a'))
    if n['id']=='review':v.update(needsChanges=True,findings=[{'issue':'示例修订'}])
    if n['id']=='rereview':v['needsChanges']=True
    if n['id']=='final_recheck':v['needsChanges']=False
    if n['id']=='acceptance':
     acceptance_calls.append(1);v['accepted']=len(acceptance_calls)>1;n['text']=json.dumps(v)
    return json.dumps(v)
   m.perform_text=respond;m.image=lambda *a:b'\x89PNG\r\n\x1a\nfixture'
   m.start_scenario();m.thread.join(15)
   self.assertFalse(m.thread.is_alive());self.assertEqual(m.run['status'],'partial')
   self.assertEqual(m.node('acceptance')['status'],'failed')
   m.repair();m.thread.join(15)
   self.assertEqual(m.run['status'],'partial');self.assertEqual(m.node('verification')['status'],'failed')
   m.repair();m.thread.join(15)
   self.assertFalse(m.thread.is_alive());self.assertEqual(m.run['status'],'completed',m.run['events'][-1])
   self.assertEqual(len(m.run['nodes']),19);self.assertEqual(len(m.run['plan']['nodes']),16)
   self.assertEqual(m.node('rereview')['branchOf'],'revision')
   self.assertIn('rereview',m.node('acceptance')['deps'])
   self.assertTrue(m.run['planReady'])
   self.assertEqual({e['node'] for e in m.run['events'] if e['type']=='branch_opened'},{'rain_a','rain_b','revision','rereview','final_copy','final_recheck'})
   self.assertTrue(any(e['type']=='merge' and e['node']=='decision' for e in m.run['events']))
   self.assertTrue(m.node('verification')['result']['passed'])
   _,w=m.store.current(m.run['projectId'])
   self.assertTrue((w/'artifacts/index-before-review.html').is_file())
   self.assertGreater(m.node('verification')['result']['handoffFilesChecked'],20)
   self.assertNotIn('test-memory-key',json.dumps(m.view()))

if __name__=='__main__':unittest.main()
