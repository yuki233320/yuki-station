"""Directed role handoffs observed in production sessions, without causal scores."""
import hashlib
import json
import math


NOTICE = ('仅真实业务会话中已经开始执行的直接依赖；同一运行的模块边只算一次。'
          '可追溯只证明冻结契约与收到的文件一致，不证明正确使用。下游关卡是模块整体结果，'
          '多个上游共享该结果，不能归功于任一岗位。会话级区间以会话独立为工作假设，'
          '任务差异与历史相关性尚未排除；这些观测不是因果收益或模型排名。')


def build_role_cooperation(sessions):
    groups, eligible_runs, missing_modules, planned_edges, not_reached = {}, 0, 0, 0, 0
    for session in sessions:
        for run in session.get('runs', []):
            if run.get('mode') != 'live' or run.get('workflowVersion') != 2:
                continue
            eligible_runs += 1
            modules = {m['id']: m for m in (run.get('plan') or {}).get('modules', [])}
            for target in modules.values():
                dependencies = list(dict.fromkeys(target.get('dependsOn', [])))
                planned_edges += len(dependencies)
                builds = [n for n in run.get('nodes', []) if n.get('moduleId') == target['id']
                          and n.get('phase') == 'build' and n.get('started')]
                if not builds:
                    not_reached += len(dependencies)
                    continue
                for mid in dependencies:
                    source = modules.get(mid)
                    if source is None:
                        missing_modules += 1
                        continue
                    profile = _profile(run, source, target)
                    group = groups.setdefault(profile['id'], {'profile': profile, 'edges': []})
                    group['edges'].append(_edge(session, run, source, target, builds))
    rows = [_row(group['profile'], group['edges']) for group in groups.values()]
    return {'schema': 'yuki-role-cooperation/v1', 'rows': sorted(rows, key=lambda row: row['id']),
            'coverage': {'eligibleRuns': eligible_runs, 'plannedEdges': planned_edges, 'notReachedEdges': not_reached, 'opportunities': sum(r['opportunities'] for r in rows),
                'unknownHandoffs': sum(r['handoff']['unknown'] for r in rows),
                'missingDependencyModules': missing_modules, 'rolePairGroups': len(rows),
                'notice': '缺少旧契约、交接或关卡时显示未知；未开始的下游单列未到达边，不记失败或交接机会。'},
            'interaction': {'value': None, 'status': 'missing_matched_ablations',
                'notice': '未取得同任务、同预算、同验收的岗位组合四臂消融，不能估计超出各岗位单独贡献的协同增益。'},
            'notice': NOTICE}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _profile(run, source, target):
    config = run.get('projectSnapshot', {}).get('teamConfig', {})
    saved_roles = {r['roleId']: r for r in run.get('teamSnapshot', {}).get('roles', [])}
    specs = []
    for module in (source, target):
        rid = module.get('roleId')
        inherited = module.get('roleType')
        saved = saved_roles.get(rid, {})
        specs.append({'roleId': rid, 'roleType': inherited,
            'definition': module.get('roleDefinition') or saved.get('definition') or config.get('customRoles', {}).get(rid),
            'guide': saved.get('guide') or config.get('roleGuides', {}).get(rid),
            'inheritedGuide': config.get('roleGuides', {}).get(inherited)})
    context = {'roleSpecs': specs, 'workPolicy': run.get('workPolicy')}
    profile = {'sourceRole': source.get('roleId'), 'targetRole': target.get('roleId'),
               'sameRole': source.get('roleId') == target.get('roleId'),
               'sourceModel': source.get('assignment', {}).get('model'),
               'targetModel': target.get('assignment', {}).get('model'),
               'sourceWorkType': source.get('workType'), 'targetWorkType': target.get('workType'),
               'contextFingerprint': _fingerprint(context),
               'contextComplete': run.get('workPolicy') is not None and all(s['guide'] or s['definition'] for s in specs),
               'contextNotice': '仅使用运行中保存的岗位规范与全局规则；外部岗位文件正文未随会话提供，无法核验其完整性。'}
    profile['id'] = _fingerprint(profile)
    return profile


