"""Fixed business cases and descriptive, task-matched collaboration measures."""
import copy
import hashlib
import json
import math
from datetime import datetime
from development_evaluation import CASE_ID as DEVELOPMENT_CASE_ID


CASE_ID = 'order-fulfillment-v1'
RESEARCH_CASE_ID = 'research-evidence-v2'
COORDINATION_PHASES = {'draft', 'plan', 'review', 'decision', 'clarification', 'verification', 'integration'}
ORDERS = [
    ('r01', 'O100', 2, False, {'A': 4, 'B': 2}),
    ('r02', 'O101', 1, False, {'A': 5, 'C': 2}),
    ('r03', 'O102', 3, False, {'B': 5}),
    ('r04', 'O103', 1, False, {'B': 4, 'C': 2}),
    ('r05', 'O104', 2, False, {'A': 4, 'C': 1}),
    ('r06', 'O105', 1, True, {'A': 1, 'C': 1}),
    ('r07', 'O106', 3, False, {'C': 3}),
    ('r08', 'O101', 1, False, {'A': 5, 'C': 2}),
    ('r09', 'O107', 2, False, {'A': 2, 'B': 2}),
    ('r10', 'O108', 1, False, {'A': 2, 'B': 3}),
    ('r11', 'O109', 2, False, {'C': 2}),
    ('r12', 'O110', 3, False, {'A': 1, 'C': 1}),
]
REFERENCE_ROWS = [
    ('r01', 'O100', 'rejected', {}, {'B': 1}),
    ('r02', 'O101', 'fulfilled', {'A': 5, 'C': 2}, {}),
    ('r03', 'O102', 'rejected', {}, {'B': 4}),
    ('r04', 'O103', 'fulfilled', {'B': 4, 'C': 2}, {}),
    ('r05', 'O104', 'fulfilled', {'A': 4, 'C': 1}, {}),
    ('r06', 'O105', 'cancelled', {}, {}),
    ('r07', 'O106', 'rejected', {}, {'C': 2}),
    ('r08', 'O101', 'duplicate', {}, {}),
    ('r09', 'O107', 'rejected', {}, {'A': 1, 'B': 1}),
    ('r10', 'O108', 'fulfilled', {'A': 2, 'B': 3}, {}),
    ('r11', 'O109', 'rejected', {}, {'C': 1}),
    ('r12', 'O110', 'fulfilled', {'A': 1, 'C': 1}, {}),
]


