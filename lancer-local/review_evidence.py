"""Host-owned bindings between frozen review duties and actual saved reviews."""
import copy
import hashlib
import json
import re

from app import safe_path


VERSION = 1


def review_version(run):
    """Reject partial removal of new markers; only wholly old records are legacy."""
    modules = (run.get('plan') or {}).get('modules', [])
    contracts = list(run.get('taskContracts', {}).values())
    records = run.get('qualityHistory', [])
    values = [*modules, *contracts, *records,
              *(row for record in records for row in record.get('modules', []))]
    def marked_value(value):
        return isinstance(value, dict) and ('reviewRequirementVersion' in value
            or (isinstance(value.get('reviewSchedule'), dict) and value['reviewSchedule'].get('schema') == 'yuki-review-schedule/v1')
            or (isinstance(value.get('reviewEvidence'), dict) and value['reviewEvidence'].get('schema') == 'yuki-review-evidence/v1'))
    marked = any(marked_value(value) for value in values)
    marked = marked or any((node.get('artifactReviewBinding') or {}).get('schema') in ('yuki-artifact-review-binding/v1', 'yuki-artifact-review-binding/v2')
                           for node in run.get('nodes', []))
    if 'reviewRequirementVersion' not in run and not marked:
        return 0
    if type(run.get('reviewRequirementVersion')) is not int or run['reviewRequirementVersion'] != VERSION:
        raise ValueError('复核版本标记缺失或不一致，不能把新契约降级为历史任务')
    for value in values:
        if type(value.get('reviewRequirementVersion')) is not int or value['reviewRequirementVersion'] != VERSION:
            raise ValueError('新版任务包含缺失或不同版本的复核模块、契约或质量记录')
    return VERSION


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_contract(module, contract):
    """Return the frozen schedule; never upgrade a historical contract in place."""
    if contract.get('reviewRequirementVersion') != VERSION:
        if module.get('reviewRequirementVersion') == VERSION:
            raise ValueError('新版模块缺少冻结复核契约版本，不能降级为历史语义')
        return None
    body = {key: value for key, value in contract.items() if key not in ('fingerprint', 'created', 'eventSeq')}
    if fingerprint(body) != contract.get('fingerprint'):
        raise ValueError('冻结复核契约指纹已变化')
    for key in ('reviewRequirements', 'reviewSchedule'):
        if module.get(key) != contract.get(key):
            raise ValueError('模块复核义务或指派与冻结契约不一致')
    requirement, schedule = contract.get('reviewRequirements'), contract.get('reviewSchedule')
    if not isinstance(requirement, dict) or not isinstance(schedule, dict):
        raise ValueError('冻结契约缺少复核义务与调度')
    ids = {item['id'] for item in contract['criteria']}
    refs = schedule.get('criterionIds')
    if (schedule.get('schema') != 'yuki-review-schedule/v1' or schedule.get('mode') not in ('none', 'independent', 'same_model')
            or schedule.get('author') != contract.get('model')
            or schedule.get('author') != module.get('assignment', {}).get('model')
            or not isinstance(refs, list) or len(set(refs)) != len(refs) or set(refs) - ids):
        raise ValueError('冻结复核调度无效')
    mode = schedule['mode']
    if requirement.get('mode') == 'independent' and (mode != 'independent'
            or not set(requirement.get('criterionIds', [])) <= set(refs)):
        raise ValueError('必需独立复核不能降级或遗漏固定标准')
    if mode == 'none':
        if schedule.get('reviewer') is not None or schedule.get('nodeId') is not None:
            raise ValueError('无需复核的契约不能伪造审核节点')
    elif not schedule.get('reviewer') or schedule.get('nodeId') != 'verify-0-' + module['id']:
        raise ValueError('必需复核缺少固定审核者或计划节点')
    if mode == 'independent' and (schedule['reviewer'] == schedule['author']
            or not schedule.get('authorFamily') or not schedule.get('reviewerFamily')
            or schedule['authorFamily'] == schedule['reviewerFamily']):
        raise ValueError('独立复核不能由同一作者或同系列配置代替')
    if mode == 'same_model' and (requirement.get('mode') != 'none' or schedule['reviewer'] != schedule['author']):
        raise ValueError('同模型对照复核不能冒充独立要求')
    return schedule


