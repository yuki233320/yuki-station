"""Artifact-bound quality gates and two bounded, dependency-aware repair rounds."""
import concurrent.futures
import copy

from live import Cancelled
from model_catalog import MODELS
from quality_assessment import SAME_MODEL_REVIEW_NOTICE, artifact_manifest, artifact_review_required, build_assessment
from studio_business import (BUILD_PROMPT, FINAL_PROMPT, _commit_delivery, _distinct_paths,
                             _node, _save_candidates, acceptance_evidence_paths,
                             native_verification_checks)


REVIEW_PROMPT = '''你是独立产物审查员。现在审查已经写出的文件，而不是讨论实现计划。按 qualityContract 中每个标准逐项核对 reviewFiles 的实际内容，结合业务全貌与明确提供的实际检查。不要采纳作者自评；不能声称运行未执行的测试。文件行号从1开始，必须引用实际存在的路径和行范围。只看源码能确认的行为可标 met；必须运行才能确认而缺少运行证据的标准标 unverified。不要因为有报告文件就声称浏览器或部署通过。
evidence.path 只能取 reviewFiles 中的路径；allowedEvidenceFiles 给出允许路径及 lineCount，行号范围须满足 1 <= startLine <= endLine <= lineCount。项目上下文里的输入示例、需求文件可在 reason 中说明，但不在 reviewFiles 中的文件不得放入 evidence；不得仅凭输入要求判定产物满足标准。
输出完整JSON：{"summary":"范围明确的结论","criteria":[{"criterionId":"原标准编号","status":"met/unmet/unverified","reason":"具体理由","evidence":[{"path":"实际路径","startLine":1,"endLine":2}]}],"findings":[{"priority":"P0/P1/P2/P3","claim":"具体可复现的问题","proposal":"最小修复建议","evidence":[{"path":"实际路径","startLine":1,"endLine":2}]}]}。
覆盖全部标准，不给总分。P0/P1必须是阻止当前交付的具体问题；不要为猜测中几乎不会发生的情况要求扩大实现。P2/P3是非阻断建议。'''


def finish_with_quality(studio, session, run, plan, complete, available, cancel):
    from repair_routing import version as routing_version
    routed = routing_version(run) == 1
    for round_no in range(run['repairLimit'] + 1):
        if cancel.is_set():
            raise Cancelled()
        if round_no:
            _repair_modules(studio, session, run, plan, complete, round_no, cancel)
        files = {f['path']: f for result in complete.values() for f in result['files']}
        _distinct_paths([f['path'] for result in complete.values() for f in result['files']])
        data_files = _save_candidates(studio, session, run, files)
        from host_quality import execute_round
        execute_round(studio, session, run, plan, complete, data_files, round_no, cancel)
        assessments = _assess_modules(studio, session, run, plan, complete, available, round_no, cancel)
        final = _coordinator_acceptance(studio, session, run, plan, complete, assessments, round_no, cancel)
        record = _record_quality(studio, session, run, assessments, final, round_no)
        if routed and not record['passed'] and any(row['repairRoute']['action'] == 'review' for row in record['modules']):
            record = _review_same_artifacts(studio, session, run, plan, complete, available, record, cancel)
        if record['passed']:
            if cancel.is_set():
                raise Cancelled()
            _commit_delivery(studio, session, run, data_files)
            return
        if routed:
            from repair_routing import require_repairable
            require_repairable(run, record)
        else:
            _require_repairable(record)
        if round_no == run['repairLimit']:
            raise ValueError('质量验收仍未通过，已用完两轮定向修复；候选文件和每轮证据已保留。请补充要求后开启新一轮，不继续自动消耗模型调用。')
        if not any(not item['gatePassed'] for item in record['modules']):
            raise ValueError('模块检查已通过，但整体业务验收未通过，缺少可归属的修复目标；请补充项目要求。')