def _order_case_definition():
    orders = [dict(zip(('rowId', 'orderId', 'priority', 'cancelled', 'items'), row)) for row in ORDERS]
    result = {'decisions': [dict(zip(('rowId', 'orderId', 'status', 'allocated', 'shortages'), row))
                            for row in REFERENCE_ROWS],
              'processingOrder': ['r02', 'r04', 'r10', 'r01', 'r05', 'r09', 'r11', 'r03', 'r07', 'r12'],
              'remainingInventory': {'A': 0, 'B': 1, 'C': 0},
              'summary': {'fulfilled': 5, 'rejected': 5, 'cancelled': 1, 'duplicate': 1}}
    instructions = '''处理 inputs/inventory.json 与 inputs/orders.json，交付唯一的 result.json。
这是数据/文字业务任务：所有模块 workType 为 data、writing 或 general，complexity 不超过2，
requiredCapabilities 仅 text；不用编程、脚本、联网、原生工具或交叉讨论。
可以分工核对业务规则与订单，但只能有一个最终汇总模块拥有 result.json 写入范围；其他模块用不同文件交接。
规则：先按输入顺序去重，同一 orderId 只保留第一次，后续行一律 duplicate。
首次出现且 cancelled=true 的行标 cancelled，不分配库存。其余行按 priority 数字升序处理，
相同 priority 按输入顺序。检查全部商品：任一不足即整单 rejected，不扣任何库存；
shortages 只列不足商品及需求减当前库存的正整数。全部足够则 fulfilled 并扣除全部需求。
duplicate/cancelled 的 allocated 和 shortages 都是空对象；rejected 的 allocated 是空对象；
fulfilled 的 allocated 等于原需求，shortages 是空对象。不得补货或重新处理拒绝的订单。
result.json 为JSON对象，字段为 decisions、processingOrder、remainingInventory、summary。
decisions 按输入顺序包含全部12行，每行包含 rowId、orderId、status、allocated、shortages；
status 仅 fulfilled/rejected/cancelled/duplicate。processingOrder 是实际参与库存分配的 rowId 数组，
不含 duplicate/cancelled。remainingInventory 含全部SKU；summary 含四种status的行数。
所有数量和计数为非负整数。不要用自评分或完成声明代替实际结果文件。'''
    rubric = [{'id': row[0], 'title': row[0] + ' 订单决定、分配量与缺货量', 'required': True}
              for row in ORDERS]
    rubric += [{'id': key, 'title': title, 'required': True} for key, title in [
        ('processingOrder', '优先级及同级处理顺序'), ('remainingInventory', '最终剩余库存'),
        ('summary', '订单完整性、输出顺序及状态汇总')]]
    definition = {'id': CASE_ID, 'title': '订单履约：优先级、整单拒绝、去重与取消',
                  'instructions': instructions, 'files': [
                      {'path': 'inputs/inventory.json', 'content': json.dumps({'A': 12, 'B': 8, 'C': 6})},
                      {'path': 'inputs/orders.json', 'content': json.dumps(orders, ensure_ascii=False, indent=2)}],
                  'rubric': rubric, 'reference': {'result': result, 'explanation': [
                      'r06取消；r08重复O101，不参与分配。优先级1依次履约r02/r04/r10，库存变成A5/B1/C2。',
                      '优先级2：r01缺B1整单拒绝，A仍为5；r05履约后A1/B1/C1；r09缺A1/B1；r11缺C1。',
                      '优先级3：r03缺B4；r07缺C2；r12履约后A0/B1/C0。共5履约、5拒绝、1取消、1重复。']}}
    return copy.deepcopy(definition)