def files_manifest(files):
    rows = []
    for item in files:
        if not isinstance(item.get('content'), str):
            raise ValueError('复核输入必须保留实际文本')
        data = item['content'].encode()
        rows.append({'path': item['path'], 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    if len({row['path'] for row in rows}) != len(rows):
        raise ValueError('复核输入路径重复')
    return sorted(rows, key=lambda row: row['path'])


def _identity(run, alias):
    profile = run.get('modelRequestProfiles', {}).get(alias)
    return ({key: profile.get(key) for key in ('provider', 'base', 'model', 'protocol')}
            if isinstance(profile, dict) else None)


def _review_identity(run, module, round_no, review_attempt):
    """The same host round may have one separately bound, read-only review."""
    from repair_routing import version as routing_version, review_attempt as allowed_review_attempt
    routed = routing_version(run)
    if review_attempt is not None:
        if (routed != 1 or not isinstance(review_attempt, str)
                or allowed_review_attempt(run, round_no, module['id']) != review_attempt):
            raise ValueError('同产物复审没有对应的冻结返工分流决定')
        return routed, 'verify-' + review_attempt + '-' + module['id']
    return routed, 'verify-' + str(round_no) + '-' + module['id']


def _review_source(run, round_no, review_attempt):
    if review_attempt is None:
        return None
    from repair_routing import source_record
    return source_record(run, round_no)


def binding_for(run, module, contract, build, reviewer, review_files, manifest, checks, round_no, review_attempt=None):
    version = review_version(run)
    if (version == VERSION and contract.get('reviewRequirementVersion') != VERSION
            or version != VERSION and (module.get('reviewRequirementVersion') == VERSION or contract.get('reviewRequirementVersion') == VERSION)):
        raise ValueError('本轮缺少执行前冻结的复核契约版本')
    schedule = validate_contract(module, contract)
    if schedule is None:
        return None
    if schedule['mode'] == 'none' or reviewer != schedule['reviewer'] or build.get('agent') != schedule['author']:
        raise ValueError('实际作者或审核者不符合冻结复核调度')
    if schedule['mode'] == 'same_model' and run.get('comparisonArm') != 'single':
        raise ValueError('同模型复核仅限明确的单模型对照')
    author_identity, reviewer_identity = _identity(run, schedule['author']), _identity(run, reviewer)
    if (schedule['mode'] == 'independent' and author_identity and reviewer_identity
            and author_identity.get('model') == reviewer_identity.get('model')):
        raise ValueError('不同别名指向同一请求型号，不能作为独立复核')
    routed, _ = _review_identity(run, module, round_no, review_attempt)
    body = {'schema': 'yuki-artifact-review-binding/v2' if routed else 'yuki-artifact-review-binding/v1', 'moduleId': module['id'],
            'contractId': contract['id'], 'contractFingerprint': contract['fingerprint'],
            'schedule': copy.deepcopy(schedule), 'sourceNodeId': build['id'],
            'authorIdentity': author_identity, 'reviewerIdentity': reviewer_identity,
            'qualityRound': round_no, 'artifactManifest': sorted(copy.deepcopy(manifest), key=lambda row: row['path']),
            'reviewFiles': files_manifest(review_files), 'checksFingerprint': fingerprint(checks)}
    if routed:
        source = _review_source(run, round_no, review_attempt)
        body.update(repairRoutingVersion=routed, reviewAttemptId=review_attempt,
                    sourceRoutingFingerprint=source['repairRoutingFingerprint'] if source else None)
    from worker_review_evidence import version as worker_review_version, report_path
    view_version = worker_review_version(run,module,contract)
    if view_version and contract.get('workerBinding'):
        worker_files = [f for f in review_files if f['path'] == report_path(run,build)]
        if len(worker_files) != 1:
            raise ValueError('新版Worker复核必须包含绑定作者节点的宿主过程报告')
        body.update(workerReviewEvidenceVersion=view_version,workerReviewFiles=files_manifest(worker_files))
        if view_version == 2:
            from worker_review_evidence import requirements_fingerprint
            body.update(workerProcessVersion=1,workerProcessRequirementsFingerprint=requirements_fingerprint(contract['workerProcessRequirements']))
    return {**body, 'fingerprint': fingerprint(body)}


def bind_input(studio, session, node, binding):
    if binding is None:
        return
    with studio.lock:
        old = node.get('artifactReviewBinding')
        if (old is not None and old != binding) or (node.get('status') == 'done' and old is None):
            raise ValueError('缓存复核不匹配当前契约、作者、产物或证据输入')
        node['artifactReviewBinding'] = copy.deepcopy(binding)
        studio.save(session)


def _record(studio, session, kind, ref):
    if not isinstance(ref, str) or not re.fullmatch('[a-f0-9]{12}', ref):
        raise ValueError('复核缺少实际上下文或响应引用')
    path = safe_path(studio.project_dir(session['projectId']) / session['id'], kind + '/' + ref + '.json')
    if not path.is_file() or path.is_symlink():
        raise ValueError('复核原始上下文或响应缺失')
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def verify_evidence(studio, session, run, module, contract, review, evidence, manifest, actual_files=None):
    """Read-only validation. A historic model result alone is not review proof."""
    version = review_version(run)
    if (version == VERSION and contract.get('reviewRequirementVersion') != VERSION
            or version != VERSION and (module.get('reviewRequirementVersion') == VERSION or contract.get('reviewRequirementVersion') == VERSION)):
        raise ValueError('本轮缺少执行前冻结的复核契约版本')
    schedule = validate_contract(module, contract)
    if schedule is None:
        return
    if schedule['mode'] == 'none':
        if evidence is not None or review is not None:
            raise ValueError('未安排复核的模块不能附加独立复核证明')
        return
    if not isinstance(evidence, dict) or evidence.get('schema') != 'yuki-review-evidence/v1' or evidence.get('status') != 'verified':
        raise ValueError('必需复核缺少宿主绑定的实际响应证据')
    binding = evidence.get('binding')
    if not isinstance(binding, dict) or binding.get('fingerprint') != fingerprint({k: v for k, v in binding.items() if k != 'fingerprint'}):
        raise ValueError('复核证据绑定指纹无效')
    if (binding.get('contractId') != contract['id'] or binding.get('contractFingerprint') != contract['fingerprint']
            or binding.get('schedule') != schedule or binding.get('moduleId') != module['id']
            or binding.get('artifactManifest') != sorted(manifest, key=lambda row: row['path'])):
        raise ValueError('复核证据不是当前冻结契约与产物')
    round_no = binding.get('qualityRound')
    routed, expected_node = _review_identity(run, module, round_no, binding.get('reviewAttemptId'))
    source = _review_source(run, round_no, binding.get('reviewAttemptId'))
    if routed:
        if (binding.get('schema') != 'yuki-artifact-review-binding/v2'
                or type(binding.get('repairRoutingVersion')) is not int or binding['repairRoutingVersion'] != routed
                or 'reviewAttemptId' not in binding or 'sourceRoutingFingerprint' not in binding
                or binding['sourceRoutingFingerprint'] != (source['repairRoutingFingerprint'] if source else None)):
            raise ValueError('新版返工复审缺少对应的版本或尝试绑定')
    elif (binding.get('schema') != 'yuki-artifact-review-binding/v1'
          or 'repairRoutingVersion' in binding or 'reviewAttemptId' in binding or 'sourceRoutingFingerprint' in binding):
        raise ValueError('历史复核不能事后增加或降级返工复审绑定')
    node = next((node for node in run['nodes'] if node['id'] == evidence.get('reviewNodeId')), None)
    build = next((node for node in run['nodes'] if node['id'] == binding.get('sourceNodeId')), None)
    if (type(round_no) is not int or round_no < 0 or node is None or build is None
            or node.get('id') != expected_node
            or routed and node.get('qualityReviewId') != binding['reviewAttemptId']
            or node.get('status') != 'done' or node.get('phase') != 'verification'
            or node.get('responseValidation') != 'passed' or node.get('moduleId') != module['id']
            or node.get('agent') != schedule['reviewer'] or build.get('agent') != schedule['author']
            or build.get('status') != 'done' or build.get('moduleId') != module['id'] or build.get('phase') != 'build'
            or node.get('qualityRound') != binding.get('qualityRound') or node.get('artifactReviewBinding') != binding
            or node.get('result') != review or node.get('contextRef') != evidence.get('contextRef')
            or node.get('responseRef') != evidence.get('responseRef')):
        raise ValueError('实际复核节点缺失、未完成或身份/产物绑定不一致')
    from quality_assessment import artifact_manifest, validate_review
    if sorted(artifact_manifest(studio, session, build['result']), key=lambda row: row['path']) != binding['artifactManifest']:
        raise ValueError('作者产物已变化，旧复核不能复用')
    context, context_sha = _record(studio, session, 'contexts', evidence['contextRef'])
    response, response_sha = _record(studio, session, 'responses', evidence['responseRef'])
    if context_sha != evidence.get('contextSha256') or response_sha != evidence.get('responseSha256'):
        raise ValueError('复核上下文或响应原件校验失败')
    shared = context.get('memory', {}).get('sharedEvidence', {})
    if source:
        feedback = next(row for row in source['modules'] if row['moduleId'] == module['id'])
        if shared.get('repairReviewFeedback') != feedback:
            raise ValueError('同产物复审没有读取对应原始质量记录的实际反馈')
    if (context.get('runId') != run['id'] or context.get('model') != schedule['reviewer']
            or context.get('agentId') != node.get('agentId') or context.get('agentId') == build.get('agentId')
            or context.get('id') == build.get('contextRef')
            or shared.get('qualityContract') != contract or shared.get('qualityRound') != binding['qualityRound']
            or routed and shared.get('qualityReviewId') != binding['reviewAttemptId']
            or files_manifest(shared.get('reviewFiles', [])) != binding['reviewFiles']
            or fingerprint(shared.get('checks')) != binding['checksFingerprint']
            or response.get('node') != node['id'] or response.get('contextRef') != context['id']
            or response.get('result') != review or response.get('receipt') != node.get('receipt')):
        raise ValueError('复核结果没有对应的独立上下文和真实响应')
    if binding.get('authorIdentity') != _identity(run, schedule['author']) or binding.get('reviewerIdentity') != _identity(run, schedule['reviewer']):
        raise ValueError('复核配置身份与本轮冻结请求不同')
    if schedule['mode'] == 'independent':
        author_model = build.get('receipt', {}).get('returnedModel')
        review_model = node.get('receipt', {}).get('returnedModel')
        if author_model and review_model and author_model == review_model:
            raise ValueError('提供商回执指向同一型号，不能称为独立复核')
    validate_review(copy.deepcopy(review), shared)
    from worker_review_evidence import version as worker_review_version, evidence_files as worker_files
    view_version = worker_review_version(run,module,contract)
    worker_reports = worker_files(studio,session,run,module,build['result']) if view_version and contract.get('workerBinding') else []
    expected_worker = files_manifest(worker_reports)
    expected_paths = {f['path'] for f in expected_worker}
    bound_worker = [f for f in binding['reviewFiles'] if f['path'] in expected_paths]
    if worker_reports:
        if (binding.get('workerReviewEvidenceVersion') != view_version or binding.get('workerReviewFiles') != expected_worker
                or bound_worker != expected_worker):
            raise ValueError('Worker过程复核报告缺失或原始回执已变化，不能复用旧审查')
    elif any(k in binding for k in ('workerReviewEvidenceVersion','workerReviewFiles','workerProcessVersion','workerProcessRequirementsFingerprint')):
        raise ValueError('旧任务或普通模块不能附加未冻结Worker过程证据')
    if actual_files is not None:
        for item in binding['artifactManifest']:
            raw = actual_files.get(item['path'])
            if raw is None or len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('当前交付缺少复核绑定的本模块产物或内容已变化')
        from host_quality import evidence_files
        reports = {item['path']: item['content'].encode() for item in evidence_files(run, binding['qualityRound'], module['id'])}
        reports.update({item['path']:item['content'].encode() for item in worker_reports})
        for item in binding['reviewFiles']:
            raw = actual_files.get(item['path'], reports.get(item['path']))
            if raw is None or len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('复核引用的交付文件或宿主报告已变化，必须重新核对')


def capture_evidence(studio, session, run, module, contract, node, binding, review, manifest):
    if binding is None:
        return None
    _, context_sha = _record(studio, session, 'contexts', node.get('contextRef'))
    _, response_sha = _record(studio, session, 'responses', node.get('responseRef'))
    evidence = {'schema': 'yuki-review-evidence/v1', 'status': 'verified',
                'identityBasis': 'frozen_requested_configuration', 'reviewNodeId': node['id'],
                'contextRef': node['contextRef'], 'responseRef': node['responseRef'],
                'contextSha256': context_sha, 'responseSha256': response_sha, 'binding': copy.deepcopy(binding)}
    verify_evidence(studio, session, run, module, contract, review, evidence, manifest)
    return evidence
