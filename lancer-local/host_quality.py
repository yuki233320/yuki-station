"""Bind registered host receipts to quality gates; reports are never deliverables."""
import copy
import hashlib
import json

from app import safe_path
from live import Cancelled


NOTICE = ('以下是宿主验收回执，不是模型自述。是否实际执行以executed为准；'
          '仅覆盖登记criteria和绑定产物，不能推及其他标准、浏览器或部署。'
          '报告可以逐行引用，但不属于交付产物；其他固定标准仍须独立核对。')


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _manifest(files):
    return [{'path': path, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for path, data in sorted(files.items())]


def _brief_criteria(criteria):
    budget, result = 8000, []
    for item in criteria:
        row = {key: item[key] for key in ('criterionId', 'status') if key in item}
        for key, limit in (('description', 250), ('detail', 600)):
            if key in item:
                value = str(item[key])
                take = min(limit, budget)
                row[key] = value[:take]
                budget -= len(row[key])
                if len(value) > take or item.get(key + 'Truncated') is True:
                    row[key + 'Truncated'] = True
        result.append(row)
    return result


def _brief_receipt(receipt):
    fields = ('id', 'round', 'status', 'passed', 'executed', 'exitCode', 'scriptSha256',
              'artifactManifest', 'artifactFingerprint', 'bindingFingerprint', 'receiptRef',
              'stdoutRef', 'stderrRef', 'stdoutSha256', 'stderrSha256', 'startedAt', 'endedAt',
              'elapsedMs', 'workspaceUnchanged', 'basis', 'cancelled')
    result = {key: copy.deepcopy(receipt[key]) for key in fields if key in receipt}
    result['criteria'] = _brief_criteria(receipt.get('criteria', []))
    if receipt.get('error'):
        result['error'] = str(receipt['error'])[:2000]
        result['errorTruncated'] = len(str(receipt['error'])) > 2000
    result['notice'] = '完整标准详情和输出保存在原始receipt/stdout/stderr引用中；限长字段有Truncated标记。'
    return result


def validate_plan_checks(plan, checks):
    """Map registered paths to one declared owner, even before output exists."""
    from studio import file_allowed
    owners = {}
    for check in checks:
        cid = check['id']
        if cid in owners or not check.get('files'):
            raise ValueError('宿主验收编号重复或未登记确切产物路径：' + cid)
        mids = set()
        for path in check['files']:
            matched = [m['id'] for m in plan['modules'] if file_allowed(path, m['scope'])]
            if len(matched) != 1:
                raise ValueError('宿主验收文件必须属于唯一模块范围：' + path)
            mids.add(matched[0])
        owners[cid] = sorted(mids)
    return owners


def _history(run, round_no):
    if not run.get('hostAcceptanceChecks'):
        return None
    row = next((item for item in reversed(run.get('hostAcceptanceHistory', [])) if item['round'] == round_no), None)
    if row is None:
        raise ValueError('当前候选缺少宿主验收回执，不能跳过宿主关卡')
    if (row['checksFingerprint'] != _fingerprint(run['hostAcceptanceChecks'])
            or row['fingerprint'] != _fingerprint({key: value for key, value in row.items() if key != 'fingerprint'})):
        raise ValueError('宿主验收记录或冻结检查已变化，不能复用证据')
    return row


def _receipt_file(studio, session, receipt):
    ref = receipt.get('receiptRef')
    if not isinstance(ref, str) or not ref:
        raise ValueError('宿主验收缺少原始回执引用')
    path = safe_path(studio.project_dir(session['projectId']) / session['id'], ref)
    if not path.is_file() or path.is_symlink():
        raise ValueError('宿主验收原始回执不存在或不安全')
    return path


def _receipt_status(studio, session, check, receipt, manifest, round_no):
    errors, raw_sha = [], None
    status = receipt.get('status', 'unknown')
    if status not in {'passed', 'failed', 'error', 'timeout', 'unknown'}:
        status = 'unknown'
        errors.append('未识别的宿主验收状态')
    try:
        raw = _receipt_file(studio, session, receipt).read_bytes()
        raw_sha = hashlib.sha256(raw).hexdigest()
        if status != 'unknown' and json.loads(raw) != receipt:
            errors.append('返回摘要与原始回执不一致')
    except (ValueError, OSError) as exc:
        errors.append(str(exc))
    if status in {'passed', 'failed'}:
        if status == 'passed' and (receipt.get('executed') is not True
                                  or type(receipt.get('exitCode')) is not int or receipt['exitCode'] != 0):
            errors.append('通过回执必须证明脚本已实际执行且退出码为0')
        if (receipt.get('id') != check['id'] or receipt.get('round') != round_no
                or receipt.get('scriptSha256') != check['scriptSha256']
                or receipt.get('artifactManifest') != manifest
                or receipt.get('artifactFingerprint') != _fingerprint(manifest)
                or not receipt.get('bindingFingerprint')):
            errors.append('宿主验收与本轮检查或实际产物指纹不匹配')
        criteria = receipt.get('criteria', [])
        expected = {item['id'] for item in check['criteria']}
        if (not isinstance(criteria, list) or any(not isinstance(item, dict) for item in criteria)
                or len(criteria) != len(expected)
                or {item.get('criterionId') for item in criteria} != expected
                or any(item.get('status') not in ('passed', 'failed') for item in criteria)):
            errors.append('宿主验收没有完整覆盖已登记标准')
        elif (status == 'passed') != all(item['status'] == 'passed' for item in criteria):
            errors.append('宿主验收状态与逐项标准不一致')
        if type(receipt.get('passed')) is not bool or receipt['passed'] != (status == 'passed'):
            errors.append('宿主验收通过标志与状态不一致')
    return ('unknown' if errors else status), raw_sha, errors


def execute_round(studio, session, run, plan, complete, files, round_no, cancel):
    checks = run.get('hostAcceptanceChecks', [])
    if not checks:
        return None
    from host_acceptance import run_check
    from studio import file_allowed
    owners = validate_plan_checks(plan, checks)
    for check in checks:
        for path in check['files']:
            expected_owner = next(module['id'] for module in plan['modules'] if file_allowed(path, module['scope']))
            actual = [mid for mid, output in complete.items() if any(item['path'] == path for item in output['files'])]
            if actual and (len(actual) != 1 or actual[0] != expected_owner):
                raise ValueError('宿主验收产物与实际交付模块不一致：' + path)
    manifest, rows = _manifest(files), []
    for check in checks:
        if cancel.is_set():
            raise Cancelled()
        # The runner verifies its durable cache every time; memory is not proof.
        receipt = run_check(studio, session, run, check, files, round_no, cancel)
        if cancel.is_set():
            raise Cancelled()
        status, raw_sha, errors = _receipt_status(studio, session, check, receipt, manifest, round_no)
        missing = [path for path in check['files'] if path not in files]
        if missing and status == 'passed':
            status = 'unknown'
            errors.append('回执声称通过但登记产物缺失：' + '、'.join(missing))
        report_path = 'host-acceptance-evidence/' + run['id'] + '/round-' + str(round_no) + '/' + _fingerprint(check)[:24] + '.json'
        if report_path in files:
            raise ValueError('交付产物与宿主只读证据路径冲突')
        report = {'notice': NOTICE, 'check': {key: copy.deepcopy(check[key]) for key in ('id', 'name', 'files', 'scriptSha256') if key in check},
                  'moduleIds': owners[check['id']],
                  'round': round_no, 'status': status, 'passed': status == 'passed',
                  'expectedArtifactManifest': manifest, 'receipt': _brief_receipt(receipt), 'validationErrors': errors}
        rows.append({'checkId': check['id'], 'name': check.get('name', check['id']),
                     'moduleIds': owners[check['id']], 'status': status, 'passed': status == 'passed',
                     'receipt': copy.deepcopy(receipt), 'receiptSha256': raw_sha, 'validationErrors': errors,
                     'evidenceFile': {'path': report_path, 'content': json.dumps(report, ensure_ascii=False, indent=2) + '\n'}})
    record = {'schema': 'yuki-host-quality/v1', 'round': round_no, 'checksFingerprint': _fingerprint(checks),
              'artifactManifest': manifest, 'artifactFingerprint': _fingerprint(manifest), 'checks': rows}
    record['fingerprint'] = _fingerprint(record)
    with studio.lock:
        history = run.setdefault('hostAcceptanceHistory', [])
        existing = next((item for item in reversed(history) if item['round'] == round_no), None)
        if existing != record:
            history.append(record)
            studio.event(session, 'host_acceptance', '宿主验收已返回：' + '；'.join(row['name'] + ' · ' + row['status'] for row in rows),
                         round=round_no, hostAcceptanceFingerprint=record['fingerprint'])
        # Persist even when a failed save left a matching row in memory.
        studio.save(session)
    return record


def evidence_files(run, round_no, module_id=None):
    record = _history(run, round_no)
    return [] if record is None else [copy.deepcopy(row['evidenceFile']) for row in record['checks']
                                     if module_id is None or module_id in row['moduleIds']]


def evidence_paths(run, round_no, plan, complete):
    return {module['id']: [item['path'] for item in evidence_files(run, round_no, module['id'])]
            for module in plan['modules']}


def summaries(run, round_no, module_id=None):
    record = _history(run, round_no)
    if record is None:
        return []
    return [{'checkId': row['checkId'], 'name': row['name'], 'status': row['status'], 'passed': row['passed'],
             'criteria': _brief_criteria(row['receipt'].get('criteria', [])), 'receiptRef': row['receipt'].get('receiptRef'),
             'receiptSha256': row['receiptSha256'], 'artifactFingerprint': record['artifactFingerprint'],
             'artifactManifest': copy.deepcopy(record['artifactManifest']), 'evidencePath': row['evidenceFile']['path'],
             'validationErrors': copy.deepcopy(row['validationErrors']), 'executed': row['receipt'].get('executed'),
             'error': str(row['receipt'].get('error', ''))[:2000],
             'errorTruncated': len(str(row['receipt'].get('error', ''))) > 2000, 'notice': NOTICE}
            for row in record['checks'] if module_id is None or module_id in row['moduleIds']]


def module_checks(run, round_no, module_id):
    checks = []
    for row in summaries(run, round_no, module_id):
        failed = [criterion for criterion in row['criteria'] if criterion['status'] == 'failed']
        details = [criterion['criterionId'] + '：' + str(criterion.get('detail') or criterion.get('description') or 'failed')
                   for criterion in failed]
        details.extend(row['validationErrors'])
        if row.get('error'):
            details.append(row['error'])
        detail = '；'.join(details) or row['status']
        truncated = (len(detail) > 3000 or row.get('errorTruncated') is True
                     or any(criterion.get('detailTruncated') or criterion.get('descriptionTruncated')
                            for criterion in failed))
        # Keep host counter-evidence in repair.feedback even if the coordinator
        # wrongly approved the candidate and supplied no unmet explanation.
        checks.append({'name': '宿主验收 · ' + row['name'], 'pass': row['passed'], 'type': 'host_acceptance',
                       'checkId': row['checkId'], 'status': row['status'], 'receiptRef': row['receiptRef'],
                       'artifactFingerprint': row['artifactFingerprint'], 'evidencePath': row['evidencePath'],
                       'repairAction': 'repair' if row['status'] in ('passed', 'failed') else 'needs_input',
                       'criteria': copy.deepcopy(row['criteria']), 'executed': row['executed'],
                       'detail': detail[:3000], 'detailTruncated': bool(truncated)})
    return checks


def bind_review(studio, session, run, node, round_no, files):
    record = _history(run, round_no)
    if record is None:
        return
    binding = _fingerprint({'hostEvidence': record['fingerprint'], 'reviewFiles': files})
    with studio.lock:
        if node['status'] == 'done' and node.get('hostEvidenceBinding') != binding:
            raise ValueError('缓存的模型审查不匹配当前宿主回执或产物，不能复用旧结论')
        node['hostEvidenceBinding'] = binding
        studio.save(session)


def history_fingerprint(run, round_no):
    record = _history(run, round_no)
    return record['fingerprint'] if record else None


def verify_delivery(studio, session, run, files):
    """Read-only final check before first publication; never execute a checker."""
    if not run.get('hostAcceptanceChecks'):
        return
    from host_acceptance import verify_receipt
    history = run.get('qualityHistory', [])
    quality = history[-1] if history else None
    if quality is None or quality.get('passed') is not True:
        raise ValueError('交付缺少已通过的宿主质量关卡')
    record = _history(run, quality['round'])
    if (quality.get('hostAcceptanceFingerprint') != record['fingerprint']
            or _manifest(files) != record['artifactManifest']):
        raise ValueError('交付产物或验收回执已经变化，不能使用旧宿主结果合入')
    for row in record['checks']:
        if not row['passed']:
            raise ValueError('宿主验收未通过，不能合入')
        raw = _receipt_file(studio, session, row['receipt']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['receiptSha256'] or json.loads(raw) != row['receipt']:
            raise ValueError('宿主原始回执变化，不能以旧摘要合入')
        check = next(check for check in run['hostAcceptanceChecks'] if check['id'] == row['checkId'])
        if not verify_receipt(studio, session, run, check, files, quality['round'], row['receipt']):
            raise ValueError('宿主验收原始输出、脚本授权或候选副本未通过只读核验，不能合入')


def verify_round_evidence(studio, session, run, files, round_no):
    """Verify even failed round originals without executing any checker."""
    if not run.get('hostAcceptanceChecks'):
        return
    from host_acceptance import verify_receipt
    record = _history(run, round_no)
    if record is None or _manifest(files) != record['artifactManifest']:
        raise ValueError('同产物复审缺少匹配候选的宿主验收记录')
    for row in record['checks']:
        raw = _receipt_file(studio, session, row['receipt']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['receiptSha256'] or json.loads(raw) != row['receipt']:
            raise ValueError('宿主原始回执已变化，停止复审')
        check = next(check for check in run['hostAcceptanceChecks'] if check['id'] == row['checkId'])
        if not verify_receipt(studio, session, run, check, files, round_no, row['receipt']):
            raise ValueError('宿主原始输出、脚本授权或候选副本不完整，停止复审')
