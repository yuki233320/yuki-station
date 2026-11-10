"""Explicit project-role grants for bounded workspace workers.

No configured grant means no worker. Model choice does not add permissions;
scripts must already be selected for this project and remain locally approved.
"""
import copy
import hashlib
import json
import re


VERSION = 1
CAPABILITIES = ['read', 'search', 'write', 'run_check']
ROLE_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}')
CHECK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')


def _forbidden(role):
    return role in ('coordinator', 'reviewer') or role.startswith('reviewer-')


def _role(role):
    if not isinstance(role, str) or not ROLE_ID.fullmatch(role) or _forbidden(role):
        raise ValueError('Worker工具只授权执行岗位，不能授权主负责人或复核者')
    return role


def normalize_policy(raw):
    if raw is None:
        return {'version': VERSION, 'roleGrants': {}}
    if (not isinstance(raw, dict) or set(raw) - {'version', 'roleGrants'}
            or type(raw.get('version')) is not int or raw['version'] != VERSION):
        raise ValueError('Worker岗位授权版本或字段无效')
    grants = raw.get('roleGrants', {})
    if not isinstance(grants, dict) or len(grants) > 64:
        raise ValueError('Worker岗位授权须为最多64个岗位的映射')
    result = {}
    for role, grant in grants.items():
        _role(role)
        if not isinstance(grant, dict) or set(grant) - {'enabled', 'checkIds', 'childExecution', 'processRequirements'}:
            raise ValueError('Worker岗位授权字段无效')
        enabled, ids = grant.get('enabled', False), grant.get('checkIds', [])
        if type(enabled) is not bool:
            raise ValueError('Worker岗位开关须为布尔值')
        if (not isinstance(ids, list) or len(ids) > 8
                or any(not isinstance(cid, str) or not CHECK_ID.fullmatch(cid) for cid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Worker固定脚本须为最多8个不重复的登记编号')
        result[role] = {'enabled': enabled, 'checkIds': list(ids)}
        # Absence stays absent: old frozen grants and their hashes retain their meaning.
        if 'childExecution' in grant:
            if type(grant['childExecution']) is not bool:
                raise ValueError('子智能体执行开关须为布尔值')
            result[role]['childExecution'] = grant['childExecution']
        if 'processRequirements' in grant:
            from worker_process_policy import normalize_requirements
            requirement=normalize_requirements(grant['processRequirements'])
            if requirement is None:raise ValueError('未启用必做过程时应省略processRequirements')
            result[role]['processRequirements']=requirement
    return {'version': VERSION, 'roleGrants': result}


def _check_directory(checks):
    from host_acceptance import _check
    if not isinstance(checks, list):
        raise ValueError('Worker脚本须来自本项目冻结的宿主验收列表')
    directory = {}
    for item in checks:
        check = _check(item)
        if check['id'] in directory:
            raise ValueError('项目宿主验收脚本编号重复')
        directory[check['id']] = check
    return directory


def validate_policy_checks(policy, checks):
    normalized = normalize_policy(policy)
    directory = _check_directory(checks)
    for grant in normalized['roleGrants'].values():
        missing = set(grant['checkIds']) - set(directory)
        if missing:
            raise ValueError('Worker脚本不在项目已选择的宿主验收范围：' + '、'.join(sorted(missing)))
        if grant.get('processRequirements'):
            from worker_process_policy import validate_requirements
            if not grant['enabled'] or not grant['checkIds']:
                raise ValueError('必做过程要求须绑定已启用的Worker岗位和固定检查')
            validate_requirements(grant['processRequirements'],[directory[cid] for cid in grant['checkIds']],grant.get('childExecution'))


def _source_role(module, policy):
    role = _role(module.get('roleId'))
    if role in policy['roleGrants']:
        return role
    # roleDefinition is copied from the user-owned role directory by the host.
    # A custom role cannot replace that base with a model-written roleType.
    if role.startswith('custom-'):
        base = (module.get('roleDefinition') or {}).get('baseRoleType')
    else:
        base = module.get('roleType')
    from project_team import ROLE_SPECS
    return base if base in ROLE_SPECS and not _forbidden(base) and base in policy['roleGrants'] else None


def bind_role(module, policy, checks, registry=None):
    policy = normalize_policy(policy)
    validate_policy_checks(policy, checks)
    source = _source_role(module, policy)
    grant = policy['roleGrants'].get(source)
    # An explicit empty grant blocks inheritance and grants no file operations.
    if not grant or not grant['enabled'] or not grant['checkIds']:
        return None
    from studio import clean_path, file_allowed
    scopes = module.get('scope')
    if (not isinstance(scopes, list) or not scopes or any(not isinstance(scope, str) for scope in scopes)
            or len(set(scopes)) != len(scopes)):
        raise ValueError('Worker模块必须声明不重复的写入范围')
    for scope in scopes:
        clean_path(scope.rstrip('/'))
        if scope.rstrip('/') == 'inputs' or scope.startswith('inputs/'):
            raise ValueError('Worker不能将只读输入目录声明为写入范围')
    directory = _check_directory(checks)
    selected = [copy.deepcopy(directory[cid]) for cid in grant['checkIds']]
    for check in selected:
        if any(not file_allowed(path, scopes) for path in check['files']):
            raise ValueError('Worker模块范围必须覆盖已授权脚本的全部交付文件：' + check['id'])
        if registry is not None:
            registry.authorize(check)
    binding = {'schema': 'yuki-worker-binding/v1', 'enabled': True,
               'roleId': module['roleId'], 'sourceRoleId': source,
               'scope': list(scopes), 'checkIds': list(grant['checkIds']),
               'checks': selected, 'capabilities': list(CAPABILITIES)}
    if grant.get('childExecution') is True:
        binding['childExecution'] = True
    from worker_process_policy import merge_requirements, validate_requirements
    requirements=merge_requirements(grant.get('processRequirements'),module.get('processRequirements'))
    requirements=validate_requirements(requirements,selected,grant.get('childExecution'))
    if requirements is not None:binding['processRequirements']=requirements
    binding['fingerprint'] = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True,
                                                     separators=(',', ':')).encode()).hexdigest()
    return binding


def require_workspace_process(module, policy):
    """A planner cannot omit workspace to evade a selected role's obligations."""
    policy=normalize_policy(policy)
    if module.get('processRequirements') is None and not any(g.get('processRequirements') for g in policy['roleGrants'].values()):
        return
    source=_source_role(module,policy)
    grant=policy['roleGrants'].get(source) or {}
    from worker_process_policy import normalize_requirements
    declared=normalize_requirements(module.get('processRequirements'))
    required=declared or (grant.get('processRequirements') if grant.get('enabled') else None)
    if required and 'workspace' not in module.get('requiredCapabilities',[]):
        raise ValueError('该任务或岗位已声明必做Worker过程，不能省略workspace规避')


def validate_plan_bindings(plan, policy, checks, registry=None):
    validate_policy_checks(policy, checks)
    modules = plan.get('modules', [])
    if len({module['id'] for module in modules}) != len(modules):
        raise ValueError('Worker计划模块编号重复')
    bindings = {}
    for module in modules:
        require_workspace_process(module,policy)
        wants = 'workspace' in module.get('requiredCapabilities', [])
        if wants and set(module['requiredCapabilities']) - {'text', 'workspace'}:
            raise ValueError('Worker workspace须与原生、浏览器和图像能力分为不同模块')
        binding = bind_role(module, policy, checks, registry) if wants else None
        if wants and binding is None:
            raise ValueError('该岗位没有可用的Worker文件与固定脚本授权：' + module['roleId'])
        bindings[module['id']] = binding
    return bindings