def _edge(session, run, source, target, builds):
    quality = _quality_rows(run, target['id'])
    contract = run.get('taskContracts', {}).get(target['id'])
    first = _gate(quality[0][1], contract) if quality and quality[0][0].get('round') == 0 else None
    final = _gate(quality[-1][1], contract) if quality else None
    target_node = builds[0]
    packet = _first_packet(run, target_node)
    trace = _handoff(run, source, target, target_node, packet)
    repairs = ({n['id'] for n in builds if n.get('repairRound', 0) > 0}
               if run.get('qualityVersion') == 1 else None)
    return {'session': (session.get('projectId'), session['id']), 'run': run['id'],
            'sourceModule': source['id'], 'targetModule': target['id'],
            'handoff': trace, 'firstGate': first, 'finalGate': final,
            'evidence': {'sessionId': session['id'], 'runId': run['id'], 'sourceModule': source['id'],
                'targetModule': target['id'], 'targetNodeId': target_node['id'], 'packetId': packet.get('id') if packet else None,
                'handoff': trace, 'firstGate': first, 'finalGate': final},
            'repairs': repairs, 'clarifications': _clarifications(run, source, target),
            'shared': len(set(target.get('dependsOn', []))) > 1}


def _quality_rows(run, mid):
    records = run.get('qualityHistory', [])
    if all(record.get('eventSeq') is not None for record in records):
        records = sorted(records, key=lambda record: record['eventSeq'])
    return [(record, row) for record in records for row in record.get('modules', []) if row.get('moduleId') == mid]


def _gate(row, contract):
    if (_contract_state(contract) is not True or row.get('contractFingerprint') != contract['fingerprint']
            or row.get('contractId') != contract.get('id') or type(row.get('gatePassed')) is not bool):
        return None
    return row['gatePassed']


def _contract_state(contract):
    if not contract or not contract.get('fingerprint'):
        return None
    contents = {k: v for k, v in contract.items() if k not in ('fingerprint', 'created', 'eventSeq')}
    return _fingerprint(contents) == contract['fingerprint']


def _first_packet(run, target_node):
    packets = [p for p in run.get('handoffs', []) if p.get('nodeId') == target_node['id']]
    if packets and all(p.get('eventSeq') is not None for p in packets):
        packets.sort(key=lambda p: p['eventSeq'])
    return packets[0] if packets else None


def _handoff(run, source, target, target_node, packet):
    contracts = run.get('taskContracts', {})
    source_contract, target_contract = contracts.get(source['id']), contracts.get(target['id'])
    states = [_contract_state(c) for c in (source_contract, target_contract)]
    if None in states:
        return None
    if not all(states):
        return False
    if not packet or not packet.get('contextRef'):
        return None
    calls = [c for c in run.get('callMetrics', []) if c.get('nodeId') == target_node['id']]
    context_ref = calls[0].get('contextRef') if calls else target_node.get('contextRef')
    if not context_ref or not all(k in packet for k in ('contractId', 'contractFingerprint', 'sources', 'target', 'runId', 'projectRevision')):
        return None
    if (context_ref != packet['contextRef'] or packet['runId'] != run['id']
            or packet['projectRevision'] != run.get('projectRevision')
            or source_contract.get('sourceRevision') != run.get('projectRevision')
            or target_contract.get('sourceRevision') != run.get('projectRevision')
            or source_contract.get('roleId') != source.get('roleId') or target_contract.get('roleId') != target.get('roleId')
            or target_contract.get('model') != target_node.get('agent')
            or packet['contractId'] != target_contract['id'] or packet['contractFingerprint'] != target_contract['fingerprint']
            or source['id'] not in target_contract.get('dependsOn', [])
            or packet['target'].get('moduleId') != target['id']
            or packet['target'].get('roleId') != target.get('roleId')
            or packet['target'].get('model') != target_node.get('agent')):
        return False
    matches = [s for s in packet['sources'] if s.get('moduleId') == source['id']]
    if len(matches) != 1:
        return False
    proof = matches[0]
    source_node = next((n for n in run.get('nodes', []) if n['id'] == proof.get('nodeId')), None)
    if (not source_node or not all(k in proof for k in ('responseRef', 'model', 'files'))
            or not proof.get('responseRef') or not source_node.get('responseRef')):
        return None
    if (run['nodes'].index(source_node) >= run['nodes'].index(target_node)
            or source_node.get('moduleId') != source['id'] or source_node.get('phase') != 'build'
            or source_node.get('status') != 'done' or source_node.get('responseRef') != proof['responseRef']
            or source_node.get('agent') != proof['model'] or source_contract.get('model') != proof['model']):
        return False
    if any(not all(k in item for k in ('path', 'size', 'sha256')) for item in proof['files']):
        return None
    files = source_node.get('result', {}).get('files')
    if files is None or any('content' not in item for item in files):
        return None
    manifest = [{'path': item['path'], 'size': len(item['content'].encode()),
                 'sha256': hashlib.sha256(item['content'].encode()).hexdigest()} for item in files]
    return sorted(manifest, key=lambda f: f['path']) == sorted(proof['files'], key=lambda f: f['path'])


