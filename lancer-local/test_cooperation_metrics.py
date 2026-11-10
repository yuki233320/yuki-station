import copy
import json
import unittest

from cooperation_metrics import evaluate_pair, summarize_pairs, SCHEMA, POLICY


def arm(score=100, incoming=800, outgoing=200, multi=False):
    roles = ['plan', 'worker1', 'worker2', 'aggregate'] if multi else ['single']
    caps = [1000, 2000, 2000, 3000] if multi else [8000]
    calls = [{'id': role, 'role': role, 'cap': cap, 'status': 'completed',
              'receipt': {'usage': {'input_tokens': incoming // len(roles),
                                   'output_tokens': outgoing // len(roles)}}}
             for role, cap in zip(roles, caps)]
    return {'status': 'completed', 'configuredBudget': 8000, 'calls': calls,
            'evaluation': {'scorePct': score, 'accepted': score == 100},
            'metrics': {'usage': {'complete': True, 'inputTokens': incoming, 'outputTokens': outgoing,
                                 'knownCalls': len(roles), 'totalCalls': len(roles)},
                        'cost': {'status': 'unknown', 'amount': None}}}


def pair():
    return {'schema': SCHEMA, 'id': 'one', 'caseId': 'task-1', 'inputFingerprint': 'input-1',
            'caseFingerprint': 'case-1', 'graderFingerprint': 'grader', 'protocolFingerprint': 'protocol',
            'profilesFingerprint': 'profile', 'homogeneous': True,
            'budget': {'unit': 'provider_output_tokens', 'cap': 8000},
            'evaluationPolicy': {'schema': 'yuki-egt-policy/v1', 'lambda': 1.0, 'corLimit': 0.25},
            'arms': {'single': arm(80), 'multi': arm(100, 1200, 300, True)}}


def priced(arm, amount, currency='CNY'):
    arm['metrics']['cost'] = {'complete': True, 'basis': 'actual_receipt', 'amount': amount, 'currency': currency}


class CooperationMetricsTests(unittest.TestCase):
    def test_cor_uses_all_input_and_output_not_coordination_call_share(self):
        result = evaluate_pair(pair())
        self.assertEqual(result['resources']['resourceOverheadRatio'], 0.5)
        self.assertEqual(result['resources']['singleTokens'], 1000)
        self.assertEqual(result['resources']['multiTokens'], 1500)
        self.assertEqual(result['resources']['coordination']['multi']['callShare'], 0.5)
        self.assertEqual(result['resources']['coordination']['single']['callShare'], 0)
        self.assertEqual(result['qualityDeltaPoints'], 20)
        self.assertEqual(result['egt']['relativeQualityGain'], 0.25)
        self.assertEqual(result['egt']['tokenProxyMargin'], -0.25)
        self.assertEqual(result['pareto']['status'], 'tradeoff')

    def test_equal_output_allowance_is_never_strict_compute_alignment(self):
        result = evaluate_pair(pair())
        self.assertEqual(result['budgetAudit']['status'], 'output_allowance_only')
        self.assertTrue(result['budgetAudit']['equalOutputAllowance'])
        self.assertFalse(result['budgetAudit']['strictBudgetMatched'])
        self.assertEqual(result['decision']['status'], 'insufficient_budget_evidence')
        self.assertFalse(result['decision']['provesSuperiority'])
        self.assertEqual(result['quality']['balancedAccuracy']['status'], 'not_applicable')

    def test_missing_usage_does_not_become_free_computation(self):
        record = pair()
        record['arms']['multi']['metrics']['usage']['complete'] = False
        result = evaluate_pair(record)
        self.assertIsNone(result['resources']['resourceOverheadRatio'])
        self.assertIsNone(result['egt']['tokenProxyMargin'])
        self.assertEqual(result['resources']['tokensPerSuccessfulTask']['multi']['status'], 'unknown')
        self.assertEqual(result['budgetAudit']['status'], 'unverified')

    def test_failed_task_cost_is_included_in_cost_per_success(self):
        first = pair()
        first['arms']['single'] = arm()
        priced(first['arms']['single'], 1)
        second = copy.deepcopy(first)
        second['id'] = 'two'
        second['arms']['single']['status'] = 'failed'
        priced(second['arms']['single'], 2)
        summary = summarize_pairs([first, second])[0]
        costs = summary['costPerSuccessfulTask']['single']
        self.assertEqual(costs['total'], 3)
        self.assertEqual(costs['successfulTasks'], 1)
        self.assertEqual(costs['value'], 3)
        self.assertEqual(summary['tokensPerSuccessfulTask']['single']['value'], 2000)
        self.assertEqual(summary['multiOnlyPassed'], 1)

    def test_zero_success_keeps_total_without_dividing_by_zero(self):
        record = pair()
        priced(record['arms']['single'], 2)
        result = evaluate_pair(record)['resources']['costPerSuccessfulTask']['single']
        self.assertEqual(result['status'], 'no_success')
        self.assertEqual(result['total'], 2)
        self.assertIsNone(result['value'])

    def test_unknown_price_is_separate_from_token_per_success(self):
        result = evaluate_pair(pair())['resources']
        self.assertEqual(result['costPerSuccessfulTask']['multi']['status'], 'unknown')
        self.assertIsNone(result['costPerSuccessfulTask']['multi']['total'])
        self.assertEqual(result['tokensPerSuccessfulTask']['multi']['value'], 1500)

    def test_currency_mismatch_blocks_cost_ratio_and_aggregation(self):
        record = pair()
        priced(record['arms']['single'], 1, 'CNY')
        priced(record['arms']['multi'], 1, 'USD')
        self.assertIsNone(evaluate_pair(record)['egt']['relativeCostIncrease'])
        second = copy.deepcopy(record)
        second['id'] = 'two'
        priced(second['arms']['single'], 1, 'USD')
        costs = summarize_pairs([record, second])[0]['costPerSuccessfulTask']['single']
        self.assertEqual(costs['status'], 'currency_mismatch')
        self.assertIsNone(costs['total'])

    def test_estimated_prices_are_not_actual_receipts(self):
        record = pair()
        priced(record['arms']['single'], 1)
        record['arms']['single']['metrics']['cost']['basis'] = 'catalog_estimate'
        self.assertEqual(evaluate_pair(record)['resources']['costPerSuccessfulTask']['single']['status'], 'unknown')

    def test_zero_baseline_produces_no_relative_egt_or_infinity(self):
        record = pair()
        record['arms']['single'] = arm(0, 0, 0)
        result = evaluate_pair(record)
        self.assertIsNone(result['egt']['relativeQualityGain'])
        self.assertIsNone(result['resources']['resourceOverheadRatio'])
        self.assertIsNone(result['egt']['tokenProxyMargin'])
        json.dumps(result, allow_nan=False)

    def test_legacy_same_model_multiple_roles_cannot_become_sas(self):
        record = pair()
        record['schema'] = 'yuki-collaboration-comparison/v1'
        record.pop('budget')
        record.pop('homogeneous')
        result = evaluate_pair(record)
        self.assertEqual(result['decision']['status'], 'legacy_not_sas')
        self.assertFalse(result['budgetAudit']['equalOutputAllowance'])
        self.assertFalse(result['decision']['provesSuperiority'])

    def test_heterogeneous_tokens_do_not_attest_equal_flops(self):
        record = pair()
        record['homogeneous'] = False
        result = evaluate_pair(record)
        self.assertFalse(result['budgetAudit']['homogeneousDeclared'])
        self.assertTrue(any('FLOPs' in reason for reason in result['decision']['reasons']))

    def test_provider_output_overrun_rejects_budget_verification(self):
        record = pair()
        record['arms']['multi']['calls'][0]['receipt']['usage']['output_tokens'] = 1001
        result = evaluate_pair(record)['budgetAudit']
        self.assertTrue(result['observedOutputOverrun'])
        self.assertEqual(result['status'], 'unverified')

    def test_underallocated_calls_do_not_match_equal_declared_caps(self):
        record = pair()
        record['arms']['multi']['calls'][3]['cap'] = 2000
        result = evaluate_pair(record)['budgetAudit']
        self.assertTrue(result['allocationWithinCap'])
        self.assertFalse(result['fullAllowanceAllocated'])
        self.assertEqual(result['status'], 'unverified')

    def test_failed_execution_is_not_verified_merely_by_declared_caps(self):
        record = pair()
        record['arms']['multi']['status'] = 'failed'
        record['arms']['multi']['calls'] = record['arms']['multi']['calls'][:1]
        result = evaluate_pair(record)['budgetAudit']
        self.assertTrue(result['equalOutputAllowance'])
        self.assertFalse(result['executionComplete'])
        self.assertEqual(result['status'], 'unverified')

    def test_repeated_same_task_is_one_independent_unit(self):
        records = []
        for index in range(5):
            record = pair()
            record['id'] = str(index)
            records.append(record)
        summary = summarize_pairs(records)[0]
        self.assertEqual(summary['assessedPairs'], 5)
        self.assertEqual(summary['inference']['independentTasks'], 1)
        self.assertIsNone(summary['inference']['pValue'])
        self.assertEqual(summary['inference']['status'], 'insufficient_evidence')

    def test_changed_inputs_or_budgets_never_share_group(self):
        first = pair()
        second = copy.deepcopy(first)
        second['id'] = 'two'
        second['inputFingerprint'] = 'different'
        third = copy.deepcopy(first)
        third['id'] = 'three'
        third['budget']['cap'] = 16000
        self.assertEqual(len(summarize_pairs([first, second, third])), 3)

    def test_incomplete_pair_remains_scheduled_and_unknown_cost(self):
        record = pair()
        record['arms']['multi'] = {'status': 'waiting', 'calls': [], 'configuredBudget': 8000}
        self.assertEqual(evaluate_pair(record)['decision']['status'], 'incomplete_pair')
        summary = summarize_pairs([record])[0]
        self.assertEqual(summary['scheduledPairs'], 1)
        self.assertEqual(summary['assessedPairs'], 0)
        self.assertEqual(summary['incompletePairs'], 1)
        self.assertEqual(summary['inference']['independentTasks'], 0)

    def test_passing_descriptive_egt_is_not_a_superiority_claim(self):
        record = pair()
        record['arms']['multi'] = arm(100, 800, 200, True)
        priced(record['arms']['single'], 1)
        priced(record['arms']['multi'], 1)
        result = evaluate_pair(record)
        self.assertTrue(result['egt']['passesObservedCostThreshold'])
        self.assertEqual(result['pareto']['status'], 'multi_dominates_observed')
        self.assertFalse(result['decision']['provesSuperiority'])

    def test_frozen_policy_survives_changed_global_defaults(self):
        record = pair()
        before = evaluate_pair(record)['egt']
        old = dict(POLICY)
        try:
            POLICY.update({'lambda': 3.0, 'corLimit': 0.9})
            after = evaluate_pair(record)
            self.assertEqual(after['egt'], before)
            self.assertEqual(after['policyBasis'], 'frozen_at_start')
        finally:
            POLICY.clear()
            POLICY.update(old)

    def test_unfrozen_policy_is_explicit_and_not_grouped_with_frozen(self):
        frozen = pair()
        historical = copy.deepcopy(frozen)
        historical['id'] = 'old'
        del historical['evaluationPolicy']
        result = evaluate_pair(historical)
        self.assertEqual(result['policyBasis'], 'current_policy_not_frozen')
        self.assertTrue(any('预注册' in reason for reason in result['decision']['reasons']))
        self.assertEqual(len(summarize_pairs([frozen, historical])), 2)

    def test_different_frozen_policy_keeps_separate_summary(self):
        first = pair()
        second = copy.deepcopy(first)
        second['id'] = 'two'
        second['evaluationPolicy']['lambda'] = 2.0
        self.assertEqual(evaluate_pair(second)['egt']['tokenProxyMargin'], -0.75)
        self.assertEqual(len(summarize_pairs([first, second])), 2)

    def test_invalid_protocol_keeps_expenditure_but_removes_quality_claim(self):
        record = pair()
        record['evaluationNotice'] = '题目产物路径与协议冲突，此结果不作为能力证据。'
        priced(record['arms']['multi'], 3)
        result = evaluate_pair(record)
        self.assertEqual(result['decision']['status'], 'invalid_protocol')
        self.assertIsNone(result['qualityDeltaPoints'])
        self.assertIsNone(result['quality']['multiScorePct'])
        self.assertIsNone(result['quality']['multiSuccessful'])
        self.assertIsNone(result['egt']['tokenProxyMargin'])
        self.assertIsNone(result['egt']['passesObservedCostThreshold'])
        self.assertEqual(result['pareto']['status'], 'invalid_protocol')
        self.assertEqual(result['resources']['multiTokens'], 1500)
        costs = result['resources']['costPerSuccessfulTask']['multi']
        self.assertEqual(costs['total'], 3)
        self.assertIsNone(costs['value'])
        self.assertIsNone(costs['successfulTasks'])
        self.assertEqual(summarize_pairs([record]), [])

    def test_provider_default_keeps_observations_without_budget_or_cost_gate(self):
        record = pair()
        record['budget'] = {'mode': 'provider_default', 'cap': None, 'enforcement': 'none'}
        record['evaluationPolicy'] = {'schema': 'yuki-egt-policy/v2', 'costControl': False}
        for arm in record['arms'].values():
            arm['configuredBudget'] = None
            for call in arm['calls']:
                call['cap'] = None
        priced(record['arms']['single'], 1)
        priced(record['arms']['multi'], 3)
        before = copy.deepcopy(record)
        result = evaluate_pair(record)
        self.assertEqual(result['budgetAudit']['status'], 'not_controlled')
        self.assertFalse(result['budgetAudit']['equalOutputAllowance'])
        self.assertFalse(result['budgetAudit']['strictBudgetMatched'])
        self.assertIsNone(result['budgetAudit']['observedOutputOverrun'])
        self.assertEqual(result['resources']['resourceOverheadRatio'], 0.5)
        self.assertEqual(result['qualityDeltaPoints'], 20)
        self.assertFalse(result['egt']['enabled'])
        for key in ('lambda', 'corLimit', 'tokenProxyMargin', 'costMargin', 'passesObservedCostThreshold'):
            self.assertIsNone(result['egt'][key])
        self.assertEqual(result['decision']['status'], 'descriptive_uncontrolled')
        self.assertFalse(result['decision']['provesSuperiority'])
        self.assertEqual(record, before)

    def test_new_no_budget_policy_does_not_mix_with_old_matched_allowance(self):
        historical = pair()
        current = copy.deepcopy(historical)
        current['id'] = 'current'
        current['budget'] = {'mode': 'provider_default', 'cap': None, 'enforcement': 'none'}
        current['evaluationPolicy'] = {'schema': 'yuki-egt-policy/v2', 'costControl': False}
        for arm in current['arms'].values():
            arm['configuredBudget'] = None
            for call in arm['calls']:
                call['cap'] = None
        self.assertEqual(len(summarize_pairs([historical, current])), 2)
        self.assertTrue(evaluate_pair(historical)['egt']['enabled'])
        self.assertEqual(evaluate_pair(historical)['budgetAudit']['status'], 'output_allowance_only')

    def test_analysis_does_not_rewrite_original_evidence(self):
        record = pair()
        original = copy.deepcopy(record)
        evaluate_pair(record)
        summarize_pairs([record])
        self.assertEqual(record, original)


if __name__ == '__main__':
    unittest.main()
