import unittest
from business_routing import rank_task_models,assign_modules

def module(**overrides):
 return dict(id='report',title='业务说明',priority='P1',complexity=2,workType='writing',skills={'writing':1},**overrides)

class RoutingTests(unittest.TestCase):
 def test_native_executor_requires_explicit_tools(self):
  self.assertEqual(rank_task_models(module(),['sol','native_codex'])[0]['model'],'sol')
  for required in (['terminal'],['files'],['files','terminal']):
   result=rank_task_models(module(requiredCapabilities=required),['opus','sol','native_codex'])[0]
   self.assertEqual(result['model'],'native_codex')
   self.assertIn('terminal',result['capabilities'])
   with self.assertRaisesRegex(ValueError,'未接通'):rank_task_models(module(requiredCapabilities=required),['opus','sol'])
 def test_tool_availability_is_a_hard_boundary(self):
  with self.assertRaisesRegex(ValueError,'未接通'):
   rank_task_models(module(requiredCapabilities=['browser']),['opus','sol'])
 def test_coordinator_does_not_take_work_when_peer_can(self):
  self.assertEqual(rank_task_models(module(),['opus','sol'])[0]['model'],'sol')
  self.assertEqual(rank_task_models(module(),['opus'])[0]['model'],'opus')
  self.assertEqual(rank_task_models(module(),['opus','gemini'],coordinator='gemini')[0]['model'],'opus')
 def test_web_search_requires_runtime_capability_and_native_executor(self):
  task=module(requiredCapabilities=['text','web_search'])
  with self.assertRaisesRegex(ValueError,'未接通'):
   rank_task_models(task,['opus','native_codex'],native_capabilities=['files','terminal'])
  result=rank_task_models(task,['opus','native_codex'],native_capabilities=['files','terminal','web_search'])[0]
  self.assertEqual(result['model'],'native_codex')
  self.assertIn('web_search',result['capabilities'])
 def test_unverified_alias_cannot_win_by_label(self):
  self.assertEqual(rank_task_models(module(),['codex','sol'])[0]['model'],'sol')
 def test_history_is_small_labelled_bonus_and_requires_independent_sessions(self):
  rows=[{'model':'sol','workType':'writing','accepted':True,'source':{'session':'one'}}]*8
  self.assertEqual(rank_task_models(module(),['sol'],rows)[0]['components']['projectExperienceBonus'],0)
  rows=[dict(row,source={'session':str(i)}) for i,row in enumerate(rows)]
  result=rank_task_models(module(),['sol'],rows)[0]
  self.assertGreater(result['components']['projectExperienceBonus'],0)
  self.assertLessEqual(result['components']['projectExperienceBonus'],2)
  self.assertIn('不代表独立测试通过',result['evidenceNotice'])
  for row in rows:row['accepted']=False
  self.assertEqual(rank_task_models(module(),['sol'],rows)[0]['components']['acceptedSessions'],0)
 def test_assignment_contains_explanation_and_alternatives(self):
  modules=[module()];assign_modules(modules,['opus','sol','gemini'])
  self.assertIn('reason',modules[0]['assignment']);self.assertTrue(modules[0]['alternatives'])

if __name__=='__main__':unittest.main()
