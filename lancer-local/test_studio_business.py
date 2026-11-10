"""Business workflow tests use deterministic provider fixtures, never paid calls."""
import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from app import Store
from model_catalog import MODELS
from studio import Studio, file_allowed
from studio_business import acceptance_evidence_paths, native_verification_checks, review_policy, validate_business_plan, validate_business_response
from studio_client import StudioClient


def module(mid, work='writing', complexity=1, deps=()):
    return {'id': mid, 'roleId': mid + '-owner', 'title': mid,
            'workType': work, 'priority': 'P1', 'complexity': complexity,
            'skills': {'coding': 1} if work == 'development' else {'writing': 1},
            'requiredCapabilities': ['text'], 'instructions': '完整交付 ' + mid,
            'scope': [mid + '.md'], 'dependsOn': list(deps),
            'reviewRequirements': {'mode': 'none', 'criterionIds': [], 'reason': 'No additional independent review obligation in this fixture.'},
            'acceptance': ['明确说明 ' + mid + ' 的成果']}


def required_checks(mid):
    return [{'id': mid + '-run', 'criterionIds': [mid + '-c1'],
             'command': ['python3', '-B', 'verification/check.py'], 'cwd': '.', 'expectedExitCode': 0}]


def bind_native_result(output, contract):
    if contract and 'requiredChecks' in contract:
        from native_checks import bind_required_checks
        output['nativeCheckEvidence'] = bind_required_checks(contract, output['nativeEvidence'], Path('/fixture-workspace'))
    return output


def native_result(mid='verification', contract=None):
    content = '# 实际执行报告\n\n完成一条本地检查命令。\n'
    output = {'summary': '原生命令已完成，证据仅覆盖当前校验。',
            'files': [{'path': mid + '.md', 'content': content}], 'checks': ['仅报告实际命令覆盖的检查'],
            'questions': [], 'nativeEvidence': {
                'status': 'completed', 'model': 'gpt-6.1-codex', 'receiptRef': 'native/build-' + mid + '/receipt.json', 'inputUnchanged': True,
                'manifestRef': 'native/build-' + mid + '/input-manifest.json',
                'tools': [{'id': 'command-1', 'type': 'commandExecution', 'status': 'completed', 'exitCode': 0,
                           'command': 'python3 -B verification/check.py', 'cwd': '/fixture-workspace'}],
                'checks': [{'name': '验证上游文件', 'status': 'passed', 'evidence': 'command-1 退出 0；检查范围见实际报告'}],
                'verifiedArtifacts': [{'path': mid + '.md', 'bytes': len(content.encode()),
                                       'sha256': hashlib.sha256(content.encode()).hexdigest()}]}}
    return bind_native_result(output, contract)


class BusinessClient(StudioClient):
    def __init__(self, modules=None):
        super().__init__()
        self.modules = modules or [module('brief')]
        self.calls = []
        self.fail_once = None
        self.reject = False
        self.mutate = None
        self.question = False
        self.reask = False
        self.before_final = None

    def available(self):
        return list(MODELS)

    def native_info(self):
        return {'available': True, 'enabled': True, 'capabilities': ['files', 'terminal'],
                'webSearch': {'available': False}, 'collection': {'configured': False}}

    def call(self, model, messages, cancel, on_delta=None):
        memory = json.loads(messages[-1]['content'].split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])
        shared = memory['sharedEvidence']
        stage = shared['businessStage']
        mid = (shared.get('module') or shared.get('currentModule') or {}).get('id')
        key = (stage, mid, model, len(shared.get('previousRounds', [])) + 1 if stage == 'review' else 0)
        with self.lock:
            self.calls.append({'key': key, 'memory': copy.deepcopy(memory)})
            if key == self.fail_once:
                self.fail_once = None
                raise ValueError('fixture interrupted')
        if stage == 'plan':
            result = {'summary': '按成果安排职责', 'businessUnderstanding': '团队交付明确、可核对的成果。缺少的外部事实不编造。',
                      'modules': copy.deepcopy(self.modules), 'integration': '上游交给依赖模块，全部交付后按项验收。', 'risks': [], 'blockingQuestions': []}
            if shared.get('nativeCheckVersion') == 1:
                for item in result['modules']:
                    if 'terminal' in item['requiredCapabilities']:
                        item.setdefault('requiredChecks', required_checks(item['id']))
        elif stage == 'review':
            previous = shared['previousRounds']
            target = previous[-1]['reviews'][0]['findings'][0]['id'] if previous else ''
            result = {'summary': '限定在当前开发模块的检查', 'findings': [{'claim': '接口约束须明确', 'priority': 'P1', 'complexity': 3,
                      'evidence': '该模块要求完整交付，须遵循明确接口。', 'proposal': '在产物中明确接口约束。', 'respondsTo': target}]}
        elif stage == 'reflection':
            result = {'summary': '深层检查交接目标与接口约束的一致性',
                      'reflection': {'assessment': 'deeper_issue', 'issue': '交接目标是否与接收方使用的接口契约一致？',
                                     'basis': '前几轮要求明确接口，但尚未说明接收方如何确认同一接口。',
                                     'proposal': '在已有产物中写明接口约束与接收方验收条件，不新增模块。',
                                     'verification': '对照本模块验收项与产物中的接口约束，逐项确认。'},
                      'findings': [{'claim': '交接目标须匹配接口验收条件', 'priority': 'P1', 'complexity': 3,
                                    'evidence': '固定业务要求完整交付且明确接口。',
                                    'proposal': '在当前产物中明确接收方验收条件。', 'respondsTo': ''}]}
        elif stage == 'decision':
            result = {'summary': '按论据完善当前模块',
                      'decisions': [{'findingId': f['id'], 'decision': 'accept', 'reason': '纳入验收'} for f in shared['evidence']],
                      'moduleUpdate': {'instructions': shared['currentModule']['instructions'] + '；明确接口。',
                                       'acceptance': shared['currentModule']['acceptance']}}
        elif stage == 'build':
            if self.question and mid == 'downstream' and (not shared.get('clarificationsUsed') or self.reask):
                result = {'summary': '需要上游确认版本', 'files': [], 'questions': [{'toRole': 'upstream-owner',
                          'question': '应采用哪个文件版本？', 'evidence': '本模块依赖 upstream 交付。'}]}
            else:
                result = {'summary': '已完整交付 ' + mid, 'files': [{'path': mid + '.md', 'content': '# ' + mid + '\n\n明确成果与接口约束。'}],
                          'notes': [], 'checks': ['仅检查文本内容，未运行应用'], 'questions': []}
        elif stage == 'clarification':
            result = {'summary': '使用当前上游版本', 'answer': '采用此次 upstream.md。', 'evidence': '本轮上游交付文件 upstream.md', 'unresolved': False}
        elif stage == 'quality':
            path = next(item['path'] for item in shared['reviewFiles'] if file_allowed(item['path'], shared['module']['scope']))
            result = {'summary': '独立核对实际文件，未执行运行测试',
                      'criteria': [{'criterionId': criterion['id'], 'status': 'met', 'reason': '文件明确成果与接口约束。',
                                    'evidence': [{'path': path, 'startLine': 1, 'endLine': 1}]}
                                   for criterion in shared['qualityContract']['criteria']], 'findings': []}
        elif stage == 'integration':
            if self.before_final:
                action, self.before_final = self.before_final, None
                action()
            acceptance = []
            for item in shared['plan']['modules']:
                paths = [f['path'] for f in shared['modules'][item['id']]['files']]
                acceptance.append({'moduleId': item['id'], 'accepted': not self.reject, 'evidence': paths,
                                   'unmet': ['要求未满足'] if self.reject else [],
                                   'criteria': [{'criterion': c, 'status': 'unmet' if self.reject else 'met', 'evidence': paths} for c in item['acceptance']]})
            result = {'summary': '按产物逐项验收', 'accepted': not self.reject, 'moduleAcceptance': acceptance,
                      'files': [], 'risks': ['未运行生成程序'], 'nextSteps': []}
        else:
            raise AssertionError(stage)
        if self.mutate:
            result = self.mutate(stage, result)
        return result, {'mode': 'fixture', 'requestedModel': model, 'returnedModel': model}


class BusinessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = BusinessClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('业务测试', '明确分工并验收成果', '不要虚构已运行的工具。')['id']

    def tearDown(self):
        for thread in self.studio.threads.values():
            thread.join(15)
        self.tmp.cleanup()

    def run_task(self, session=None, resume=False, coordinator_model=None, team_models=None):
        session = session or self.studio.create_session(self.pid, '业务任务', '整理项目资料并输出明确成果', 'general')
        session['workflowVersion'] = 2
        self.studio.save(session)
        self.studio.start(self.pid, session['id'], 'live', resume, coordinator_model, team_models)
        self.studio.threads[session['id']].join(15)
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        return self.studio.load(self.pid, session['id'])

    def integration_fixture(self):
        modules = [dict(module('dashboard'), assignment={'model': 'sol'}),
                   dict(module('verification', 'general', deps=['dashboard']), assignment={'model': 'native_codex'}, requiredCapabilities=['terminal', 'files']),
                   dict(module('guide'), assignment={'model': 'gemini'})]
        outputs = {'dashboard': {'files': [{'path': 'dashboard.md', 'content': 'dashboard'}]},
                   'verification': native_result(), 'guide': {'files': [{'path': 'guide.md', 'content': 'guide'}]}}
        assessments = [{'moduleId': m['id'], 'accepted': True, 'evidence': [m['id'] + '.md'], 'unmet': [],
                        'criteria': [{'criterion': c, 'status': 'met', 'evidence': [m['id'] + '.md']} for c in m['acceptance']]} for m in modules]
        assessments[0]['evidence'].append('verification.md')
        assessments[0]['criteria'][0]['evidence'] = ['verification.md']
        return ({'summary': '产物和实际执行证据联合验收', 'accepted': True, 'moduleAcceptance': assessments, 'files': []},
                {'businessStage': 'integration', 'plan': {'modules': modules}, 'modules': outputs,
                 'checks': native_verification_checks(modules[1], outputs['verification'])})

    def test_ordinary_task_one_plan_worker_and_acceptance(self):
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual([n['phase'] for n in run['nodes']], ['draft', 'build', 'integration'])
        self.assertEqual(run['completedRounds'], 0)
        self.assertEqual(len([c for c in self.client.calls if c['key'][0] == 'plan']), 1)
        self.assertFalse(run['discussions'])
        self.assertFalse(run['plan']['modules'][0]['reviewPolicy']['required'])
        self.assertEqual(run['final']['moduleAcceptance'][0]['basis'], 'artifact_review')
        self.assertIn('没有运行', run['final']['verificationNotice'])
        self.assertTrue((self.store.current(self.pid)[1] / 'brief.md').exists())

    def test_mixed_workflow_discusses_complex_tasks_but_reviews_only_development_artifacts(self):
        self.client.modules = [module('research', 'research', 5), module('software', 'development', 4), module('smallfix', 'development', 1)]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        review_nodes = [n for n in run['nodes'] if n['phase'] == 'review']
        self.assertEqual(len([n for n in review_nodes if n.get('discussionKind') != 'reflection']), 15)
        self.assertEqual(len([n for n in review_nodes if n.get('discussionKind') == 'reflection']), 6)
        self.assertEqual({n['moduleId'] for n in review_nodes}, {'software', 'research'})
        self.assertEqual({n['round'] for n in review_nodes}, {1, 2, 3})
        self.assertEqual(run['moduleReviews']['software']['completedRounds'], 2)
        self.assertEqual(run['moduleReviews']['research']['completedRounds'], 3)
        self.assertEqual({n['moduleId'] for n in run['nodes'] if n['phase'] == 'verification'}, {'software'})
        self.assertEqual(len([n for n in run['nodes'] if n['phase'] == 'decision' and not n.get('fixedPrompt')]), 2)
        self.assertEqual(len([n for n in run['nodes'] if n.get('fixedPrompt')]), 2)
        self.assertTrue(all(d['nodeId'] and d['eventSeq'] > 0 for d in run['discussions']))
        round2 = next(c for c in self.client.calls if c['key'][0] == 'review' and c['key'][1] == 'software' and c['key'][3] == 2)
        self.assertEqual(len(round2['memory']['sharedEvidence']['previousRounds']), 1)
        self.assertEqual(round2['memory']['sharedEvidence']['currentModule']['id'], 'software')

    def test_research_cannot_disguise_itself_as_software_design_risk(self):
        self.client.modules = [dict(module('research', 'research', 5), reviewRisk={'softwareDesign': True, 'reason': '想让大家讨论'})]
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('常规业务模块', session['error'])
        self.assertEqual(len(self.client.calls), 1)

    def test_explicit_software_design_risk_is_scoped_and_reasoned(self):
        item = module('architecture', 'design', 2)
        item.update(skills={'coding': .5, 'review': .5}, reviewRisk={'softwareDesign': True, 'reason': '跨服务事务一致性风险'})
        self.assertTrue(review_policy(item)['required'])
        self.assertIn('跨服务', review_policy(item)['reason'])

    def test_discussion_limits_are_explicit_and_legacy_policy_is_unchanged(self):
        for complexity, expected in ((1, 0), (2, 0), (3, 1), (4, 2), (5, 3)):
            with self.subTest(complexity=complexity):
                item = module('research', 'research', complexity)
                self.assertEqual(review_policy(item, 1)['roundLimit'], expected)
                self.assertFalse(review_policy(item)['required'])
                self.assertEqual(review_policy(module('picture', 'image', complexity), 1)['roundLimit'], 0)
        risk = dict(module('design', 'design', 1), skills={'review': 1},
                    reviewRisk={'softwareDesign': True, 'reason': '跨模块权限边界'})
        self.assertEqual(review_policy(risk, 1)['roundLimit'], 1)
        self.assertEqual(review_policy(risk)['roundLimit'], 3)

    def test_selected_coordinator_routes_only_within_team_and_can_assign_opus(self):
        self.client.modules = [module('writing', 'writing', 3), module('software', 'development', 3)]
        team = ['gemini', 'opus', 'sol']
        session = self.run_task(coordinator_model='gemini', team_models=team)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['coordinatorModel'], 'gemini')
        self.assertEqual(run['teamModels'], team)
        self.assertEqual({n['agent'] for n in run['nodes'] if n['phase'] in {'draft', 'decision', 'integration'}}, {'gemini'})
        self.assertEqual({n['moduleId'] for n in run['nodes'] if n['phase'] == 'verification'}, {'software'})
        self.assertTrue(all(n['agent'] in team for n in run['nodes']))
        writing = next(m for m in run['plan']['modules'] if m['id'] == 'writing')
        self.assertEqual(writing['assignment']['model'], 'opus')
        self.assertFalse(writing['artifactReview'])
        for item in run['plan']['modules']:
            self.assertEqual(set(item['discussionModels']), {'opus', 'sol'})
        planning = self.client.calls[0]['memory']['sharedEvidence']
        self.assertEqual({m['id'] for m in planning['catalog']['models']}, set(team))
        self.assertEqual(planning['executionCapabilities'], ['text'])
        review = next(n for n in run['nodes'] if n['phase'] == 'verification')
        self.assertEqual(review['agent'], 'opus')
        self.assertNotEqual(MODELS[review['agent']]['family'], MODELS['sol']['family'])

    def test_single_model_team_handles_simple_work_without_discussion(self):
        session = self.run_task(coordinator_model='gemini', team_models=['gemini'])
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual([n['phase'] for n in run['nodes']], ['draft', 'build', 'integration'])
        self.assertEqual({n['agent'] for n in run['nodes']}, {'gemini'})
        self.assertEqual(run['roundLimit'], 0)

    def test_complex_work_rejects_insufficient_selected_team_before_execution(self):
        self.client.modules = [module('research', 'research', 3)]
        session = self.run_task(coordinator_model='gemini', team_models=['gemini', 'opus'])
        self.assertEqual(session['status'], 'failed')
        self.assertIn('至少需要主模型之外两个', session['error'])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0]['key'][2], 'gemini')

    def test_resume_keeps_selected_team_and_per_module_discussion_limit(self):
        self.client.modules = [module('research', 'research', 4)]
        self.client.fail_once = ('review', 'research', 'sol', 2)
        team = ['gemini', 'opus', 'sol']
        session = self.run_task(coordinator_model='gemini', team_models=team)
        self.assertEqual(session['status'], 'failed')
        progress = copy.deepcopy(session['runs'][-1]['moduleReviews']['research'])
        self.assertEqual(progress['roundLimit'], 2)
        session = self.run_task(session, True, 'opus', ['opus', 'deepseek'])
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['teamModels'], team)
        self.assertEqual(run['coordinatorModel'], 'gemini')
        reviews = [c for c in self.client.calls if c['key'][0] == 'review']
        self.assertEqual(len(reviews), 5)
        self.assertEqual({c['key'][3] for c in reviews}, {1, 2})
        self.assertEqual(run['moduleReviews']['research']['models'], progress['models'])

    def test_legacy_workflow_two_resume_retains_three_round_development_policy(self):
        self.client.modules = [module('research', 'research', 5), module('software', 'development', 3)]
        session = self.studio.create_session(self.pid, '旧流程', '整理项目成果', 'general')
        session['status'] = 'interrupted'
        session['runs'] = [{'id': 'legacy-run', 'mode': 'live', 'status': 'interrupted',
            'request': session['messages'][0]['text'], 'classification': session['classification'],
            'workflowVersion': 2, 'collaborationVersion': 1, 'qualityVersion': 1, 'repairLimit': 2,
            'projectRevision': self.store.current(self.pid)[0]['revision'],
            'projectSnapshot': self.studio.meta(self.pid), 'teamSnapshot': {},
            'reviewers': [], 'roundLimit': 3, 'completedRounds': 0, 'nodes': [], 'plan': None,
            'artifacts': [], 'started': session['created']}]
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertNotIn('orchestrationVersion', run)
        reviews = [n for n in run['nodes'] if n['phase'] == 'review']
        self.assertEqual(len(reviews), 9)
        self.assertEqual({n['moduleId'] for n in reviews}, {'software'})
        self.assertEqual({n['round'] for n in reviews}, {1, 2, 3})
        self.assertTrue(all('artifactReview' not in item for item in run['plan']['modules']))

    def test_read_only_audit_plan_cannot_write_outside_report_root(self):
        item = module('source')
        plan = {'summary': '审查', 'businessUnderstanding': '只读检查源码', 'modules': [item], 'integration': '汇总'}
        with self.assertRaisesRegex(ValueError, '只读审查'):
            validate_business_plan(copy.deepcopy(plan), ['opus', 'sol'], audit_output_root='reviews/run1/')
        item['scope'] = ['reviews/run1/report.md']
        validate_business_plan(plan, ['opus', 'sol'], audit_output_root='reviews/run1/')

    def test_web_search_plan_uses_actual_capabilities_without_requiring_terminal_checks(self):
        item = dict(module('research', 'research'), requiredCapabilities=['text', 'web_search'])
        plan = {'summary': '调研', 'businessUnderstanding': '从真实检索收集资料', 'modules': [item], 'integration': '汇总'}
        with self.assertRaisesRegex(ValueError, '尚未接通'):
            validate_business_plan(copy.deepcopy(plan), ['opus', 'native_codex'], 1,
                                   execution_capabilities=['text', 'files', 'terminal'])
        result = validate_business_plan(plan, ['opus', 'native_codex'], 1,
                                        execution_capabilities=['text', 'files', 'terminal', 'web_search'])
        self.assertEqual(result['modules'][0]['nativeCheckVersion'], 1)
        self.assertEqual(result['modules'][0]['requiredChecks'], [])

    def test_missing_tool_capability_fails_before_execution(self):
        self.client.modules = [dict(module('browser'), requiredCapabilities=['browser'])]
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('尚未接通', session['error'])
        self.assertEqual(len(self.client.calls), 1)

    def test_native_capabilities_are_available_only_with_native_executor(self):
        for capabilities in (['files'], ['terminal'], ['text', 'files', 'terminal']):
            item = dict(module('verification', 'general'), requiredCapabilities=capabilities)
            plan = {'summary': '验证文件', 'businessUnderstanding': '检查真实产物。', 'modules': [item],
                    'integration': '汇总报告', 'blockingQuestions': []}
            with self.assertRaisesRegex(ValueError, '尚未接通'):
                validate_business_plan(copy.deepcopy(plan), ['opus', 'sol'])
            validate_business_plan(copy.deepcopy(plan), ['opus', 'sol', 'native_codex'])
            self.assertFalse(review_policy(item)['required'])
        for capability in ('browser', 'plugins'):
            plan['modules'][0]['requiredCapabilities'] = [capability]
            with self.assertRaisesRegex(ValueError, '尚未接通'):
                validate_business_plan(copy.deepcopy(plan), ['opus', 'native_codex'])

    def test_native_tools_and_model_reports_have_separate_acceptance_evidence(self):
        self.client.modules = [module('brief'), dict(module('verification', 'general', deps=['brief']),
                                                   requiredCapabilities=['text', 'files', 'terminal'])]
        captured = []
        def execute(studio, session, run, node, context, messages, shared, cancel, delta):
            captured.append(copy.deepcopy(shared))
            return native_result(contract=shared.get('executionContract')), {'mode': 'fixture-native', 'requestedModel': 'native_codex', 'returnedModel': 'gpt-6.1-codex'}
        def cite_verifier(stage, result):
            if stage == 'integration':
                brief = next(a for a in result['moduleAcceptance'] if a['moduleId'] == 'brief')
                brief['evidence'].append('verification.md')
                brief['criteria'][0]['evidence'] = ['verification.md']
            return result
        self.client.mutate = cite_verifier
        with patch('studio_native.execute_native', side_effect=execute):
            session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['completedRounds'], 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['upstream']['brief']['files'][0]['path'], 'brief.md')
        plan_shared = self.client.calls[0]['memory']['sharedEvidence']
        self.assertIn('terminal', plan_shared['executionCapabilities'])
        self.assertIn('files', plan_shared['executionCapabilities'])
        actual = next(c for c in run['checks'] if c.get('basis') == 'native_required_checks')
        self.assertEqual(actual['bindingEvidence']['checks'][0]['status'], 'passed')
        self.assertEqual(actual['bindingEvidence']['checks'][0]['toolId'], 'command-1')
        self.assertTrue(actual['pass'])
        self.assertEqual(actual['receiptRef'], 'native/build-verification/receipt.json')
        self.assertEqual(captured[0]['executionContract']['requiredChecks'], required_checks('verification'))
        assessment = next(a for a in run['final']['moduleAcceptance'] if a['moduleId'] == 'verification')
        self.assertEqual(assessment['basis'], 'artifact_review')
        self.assertEqual(assessment['toolEvidence']['basis'], 'native_receipt')
        self.assertIn('未附回执', run['final']['verificationNotice'])
        brief = next(a for a in run['final']['moduleAcceptance'] if a['moduleId'] == 'brief')
        self.assertEqual(brief['verificationEvidence'], [{'moduleId': 'verification', 'receiptRef': 'native/build-verification/receipt.json',
                                                         'artifactPaths': ['verification.md'], 'basis': 'native_receipt'}])
        final_shared = next(c['memory']['sharedEvidence'] for c in self.client.calls if c['key'][0] == 'integration')
        self.assertEqual(final_shared['acceptanceEvidencePathsByModule']['brief'], ['brief.md', 'verification.md'])
        self.assertEqual(final_shared['acceptanceEvidencePathsByModule']['verification'], ['brief.md', 'verification.md'])

    def test_acceptance_can_use_verified_direct_downstream_qa_with_own_anchor(self):
        result, shared = self.integration_fixture()
        validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))
        assessment = result['moduleAcceptance'][0]
        self.assertEqual(assessment['basis'], 'artifact_review')
        self.assertEqual(assessment['criteria'][0]['evidence'], ['verification.md'])
        self.assertEqual(assessment['verificationEvidence'][0]['moduleId'], 'verification')

    def test_downstream_qa_evidence_requires_own_module_file_anchor(self):
        result, shared = self.integration_fixture()
        result['moduleAcceptance'][0]['evidence'] = ['verification.md']
        with self.assertRaisesRegex(ValueError, '至少引用该模块'):
            validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))

    def test_direct_upstream_handoff_files_are_allowed_but_not_runtime_proof(self):
        result, shared = self.integration_fixture()
        shared['plan']['modules'][0]['dependsOn'] = ['guide']
        assessment = result['moduleAcceptance'][0]
        assessment['evidence'].append('guide.md')
        assessment['criteria'][0]['evidence'] = ['guide.md']
        allowed = acceptance_evidence_paths(shared['plan'], shared['modules'])
        self.assertEqual(allowed['dashboard'], ['dashboard.md', 'guide.md', 'verification.md'])
        validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))
        self.assertEqual(assessment['basis'], 'artifact_review')
        self.assertEqual(assessment['handoffEvidence'], [{'moduleId': 'guide', 'artifactPaths': ['guide.md'], 'basis': 'upstream_handoff'}])
        self.assertNotIn('toolEvidence', assessment)
        result['moduleAcceptance'][0]['evidence'] = ['guide.md']
        with self.assertRaisesRegex(ValueError, '至少引用该模块'):
            validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))

    def test_transitive_upstream_files_and_forged_allowlist_are_not_accepted(self):
        result, shared = self.integration_fixture()
        # verification receives dashboard; dashboard receives guide. The native
        # verifier does not receive guide through that transitive edge alone.
        shared['plan']['modules'][0]['dependsOn'] = ['guide']
        result['moduleAcceptance'][1]['evidence'].append('guide.md')
        shared['acceptanceEvidencePathsByModule'] = {'verification': ['verification.md', 'guide.md']}
        with self.assertRaisesRegex(ValueError, '实际交付文件'):
            validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))

    def test_downstream_qa_must_have_real_successful_unchanged_input_evidence(self):
        cases = ('no_dependency', 'transitive_only', 'not_native', 'command_failed', 'command_missing',
                 'reported_failed', 'input_changed', 'input_unknown', 'hash_changed', 'receipt_missing')
        for case in cases:
            with self.subTest(case=case):
                result, shared = self.integration_fixture()
                verifier = shared['plan']['modules'][1]
                output = shared['modules']['verification']
                receipt = output['nativeEvidence']
                if case == 'no_dependency':
                    verifier['dependsOn'] = []
                elif case == 'transitive_only':
                    verifier['dependsOn'] = ['guide']
                    shared['plan']['modules'][2]['dependsOn'] = ['dashboard']
                elif case == 'not_native':
                    verifier['assignment']['model'] = 'sol'
                elif case == 'command_failed':
                    receipt['tools'][0]['exitCode'] = 1
                elif case == 'command_missing':
                    receipt['tools'] = []
                elif case == 'reported_failed':
                    receipt['checks'][0]['status'] = 'failed'
                elif case == 'input_changed':
                    receipt['inputUnchanged'] = False
                elif case == 'input_unknown':
                    del receipt['inputUnchanged']
                elif case == 'hash_changed':
                    output['files'][0]['content'] += 'changed'
                elif case == 'receipt_missing':
                    del receipt['receiptRef']
                with self.assertRaisesRegex(ValueError, '原生下游验证文件'):
                    validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))

    def test_qa_evidence_does_not_allow_other_modules_or_fabricated_files(self):
        for field in ('module', 'criterion'):
            for path in ('guide.md', 'does-not-exist.md'):
                with self.subTest(field=field, path=path):
                    result, shared = self.integration_fixture()
                    assessment = result['moduleAcceptance'][0]
                    if field == 'module':
                        assessment['evidence'].append(path)
                    else:
                        assessment['criteria'][0]['evidence'].append(path)
                    with self.assertRaises(ValueError):
                        validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))

    def test_qa_evidence_cannot_remove_original_acceptance_criteria(self):
        result, shared = self.integration_fixture()
        shared['plan']['modules'][0]['acceptance'].append('保留第二条原定验收标准')
        with self.assertRaisesRegex(ValueError, '每一条原定'):
            validate_business_response({'agent': 'opus'}, result, shared, list(MODELS))

    def test_native_failure_or_unrun_checks_block_merge_despite_false_acceptance(self):
        cases = ('command_failed', 'command_missing', 'reported_failed', 'reported_not_run', 'status_failed')
        for case in cases:
            with self.subTest(case=case):
                self.client.modules = [dict(module('verification', 'general'), requiredCapabilities=['text', 'files', 'terminal'])]
                output = native_result()
                evidence = output['nativeEvidence']
                if case == 'command_failed':
                    evidence['tools'][0]['exitCode'] = 1
                elif case == 'command_missing':
                    evidence['tools'] = []
                elif case == 'status_failed':
                    evidence['status'] = 'failed'
                else:
                    evidence['checks'][0]['status'] = 'failed' if case == 'reported_failed' else 'not_run'
                before = self.store.current(self.pid)[0]['revision']
                def execute(studio, session, run, node, context, messages, shared, cancel, delta):
                    return bind_native_result(copy.deepcopy(output), shared['executionContract']), {'mode': 'fixture-native'}
                with patch('studio_native.execute_native', side_effect=execute):
                    session = self.run_task()
                self.assertEqual(session['status'], 'failed', session.get('error'))
                self.assertIn('失败或未执行', session['error'])
                self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
                run = session['runs'][-1]
                self.assertNotIn('mergedRevision', run)
                self.assertTrue(any(not c['pass'] for c in run['checks']))
                self.assertTrue(run['artifacts'])
                self.assertEqual(run['artifacts'][0]['state'], 'candidate')

    def test_native_artifact_hash_and_receipt_are_checked_before_node_completion(self):
        self.client.modules = [dict(module('verification', 'general'), requiredCapabilities=['files', 'terminal'])]
        output = native_result()
        output['files'][0]['content'] += ' changed after verification'
        with patch('studio_native.execute_native', return_value=(output, {'mode': 'fixture-native'})):
            session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('文件指纹', session['error'])
        self.assertFalse(any(n['phase'] == 'integration' for n in session['runs'][-1]['nodes']))
        output = native_result()
        del output['nativeEvidence']['receiptRef']
        with self.assertRaisesRegex(ValueError, '工具回执'):
            native_verification_checks(self.client.modules[0], output)

    def test_native_receipt_cannot_hide_failed_command_behind_successful_command(self):
        item = dict(module('verification', 'general'), requiredCapabilities=['terminal'])
        output = native_result()
        output['nativeEvidence']['tools'].append({'id': 'failed-1', 'type': 'commandExecution', 'status': 'completed', 'exitCode': 2})
        checks = native_verification_checks(item, output)
        receipt = next(c for c in checks if c['basis'] == 'native_tool_receipt')
        self.assertFalse(receipt['pass'])
        self.assertEqual(receipt['commandCount'], 2)
        self.assertEqual(receipt['failedCommands'], 1)

    def test_questions_about_native_receipts_are_directed_to_coordinator(self):
        upstream = dict(module('upstream'), assignment={'model': 'native_codex'})
        downstream = module('downstream', deps=['upstream'])
        shared = {'businessStage': 'build', 'module': downstream, 'plan': {'modules': [upstream, downstream]}}
        result = {'summary': '需要解释工具范围', 'files': [], 'questions': [{'toRole': 'upstream-owner',
                  'question': '该检查覆盖什么？', 'evidence': 'upstream 由原生执行者交付。'}]}
        with self.assertRaisesRegex(ValueError, 'coordinator'):
            validate_business_response({'agent': 'sol'}, result, shared, list(MODELS))
        result['questions'][0]['toRole'] = 'coordinator'
        validate_business_response({'agent': 'sol'}, result, shared, list(MODELS))

    def test_review_cannot_reference_other_modules_or_future_findings(self):
        self.client.modules = [module('software', 'development', 4)]
        def wrong_reference(stage, result):
            if stage == 'review':
                result['findings'][0]['respondsTo'] = 'other-module-future-finding'
            return result
        self.client.mutate = wrong_reference
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('本模块上一轮', session['error'])
        self.assertFalse(any(n['phase'] == 'build' for n in session['runs'][-1]['nodes']))

    def test_failed_acceptance_preserves_candidates_without_merging(self):
        self.client.reject = True
        before = self.store.current(self.pid)[0]['revision']
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        run = session['runs'][-1]
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        self.assertTrue(run['artifacts'])
        self.assertEqual(run['artifacts'][0]['state'], 'candidate')
        self.assertTrue((self.studio.project_dir(self.pid) / session['id'] / 'artifacts' / run['id'] / 'brief.md').exists())

    def test_false_acceptance_or_fabricated_evidence_is_rejected(self):
        def fabricate(stage, result):
            if stage == 'integration':
                result['moduleAcceptance'][0]['evidence'] = ['not-delivered.md']
            return result
        self.client.mutate = fabricate
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('实际交付文件', session['error'])
        self.assertFalse(any(n['id'] == 'integration' and n['status'] == 'done' for n in session['runs'][-1]['nodes']))

    def test_missing_acceptance_criteria_cannot_be_marked_complete(self):
        def remove(stage, result):
            if stage == 'integration':
                result['moduleAcceptance'][0]['criteria'] = []
            return result
        self.client.mutate = remove
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('每一条', session['error'])

    def test_resume_does_not_repeat_completed_review_rounds(self):
        self.client.modules = [module('software', 'development', 4)]
        self.client.fail_once = ('build', 'software', 'sol', 0)
        session = self.run_task()
        self.assertEqual(session['status'], 'failed', session.get('error'))
        calls = len([c for c in self.client.calls if c['key'][0] == 'review'])
        self.assertEqual(calls, 6)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(len([c for c in self.client.calls if c['key'][0] == 'review']), 6)
        self.assertEqual(len({d['id'] for d in session['runs'][-1]['discussions']}), len(session['runs'][-1]['discussions']))

    def test_single_directed_handoff_question_returns_to_worker(self):
        self.client.modules = [module('upstream'), module('downstream', deps=['upstream'])]
        self.client.question = True
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(len(run['discussions']), 2)
        question, answer = run['discussions']
        self.assertEqual(question['toRole'], 'upstream-owner')
        self.assertEqual(answer['respondsTo'], question['id'])
        self.assertEqual(answer['toRole'], 'downstream-owner')
        self.assertTrue(any(n['id'] == 'build-downstream-resolved' for n in run['nodes']))

    def test_worker_cannot_open_second_clarification_round(self):
        self.client.modules = [module('upstream'), module('downstream', deps=['upstream'])]
        self.client.question = self.client.reask = True
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('只能在首次交付之前', session['error'])
        self.assertEqual(len([c for c in self.client.calls if c['key'][0] == 'clarification']), 1)

    def test_project_change_before_acceptance_preserves_external_file(self):
        def change():
            self.store.commit(self.pid, 'external', 'test', lambda s, w, old: ((w / 'external.md').write_text('keep'), {'detail': 'external'})[1])
        self.client.before_final = change
        session = self.run_task()
        self.assertEqual(session['status'], 'conflict')
        self.assertEqual((self.store.current(self.pid)[1] / 'external.md').read_text(), 'keep')
        self.assertTrue(session['runs'][-1]['artifacts'])

    def test_parallel_module_review_histories_remain_independent(self):
        self.client.modules = [dict(module('api', 'development', 3), roleId='engineering'),
                               dict(module('ui', 'development', 4), roleId='engineering')]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(len(session['agents']['reviewer-api-sol']['turns']), 1)
        self.assertEqual(len(session['agents']['reviewer-ui-sol']['turns']), 3)
        for mid in ('api', 'ui'):
            assigned = next(m['assignment']['model'] for m in session['runs'][-1]['plan']['modules'] if m['id'] == mid)
            self.assertIn('worker-engineering-' + mid + '-' + assigned, session['agents'])

    def test_development_review_receives_real_upstream_handoff(self):
        self.client.modules = [module('research', 'research'), module('software', 'development', 4, deps=['research'])]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        first = next(n for n in run['nodes'] if n['phase'] == 'review' and n['round'] == 1)
        self.assertIn('build-research', first['deps'])
        request = next(c for c in self.client.calls if c['key'][0] == 'review')
        self.assertEqual(request['memory']['sharedEvidence']['upstream']['research']['files'][0]['path'], 'research.md')

    def test_no_findings_converges_without_forcing_three_rounds(self):
        self.client.modules = [module('software', 'development', 4)]
        self.client.mutate = lambda stage, result: dict(result, findings=[]) if stage == 'review' else result
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['moduleReviews']['software']['completedRounds'], 1)
        self.assertEqual(len([n for n in run['nodes'] if n['phase'] == 'review' and n.get('discussionKind') != 'reflection']), 3)
        self.assertEqual(len([n for n in run['nodes'] if n.get('discussionKind') == 'reflection']), 3)
        self.assertIn('提前', run['moduleReviews']['software']['finishReason'])

    def test_metadata_write_failure_after_commit_recovers_once_without_model_calls(self):
        original = self.studio._write
        failed = []
        def fail_project_memory(path, obj):
            if path.name == 'project.json' and obj.get('memory') and not failed:
                failed.append(True)
                raise OSError('injected project metadata write failure')
            return original(path, obj)
        self.studio._write = fail_project_memory
        before = self.store.current(self.pid)[0]['revision']
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        revision = self.store.current(self.pid)[0]['revision']
        self.assertNotEqual(revision, before)
        self.assertEqual(session['runs'][-1]['mergedRevision'], revision)
        calls = len(self.client.calls)
        self.client.available = lambda: []
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(self.store.current(self.pid)[0]['revision'], revision)
        self.assertEqual(len(session['memory']), 1)
        self.assertEqual(len(self.studio.meta(self.pid)['memory']), 1)
        self.assertEqual(len([e for e in session['events'] if e['type'] == 'project_merged']), 1)

    def test_lost_commit_return_recovers_from_store_event_without_second_commit(self):
        original = self.store.commit
        commits = []
        def lose_commit_return(*args, **kwargs):
            result = original(*args, **kwargs)
            commits.append(result['revision'])
            if len(commits) == 1:
                raise OSError('injected failure after Store publication, before return')
            return result
        self.store.commit = lose_commit_return
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertNotIn('mergedRevision', session['runs'][-1])
        self.assertTrue(session['runs'][-1]['commitIntent'])
        calls = len(self.client.calls)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(len(commits), 1)
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(session['runs'][-1]['mergedRevision'], commits[0])
        self.assertEqual(len(self.studio.meta(self.pid)['memory']), 1)

    def test_failure_before_commit_reuses_verified_candidates(self):
        original = self.store.commit
        attempts = []
        def fail_before_commit(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError('injected failure before Store commit')
            return original(*args, **kwargs)
        self.store.commit = fail_before_commit
        before = self.store.current(self.pid)[0]['revision']
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        calls = len(self.client.calls)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(len(self.client.calls), calls)
        self.assertNotEqual(self.store.current(self.pid)[0]['revision'], before)

    def test_final_session_save_failure_does_not_duplicate_project_memory(self):
        original = self.studio.save
        failed = []
        def fail_final_save(session):
            if session['status'] == 'completed' and not failed:
                failed.append(True)
                raise OSError('injected final session save failure')
            return original(session)
        self.studio.save = fail_final_save
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        revision = self.store.current(self.pid)[0]['revision']
        self.assertEqual(len(self.studio.meta(self.pid)['memory']), 1)
        calls = len(self.client.calls)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(self.store.current(self.pid)[0]['revision'], revision)
        self.assertEqual(len(session['memory']), 1)
        self.assertEqual(len(self.studio.meta(self.pid)['memory']), 1)
        self.assertEqual(len([e for e in session['events'] if e['type'] == 'merge']), 1)

    def test_case_variant_integration_file_cannot_overwrite_module_file(self):
        self.client.modules = [module('readme')]
        def conflict(stage, result):
            if stage == 'integration':
                result['files'] = [{'path': 'README.md', 'content': 'must not replace readme.md'}]
            return result
        self.client.mutate = conflict
        before = self.store.current(self.pid)[0]['revision']
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('路径存在', session['error'])
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        self.assertFalse(any(n['id'] == 'integration' and n['status'] == 'done' for n in session['runs'][-1]['nodes']))

    def test_unicode_normalization_aliases_are_rejected_in_module_scopes(self):
        self.client.modules = [dict(module('one'), scope=['caf\u00e9.md']), dict(module('two'), scope=['cafe\u0301.md'])]
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('Unicode', session['error'])
        self.assertEqual(len(self.client.calls), 1)

    def test_file_parent_conflicts_are_rejected_before_saving_candidates(self):
        self.client.modules = [dict(module('files'), scope=['docs/'])]
        def conflict(stage, result):
            if stage == 'build':
                result['files'] = [{'path': 'docs/Report', 'content': 'file'}, {'path': 'docs/report/a.md', 'content': 'child'}]
            return result
        self.client.mutate = conflict
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('父子冲突', session['error'])
        self.assertFalse(session['runs'][-1]['artifacts'])

    def test_unpublished_revision_directory_is_not_false_completion_evidence(self):
        original = self.store.publish
        orphan = []
        def fail_before_head(directory, stage, revision):
            if not orphan:
                os.replace(stage, directory / 'revisions' / revision)
                orphan.append(revision)
                raise OSError('injected failure before HEAD publication')
            return original(directory, stage, revision)
        self.store.publish = fail_before_head
        before = self.store.current(self.pid)[0]['revision']
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        calls = len(self.client.calls)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertNotEqual(session['runs'][-1]['mergedRevision'], orphan[0])
        self.assertNotEqual(self.store.current(self.pid)[0]['revision'], before)
        self.assertEqual(len(self.client.calls), calls)

    def test_recovery_does_not_overwrite_a_later_project_revision(self):
        original = self.studio._write
        failed = []
        def fail_project_memory(path, obj):
            if path.name == 'project.json' and obj.get('memory') and not failed:
                failed.append(True)
                raise OSError('injected metadata failure')
            return original(path, obj)
        self.studio._write = fail_project_memory
        session = self.run_task()
        committed = self.store.current(self.pid)[0]['revision']
        later = self.store.commit(self.pid, 'later change', 'test', lambda s, w, old: ((w / 'later.md').write_text('keep'), {'detail': 'later'})[1])
        calls = len(self.client.calls)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(session['runs'][-1]['mergedRevision'], committed)
        self.assertEqual(self.store.current(self.pid)[0]['revision'], later['revision'])
        self.assertEqual((self.store.current(self.pid)[1] / 'later.md').read_text(), 'keep')
        self.assertEqual(len(self.client.calls), calls)

    def test_tampered_pending_artifact_is_not_committed_on_resume(self):
        original = self.store.commit
        attempts = []
        def fail_before_commit(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError('injected failure before commit')
            return original(*args, **kwargs)
        self.store.commit = fail_before_commit
        before = self.store.current(self.pid)[0]['revision']
        session = self.run_task()
        run = session['runs'][-1]
        artifact = self.studio.project_dir(self.pid) / session['id'] / 'artifacts' / run['id'] / 'brief.md'
        artifact.write_text('changed after acceptance')
        calls = len(self.client.calls)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'failed')
        self.assertIn('产物已变化', session['error'])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        self.assertEqual(len(self.client.calls), calls)

    def test_midrun_business_configuration_change_blocks_first_commit(self):
        def change_business():
            metadata = self.studio.meta(self.pid)
            configuration = copy.deepcopy(metadata['teamConfig'])
            configuration['businessBrief'] = '新的业务规则：此版本仅面向内部用户。'
            self.studio.update_project(self.pid, metadata['instructions'], metadata['version'], configuration)
        self.client.before_final = change_business
        before = self.store.current(self.pid)[0]['revision']
        session = self.run_task()
        self.assertEqual(session['status'], 'conflict')
        self.assertIn('业务说明', session['error'])
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        self.assertTrue(session['runs'][-1]['artifacts'])
        self.assertEqual(self.studio.meta(self.pid)['memory'], [])

    def test_recovery_of_committed_delivery_preserves_new_project_rules(self):
        original = self.studio._write
        failed = []
        def fail_project_memory(path, obj):
            if path.name == 'project.json' and obj.get('memory') and not failed:
                failed.append(True)
                raise OSError('injected metadata failure')
            return original(path, obj)
        self.studio._write = fail_project_memory
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        revision = self.store.current(self.pid)[0]['revision']
        metadata = self.studio.meta(self.pid)
        configuration = copy.deepcopy(metadata['teamConfig'])
        configuration['businessBrief'] = '后续任务采用更新后的业务说明。'
        configuration['roleGuides']['writing'] = '后续文稿用新的术语规范。'
        self.studio.update_project(self.pid, '新的长期约定', metadata['version'], configuration)
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        metadata = self.studio.meta(self.pid)
        self.assertEqual(metadata['instructions'], '新的长期约定')
        self.assertEqual(metadata['teamConfig'], configuration)
        self.assertEqual(len(metadata['memory']), 1)
        self.assertEqual(metadata['memory'][0]['revision'], revision)


if __name__ == '__main__':
    unittest.main()