def _review_same_artifacts(studio, session, run, plan, complete, available, source, cancel):
    """One independent re-reading of the exact candidate; no Worker or test run."""
    from repair_routing import review_attempt, verify_record
    from evidence_review import _candidate_files
    from host_quality import history_fingerprint
    verify_record(run, source)
    # Configuration/user decisions take precedence over spending a review call.
    if any(row['repairRoute']['action'] == 'stop' for row in source['modules']):
        return source
    round_no = source['round']
    attempt = review_attempt(run, round_no)
    root = studio.project_dir(session['projectId']) / session['id']
    candidate_files = _candidate_files(root, run, complete)
    from host_quality import verify_round_evidence
    verify_round_evidence(studio, session, run, candidate_files, round_no)
    _verify_review_rows(studio, session, run, source['modules'])
    if history_fingerprint(run, round_no) != source.get('hostAcceptanceFingerprint'):
        raise ValueError('同产物复审不能替换原宿主验收回执')
    selected = [row for row in source['modules'] if row['repairRoute']['action'] == 'review']
    kind = 'review_disagreement' if any(r['repairRoute']['kind'] == 'review_disagreement' for r in selected) else 'evidence_review'
    routing_review = {'id': attempt, 'kind': kind, 'moduleIds': [r['moduleId'] for r in selected], 'source': source}
    assessments = _assess_modules(studio, session, run, plan, complete, available, round_no, cancel, routing_review)
    if cancel.is_set():
        raise Cancelled()
    original_node = 'integration' if round_no == 0 else 'integration-r' + str(round_no)
    metadata = {'id': attempt, 'repairRoutingVersion': 1, 'kind': kind,
                'nodeId': 'integration-' + attempt, 'sourceFinalNodeId': original_node,
                'sourceNodes': {row['moduleId']: row['reviewNodeId'] or _build_node(run, row['moduleId'], complete[row['moduleId']], round_no)['id'] for row in assessments},
                'sourceQualityEventSeq': source['eventSeq'], 'sourceRoutingFingerprint': source['repairRoutingFingerprint']}
    final = _coordinator_acceptance(studio, session, run, plan, complete, assessments, round_no, cancel, evidence_review=metadata)
    return _record_quality(studio, session, run, assessments, final, round_no, review_id=attempt)


def _require_repairable(record):
    blocked = record.get('repairBlocked', [])
    if not blocked:
        return
    labels = {'replan': '需重新规划', 'needs_input': '需补充信息或决定'}
    reasons = ['模块 ' + item['moduleId'] + '（' + labels[item['action']] + '）：'
               + '；'.join(item['unmet']) for item in blocked]
    raise ValueError('质量验收未通过，停止自动返工；' + '；'.join(reasons)
                     + '。本轮证据和固定契约已保留，请补充要求后开启新一轮。')


def _build_node(run, module_id, output, round_no):
    # Resolve by the actual output rather than the last node in the list: during
    # resume, later repair nodes already exist before earlier rounds are replayed.
    return next(n for n in reversed(run['nodes']) if n['phase'] == 'build'
                and n.get('repairRound', 0) <= round_no and n.get('moduleId') == module_id
                and n.get('result') == output and n['status'] == 'done')