def _research_case_definition():
    instructions = '''只根据 sources/ 中的8份给定资料，为北岚协作云 Team 标准方案整理证据结论。
这是虚构的离线资料综合任务，不验证联网搜索能力。观察时点 asOf 固定为 2026-10-15T12:00:00+08:00。
所有模块 workType 为 research、data、writing 或 general，complexity 不超过2，requiredCapabilities 仅 text；
不要联网、调用工具、编程或交叉讨论。分配时优先匹配项目 roleDirectory 中适合证据分析/核对/文字汇总的自定义岗位，
使用目录里真实的 roleId；没有匹配岗位时再用默认岗位。可以分工核对资料，但只有一个汇总模块写 result.json，
其他模块使用不同文件交接。不能用自评分或完成声明代替结果。
以已批准且在观察时点生效的适用条款为准；同一条款的官方勘误优先。发布日期晚不等于内容更新。
草案、未批准计划不是确定承诺；营销上限、其他套餐或测试账号不是 Team 标准条款。
事故影响即时可用状态，不改变合同配额。缺乏适用证据的事项保留 unverified 和 null，不能猜测。
输出唯一的 result.json，JSON对象包含 asOf、findings、sourceDispositions。
findings 包含以下7个唯一id（顺序不限）：seat_limit（整数席位）、monthly_exports（整数月配额）、
history_days（整数保留天数）、support_response（next_business_day 或 24x7）、
east_export_status（available 或 paused）、november_upgrade（11月升级为10席是否已获批准，布尔值）、
cross_region_migration（是否支持跨区域迁移，布尔值）。每项含 id、status（confirmed/unverified）、value、sourceIds。
confirmed 表示该结论有明确证据，value 可以是 false；已确认尚未批准，不代表确认未来不会升级。
每项引用直接支持该结论的最少 sourceId，不引用反例充数；月配额可以同时引用原条款及勘误。
unverified 的 value 必须为 null；明确未批准的计划引用对应草案，完全无依据的事项 sourceIds 为 []。
sourceDispositions 为覆盖全部 S01 至 S08 的对象，每项取以下一个分类：current（有效条款/勘误）、
superseded（被替代的正式条款）、draft（未批准草案）、stale（过期说明）、
non_normative（非规范营销）、out_of_scope（套餐/账号不适用）、active_incident（生效中的事故通知）。'''
    sources = [
        ('S01', 'Team 标准方案 v1', '''发布方：北岚协作云官方产品委员会。文类：已批准正式条款。
发布：2026-07-20。生效：2026-08-01 至 2026-10-09 23:59:59+08:00；此后由 v2 替代。
Team 标准方案包含5个席位，每月200次导出，历史保留30天。'''),
        ('S02', 'Team 标准方案 v2', '''发布方：北岚协作云官方产品委员会。文类：已批准正式条款。
发布：2026-09-25。生效：2026-10-10 00:00:00+08:00，替代 v1。
适用于 Team 标准方案：8个席位，每月500次导出，历史保留90天。
支持响应承诺为下一个工作日内首次响应（next_business_day），不承诺7x24响应。
经官方勘误的字段以勘误为准，其余条款继续有效。'''),
        ('S03', 'Team v2 配额勘误', '''发布方：北岚协作云官方产品委员会。文类：已批准官方勘误。
发布：2026-10-12。追溯生效：2026-10-10 00:00:00+08:00。
S02 的 Team 每月导出配额“500次”为排版错误，正确配额为350次。仅修改此字段，其他条款不变。'''),
        ('S04', 'Team v3 讨论稿', '''发布方：北岚协作云官方产品委员会。文类：DRAFT / 未批准草案。
发布：2026-10-14。拟议日期：2026-11-01；未完成审批，无生效日期，不能作为升级承诺。
提议升级为10个席位，每月800次导出，历史保留180天。是否采纳及上线时间尚未决定。'''),
        ('S05', '帮助中心：Team 常见问题', '''发布方：北岚协作云帮助中心。文类：FAQ转载。
页面重新发布：2026-10-13。内容最后复核：2026-09-20。适用版本：v1，尚未更新到v2。
本说明摘录旧版：5个席位，每月200次导出，历史保留30天。正式条款与本页不一致时以正式条款为准。'''),
        ('S06', '合作伙伴宣传页', '''发布方：北岚协作云销售合作伙伴。文类：营销宣传，2026-10-14发布。
标题：最高10人畅享，无限导出，7x24全程陪伴！
脚注：展示全产品线可能达到的上限，活动和套餐另有条件；本页不构成任何具体套餐的条款或服务承诺。'''),
        ('S07', '第三方体验报告', '''发布方：独立评测员南风。文类：试用观察，2026-10-15发布。
样本为 Enterprise beta 专属测试账号，并非 Team 标准方案；该账号有临时配额覆盖。
试用中12个用户、2000次导出可用，客服约10分钟响应。作者未验证正式Team条款。'''),
        ('S08', '华东导出暂停通知', '''发布方：北岚协作云官方状态页。文类：已确认事故通知。
发布：2026-10-14 17:30:00+08:00。影响：华东区域全部套餐的导出服务。
暂停区间：2026-10-14 18:00:00+08:00（含）至2026-10-16 09:00:00+08:00（不含）。
区间内导出状态为 paused。该事故不修改任何套餐的合同月配额；无补充通知。''')]
    values = [('seat_limit', '席位与有效版本', 'confirmed', 8, ['S02']),
              ('monthly_exports', '勘误后的月配额', 'confirmed', 350, ['S03']),
              ('history_days', '历史保留期限', 'confirmed', 90, ['S02']),
              ('support_response', '支持响应承诺', 'confirmed', 'next_business_day', ['S02']),
              ('east_export_status', '事故区间内可用状态', 'confirmed', 'paused', ['S08']),
              ('november_upgrade', '计划批准状态', 'confirmed', False, ['S04']),
              ('cross_region_migration', '无证据事项不推断', 'unverified', None, [])]
    dispositions = {'S01': 'superseded', 'S02': 'current', 'S03': 'current', 'S04': 'draft',
                    'S05': 'stale', 'S06': 'non_normative', 'S07': 'out_of_scope', 'S08': 'active_incident'}
    rubric = [{'id': row[0], 'title': row[1] + '及引用', 'required': True} for row in values]
    rubric += [{'id': key, 'title': title, 'required': True} for key, title in [
        ('asOf', '固定观察时点'), ('current_sources', '有效条款、勘误及事故来源'),
        ('old_sources', '被替代条款与过期FAQ'), ('draft_source', '未批准草案'),
        ('unsuitable_sources', '营销与不适用样本'), ('completeness', '结论及来源清单完整唯一')]]
    return {'id': RESEARCH_CASE_ID, 'title': '资料综合：有效版本、勘误、草案与证据边界',
            'instructions': instructions, 'files': [
                {'path': 'sources/' + sid + '.md', 'content': '# ' + sid + ' · ' + title + '\n\n' + body}
                for sid, title, body in sources], 'rubric': rubric,
            'reference': {'result': {'asOf': '2026-10-15T12:00:00+08:00',
                'findings': [{'id': sid, 'status': status, 'value': value, 'sourceIds': refs}
                             for sid, title, status, value, refs in values], 'sourceDispositions': dispositions},
                'sourceAlternatives': {'monthly_exports': [['S03'], ['S02', 'S03']]},
                'explanation': [
                    '观察时点已过v2生效日：8席、90天、下一工作日响应来自S02；350次由S03勘误确定。',
                    '观察时点处于S08暂停区间，华东状态paused；即时故障不把350的合同配额变成0。',
                    'S04明确尚未批准，批准状态为confirmed/false；这不代表未来不会升级。跨区域迁移没有适用证据。',
                    'S01已替代，S05转发旧内容，S06没有规范效力，S07是另一个套餐的测试账号。']}}


