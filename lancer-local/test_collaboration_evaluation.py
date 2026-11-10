"""Fixed answers and recorded calls exercise the actual evaluator, without model calls."""
import copy
import hashlib
import json
import unittest

from collaboration_evaluation import (
    CASE_ID, RESEARCH_CASE_ID, DEVELOPMENT_CASE_ID, case_definition, public_case, public_cases,
    evaluate_delivery, run_metrics, compare_arms,
)


class CollaborationEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.answer = case_definition()['reference']['result']

    def evaluate(self, result):
        return evaluate_delivery([{'path': 'result.json', 'content': json.dumps(result)}])

    def test_public_case_has_no_answer_and_uses_stable_business_inputs(self):
        canonical = json.dumps(case_definition(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(),
                         'f92dfe353518f7e946e36c290e6d4df08f58c12de9e681a838dd44945b9f75dc')
        public = public_case()
        self.assertNotIn('reference', public)
        self.assertEqual(public['id'], 'order-fulfillment-v1')
        inputs = {item['path']: json.loads(item['content']) for item in public['files']}
        self.assertEqual(inputs['inputs/inventory.json'], {'A': 12, 'B': 8, 'C': 6})
        self.assertEqual(len(inputs['inputs/orders.json']), 12)
        self.assertEqual(self.answer['remainingInventory'], {'A': 0, 'B': 1, 'C': 0})
        self.assertEqual(self.answer['summary'], {'fulfilled': 5, 'rejected': 5, 'cancelled': 1, 'duplicate': 1})
        self.answer['remainingInventory']['A'] = 99
        self.assertEqual(case_definition()['reference']['result']['remainingInventory']['A'], 0)

    def test_reference_is_fully_accepted_with_actual_artifact_fingerprint(self):
        raw = json.dumps(self.answer)
        result = evaluate_delivery([{'path': 'result.json', 'content': raw}])
        self.assertTrue(result['accepted'])
        self.assertEqual((result['passed'], result['total'], result['scorePct']), (15, 15, 100))
        self.assertEqual(result['artifactFingerprint'], hashlib.sha256(raw.encode()).hexdigest())

    def test_business_errors_fail_the_corresponding_order_without_a_self_score_override(self):
        cases = [('r01', 'allocated', {'A': 4}), ('r08', 'status', 'fulfilled'),
                 ('r06', 'status', 'fulfilled'), ('r09', 'shortages', {'B': 1})]
        for row_id, field, value in cases:
            with self.subTest(row=row_id):
                answer = copy.deepcopy(self.answer)
                next(row for row in answer['decisions'] if row['rowId'] == row_id)[field] = value
                answer['selfScore'] = 100
                result = self.evaluate(answer)
                self.assertFalse(result['accepted'])
                self.assertEqual(next(row['status'] for row in result['criteria'] if row['id'] == row_id), 'failed')

    def test_priority_inventory_complete_rows_and_integer_types_are_checked(self):
        for field, value, criterion in [
            ('processingOrder', ['r01', *self.answer['processingOrder'][1:]], 'processingOrder'),
            ('remainingInventory', {'A': 0, 'B': True, 'C': 0}, 'remainingInventory'),
            ('decisions', self.answer['decisions'][:-1], 'summary'),
            ('decisions', self.answer['decisions'] + [self.answer['decisions'][0]], 'summary')]:
            with self.subTest(criterion=criterion):
                result = self.evaluate({**self.answer, field: value})
                self.assertFalse(result['accepted'])
                self.assertEqual(next(row['status'] for row in result['criteria'] if row['id'] == criterion), 'failed')

    def test_missing_invalid_and_non_object_outputs_keep_evaluation_records(self):
        missing = evaluate_delivery([])
        self.assertEqual({row['status'] for row in missing['criteria']}, {'not_run'})
        self.assertIsNone(missing['artifactFingerprint'])
        for raw in ('{bad json', '[]', 'null'):
            result = evaluate_delivery([{'path': 'result.json', 'content': raw}])
            self.assertFalse(result['accepted'])
            self.assertEqual(result['passed'], 0)
            self.assertEqual({row['status'] for row in result['criteria']}, {'failed'})
            self.assertIsNotNone(result['artifactFingerprint'])

    def test_recorded_requests_are_counted_once_and_latest_receipt_does_not_cover_earlier_attempt(self):
        receipt = {'returnedModel': 'provider/model-v1', 'usage': {'input_tokens': 100, 'output_tokens': 20}}
        run = {'started': '2026-10-31T10:00:00+0800', 'ended': '2026-10-31T10:00:05+0800',
            'callMetrics': [
                {'id': '1', 'nodeId': 'draft', 'contextRef': 'old', 'outcome': 'error', 'elapsedMs': 300},
                {'id': '2', 'nodeId': 'draft', 'contextRef': 'new', 'outcome': 'returned', 'elapsedMs': 500},
                {'id': '3', 'nodeId': 'repair-1-output', 'contextRef': 'build', 'outcome': 'cancelled', 'elapsedMs': 700}],
            'nodes': [
                {'id': 'draft', 'phase': 'draft', 'contextRef': 'new', 'receipt': receipt,
                 'attempts': [{'receipt': receipt}, {'receipt': receipt}]},
                {'id': 'repair-1-output', 'phase': 'build', 'repairRound': 1, 'started': 'now'},
                {'id': 'repair-2-output', 'phase': 'build', 'repairRound': 2}]}
        result = run_metrics(run)
        self.assertEqual(result['wallMs'], 5000)
        self.assertEqual((result['callCount'], result['errorCount'], result['cancelledCount'], result['repairCount']), (3, 1, 1, 1))
        self.assertEqual((result['coordinationCallCount'], result['coordinationMs']), (2, 800))
        self.assertEqual(result['usage'], {'inputTokens': 100, 'outputTokens': 20, 'knownCalls': 1, 'totalCalls': 3, 'complete': False})
        self.assertEqual(result['actualModels'], ['provider/model-v1'])
        self.assertEqual(result['cost'], {'status': 'unknown', 'amount': None})

    def test_unknown_usage_time_and_cost_stay_unknown(self):
        result = run_metrics({'callMetrics': [{'nodeId': 'n', 'outcome': 'returned'}],
                              'nodes': [{'id': 'n', 'phase': 'integration', 'receipt': {}}]})
        self.assertIsNone(result['usage']['inputTokens'])
        self.assertIsNone(result['coordinationMs'])
        self.assertIsNone(result['wallMs'])
        self.assertIsNone(result['cost']['amount'])
        self.assertFalse(run_metrics({})['usage']['complete'])
        self.assertIsNone(run_metrics({})['usage']['inputTokens'])

    def test_only_two_completed_accepted_arms_receive_a_speedup_ratio(self):
        good = self.evaluate(self.answer)
        single = {'status': 'completed', 'evaluation': good, 'metrics': {'wallMs': 5000}}
        multi = {'status': 'completed', 'evaluation': good, 'metrics': {'wallMs': 2500}}
        result = compare_arms(single, multi)
        self.assertEqual((result['n'], result['qualityDeltaPoints'], result['speedupRatio']), (1, 0, 2))
        for arm in ({**multi, 'status': 'failed'}, {**multi, 'evaluation': evaluate_delivery([])},
                    {**multi, 'metrics': {'wallMs': None}}, {**multi, 'metrics': {'wallMs': 0}}):
            self.assertIsNone(compare_arms(single, arm)['speedupRatio'])
        failure = compare_arms(single, {**multi, 'evaluation': evaluate_delivery([])})
        self.assertEqual(failure['qualityDeltaPoints'], -100)
        self.assertIn('描述性', result['notice'])
        self.assertIn('不包含讨论', result['notice'])

    def test_requested_aliases_and_actual_model_evidence_are_separate(self):
        run = {'callMetrics': [{'nodeId': 'a', 'model': 'alias-a'}, {'nodeId': 'b', 'model': 'alias-b'}],
               'nodes': [{'id': node, 'receipt': {'returnedModel': 'same-actual-model'}} for node in ('a', 'b')]}
        metrics = run_metrics(run)
        self.assertEqual(metrics['requestedModels'], ['alias-a', 'alias-b'])
        self.assertEqual(metrics['actualModels'], ['same-actual-model'])
        self.assertEqual(metrics['returnedModelCalls'], 2)
        self.assertTrue(metrics['modelEvidenceComplete'])
        arm = {'status': 'completed', 'evaluation': self.evaluate(self.answer), 'metrics': metrics}
        self.assertIn('同一实际型号', compare_arms(arm, arm)['participationNotice'])
        run['nodes'][1]['receipt'] = {'requestedModel': 'configured-only'}
        partial = run_metrics(run)
        self.assertEqual(partial['actualModels'], ['same-actual-model'])
        self.assertFalse(partial['modelEvidenceComplete'])
        self.assertIn('1/2次调用', compare_arms(arm, {**arm, 'metrics': partial})['participationNotice'])
        run['callMetrics'] = run['callMetrics'][:1]
        one = run_metrics(run)
        self.assertIn('仅包含1个请求成员', compare_arms(arm, {**arm, 'metrics': one})['participationNotice'])

    def test_efficiency_points_use_unrounded_whole_workflow_reduction(self):
        single = {'status': 'completed', 'evaluation': self.evaluate(self.answer), 'metrics': {'wallMs': 100000}}
        for duration, reduction, points in ((90001, 9.999, 0), (90000, 10, 1), (80000, 20, 2), (120000, -20, 0)):
            with self.subTest(duration=duration):
                multi = {**single, 'metrics': {'wallMs': duration, 'coordinationMs': 0, 'localSpeedup': 100}}
                result = compare_arms(single, multi)['efficiency']
                self.assertAlmostEqual(result['durationReductionPct'], reduction)
                self.assertEqual(result['globalPoints'], points)
                self.assertEqual(result['basis'], 'completed_accepted_wall_time')
                self.assertIn('不能叠加计分', result['notice'])

    def test_efficiency_is_unknown_for_unqualified_or_missing_duration_on_either_arm(self):
        good = {'status': 'completed', 'evaluation': self.evaluate(self.answer), 'metrics': {'wallMs': 100000}}
        invalid = [{**good, 'status': 'failed'}, {**good, 'evaluation': evaluate_delivery([])}]
        invalid += [{**good, 'metrics': {'wallMs': value}} for value in (None, 0, -1, True, float('inf'))]
        invalid.append({**good, 'metrics': {}})
        for bad in invalid:
            for single, multi in ((bad, good), (good, bad)):
                with self.subTest(bad=bad, invalid_single=single is bad):
                    result = compare_arms(single, multi)
                    self.assertIsNone(result['efficiency']['durationReductionPct'])
                    self.assertIsNone(result['efficiency']['globalPoints'])
                    self.assertIsNone(result['speedupRatio'])


class ResearchEvidenceEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.answer = {'asOf': '2026-10-15T12:00:00+08:00', 'findings': [
            {'id': 'seat_limit', 'status': 'confirmed', 'value': 8, 'sourceIds': ['S02']},
            {'id': 'monthly_exports', 'status': 'confirmed', 'value': 350, 'sourceIds': ['S03']},
            {'id': 'history_days', 'status': 'confirmed', 'value': 90, 'sourceIds': ['S02']},
            {'id': 'support_response', 'status': 'confirmed', 'value': 'next_business_day', 'sourceIds': ['S02']},
            {'id': 'east_export_status', 'status': 'confirmed', 'value': 'paused', 'sourceIds': ['S08']},
            {'id': 'november_upgrade', 'status': 'confirmed', 'value': False, 'sourceIds': ['S04']},
            {'id': 'cross_region_migration', 'status': 'unverified', 'value': None, 'sourceIds': []}],
            'sourceDispositions': {'S01': 'superseded', 'S02': 'current', 'S03': 'current', 'S04': 'draft',
                                   'S05': 'stale', 'S06': 'non_normative', 'S07': 'out_of_scope', 'S08': 'active_incident'}}

    def evaluate(self, result):
        return evaluate_delivery([{'path': 'result.json', 'content': json.dumps(result)}], RESEARCH_CASE_ID)

    def assertFailed(self, result, criterion):
        self.assertFalse(result['accepted'])
        self.assertEqual(next(item['status'] for item in result['criteria'] if item['id'] == criterion), 'failed')

    def test_registry_hides_reference_and_preserves_default(self):
        cases = public_cases()
        self.assertEqual([case['id'] for case in cases], [CASE_ID, RESEARCH_CASE_ID, DEVELOPMENT_CASE_ID])
        self.assertEqual(RESEARCH_CASE_ID, 'research-evidence-v2')
        self.assertTrue(all('reference' not in case for case in cases))
        self.assertEqual(public_case(), cases[0])
        self.assertEqual(len(cases[1]['files']), 8)
        self.assertIn('roleDirectory', cases[1]['instructions'])
        cases[1]['files'][0]['content'] = 'changed'
        self.assertNotEqual(public_case(RESEARCH_CASE_ID)['files'][0]['content'], 'changed')
        with self.assertRaisesRegex(ValueError, '未知评测案例'):
            case_definition('unknown')
        with self.assertRaisesRegex(ValueError, '未知评测案例'):
            case_definition('research-evidence-v1')

    def test_independently_written_reference_passes_and_quote_order_is_irrelevant(self):
        self.assertEqual(case_definition(RESEARCH_CASE_ID)['reference']['result'], self.answer)
        result = self.evaluate(self.answer)
        self.assertEqual((result['passed'], result['total'], result['scorePct']), (13, 13, 100))
        self.assertTrue(result['accepted'])
        self.answer['findings'][1]['sourceIds'] = ['S03', 'S02']
        self.answer['findings'].reverse()
        self.assertTrue(self.evaluate(self.answer)['accepted'])

    def test_stale_draft_marketing_and_wrong_scope_cannot_supply_answers(self):
        for index, value, refs in [(0, 5, ['S01']), (1, 500, ['S02']), (1, 0, ['S08']),
                                    (2, 180, ['S04']), (3, '24x7', ['S06']),
                                    (4, 'available', ['S02']), (5, True, ['S04']), (6, True, ['S07'])]:
            with self.subTest(index=index, value=value):
                answer = copy.deepcopy(self.answer)
                answer['findings'][index].update(status='confirmed', value=value, sourceIds=refs)
                answer['selfScore'] = 100
                self.assertFailed(self.evaluate(answer), answer['findings'][index]['id'])

    def test_correct_value_still_requires_valid_evidence_and_exact_value_type(self):
        for patch in ({'sourceIds': ['S06']}, {'sourceIds': ['S02', 'S06']}, {'sourceIds': ['S02', 'S02']},
                      {'sourceIds': None}, {'sourceIds': [{}]}, {'value': 8.0}, {'value': True}):
            with self.subTest(patch=patch):
                answer = copy.deepcopy(self.answer)
                answer['findings'][0].update(patch)
                self.assertFailed(self.evaluate(answer), 'seat_limit')
        for index, patch in ((5, {'value': 0}), (6, {'sourceIds': ['S07']})):
            answer = copy.deepcopy(self.answer)
            answer['findings'][index].update(patch)
            self.assertFailed(self.evaluate(answer), answer['findings'][index]['id'])

    def test_explicit_negative_approval_is_distinct_from_missing_migration_evidence(self):
        self.assertTrue(self.evaluate(self.answer)['accepted'])
        for patch in ({'status': 'confirmed', 'value': True},
                      {'status': 'unverified', 'value': None}, {'sourceIds': ['S06']}):
            with self.subTest(approval=patch):
                answer = copy.deepcopy(self.answer)
                answer['findings'][5].update(patch)
                self.assertFailed(self.evaluate(answer), 'november_upgrade')
        for value in (True, False):
            with self.subTest(migration=value):
                answer = copy.deepcopy(self.answer)
                answer['findings'][6].update(status='confirmed', value=value)
                self.assertFailed(self.evaluate(answer), 'cross_region_migration')

    def test_asof_source_dispositions_and_complete_identifiers_are_graded(self):
        for sid, category, criterion in [('S03', 'stale', 'current_sources'), ('S05', 'current', 'old_sources'),
                                          ('S04', 'current', 'draft_source'), ('S07', 'current', 'unsuitable_sources')]:
            with self.subTest(source=sid):
                answer = copy.deepcopy(self.answer)
                answer['sourceDispositions'][sid] = category
                self.assertFailed(self.evaluate(answer), criterion)
        self.assertFailed(self.evaluate({**self.answer, 'asOf': '2026-11-01'}), 'asOf')
        for rows in (self.answer['findings'][:-1], self.answer['findings'] + [self.answer['findings'][0]],
                     self.answer['findings'] + [False], [{'id': []}], None):
            self.assertFailed(self.evaluate({**self.answer, 'findings': rows}), 'completeness')
        for sources in ({**self.answer['sourceDispositions'], 'S09': 'current'}, None):
            self.assertFailed(self.evaluate({**self.answer, 'sourceDispositions': sources}), 'completeness')

    def test_missing_and_bad_research_outputs_keep_all_criteria(self):
        missing = evaluate_delivery([], RESEARCH_CASE_ID)
        self.assertEqual((missing['passed'], missing['total']), (0, 13))
        self.assertEqual({item['status'] for item in missing['criteria']}, {'not_run'})
        for content in ('{bad', '[]', '{}'):
            result = evaluate_delivery([{'path': 'result.json', 'content': content}], RESEARCH_CASE_ID)
            self.assertEqual((result['passed'], result['total']), (0, 13))
            self.assertEqual(result['artifactFingerprint'], hashlib.sha256(content.encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
