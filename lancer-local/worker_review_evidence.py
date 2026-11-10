"""Host-generated, citeable Worker process views; never author-written reports.

Original records remain authoritative. A view is regenerated from verified
receipts, contains no file bodies, and cannot upgrade old review bindings.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import re

# V1 text/field projection is part of saved review file hashes. Future changes
# require a new version renderer; do not silently rewrite historical V1 views.
VERSION = 1
SUPPORTED_VERSIONS = (1, 2)
PROCESS_FIELD = 'workerProcessVersion'
PROCESS_REQUIREMENTS = 'workerProcessRequirements'
FIELD = 'workerReviewEvidenceVersion'
PREFIX = 'worker-execution-evidence/'
MAX_PARENT_STEPS = 120
MAX_CHILD_STEPS = 64
MAX_VIEW_BYTES = 220000
NOTICE = ('这是宿主从已核验Worker原始回执重建的只读过程证据，不是作者编写的报告。'
          '只证明列出的实际动作、状态及其绑定副本；未列出的步骤不能推断已完成。'
          '子预检和父预检均不替代正式宿主验收或独立质量复核；子智能体不是独立审核者。'
          'initialWorkspaceTests只指该次Worker执行副本的初始状态，返工副本可能已包含上轮候选，不能自动当成最初项目基线。')
NOTICE_V2 = NOTICE + (' V2的process字段给出执行前冻结的必做要求和宿主机器核验结果；模型判断不能豁免。'
    '原始失败复现只看process.verification.baseline绑定的原始冻结输入及真实指定标准；'
    'reused表示返工只读继承原证明，不代表又运行了一次原始测试。host_requirement表示宿主按契约自动执行。')


def requirements_fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _contract_version(contract):
    value = contract.get(FIELD)
    if type(value) is not int or value not in SUPPORTED_VERSIONS or not contract.get('workerBinding'):
        raise ValueError('Worker过程复核契约版本无效')
    if value == 2:
        if type(contract.get(PROCESS_FIELD)) is not int or contract[PROCESS_FIELD] != 1 or PROCESS_REQUIREMENTS not in contract:
            raise ValueError('V2 Worker契约缺少冻结的必做过程要求')
        if contract[PROCESS_REQUIREMENTS] != contract['workerBinding'].get('processRequirements'):
            raise ValueError('V2 Worker过程要求与岗位绑定不一致')
    elif PROCESS_FIELD in contract or PROCESS_REQUIREMENTS in contract:
        raise ValueError('必做过程契约不能降级为V1展示')
    return value


def version(run, module=None, contract=None):
    """Cross-check V1/V2 markers; entirely old tasks keep their original view."""
    contracts = list(run.get('taskContracts', {}).values())
    if contract is not None and not any(c is contract for c in contracts):contracts.append(contract)
    bindings = [(n.get('artifactReviewBinding') or {}) for n in run.get('nodes', [])]
    for history in run.get('qualityHistory', []):
        bindings.extend((row.get('reviewEvidence') or {}).get('binding') or {} for row in history.get('modules', []))
    markers = {FIELD,PROCESS_FIELD,PROCESS_REQUIREMENTS,'workerProcessRequirementsFingerprint'}
    marked = any(markers.intersection(item) for item in contracts + bindings)
    if FIELD not in run and PROCESS_FIELD not in run and not marked:return 0
    value = run.get(FIELD)
    if type(value) is not int or value not in SUPPORTED_VERSIONS:
        raise ValueError('Worker过程复核版本缺失或不一致，不能降级为历史输入')
    if value == 2:
        if type(run.get(PROCESS_FIELD)) is not int or run[PROCESS_FIELD] != 1:
            raise ValueError('V2 Worker复核缺少必做过程版本')
    elif PROCESS_FIELD in run or any(markers.difference({FIELD}).intersection(item) for item in contracts+bindings):
        raise ValueError('必做过程版本不能降级为V1复核')
    for item in contracts:
        if item.get('workerBinding'):
            if _contract_version(item) != value:
                raise ValueError('Worker契约缺少一致的过程复核版本')
        elif markers.intersection(item):
            raise ValueError('普通模块不能声明Worker过程证据')
    for item in bindings:
        bound = next((c for c in contracts if c.get('id') == item.get('contractId')), None)
        if bound and bound.get('workerBinding'):
            if type(item.get(FIELD)) is not int or item[FIELD] != value:
                raise ValueError('Worker复核输入丢失过程证据版本')
            if value == 2 and (type(item.get(PROCESS_FIELD)) is not int or item[PROCESS_FIELD] != 1
                    or item.get('workerProcessRequirementsFingerprint') != requirements_fingerprint(bound[PROCESS_REQUIREMENTS])):
                raise ValueError('Worker复核未绑定原必做过程要求')
        if markers.intersection(item) and (not bound or not bound.get('workerBinding')):
            raise ValueError('Worker复核绑定的版本或模块身份不符')
    if module is not None and contract is not None:
        wants = 'workspace' in module.get('requiredCapabilities', [])
        if wants != bool(contract.get('workerBinding')):
            raise ValueError('Worker过程复核模块与契约不一致')
    return value


def notice(view_version):
    if view_version == 1:return NOTICE
    if view_version == 2:return NOTICE_V2
    raise ValueError('Unsupported Worker review view')


def report_path(run, node):
    return PREFIX + run['id'] + '/' + node['id'] + '.json'


def _pick(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def _test(record):
    receipt = record.get('receipt') or {}
    result = _pick(record, ('checkId','status','passed','testRunId','workspaceFingerprint','revision','receiptSha256'))
    result.update(_pick(receipt, ('executed','exitCode','workspaceUnchanged','scriptSha256')))
    result['receiptRef'] = receipt.get('receiptRef', record.get('receiptRef'))
    result['criteria'] = [_pick(c, ('criterionId','status')) for c in receipt.get('criteria', [])]
    return result


def _step(raw, sequence, ref=None, sha=None):
    action = raw.get('action') or {}
    name = raw.get('name') or action.get('name')
    value = raw.get('result') or {}
    # File contents, search queries/matches, model summaries and error text are
    # deliberately absent. inspect_child hashes only the bytes actually returned.
    arguments = _pick(action.get('arguments') or {}, ('path','startLine','endLine','offset','limit','maxResults',
        'checkId','childId','expectedFingerprint','expectedSha256','mode','scope','checkIds'))
    result = _pick(value, ('status','path','sha256','bytes','lineCount','startLine','endLine','truncated','readable','writable',
        'changed','revision','workspaceFingerprint','childId','proposalFingerprint','beforeFingerprint','afterFingerprint',
        'currentWorkspaceFingerprint','paths','id','mode','model','callCount'))
    if name == 'test':result['test'] = _test(value)
    if name == 'inspect_child' and isinstance(value.get('content'), str):
        data = value['content'].encode()
        result.update(returnedContentBytes=len(data), returnedContentSha256=hashlib.sha256(data).hexdigest())
    status = raw.get('status')
    if status is None:status = 'error' if value.get('status') == 'error' else 'completed'
    row = {'sequence':sequence,'name':name,'status':status,'arguments':arguments,'result':result,
           **_pick(raw, ('id','beforeFingerprint','afterFingerprint','endedAt','time','automatic'))}
    if ref is not None:row.update(rawRef=ref,rawSha256=sha)
    return row


def _bounded(rows, limit):
    if len(rows) <= limit:return rows, False
    half = limit // 2
    return rows[:half] + rows[-(limit-half):], True


def _redact(studio, value):
    if isinstance(value, str):
        value = studio.client.clean(value)
        return re.sub(r'github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+', '[redacted]', value)
    if isinstance(value, dict):return {key:_redact(studio,item) for key,item in value.items()}
    if isinstance(value, list):return [_redact(studio,item) for item in value]
    return value


def integration_paths(shared):
    """Validate the host's module-to-report map, never permit a path by prefix."""
    contracts = shared.get('taskContracts', {})
    outputs = shared.get('modules', {})
    for contract in contracts.values():
        if FIELD in contract:_contract_version(contract)
        elif PROCESS_FIELD in contract or PROCESS_REQUIREMENTS in contract:raise ValueError('必做过程契约缺少复核版本')
    expected = {mid for mid,contract in contracts.items() if contract.get(FIELD) in SUPPORTED_VERSIONS and contract.get('workerBinding')}
    paths = shared.get('workerExecutionEvidencePathsByModule', {})
    reports = shared.get('workerExecutionReports', [])
    if not isinstance(paths,dict) or set(paths) != expected or not isinstance(reports,list):
        raise ValueError('Worker宿主过程报告索引与冻结模块不一致')
    by_path = {}
    for report in reports:
        if (not isinstance(report,dict) or not isinstance(report.get('path'),str)
                or not isinstance(report.get('content'),str) or report['path'] in by_path):
            raise ValueError('Worker宿主过程报告格式或路径重复')
        by_path[report['path']] = report
    used = set()
    for mid in expected:
        contract=contracts[mid];proof=(outputs.get(mid) or {}).get('workerEvidence') or {}
        path=PREFIX+contract['runId']+'/'+str(proof.get('nodeId'))+'.json'
        if paths[mid] != [path] or path not in by_path:
            raise ValueError('Worker过程证据必须对应实际作者节点，不能跨模块引用')
        body=json.loads(by_path[path]['content'])
        if not isinstance(body,dict) or not isinstance(body.get('source'),dict):raise ValueError('Worker过程报告内容无效')
        source=body['source']
        view_version=_contract_version(contract)
        if (body.get('schema') != 'yuki-worker-review-evidence/v'+str(view_version) or source.get('moduleId') != mid
                or source.get('nodeId') != proof.get('nodeId') or source.get('model') != contract.get('model')
                or source.get('roleId') != contract.get('roleId') or source.get('scope') != contract.get('scope')
                or source.get('contractFingerprint') != contract.get('fingerprint')
                or source.get('receiptRef') != proof.get('receiptRef') or source.get('receiptSha256') != proof.get('receiptSha256')
                or source.get('workspaceFingerprint') != proof.get('workspaceFingerprint')):
            raise ValueError('Worker过程报告的模块、契约或原始回执归属不一致')
        if view_version == 2:
            process=body.get('process')
            if not isinstance(process,dict) or process.get('version') != 1 or type(process.get('version')) is not int or process.get('requirements') != contract[PROCESS_REQUIREMENTS]:
                raise ValueError('Worker过程报告未绑定冻结的必做要求')
            verification=process.get('verification')
            if (type(proof.get(PROCESS_FIELD)) is not int or proof[PROCESS_FIELD] != 1 or 'processEvidence' not in proof
                    or verification != proof['processEvidence']
                    or source.get('processEvidenceFingerprint') != requirements_fingerprint(verification)):
                raise ValueError('Worker必做过程报告与作者的宿主过程回执不一致')
            if contract[PROCESS_REQUIREMENTS] is None and verification is not None:
                raise ValueError('未要求的过程不能补造为已通过')
            if contract[PROCESS_REQUIREMENTS] is not None and (not isinstance(verification,dict) or verification.get('passed') is not True
                    or verification.get('requirementsFingerprint') != requirements_fingerprint(contract[PROCESS_REQUIREMENTS])):
                raise ValueError('Worker必做过程报告缺少宿主通过证明')
        used.add(path)
    if used != set(by_path):raise ValueError('存在未归属的Worker过程报告')
    return deepcopy(paths)


