"""Frozen process obligations enforced by the host, not model self-reports.

Baseline intent is per run/module. Unknown checks never replay; repairs inherit
only the original verified baseline. Inspection means delivered bytes, not a
claim that a model understood them. This module never grants extra tools.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json

VERSION = 1

class WorkerProcessIncomplete(ValueError):
    pass


def _fail(message):
    from worker_execution import WorkerExecutionError
    raise WorkerExecutionError(message)


def requirements(run, module, contract):
    binding = (module or {}).get('workerBinding') or {}
    req = binding.get('processRequirements')
    marked = ('workerProcessVersion' in run or req is not None
              or contract and ('workerProcessVersion' in contract or 'workerProcessRequirements' in contract)
              or any('workerProcessVersion' in c or 'workerProcessRequirements' in c for c in run.get('taskContracts', {}).values()))
    if not marked:return None
    if type(run.get('workerProcessVersion')) is not int or run['workerProcessVersion'] != VERSION:
        _fail('Worker必做过程版本缺失，不能降级成旧任务')
    for c in run.get('taskContracts', {}).values():
        if c.get('workerBinding') and (type(c.get('workerProcessVersion')) is not int or c['workerProcessVersion'] != VERSION
                                       or 'workerProcessRequirements' not in c):
            _fail('Worker执行契约丢失必做过程版本')
    if not binding:
        if contract and ('workerProcessVersion' in contract or 'workerProcessRequirements' in contract):
            _fail('普通模块不能声明Worker必做过程')
        return None
    if contract is None:
        if req is not None:
            from worker_process_policy import normalize_requirements
            if normalize_requirements(req) != req:_fail('Worker必做过程格式或版本不符')
        return deepcopy(req)
    if type(contract.get('workerProcessVersion')) is not int or contract['workerProcessVersion'] != VERSION:
        _fail('Worker执行契约缺少必做过程版本')
    if 'workerProcessRequirements' not in contract or contract['workerProcessRequirements'] != req:
        _fail('Worker必做过程与冻结岗位授权不一致')
    if req is not None:
        from worker_process_policy import normalize_requirements
        if normalize_requirements(req) != req:_fail('Worker必做过程格式或版本不符')
        if req['requireExecutionChild'] and binding.get('childExecution') is not True:
            _fail('必做执行子任务未获得岗位授权')
    return deepcopy(req)


def _check_journal(run, journal):
    binding = journal['binding'];req=requirements(run,binding['module'],binding['contract'])
    if 'workerProcessVersion' in binding['contract']:
        if (type(journal.get('workerProcessVersion')) is not int or journal['workerProcessVersion'] != VERSION
                or 'processRequirements' not in journal or journal['processRequirements'] != req):
            _fail('Worker日志丢失冻结必做过程，不能恢复为旧行为')
    elif 'workerProcessVersion' in journal or 'processRequirements' in journal or 'processEvidence' in journal:
        _fail('旧Worker不能事后补写过程义务')
    return req


def _paths(root, run, module):
    from app import safe_path
    relative='worker-process/'+run['id']+'/'+module['id']+'/baseline.json'
    return safe_path(root,relative),relative


def _basis(journal, requirement):
    b=journal['binding']
    return {'projectId':b['projectId'],'sessionId':b['sessionId'],'runId':b['runId'],
            'moduleId':b['module']['id'],'roleId':b['module']['roleId'],'projectRevision':b['projectRevision'],
            'contractFingerprint':b['contract']['fingerprint'],'requirement':deepcopy(requirement)}


def _reproduced(test, requirement):
    receipt=test.get('receipt') or {}
    statuses={row.get('criterionId'):row.get('status') for row in receipt.get('criteria',[])}
    return (test.get('status')=='failed' and test.get('passed') is False and receipt.get('executed') is True
            and all(statuses.get(cid)=='failed' for cid in requirement['criterionIds']))


def _continuation_baseline(studio, session, run, node, journal, root, create=False):
    """Bind the verified original failure proof to a follow-up, without rerunning it."""
    from app import safe_path
    from trigger_continuation import validate_continuation
    from worker_execution import _load, _write, _raw, _hash
    lineage = validate_continuation(studio, session, run)
    if not lineage:
        _fail('订阅续办缺少原任务关联，不能继承失败复现')
    original = lineage['rootRun']
    continuation = run['triggerContinuation']
    if original.get('triggerContinuation') or original['id'] == run['id']:
        _fail('原始失败复现的根任务无效')
    original_node = next((n for n in original['nodes'] if n['id'] == continuation['rootNodeId']), None)
    current_module = journal['binding']['module']
    original_module = next((m for m in original['plan']['modules'] if m['id'] == current_module['id']), None)
    original_contract = original.get('taskContracts', {}).get(current_module['id'])
    if (not original_node or original_node.get('status') != 'done' or not original_module
            or not original_contract or original_contract['fingerprint'] != continuation['rootContractFingerprint']
            or original_contract.get('workerProcessRequirements') != journal['processRequirements']):
        _fail('续办的必做过程与原始契约不一致')
    evidence = (original_node.get('result') or {}).get('workerEvidence') or {}
    receipt = safe_path(root, evidence.get('receiptRef'))
    if _hash(_raw(receipt)) != evidence.get('receiptSha256'):
        _fail('原Worker过程回执已变化，不能继承失败复现')
    proof = verification_summary(studio, session, original, original_node, original_module, original_node['result'])
    if not proof or proof.get('passed') is not True or not proof.get('baseline'):
        _fail('原任务缺少已核验失败复现')
    original_baseline = proof['baseline']
    path, ref = _paths(root, run, current_module)
    expected = {'schema': 'yuki-worker-baseline-continuation/v1', 'basis': _basis(journal, journal['processRequirements']['initialFailure']),
                'rootRunId': original['id'], 'rootNodeId': original_node['id'],
                'rootContractFingerprint': original_contract['fingerprint'],
                'continuation': deepcopy(continuation), 'originalBaseline': original_baseline}
    if not path.exists():
        started = any(e.get('runId') == run['id'] and e.get('node') == node['id']
                      and e.get('type') == 'worker_tool_started' for e in session.get('events', []))
        if not create or started or journal['steps'] or journal['modelCalls'] or node.get('repairRound'):
            _fail('续办原始失败证明缺失，不能在已开始执行后重新建立')
        _write(path, expected)
    elif _load(path) != expected:
        _fail('续办引用的原始失败复现或任务关联已经变化')
    summary = {**deepcopy(original_baseline), 'ref': ref, 'sha256': _hash(_raw(path)),
               'reused': True, 'inheritedFromRunId': original['id'], 'originalBaseline': deepcopy(original_baseline)}
    if journal.get('processBaseline') is not None and journal['processBaseline'] != summary:
        _fail('续办的原始失败复现引用已变化')
    return summary


def _baseline(studio,session,run,node,workspace,journal,root):
    from app import safe_path
    from worker_execution import _load,_raw,_hash,_fingerprint,_load_journal
    from worker_workspace import WorkerWorkspace
    from worker_review_evidence import _verify_test_original
    req=_check_journal(run,journal)
    if not req or req['initialFailure'] is None:return None
    if run.get('triggerContinuation'):
        return _continuation_baseline(studio,session,run,node,journal,root)
    path,ref=_paths(root,run,journal['binding']['module'])
    if not path.exists():_fail('缺少原始失败复现回执，禁止开始执行')
    record=_load(path)
    if record.get('schema')!='yuki-worker-baseline/v1' or record.get('basis')!=_basis(journal,req['initialFailure']):
        _fail('原始失败复现绑定与本轮契约不同')
    if record.get('status')=='not_reproduced':
        _fail('初始检查未复现指定失败标准（可能已通过）；未修改候选，不会重复检查')
    if record.get('status')=='check_error':
        _fail('原始固定检查执行出错或超时，不能当作业务失败复现；不会重放')
    if record.get('status')!='reproduced':
        _fail('原始失败复现结果未确认；不会重放检查或修改候选')
    origin=next((n for n in run['nodes'] if n['id']==record.get('sourceNodeId')),None)
    if not origin or origin.get('phase')!='build' or origin.get('moduleId')!=journal['binding']['module']['id']:
        _fail('原始失败复现作者节点缺失')
    b=journal['binding']
    origin_workspace=WorkerWorkspace(studio,session,run,origin,
        {'module':b['module'],'executionContract':b['contract']},b['module']['workerBinding'])
    origin_workspace.verify_inputs()
    meta=origin_workspace._load(origin_workspace.meta_path)
    if (_hash(_raw(origin_workspace.meta_path))!=record.get('workspaceBindingSha256')
            or _fingerprint(meta['initialManifest'])!=record.get('initialWorkspaceFingerprint')
            or any(r['path'].startswith('previous/') for r in meta['sources'])):
        _fail('基线不是未经修改的原始冻结输入')
    original_journal=_load_journal(safe_path(root,'worker-executions/'+run['id']+'/'+origin['id']+'/receipt.json'))
    rows=original_journal['steps'];matching=next((i for i,r in enumerate(rows) if r['rawRef']==record.get('stepRef')),None)
    if matching is None or any(r['name'] not in ('read','list','search','test') for r in rows[:matching]):
        _fail('原始基线之前已有修改或子任务，不能称为失败复现')
    step_path=safe_path(root,record['stepRef']);raw=_raw(step_path)
    if _hash(raw)!=record.get('stepSha256') or rows[matching]['rawSha256']!=record['stepSha256']:
        _fail('原始失败复现工具原件已变化')
    step=json.loads(raw);test=record['test']
    if (step.get('name')!='test' or step.get('status')!='completed' or step.get('result')!=test
            or step.get('initiator')!='host_requirement' or step.get('purpose')!='initial_failure'
            or test.get('revision')!=0 or test.get('workspaceFingerprint')!=record['initialWorkspaceFingerprint']
            or step.get('beforeFingerprint')!=record['initialWorkspaceFingerprint']
            or step.get('afterFingerprint')!=record['initialWorkspaceFingerprint']
            or not _reproduced(test,req['initialFailure'])):
        _fail('原始失败复现没有对应未修改副本上的指定失败标准')
    _verify_test_original(studio,session,root,b['module']['workerBinding']['checks'],test)
    frozen=journal.get('processBaseline')
    summary={'status':'reproduced','ref':ref,'sha256':_hash(_raw(path)),'sourceNodeId':origin['id'],
             'projectRevision':run['projectRevision'],'initialWorkspaceFingerprint':record['initialWorkspaceFingerprint'],
             'checkId':test['checkId'],'criterionIds':deepcopy(req['initialFailure']['criterionIds']),
             'actualFailedCriterionIds':[c['criterionId'] for c in test['receipt']['criteria'] if c['status']=='failed'],
             'testRunId':test['testRunId'],'receiptRef':test['receipt']['receiptRef'],
             'receiptSha256':test['receiptSha256'],'initiator':'host_requirement','reused':origin['id']!=node['id']}
    if frozen is not None and frozen!=summary:_fail('Worker基线引用或原件已变化')
    return summary


def ensure_baseline(studio,session,run,node,workspace,journal,root,tool,save,cancel):
    from worker_execution import _load,_write,_hash,_raw,_fingerprint
    from service_lock import ServiceDataLock
    req=_check_journal(run,journal)
    if not req or req['initialFailure'] is None:return
    path,ref=_paths(root,run,journal['binding']['module'])
    workspace._safe(path.parent)
    with ServiceDataLock(path.parent):
        if run.get('triggerContinuation'):
            summary=_continuation_baseline(studio,session,run,node,journal,root,create=True)
        elif path.exists():
            summary=_baseline(studio,session,run,node,workspace,journal,root)
        else:
            meta=workspace._load(workspace.meta_path)
            started_before=any(e.get('type')=='worker_tool_started' and e.get('node')==node['id'] and e.get('runId')==run['id'] for e in session.get('events',[]))
            if (started_before or journal['steps'] or journal['modelCalls'] or node.get('repairRound',0)
                    or node['id'].startswith('repair-') or workspace.shared.get('repair')
                    or any(r['path'].startswith('previous/') for r in meta['sources'])
                    or workspace._view()['revision']!=0
                    or workspace.fingerprint()!=_fingerprint(meta['initialManifest'])):
                _fail('缺少原始基线；不得在已修改或返工副本上补跑失败复现')
            if cancel.is_set():
                from live import Cancelled
                raise Cancelled()
            record={'schema':'yuki-worker-baseline/v1','status':'pending','basis':_basis(journal,req['initialFailure']),
                    'sourceNodeId':node['id'],'initialWorkspaceFingerprint':workspace.fingerprint(),
                    'workspaceBindingSha256':_hash(_raw(workspace.meta_path))}
            _write(path,record)  # Must precede any checker execution; unknown intent never replays.
            try:
                test=tool({'name':'test','arguments':{'checkId':req['initialFailure']['checkId']}},purpose='initial_failure')
            except BaseException:
                # A known checker protocol/error result is not a reproduced
                # assertion failure. A lost successful/failed reply stays pending.
                state=workspace._load(workspace.state_path)
                saved=state.get('tests',{}).get('0:'+req['initialFailure']['checkId'])
                if saved and saved.get('status') in ('error','timeout') and workspace.verify_test(saved):
                    record.update(status='check_error',test=deepcopy(saved));_write(path,record)
                raise
            step=journal['steps'][-1]
            record.update(status='reproduced' if _reproduced(test,req['initialFailure']) else 'not_reproduced',
                          stepRef=step['rawRef'],stepSha256=step['rawSha256'],test=deepcopy(test))
            _write(path,record)
            summary=_baseline(studio,session,run,node,workspace,journal,root)
        journal['processBaseline']=summary;save()
        # The source proof is read-only background on repairs, never a synthetic model call.
        if summary['reused']:
            message={'role':'user','content':'宿主已只读核验本轮原始冻结输入的失败复现回执；返工沿用该原件，不在修复候选上伪复现。'+json.dumps(summary,ensure_ascii=False)}
            if message not in journal['baseMessages']:journal['baseMessages'].append(message);save()


def _steps(journal,root):
    from app import safe_path
    from worker_execution import _raw,_hash
    result=[]
    for row in journal['steps']:
        raw=_raw(safe_path(root,row['rawRef']))
        if _hash(raw)!=row['rawSha256']:_fail('必做过程工具原件已变化')
        step=json.loads(raw);step['rawRef']=row['rawRef'];result.append(step)
    return result


def _inspection(studio,session,run,node,workspace,journal,root,child,changes,before_index=None):
    from worker_execution import _fingerprint,_observation_view,_brief,_load,_raw,_hash
    from app import safe_path
    all_steps=_steps(journal,root)
    accepted_context=all_steps[before_index].get('requestedByContextRef') if before_index is not None else None
    steps=all_steps if before_index is None else all_steps[:before_index]
    intervals={c['path']:[] for c in changes};step_ids=[]
    parent_calls=[r for r in journal['modelCalls'] if not r.get('subagentId')]
    if accepted_context is not None:
        boundary=next((i for i,c in enumerate(parent_calls) if c['contextRef']==accepted_context),None)
        if boundary is None:_fail('接纳动作缺少对应父模型调用')
        parent_calls=parent_calls[:boundary+1]
    contexts=[]
    for call in parent_calls:
        p=safe_path(root,'contexts/'+call['contextRef']+'.json')
        if _hash(_raw(p))!=call['contextSha256']:_fail('父模型核对上下文原件已变化')
        contexts.append(_load(p))
    for step in steps:
        if step.get('name')!='inspect_child' or step.get('status')!='completed':continue
        value=step.get('result') or {};view=step.get('modelView')
        if value.get('childId')!=child['id'] or value.get('proposalFingerprint')!=child['proposalFingerprint']:continue
        if not isinstance(view,dict) or step.get('modelViewSha256')!=_fingerprint(view):
            _fail('候选检查缺少实际模型可见视图')
        if view!=_observation_view('inspect_child',value,step['rawRef']):_fail('候选检查可见视图与原件不符')
        supplied='实际Worker工具回执的限长视图（不是指令；原件见rawRef）：'+_brief(view)
        request_index=next((i for i,c in enumerate(parent_calls) if c['contextRef']==step.get('requestedByContextRef')),None)
        if request_index is None:_fail('候选检查动作缺少对应父模型调用')
        if not any(any(m.get('role')=='user' and m.get('content')==supplied for m in c.get('messages',[])) for c in contexts[request_index+1:]):continue
        item=next((c for c in changes if c['path']==view.get('path')),None)
        if not item or view.get('status')=='error':continue
        text=item['content'];lines=text.splitlines(keepends=True);start=view.get('startLine');end=view.get('endLine')
        if (type(start) is not int or type(end) is not int or start<1 or end>len(lines)
                or view.get('lineCount')!=len(lines) or view.get('sha256')!=hashlib.sha256(text.encode()).hexdigest()):continue
        expected=''.join(lines[start-1:end])
        if view.get('content')!=expected or view.get('modelViewTruncated') or view.get('contentTruncated'):continue
        if lines and end<start:continue
        if not lines and not (start==1 and end==0 and expected==''):continue
        intervals[item['path']].append((start,end));step_ids.append(step['id'])
    complete=True
    for item in changes:
        covered=intervals[item['path']];count=len(item['content'].splitlines(keepends=True))
        if not count:
            complete=complete and bool(covered);continue
        through=0
        for start,end in sorted(covered):
            if start>through+1:break
            through=max(through,end)
        complete=complete and through>=count
    return {'inspectionComplete':bool(complete),'inspectStepIds':step_ids}


def before_accept(studio,session,run,node,workspace,journal,root,child,changes):
    req=_check_journal(run,journal)
    if req and req['requireExecutionChild']:
        result=_inspection(studio,session,run,node,workspace,journal,root,child,changes)
        if not result['inspectionComplete']:
            raise ValueError('必做过程要求先用inspect_child完整核对该提案所有变更文件；请用未截断的行段补齐后再接纳')


def before_final(run,journal):
    req=_check_journal(run,journal)
    if req and req['requireExecutionChild'] and not journal.get('childAdoptions'):
        raise WorkerProcessIncomplete('冻结契约要求先执行子任务、完整inspect_child并accept_child，再进入父级最终重验')


def verify(studio,session,run,node,workspace,journal,root,final=False):
    from worker_execution import _fingerprint
    from worker_subagents import verify_execution_child
    req=_check_journal(run,journal)
    if not req:return None
    baseline=_baseline(studio,session,run,node,workspace,journal,root)
    result={'schema':'yuki-worker-process-evidence/v1','requirementsFingerprint':_fingerprint(req),
            'baseline':baseline,'children':[],'passed':True}
    if req['requireExecutionChild'] and final:
        steps=_steps(journal,root);last_adoption=-1
        accepted=[]
        for i,step in enumerate(steps):
            value=step.get('result') or {}
            if step.get('name')=='accept_child' and step.get('status')=='completed' and value.get('status')=='accepted':
                child=next((c for c in journal['subagents'] if c['id']==value.get('childId')),None)
                if not child or child.get('mode')!='execute' or child.get('status')!='completed':_fail('必做执行子任务未实际完成')
                changes=verify_execution_child(workspace,child,root)
                if not changes or not value.get('paths') or value.get('proposalFingerprint')!=child['proposalFingerprint']:
                    _fail('必做子任务没有实际变更或正确接纳证明')
                inspected=_inspection(studio,session,run,node,workspace,journal,root,child,changes,i)
                if not inspected['inspectionComplete']:_fail('已接纳子任务缺少完整的先行候选检查')
                accepted.append((child,step,inspected));last_adoption=i
        if not accepted:raise WorkerProcessIncomplete('冻结契约要求实际执行、完整检查并接纳至少一个子任务，尚未满足')
        tests=[]
        for check in journal['binding']['module']['workerBinding']['checks']:
            current=journal['tests'].get(check['id'])
            if not current or current.get('passed') is not True or not workspace.verify_test(current):
                _fail('接纳后的父Worker固定检查未通过或原件不符')
            found=next((step for step in steps[last_adoption+1:] if step.get('name')=='test' and step.get('status')=='completed'
                        and step['result']==current),None)
            if found is None:_fail('缺少最后接纳之后的父Worker真实重验')
            tests.append(found['id'])
        for child,adoption,inspected in accepted:
            if any(t.get('testRunId')==workspace.test_run_id for t in child.get('tests',{}).values()):
                _fail('子测试不能冒充父Worker重验')
            result['children'].append({'childId':child['id'],'proposalFingerprint':child['proposalFingerprint'],
                **inspected,'adopted':True,'adoptStepId':adoption['id'],'parentRetested':True,'parentTestStepIds':tests})
    if final and journal.get('processEvidence') is not None and journal['processEvidence']!=result:
        _fail('已保存的必做过程证明与原始记录不一致')
    return result


def verification_summary(studio,session,run,node,module,output):
    """Read-only adapter for quality/evidence callers; never prepares a copy."""
    from worker_execution import _binding,_paths,_load_journal,_check_originals
    from worker_workspace import WorkerWorkspace
    contract=run.get('taskContracts',{}).get(module['id'])
    req=requirements(run,module,contract)
    if not req:return None
    shared={'module':module,'executionContract':contract}
    binding=_binding(session,run,node,shared);root,job,_=_paths(studio,session,run,node)
    journal=_load_journal(job/'receipt.json');_check_originals(root,journal)
    if journal['binding']!=binding:_fail('必做过程绑定与作者不符')
    workspace=WorkerWorkspace(studio,session,run,node,shared,module['workerBinding'])
    workspace.verify_inputs()
    return verify(studio,session,run,node,workspace,journal,root,final=True)


def verification_checks(studio,session,run,node,module,output):
    summary=verification_summary(studio,session,run,node,module,output)
    if summary is None:return []
    return [{'type':'worker_process','name':'冻结必做过程已按真实原件核验','pass':True,'status':'passed',
             'basis':'host_verified_process_requirements','requirementsFingerprint':summary['requirementsFingerprint'],
             'baseline':summary['baseline'],'children':summary['children']}]
