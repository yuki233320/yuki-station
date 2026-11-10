"""Project and role grants for centrally registered MCP connections."""
import copy
import re


def normalize_policy(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {'mode', 'serverIds', 'roleTools'}:
        raise ValueError('MCP 岗位配置格式无效')
    if raw.get('mode') not in ('roles', 'workspace'):
        raise ValueError('MCP 配置模式须为 roles 或 workspace')
    server_ids = _server_ids(raw.get('serverIds', []))
    grants = raw.get('roleTools', {})
    if not isinstance(grants, dict):
        raise ValueError('MCP 岗位工具须为岗位到授权列表的映射')
    normalized = {}
    for role_id, entries in grants.items():
        if not isinstance(role_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', role_id):
            raise ValueError('MCP 岗位编号无效')
        normalized[role_id] = _role_grants(entries)
    return {'mode': raw['mode'], 'serverIds': server_ids, 'roleTools': normalized}


def validate_connections(policy, servers):
    if policy is None:
        return
    directory = {server['id']: {tool['name'] for tool in server['tools']} for server in servers}
    for server_id in policy['serverIds']:
        if server_id not in directory:
            raise ValueError('项目 MCP 引用了不存在的连接：' + server_id)
    for grants in policy['roleTools'].values():
        for grant in grants:
            server_id = grant['serverId']
            if server_id not in policy['serverIds']:
                raise ValueError('岗位 MCP 连接不在项目选择范围内：' + server_id)
            for name in grant['tools']:
                if name not in directory[server_id]:
                    raise ValueError('岗位 MCP 引用了未发现的工具：' + name)


def project_servers(policy, servers):
    selected = servers if policy is None or policy['mode'] == 'workspace' else [
        server for server in servers if server['id'] in policy['serverIds']]
    return copy.deepcopy(selected)


def role_servers(module, policy, servers):
    selected = project_servers(policy, servers)
    if 'browser' in module.get('requiredCapabilities', []):
        selected = [server for server in selected if server['browser']]
    if policy is not None and policy['mode'] == 'roles':
        role = _source_role(module, policy)
        grants = {entry['serverId']: set(entry['tools']) for entry in policy['roleTools'].get(role, [])}
        for server in selected:
            server['tools'] = [tool for tool in server['tools'] if tool['name'] in grants.get(server['id'], set())]
    return [server for server in selected if server['tools']]


def role_binding(module, policy, servers):
    selected = role_servers(module, policy, servers)
    return {
        'mode': policy['mode'] if policy is not None else 'workspace',
        'roleId': module['roleId'], 'sourceRoleId': _source_role(module, policy),
        'servers': [{'serverId': server['id'], 'tools': [tool['name'] for tool in server['tools']]}
                    for server in selected],
        'capabilities': (['plugins'] if selected else []) + (['browser'] if any(server['browser'] for server in selected) else []),
    }


def _source_role(module, policy):
    if policy is None or policy['mode'] == 'workspace':
        return None
    role = module['roleId']
    if role in policy['roleTools']:
        return role
    base = module.get('roleType') or (module.get('roleDefinition') or {}).get('baseRoleType')
    return base if base in policy['roleTools'] else None


def _server_ids(values):
    if not isinstance(values, list) or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{12}', value) for value in values):
        raise ValueError('MCP 连接编号须为有效编号列表')
    if len(set(values)) != len(values):
        raise ValueError('MCP 连接编号不能重复')
    return list(values)


def _role_grants(entries):
    if not isinstance(entries, list):
        raise ValueError('每个岗位的 MCP 授权须为列表')
    grants = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'serverId', 'tools'}:
            raise ValueError('岗位 MCP 授权须包含 serverId 和 tools')
        _server_ids([entry['serverId']])
        names = entry['tools']
        if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError('岗位 MCP 工具须为非空名称列表')
        if len(set(names)) != len(names):
            raise ValueError('同一岗位的 MCP 工具名称不能重复')
        grants.append({'serverId': entry['serverId'], 'tools': list(names)})
    _server_ids([entry['serverId'] for entry in grants])
    return grants
