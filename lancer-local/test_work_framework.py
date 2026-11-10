"""Role inheritance and handoff contracts through the real local scheduler."""
import copy
import unittest

from project_team import project_context
from studio_business import validate_business_plan, validate_business_response
from task_contracts import freeze_contract
import test_studio_business as fixtures
from test_studio_business import module


class WorkFrameworkTests(unittest.TestCase):
    setUp = fixtures.BusinessWorkflowTests.setUp
    tearDown = fixtures.BusinessWorkflowTests.tearDown
    run_task = fixtures.BusinessWorkflowTests.run_task

    def test_custom_role_inherits_type_before_specific_project_rules(self):
        meta = self.studio.meta(self.pid)
        meta['teamConfig']['roleGuides'].update(frontend='复用现有设计系统。', **{'checkout-ui': '结账确认按钮使用项目规定文案。'})
        self.studio.update_project(self.pid, meta['instructions'], meta['version'], meta['teamConfig'])
        def source_files(state, workspace, old):
            directory = workspace / 'roles'
            directory.mkdir()
            (directory / 'frontend.md').write_text('保证键盘可操作。')
            (directory / 'checkout-ui.md').write_text('金额确认必须显示币种。')
            (directory / 'domain-expert.md').write_text('核对订单术语。')
            return {'detail': 'fixture role documents'}
        self.store.commit(self.pid, '规范', 'fixture', source_files)
        self.client.modules = [dict(module('checkout', 'development', 2), roleId='checkout-ui', roleType='frontend')]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        node = next(n for n in run['nodes'] if n['id'] == 'build-checkout')
        memory = self.studio.context(self.pid, session['id'], node['contextRef'])['memory']
        role = memory['roleAssignment']
        self.assertIn('复用现有设计系统', role['guide'])
        self.assertIn('结账确认按钮', role['guide'])
        self.assertEqual(role['guideInheritance']['roleIds'], ['frontend', 'checkout-ui'])
        paths = [s['path'] for s in memory['guidanceSnapshot']['sources']]
        expected = ['teamConfig.roleGuides.frontend', 'roles/frontend.md',
                    'teamConfig.roleGuides.checkout-ui', 'roles/checkout-ui.md']
        positions = [paths.index(path) for path in expected]
        self.assertEqual(positions, sorted(positions))
        framework = run['taskContracts']['checkout']['workFramework']
        self.assertEqual(role['workFramework'], framework)
        self.assertEqual(framework['roleType'], 'frontend')
        self.assertEqual(framework['outputs'], ['checkout.md'])
        self.assertEqual(framework['completionCriteria'], ['明确说明 checkout 的成果'])
        self.assertEqual(framework['communication']['discussionRoundLimit'], 0)
        directory = self.studio.project(self.pid)['roleDirectory']
        file_role = next(r for r in directory['roles'] if r['roleId'] == 'domain-expert')
        self.assertEqual(file_role['file'], 'roles/domain-expert.md')
        planning = next(c for c in self.client.calls if c['key'][0] == 'plan')['memory']['sharedEvidence']
        self.assertEqual(planning['roleDirectory'], directory)

    def test_framework_is_frozen_and_does_not_replace_actual_handoff(self):
        self.client.modules = [module('upstream'), module('downstream', deps=['upstream'])]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        target = next(m for m in run['plan']['modules'] if m['id'] == 'downstream')
        original = copy.deepcopy(run['taskContracts']['downstream'])
        frame = original['workFramework']
        self.assertEqual(frame['inputs'], [{'moduleId': 'upstream', 'expectedPaths': ['upstream.md']}])
        self.assertEqual(frame['upstream'][0]['roleId'], 'upstream-owner')
        self.assertEqual(run['taskContracts']['upstream']['workFramework']['downstream'][0]['moduleId'], 'downstream')
        handoff = next(h for h in run['handoffs'] if h['nodeId'] == 'build-downstream')
        self.assertEqual(handoff['contractFingerprint'], original['fingerprint'])
        self.assertTrue(handoff['sources'][0]['files'][0]['sha256'])
        target['instructions'] = 'later change must not mutate frozen scope'
        self.assertEqual(freeze_contract(self.studio, session, run, target, 'draft'), original)
        memory = project_context(self.studio, session, run, 'downstream-worker', target['assignment']['model'], '',
                                 {'businessStage': 'build', 'roleId': target['roleId'], 'module': target,
                                  'executionContract': original})
        self.assertEqual(memory['roleAssignment']['workFramework'], frame)

    def test_two_upstream_modules_sharing_role_can_receive_separate_questions(self):
        self.client.modules = [dict(module('left'), roleId='backend'), dict(module('right'), roleId='backend'),
                               module('downstream', deps=['left', 'right'])]
        asked = []
        def ask_once(stage, result):
            if stage == 'build' and result['summary'].endswith('downstream') and not asked:
                asked.append(True)
                result.update(files=[], questions=[{'toRole': 'backend', 'toModule': mid,
                    'question': '确认本模块的交接约定。', 'evidence': mid + '.md 的字段需要确认。'} for mid in ('left', 'right')])
            return result
        self.client.mutate = ask_once
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        answers = [c for c in self.client.calls if c['key'][0] == 'clarification']
        self.assertEqual(len(answers), 2)
        self.assertEqual({c['memory']['currentPosition']['moduleId'] for c in answers}, {'left', 'right'})
        self.assertTrue(all(c['memory']['questionOrigin']['moduleId'] == 'downstream' for c in answers))
        frame = session['runs'][-1]['taskContracts']['downstream']['workFramework']
        self.assertEqual(frame['communication']['maxQuestions'], 2)
        self.assertEqual(frame['communication']['contacts'][1:],
                         [{'roleId': 'backend', 'moduleId': 'left'}, {'roleId': 'backend', 'moduleId': 'right'}])

    def test_role_type_is_optional_but_explicit_values_are_validated(self):
        plan = {'summary': '岗位计划', 'businessUnderstanding': '完成当前业务交付',
                'modules': [module('brief')], 'integration': '逐项验收'}
        validate_business_plan(copy.deepcopy(plan), ['opus', 'sol'])
        for role_type in ('coordinator', 'frontend-owner', ['frontend']):
            with self.subTest(role_type=role_type):
                invalid = copy.deepcopy(plan)
                invalid['modules'][0]['roleType'] = role_type
                with self.assertRaisesRegex(ValueError, 'roleType'):
                    validate_business_plan(invalid, ['opus', 'sol'])
        plan['modules'][0]['roleType'] = 'writing'
        self.assertEqual(validate_business_plan(plan, ['opus', 'sol'])['modules'][0]['roleType'], 'writing')

    def test_same_actual_recipient_still_requires_one_combined_question(self):
        upstream = dict(module('upstream'), roleId='backend')
        downstream = module('downstream', deps=['upstream'])
        shared = {'businessStage': 'build', 'module': downstream, 'plan': {'modules': [upstream, downstream]}}
        for role, explicit in (('backend', 'upstream'), ('coordinator', '')):
            with self.subTest(role=role):
                question = {'toRole': role, 'question': '确认约定', 'evidence': '当前交接需要明确约定。'}
                result = {'summary': '需要确认', 'files': [], 'questions': [question, dict(question, toModule=explicit)]}
                with self.assertRaisesRegex(ValueError, '同一接收角色及模块'):
                    validate_business_response({'agent': 'sol'}, result, shared, ['opus', 'sol'])


if __name__ == '__main__':
    unittest.main()
