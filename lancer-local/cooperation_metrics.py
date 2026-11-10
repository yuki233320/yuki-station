"""Budget-audited paired observations; local EGT policy, not a superiority score."""
import hashlib
import json

SCHEMA = 'yuki-controlled-comparison/v1'
POLICY = {'lambda': 1.0, 'corLimit': 0.25}
NOTICE = ('参考 CooperBench 的 solo/coop 同任务验收设计；新实验不设置固定额度，费用由用户依据账单决定。'
          '历史预算与 EGT 只解释当时记录，非论文认证评分；不自动改变模型路由。')


def evaluate_pair(record):
    policy = record.get('evaluationPolicy') or dict(POLICY)
    policy_basis = 'frozen_at_start' if record.get('evaluationPolicy') else 'current_policy_not_frozen'
    cost_control = policy.get('costControl', True) and record.get('budget', {}).get('mode') != 'provider_default'
    invalid = bool(record.get('evaluationNotice'))
    arms = record.get('arms', {})
    single, multi = arms.get('single', {}), arms.get('multi', {})
    audited = _budget_audit(record, single, multi)
    totals = [_total_tokens(arm) for arm in (single, multi)]
    scores = [None, None] if invalid else [arm.get('evaluation', {}).get('scorePct') for arm in (single, multi)]
    complete = all('evaluation' in arm for arm in (single, multi))
    delta = scores[1] - scores[0] if all(s is not None for s in scores) else None
    overhead, quality_gain = _relative(*totals), _relative(*scores)
    costs = [_cost_summary([arm]) for arm in (single, multi)]
    token_costs = [_token_summary([arm]) for arm in (single, multi)]
    if invalid:
        for item in costs + token_costs:
            item.update(status='invalid_protocol', successfulTasks=None, value=None)
    matched = all(c['total'] is not None for c in costs) and costs[0]['currency'] == costs[1]['currency']
    cost_increase = _relative(costs[0]['total'], costs[1]['total']) if matched else None
    token_margin = quality_gain - policy['lambda'] * overhead if cost_control and quality_gain is not None and overhead is not None else None
    cost_margin = quality_gain - policy['lambda'] * cost_increase if cost_control and quality_gain is not None and cost_increase is not None else None
    reasons = list(audited['reasons'])
    if invalid:
        reasons.append(record['evaluationNotice'])
    if policy_basis == 'current_policy_not_frozen':
        reasons.append('此历史记录没有执行前冻结的 EGT 参数；显示当前规则重算，不能冒充预注册判断。')
    if not complete:
        reasons.append('至少一臂尚无固定验收结果，未完成任务不能作为成功样本。')
    reasons.append('本记录仅一个任务配对，没有支持总体优越性的独立任务样本。')
    status = ('invalid_protocol' if invalid else 'legacy_not_sas' if record.get('schema') != SCHEMA else 'incomplete_pair' if not complete
              else 'descriptive_uncontrolled' if audited['status'] == 'not_controlled' else 'insufficient_budget_evidence')
    return {'schema': 'yuki-cooperation-metrics/v1', 'pairId': record.get('id'),
            'scope': 'single_task_observation', 'policyBasis': policy_basis, 'qualityDeltaPoints': delta,
            'quality': {'singleScorePct': scores[0], 'multiScorePct': scores[1],
                'singleSuccessful': None if invalid else _success(single),
                'multiSuccessful': None if invalid else _success(multi),
                'balancedAccuracy': {'status': 'not_applicable', 'value': None,
                    'reason': '固定验收项通过率不是二分类缺陷检测；没有真实 TP/TN/FP/FN，不能计算平衡准确率或误报增量。'}},
            'budgetAudit': audited,
            'resources': {'singleTokens': totals[0], 'multiTokens': totals[1],
                'resourceOverheadRatio': overhead, 'basis': 'reported_input_plus_output_tokens',
                'corNotice': 'COR 是相对总用量增量，包含执行、规划、交接和失败请求，不是纯协调开销。',
                'coordination': {key: _coordination(arm) for key, arm in (('single', single), ('multi', multi))},
                'costPerSuccessfulTask': dict(zip(('single', 'multi'), costs)),
                'tokensPerSuccessfulTask': dict(zip(('single', 'multi'), token_costs))},
            'egt': {**policy, 'enabled': cost_control,
                'lambda': policy.get('lambda') if cost_control else None,
                'corLimit': policy.get('corLimit') if cost_control else None,
                'policyBasis': policy_basis, 'relativeQualityGain': quality_gain,
                'relativeCostIncrease': cost_increase, 'tokenProxyMargin': token_margin, 'costMargin': cost_margin,
                'passesObservedCostThreshold': (cost_margin > 0 and overhead < policy['corLimit'])
                    if cost_margin is not None and overhead is not None else None,
                'notice': ('历史λ和COR阈值仅解释当时记录，不用于当前分配或费用决策。tokenProxyMargin不是费用增益。'
                           '观察阈值满足也不等于统计或因果优势。' if cost_control else
                           '已取消固定额度与成本阈值门控；费用由用户依据账单决定。仅保留质量、用量与耗时观察，不能宣称等预算优势。')},
            'pareto': {**_pareto(scores, totals), 'status': 'invalid_protocol'} if invalid else _pareto(scores, totals),
            'decision': {'status': status, 'provesSuperiority': False, 'reasons': reasons}, 'notice': NOTICE}