def _assess_modules(studio, session, run, plan, complete, available, round_no, cancel, routing_review=None):
    from host_quality import module_checks, evidence_files, summaries, bind_review
    from review_evidence import review_version, validate_contract, binding_for, bind_input, capture_evidence, verify_evidence
    strict = review_version(run) == 1
    def assess(module):
        mid = module['id']
        if routing_review and mid not in routing_review['moduleIds']:
            return copy.deepcopy(next(row for row in routing_review['source']['modules'] if row['moduleId'] == mid))
        review_attempt = routing_review['id'] if routing_review else None
        host_checks = module_checks(run, round_no, mid)
        unchanged = not routing_review and round_no and not host_checks and not any(n['id'] == 'repair-' + str(round_no) + '-' + mid for n in run['nodes'])
        previous = next((r for r in reversed(run.get('qualityHistory', [])) if r['round'] == round_no - 1), None) if unchanged else None
        previous_row = next((row for row in previous['modules'] if row['moduleId'] == mid), None) if previous else None
        if unchanged and not strict:
            return copy.deepcopy(previous_row)
        output = complete[mid]
        contract = run['taskContracts'][mid]
        if strict and contract.get('reviewRequirementVersion') != 1:
            raise ValueError('本轮缺少冻结复核契约，不能补写历史义务')
        schedule = validate_contract(module, contract) if strict else None
        manifest = artifact_manifest(studio, session, output)
        checks = studio.static_checks({f['path']: f for f in output['files']})
        checks += host_checks
        from worker_integration import checks as worker_checks
        checks += worker_checks(studio, session, run, module, output)
        if set(module['requiredCapabilities']) & {'browser', 'plugins'}:
            from studio_mcp import verification_checks
            checks += verification_checks(module, output)
        if module['assignment']['model'] == 'native_codex':
            checks += native_verification_checks(module, output)
        review = node = reviewer = proof = None
        if artifact_review_required(module, contract):
            reviewer = schedule['reviewer'] if schedule else module['qualityReviewer']
            mode = schedule['mode'] if schedule else module.get('reviewMode')
            review_label = '同模型复核' if mode == 'same_model' else '独立产物审查'
            if reviewer not in available:
                raise ValueError('请恢复已分配的' + review_label + '模型：' + MODELS[reviewer]['name'])
            allowed = set(acceptance_evidence_paths(plan, complete)[mid])
            for verifier in plan['modules']:
                result = complete[verifier['id']]
                if (verifier['assignment']['model'] == 'native_codex' and mid in verifier['dependsOn']
                        and result['nativeEvidence'].get('inputUnchanged') is True):
                    allowed.update(f['path'] for f in result['files'])
            sources = {source_mid: result for source_mid, result in complete.items()
                       if any(f['path'] in allowed for f in result['files'])}
            evidence_deps = [_build_node(run, source_mid, result, round_no)['id']
                             for source_mid, result in sources.items()]
            build = _build_node(run, mid, output, round_no)
            review_files = [copy.deepcopy(f) for result in complete.values() for f in result['files']
                            if f['path'] in allowed and 'content' in f]
            review_files += evidence_files(run, round_no, mid)
            from worker_review_evidence import evidence_files as worker_review_files
            review_files += worker_review_files(studio,session,run,module,output)
            # Unaffected modules may reuse an earlier review only when its
            # complete input binding and original records are still identical.
            if strict and unchanged and previous_row and previous_row.get('reviewEvidence'):
                old_proof = previous_row['reviewEvidence']
                old_round = old_proof.get('binding', {}).get('qualityRound')
                expected = binding_for(run, module, contract, build, reviewer, review_files, manifest, checks, old_round, review_attempt=old_proof.get('binding', {}).get('reviewAttemptId'))
                if old_proof.get('binding') == expected:
                    review = previous_row.get('sameModelReview') if mode == 'same_model' else previous_row.get('independentReview')
                    verify_evidence(studio, session, run, module, contract, review, old_proof, manifest)
                    proof = copy.deepcopy(old_proof)
                    node = next(n for n in run['nodes'] if n['id'] == proof['reviewNodeId'])
            if proof is None:
                node = _node(studio, run, 'verify-' + (review_attempt or str(round_no)) + '-' + mid,
                             module['title'] + ' · ' + review_label, reviewer, 'verification',
                             evidence_deps, module, 'reviewer-' + reviewer)
                node['qualityRound'] = round_no
                if routing_review:
                    node.update(qualityReviewId=review_attempt, repairReview=True, recheckReason=routing_review['kind'])
                binding = binding_for(run, module, contract, build, reviewer, review_files, manifest, checks, round_no, review_attempt=review_attempt) if strict else None
                bind_input(studio, session, node, binding)
                prompt = REVIEW_PROMPT
                if routing_review:
                    prompt += '\n这是一次有界的同产物复审：未改文件、标准或已执行检查。repairReviewFeedback 是上次判断与分歧，逐项重新核对当前原件引用；不得为求通过而改票，不得虚构缺失步骤。缺证仍标 unverified，发现实际缺陷保留 unmet 和明确引用。'
                if mode == 'same_model':
                    prompt = prompt.replace('你是独立产物审查员。', SAME_MODEL_REVIEW_NOTICE)
                if strict:
                    prompt += '\nqualityContract.reviewRequirements是执行前明确的复核义务，本次正在执行对应产物复核。宿主将核对实际审核者、作者、上下文、响应和产物指纹；不要为本次复核再发明第三位复核者，也不能把同模型岗位复核称为跨模型独立确认。'
                if contract.get('workerReviewEvidenceVersion') in (1,2):
                    from worker_review_evidence import notice as worker_notice
                    prompt += '\n' + worker_notice(contract['workerReviewEvidenceVersion'])
                if host_checks:
                    from host_quality import NOTICE
                    prompt += '\n' + NOTICE
                bind_review(studio, session, run, node, round_no, review_files)
                review = studio.ask(session, run, node, 'quality-' + mid + '-' + reviewer, prompt,
                    {'businessStage': 'quality', 'module': copy.deepcopy(module), 'plan': copy.deepcopy(plan),
                     'qualityRound': round_no, 'qualityContract': copy.deepcopy(contract),
                     **({'qualityReviewId': review_attempt, 'repairReviewFeedback': copy.deepcopy(next(row for row in routing_review['source']['modules'] if row['moduleId'] == mid))} if routing_review else {}),
                     'reviewFiles': review_files,
                     'allowedEvidenceFiles': [{'path': f['path'], 'lineCount': len(f['content'].splitlines())}
                                              for f in review_files], 'checks': checks,
                     **({'hostAcceptance': summaries(run, round_no, mid)} if run.get('hostAcceptanceChecks') else {}),
                     'verificationEvidence': [{'moduleId': source_mid, 'nativeEvidence': copy.deepcopy(result['nativeEvidence'])}
                         for source_mid, result in sources.items() if 'nativeEvidence' in result]}, cancel)
                proof = capture_evidence(studio, session, run, module, contract, node, binding, review, manifest)
        row = build_assessment(module, contract, output, manifest, checks, review, review_evidence=proof)
        if output.get('workerEvidence'):
            row['workerEvidence'] = copy.deepcopy(output['workerEvidence'])
        row.update(reviewer=reviewer, reviewNodeId=node['id'] if node else None,
                   criteria=copy.deepcopy(review['criteria']) if review else [],
                   findings=copy.deepcopy(review['findings']) if review else [])
        return row
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(assess, plan['modules']))


