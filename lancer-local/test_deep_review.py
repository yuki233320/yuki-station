"""Exercise the real discussion engine with deterministic provider responses."""
import copy
import io
import json
import unittest
import zipfile

from studio_business import DEEP_QUESTION, validate_business_response
import test_studio_business as fixtures
from test_studio_business import module


class DeepReviewTests(unittest.TestCase):
    setUp = fixtures.BusinessWorkflowTests.setUp
    tearDown = fixtures.BusinessWorkflowTests.tearDown
    run_task = fixtures.BusinessWorkflowTests.run_task

    def run_research(self, complexity=4):
        self.client.modules = [module('analysis', 'research', complexity)]
        session = self.run_task(coordinator_model='opus', team_models=['opus', 'sol', 'deepseek'])
        self.assertEqual(session['status'], 'completed', session.get('error'))
        return session, session['runs'][-1]

    def test_one_round_does_not_add_deep_question(self):
        _, run = self.run_research(3)
        self.assertEqual(run['moduleReviews']['analysis']['completedRounds'], 1)
        self.assertNotIn('reflection', run['moduleReviews']['analysis'])
        self.assertFalse(any(node.get('fixedPrompt') for node in run['nodes']))

    def test_two_and_three_rounds_have_one_fixed_question_and_all_answers(self):
        for complexity in (4, 5):
            with self.subTest(complexity=complexity):
                session, run = self.run_research(complexity)
                progress = run['moduleReviews']['analysis']
                reflection = progress['reflection']
                self.assertEqual(reflection['round'], complexity - 2)
                self.assertEqual(progress['completedRounds'], complexity - 2)
                nodes = {node['id']: node for node in run['nodes']}
                question = nodes[reflection['questionNodeId']]
                self.assertTrue(question['fixedPrompt'])
                self.assertEqual(question['discussionKind'], 'reflection_prompt')
                self.assertEqual(question['result']['summary'], DEEP_QUESTION)
                self.assertNotIn('receipt', question)
                self.assertNotIn('contextRef', question)
                message = next(m for m in session['messages'] if m.get('node') == question['id'])
                self.assertEqual(message['origin'], 'workflow_prompt')
                self.assertFalse(any(turn['id'] == message['id'] for agent in session['agents'].values() for turn in agent['turns']))
                with zipfile.ZipFile(io.BytesIO(self.studio.export(self.pid, session['id']))) as archive:
                    exported = json.loads(archive.read('会话与执行记录.json'))
                    exported_question = next(m for m in exported['messages'] if m.get('node') == question['id'])
                    self.assertEqual(exported_question['origin'], 'workflow_prompt')
                    self.assertNotIn('contextRef', exported_question)
                self.assertEqual(len(reflection['answerNodeIds']), len(progress['models']))
                for node_id in reflection['answerNodeIds']:
                    node = nodes[node_id]
                    self.assertEqual(node['deps'], [question['id']])
                    self.assertEqual(node['round'], complexity - 2)
                    self.assertEqual(node['discussionKind'], 'reflection')
                    self.assertEqual(node['result']['reflection']['assessment'], 'deeper_issue')
                    self.assertEqual(node['result']['findings'][0]['contributionWeight'], 2)
                    agent = session['agents']['reviewer-analysis-' + node['agent']]
                    self.assertEqual(len(agent['turns']), complexity - 1)
                decision = nodes['decision-analysis']
                self.assertEqual(set(decision['deps']), set(reflection['answerNodeIds']))
                self.assertFalse(any(row.get('nodeId') == question['id'] for row in run.get('callMetrics', [])))
                self.assertEqual(len([m for m in session['messages'] if m.get('node') == question['id']]), 1)
                for node in nodes.values():
                    if node['phase'] != 'review' or node.get('discussionKind'):
                        continue
                    context = self.studio.context(self.pid, session['id'], node['contextRef'])
                    instruction = context['messages'][-1]['content'].split('\n本轮角色任务：\n', 1)[1]
                    self.assertIn('已被当前方案覆盖的约束、实施提醒', instruction)
                    self.assertIn('写入 summary，不作为新 findings', instruction)
                    self.assertIn('具体缺口、冲突或待决事项', instruction)
                context = self.studio.context(self.pid, session['id'], decision['contextRef'])
                instruction = context['messages'][-1]['content'].split('\n本轮角色任务：\n', 1)[1]
                self.assertIn('后续 respondsTo 的依据', instruction)
                self.assertIn('不得继续作为未解决的 P0/P1 问题采纳', instruction)
                self.assertIn('reason 中说明采用了哪条后续依据', instruction)

    def test_early_convergence_keeps_closing_question_without_empty_rounds(self):
        self.client.mutate = lambda stage, result: dict(result, findings=[]) if stage == 'review' else result
        _, run = self.run_research(5)
        progress = run['moduleReviews']['analysis']
        self.assertEqual(progress['roundLimit'], 3)
        self.assertEqual(progress['completedRounds'], 1)
        self.assertEqual(progress['reflection']['round'], 1)
        self.assertIn('不增加空转轮次', progress['finishReason'])
        self.assertEqual(len([n for n in run['nodes'] if n.get('discussionKind') == 'reflection']), 2)

    def test_no_deeper_issue_is_a_concrete_answer_without_forced_findings(self):
        def no_issue(stage, result):
            if stage == 'reflection':
                result['reflection'].update(assessment='no_deeper_issue',
                    issue='未发现额外问题：当前接口契约已覆盖接收方验收条件。',
                    basis='本模块唯一验收项要求明确成果，方案已限定文件和接口说明。',
                    proposal='保留当前文件说明，不增加新模块。',
                    verification='交付后核对文件中接口与原定验收项逐项对应。')
                result['findings'] = []
            return result
        self.client.mutate = no_issue
        _, run = self.run_research()
        answers = [n for n in run['nodes'] if n.get('discussionKind') == 'reflection']
        self.assertEqual(len(answers), 2)
        self.assertTrue(all(not n['result']['findings'] for n in answers))
        decision = next(c for c in reversed(self.client.calls) if c['key'][0] == 'decision')
        self.assertEqual(len(decision['memory']['sharedEvidence']['reflectionAnswers']), 2)

    def test_resume_reuses_question_and_successful_peer_answer(self):
        self.client.modules = [module('analysis', 'research', 4)]
        self.client.fail_once = ('reflection', 'analysis', 'sol', 0)
        session = self.run_task(coordinator_model='opus', team_models=['opus', 'sol', 'deepseek'])
        self.assertEqual(session['status'], 'failed')
        session = self.run_task(session, True)
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        question = run['moduleReviews']['analysis']['reflection']['questionNodeId']
        self.assertEqual(len([m for m in session['messages'] if m.get('node') == question]), 1)
        counts = {model: len([c for c in self.client.calls if c['key'][:3] == ('reflection', 'analysis', model)])
                  for model in ('sol', 'deepseek')}
        self.assertEqual(counts, {'sol': 2, 'deepseek': 1})

    def test_final_reflection_can_revisit_first_round_in_two_round_workflow(self):
        def revisit(stage, result):
            if stage == 'reflection':
                shared = next(call['memory']['sharedEvidence'] for call in self.client.calls
                              if call['key'][0] == 'reflection')
                result['findings'][0]['respondsTo'] = shared['previousRounds'][0]['reviews'][0]['findings'][0]['id']
            return result
        self.client.mutate = revisit
        _, run = self.run_research(4)
        progress = run['moduleReviews']['analysis']
        self.assertEqual(progress['completedRounds'], 2)
        self.assertEqual(progress['reflection']['round'], 2)
        first_node = next(node for node in run['nodes'] if node['id'] == 'review-analysis-1-sol')
        first_id = first_node['result']['findings'][0]['id']
        decision = next(call for call in self.client.calls if call['key'][0] == 'decision')
        reflections = [finding for finding in decision['memory']['sharedEvidence']['evidence']
                       if finding.get('reflection')]
        self.assertEqual(len(reflections), len(progress['models']))
        self.assertTrue(all(finding['respondsTo'] == first_id for finding in reflections))
        self.assertTrue(all(finding['contributionWeight'] == 2 for finding in reflections))
        self.assertFalse(any((node.get('round') or 0) > 2 for node in run['nodes']))

    def test_only_reflection_can_reference_earlier_provided_rounds(self):
        node = {'id': 'reflect-analysis-sol', 'agent': 'sol', 'roleId': 'reviewer-sol', 'round': 2}
        previous = [{'round': round_no, 'reviews': [{'findings': [{'id': finding_id}]}]}
                    for round_no, finding_id in ((1, 'first-round-f1'), (2, 'last-round-f1'))]
        response = {'summary': '核对早期接口约束', 'findings': [{
            'claim': '接口条件需要补齐', 'priority': 'P1', 'complexity': 2,
            'evidence': '早期讨论已指出接收方要求同一版本接口',
            'proposal': '在既有说明补全版本条件', 'respondsTo': ''}],
            'reflection': {'assessment': 'deeper_issue', 'issue': '早期接口约束尚未完整解决',
                           'basis': '两轮讨论中存在同一接口版本问题',
                           'proposal': '补全既有接口说明', 'verification': '按接收方契约逐项核对'}}
        for stage in ('review', 'reflection'):
            shared = {'businessStage': stage, 'previousRounds': previous, 'reflectionWeight': 2}
            for target in ('', 'first-round-f1', 'last-round-f1', 'unknown-f1', 'other-module-f1'):
                with self.subTest(stage=stage, target=target):
                    candidate = copy.deepcopy(response)
                    candidate['findings'][0]['respondsTo'] = target
                    allowed = target in ('', 'last-round-f1') or (stage == 'reflection' and target == 'first-round-f1')
                    if allowed:
                        result = validate_business_response(node, candidate, shared, ['sol'])
                        self.assertEqual(result['findings'][0]['respondsTo'], target)
                    else:
                        with self.assertRaisesRegex(ValueError, '历史实际论据' if stage == 'reflection' else '上一轮'):
                            validate_business_response(node, candidate, shared, ['sol'])

    def test_reflection_priority_is_higher_only_within_same_risk_and_complexity(self):
        self.run_research()
        decision = next(c for c in reversed(self.client.calls) if c['key'][0] == 'decision')
        self.assertTrue(decision['memory']['sharedEvidence']['evidence'][0]['reflection'])

        def urgent_regular(stage, result):
            if stage == 'review':
                result['findings'][0]['priority'] = 'P0'
            return result
        self.client.mutate = urgent_regular
        self.run_research()
        decision = next(c for c in reversed(self.client.calls) if c['key'][0] == 'decision')
        evidence = decision['memory']['sharedEvidence']['evidence']
        self.assertEqual(evidence[0]['priority'], 'P0')
        self.assertFalse(evidence[0].get('reflection', False))
        self.assertTrue(evidence[-1]['reflection'])

    def test_response_requires_structured_concrete_answer_and_host_weight(self):
        node = {'id': 'reflect-analysis-sol', 'agent': 'sol', 'roleId': 'reviewer-sol', 'round': 2}
        shared = {'businessStage': 'reflection', 'previousRounds': [], 'reflectionWeight': 2}
        finding = {'claim': '接口条件需要补齐', 'priority': 'P1', 'complexity': 2,
                   'evidence': '接收方要求同一版本接口', 'proposal': '在既有说明补全版本条件', 'respondsTo': '',
                   'reflection': False, 'contributionWeight': 999}
        response = {'summary': '具体说明', 'findings': [finding],
                    'reflection': {'assessment': 'deeper_issue', 'issue': finding['claim'],
                                   'basis': finding['evidence'], 'proposal': finding['proposal'],
                                   'verification': '交付后与接收方契约逐项核对'}}
        for field in ('reflection', 'basis', 'verification'):
            invalid = copy.deepcopy(response)
            if field == 'reflection':
                invalid.pop(field)
            else:
                invalid['reflection'][field] = ''
            with self.assertRaises(ValueError):
                validate_business_response(node, invalid, shared, ['sol'])
        result = validate_business_response(node, copy.deepcopy(response), shared, ['sol'])
        self.assertTrue(result['findings'][0]['reflection'])
        self.assertEqual(result['findings'][0]['contributionWeight'], 2)
        ordinary = validate_business_response(node, copy.deepcopy(response), dict(shared, businessStage='review'), ['sol'])
        self.assertNotIn('reflection', ordinary['findings'][0])
        self.assertNotIn('contributionWeight', ordinary['findings'][0])


if __name__ == '__main__':
    unittest.main()