def case_definition(case_id=CASE_ID):
    from development_evaluation import case_definition as development_case
    factories = {CASE_ID: _order_case_definition, RESEARCH_CASE_ID: _research_case_definition,
                 DEVELOPMENT_CASE_ID: development_case}
    if case_id not in factories:
        raise ValueError('未知评测案例：' + str(case_id))
    return factories[case_id]()


def public_case(case_id=CASE_ID):
    definition = case_definition(case_id)
    del definition['reference']
    return definition


def public_cases():
    return [public_case(case_id) for case_id in (CASE_ID, RESEARCH_CASE_ID, DEVELOPMENT_CASE_ID)]


def evaluate_delivery(files, case_id=CASE_ID):
    if case_id == DEVELOPMENT_CASE_ID:
        from development_evaluation import evaluate_delivery as evaluate_development
        return evaluate_development(files)
    case = case_definition(case_id)
    evaluator = _evaluation if case_id == CASE_ID else _research_evaluation
    outputs = [item for item in files if item.get('path') == 'result.json']
    if not outputs:
        return evaluator(case, {}, None, 'not_run', '未交付 result.json。')
    if len(outputs) != 1 or not isinstance(outputs[0].get('content'), str):
        return evaluator(case, {}, None, 'failed', 'result.json 必须是唯一的UTF-8文本文件。')
    content = outputs[0]['content']
    fingerprint = hashlib.sha256(content.encode()).hexdigest()
    try:
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError('结果须为JSON对象')
    except (ValueError, TypeError):
        return evaluator(case, {}, fingerprint, 'failed', 'result.json 不是有效JSON对象。')
    return evaluator(case, result, fingerprint)


def _quantities(actual, expected):
    return (isinstance(actual, dict) and actual == expected
            and all(type(value) is int and value >= 0 for value in actual.values()))