def _coordinator_acceptance(studio, session, run, plan, complete, assessments, round_no, cancel, evidence_review=None):
    from host_quality import summaries, evidence_files, evidence_paths, bind_review, NOTICE
    from review_evidence import review_version
    node_id = 'integration' if round_no == 0 else 'integration-r' + str(round_no)
    if evidence_review:
        node_id = evidence_review['nodeId']
        deps = list(evidence_review['sourceNodes'].values()) + [evidence_review['sourceFinalNodeId']]
    else:
        deps = [row['reviewNodeId'] or _build_node(run, row['moduleId'], complete[row['moduleId']], round_no)['id'] for row in assessments]
    coordinator = run.get('coordinatorModel', 'opus')
    node = _node(studio, run, node_id, MODELS[coordinator]['name'] + ' 逐项验收' + (' · 修复后 ' + str(round_no) if round_no else ''),
                 coordinator, 'integration', deps)
    if review_version(run) == 1:
        from review_evidence import fingerprint
        _verify_review_rows(studio, session, run, assessments)
        binding = fingerprint({'outputs': complete, 'reviewEvidence': _review_rows_fingerprint(assessments)})
        with studio.lock:
            if node['status'] == 'done' and node.get('artifactAcceptanceBinding') != binding:
                raise ValueError('缓存的主模型验收与当前产物或复核证据不一致')
            node['artifactAcceptanceBinding'] = binding
            studio.save(session)
    checks = [check for row in assessments for check in row['checks']]
    with studio.lock:
        run['checks'] = checks
        studio.save(session)
    comparison_notice = '\n' + SAME_MODEL_REVIEW_NOTICE if run.get('comparisonArm') == 'single' else ''
    host_summaries = summaries(run, round_no)
    host_reports = evidence_files(run, round_no)
    host_paths = evidence_paths(run, round_no, plan, complete)
    allowed_paths = acceptance_evidence_paths(plan, complete)
    for mid, paths in host_paths.items():
        allowed_paths[mid] = sorted(set(allowed_paths[mid]) | set(paths))
    from worker_review_evidence import evidence_files as worker_review_files, notice as worker_notice
    worker_reports = [];worker_paths = {}
    for module in plan['modules']:
        reports = worker_review_files(studio,session,run,module,complete[module['id']])
        worker_reports.extend(reports)
        if reports:
            worker_paths[module['id']] = [f['path'] for f in reports]
            allowed_paths[module['id']] = sorted(set(allowed_paths[module['id']]) | set(worker_paths[module['id']]))
    host_binding_view = {'outputs': complete, 'assessments': assessments, 'hostReports': host_reports}
    if worker_reports:host_binding_view['workerReports'] = worker_reports
    bind_review(studio, session, run, node, round_no, host_binding_view)
    if worker_reports:comparison_notice += '\n' + worker_notice(run['workerReviewEvidenceVersion'])

    if host_summaries:
        comparison_notice += '\n' + NOTICE
    review_notice = ('\n这是独立补证复审，只重审同一固定契约和相同产物。evidenceReview 引用原始工具事件；补齐的参数与返回值在 modules 的 nativeEvidence.tools 中，搜索正文只保留这一份，其他位置按 toolId/resultRef 引用。missing、截断和无法验证项必须如实处理；历史失败不得删除，不再执行或修复模块。' if evidence_review else '')
    if evidence_review and evidence_review.get('repairRoutingVersion') == 1:
        node.update(qualityReviewId=evidence_review['id'], repairReview=True, recheckReason=evidence_review['kind'])
        review_notice = '\n这是有界的同产物复审，文件和标准未变，未重跑任何Worker或脚本；核对新的独立判断与原有真实证据，不得用投票豁免关卡。原失败保留，缺失事实仍不得声称发生。'
    final = studio.ask(session, run, node, 'coordinator', FINAL_PROMPT.replace('你是 Opus 总负责人。', '你是本轮选定的项目总负责人。') + '''
本轮增加产物复核 qualityAssessments。文件审查不能当运行证据。未满足/未验证的标准和P0/P1问题必须明确处理。你不能改写固定标准或豁免宿主质量关卡；通过与否由宿主结合全部证据计算。本阶段只汇总已有产物，files 必须为空，额外代码必须预先作为模块分派。
每个未接纳模块在 moduleAcceptance 中给出 repairAction：在当前范围和固定契约内可修复用 repair；需要改变冻结输入、范围或验收标准用 replan；缺少用户信息或决定用 needs_input。具体原因写入该模块 unmet。不要把契约矛盾当成执行者可修复的问题，不能为通过验收删除或降低标准。宿主遇到 replan 或 needs_input 会保留证据并停止自动返工。''' + comparison_notice + review_notice,
        {'businessStage': 'integration', 'qualityVersion': 1, 'qualityRound': round_no,
         'roleId': 'coordinator', 'plan': copy.deepcopy(plan), 'modules': complete, 'checks': checks,
         'qualityAssessments': copy.deepcopy(assessments), 'taskContracts': copy.deepcopy(run['taskContracts']),
         'acceptanceEvidencePathsByModule': allowed_paths,
         **({'workerExecutionReports':worker_reports,'workerExecutionEvidencePathsByModule':worker_paths} if worker_reports else {}),
         **({'hostAcceptance': host_summaries, 'hostAcceptanceReports': host_reports,
             'hostAcceptanceEvidencePathsByModule': host_paths} if run.get('hostAcceptanceChecks') else {}),
         **({'evidenceReview': copy.deepcopy(evidence_review)} if evidence_review else {})}, cancel)
    with studio.lock:
        run['final'] = copy.deepcopy(final)
        run['finalNodeId'] = node_id
        run['checks'] = checks
        studio.save(session)
    return final


