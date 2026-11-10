import copy
import unittest

from app import Conflict
from project_team import read_summary, validate_team_config
from studio_business import validate_business_plan
import test_studio_business as fixtures
from test_studio_business import module


def definition(**changes):
    return dict(title='门店补货协调员', businessContext='连接门店库存与供应商，减少断货。',
                responsibilities='核对库存及补货约束，不直接下采购单。',
                deliverables='提供补货建议表和原因说明。', acceptance='商品、数量和建议依据齐全。', **changes)


class CustomRoleTests(unittest.TestCase):
    setUp = fixtures.BusinessWorkflowTests.setUp
    tearDown = fixtures.BusinessWorkflowTests.tearDown
    run_task = fixtures.BusinessWorkflowTests.run_task

    def create_role(self, value=None):
        before = self.studio.project(self.pid)
        after = self.studio.save_custom_role(self.pid, None, value or definition(), before['version'])
        rid = next(iter(set(after['teamConfig']['customRoles']) - set(before['teamConfig']['customRoles'])))
        return rid, after

    def test_name_alone_is_rejected_and_backend_generates_stable_id(self):
        before = self.studio.project(self.pid)
        with self.assertRaises(ValueError):
            self.studio.save_custom_role(self.pid, None, {'title': '新岗位'}, before['version'])
        self.assertEqual(self.studio.project(self.pid)['version'], before['version'])
        rid, created = self.create_role()
        self.assertTrue(rid.startswith('custom-'))
        changed = definition()
        changed['title'] = '华东门店补货协调员'
        updated = self.studio.save_custom_role(self.pid, rid, changed, created['version'])
        self.assertEqual(list(updated['teamConfig']['customRoles']), [rid])
        row = next(r for r in updated['roleDirectory']['roles'] if r['roleId'] == rid)
        self.assertEqual(row['title'], changed['title'])
        self.assertEqual(row['definition'], changed)
        self.assertEqual(row['source'], 'user-defined')
        with self.assertRaises(Conflict):
            self.studio.save_custom_role(self.pid, rid, changed, created['version'])

    def test_custom_role_needs_no_base_and_model_cannot_change_definition(self):
        rid, project = self.create_role()
        saved = project['teamConfig']['customRoles'][rid]
        self.client.modules = [dict(module('restock'), roleId=rid, roleType='backend',
                                    roleDefinition={'title': '模型自行改名'})]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        planned = run['plan']['modules'][0]
        self.assertNotIn('roleType', planned)
        self.assertEqual(planned['roleDefinition'], saved)
        contract = run['taskContracts']['restock']
        self.assertEqual(contract['workFramework']['roleTitle'], saved['title'])
        self.assertEqual(contract['workFramework']['roleDefinition'], saved)
        node = next(n for n in run['nodes'] if n['id'] == 'build-restock')
        context = self.studio.context(self.pid, session['id'], node['contextRef'])
        role = context['memory']['roleAssignment']
        self.assertEqual(role['title'], saved['title'])
        self.assertIsNone(role['guideInheritance']['roleType'])
        for field in ('businessContext', 'responsibilities', 'deliverables', 'acceptance'):
            self.assertIn(saved[field], role['guide'])
        memories = [m for m in read_summary(self.studio, self.pid)['memories'] if m['roleId'] == rid]
        self.assertEqual(memories[-1]['title'], saved['title'])
        self.assertEqual(memories[-1]['roleDefinition'], saved)
        changed = dict(saved, title='新名称', responsibilities='新增工作要求。')
        current = self.studio.project(self.pid)
        self.studio.save_custom_role(self.pid, rid, changed, current['version'])
        reloaded = self.studio.load(self.pid, session['id'])['runs'][-1]
        self.assertEqual(reloaded['taskContracts']['restock'], contract)
        self.assertEqual(self.studio.context(self.pid, session['id'], node['contextRef']), context)
        self.assertEqual(read_summary(self.studio, self.pid)['memories'][-2]['title'], saved['title'])

    def test_selected_base_is_inherited_before_user_role_definition(self):
        rid, project = self.create_role(definition(baseRoleType='data'))
        self.client.modules = [dict(module('data-check'), roleId=rid, roleType='frontend')]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['plan']['modules'][0]['roleType'], 'data')
        node = next(n for n in run['nodes'] if n['id'] == 'build-data-check')
        memory = self.studio.context(self.pid, session['id'], node['contextRef'])['memory']
        self.assertEqual(memory['roleAssignment']['guideInheritance']['roleIds'], ['data', rid])
        paths = [s['path'] for s in memory['guidanceSnapshot']['sources']]
        self.assertLess(paths.index('teamConfig.roleGuides.data'), paths.index('teamConfig.customRoles.' + rid))

    def test_unregistered_custom_role_is_rejected_and_legacy_role_is_supported(self):
        plan = {'summary': '任务', 'businessUnderstanding': '已知业务', 'integration': '验收',
                'modules': [dict(module('report'), roleId='custom-invented')]}
        with self.assertRaisesRegex(ValueError, '尚未由用户登记'):
            validate_business_plan(copy.deepcopy(plan), ['opus', 'sol'])
        plan['modules'][0]['roleId'] = 'existing-technical-owner'
        validate_business_plan(plan, ['opus', 'sol'])

    def test_user_role_does_not_grant_tools_and_full_config_validates_definitions(self):
        rid, project = self.create_role()
        plan = {'summary': '任务', 'businessUnderstanding': '已知业务', 'integration': '验收',
                'modules': [dict(module('browser'), roleId=rid, requiredCapabilities=['browser'])]}
        with self.assertRaisesRegex(ValueError, '尚未接通'):
            validate_business_plan(plan, ['opus', 'sol'], role_directory=project['roleDirectory'])
        for field in ('businessContext', 'responsibilities', 'deliverables', 'acceptance'):
            invalid = copy.deepcopy(project['teamConfig'])
            invalid['customRoles'][rid][field] = ''
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_team_config(invalid)
        invalid = copy.deepcopy(project['teamConfig'])
        invalid['customRoles'][rid]['baseRoleType'] = 'coordinator'
        with self.assertRaises(ValueError):
            validate_team_config(invalid)


if __name__ == '__main__':
    unittest.main()
