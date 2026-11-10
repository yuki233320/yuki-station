"""Versioned source policy and zero-model event gates.

The adapter owns source and actor identity. Event body/title are untrusted data
and cannot grant models, actions, paths, or another source's permissions.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import re


SCHEMA = 'yuki-trigger-source/v1'
KINDS = {'github', 'custom', 'timer'}
MODES = {'observe', 'review', 'auto'}
ACTIONS = {'create_task', 'draft_pr'}
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
STANDARD_TOKEN = re.compile(r'sk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{20,}')
PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')


def _text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise ValueError(name + '须为有界非空文本')
    return value.strip()


def _strings(value, name, limit=100):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(name + '须为有界列表')
    rows = [_text(item, name) for item in value]
    if len(set(rows)) != len(rows) or any('*' in item or '?' in item for item in rows):
        raise ValueError(name + '须为精确、不重复的值，不支持通配符')
    return rows


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + '超出允许整数范围')
    return value


def _date(value):
    value = _text(value, '时间', 80)
    try:
        parsed = datetime.fromisoformat(re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value.replace('Z', '+00:00')))
    except ValueError:
        raise ValueError('时间须为带时区的有效ISO格式') from None
    if parsed.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return parsed.astimezone(timezone.utc)


def _safe_config(kind, config):
    if not isinstance(config, dict):
        raise ValueError('来源config须为对象')
    if kind == 'github':
        from github_source import validate_source_config
        return validate_source_config(kind, config)
    if kind == 'custom':
        if set(config) - {'actor', 'container', 'eventType'}:
            raise ValueError('自定义来源只接受固定actor/container/eventType身份')
        return {key: _text(config.get(key), '自定义来源' + key) for key in ('actor', 'container', 'eventType')}
    if set(config) - {'dueAt', 'intervalSeconds'}:
        raise ValueError('定时来源只接受dueAt与intervalSeconds')
    interval = config.get('intervalSeconds')
    if interval is not None:
        interval = _integer(interval, '定时间隔', 1, 31536000)
    return {'dueAt': _date(config.get('dueAt')).isoformat(), 'intervalSeconds': interval}


def normalize_source(request):
    """Normalize configuration only; store owns IDs, CAS, and revision increment."""
    keys = {'schema', 'id', 'revision', 'name', 'kind', 'enabled', 'mode', 'config', 'policy', 'target', 'evaluatorModel', 'subscription'}
    if not isinstance(request, dict) or set(request) - keys:
        raise ValueError('Trigger来源包含未支持的字段')
    from project_team import SECRET
    encoded = json.dumps(request, ensure_ascii=False, allow_nan=False)
    if SECRET.search(encoded) or STANDARD_TOKEN.search(encoded) or PRIVATE_KEY.search(encoded):
        raise ValueError('Trigger配置不能保存凭据；请使用仅在服务内存中的连接入口')
    if request.get('schema', SCHEMA) != SCHEMA:
        raise ValueError('Trigger来源schema不受支持')
    source_id = request.get('id')
    if source_id is not None and (not isinstance(source_id, str) or not IDENTIFIER.fullmatch(source_id)):
        raise ValueError('Trigger来源编号无效')
    revision = _integer(request.get('revision', 0), '来源版本', 0, 2147483647)
    kind, mode = request.get('kind'), request.get('mode', 'observe')
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(mode, str) or mode not in MODES:
        raise ValueError('来源类型或运行模式无效')
    enabled = request.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('来源开关须为布尔值')
    policy = request.get('policy', {})
    policy_keys = {'actors', 'containers', 'eventTypes', 'allowBots', 'selfActors', 'excludeActors',
                   'debounceSeconds', 'maxBatchEvents', 'cooldownSeconds', 'rules', 'allowUnverifiedEditors'}
    if not isinstance(policy, dict) or set(policy) - policy_keys:
        raise ValueError('来源规则包含未支持字段')
    filters = {key: _strings(policy.get(key, []), key) for key in
               ('actors', 'containers', 'eventTypes', 'selfActors', 'excludeActors')}
    allow_bots = policy.get('allowBots', False)
    if type(allow_bots) is not bool:
        raise ValueError('allowBots须为布尔值')
    allow_unverified = policy.get('allowUnverifiedEditors', False)
    if type(allow_unverified) is not bool:
        raise ValueError('allowUnverifiedEditors须为布尔值')
    filters.update(allowBots=allow_bots, allowUnverifiedEditors=allow_unverified,
                   debounceSeconds=_integer(policy.get('debounceSeconds', 10), '合并窗口', 0, 3600),
                   maxBatchEvents=_integer(policy.get('maxBatchEvents', 100), '批次事件数', 1, 100),
                   cooldownSeconds=_integer(policy.get('cooldownSeconds', 120), '冷却时间', 0, 86400))
    rules = policy.get('rules', [])
    if not isinstance(rules, list) or len(rules) > 32:
        raise ValueError('研判规则须为最多32项的列表')
    normalized_rules = []
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) - {'id', 'description', 'enabled'}
                or not isinstance(rule.get('id'), str) or not IDENTIFIER.fullmatch(rule['id'])
                or type(rule.get('enabled', True)) is not bool):
            raise ValueError('每条研判规则须有合法id、description与布尔enabled')
        normalized_rules.append({'id': rule['id'], 'description': _text(rule.get('description'), '研判规则', 4000),
                                 'enabled': rule.get('enabled', True)})
    if len({rule['id'] for rule in normalized_rules}) != len(normalized_rules):
        raise ValueError('研判规则编号不能重复')
    if sum(len(rule['description']) for rule in normalized_rules) > 16000:
        raise ValueError('研判规则说明总量不能超过16000字符')
    if mode in ('review', 'auto') and not any(rule['enabled'] for rule in normalized_rules):
        raise ValueError('review/auto须至少启用一条用户明确授权的研判规则')
    filters['rules'] = normalized_rules
    target = request.get('target', {})
    target_keys = {'projectId', 'coordinatorModel', 'teamModels', 'taskRoute', 'allowedPaths', 'allowedActions'}
    if not isinstance(target, dict) or set(target) - target_keys:
        raise ValueError('Trigger目标包含未支持字段')
    pid = target.get('projectId')
    if not isinstance(pid, str) or not re.fullmatch('[a-f0-9]{12}', pid):
        raise ValueError('来源必须绑定有效的本地项目编号')
    coordinator = target.get('coordinatorModel')
    if coordinator is not None:
        coordinator = _text(coordinator, '主模型', 160)
    team = _strings(target.get('teamModels', []), '工作小组', 20)
    actions = _strings(target.get('allowedActions', []), '允许动作', 2)
    if set(actions) - ACTIONS:
        raise ValueError('仅允许create_task/draft_pr；不能授予部署、合并或任意外部动作')
    if 'draft_pr' in actions and kind != 'github':
        raise ValueError('草稿PR仅用于明确绑定仓库与分支的GitHub来源')
    paths = _strings(target.get('allowedPaths', []), '允许路径', 64)
    from studio import clean_path
    from pathlib import PurePosixPath
    for path in paths:
        clean_path(path.rstrip('/'))
        if str(PurePosixPath(path.rstrip('/'))) != path.rstrip('/'):
            raise ValueError('允许路径须为规范相对路径，不能使用别名')
    route = target.get('taskRoute', {'mode': 'team'})
    from task_routing import normalize_route
    normalized_route = normalize_route(route, '按已授权Trigger来源的固定合同处理候选')
    if normalized_route['mode'] == 'direct' and paths:
        for scope in normalized_route['contract']['scope']:
            if not any(scope == path or path.endswith('/') and scope.startswith(path) for path in paths):
                raise ValueError('固定独立合同的范围超出来源allowedPaths')
    evaluator = request.get('evaluatorModel')
    if evaluator is not None:
        evaluator = _text(evaluator, '评估型号', 160)
    if mode in ('review', 'auto') and evaluator is None:
        raise ValueError('评估或自动模式须显式指定评估型号，后续评估可能计费')
    if mode == 'auto' and (not paths or 'create_task' not in actions or not coordinator or coordinator not in team):
        raise ValueError('自动模式须明确create_task、非空允许路径及含主模型的工作小组')
    result = {'schema': SCHEMA, 'id': source_id, 'revision': revision, 'name': _text(request.get('name'), '来源名称', 120),
              'kind': kind, 'enabled': enabled, 'mode': mode, 'config': _safe_config(kind, request.get('config', {})),
              'policy': filters, 'target': {'projectId': pid, 'coordinatorModel': coordinator, 'teamModels': team,
                  'taskRoute': normalized_route, 'allowedPaths': paths, 'allowedActions': actions}, 'evaluatorModel': evaluator}
    if 'subscription' in request:
        subscription = request['subscription']
        if (not isinstance(subscription, dict) or set(subscription) != {'version', 'enabled'}
                or type(subscription.get('version')) is not int or subscription['version'] != 1
                or type(subscription.get('enabled')) is not bool):
            raise ValueError('订阅设置须有version=1及enabled布尔值')
        if subscription['enabled'] and (mode != 'auto' or normalized_route['mode'] != 'direct'):
            raise ValueError('后续事件订阅需要auto模式和完整单Worker合同')
        result['subscription'] = copy.deepcopy(subscription)
    return copy.deepcopy(result)


def normalize_event(event):
    keys = {'sourceId', 'externalId', 'container', 'threadKey', 'eventType', 'actor', 'isBot', 'isSelfEcho',
            'title', 'body', 'url', 'occurredAt', 'origin'}
    if not isinstance(event, dict) or set(event) - keys:
        raise ValueError('来源事件字段无效；正文不能新增授权字段')
    encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
    if PRIVATE_KEY.search(encoded):
        raise ValueError('来源事件包含私钥，已拒绝接收')
    event = json.loads(STANDARD_TOKEN.sub('[已隐藏]', encoded))
    result = {key: _text(event.get(key), '事件' + key, 500) for key in
              ('externalId', 'container', 'threadKey', 'eventType', 'actor')}
    sid = event.get('sourceId')
    if sid is not None and (not isinstance(sid, str) or not IDENTIFIER.fullmatch(sid)):
        raise ValueError('事件来源编号无效')
    result['sourceId'] = sid
    for key in ('isBot', 'isSelfEcho'):
        value = event.get(key, False)
        if type(value) is not bool:
            raise ValueError('事件身份标记须为布尔值')
        result[key] = value
    for key, limit in (('title', 1000), ('url', 3000)):
        value = event.get(key, '')
        if not isinstance(value, str) or len(value) > limit or '\0' in value:
            raise ValueError('事件' + key + '须为有界文本')
        result[key] = value
    body = event.get('body', '')
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(body) > 40000 or '\0' in body:
        raise ValueError('事件正文过长或无效')
    origin = event.get('origin', {})
    if not isinstance(origin, dict) or len(json.dumps(origin, ensure_ascii=False, allow_nan=False)) > 12000:
        raise ValueError('事件来源证明格式无效或过长')
    result.update(body=body, occurredAt=_date(event.get('occurredAt')).isoformat(), origin=copy.deepcopy(origin))
    return result


def rule_gate(source, event, subscription=None):
    """Only normalized host/adapter identity participates; never inspect body."""
    def deny(reason):
        return {'accepted': False, 'reason': reason}
    if not source.get('enabled'):
        return deny('source_disabled')
    if not source.get('id') or event.get('sourceId') != source['id']:
        return deny('source_identity_mismatch')
    origin, config, kind = event.get('origin', {}), source['config'], source['kind']
    if origin.get('rejectedContent') is True:
        return deny('invalid_or_sensitive_content')
    if origin.get('kind') != kind:
        return deny('origin_kind_mismatch')
    if kind == 'github':
        if origin.get('repository') != config['repository'] or event['container'] != config['repository']:
            return deny('repository_mismatch')
        if event['eventType'] == 'github.workflow_run' and origin.get('branch') != config['branch']:
            from trigger_store import fingerprint
            if (not subscription or subscription.get('repository') != config['repository']
                    or subscription.get('threadKey') != event['threadKey']
                    or subscription.get('headBranch') != origin.get('branch')
                    or subscription.get('headSHA') != origin.get('headSHA')
                    or fingerprint(subscription) != origin.get('subscriptionBindingFingerprint')):
                return deny('branch_mismatch')
        if origin.get('actorBasis') == 'author_unverified_editor' and not source['policy'].get('allowUnverifiedEditors', False):
            return deny('editor_unverified')
    elif kind == 'custom':
        if any(event.get(key) != config[key] for key in ('actor', 'container', 'eventType')):
            return deny('custom_identity_mismatch')
    elif kind == 'timer':
        if (event.get('actor'), event.get('container'), event.get('eventType')) != ('timer', 'timer', 'timer.tick'):
            return deny('timer_identity_mismatch')
    policy = source['policy']
    if event.get('isSelfEcho') or event.get('actor') in policy['selfActors']:
        return deny('self_echo')
    if event.get('actor') in policy['excludeActors']:
        return deny('actor_excluded')
    if event.get('isBot') and not policy['allowBots']:
        return deny('bot_not_allowed')
    for field, key in (('actor', 'actors'), ('container', 'containers'), ('eventType', 'eventTypes')):
        if not policy[key] or event.get(field) not in policy[key]:
            return deny(field + '_not_allowed')
    return {'accepted': True, 'reason': 'matched_source_policy'}


def custom_event(source, payload, *, external_id=None, occurred_at=None):
    if source.get('kind') != 'custom' or not source.get('id'):
        raise ValueError('仅已保存的custom来源可提交自定义事件')
    config = source['config']
    if not isinstance(payload, dict):
        raise ValueError('自定义事件须为对象')
    # This envelope is the source's authority; payload identity fields are data.
    return normalize_event({'sourceId': source['id'], 'externalId': external_id or payload.get('externalId'),
        'actor': config['actor'], 'container': config['container'], 'eventType': config['eventType'],
        'threadKey': payload.get('threadKey') or config['container'], 'isBot': False, 'isSelfEcho': False,
        'title': payload.get('title', ''), 'body': payload.get('body', payload), 'url': '',
        'occurredAt': occurred_at or payload.get('occurredAt') or datetime.now(timezone.utc).isoformat(),
        'origin': {'kind': 'custom', 'identityBasis': 'configured_source'}})


def timer_event(source, now=None):
    """Return only the latest due slot; persistence deduplicates its externalId."""
    if source.get('kind') != 'timer' or not source.get('id'):
        raise ValueError('仅已保存的timer来源可生成定时事件')
    if not source.get('enabled'):
        return None
    current = datetime.now(timezone.utc) if now is None else _date(now) if isinstance(now, str) else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError('当前时间须包含时区')
    due = _date(source['config']['dueAt'])
    if current < due:
        return None
    interval = source['config']['intervalSeconds']
    if interval is not None:
        due += timedelta(seconds=int((current - due).total_seconds() // interval) * interval)
    scheduled = due.isoformat()
    return normalize_event({'sourceId': source['id'], 'externalId': 'timer:' + source['id'] + ':' + scheduled,
        'actor': 'timer', 'container': 'timer', 'eventType': 'timer.tick', 'threadKey': source['id'],
        'isBot': False, 'isSelfEcho': False, 'title': source['name'],
        'body': {'scheduledAt': scheduled, 'notice': 'Only the latest due slot is emitted; no backlog replay.'},
        'url': '', 'occurredAt': scheduled, 'origin': {'kind': 'timer', 'scheduledAt': scheduled}})