def _verify_test_original(studio, session, root, checks, record):
    """Validate even superseded failed tests against their own saved artifact copy."""
    from app import safe_path
    from worker_execution import _raw
    from host_acceptance import verify_receipt
    receipt = record.get('receipt') or {}
    check = next((c for c in checks if c['id'] == record.get('checkId')), None)
    if check is None or not receipt.get('receiptRef'):
        raise ValueError('Worker过程测试缺少冻结检查或原始回执')
    path = safe_path(root, receipt['receiptRef'])
    raw = _raw(path)
    if hashlib.sha256(raw).hexdigest() != record.get('receiptSha256') or json.loads(raw) != receipt:
        raise ValueError('Worker历史测试回执已变化，不能引用旧摘要')
    files = {item['path']:_raw(safe_path(path.parent/'workspace',item['path']))
             for item in receipt.get('artifactManifest', [])}
    if not verify_receipt(studio, session, {'id':record.get('testRunId')}, check, files, record.get('revision'), receipt):
        raise ValueError('Worker历史测试的脚本、日志或候选原件无法核验')


def evidence_files(studio, session, run, module, output):
    contract = run.get('taskContracts', {}).get(module['id'])
    view_version=version(run,module,contract)
    if not view_version or 'workspace' not in module.get('requiredCapabilities', []):return []
    if contract is None:raise ValueError('Worker过程证据缺少冻结契约')
    from worker_execution import verify_worker_result, _paths, _load_journal, _load, _fingerprint
    from app import safe_path
    proof = output.get('workerEvidence') or {}
    node = next((n for n in run['nodes'] if n['id'] == proof.get('nodeId')), None)
    if not node or node.get('phase') != 'build' or node.get('status') != 'done' or node.get('result') != output:
        raise ValueError('Worker过程证据没有对应已完成的作者节点')
    verify_worker_result(studio, session, run, node, {'module':module,'executionContract':contract}, output)
    root, job, _ = _paths(studio,session,run,node)
    journal = _load_journal(job/'receipt.json')
    checks = journal['binding']['module']['workerBinding']['checks']
    originals = [_load(safe_path(root,row['rawRef'])) for row in journal['steps']]
    for item in originals:
        if item['name'] == 'test':_verify_test_original(studio,session,root,checks,item['result'])
    steps = [_step(originals[i], i+1, row['rawRef'], row['rawSha256']) for i,row in enumerate(journal['steps'])]
    initial = (journal.get('initialWorkspace') or {}).get('workspaceFingerprint')
    children = []
    for child in journal.get('subagents', []):
        for item in child.get('tools', []):
            if item['action']['name'] == 'test':_verify_test_original(studio,session,root,checks,item['result'])
        child_steps = [_step(t,i+1) for i,t in enumerate(child.get('tools', []))]
        shown,truncated = _bounded(child_steps,MAX_CHILD_STEPS)
        row = _pick(child, ('id','mode','executionVersion','status','model','depth','parentNodeId','scope','checkIds',
              'workspaceFingerprint','finalWorkspaceFingerprint','callCount','rawRef','rawSha256','proposalFingerprint'))
        row.setdefault('mode','read')
        row.update(steps=shown,stepsTotal=len(child_steps),stepsTruncated=truncated,
                   finalTests=[_test(r) for _,r in sorted(child.get('tests',{}).items())],
                   changes=[_pick(r, ('path','beforeSha256','afterSha256')) for r in child.get('changes',[])])
        children.append(row)
    shown,truncated = _bounded(steps,MAX_PARENT_STEPS)
    counts = Counter(row['name'] for row in steps if row['status']=='completed' and row['result'].get('status')!='error')
    report = {'schema':'yuki-worker-review-evidence/v1','notice':NOTICE,
        'source':{'moduleId':module['id'],'nodeId':node['id'],'roleId':module['roleId'],'model':node['agent'],
                  'scope':list(module['scope']),'contractFingerprint':contract['fingerprint'],
                  'receiptRef':proof['receiptRef'],'receiptSha256':proof['receiptSha256'],
                  'workspaceFingerprint':journal['workspaceFingerprint'],
                  'initialWorkspaceFingerprint':initial,'requestProfileFingerprint':journal['binding']['requestProfileFingerprint'],
                  'modelCallsFingerprint':_fingerprint(journal['modelCalls'])},
        'parent':{'initialWorkspaceFingerprint':initial,'steps':shown,'stepsTotal':len(steps),'stepsTruncated':truncated,
                  'finalTests':[_test(r) for _,r in sorted(journal['tests'].items())],
                  'adoptions':[_pick(r,('status','childId','proposalFingerprint','beforeFingerprint','afterFingerprint','paths','revision')) for r in journal.get('childAdoptions',[])]},
        'children':children,
        'facts':{'completedToolCounts':dict(sorted(counts.items())),
                 'initialWorkspaceTests':[row['result']['test'] for row in steps if row['name']=='test'
                     and row['status']=='completed' and row['result'].get('test',{}).get('workspaceFingerprint')==initial]}}
    if view_version == 2:
        from worker_workspace import WorkerWorkspace
        from worker_process import verify as verify_process
        workspace=WorkerWorkspace(studio,session,run,node,{'module':module,'executionContract':contract},module['workerBinding'])
        process=verify_process(studio,session,run,node,workspace,journal,root,final=True)
        report.update(schema='yuki-worker-review-evidence/v2',notice=NOTICE_V2,
                      process={'version':1,'requirements':deepcopy(contract[PROCESS_REQUIREMENTS]),'verification':deepcopy(process)})
        if type(proof.get(PROCESS_FIELD)) is not int or proof[PROCESS_FIELD] != 1 or 'processEvidence' not in proof or proof['processEvidence'] != process:
            raise ValueError('Worker输出与重新核验的必做过程证明不符')
        report['source']['processEvidenceFingerprint']=requirements_fingerprint(process)
        # These fields are absent from V1 projection, whose bytes stay stable.
        by_id={row.get('id'):row for row in originals}
        for row in report['parent']['steps']:
            original=by_id.get(row.get('id'),{})
            row.update(_pick(original,('initiator','purpose')))
    # No artifact can impersonate the host evidence namespace.
    path = report_path(run,node)
    if any(item['path'] == path for n in run['nodes'] for item in (n.get('result') or {}).get('files',[])):
        raise ValueError('交付文件与Worker宿主过程证据路径冲突')
    content = json.dumps(_redact(studio,report),ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    if len(content.encode()) > MAX_VIEW_BYTES:
        raise ValueError('Worker过程证据视图超过上限；不能静默省略必要事实后继续复核')
    return [{'path':path,'content':content}]