def _clarifications(run, source, target):
    if 'discussions' not in run:
        return None
    questions = {q['id']: q for q in run['discussions'] if q.get('kind') == 'clarification'
                 and q.get('moduleId') == target['id'] and q.get('fromRole') == target.get('roleId')
                 and q.get('toRole') == source.get('roleId')}
    same_role_sources = [m for m in (run.get('plan') or {}).get('modules', [])
                         if m['id'] in target.get('dependsOn', []) and m.get('roleId') == source.get('roleId')]
    states = {}
    for qid, question in questions.items():
        if len(same_role_sources) > 1:
            node = next((n for n in run.get('nodes', []) if n['id'] == question.get('nodeId')), {})
            original = next((q for i, q in enumerate(node.get('result', {}).get('questions', []), 1)
                             if qid == 'question-' + target['id'] + '-' + str(i)), {})
            if not original.get('toModule'):
                states[qid] = 'unknown'
                continue
            if original['toModule'] != source['id']:
                continue
        answers = [a for a in run['discussions'] if a.get('kind') == 'answer' and a.get('respondsTo') == qid
                   and a.get('fromRole') == source.get('roleId') and a.get('toRole') == target.get('roleId')]
        if not answers:
            states[qid] = 'unanswered'
            continue
        if all(a.get('eventSeq') is not None for a in answers):
            answers.sort(key=lambda a: a['eventSeq'])
        status = answers[-1].get('status')
        states[qid] = status if status in ('answered', 'unresolved') else 'unknown'
    return states


def _rate(values, positive='passed', negative='failed'):
    passed, failed = values.count(True), values.count(False)
    observed = passed + failed
    return {positive: passed, negative: failed, 'unknown': values.count(None), 'observed': observed,
            'total': len(values), 'ratePct': 100 * passed / observed if observed else None}


def _row(profile, edges):
    first, final, handoffs = [[e[key] for e in edges] for key in ('firstGate', 'finalGate', 'handoff')]
    by_session = {}
    for edge in edges:
        by_session.setdefault(edge['session'], []).extend([edge['handoff'], edge['firstGate']])
    statuses = [False if False in values else None if None in values else True for values in by_session.values()]
    success = _rate(statuses)
    success.update(wilson95=_wilson(success['passed'], success['observed']),
                   insufficientSamples=success['observed'] < 3)
    clarifications = {'questions': 0, 'answered': 0, 'unresolved': 0, 'unanswered': 0, 'unknown': 0}
    known = [edge for edge in edges if edge['clarifications'] is not None]
    question_states = {(edge['session'], edge['run'], qid): status for edge in known
                       for qid, status in edge['clarifications'].items()}
    clarifications['questions'] = len(question_states)
    for status in question_states.values():
        clarifications[status] += 1
    denominator = sum(clarifications[key] for key in ('answered', 'unresolved', 'unanswered'))
    clarifications.update(observedOpportunities=len(known), unknownOpportunities=len(edges) - len(known),
        answeredRatePct=100 * (clarifications['answered'] + clarifications['unresolved']) / denominator if denominator else None)
    if not known:
        for key in ('questions', 'answered', 'unresolved', 'unanswered', 'unknown'):
            clarifications[key] = None
    known_repairs = [edge for edge in edges if edge['repairs'] is not None]
    repair_ids = {(edge['session'], edge['run'], nid) for edge in known_repairs for nid in edge['repairs']}
    return {**profile, 'opportunities': len(edges), 'sessionCount': len(by_session),
            'handoff': _rate(handoffs, 'verified', 'mismatched'), 'firstGate': _rate(first), 'finalGate': _rate(final),
            'repairs': {'startedBuilds': len(repair_ids) if known_repairs else None,
                        'observedOpportunities': len(known_repairs), 'unknownOpportunities': len(edges) - len(known_repairs)},
            'clarifications': clarifications, 'sessionSuccess': success,
            'evidence': [edge['evidence'] for edge in edges],
            'sharedDownstreamOpportunities': sum(e['shared'] for e in edges)}


def _wilson(passed, total):
    if not total:
        return None
    z = 1.959963984540054
    p = passed / total
    denominator = 1 + z * z / total
    middle = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {'lowPct': 100 * max(0, middle - half), 'highPct': 100 * min(1, middle + half)}
