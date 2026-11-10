"""Persist contribution evidence at the event that made it available."""
from app import Conflict, stamp
from contribution_ledger import build_contribution_ledger


def record_snapshot(studio, session, run, event_seq):
    if run.get('contributionVersion') != 1:
        return
    history = run.setdefault('contributionHistory', [])
    if any(item['eventSeq'] == event_seq for item in history):
        return
    history.append({**build_contribution_ledger(run), 'eventSeq': event_seq})


def verify_contribution(studio, pid, sid, request):
    _validate_request(studio, request)
    with studio.lock:
        session = studio.load(pid, sid)
        if request['version'] != session['version']:
            raise Conflict('会话已更新，请刷新贡献记录后核验')
        if session['status'] in ('queued', 'running'):
            raise ValueError('请等待本轮排队或执行结束后核验贡献')
        run = next((r for r in session['runs'] if r['id'] == request['runId']), None)
        if run is None or run.get('contributionVersion') != 1:
            raise ValueError('这轮执行尚无贡献记账记录')
        ledger = build_contribution_ledger(run)
        record = next((r for r in ledger['records'] if r['id'] == request['recordId']), None)
        if record is None or record['evidenceFingerprint'] != request['evidenceFingerprint']:
            raise Conflict('贡献证据已变化，请查看最新产物后重新核验')
        if request['verdict'] == 'verified' and not record['canVerify']:
            raise ValueError('尚未绑定固定标准、缺少前后证据或最新验收未通过，不能计分')
        validation = {key: request[key].strip() for key in
                      ('recordId', 'evidenceFingerprint', 'verdict', 'beforeObservation', 'afterObservation', 'method')}
        validation.update(actor='user', created=stamp())
        studio.event(session, 'contribution_verified' if request['verdict'] == 'verified' else 'contribution_rejected',
                     '用户核验贡献：' + record['title'], runId=run['id'], recordId=record['id'])
        validation['eventSeq'] = session['events'][-1]['seq']
        run.setdefault('contributionValidations', []).append(validation)
        record_snapshot(studio, session, run, validation['eventSeq'])
        studio.save(session)
        return session


def _validate_request(studio, request):
    for key in ('runId', 'recordId', 'evidenceFingerprint', 'beforeObservation', 'afterObservation', 'method'):
        studio.check_text(request.get(key), 4000)
        if not request[key].strip():
            raise ValueError('请填写完整的前后观察和实际验证方法')
    if request.get('verdict') not in ('verified', 'rejected') or type(request.get('version')) is not int:
        raise ValueError('贡献核验状态或会话版本无效')


def record_objective_evaluations(studio, session, run, case_id, grader_fingerprint):
    if run.get('contributionVersion') != 1 or run.get('objectiveEvaluations'):
        return
    from collaboration_evaluation import case_definition, evaluate_delivery
    from quality_assessment import artifact_manifest
    case = case_definition(case_id)
    evaluations, criteria = [], {}
    for node in run['nodes']:
        if node['phase'] != 'build' or node['status'] != 'done':
            continue
        files = node['result']['files']
        if not any(f['path'] == 'result.json' for f in files):
            continue
        mid = node['moduleId']
        result = evaluate_delivery(files, case_id)
        criteria[mid] = [{'id': mid + '-objective-' + c['id'], 'text': c['title']} for c in case['rubric']]
        evaluations.append({'round': node.get('repairRound', 0), 'moduleId': mid, 'nodeId': node['id'],
                            'evaluatorFingerprint': grader_fingerprint,
                            'artifactManifest': artifact_manifest(studio, session, node['result']),
                            'criteria': [{'id': mid + '-objective-' + c['id'], 'status': c['status']}
                                         for c in result['criteria']]})
    with studio.lock:
        run['objectiveCriteria'] = criteria
        run['objectiveEvaluations'] = evaluations
        run['objectiveCaseId'] = case_id
        studio.event(session, 'contribution_evaluated', '固定案例已对各次实际产物完成独立复核', runId=run['id'])
        seq = session['events'][-1]['seq']
        for evaluation in evaluations:
            evaluation['eventSeq'] = seq
        record_snapshot(studio, session, run, seq)
        studio.save(session)


def project_contribution_metrics(studio, pid):
    totals = {'verifiedPoints': 0, 'pendingCount': 0, 'rejectedCount': 0, 'runCount': 0,
              'humanVerifiedPoints': 0, 'objectiveVerifiedPoints': 0,
              'notice': '仅汇总真实执行的最新贡献账本；每个固定验收项最多一分。不同任务的原始积分不可直接排名，不自动改变模型分配权重。'}
    for session in studio.list_sessions(pid):
        for run in session['runs']:
            if run.get('mode') != 'live' or not run.get('contributionHistory'):
                continue
            ledger = run['contributionHistory'][-1]
            totals['runCount'] += 1
            for key in ('verifiedPoints', 'pendingCount', 'rejectedCount'):
                totals[key] += ledger['summary'][key]
            for record in ledger['records']:
                if record['points']:
                    key = 'humanVerifiedPoints' if record['verification']['basis'] == 'human_review' else 'objectiveVerifiedPoints'
                    totals[key] += record['points']
    return totals
