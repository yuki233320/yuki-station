"""Structured review plans and early dispatch checks; no model/network calls."""
import copy
from pathlib import Path
import tempfile
import unittest

from app import Store
from model_catalog import MODELS
from review_requirements import (assert_review_schedule, bind_review_schedule,
                                 validate_module_requirements, validate_plan_requirements,
                                 validate_review_decision)
from studio import Studio
from studio_business import review_policy, validate_business_plan
from test_studio_business import BusinessClient, module


class ReviewRequirementsTests(unittest.TestCase):
    def item(self, mode='independent', work='writing', complexity=2):
        result = module('report', work, complexity)
        result['acceptance'] = ['完整交付文件', '保留既有输入', '通过独立代码复核确认实现与约束一致']
        result['reviewRequirements'] = {'mode': mode, 'criterionIds': ['report-c3'] if mode == 'independent' else [],
                                        'reason': '明确绑定原定复核标准'}
        return result

    def bind(self, item, **changes):
        item['assignment'] = {'model': 'sol'}
        return bind_review_schedule(item, changes.get('available', ['opus', 'sol', 'gemini', 'deepseek']),
                                    'opus', changes.get('single_comparison', False), changes.get('baseline_required', False))

    def plan(self, item):
        return {'summary': '完成明确产物', 'businessUnderstanding': '按给定输入完成工作，不编造业务事实',
                'blockingQuestions': [], 'modules': [item], 'integration': '逐项核对产物与固定标准', 'risks': []}

    def test_low_complexity_explicit_obligation_schedules_review_without_discussion(self):
        for work in ('writing', 'data', 'development'):
            with self.subTest(work=work):
                item = self.item(work=work)
                self.assertEqual(review_policy(item, 1)['roundLimit'], 0)
                schedule = self.bind(item)
                self.assertEqual(schedule['mode'], 'independent')
                self.assertEqual(schedule['criterionIds'], ['report-c3'])
                self.assertEqual(schedule['nodeId'], 'verify-0-report')
                self.assertEqual(schedule['basis'], 'explicit_requirement')
                self.assertNotEqual(schedule['authorFamily'], schedule['reviewerFamily'])
                self.assertEqual(item['qualityReviewer'], schedule['reviewer'])
                self.assertEqual(assert_review_schedule(item), schedule)

    def test_missing_invalid_and_inconsistent_structured_requirements_are_rejected(self):
        original = self.item()
        bad_values = [None, {}, {'mode': 'same_model', 'criterionIds': ['report-c3'], 'reason': 'bad'},
                      {'mode': 'independent', 'criterionIds': [], 'reason': 'bad'},
                      {'mode': 'none', 'criterionIds': ['report-c3'], 'reason': 'bad'},
                      {'mode': 'independent', 'criterionIds': ['other-c3'], 'reason': 'bad'},
                      {'mode': 'independent', 'criterionIds': ['report-c3']*2, 'reason': 'bad'},
                      {'mode': 'independent', 'criterionIds': ['report-c1'], 'reason': 'wrong obligation'},
                      {'mode': 'independent', 'criterionIds': ['report-c3'], 'reason': ''}]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_module_requirements({**original, 'reviewRequirements': value})
        missing = copy.deepcopy(original); missing.pop('reviewRequirements')
        with self.assertRaises(ValueError): validate_module_requirements(missing)
        with self.assertRaisesRegex(ValueError, '独立复核承诺'):
            validate_module_requirements(self.item(mode='none'))

    def test_plan_text_requires_structure_but_common_negations_do_not_promise_review(self):
        item = self.item(mode='none')
        item['acceptance'] = ['内容完整']
        for text in ('无需独立复核', '不要求独立代码审查', 'No independent review is required',
                     'This is not an independent review', 'Without additional independent code review'):
            with self.subTest(text=text):
                item['instructions'] = text
                validate_module_requirements(item)
                plan = self.plan(item); plan['integration'] = text
                validate_plan_requirements(plan)
        item['instructions'] = '生成完整文件'
        plan = self.plan(item); plan['integration'] = '完成产物后进行独立代码复核'
        with self.assertRaisesRegex(ValueError, '规划正文承诺'): validate_plan_requirements(plan)
        item['instructions'] = '交由另一位模型审查产物'
        with self.assertRaisesRegex(ValueError, '执行说明承诺'): validate_module_requirements(item)

    def test_cannot_omit_or_skip_review_is_a_positive_obligation(self):
        for text in ('不得省略独立代码复核', '不能跳过独立产物审查'):
            with self.subTest(text=text):
                item = self.item(mode='none')
                item['acceptance'] = [text]
                with self.assertRaisesRegex(ValueError, '独立复核承诺'):
                    validate_module_requirements(item)
                item['reviewRequirements'] = {'mode': 'independent', 'criterionIds': ['report-c1'], 'reason': text}
                validate_module_requirements(item)

    def test_image_obligation_and_unavailable_reviewer_fail_before_binding(self):
        image = self.item(work='image')
        image['requiredCapabilities'] = ['image']
        with self.assertRaisesRegex(ValueError, '视觉复核尚未接通'): validate_module_requirements(image)
        item = self.item()
        with self.assertRaisesRegex(ValueError, '独立产物审查需要'):
            self.bind(item, available=['opus', 'sol'])
        self.assertNotIn('reviewSchedule', item)

    def test_single_baseline_remains_same_model_but_explicit_requirement_never_downgrades(self):
        item = self.item(mode='none', work='development', complexity=3)
        item['acceptance'] = ['实现固定接口', '覆盖边界条件']
        item['assignment'] = {'model': 'opus'}
        schedule = bind_review_schedule(item, ['opus'], 'opus', True, True)
        self.assertEqual(schedule['mode'], 'same_model')
        self.assertEqual(schedule['basis'], 'policy_baseline')
        self.assertEqual(schedule['criterionIds'], ['report-c1', 'report-c2'])
        self.assertEqual(item['reviewRequirements']['mode'], 'none')
        explicit = self.item(); explicit['assignment'] = {'model': 'opus'}
        with self.assertRaisesRegex(ValueError, '不能降级'):
            bind_review_schedule(explicit, ['opus', 'sol', 'gemini'], 'opus', True, False)

    def test_policy_baseline_is_retained_and_not_controlled_by_discussion_rounds(self):
        item = self.item(mode='none', work='development', complexity=3)
        item['acceptance'] = ['完成实现']
        schedule = self.bind(item, baseline_required=review_policy(item)['required'])
        self.assertEqual(schedule['mode'], 'independent')
        self.assertEqual(schedule['basis'], 'policy_baseline')
        item = self.item(work='research', complexity=5)
        item['acceptance'] = ['完成资料整理']
        item['reviewRequirements'] = {'mode': 'none', 'criterionIds': [], 'reason': '无新增独立义务'}
        self.assertEqual(review_policy(item, 1)['roundLimit'], 3)
        self.assertEqual(self.bind(item)['mode'], 'none')

    def test_decision_cannot_rebind_a_criterion_or_append_unscheduled_independent_obligation(self):
        item = self.item(); self.bind(item)
        original = copy.deepcopy(item)
        reordered = list(item['acceptance']); reordered[0], reordered[2] = reordered[2], reordered[0]
        with self.assertRaisesRegex(ValueError, '不得重排'):
            validate_review_decision(item, {'instructions': item['instructions'], 'acceptance': reordered})
        with self.assertRaisesRegex(ValueError, '结构化要求覆盖'):
            validate_review_decision(item, {'instructions': item['instructions'],
                                           'acceptance': item['acceptance'] + ['另外通过独立产物审查确认数据准确性']})
        result = validate_review_decision(item, {'instructions': item['instructions'] + '；细化实现方法',
                                                  'acceptance': item['acceptance'] + ['输出稳定排序']})
        self.assertEqual(item, original)
        self.assertEqual(result['reviewSchedule'], item['reviewSchedule'])
        self.assertEqual(result['acceptance'][-1], '输出稳定排序')

    def test_corrupted_projection_or_review_identity_cannot_pass_pre_dispatch_assertion(self):
        item = self.item(); self.bind(item)
        for mutate in (lambda m: m.update(artifactReview=False),
                       lambda m: m.update(qualityReviewer='sol'),
                       lambda m: m['reviewSchedule'].update(criterionIds=['report-c1']),
                       lambda m: m['reviewSchedule'].update(nodeId='not-the-frozen-node')):
            changed = copy.deepcopy(item); mutate(changed)
            with self.assertRaises(ValueError): assert_review_schedule(changed)
        with self.assertRaisesRegex(ValueError, '当前不可用'): assert_review_schedule(item, ['opus', 'sol'])

    def test_legacy_raw_plan_keeps_original_semantics_while_new_plan_requires_the_field(self):
        item = self.item(); item.pop('reviewRequirements')
        raw = self.plan(item)
        validate_business_plan(copy.deepcopy(raw), ['opus', 'sol', 'gemini'], review_requirement_version=0)
        self.assertFalse(review_policy(item)['required'])
        with self.assertRaisesRegex(ValueError, 'reviewRequirements'):
            validate_business_plan(copy.deepcopy(raw), ['opus', 'sol', 'gemini'], review_requirement_version=1)
        fixed = copy.deepcopy(raw)
        fixed['modules'][0]['reviewRequirements'] = {'mode': 'independent', 'criterionIds': ['report-c3'], 'reason': '保留原c3承诺'}
        validate_business_plan(fixed, ['opus', 'sol', 'gemini'], review_requirement_version=1)
        self.assertEqual(fixed['modules'][0]['acceptance'], raw['modules'][0]['acceptance'])

    def test_real_studio_rejects_unsatisfiable_plan_before_any_worker_call(self):
        with tempfile.TemporaryDirectory() as folder:
            client = BusinessClient([self.item()])
            client.available = lambda: ['opus', 'sol']
            studio = Studio(Store(Path(folder), seed=False), client)
            pid = studio.create_project('Early review feasibility', 'Preserve original c3')['id']
            session = studio.create_session(pid, 'Review', '整理说明并遵守独立复核要求', 'general')
            studio.start(pid, session['id'], coordinator_model='opus', team_models=['opus', 'sol'])
            studio.threads[session['id']].join(10)
            self.assertFalse(studio.threads[session['id']].is_alive())
            saved = studio.load(pid, session['id'])
            self.assertEqual(saved['status'], 'failed')
            self.assertIn('独立产物审查需要', saved['error'])
            self.assertEqual([call['key'][0] for call in client.calls], ['plan'])
            self.assertIsNone(saved['runs'][-1]['plan'])
            self.assertEqual(saved['runs'][-1]['reviewRequirementVersion'], 1)
            self.assertTrue(next(n for n in saved['runs'][-1]['nodes'] if n['id']=='draft')['responseRef'])

    def test_same_requested_model_behind_distinct_families_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as folder:
            client = BusinessClient([self.item()])
            client.profiles = copy.deepcopy(client.profiles)
            client.profiles['sol']['model'] = 'same-actual-requested-model'
            client.profiles['gemini']['model'] = 'same-actual-requested-model'
            client.available = lambda: ['opus', 'sol', 'gemini']
            studio = Studio(Store(Path(folder), seed=False), client)
            pid = studio.create_project('Alias collision fixture', 'Reject a known identity collision')['id']
            session = studio.create_session(pid, 'Review', '整理说明并遵守独立复核要求', 'general')
            studio.start(pid, session['id'], coordinator_model='opus', team_models=['opus', 'sol', 'gemini'])
            studio.threads[session['id']].join(10)
            self.assertFalse(studio.threads[session['id']].is_alive())
            saved = studio.load(pid, session['id'])
            self.assertEqual(saved['status'], 'failed')
            self.assertIn('同一请求型号', saved['error'])
            self.assertEqual([call['key'][0] for call in client.calls], ['plan'])
            self.assertIsNone(saved['runs'][-1]['plan'])
            self.assertFalse(any(node['phase'] == 'build' for node in saved['runs'][-1]['nodes']))


if __name__ == '__main__':
    unittest.main()