def _evaluation(case, result, fingerprint, forced_status=None, detail=None):
    expected = case['reference']['result']
    decisions = result.get('decisions')
    rows = decisions if isinstance(decisions, list) else []
    passed, differences = {}, {}
    for reference in expected['decisions']:
        matches = [row for row in rows if isinstance(row, dict) and row.get('rowId') == reference['rowId']]
        actual = matches[0] if len(matches) == 1 else {}
        passed[reference['rowId']] = (actual.get('orderId') == reference['orderId']
            and actual.get('status') == reference['status']
            and _quantities(actual.get('allocated'), reference['allocated'])
            and _quantities(actual.get('shortages'), reference['shortages']))
        differences[reference['rowId']] = ('固定结果：' + json.dumps(reference, ensure_ascii=False)
                                           + '；实际：' + json.dumps(actual, ensure_ascii=False))
    passed['processingOrder'] = result.get('processingOrder') == expected['processingOrder']
    passed['remainingInventory'] = _quantities(result.get('remainingInventory'), expected['remainingInventory'])
    passed['summary'] = (_quantities(result.get('summary'), expected['summary'])
        and all(isinstance(row, dict) for row in rows)
        and [row.get('rowId') for row in rows] == [row['rowId'] for row in expected['decisions']])
    for key in ('processingOrder', 'remainingInventory', 'summary'):
        differences[key] = ('固定结果：' + json.dumps(expected[key], ensure_ascii=False)
                            + '；实际：' + json.dumps(result.get(key), ensure_ascii=False))
    if not passed['summary']:
        differences['summary'] += '；decisions还须按原顺序完整且唯一地覆盖12条订单。'
    return _scored_evaluation(case, passed, differences, fingerprint, forced_status, detail)


def _research_evaluation(case, result, fingerprint, forced_status=None, detail=None):
    expected = case['reference']['result']
    rows = result.get('findings') if isinstance(result.get('findings'), list) else []
    dispositions = result.get('sourceDispositions') if isinstance(result.get('sourceDispositions'), dict) else {}
    passed, differences = {}, {}
    for reference in expected['findings']:
        key = reference['id']
        matches = [row for row in rows if isinstance(row, dict) and row.get('id') == key]
        actual = matches[0] if len(matches) == 1 else {}
        alternatives = case['reference']['sourceAlternatives'].get(key, [reference['sourceIds']])
        refs = actual.get('sourceIds')
        valid_refs = (isinstance(refs, list) and all(isinstance(ref, str) for ref in refs)
                      and any(sorted(refs) == sorted(option) for option in alternatives))
        passed[key] = (actual.get('status') == reference['status'] and 'value' in actual
                       and type(actual['value']) is type(reference['value'])
                       and actual['value'] == reference['value'] and valid_refs)
        differences[key] = ('固定结论：' + json.dumps(reference, ensure_ascii=False)
                            + '；允许引用组合：' + json.dumps(alternatives, ensure_ascii=False)
                            + '；实际：' + json.dumps(actual, ensure_ascii=False))
    passed['asOf'] = result.get('asOf') == expected['asOf']
    differences['asOf'] = 'asOf 须为 ' + expected['asOf']
    for key, source_ids in [('current_sources', ['S02', 'S03', 'S08']), ('old_sources', ['S01', 'S05']),
                            ('draft_source', ['S04']), ('unsuitable_sources', ['S06', 'S07'])]:
        expected_group = {sid: expected['sourceDispositions'][sid] for sid in source_ids}
        actual_group = {sid: dispositions.get(sid) for sid in source_ids}
        passed[key] = actual_group == expected_group
        differences[key] = ('固定分类：' + json.dumps(expected_group, ensure_ascii=False)
                            + '；实际：' + json.dumps(actual_group, ensure_ascii=False))
    ids = [row.get('id') for row in rows if isinstance(row, dict)]
    passed['completeness'] = (len(ids) == len(rows) and all(isinstance(key, str) for key in ids)
                              and sorted(ids) == sorted(row['id'] for row in expected['findings'])
                              and sorted(dispositions) == sorted(expected['sourceDispositions']))
    differences['completeness'] = 'findings须完整且唯一地覆盖7项；sourceDispositions须恰好覆盖S01至S08。'
    return _scored_evaluation(case, passed, differences, fingerprint, forced_status, detail)