def _record_quality(studio, session, run, assessments, final, round_no, review_id=None):
    from host_quality import history_fingerprint
    from review_evidence import review_version
    host_fingerprint = history_fingerprint(run, round_no)
    review_fingerprint = None
    if review_version(run) == 1:
        _verify_review_rows(studio, session, run, assessments)
        review_fingerprint = _review_rows_fingerprint(assessments)
    with studio.lock:
        existing = next((r for r in run.get('qualityHistory', []) if r['round'] == round_no and r.get('reviewId') == review_id), None)
        if existing:
            from repair_routing import verify_record
            verify_record(run, existing)
            if review_fingerprint is not None and existing.get('reviewEvidenceFingerprint') != review_fingerprint:
                raise ValueError('已有质量结论与当前冻结复核证据不一致')
            if host_fingerprint is not None and existing.get('hostAcceptanceFingerprint') != host_fingerprint:
                raise ValueError('已有质量结论与当前宿主验收不一致，不能复用旧结论')
            run['final']['coordinatorAccepted'] = final['accepted']
            run['final']['accepted'] = existing['passed']
            studio.save(session)
            return existing
        for row in assessments:
            acceptance = next(a for a in final['moduleAcceptance'] if a['moduleId'] == row['moduleId'])
            row['coordinatorAccepted'] = acceptance['accepted']
            row['coordinatorCriteria'] = copy.deepcopy(acceptance['criteria'])
            row['repairAction'] = acceptance.get('repairAction', 'repair')
            row['unmet'] = copy.deepcopy(acceptance['unmet'])
            if not row['reviewer']:
                row['basis'] = 'coordinator_review'
                row['criteria'] = [{'criterionId': c['criterionId'], 'status': c['status'],
                                    'reason': c['criterion'], 'evidence': [{'path': p} for p in c['evidence']]}
                                   for c in acceptance['criteria']]
                met = sum(c['status'] == 'met' for c in row['criteria'])
                row['metrics'].update(criteriaMet=met,
                    criteriaUnverified=sum(c['status'] == 'unverified' for c in row['criteria']),
                    criterionCoveragePct=round(100 * met / row['metrics']['criteriaTotal'], 1))
                row['gatePassed'] = all(c['pass'] for c in row['checks'])
            row['gatePassed'] = row['gatePassed'] and acceptance['accepted']
            failed_host = [check for check in row['checks'] if check.get('type') == 'host_acceptance' and not check['pass']]
            if failed_host:
                row['gatePassed'] = False
                row['unmet'] += [check['name'] + '：' + str(check.get('detail') or check['status']) for check in failed_host]
                if any(check['repairAction'] == 'needs_input' for check in failed_host):
                    row['repairAction'] = 'needs_input'
        passed = final['accepted'] and all(row['gatePassed'] for row in assessments)
        summary = ('质量关卡通过' if passed else '质量关卡未通过') + (' · 首次交付' if round_no == 0 else ' · 修复第%d轮' % round_no)
        if review_id:
            summary = ('质量关卡通过' if passed else '质量关卡未通过') + ' · 补证复审'
        record = {'schema': 'yuki-quality/v1', 'round': round_no, 'modules': assessments,
                  'passed': passed, 'summary': summary,
                  'repairBlocked': [{'moduleId': row['moduleId'], 'action': row['repairAction'],
                                     'unmet': copy.deepcopy(row['unmet'])}
                                    for row in assessments if not row['gatePassed']
                                    and row['repairAction'] in ('replan', 'needs_input')],
                  'notice': '百分比表示已满足的固定标准比例，不是正确率或质量概率；模型审查仍属源码判断。运行范围仅以实际工具回执为准。'
                            + (SAME_MODEL_REVIEW_NOTICE if run.get('comparisonArm') == 'single' else '')}
        if review_id:
            record['reviewId'] = review_id
            if review_id.startswith('repair-review-r'):
                record['reviewKind'] = next((n.get('recheckReason') for n in run['nodes'] if n['id'] == run['finalNodeId']), None)
        if host_fingerprint is not None:
            record['hostAcceptanceFingerprint'] = host_fingerprint
        if review_fingerprint is not None:
            record['reviewRequirementVersion'] = 1
            record['reviewEvidenceFingerprint'] = review_fingerprint
        from repair_routing import attach
        attach(run, record)
        studio.event(session, 'quality_passed' if passed else 'quality_failed', summary, run['finalNodeId'])
        record['eventSeq'] = session['events'][-1]['seq']
        run.setdefault('qualityHistory', []).append(record)
        from contribution_tracking import record_snapshot
        record_snapshot(studio, session, run, record['eventSeq'])
        # Preserve the coordinator's response separately; the commit consumes the
        # host decision and cannot publish a module rejected by its independent gate.
        run['final']['coordinatorAccepted'] = final['accepted']
        run['final']['accepted'] = passed
        studio.save(session)
        return record


