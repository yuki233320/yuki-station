"""Conservative contribution records derived from persisted workflow evidence."""
import copy
import hashlib
import json


NOTICE = ('每个模块的固定验收项只计一次；普通贡献 1 分，深层追问贡献按本轮冻结权重计分，重复来源不叠加。'
          '采纳、模型判断和测试命令返回不自动产生贡献分。'
          '客观验证须同一评测器、同一标准及对应前后产物；人工核验单独标记。'
          '同一模块的同一前后产物已有客观验证积分时，合同候选保守地不另计分，不表示两类标准语义等价。'
          '首交质量、返工和讨论请求独立统计；调用耗时之和不是总历时或关键路径。'
          '积分不是模型排名或因果收益证明。')


def build_contribution_ledger(run):
    contracts = run.get('taskContracts', {})
    snapshots = {}
    for record in sorted(run.get('qualityHistory', []), key=lambda item: item['round']):
        for row in record['modules']:
            snapshots.setdefault(row['moduleId'], []).append((record, row))
    candidates = _accepted_suggestions(run, contracts)
    for mid, history in snapshots.items():
        if mid not in contracts:
            continue
        for criterion in contracts[mid]['criteria']:
            cid = criterion['id']
            before = next((pair for pair in history if _status(pair[1], cid) == 'unmet'), None)
            if before:
                candidate = candidates.setdefault((mid, cid), {'sources': [], 'sequences': []})
                candidate['before'] = before
                candidate['sources'].append(_correction_source(run, before, cid))
                candidate['sequences'].append(before[0].get('eventSeq'))
    _objective_candidates(run, candidates)
    records = [_record(run, mid, cid, candidate, contracts.get(mid), snapshots.get(mid, []))
               for (mid, cid), candidate in candidates.items()]
    _avoid_double_counting(records)
    records.sort(key=lambda item: (item['identifiedEventSeq'] or 0, item['id']))
    phases = {node['id']: node for node in run.get('nodes', [])}
    calls = run.get('callMetrics', [])
    repairs = [call for call in calls if phases.get(call['nodeId'], {}).get('phase') == 'build'
               and phases[call['nodeId']].get('repairRound', 0) > 0]
    discussions = [call for call in calls if phases.get(call['nodeId'], {}).get('phase') == 'review']
    first = [history[0][1] for history in snapshots.values()]
    passed = sum(row.get('gatePassed') is True for row in first)
    return {'schema': 'yuki-contribution-ledger/v1', 'records': records,
            'summary': {'verifiedPoints': sum(row['points'] for row in records),
                        'pendingCount': sum(row['state'] == 'pending' for row in records),
                        'rejectedCount': sum(row['state'] == 'rejected' for row in records),
                        'firstPass': {'passed': passed, 'total': len(first),
                                      'ratePct': round(100 * passed / len(first), 1) if first else None},
                        'repairCalls': len(repairs), 'discussionCalls': len(discussions),
                        'repairElapsedMs': _elapsed(repairs) if 'callMetrics' in run else None,
                        'discussionElapsedMs': _elapsed(discussions) if 'callMetrics' in run else None},
            'notice': NOTICE}


def _elapsed(calls):
    return sum(call['elapsedMs'] for call in calls) if all(call.get('elapsedMs') is not None for call in calls) else None


def _avoid_double_counting(records):
    objective = [row for row in records if row['criterionSource'] == 'objective' and row['state'] == 'verified']
    for row in records:
        if row['criterionSource'] != 'contract' or row['beforeKind'] != 'artifact':
            continue
        covered = [item['id'] for item in objective if item['moduleId'] == row['moduleId']
                   and item['beforeManifest'] == row['beforeManifest'] and item['afterManifest'] == row['afterManifest']]
        if covered:
            row.update(coveredBy=covered, points=0, potentialPoints=0, canVerify=False,
                       state='rejected' if row['state'] == 'rejected' else 'pending',
                       verification={'basis': 'covered_by_objective',
                                     'reason': '同一模块的相同前后产物已有客观验证记录，保守地不重复计分；不表示两类标准语义等价。'})


def _accepted_suggestions(run, contracts):
    candidates = {}
    nodes = run.get('nodes', [])
    sequences = {row['id']: row.get('eventSeq') for row in run.get('discussions', [])}
    for mid, review in run.get('moduleReviews', {}).items():
        evidence = {finding['id']: finding for finding in review.get('evidence', [])}
        known = {criterion['id'] for criterion in contracts.get(mid, {}).get('criteria', [])}
        for decision in review.get('decisions', []):
            if decision.get('decision') != 'accept':
                continue
            finding = evidence.get(decision.get('findingId'))
            if not finding:
                continue
            cid = decision.get('criterionId')
            # An absent or stale binding stays visible without claiming a fixed criterion.
            key = (mid, cid if cid in known else 'unmapped:' + finding['id'])
            candidate = candidates.setdefault(key, {'sources': [], 'sequences': []})
            node = next((node for node in nodes
                         if any(item.get('id') == finding['id'] for item in node.get('result', {}).get('findings', []))), {})
            source = {'nodeId': node.get('id'), 'model': finding.get('model') or node.get('agent'),
                      'kind': 'accepted_suggestion', 'claim': finding.get('claim', ''),
                      'findingId': finding['id']}
            source.update(_reflection_origin(run, mid, review, node))
            candidate['sources'].append(source)
            event_id = node['id'] + '-answer' if source.get('origin') == 'reflection' else finding['id']
            candidate['sequences'].append(sequences.get(event_id))
    return candidates