def _scored_evaluation(case, passed, differences, fingerprint, forced_status, detail):
    criteria = [{**item, 'status': forced_status or ('passed' if passed[item['id']] else 'failed'),
                 'detail': detail or ('与固定参考结果一致。' if passed[item['id']] else differences[item['id']])}
                for item in case['rubric']]
    count = sum(item['status'] == 'passed' for item in criteria)
    return {'criteria': criteria, 'passed': count, 'total': len(criteria),
            'scorePct': round(count * 100 / len(criteria), 1), 'accepted': count == len(criteria),
            'artifactFingerprint': fingerprint}


def run_metrics(run):
    calls, nodes = run.get('callMetrics', []), run.get('nodes', [])
    tracked = 'callMetrics' in run
    phases = {node['id']: node.get('phase') for node in nodes}
    coordination = [call for call in calls if phases.get(call.get('nodeId')) in COORDINATION_PHASES]
    latest = {call['nodeId']: call for call in calls}
    known = []
    actual_models = set()
    returned_model_calls = 0
    for node in nodes:
        receipt = node.get('receipt') or {}
        model = receipt.get('returnedModel') or receipt.get('model')
        if isinstance(model, str) and model:
            actual_models.add(model)
        call = latest.get(node['id'])
        # A node retains its latest receipt; earlier calls cannot borrow its usage.
        if not call or (node.get('contextRef') and call.get('contextRef') != node['contextRef']):
            continue
        if isinstance(model, str) and model:
            returned_model_calls += 1
        usage = receipt.get('usage') or {}
        incoming = usage.get('input_tokens', usage.get('prompt_tokens'))
        outgoing = usage.get('output_tokens', usage.get('completion_tokens'))
        if type(incoming) is int and incoming >= 0 and type(outgoing) is int and outgoing >= 0:
            known.append((incoming, outgoing))
    elapsed = [call.get('elapsedMs') for call in coordination]
    return {'wallMs': _wall_ms(run.get('started'), run.get('ended')), 'callCount': len(calls),
            'errorCount': sum(call.get('outcome') == 'error' for call in calls),
            'cancelledCount': sum(call.get('outcome') == 'cancelled' for call in calls),
            'repairCount': sum(node.get('phase') == 'build' and node.get('repairRound', 0) > 0
                               and bool(node.get('started')) for node in nodes),
            'coordinationCallCount': len(coordination),
            'coordinationMs': sum(elapsed) if all(_number(value) for value in elapsed) else None,
            'usage': {'inputTokens': sum(row[0] for row in known) if known or (tracked and not calls) else None,
                      'outputTokens': sum(row[1] for row in known) if known or (tracked and not calls) else None,
                      'knownCalls': len(known), 'totalCalls': len(calls), 'complete': tracked and len(known) == len(calls)},
            'cost': {'status': 'unknown', 'amount': None}, 'actualModels': sorted(actual_models),
            'requestedModels': sorted({call['model'] for call in calls if isinstance(call.get('model'), str) and call['model']}),
            'returnedModelCalls': returned_model_calls,
            'modelEvidenceComplete': tracked and bool(calls) and returned_model_calls == len(calls)}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _wall_ms(started, ended):
    if not isinstance(started, str) or not isinstance(ended, str):
        return None
    try:
        parsed = []
        for value in (started, ended):
            # Python 3.9 needs a colon in the offset emitted by app.stamp().
            if len(value) >= 5 and value[-5] in '+-' and value[-4:].isdigit():
                value = value[:-2] + ':' + value[-2:]
            parsed.append(datetime.fromisoformat(value.replace('Z', '+00:00')))
        elapsed = (parsed[1] - parsed[0]).total_seconds() * 1000
        return elapsed if elapsed >= 0 else None
    except (ValueError, TypeError):
        return None