def _review_rows_fingerprint(rows):
    from review_evidence import fingerprint
    fields = ('moduleId', 'contractId', 'contractFingerprint', 'artifactManifest', 'reviewRequirements',
              'reviewSchedule', 'reviewEvidence', 'reviewer', 'reviewNodeId', 'checks',
              'independentReview', 'sameModelReview', 'workerEvidence')
    if not any(row.get('workerEvidence') for row in rows):
        fields = tuple(key for key in fields if key != 'workerEvidence')
    return fingerprint([{key: copy.deepcopy(row.get(key)) for key in fields}
                        for row in sorted(rows, key=lambda item: item['moduleId'])])


def _verify_review_rows(studio, session, run, rows, files=None):
    from review_evidence import review_version
    if review_version(run) != 1:
        return
    from review_evidence import validate_contract, verify_evidence
    modules = run['plan']['modules']
    if len(rows) != len(modules) or {row.get('moduleId') for row in rows} != {module['id'] for module in modules}:
        raise ValueError('质量记录没有完整覆盖本轮模块')
    for module in modules:
        contract = run.get('taskContracts', {}).get(module['id'], {})
        if contract.get('reviewRequirementVersion') != 1:
            raise ValueError('本轮缺少执行前冻结的复核契约')
        schedule = validate_contract(module, contract)
        row = next(row for row in rows if row['moduleId'] == module['id'])
        if (row.get('contractId') != contract['id'] or row.get('contractFingerprint') != contract['fingerprint']
                or row.get('reviewRequirementVersion') != 1
                or row.get('reviewRequirements') != contract['reviewRequirements'] or row.get('reviewSchedule') != schedule):
            raise ValueError('质量记录的复核义务与冻结契约不同')
        if schedule['mode'] == 'none':
            if any(row.get(key) is not None for key in ('reviewer', 'reviewNodeId', 'independentReview', 'sameModelReview', 'reviewEvidence')):
                raise ValueError('未安排复核不能重分类旧结论为独立证明')
            review = None
        else:
            same = schedule['mode'] == 'same_model'
            review = row.get('sameModelReview' if same else 'independentReview')
            evidence = row.get('reviewEvidence') or {}
            if (row.get('reviewer') != schedule['reviewer'] or row.get('reviewNodeId') != evidence.get('reviewNodeId')
                    or row.get('independentReview' if same else 'sameModelReview') is not None or review is None):
                raise ValueError('必需复核的身份、模式或响应证明缺失')
        verify_evidence(studio, session, run, module, contract, review, row.get('reviewEvidence'),
                        row.get('artifactManifest', []), actual_files=files)