def _reflection_origin(run, mid, review, node):
    policy = run.get('workPolicy', {})
    reflection = review.get('reflection', {})
    if (not policy.get('deepReview') or review.get('roundLimit', 0) < 2
            or node.get('id') not in reflection.get('answerNodeIds', [])
            or node.get('moduleId') != mid or node.get('discussionKind') != 'reflection'
            or node.get('phase') != 'review' or node.get('status') != 'done'
            or node.get('round') != reflection.get('round')):
        return {}
    question = next((item for item in run['nodes'] if item['id'] == reflection.get('questionNodeId')), {})
    if (question.get('fixedPrompt') is not True or question.get('status') != 'done'
            or question.get('discussionKind') != 'reflection_prompt'):
        return {}
    return {'origin': 'reflection', 'weight': policy['deepReviewMultiplier'],
            'questionNodeId': question['id']}


def _criteria(row, cid):
    return [item for key in ('criteria', 'coordinatorCriteria') for item in row.get(key, [])
            if item.get('criterionId') == cid]


def _status(row, cid):
    statuses = [item.get('status') for item in _criteria(row, cid)]
    if 'unmet' in statuses:
        return 'unmet'
    return 'met' if statuses and all(status == 'met' for status in statuses) else 'unverified'


def _manifest(row):
    return sorted(copy.deepcopy(row.get('artifactManifest', [])), key=lambda item: item['path'])


def _objective_candidates(run, candidates):
    evaluations = sorted(run.get('objectiveEvaluations', []), key=lambda item: item['round'])
    for mid, criteria in run.get('objectiveCriteria', {}).items():
        for criterion in criteria:
            failed = {}
            for evaluation in evaluations:
                if evaluation['moduleId'] != mid or not evaluation.get('evaluatorFingerprint'):
                    continue
                evaluator = evaluation['evaluatorFingerprint']
                status = next((item['status'] for item in evaluation['criteria'] if item['id'] == criterion['id']), None)
                if status == 'failed':
                    failed.setdefault(evaluator, evaluation)
                before = failed.get(evaluator)
                if (status != 'passed' or not before or before['round'] >= evaluation['round']
                        or not _manifest(before) or _manifest(before) == _manifest(evaluation)):
                    continue
                candidate = candidates.setdefault((mid, criterion['id']), {'sources': [], 'sequences': []})
                candidate['objectiveBefore'] = before
                candidate['sources'].append({'nodeId': before.get('nodeId'),
                    'model': None, 'evaluatorFingerprint': evaluator,
                    'kind': 'objective_failed_criterion', 'claim': criterion['text']})
                candidate['sequences'].append(before.get('eventSeq'))
                break


def _correction_source(run, before, cid):
    quality, row = before
    criterion = next(item for item in _criteria(row, cid) if item.get('status') == 'unmet')
    reviewed = any(item is criterion for item in row.get('criteria', [])) and row.get('reviewNodeId')
    node_id = reviewed or ('integration' if quality['round'] == 0 else 'integration-r' + str(quality['round']))
    node = next((item for item in run.get('nodes', []) if item['id'] == node_id), {})
    model = node.get('agent') or (row.get('reviewer') if reviewed else run.get('coordinatorModel', 'opus'))
    return {'nodeId': node_id, 'model': model,
            'kind': 'identified_unmet_criterion',
            'claim': criterion.get('reason') or criterion.get('criterion') or cid}


