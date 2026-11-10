"""Version and quality boundaries for opt-in project workspace workers."""
def run_version(run):
    from worker_review_evidence import version as worker_review_version
    worker_review_version(run)
    modules = (run.get('plan') or {}).get('modules', [])
    contracts = list(run.get('taskContracts', {}).values())
    nodes = run.get('nodes', [])
    marked = any('workspace' in m.get('requiredCapabilities', []) or m.get('workerBinding') for m in modules)
    marked = marked or any('workerToolsVersion' in c or c.get('workerBinding') for c in contracts)
    marked = marked or any(n.get('executorId') == 'workspace' or (n.get('result') or {}).get('workerEvidence') for n in nodes)
    if 'workerToolsVersion' not in run and not marked:
        return 0
    if type(run.get('workerToolsVersion')) is not int or run['workerToolsVersion'] != 1:
        raise ValueError('Worker工具版本缺失或不一致，不能降级为普通生成任务')
    from worker_policy import normalize_policy
    if normalize_policy(run.get('workerTools')) != run.get('workerTools'):
        raise ValueError('Worker岗位授权快照缺失或格式改变')
    for module in modules:
        validate_module(run, module)
    return 1


def validate_module(run, module, contract=None):
    from worker_review_evidence import version as worker_review_version
    worker_review_version(run,module,contract)
    from worker_policy import bind_role
    wants = 'workspace' in module.get('requiredCapabilities', [])
    if not wants:
        if module.get('workerBinding') or contract and contract.get('workerBinding'):
            raise ValueError('普通模块不能携带Worker执行授权')
        return False
    if run.get('workerToolsVersion') != 1:
        raise ValueError('本轮未冻结Worker工具授权')
    expected = bind_role(module, run['workerTools'], run.get('hostAcceptanceChecks', []))
    if not expected or module.get('workerBinding') != expected:
        raise ValueError('Worker岗位、范围或测试脚本与冻结授权不一致')
    if contract is not None:
        if contract.get('workerToolsVersion') != 1 or contract.get('workerBinding') != expected:
            raise ValueError('执行合同缺少本轮Worker工具授权，不能事后补写')
        if contract.get('scope') != module['scope']:
            raise ValueError('Worker写入范围与固定合同不同')
        from review_evidence import fingerprint
        body = {k: v for k, v in contract.items() if k not in ('fingerprint', 'created', 'eventSeq')}
        if fingerprint(body) != contract.get('fingerprint'):
            raise ValueError('Worker执行合同指纹已改变')
    from worker_process import requirements
    requirements(run,module,contract)
    return True


def checks(studio, session, run, module, output):
    run_version(run)
    contract = run.get('taskContracts', {}).get(module['id'])
    if not validate_module(run, module, contract):
        if output.get('workerEvidence'):
            raise ValueError('未授权模块不能附加Worker工具证明')
        return []
    if contract is None:
        raise ValueError('Worker缺少执行前冻结的合同')
    evidence = output.get('workerEvidence') or {}
    node = next((n for n in run['nodes'] if n['id'] == evidence.get('nodeId')), None)
    if not node or node.get('moduleId') != module['id'] or node.get('phase') != 'build' or node.get('status') != 'done':
        raise ValueError('Worker产物缺少已完成的真实执行节点')
    from worker_execution import verify_worker_result
    result=verify_worker_result(studio, session, run, node,
                                {'module': module, 'executionContract': contract}, output)
    from worker_process import verification_checks
    return result + verification_checks(studio,session,run,node,module,output)


def verify_delivery(studio, session, run, files):
    """First publication only. Validate original evidence; never run a tool."""
    if not run_version(run):
        return
    for module in (run.get('plan') or {}).get('modules', []):
        if 'workspace' not in module.get('requiredCapabilities', []):
            continue
        history = run.get('qualityHistory', [])
        record = history[-1] if history else None
        if not record or record.get('passed') is not True:
            raise ValueError('Worker交付缺少通过的质量关卡')
        row = next((r for r in record['modules'] if r['moduleId'] == module['id']), None)
        evidence = row.get('workerEvidence') if row else None
        node = next((n for n in run['nodes'] if n['id'] == (evidence or {}).get('nodeId')), None)
        if not node or node.get('result', {}).get('workerEvidence') != evidence:
            raise ValueError('交付引用的Worker原始证据缺失或已变化')
        output = node['result']
        if not all(check['pass'] for check in checks(studio, session, run, module, output)):
            raise ValueError('Worker必要测试未通过，不能合入')
        from worker_review_evidence import evidence_files
        evidence_files(studio,session,run,module,output)  # Revalidate historic process receipts, even without an independent-review node.
        expected = {f['path']: f['content'].encode() for f in output['files']}
        if set(expected) != {f['path'] for f in row['artifactManifest']}:
            raise ValueError('Worker质量记录与实际产物范围不一致')
        for path, raw in expected.items():
            if files.get(path) != raw:
                raise ValueError('当前交付与已测试的Worker产物不同：' + path)


PROMPT = '''
本项目已按岗位登记workerTools。仅被授权的岗位可声明requiredCapabilities:["text","workspace"]。
workspace提供读取冻结项目文件、字面搜索、修改本模块scope内文件、运行已登记固定测试脚本的闭环；不等于任意终端或网络能力。
只允许使用该岗位授权checkIds。测试会在候选副本中执行，失败详情返回同一Worker，修复后必须重新测试；最终还要经过正式宿主验收与独立复核。
workspace暂不能与files/terminal/web_search/browser/plugins/image混在一个模块。仍由当前选定API模型承担岗位，不替换为原生Codex。
不要生成workerBinding或修改脚本授权。计划scope必须覆盖本岗位测试涉及的交付路径；缺少必要脚本或岗位权限则放blockingQuestions。
岗位processRequirements是不可删除的必做流程；有要求的岗位必须使用workspace，不能改成纯文本规避。
明确单Worker合同也可声明processRequirements增强义务，不能弱化岗位默认。initialFailure固定checkId和必须失败的criterionIds；宿主在最初冻结副本上自动检查，未复现即停止，不能先改坏再测。requireExecutionChild要求同型号execute子任务、完整inspect、显式接纳与父重验；模型自述不能替代。'''
