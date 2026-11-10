"""Host-authored direct-task plans; no model judgement or execution authority."""
import copy
import hashlib
import json

from app import safe_path, stamp
from live import Cancelled


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def is_direct(run):
    nodes = [n for n in run.get('nodes', []) if n.get('executorId') == 'task_router']
    contracts = [c for c in run.get('taskContracts', {}).values() if c.get('taskRoutingVersion')]
    proof = run.get('taskRoutingProof')
    version = run.get('taskRoutingVersion')
    route = run.get('taskRouting')
    if version is None and not (nodes or contracts or proof or route):
        return False
    if type(version) is not int or version != 1 or not isinstance(route, dict) or route.get('mode') not in ('team', 'direct'):
        raise ValueError('任务路由版本缺失或不一致，不能将明确合同降级为旧规划')
    direct = route['mode'] == 'direct'
    if not direct and (nodes or contracts or proof):
        raise ValueError('已固定的单Worker任务不能在恢复时改为小组重新规划')
    return direct


def _inputs(run):
    inputs = {'route': copy.deepcopy(run['taskRouting']), 'request': run['request'],
              'projectRevision': run['projectRevision'], 'projectSnapshot': copy.deepcopy(run['projectSnapshot'])}
    if run.get('triggerContinuation'):
        inputs['triggerContinuation'] = copy.deepcopy(run['triggerContinuation'])
    return inputs


def verify_route(studio, session, run):
    if not is_direct(run):
        return
    from trigger_continuation import validate_continuation
    validate_continuation(studio, session, run)
    proof = run.get('taskRoutingProof')
    if not isinstance(proof, dict):
        raise ValueError('明确任务缺少宿主路由原件')
    root = studio.project_dir(session['projectId']) / session['id']
    path = safe_path(root, proof.get('ref'))
    if path.is_symlink() or not path.is_file():
        raise ValueError('宿主任务路由原件缺失')
    raw = path.read_bytes(); record = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != proof.get('sha256')
            or record.get('schema') != 'yuki-task-route/v1' or record.get('runId') != run['id']
            or record.get('inputs') != _inputs(run) or proof.get('inputFingerprint') != _fingerprint(_inputs(run))):
        raise ValueError('明确任务的输入、范围或宿主路由原件已变化')
    node = next((n for n in run['nodes'] if n['id'] == 'draft'), None)
    if (not node or node.get('executorId') != 'task_router' or node.get('status') != 'done'
            or node.get('phase') != 'routing' or node.get('agent') != 'host'
            or node.get('result') != record.get('plan')):
        raise ValueError('明确任务的宿主路由节点不一致')
    plan = run.get('plan')
    if plan is not None:
        original = record['plan']['modules']
        if len(plan['modules']) != 1 or len(original) != 1:
            raise ValueError('明确独立任务不能追加未授权模块')
        keys = ('id', 'roleId', 'roleType', 'workType', 'instructions', 'scope', 'acceptance',
                'skills', 'requiredCapabilities', 'complexity', 'dependsOn', 'reviewRequirements', 'processRequirements')
        if any(plan['modules'][0].get(k) != original[0].get(k) for k in keys):
            raise ValueError('单Worker计划改变了用户固定的任务合同')
    for contract in run.get('taskContracts', {}).values():
        if contract.get('taskRoutingVersion') != 1 or contract.get('taskRoutingFingerprint') != proof['inputFingerprint']:
            raise ValueError('执行合同未绑定原单Worker任务路由')


def direct_proposal(studio, session, run, shared, available, cancel):
    if cancel.is_set():
        raise Cancelled()
    if not is_direct(run):
        raise ValueError('此任务未选择明确独立任务路线')
    if run.get('taskRoutingProof'):
        verify_route(studio, session, run)
        return copy.deepcopy(next(n for n in run['nodes'] if n['id'] == 'draft')['result'])
    if run.get('plan') is not None or any(n['id'] == 'draft' for n in run['nodes']):
        raise ValueError('已开始的任务不能补造单Worker路由记录')
    from task_routing import direct_plan
    from studio_business import validate_business_response, _node
    plan = direct_plan(run)
    validate_business_response({'phase': 'routing'}, plan, {**shared, 'businessStage': 'plan'}, available)
    record = {'schema': 'yuki-task-route/v1', 'runId': run['id'], 'inputs': _inputs(run), 'plan': copy.deepcopy(plan),
              'basis': 'explicit_user_contract', 'notice': '宿主按用户明确任务生成；未请求模型规划。'}
    ref = 'task-routes/' + run['id'] + '.json'
    with studio.lock:
        path = safe_path(studio.project_dir(session['projectId']) / session['id'], ref)
        if path.exists():
            raise ValueError('已有未确认的任务路由原件，拒绝覆盖')
        studio._write(path, record)
        node = _node(studio, run, 'draft', '明确任务接入 · 宿主规则路由', 'host', 'routing', role='router')
        node.update(status='done', started=stamp(), ended=stamp(), result=copy.deepcopy(plan),
                    executorId='task_router', origin='host', responseValidation='passed',
                    receipt={'mode': 'host', 'source': 'explicit_user_contract', 'modelCalls': 0})
        run['taskRoutingProof'] = {'ref': ref, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                   'inputFingerprint': _fingerprint(_inputs(run))}
        studio.event(session, 'node_started', '按用户明确合同选择单Worker路线，不调用规划模型。', 'draft', agent='host')
        studio.event(session, 'node_completed', '宿主规则路由已建立单Worker任务；仍须实际执行和质量验收。', 'draft', agent='host')
        studio.save(session)
    return plan