def efficiency_points(duration_reduction_pct):
    return math.floor(max(0, duration_reduction_pct) / 10) if duration_reduction_pct is not None else None


def duration_efficiency(single, multi):
    durations = [arm['metrics'].get('wallMs') for arm in (single, multi)]
    eligible = (all(arm.get('status') == 'completed' and arm['evaluation']['accepted'] for arm in (single, multi))
                and all(_number(value) and value > 0 for value in durations))
    reduction = (durations[0] - durations[1]) / durations[0] * 100 if eligible else None
    return {'durationReductionPct': reduction, 'globalPoints': efficiency_points(reduction),
            'basis': 'completed_accepted_wall_time',
            'notice': '仅双方完成、验收达标且总历时有效时计分；整体用时每减少10%计1分，按未舍入比例向下取整。'
                      '负值保留显示但不加分；未知不记0。局部效率缺少同任务同标准的真实基线，仍待评测，不能叠加计分。'
                      '这是描述性积分，不代表统计显著性或普遍收益。'}


def compare_arms(single, multi, case_id=CASE_ID):
    accepted = [arm.get('status') == 'completed' and arm['evaluation']['accepted'] for arm in (single, multi)]
    scores = [arm['evaluation'].get('scorePct') for arm in (single, multi)]
    durations = [arm['metrics'].get('wallMs') for arm in (single, multi)]
    efficiency = duration_efficiency(single, multi)
    speedup = round(durations[0] / durations[1], 3) if efficiency['durationReductionPct'] is not None else None
    outcome = ('本次两臂均达标。' if all(accepted) else '本次仅多模型臂完成并达标。' if accepted[1]
               else '本次仅单模型臂完成并达标。' if accepted[0] else '本次两臂均未同时满足完成与达标。')
    metrics = multi['metrics']
    requested, actual = metrics.get('requestedModels', []), metrics.get('actualModels', [])
    participation = []
    if len(requested) <= 1:
        participation.append('多模型臂的调用记录仅包含%d个请求成员，不能确认已执行多成员协作。' % len(requested))
    elif len(actual) == 1:
        participation.append('请求了多个配置别名，但现有回执仅见同一实际型号，不能按多个已验证型号计。')
    if not metrics.get('modelEvidenceComplete'):
        participation.append('实际型号回执不完整（%d/%d次调用），不能据此声称已验证跨厂商协作。' %
                             (metrics.get('returnedModelCalls', 0), metrics.get('callCount', 0)))
    if not participation:
        participation.append('已记录多个请求成员及实际型号；本记录未独立核验厂商身份，不单独证明跨厂商增益。')
    case_notice = ('本案例运行实际交付代码，比较同模型多岗位复核与多模型协作；讨论人数与分工可不同，'
                   '不把整个工作流差异归因于单一模型或某条讨论。总历时包含最终宿主代码验收。'
                   if case_id == DEVELOPMENT_CASE_ID else
                   '本案例复杂度不超过2且不包含讨论；结果只比较整体流程配置、分派和交接，不证明讨论带来的收益。')
    return {'n': 1, 'single': copy.deepcopy(single), 'multi': copy.deepcopy(multi),
            'qualityDeltaPoints': round(scores[1] - scores[0], 1) if all(_number(s) for s in scores) else None,
            'speedupRatio': speedup, 'efficiency': efficiency, 'observedOutcome': outcome,
            'participationNotice': ''.join(participation),
            'notice': '单个同任务对照的描述性结果，不代表普遍优势或统计显著性。分数来自固定业务参考答案；'
                      'wallMs含开始至结束的等待时间，coordinationMs为协调类调用耗时之和、不是关键路径。'
                      '已知token数仅覆盖knownCalls；未知费用不记为0，不能据此比较成本收益。' + case_notice}
