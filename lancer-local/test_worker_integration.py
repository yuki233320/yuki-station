"""Routing and admission boundaries for explicitly granted workspace workers."""
import copy
from pathlib import Path
import tempfile
import unittest

from app import Store
from studio import Studio
from studio_business import validate_business_plan
from business_routing import assign_modules
from test_studio_business import BusinessClient, module
from worker_integration import run_version


class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(Store(Path(self.tmp.name) / 'data', seed=False), BusinessClient())
        self.project = self.studio.create_project('Isolated worker admission', 'Fixture only')
        self.project = self.studio.project(self.project['id'])
        script = Path(self.tmp.name) / 'check.py'
        script.write_text('raise AssertionError("Admission must never execute a checker")\n')
        check = self.studio.host_acceptance.register({'name': 'Trusted fixture', 'scriptPath': str(script),
            'files': ['calc.py'], 'criteria': [{'id': 'TEST-1', 'description': 'Calculation is correct'}], 'timeoutSeconds': 5})
        self.checks = self.studio.host_acceptance.snapshot([check['id']])
        self.policy = {'version': 1, 'roleGrants': {'backend': {'enabled': True, 'checkIds': [check['id']]}}}

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        task = module('calc', 'development', 2)
        task.update(roleId='calc-owner', roleType='backend', scope=['calc.py'], requiredCapabilities=['text', 'workspace'])
        return {'summary': 'Implement calculation', 'businessUnderstanding': 'Known calculation requirement',
                'modules': [task], 'integration': 'Verify the actual calculation', 'blockingQuestions': [], 'risks': []}

    def validate(self, plan, policy=None):
        return validate_business_plan(plan, ['opus', 'sol', 'gemini'], native_check_version=1,
            execution_capabilities=['text', 'workspace'], review_requirement_version=1,
            worker_tools=policy, host_acceptance_checks=self.checks)

    def test_authorized_workspace_routes_to_api_model_without_native_execution(self):
        plan = self.validate(self.plan(), self.policy)
        assign_modules(plan['modules'], ['opus', 'sol', 'gemini'])
        task = plan['modules'][0]
        self.assertEqual(task['assignment']['model'], 'sol')
        self.assertIn('workspace', task['assignment']['capabilities'])
        self.assertEqual(task['workerBinding']['sourceRoleId'], 'backend')
        self.assertNotIn('nativeCheckVersion', task)

    def test_model_supplied_binding_cannot_grant_itself_workspace_tools(self):
        plan = self.plan()
        plan['modules'][0]['workerBinding'] = {'enabled': True, 'checkIds': [self.checks[0]['id']]}
        with self.assertRaisesRegex(ValueError, '尚未授权'):
            self.validate(plan)
        self.assertNotIn('workerBinding', plan['modules'][0])

    def test_workspace_cannot_hide_native_or_browser_capability_in_one_module(self):
        for capability in ('files', 'terminal', 'browser', 'plugins', 'web_search'):
            with self.subTest(capability=capability):
                plan = self.plan();plan['modules'][0]['requiredCapabilities'].append(capability)
                with self.assertRaises(ValueError):self.validate(plan, self.policy)

    def test_project_cannot_remove_checks_still_granted_to_worker(self):
        project = self.studio.set_host_acceptance(self.project['id'], self.project['version'], [self.checks[0]['id']])
        config = copy.deepcopy(project['teamConfig']);config['workerTools'] = self.policy
        project = self.studio.update_project(project['id'], '', project['version'], config)
        before = self.studio.meta(project['id'])
        with self.assertRaisesRegex(ValueError, '项目已选择'):
            self.studio.set_host_acceptance(project['id'], project['version'], [])
        self.assertEqual(self.studio.meta(project['id']), before)

    def test_nested_worker_marker_cannot_be_downgraded_to_legacy(self):
        self.assertEqual(run_version({'plan': None, 'nodes': []}), 0)
        plan = self.validate(self.plan(), self.policy)
        run = {'workerToolsVersion': 1, 'workerTools': self.policy, 'hostAcceptanceChecks': self.checks,
               'plan': plan, 'nodes': []}
        self.assertEqual(run_version(run), 1)
        del run['workerToolsVersion']
        with self.assertRaisesRegex(ValueError, '不能降级'):run_version(run)

    def test_removed_registry_check_can_be_revoked_then_removed_from_project(self):
        project = self.studio.set_host_acceptance(self.project['id'], self.project['version'], [self.checks[0]['id']])
        config = copy.deepcopy(project['teamConfig']);config['workerTools'] = self.policy
        project = self.studio.update_project(project['id'], '', project['version'], config)
        self.studio.host_acceptance.remove(self.checks[0]['id'])
        with self.assertRaises(ValueError):
            self.studio.update_project(project['id'], '', project['version'], config)
        config['workerTools'] = {'version': 1, 'roleGrants': {'backend': {'enabled': False, 'checkIds': []}}}
        project = self.studio.update_project(project['id'], '', project['version'], config)
        project = self.studio.set_host_acceptance(project['id'], project['version'], [])
        self.assertEqual(project['hostAcceptanceIds'], [])
        self.assertFalse(project['teamConfig']['workerTools']['roleGrants']['backend']['enabled'])


if __name__ == '__main__':
    unittest.main()
