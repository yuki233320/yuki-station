"""Artifact reviews and host-computed measures, separate from runtime evidence."""
import copy
import hashlib

from model_catalog import MODELS, rank_models


SAME_MODEL_REVIEW_NOTICE = ('这是单模型对照：规划、执行和复核使用同一型号，各岗位上下文独立。'
                          '本次是同模型复核，不是不同模型的独立确认；仍须按固定标准核对实际证据。')


def reviewer_for(module, available, coordinator='opus'):
    author = module['assignment']['model']
    excluded = {coordinator, 'native_codex', 'image', 'codex'}
    excluded.update(mid for mid, model in MODELS.items()
                    if model['family'] == MODELS[author]['family'])
    candidates = rank_models({'review': .6, 'coding': .4}, available, exclude=excluded)
    if not candidates:
        raise ValueError('独立产物审查需要连接主负责人之外、与执行者不同系列的代码模型：' + module['title'])
    return candidates[0]['model']


def artifact_review_required(module, contract=None):
    if contract and (contract.get('reviewRequirementVersion') == 1 or module.get('reviewRequirementVersion') == 1):
        from review_evidence import validate_contract
        return validate_contract(module, contract)['mode'] != 'none'
    if module.get('reviewRequirementVersion') == 1:
        return module['reviewSchedule']['mode'] != 'none'
    return module.get('artifactReview', module['reviewPolicy']['required'])


def artifact_manifest(studio, session, output):
    manifest = []
    for item in output['files']:
        data = ((studio.project_dir(session['projectId']) / session['id'] / item['binaryRef']).read_bytes()
                if 'binaryRef' in item else item['content'].encode())
        manifest.append({'path': item['path'], 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return manifest


def validate_review(result, shared):
    _review_structure(result, shared['qualityContract'])
    line_counts = {item['path']: len(item['content'].splitlines()) for item in shared['reviewFiles']}
    for criterion in result['criteria']:
        _evidence(criterion['evidence'], line_counts, required=criterion['status'] == 'met')
    for finding in result['findings']:
        _evidence(finding['evidence'], line_counts, required=True)
    return result


def _review_structure(result, contract):
    if (not isinstance(result, dict) or not _text(result.get('summary'))
            or set(result) - {'summary', 'criteria', 'findings'}):
        raise ValueError('独立审查只接收摘要、逐项标准和具体问题，不接收模型自定总分')
    criteria = result.get('criteria')
    expected = {item['id'] for item in contract['criteria']}
    if (not isinstance(criteria, list) or len(criteria) != len(expected)
            or any(not isinstance(item, dict) or not isinstance(item.get('criterionId'), str) for item in criteria)
            or {item['criterionId'] for item in criteria} != expected):
        raise ValueError('独立审查须不重复地覆盖全部固定验收标准编号')
    for criterion in criteria:
        if (criterion.get('status') not in ('met', 'unmet', 'unverified')
                or not _text(criterion.get('reason')) or 'evidence' not in criterion):
            raise ValueError('每项独立审查须有明确状态、理由和证据列表')
    findings = result.get('findings')
    if not isinstance(findings, list) or len(findings) > 8:
        raise ValueError('独立审查最多保留 8 条具体问题')
    for finding in findings:
        if (not isinstance(finding, dict) or finding.get('priority') not in ('P0', 'P1', 'P2', 'P3')
                or not _text(finding.get('claim')) or not _text(finding.get('proposal'))
                or 'evidence' not in finding):
            raise ValueError('独立审查问题须有优先级、具体问题、修复建议和行号证据')


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _evidence(refs, line_counts, required):
    if not isinstance(refs, list) or (required and not refs):
        raise ValueError('满足的标准和具体问题必须引用实际文件行号')
    for ref in refs:
        if (not isinstance(ref, dict) or not isinstance(ref.get('path'), str)
                or ref['path'] not in line_counts
                or type(ref.get('startLine')) is not int or type(ref.get('endLine')) is not int
                or not 1 <= ref['startLine'] <= ref['endLine'] <= line_counts[ref['path']]):
            raise ValueError('独立审查引用了不存在的文件或无效行范围')


def build_assessment(module, contract, output, manifest, checks, review=None, review_evidence=None):
    schedule = None
    if contract.get('reviewRequirementVersion') == 1 or module.get('reviewRequirementVersion') == 1:
        from review_evidence import validate_contract
        schedule = validate_contract(module, contract)
    total = len(contract['criteria'])
    met = sum(item['status'] == 'met' for item in review['criteria']) if review else 0
    unverified = sum(item['status'] == 'unverified' for item in review['criteria']) if review else total
    blockers = sum(item['priority'] in {'P0', 'P1'} for item in review['findings']) if review else 0
    checks_passed = sum(check['pass'] for check in checks)
    review_passed = review is not None and met == total and blockers == 0
    same_model = schedule['mode'] == 'same_model' if schedule else module.get('reviewMode') == 'same_model'
    if schedule and schedule['mode'] != 'none':
        review_passed = review_passed and isinstance(review_evidence, dict) and review_evidence.get('status') == 'verified'
    result = {
        'moduleId': module['id'], 'title': module['title'], 'workType': module['workType'],
        'model': module['assignment']['model'], 'contractId': contract['id'],
        'contractFingerprint': contract['fingerprint'], 'artifactManifest': copy.deepcopy(manifest),
        'independentReview': None if same_model else copy.deepcopy(review),
        'sameModelReview': copy.deepcopy(review) if same_model else None,
        'checks': copy.deepcopy(checks),
        'gatePassed': checks_passed == len(checks) and (review_passed or not artifact_review_required(module, contract)),
        'metrics': {'criteriaTotal': total, 'criteriaMet': met, 'criteriaUnverified': unverified,
                    'blockingFindings': blockers, 'checksPassed': checks_passed, 'checksTotal': len(checks),
                    'criterionCoveragePct': round(100 * met / total, 1)},
        'basis': ('same_model_artifact_review' if same_model else 'independent_artifact_review') if review else 'coordinator_pending',
    }
    if schedule:
        result.update(reviewRequirementVersion=1, reviewRequirements=copy.deepcopy(contract['reviewRequirements']),
                      reviewSchedule=copy.deepcopy(schedule), reviewEvidence=copy.deepcopy(review_evidence))
    return result
