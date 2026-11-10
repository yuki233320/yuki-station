"""Worker grants are explicit, project-limited, and independent of model brand."""
import copy
import hashlib
import json
import unittest

from project_team import team_defaults, validate_team_config
from worker_policy import normalize_policy, validate_policy_checks, bind_role, validate_plan_bindings


def check(cid='fixed-check', files=None):
    return {'id': cid, 'name': '可信固定脚本', 'files': files or ['out/result.py'],
            'criteria': [{'id': 'case-1', 'description': '明确业务断言'}],
            'scriptSha256': 'a' * 64, 'timeoutSeconds': 10}


class Registry:
    def __init__(self):
        self.calls = []
        self.revoked = False

    def authorize(self, frozen):
        self.calls.append(copy.deepcopy(frozen))
        if self.revoked:
            raise ValueError('本机脚本授权已撤销')


class WorkerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.checks = [check(), check('qa-check', ['out/qa.json'])]
        self.policy = {'version': 1, 'roleGrants': {
            'backend': {'enabled': True, 'checkIds': ['fixed-check']},
            'qa': {'enabled': True, 'checkIds': ['qa-check']}}}
        self.module = {'id': 'implementation', 'roleId': 'backend', 'scope': ['out/'],
                       'requiredCapabilities': ['text', 'workspace'], 'assignment': {'model': 'sol'}}

    def test_missing_policy_and_new_defaults_do_not_grant_tools(self):
        self.assertEqual(normalize_policy(None), {'version': 1, 'roleGrants': {}})
        self.assertIsNone(bind_role(self.module, None, []))
        self.assertEqual(team_defaults(new_project=True)['workerTools'], {'version': 1, 'roleGrants': {}})
        self.assertNotIn('workerTools', team_defaults())
        self.assertNotIn('workerTools', validate_team_config({'businessBrief': '旧项目'}))

    def test_child_execution_is_explicit_frozen_and_legacy_shape_is_preserved(self):
        original = bind_role(self.module, self.policy, self.checks)
        self.assertNotIn('childExecution', original)
        self.policy['roleGrants']['backend']['childExecution'] = True
        normalized = normalize_policy(self.policy)
        enabled = bind_role(self.module, normalized, self.checks)
        self.assertTrue(enabled['childExecution'])
        self.assertNotEqual(enabled['fingerprint'], original['fingerprint'])
        self.policy['roleGrants']['backend']['childExecution'] = False
        self.assertEqual(bind_role(self.module, self.policy, self.checks), original)
        self.policy['roleGrants']['backend']['childExecution'] = 1
        with self.assertRaisesRegex(ValueError, '布尔值'):normalize_policy(self.policy)

    def test_process_requirements_are_frozen_and_task_cannot_weaken_role_obligation(self):
        from worker_policy import require_workspace_process
        req={'version':1,'initialFailure':{'checkId':'fixed-check','criterionIds':['case-1']},'requireExecutionChild':True}
        self.policy['roleGrants']['backend'].update(childExecution=True,processRequirements=req)
        self.assertEqual(normalize_policy(self.policy),self.policy)
        self.module['processRequirements']={'version':1,'initialFailure':None,'requireExecutionChild':True}
        binding=bind_role(self.module,self.policy,self.checks)
        self.assertEqual(binding['processRequirements'],req)
        self.module['requiredCapabilities']=['text']
        with self.assertRaisesRegex(ValueError,'workspace'):require_workspace_process(self.module,self.policy)

    def test_process_requirements_cannot_grant_checks_or_child_execution(self):
        grant=self.policy['roleGrants']['backend']
        grant['processRequirements']={'version':1,'initialFailure':{'checkId':'fixed-check','criterionIds':['case-1']},'requireExecutionChild':True}
        with self.assertRaisesRegex(ValueError,'childExecution'):validate_policy_checks(self.policy,self.checks)
        grant['childExecution']=True
        grant['processRequirements']['initialFailure']['criterionIds']=['made-up']
        with self.assertRaisesRegex(ValueError,'标准'):validate_policy_checks(self.policy,self.checks)
        grant['processRequirements']['initialFailure']={'checkId':'qa-check','criterionIds':['case-1']}
        with self.assertRaisesRegex(ValueError,'本岗位'):validate_policy_checks(self.policy,self.checks)

    def test_task_process_requirement_is_stronger_without_mutating_policy(self):
        self.module['processRequirements']={'version':1,'initialFailure':{'checkId':'fixed-check','criterionIds':['case-1']},'requireExecutionChild':False}
        before=copy.deepcopy(self.policy)
        self.assertEqual(bind_role(self.module,self.policy,self.checks)['processRequirements'],self.module['processRequirements'])
        self.assertEqual(self.policy,before)
        from worker_process_policy import normalize_requirements,merge_requirements
        with self.assertRaises(ValueError):normalize_requirements({'version':True,'initialFailure':None,'requireExecutionChild':True})
        with self.assertRaises(ValueError):normalize_requirements({'version':1,'initialFailure':None,'requireExecutionChild':False})
        other={'version':1,'initialFailure':{'checkId':'qa-check','criterionIds':['case-1']},'requireExecutionChild':False}
        with self.assertRaisesRegex(ValueError,'冲突'):merge_requirements(self.module['processRequirements'],other)

    def test_project_team_roundtrip_keeps_explicit_policy_without_aliasing(self):
        config = validate_team_config({'workerTools': self.policy})
        self.assertEqual(config['workerTools'], self.policy)
        config['workerTools']['roleGrants']['backend']['checkIds'].clear()
        self.assertEqual(self.policy['roleGrants']['backend']['checkIds'], ['fixed-check'])

    def test_normalizer_rejects_versions_arbitrary_tools_and_reserved_roles(self):
        invalid = [[], {}, {'version': True}, {'version': 2}, {'version': 1, 'shell': True},
                   {'version': 1, 'roleGrants': []},
                   {'version': 1, 'roleGrants': {'coordinator': {}}},
                   {'version': 1, 'roleGrants': {'reviewer': {}}},
                   {'version': 1, 'roleGrants': {'reviewer-model': {'enabled': True}}},
                   {'version': 1, 'roleGrants': {'backend': {'enabled': 1}}},
                   {'version': 1, 'roleGrants': {'backend': {'enabled': True, 'command': 'sh'}}},
                   {'version': 1, 'roleGrants': {'backend': {'checkIds': ['x', 'x']}}},
                   {'version': 1, 'roleGrants': {'backend': {'checkIds': ['../check']}}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_policy(value)

    def test_check_ids_must_be_selected_project_snapshots_not_global_registry_ids(self):
        self.assertIsNone(validate_policy_checks(self.policy, self.checks))
        with self.assertRaisesRegex(ValueError, '项目已选择'):
            validate_policy_checks(self.policy, self.checks[:1])
        with self.assertRaisesRegex(ValueError, '重复'):
            validate_policy_checks(self.policy, [self.checks[0], self.checks[0]])
        with self.assertRaises(ValueError):
            validate_policy_checks(self.policy, [{'id': 'fixed-check'}])

    def test_binding_freezes_scope_checks_and_fingerprint_but_not_model_identity(self):
        binding = bind_role(self.module, self.policy, self.checks)
        self.assertEqual(binding['schema'], 'yuki-worker-binding/v1')
        self.assertEqual(binding['capabilities'], ['read', 'search', 'write', 'run_check'])
        self.assertEqual(binding['scope'], ['out/'])
        self.assertEqual(binding['checks'], [self.checks[0]])
        expected = hashlib.sha256(json.dumps({k: v for k, v in binding.items() if k != 'fingerprint'},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(binding['fingerprint'], expected)
        self.module['assignment']['model'] = 'gemini'
        self.assertEqual(bind_role(self.module, self.policy, self.checks), binding)
        binding['checks'][0]['criteria'][0]['description'] = 'changed'
        self.assertEqual(self.checks[0]['criteria'][0]['description'], '明确业务断言')

    def test_exact_empty_disabled_or_no_checks_blocks_inheritance(self):
        self.module.update(roleId='api-worker', roleType='backend')
        self.assertEqual(bind_role(self.module, self.policy, self.checks)['sourceRoleId'], 'backend')
        for grant in ({}, {'enabled': False, 'checkIds': ['fixed-check']}, {'enabled': True, 'checkIds': []}):
            with self.subTest(grant=grant):
                self.policy['roleGrants']['api-worker'] = grant
                registry = Registry()
                self.assertIsNone(bind_role(self.module, self.policy, self.checks, registry))
                self.assertEqual(registry.calls, [])

    def test_custom_role_inherits_only_host_owned_base_not_model_supplied_type(self):
        self.module.update(roleId='custom-research', roleType='qa', roleDefinition={'baseRoleType': 'backend'})
        binding = bind_role(self.module, self.policy, self.checks)
        self.assertEqual(binding['sourceRoleId'], 'backend')
        self.assertEqual(binding['checkIds'], ['fixed-check'])
        self.module['roleDefinition'] = {}
        self.assertIsNone(bind_role(self.module, self.policy, self.checks))
        self.policy['roleGrants']['custom-research'] = {'enabled': True, 'checkIds': ['fixed-check']}
        self.assertEqual(bind_role(self.module, self.policy, self.checks)['sourceRoleId'], 'custom-research')

    def test_role_name_is_not_a_permission_hint_and_reserved_role_cannot_borrow(self):
        self.module['roleId'] = 'backend-helper'
        self.assertIsNone(bind_role(self.module, self.policy, self.checks))
        for rid in ('coordinator', 'reviewer', 'reviewer-gemini'):
            self.module.update(roleId=rid, roleType='backend')
            with self.subTest(role=rid), self.assertRaises(ValueError):
                bind_role(self.module, self.policy, self.checks)

    def test_selected_script_still_requires_current_local_authorization(self):
        registry = Registry()
        bind_role(self.module, self.policy, self.checks, registry)
        self.assertEqual(registry.calls, [self.checks[0]])
        registry.revoked = True
        with self.assertRaisesRegex(ValueError, '撤销'):
            bind_role(self.module, self.policy, self.checks, registry)

    def test_script_paths_cannot_expand_scope_or_write_into_read_only_inputs(self):
        self.module['scope'] = ['out/other.py']
        with self.assertRaisesRegex(ValueError, '覆盖'):
            bind_role(self.module, self.policy, self.checks)
        for scope in (['../escape/'], ['/absolute/'], ['inputs/'], ['inputs/project/'], [{}]):
            self.module['scope'] = scope
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                bind_role(self.module, self.policy, self.checks)

    def test_plan_requires_workspace_grant_and_cannot_mix_tool_engines(self):
        plan = {'modules': [copy.deepcopy(self.module)]}
        binding = validate_plan_bindings(plan, self.policy, self.checks)
        self.assertIsNotNone(binding[self.module['id']])
        self.assertNotIn('workerBinding', plan['modules'][0], 'Helper must not mutate planner input')
        with self.assertRaisesRegex(ValueError, '没有可用'):
            validate_plan_bindings(plan, None, self.checks)
        plan['modules'][0]['requiredCapabilities'].append('terminal')
        with self.assertRaisesRegex(ValueError, '不同模块'):
            validate_plan_bindings(plan, self.policy, self.checks)
        plan['modules'][0]['requiredCapabilities'] = ['text']
        plan['modules'][0]['workerBinding'] = {'enabled': True, 'scope': ['/']}
        self.assertEqual(validate_plan_bindings(plan, self.policy, self.checks), {self.module['id']: None})


if __name__ == '__main__':
    unittest.main()