def verify_review_delivery(studio, session, run, files):
    """Before first publication only: verify recorded reviews without model calls."""
    from review_evidence import review_version
    if review_version(run) != 1:
        return
    import hashlib
    history = run.get('qualityHistory', [])
    record = history[-1] if history else None
    if not record or record.get('passed') is not True or record.get('reviewRequirementVersion') != 1:
        raise ValueError('交付缺少本版本已通过的复核质量关卡')
    from repair_routing import verify_record
    verify_record(run, record)
    rows = record['modules']
    if record.get('reviewEvidenceFingerprint') != _review_rows_fingerprint(rows):
        raise ValueError('质量关卡的复核证据指纹已变化')
    manifest = [item for row in rows for item in row.get('artifactManifest', [])]
    if len({item['path'] for item in manifest}) != len(manifest) or {item['path'] for item in manifest} != set(files):
        raise ValueError('当前交付文件集合与质量记录不一致')
    for item in manifest:
        raw = files[item['path']]
        if len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('当前交付内容与已复核产物不一致')
    _verify_review_rows(studio, session, run, rows, files)


def _repair_modules(studio, session, run, plan, complete, round_no, cancel):
    from repair_routing import version as routing_version, latest_round, require_repairable
    previous = (latest_round(run, round_no - 1) if routing_version(run) else
                next(r for r in run['qualityHistory'] if r['round'] == round_no - 1))
    if routing_version(run):
        require_repairable(run, previous)
    failures = {r['moduleId']: r for r in previous['modules'] if not r['gatePassed']}
    affected = set(failures)
    while True:
        downstream = {m['id'] for m in plan['modules'] if affected.intersection(m['dependsOn'])}
        if downstream <= affected:
            break
        affected |= downstream
    from dag_scheduler import execute_dag
    modules = {m['id']: m for m in plan['modules'] if m['id'] in affected}
    # Only affected dependencies belong to this scheduling graph. Execution
    # still receives the original module/contract and every actual dependency.
    graph = [{**m, 'dependsOn': [mid for mid in m['dependsOn'] if mid in affected]}
             for m in modules.values()]
    before = copy.deepcopy(complete)
    anchor = ('integration-' + previous['reviewId'] if previous.get('reviewId', '').startswith('repair-review-r')
              else 'integration' if round_no == 1 else 'integration-r' + str(round_no - 1))

    def repair(scheduled, repaired_upstream):
        module = modules[scheduled['id']]
        mid = module['id']
        upstream = {dep: copy.deepcopy(repaired_upstream[dep] if dep in affected else before[dep])
                    for dep in module['dependsOn']}
        deps = [anchor] + [_build_node(run, dep, upstream[dep], round_no)['id'] for dep in module['dependsOn']]
        node = _node(studio, run, 'repair-' + str(round_no) + '-' + mid,
                     module['title'] + ' · 定向修复 ' + str(round_no), module['assignment']['model'], 'build',
                     deps, module, module['roleId'])
        node['repairRound'] = round_no
        shared = {'businessStage': 'build', 'roleId': module['roleId'], 'plan': copy.deepcopy(plan),
                  'module': copy.deepcopy(module), 'executionContract': copy.deepcopy(run['taskContracts'][mid]),
                  'clarificationsUsed': True,
                  'upstream': upstream,
                  'repair': {'round': round_no, 'limit': run['repairLimit'],
                             'feedback': copy.deepcopy(failures.get(mid)),
                             'reason': '验收未通过' if mid in failures else '上游已修正，按最新文件重新校对本模块',
                             'previousFiles': copy.deepcopy(before[mid]['files'])}}
        result = studio.ask(session, run, node, 'worker-' + module['roleId'] + '-' + mid + '-' + node['agent'],
            BUILD_PROMPT + '\n这是有限定向修复：只处理repair中的验收反馈和上游变化，保持原范围、接口和固定标准；返回完整文件，禁止提出新questions。', shared, cancel)
        studio.validate_files(result, module['scope'], allow_binary=MODELS[node['agent']]['weights'].get('image', 0) > 0)
        from worker_integration import checks as worker_checks
        worker_checks(studio, session, run, module, result)
        return result

    # Completed repair nodes already persist their own results. If this round
    # fails, resume reuses those nodes; no partial mutable map is shared between
    # workers or mistaken for an accepted round of quality evidence.
    complete.update(execute_dag(graph, repair, cancel, max_workers=3))