def summarize_pairs(records):
    groups = {}
    for record in records:
        if record.get('evaluationNotice'):
            continue
        conditions = {key: record.get(key) for key in ('schema', 'caseId', 'caseFingerprint', 'inputFingerprint',
                      'graderFingerprint', 'protocolFingerprint', 'workflowFingerprint', 'profilesFingerprint',
                      'selection', 'homogeneous', 'budget', 'workPolicy', 'evaluationPolicy')}
        key = hashlib.sha256(json.dumps(conditions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        groups.setdefault(key, []).append(record)
    summaries = []
    for key, rows in groups.items():
        arms = {name: [row.get('arms', {}).get(name, {}) for row in rows] for name in ('single', 'multi')}
        assessed = [row for row in rows if all('evaluation' in arm for arm in
                    (row.get('arms', {}).get('single', {}), row.get('arms', {}).get('multi', {})))]
        outcomes = {'bothPassed': 0, 'singleOnlyPassed': 0, 'multiOnlyPassed': 0, 'neitherPassed': 0}
        for row in assessed:
            a, b = [_success(row['arms'][name]) for name in ('single', 'multi')]
            name = 'bothPassed' if a and b else 'singleOnlyPassed' if a else 'multiOnlyPassed' if b else 'neitherPassed'
            outcomes[name] += 1
        deltas = [evaluate_pair(row)['qualityDeltaPoints'] for row in assessed]
        deltas = [value for value in deltas if value is not None]
        summaries.append({'groupId': key, 'caseId': rows[0].get('caseId'), 'recordIds': [r['id'] for r in rows],
            'scheduledPairs': len(rows), 'assessedPairs': len(assessed), 'incompletePairs': len(rows) - len(assessed),
            **outcomes, 'meanQualityDeltaPoints': sum(deltas) / len(deltas) if deltas else None,
            'costPerSuccessfulTask': {name: _cost_summary(values) for name, values in arms.items()},
            'tokensPerSuccessfulTask': {name: _token_summary(values) for name, values in arms.items()},
            'inference': {'status': 'insufficient_evidence', 'independentTasks': 1 if assessed else 0,
                'pValue': None, 'confidenceInterval': None,
                'reason': '按案例、输入、评测器、协议、模型与预算精确分组；重复同一任务只估计该任务的随机波动，'
                          '不能把验收项或重复运行当作独立任务做显著性检验。'},
            'notice': '费用和token分母为实际成功任务数，分子包含失败与取消请求；未知不当0，零成功单独标注。'})
    return summaries


def _budget_audit(record, single, multi):
    budget = record.get('budget', {})
    controlled, cap = record.get('schema') == SCHEMA, budget.get('cap')
    calls = [call for arm in (single, multi) for call in arm.get('calls', [])]
    accounting = all(_total_tokens(arm) is not None for arm in (single, multi))
    execution_complete = (single.get('status') == multi.get('status') == 'completed'
        and len(single.get('calls', [])) == 1 and len(multi.get('calls', [])) == 4
        and all(call['status'] == 'completed' for call in calls))
    if budget.get('mode') == 'provider_default':
        return {'status': 'not_controlled', 'equalOutputAllowance': False,
            'allocationWithinCap': None, 'fullAllowanceAllocated': None, 'observedOutputOverrun': None,
            'executionComplete': execution_complete, 'tokenAccountingComplete': accounting,
            'homogeneousDeclared': record.get('homogeneous') is True,
            'totalComputeMatched': False, 'strictBudgetMatched': False,
            'reasons': ['本次未设置总额度或岗位固定配额，供应商协议技术参数不代表费用预算。',
                        '实际用量与费用可能不同，只能描述本次系统配置表现，不能归因于等预算下的协作优势。']}
    equal = controlled and cap is not None and all(arm.get('configuredBudget') == cap for arm in (single, multi))
    allocation_ok = equal and all(sum(call['cap'] for call in arm.get('calls', [])) <= cap for arm in (single, multi))
    fully_allocated = equal and all(sum(call['cap'] for call in arm.get('calls', [])) == cap for arm in (single, multi))
    reasons = []
    if not controlled:
        reasons.append('旧“单模型”臂由同一型号承担多个独立岗位，不是严格 SAS；旧记录不改写。')
    if not allocation_ok or not fully_allocated:
        reasons.append('没有证据证明两臂执行了相等输出额度上限。')
    reasons.append('当前只约束供应商输出token参数；输入、隐藏推理与实际费用未全部对齐，不能归因于协作本身。')
    if not accounting:
        reasons.append('至少一臂调用usage不完整，COR与总token成本保持未知。')
    if record.get('homogeneous') is not True:
        reasons.append('异构型号或历史配置混合了模型能力与计价差异；跨模型token不代表相同FLOPs。')
    observed_overrun = any(_output_tokens(call) is not None and _output_tokens(call) > call['cap'] for call in calls)
    if not execution_complete:
        reasons.append('两臂尚未完成预定调用，声明相同额度不等于已完成预算遵守核验。')
    if observed_overrun:
        reasons.append('供应商回执输出量超过请求上限，该次参数预算未得到遵守。')
    return {'status': 'output_allowance_only' if allocation_ok and fully_allocated and accounting and execution_complete and not observed_overrun else 'unverified',
        'equalOutputAllowance': equal, 'allocationWithinCap': allocation_ok, 'fullAllowanceAllocated': fully_allocated,
        'observedOutputOverrun': observed_overrun, 'executionComplete': execution_complete,
        'tokenAccountingComplete': accounting,
        'homogeneousDeclared': record.get('homogeneous') is True,
        'totalComputeMatched': False, 'strictBudgetMatched': False, 'reasons': reasons}


def _total_tokens(arm):
    usage = arm.get('metrics', {}).get('usage', {})
    if not usage.get('complete') or usage.get('inputTokens') is None or usage.get('outputTokens') is None:
        return None
    return usage['inputTokens'] + usage['outputTokens']


def _output_tokens(call):
    usage = call.get('receipt', {}).get('usage', {})
    return usage.get('output_tokens', usage.get('completion_tokens'))


def _coordination(arm):
    calls = arm.get('calls', [])
    selected = [call for call in calls if call.get('role') in ('plan', 'aggregate')]
    count = len(calls) if calls else arm.get('metrics', {}).get('callCount', 0)
    selected_count = len(selected) if calls else arm.get('metrics', {}).get('coordinationCallCount')
    return {'callCount': selected_count, 'totalCalls': count,
            'callShare': selected_count / count if selected_count is not None and count else None,
            'basis': 'plan_and_aggregate_call_count' if calls else 'legacy_coordination_phase_call_count',
            'notice': '协调调用占比只描述拓扑，不等于协调token开销或可删除的浪费。'}


def _success(arm):
    return arm.get('status') == 'completed' and arm.get('evaluation', {}).get('accepted') is True


def _relative(baseline, observed):
    return (observed - baseline) / baseline if baseline is not None and baseline > 0 and observed is not None else None


def _cost_summary(arms):
    costs = [arm.get('metrics', {}).get('cost', {}) for arm in arms]
    known = all(cost.get('complete') is True and cost.get('basis') == 'actual_receipt'
                and cost.get('amount') is not None and cost.get('currency') for cost in costs)
    currencies = {cost.get('currency') for cost in costs}
    compatible = known and len(currencies) == 1
    total = sum(cost['amount'] for cost in costs) if compatible else None
    successes = sum(_success(arm) for arm in arms)
    return {'status': 'unknown' if not known else 'currency_mismatch' if not compatible else 'no_success' if not successes else 'measured',
            'total': total, 'successfulTasks': successes, 'attemptedTasks': len(arms),
            'currency': next(iter(currencies)) if compatible else None,
            'value': total / successes if total is not None and successes else None}


def _token_summary(arms):
    values = [_total_tokens(arm) for arm in arms]
    total = sum(values) if all(value is not None for value in values) else None
    successes = sum(_success(arm) for arm in arms)
    return {'status': 'unknown' if total is None else 'no_success' if not successes else 'measured',
            'total': total, 'successfulTasks': successes, 'attemptedTasks': len(arms),
            'value': total / successes if total is not None and successes else None,
            'unit': 'reported_input_plus_output_tokens'}


def _pareto(scores, tokens):
    status = 'unknown'
    if all(value is not None for value in scores + tokens):
        sq, mq = scores
        st, mt = tokens
        status = ('equal' if sq == mq and st == mt else 'multi_dominates_observed'
                  if mq >= sq and mt <= st else 'single_dominates_observed'
                  if sq >= mq and st <= mt else 'tradeoff')
    return {'status': status, 'basis': 'quality_score_vs_reported_total_tokens',
            'notice': '仅该样本的质量/用量位置，不是统计结论、真实费用或因果优越性证明。'}
