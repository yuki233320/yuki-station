"""Frozen host routing of failed quality outcomes; never grants execution rights."""
import copy
from review_evidence import fingerprint

VERSION = 1
POLICY = {'version': 1, 'sameArtifactReviewLimit': 1}
KINDS = {
    'none': ('none', '质量关卡通过'),
    'code_repair': ('repair', '按具体产物缺陷定向修复'),
    'evidence_review': ('review', '保持产物不变，核对已有证据'),
    'review_disagreement': ('review', '保持产物不变，澄清复核分歧'),
    'configuration': ('stop', '执行或配置证据异常，停止自动返工'),
    'needs_input': ('stop', '缺少可验证的修复目标，等待补充信息'),
    'replan': ('stop', '需要重新规划，保留本轮冻结契约'),
}


def version(run):
    contracts = list(run.get('taskContracts', {}).values())
    records = run.get('qualityHistory', [])
    marked = any(k in run for k in ('repairRoutingVersion', 'repairRoutingPolicy'))
    marked |= any('repairRoutingVersion' in c or 'repairRoutingPolicy' in c for c in contracts)
    marked |= any('repairRoutingVersion' in r or any('repairRoute' in row for row in r.get('modules', [])) for r in records)
    marked |= any('qualityReviewId' in n or (n.get('artifactReviewBinding') or {}).get('repairRoutingVersion') is not None for n in run.get('nodes', []))
    if not marked:
        return 0
    if type(run.get('repairRoutingVersion')) is not int or run['repairRoutingVersion'] != VERSION or run.get('repairRoutingPolicy') != POLICY:
        raise ValueError('返工分流策略缺失或已变化，不能降级为旧轮次')
    for contract in contracts:
        if type(contract.get('repairRoutingVersion')) is not int or contract['repairRoutingVersion'] != VERSION or contract.get('repairRoutingPolicy') != POLICY:
            raise ValueError('冻结契约缺少一致的返工分流策略')
    return VERSION


def route(row, review_exhausted=False):
    """Classify structured host/review facts, never free-form reason substrings."""
    def result(kind, *codes):
        action, summary = KINDS[kind]
        if review_exhausted and action == 'review':
            action = 'stop'
            summary = '同产物复审已用尽一次，仍未解决：' + summary
            codes = (*codes, 'review_limit_reached')
        return {'version': VERSION, 'kind': kind, 'action': action,
                'reasonCodes': list(codes), 'summary': summary}
    if row.get('gatePassed') is True:
        return result('none', 'quality_passed')
    checks = [c for c in row.get('checks', []) if c.get('pass') is not True]
    unsafe = [c for c in checks if c.get('type') not in ('syntax', 'scope', 'host_acceptance')
              or (c.get('type') == 'host_acceptance' and (c.get('status') != 'failed' or c.get('executed') is not True))]
    if unsafe:
        return result('configuration', 'machine_check_unresolved')
    if row.get('repairAction') in ('needs_input', 'replan'):
        return result(row['repairAction'], 'coordinator_requires_' + row['repairAction'])
    if checks:
        return result('code_repair', 'machine_artifact_failure')
    review = row.get('criteria', [])
    coordinator = row.get('coordinatorCriteria', [])
    blockers = [f for f in row.get('findings', []) if f.get('priority') in ('P0', 'P1') and f.get('evidence')]
    if blockers:
        return result('code_repair', 'review_cited_blocking_finding')
    by_id = {c.get('criterionId'): c for c in coordinator}
    disagreement = any({c.get('status'), by_id.get(c.get('criterionId'), {}).get('status')} == {'met', 'unmet'} for c in review)
    if row.get('reviewer') and disagreement:
        return result('review_disagreement', 'criterion_status_conflict')
    if any(c.get('status') == 'unverified' for c in review + coordinator):
        return result('evidence_review', 'criterion_unverified')
    unmet = [c for c in review + coordinator if c.get('status') == 'unmet']
    if unmet and all(c.get('evidence') for c in unmet):
        return result('code_repair', 'cited_unmet_criterion')
    return result('needs_input', 'no_verified_repair_target')


def attach(run, record):
    if not version(run):
        return
    record['repairRoutingVersion'] = VERSION
    for row in record['modules']:
        row['repairRoute'] = route(row, review_exhausted=record.get('reviewId', '').startswith('repair-review-r'))
    record['repairRoutingFingerprint'] = record_fingerprint(record)


def record_fingerprint(record):
    fields = ('moduleId', 'gatePassed', 'checks', 'criteria', 'coordinatorCriteria',
              'findings', 'reviewer', 'repairAction', 'repairRoute', 'artifactManifest',
              'contractFingerprint', 'reviewEvidence')
    return fingerprint({'round': record['round'], 'reviewId': record.get('reviewId'),
                        'passed': record['passed'], 'modules': [{k: copy.deepcopy(row.get(k)) for k in fields} for row in record['modules']]})


def verify_record(run, record):
    if not version(run):
        return
    if (record.get('repairRoutingVersion') != VERSION or record.get('repairRoutingFingerprint') != record_fingerprint(record)
            or any(row.get('repairRoute') != route(row, review_exhausted=record.get('reviewId', '').startswith('repair-review-r')) for row in record['modules'])):
        raise ValueError('质量记录的返工分类或原始依据已变化')


def source_record(run, round_no):
    return next((r for r in run.get('qualityHistory', []) if r['round'] == round_no and not r.get('reviewId')), None)


def review_attempt(run, round_no, module_id=None):
    if version(run) != VERSION:
        raise ValueError('旧轮次没有冻结的同产物复审权限')
    record = source_record(run, round_no)
    if record is None:
        raise ValueError('同产物复审缺少原始质量记录')
    verify_record(run, record)
    selected = [row['moduleId'] for row in record['modules'] if row['repairRoute']['action'] == 'review']
    if not selected or (module_id is not None and module_id not in selected):
        raise ValueError('当前模块不属于冻结的同产物复审目标')
    return 'repair-review-r' + str(round_no)


def latest_round(run, round_no):
    record = next(r for r in reversed(run['qualityHistory']) if r['round'] == round_no)
    verify_record(run, record)
    return record


def require_repairable(run, record):
    verify_record(run, record)
    blocked = [r for r in record['modules'] if r['repairRoute']['action'] not in ('repair', 'none')]
    if blocked:
        raise ValueError('质量验收未通过，停止自动返工；' + '；'.join('模块 ' + r['moduleId'] + '：' + r['repairRoute']['summary'] + ('；' + '；'.join(r.get('unmet', [])) if r.get('unmet') else '') for r in blocked)
                         + '。现有证据与冻结契约已保留；未解决的证据/分歧不转交 Worker 盲目改写。')