def _record(run, mid, cid, candidate, contract, history):
    criterion = next((item for item in (contract or {}).get('criteria', []) if item['id'] == cid), None)
    criterion_source = 'contract'
    if criterion is None:
        criterion = next((item for item in run.get('objectiveCriteria', {}).get(mid, []) if item['id'] == cid), None)
        criterion_source = 'objective'
    latest = history[-1][1] if history else {}
    before = candidate.get('before')
    objective_before = candidate.get('objectiveBefore')
    before_manifest = (_manifest(objective_before) if objective_before else _manifest(before[1]) if before
                       else _manifest(history[0][1]) if len(history) > 1 else [])
    before_kind = 'artifact'
    if not before_manifest and any(source['kind'] == 'accepted_suggestion' for source in candidate['sources']):
        draft = next((node.get('result', {}) for node in run.get('nodes', []) if node['id'] == 'draft'), {})
        module = next((item for item in draft.get('modules', []) if item['id'] == mid), None)
        if module is not None:
            content = json.dumps(module, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            before_manifest = [{'path': '@plan/' + mid + '.json', 'size': len(content),
                                'sha256': hashlib.sha256(content).hexdigest()}]
            before_kind = 'plan'
    after_manifest = _manifest(latest)
    fixed = criterion is not None
    sources = []
    for source in candidate['sources']:
        if source not in sources:
            sources.append(copy.deepcopy(source))
    source_sequences = [seq for seq in candidate['sequences'] if seq is not None]
    weight = max((source.get('weight', 1) for source in sources), default=1)
    record = {'id': run.get('id', '') + ':' + mid + ':' + cid,
              'moduleId': mid, 'criterionId': cid if fixed else None,
              'criterion': criterion['text'] if fixed else '', 'title': latest.get('title') or mid,
              'criterionSource': criterion_source if fixed else None,
              'kind': 'correction' if before or objective_before else 'suggestion', 'sources': sources,
              'beforeManifest': before_manifest, 'afterManifest': after_manifest,
              'beforeKind': before_kind, 'afterKind': 'artifact',
              'weight': weight, 'potentialPoints': weight if fixed else 0,
              'identifiedEventSeq': min(source_sequences) if source_sequences else None}
    proof = _objective_proof(run, record) if before_kind == 'artifact' else None
    current_status = ('met' if proof else 'unverified') if criterion_source == 'objective' else _status(latest, cid)
    record['canVerify'] = bool(fixed and before_manifest and after_manifest and before_manifest != after_manifest
                               and current_status == 'met' and latest.get('gatePassed') is True)
    fingerprint = {'recordId': record['id'], 'contractId': (contract or {}).get('id'),
                   'contractFingerprint': (contract or {}).get('fingerprint'),
                   'criterion': record['criterion'], 'beforeManifest': before_manifest, 'afterManifest': after_manifest,
                   'criterionSource': record['criterionSource'], 'beforeKind': before_kind, 'sources': sources,
                   'currentStatus': current_status, 'gatePassed': latest.get('gatePassed') is True,
                   'objectiveProof': proof}
    record['evidenceFingerprint'] = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True,
                                                             separators=(',', ':')).encode()).hexdigest()
    state, verification = _verify(run, record, proof)
    record.update(state=state, verification=verification, points=weight if state == 'verified' else 0)
    return record


def _verify(run, record, proof):
    pending = lambda reason: ('pending', {'basis': 'unverified', 'reason': reason})
    validations = [item for item in run.get('contributionValidations', []) if item['recordId'] == record['id']]
    manual = max(enumerate(validations), key=lambda pair: (pair[1].get('eventSeq', 0), pair[0]))[1] if validations else None
    if (manual and manual['evidenceFingerprint'] == record['evidenceFingerprint']
            and (manual['verdict'] == 'rejected' or record['canVerify'])):
        state = 'verified' if manual['verdict'] == 'verified' else 'rejected'
        return state, {'basis': 'human_review', 'reason': '用户对当前前后产物与固定验收项进行了显式核验。',
                       'actor': manual['actor'], 'beforeObservation': manual['beforeObservation'],
                       'afterObservation': manual['afterObservation'], 'method': manual['method'],
                       'eventSeq': manual['eventSeq'], 'created': manual['created']}
    if not record['criterionId']:
        return pending('建议已采纳，但未绑定本模块固定验收项，不能计分。')
    if not record['canVerify']:
        return pending('缺少可对应的前后方案或产物，或当前固定标准与模块关卡未通过；既有积分不继续生效。')
    if proof:
        return 'verified', {'basis': 'objective_evaluation', 'reason': '同一客观评测器对对应前后产物的同一固定标准由失败变为通过。', **proof}
    return pending('缺少对应前后产物的固定客观验证，或当前证据的人工核验；模型判断和命令回执仅作线索。')


def _objective_proof(run, record):
    before, after = record['beforeManifest'], record['afterManifest']
    if not before or before == after:
        return None
    evaluations = {}
    for evaluation in run.get('objectiveEvaluations', []):
        if evaluation['moduleId'] != record['moduleId'] or not evaluation.get('evaluatorFingerprint'):
            continue
        manifest = _manifest(evaluation)
        side = 'before' if manifest == before else 'after' if manifest == after else None
        if side:
            key = (evaluation['evaluatorFingerprint'], side)
            prior = evaluations.get(key)
            if prior is None or evaluation['round'] >= prior['round']:
                evaluations[key] = evaluation
    for evaluator, side in evaluations:
        first, last = evaluations.get((evaluator, 'before')), evaluations.get((evaluator, 'after'))
        if side != 'after' or not first or first['round'] >= last['round']:
            continue
        statuses = [next((criterion['status'] for criterion in item['criteria'] if criterion['id'] == record['criterionId']), None)
                    for item in (first, last)]
        if statuses == ['failed', 'passed']:
            return {'evaluatorFingerprint': evaluator, 'beforeRound': first['round'], 'afterRound': last['round']}
    return None
