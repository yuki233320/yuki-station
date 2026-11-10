"""Working guidance follows the real configuration, scheduler and native boundary."""
import copy
import unittest

from app import Conflict
from studio import Studio
import test_studio_business as business
import test_studio_native as native_fixtures
from work_policy import (CODING_DISCIPLINE, DEEP_REVIEW_QUESTION, FIRST_PRINCIPLES,
                         default_policy, policy_guide, prompt_for, read_policy, update_policy)


class WorkPolicyTests(unittest.TestCase):
    setUp = business.BusinessWorkflowTests.setUp
    tearDown = business.BusinessWorkflowTests.tearDown
    run_task = business.BusinessWorkflowTests.run_task

    def test_global_configuration_persists_and_is_not_project_metadata(self):
        self.assertEqual(read_policy(self.studio), default_policy())
        self.assertFalse((self.store.root / 'work-policy.json').exists())
        requested = dict(default_policy(), firstPrinciples=False, deepReviewMultiplier=3)
        saved = update_policy(self.studio, requested)['workPolicy']
        self.assertEqual(saved['version'], 1)
        second = self.studio.create_project('另一个项目', '使用同一规范')['id']
        self.assertNotIn('workPolicy', self.studio.meta(self.pid))
        self.assertNotIn('workPolicy', self.studio.meta(second))
        reloaded = Studio(self.store, self.client)
        self.assertEqual(reloaded.index()['workPolicy'], saved)
        self.assertEqual(reloaded.index()['workPolicyGuide'], policy_guide())
        self.assertEqual(policy_guide()['deepReviewQuestion'], DEEP_REVIEW_QUESTION)

    def test_invalid_entry_and_stale_version_leave_configuration_unchanged(self):
        valid = update_policy(self.studio, default_policy())['workPolicy']
        original = (self.store.root / 'work-policy.json').read_bytes()
        for key, value in [('firstPrinciples', 'true'), ('codingDiscipline', 1),
                           ('deepReview', None), ('deepReviewMultiplier', True),
                           ('deepReviewMultiplier', 0), ('deepReviewMultiplier', 4),
                           ('version', True)]:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    update_policy(self.studio, dict(valid, **{key: value}))
        with self.assertRaises(Conflict):
            update_policy(self.studio, default_policy())
        self.assertEqual((self.store.root / 'work-policy.json').read_bytes(), original)

    def test_role_prompts_carry_frozen_guidance_and_keep_writing_focused(self):
        self.client.modules = [business.module('code', 'development', 2), business.module('copy')]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['workPolicy'], default_policy())
        for node in run['nodes']:
            context = self.studio.context(self.pid, session['id'], node['contextRef'])
            instructions = context['memory']['globalWorkInstructions']
            self.assertEqual(context['memory']['globalWorkPolicy'], run['workPolicy'])
            self.assertIn(FIRST_PRINCIPLES, instructions)
            self.assertIn(instructions, context['messages'][-1]['content'])
            if node['id'] == 'build-copy':
                self.assertNotIn(CODING_DISCIPLINE, instructions)
            else:
                self.assertIn(CODING_DISCIPLINE, instructions)

    def test_global_edits_affect_the_next_run_only(self):
        def edit_after_planning(stage, result):
            if stage == 'plan':
                current = read_policy(self.studio)
                update_policy(self.studio, dict(current, firstPrinciples=False,
                                               codingDiscipline=False, deepReview=False))
            return result
        self.client.mutate = edit_after_planning
        first = self.run_task()
        self.assertEqual(first['status'], 'completed', first.get('error'))
        self.assertEqual(first['runs'][-1]['workPolicy'], default_policy())
        first_calls = list(self.client.calls)
        self.assertTrue(all(FIRST_PRINCIPLES in c['memory']['globalWorkInstructions'] for c in first_calls))
        self.client.mutate = None
        self.client.calls.clear()
        second = self.run_task()
        self.assertEqual(second['status'], 'completed', second.get('error'))
        self.assertFalse(second['runs'][-1]['workPolicy']['firstPrinciples'])
        self.assertTrue(all(c['memory']['globalWorkInstructions'] == '' for c in self.client.calls))

    def test_internal_comparison_snapshot_overrides_current_settings(self):
        frozen = default_policy()
        update_policy(self.studio, dict(frozen, deepReviewMultiplier=3, firstPrinciples=False))
        session = self.studio.create_session(self.pid, '预冻结工作规范', '交付一份摘要', 'general')
        self.studio.start(self.pid, session['id'], work_policy=frozen)
        self.studio.threads[session['id']].join(15)
        saved = self.studio.load(self.pid, session['id'])
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        self.assertEqual(saved['runs'][-1]['workPolicy'], frozen)
        frozen['deepReviewMultiplier'] = 1
        self.assertEqual(saved['runs'][-1]['workPolicy']['deepReviewMultiplier'], 2)

    def test_historical_run_without_snapshot_is_not_rewritten(self):
        session = self.studio.create_session(self.pid, '旧记录', '说明项目', 'general')
        revision = self.store.current(self.pid)[0]['revision']
        run = {'id': 'old-run', 'request': '说明项目', 'projectRevision': revision}
        before = copy.deepcopy(run)
        context, messages = self.studio.assemble_context(session, run, 'coordinator', 'opus', '给出说明', {})
        self.assertEqual(run, before)
        self.assertNotIn('globalWorkPolicy', context['memory'])
        self.assertNotIn(FIRST_PRINCIPLES, messages[-1]['content'])

    def test_native_instruction_includes_the_same_frozen_guidance(self):
        fixture = native_fixtures.NativeTests('test_actual_files_evidence_and_no_repeat_call')
        fixture.setUp()
        try:
            policy = default_policy()
            text = prompt_for(policy, fixture.run, 'native-qa', fixture.shared)
            fixture.context['memory']['globalWorkPolicy'] = policy
            fixture.context['memory']['globalWorkInstructions'] = text
            def check_task(task, *args, **kwargs):
                self.assertIn(FIRST_PRINCIPLES, task['instruction'])
                self.assertIn(CODING_DISCIPLINE, task['instruction'])
                self.assertEqual(task['context']['globalWorkPolicy'], policy)
                return fixture.fixture(task, *args, **kwargs)
            result, receipt = fixture.execute(check_task)
            self.assertTrue(result['nativeEvidence']['inputUnchanged'])
            self.assertEqual(receipt['status'], 'completed')
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
