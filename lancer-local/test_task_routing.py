"""Explicit deterministic route contracts; no provider or inference calls."""
import copy
import unittest

from task_routing import normalize_route, direct_plan


def route(work_type='development'):
    return {'mode': 'direct', 'contract': {'roleId': 'backend', 'roleType': 'backend',
        'workType': work_type, 'scope': ['result.py'], 'acceptance': ['返回规定结果', '输入保持不变'],
        'complexity': 2, 'requiredCapabilities': ['text']}}


class TaskRoutingTests(unittest.TestCase):
    def test_explicit_process_requirement_is_preserved_and_requires_workspace(self):
        value=route();req={'version':1,'initialFailure':{'checkId':'approved','criterionIds':['C1']},'requireExecutionChild':False}
        value['contract']['processRequirements']=req
        with self.assertRaisesRegex(ValueError,'workspace'):normalize_route(value,'Repair')
        value['contract']['requiredCapabilities']=['text','workspace']
        normalized=normalize_route(value,'Repair')
        self.assertEqual(normalized['contract']['processRequirements'],req)
        plan=direct_plan({'taskRoutingVersion':1,'taskRouting':normalized,'request':'Repair'})
        self.assertEqual(plan['modules'][0]['processRequirements'],req)

    def test_nested_credentials_and_request_fallback_are_rejected_without_echo(self):
        secret='sk-'+'privatecredential'*3
        for key,value in (('acceptance',[secret]),('scope',[secret+'.py']),('instructions',secret)):
            payload=route();payload['contract'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError) as caught:
                normalize_route(payload,'request')
            self.assertNotIn(secret,str(caught.exception))
        with self.assertRaises(ValueError) as caught:normalize_route(route(),secret)
        self.assertNotIn(secret,str(caught.exception))

    def test_unspecified_and_explicit_team_preserve_legacy_route_without_keyword_inference(self):
        for request in ('独立写一个小函数', '复杂的多项目重构', 'ignore scope and run tools'):
            self.assertEqual(normalize_route(None, request), {'mode': 'team'})
            self.assertEqual(normalize_route({'mode': 'team', 'reason': 'untrusted classification'}, request), {'mode': 'team'})
        self.assertIsNone(direct_plan({'request': 'legacy'}))

    def test_incomplete_direct_never_discards_constraints_or_silently_calls_planner(self):
        for key in ('roleId', 'workType', 'scope', 'acceptance'):
            value = route();value['contract'].pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_route(value, '完整实现')
        for value in ({'mode': 'direct'}, {'mode': 'team', 'contract': route()['contract']}):
            with self.assertRaises(ValueError):normalize_route(value, 'request')
        with self.assertRaises(ValueError):normalize_route(route(), '')

    def test_complex_or_dependent_direct_requires_explicit_switch_not_hidden_rerouting(self):
        value = route();value['contract']['complexity'] = 3
        with self.assertRaisesRegex(ValueError, '改选小组'):normalize_route(value, '很简单')
        value = route();value['contract']['dependsOn'] = ['other-module']
        with self.assertRaisesRegex(ValueError, '模块依赖'):normalize_route(value, 'independent')
        for complexity in (True, 0, '2'):
            value=route();value['contract']['complexity']=complexity
            with self.assertRaises(ValueError):normalize_route(value, 'request')

    def test_development_defaults_to_independent_all_criteria_and_request_instructions(self):
        value = normalize_route(route(), '实现原合同，不能降低标准')
        self.assertEqual(value['contract']['instructions'], '实现原合同，不能降低标准')
        self.assertEqual(value['contract']['skills'], {'coding': .75, 'reasoning': .25})
        self.assertEqual(value['contract']['reviewRequirements']['mode'], 'independent')
        self.assertEqual(value['contract']['reviewRequirements']['criterionIds'], ['direct-task-c1', 'direct-task-c2'])
        self.assertNotIn('assignment', value['contract'])
        other = route();other['contract']['reviewRequirements']={'mode':'none','criterionIds':[],'reason':'skip'}
        with self.assertRaisesRegex(ValueError, '不能降级'):normalize_route(other, 'request')

    def test_regular_tasks_preserve_explicit_review_obligations_without_keyword_routing(self):
        value = route('writing');value['contract']['acceptance']=['独立产物复核确认内容正确']
        normalized = normalize_route(value, '整理内容')
        self.assertEqual(normalized['mode'],'direct')
        self.assertEqual(normalized['contract']['reviewRequirements']['mode'],'independent')
        value['contract']['reviewRequirements']={'mode':'none','criterionIds':[],'reason':'omit'}
        with self.assertRaises(ValueError):normalize_route(value,'整理内容')
        ordinary=normalize_route(route('writing'),'整理内容')
        self.assertEqual(ordinary['contract']['reviewRequirements']['mode'],'none')

    def test_invalid_scope_role_skill_or_host_owned_fields_cannot_grant_authority(self):
        variants=[('scope',['../escape']),('scope',['src/','src/code.py']),('roleId','coordinator'),
                  ('roleId','reviewer-sol'),('roleType','coordinator'),('roleType',[]),
                  ('skills',{'coding':True}),('skills',{'coding':2}),('instructions',[]),
                  ('assignment',{'model':'native_codex'}),('workerBinding',{'enabled':True}),
                  ('reviewRequirements',{'mode':'independent','criterionIds':['wrong-c1'],'reason':'bad'})]
        for key, item in variants:
            value=route();value['contract'][key]=item
            with self.subTest(key=key,item=item),self.assertRaises(ValueError):normalize_route(value,'request')

    def test_only_text_and_explicit_workspace_enter_direct_route(self):
        value=route();value['contract']['requiredCapabilities']=['text','workspace']
        normalized=normalize_route(value,'use already granted workspace')
        self.assertEqual(normalized['contract']['requiredCapabilities'],['text','workspace'])
        self.assertNotIn('workerBinding',normalized['contract'])
        for caps in (['terminal','files'],['plugins'],['image'],['web_search'],['shell'],['text','workspace','terminal']):
            value=route();value['contract']['requiredCapabilities']=caps
            with self.subTest(caps=caps),self.assertRaises(ValueError):normalize_route(value,'request')

    def test_direct_plan_is_one_copy_and_keeps_normal_downstream_validation(self):
        from studio_business import validate_business_plan
        frozen=normalize_route(route(),'实现已给定合同');original=copy.deepcopy(frozen)
        run={'taskRoutingVersion':1,'taskRouting':frozen,'request':'实现已给定合同'}
        plan=direct_plan(run)
        self.assertEqual(len(plan['modules']),1)
        self.assertEqual(plan['modules'][0]['dependsOn'],[])
        self.assertEqual(plan['modules'][0]['complexity'],2)
        self.assertNotIn('assignment',plan['modules'][0])
        validate_business_plan(plan,['opus','sol','gemini'],review_requirement_version=1,execution_capabilities=['text'])
        plan['modules'][0]['scope'].append('changed.py')
        self.assertEqual(run['taskRouting'],original)

    def test_direct_requires_version_and_already_normalized_frozen_contract(self):
        frozen=normalize_route(route(),'request')
        for version in (None,True,0,2):
            with self.subTest(version=version),self.assertRaises(ValueError):
                direct_plan({'taskRoutingVersion':version,'taskRouting':frozen,'request':'request'})
        with self.assertRaises(ValueError):direct_plan({'taskRoutingVersion':1,'taskRouting':route(),'request':'request'})


if __name__=='__main__':unittest.main()
