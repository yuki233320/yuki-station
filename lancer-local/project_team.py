"""Project-owned guidance and role memory, independent of any model provider.

Only accepted, merged real deliveries become durable role memory. Raw model
conversations remain in their original session. Every context carries the exact
guidance revision and the references behind its compact cross-session memory.
"""
import copy
import hashlib
import json
import re

from app import safe_path, stamp


SCHEMA = 'yuki-project-team/v1'
ROLE_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}')
SECRET = re.compile(r'sk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
ROOT_GUIDANCE_LIMIT = 24000
ROLE_GUIDANCE_LIMIT = 6000
GUIDANCE_TOTAL_LIMIT = 54000

ROLE_SPECS = {
    'coordinator': ('项目负责人', '维护完整业务目标和模块关系；按能力分派；对明确争议裁决；按验收标准接受或退回产物，不把模型自述当成运行证据。'),
    'architecture': ('架构负责人', '核对业务边界、模块接口、状态与数据流；给出可落地的方案和证据；区分已有事实、推断和未验证假设。'),
    'frontend': ('前端负责人', '理解业务流程与上游接口，完成界面和交互；遵守已定契约、可访问性与交付范围；向下游说明依赖。'),
    'backend': ('后端负责人', '负责服务端规则、数据一致性和接口契约；明确异常路径与权限边界；以实际文件或测试记录支持交付。'),
    'research': ('调研负责人', '围绕项目目标收集有来源的事实，标注时间与不确定性；将发现映射到业务决策，避免未经核实的结论。'),
    'writing': ('内容负责人', '根据项目已确认的事实和受众完成内容；保持术语与业务流程一致；不为润色补造事实。'),
    'design': ('设计负责人', '将业务目标转成信息结构和视觉表达；遵守现有设计规范，向实现方交付清晰的状态与交互说明。'),
    'data': ('数据负责人', '检查数据来源、字段、计算口径与边界；明确处理过程和可复核产物；不将临时分析脚本扩大为产品开发。'),
    'qa': ('质量负责人', '按验收标准核查实际产物；提供可复核的问题、文件与依据；严格区分静态检查、模型判断和实际运行测试。'),
}


def team_defaults(*, new_project=False):
    """Return an independent editable config; no model IDs are baked into roles."""
    config = {'businessBrief': '', 'roleGuides': {key: value[1] for key, value in ROLE_SPECS.items()}, 'customRoles': {}}
    if new_project:
        config['mcpPolicy'] = {'mode': 'roles', 'serverIds': [], 'roleTools': {}}
        config['workerTools'] = {'version': 1, 'roleGrants': {}}
    return config


def _text(value, limit, label):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(label + '格式无效或超过字符上限')
    if SECRET.search(value):
        raise ValueError(label + '含凭据，请移除后保存')
    return value


def _role_id(value):
    if not isinstance(value, str) or not ROLE_ID.fullmatch(value):
        raise ValueError('项目角色编号无效；仅允许小写字母、数字、短横线和下划线')
    return value


def validate_custom_role(definition):
    limits = {'title': 80, 'businessContext': 3000, 'responsibilities': 4000,
              'deliverables': 4000, 'acceptance': 4000}
    if not isinstance(definition, dict) or set(definition) - {*limits, 'baseRoleType'}:
        raise ValueError('自定义岗位定义格式无效')
    result = {}
    for field, limit in limits.items():
        value = _text(definition.get(field), limit, '岗位 ' + field).strip()
        if not value:
            raise ValueError('自定义岗位须填写名称、业务内容、职责、交付和验收要求')
        result[field] = value
    if sum(map(len, result.values())) > 12000:
        raise ValueError('单个自定义岗位定义不能超过 12000 字符')
    base = definition.get('baseRoleType')
    if base not in (None, ''):
        if not isinstance(base, str) or base not in ROLE_SPECS or base == 'coordinator':
            raise ValueError('继承类型须为已知执行岗位；也可不选择基础类型')
        result['baseRoleType'] = base
    return result


def validate_team_config(config):
    """Validate and normalize only supported configuration fields."""
    if not isinstance(config, dict) or set(config) - {'businessBrief', 'roleGuides', 'customRoles', 'mcpPolicy', 'workerTools'}:
        raise ValueError('项目团队配置格式无效')
    business = _text(config.get('businessBrief', ''), 16000, '业务说明')
    guides = config.get('roleGuides', {})
    if not isinstance(guides, dict) or len(guides) > 32:
        raise ValueError('角色规范须为最多 32 个角色的文本映射')
    normalized = {}
    for role, guide in guides.items():
        normalized[_role_id(role)] = _text(guide, ROLE_GUIDANCE_LIMIT, '角色规范')
    if sum(map(len, normalized.values())) > 48000:
        raise ValueError('角色规范总计不能超过 48000 字符')
    custom = config.get('customRoles', {})
    if not isinstance(custom, dict) or len(custom) > 32:
        raise ValueError('自定义岗位须为最多 32 个岗位的映射')
    roles = {}
    for rid, definition in custom.items():
        if not _role_id(rid).startswith('custom-'):
            raise ValueError('自定义岗位须使用系统生成的 custom- 编号')
        roles[rid] = validate_custom_role(definition)
    if sum(len(value) for role in roles.values() for value in role.values()) > 48000:
        raise ValueError('自定义岗位定义总计不能超过 48000 字符')
    result = {'businessBrief': business, 'roleGuides': normalized, 'customRoles': roles}
    if 'mcpPolicy' in config:
        from mcp_policy import normalize_policy
        result['mcpPolicy'] = normalize_policy(config['mcpPolicy'])
    if 'workerTools' in config:
        from worker_policy import normalize_policy
        result['workerTools'] = normalize_policy(config['workerTools'])
    return result


def _config(meta):
    configured = meta.get('teamConfig')
    return validate_team_config(configured if configured is not None else team_defaults())


def _ledger_path(studio, pid):
    return studio.project_dir(pid) / 'team' / 'ledger.json'


def _ledger(studio, pid):
    path = _ledger_path(studio, pid)
    if not path.exists():
        return {'schema': SCHEMA, 'version': 0, 'recordedRuns': [], 'memories': [], 'discussions': []}
    data = json.loads(path.read_text(encoding='utf8'))
    if data.get('schema') != SCHEMA or not all(isinstance(data.get(k), list) for k in ('recordedRuns', 'memories', 'discussions')):
        raise ValueError('项目角色记忆格式无效，请保留原记录并检查')
    return data


def _label(role, definition=None):
    if definition:
        return definition['title']
    if role.startswith('reviewer-'):
        return '交叉审查成员 · ' + role[len('reviewer-'):]
    return ROLE_SPECS.get(role, (role, ''))[0]


def _default_guide(role, work_type=None):
    if role.startswith('reviewer-'):
        return ROLE_SPECS['qa'][1]
    if role in ROLE_SPECS:
        return ROLE_SPECS[role][1]
    fallback = {'development': 'architecture', 'general': 'writing', 'image': 'design'}.get(work_type, work_type)
    return ROLE_SPECS.get(fallback, ('', '明确自己负责的业务模块、上下游契约和验收标准；只交付声明范围内的产物，遇到依赖问题定向提问。'))[1]


def _custom_guide(definition):
    return '\n'.join(label + '：' + definition[field] for field, label in (
        ('title', '岗位'), ('businessContext', '业务内容与位置'), ('responsibilities', '职责'),
        ('deliverables', '交付要求'), ('acceptance', '验收要求')))


def role_directory(meta, workspace):
    config = _config(meta)
    files = {path.stem: 'roles/' + path.name for path in (workspace / 'roles').glob('*.md')
             if ROLE_ID.fullmatch(path.stem)}
    custom = config['customRoles']
    ids = list(dict.fromkeys([*ROLE_SPECS, *config['roleGuides'], *custom, *sorted(files)]))
    return {
        'types': [{'id': rid, 'title': spec[0], 'guide': config['roleGuides'].get(rid) or spec[1]}
                  for rid, spec in ROLE_SPECS.items() if rid != 'coordinator'],
        'roles': [{'roleId': rid, 'title': _label(rid, custom.get(rid)),
                   'guide': _custom_guide(custom[rid]) if rid in custom else config['roleGuides'].get(rid) or _default_guide(rid),
                   **({'definition': copy.deepcopy(custom[rid]), 'source': 'user-defined'} if rid in custom else {}),
                   **({'file': files[rid]} if rid in files else {})} for rid in ids],
    }


def work_framework(module, plan):
    if not module.get('id'):
        return None
    rid = module['roleId']
    by_id = {item['id']: item for item in plan.get('modules', [])}
    upstream = [by_id[mid] for mid in module.get('dependsOn', [])]
    downstream = [item for item in by_id.values() if module['id'] in item.get('dependsOn', [])]
    def relation(item):
        return {'moduleId': item['id'], 'roleId': item.get('roleId'), 'title': item.get('title', item['id'])}
    return {
        'schema': 'yuki-work-framework/v1', 'roleId': rid,
        'roleTitle': _label(rid, module.get('roleDefinition')),
        'roleDefinition': copy.deepcopy(module.get('roleDefinition')),
        'roleSource': 'user-defined' if module.get('roleDefinition') else 'project-role-or-execution-responsibility',
        'roleType': module.get('roleType') or (rid if rid in ROLE_SPECS else None),
        **({'mcpBinding': copy.deepcopy(module['mcpBinding'])} if 'mcpBinding' in module else {}),
        'objective': module.get('title', module['id']), 'instructions': module.get('instructions', ''),
        'inputs': [{'moduleId': item['id'], 'expectedPaths': list(item.get('scope', []))} for item in upstream],
        'outputs': list(module.get('scope', [])),
        'boundaries': ['只在本模块声明范围内交付，不能修改上游或其他模块产物。',
                       '不能自行改变职责、依赖与固定验收标准；遇到冲突向主负责人说明。',
                       '模型判断与运行证据分开；未取得实际工具回执不能声称执行成功。'],
        'completionCriteria': list(module.get('acceptance', [])),
        'upstream': [relation(item) for item in upstream], 'downstream': [relation(item) for item in downstream],
        'communication': {
            'contacts': [{'roleId': 'coordinator', 'moduleId': None}]
                        + [{'roleId': item.get('roleId'), 'moduleId': item['id']} for item in upstream
                           if item.get('assignment', {}).get('model') != 'native_codex'],
            'maxClarificationRounds': 1, 'maxQuestions': 2,
            'discussionRoundLimit': module.get('reviewPolicy', {}).get('roundLimit', 0),
        },
    }


def _source(path, text, kind, revision):
    return {'path': path, 'kind': kind, 'status': 'loaded', 'revision': revision,
            'sha256': hashlib.sha256(text.encode('utf8')).hexdigest(),
            'includedChars': len(text), 'totalChars': len(text), 'truncated': False, 'text': text}


def _required_file(workspace, name, limit, revision, kind):
    path = safe_path(workspace, name)
    if not path.exists():
        return {'path': name, 'kind': kind, 'status': 'missing', 'revision': revision, 'requiredWhenPresent': True}
    if not path.is_file() or path.stat().st_size > limit * 4:
        raise ValueError(name + '超出必读规范范围；请精简该文件后继续，不能静默截断')
    try:
        value = path.read_text(encoding='utf8')
    except UnicodeError as exc:
        raise ValueError(name + '必须是 UTF-8 文本') from exc
    if len(value) > limit:
        raise ValueError(name + '超出必读规范字符上限；请精简后继续，不能静默截断')
    _text(value, limit, name)
    return _source(name, value, kind, revision)


def _module(shared, run):
    if shared.get('businessStage') == 'clarification':
        # The receiver keeps its own responsibility; the question's module is
        # separately retained as questionOrigin instead of becoming its scope.
        return shared.get('recipientModule') or {}
    candidate = shared.get('module') or shared.get('currentModule')
    if isinstance(candidate, dict):
        return candidate
    module_id = shared.get('moduleId')
    return next((m for m in (shared.get('plan') or run.get('plan') or {}).get('modules', []) if m.get('id') == module_id), {})


def _role(agent_id, shared, module, role_id=None):
    explicit = role_id or shared.get('roleId') or module.get('roleId')
    if explicit:
        return _role_id(explicit)
    if agent_id == 'coordinator' or agent_id.startswith('coordinator-'):
        return 'coordinator'
    if agent_id.startswith('reviewer-'):
        return _role_id(agent_id)
    if module.get('id'):
        return _role_id('module-' + module['id'])
    return _role_id(agent_id)


def _roster(plan, configured, coordinator='opus'):
    rows = {'coordinator': {'roleId': 'coordinator', 'title': _label('coordinator'), 'models': [coordinator], 'modules': [], 'configured': True}}
    for item in plan.get('modules', []):
        rid = _role_id(item.get('roleId') or 'module-' + item['id'])
        row = rows.setdefault(rid, {'roleId': rid, 'title': _label(rid, item.get('roleDefinition')), 'models': [], 'modules': [],
                                    'configured': rid in configured or bool(item.get('roleDefinition'))})
        row['modules'].append({'id': item['id'], 'title': item.get('title', item['id']), 'workType': item.get('workType', 'general')})
        assigned = item.get('assignment', {}).get('model')
        if assigned and assigned not in row['models']:
            row['models'].append(assigned)
    return list(rows.values())


def project_context(studio, s, run, agent_id, model, instruction, shared, role_id=None):
    """Return fields to merge into memory before forming an actual model request.

    A run snapshots team history and metadata at start. Later calls in the same
    run therefore cannot silently inherit another concurrently finishing task.
    Live per-run progress still comes from its own nodes and explicit handoffs.
    """
    pid = s['projectId']
    meta = run.get('projectSnapshot') or studio.meta(pid)
    config = _config(meta)
    revision = run.get('projectRevision') or studio.store.current(pid)[0]['revision']
    revision_dir = studio.store.revision_dir(pid, revision)
    state = json.loads((revision_dir / 'state.json').read_text(encoding='utf8'))
    workspace = revision_dir / 'workspace'
    module = _module(shared, run)
    rid = _role(agent_id, shared, module, role_id)
    plan = shared.get('plan') or run.get('plan') or {}
    contract = shared.get('executionContract') or shared.get('qualityContract') or run.get('taskContracts', {}).get(module.get('id'), {})
    framework = copy.deepcopy(contract.get('workFramework')) if 'workFramework' in contract else work_framework(module, plan)
    definition = None
    if rid == module.get('roleId'):
        definition = ((framework or {}).get('roleDefinition') or module.get('roleDefinition')
                      or config['customRoles'].get(rid))
    sources = [_required_file(workspace, name, ROOT_GUIDANCE_LIMIT, revision, kind)
               for name, kind in [('AGENTS.md', 'project_rules'), ('PROJECT.md', 'business_overview')]]
    inherited = (definition.get('baseRoleType') if definition else module.get('roleType')) if rid == module.get('roleId') else None
    role_ids = [inherited, rid] if inherited and inherited != rid else [rid]
    role_sources = []
    guides = []
    for role in role_ids:
        if role == rid and definition:
            guide = _custom_guide(definition)
            guides.append(guide)
            role_sources.append(_source('teamConfig.customRoles.' + rid, guide, 'configured_role_rules', meta.get('version', 0)))
            role_sources.append(_required_file(workspace, 'roles/' + role + '.md', ROLE_GUIDANCE_LIMIT, revision, 'role_rules'))
            continue
        guide = config['roleGuides'].get(role) or _default_guide(role, module.get('workType'))
        if role == inherited or len(role_ids) == 1 or config['roleGuides'].get(role):
            guides.append(guide)
            guide_source = 'teamConfig.roleGuides.' + role if config['roleGuides'].get(role) else 'built-in-role-guide.' + role
            role_sources.append(_source(guide_source, guide, 'configured_role_rules', meta.get('version', 0)))
        role_sources.append(_required_file(workspace, 'roles/' + role + '.md', ROLE_GUIDANCE_LIMIT, revision, 'role_rules'))
    sources.extend(role_sources)
    if sum(source.get('includedChars', 0) for source in sources if source['kind'] != 'configured_role_rules') > GUIDANCE_TOTAL_LIMIT:
        raise ValueError('必读项目与角色规范总量过长，请精简后继续')
    instructions = _text(meta.get('instructions', ''), 12000, '项目约定')
    guide = '\n本岗位补充（同项以具体岗位约定为准）：\n'.join(guides)
    if instructions:
        sources.append(_source('project.instructions', instructions, 'project_rules', meta.get('version', 0)))
    business_source = next((src for src in sources if src['path'] == 'PROJECT.md' and src['status'] == 'loaded' and src['text'].strip()), None)
    business = config['businessBrief'].strip()
    if business:
        sources.append(_source('teamConfig.businessBrief', business, 'business_overview', meta.get('version', 0)))
    documented = bool(business or business_source)
    if not business:
        business = business_source['text'] if business_source else ('项目目标：' + state.get('goal', '') + '\n本次任务：' + run.get('request', ''))
    frozen = run.get('teamSnapshot')
    history = frozen if isinstance(frozen, dict) else read_summary(studio, pid)
    memories = [copy.deepcopy(row) for row in history.get('memories', []) if row.get('accepted') is True and row.get('roleId') == rid][-8:]
    modules = plan.get('modules', [])
    by_id = {m['id']: m for m in modules}
    nodes = run.get('nodes', [])
    current = next((n for n in nodes if n.get('id') == shared.get('nodeId')), None)
    if current is None:
        current = next((n for n in reversed(nodes) if n.get('status') == 'running' and n.get('agent') == model and
                        (n.get('roleId') == rid or n.get('agentId') == agent_id)), {})
    def relation(mid):
        item = by_id.get(mid, {})
        node = next((n for n in reversed(nodes) if n.get('moduleId') == mid and n.get('phase') == 'build'), {})
        return {'moduleId': mid, 'title': item.get('title', mid), 'roleId': item.get('roleId'),
                'model': item.get('assignment', {}).get('model'), 'status': node.get('status', 'waiting'),
                'nodeId': node.get('id'), 'artifactPaths': [f.get('path') for f in node.get('result', {}).get('files', [])]}
    upstream = [relation(mid) for mid in module.get('dependsOn', [])]
    downstream = [relation(m['id']) for m in modules if module.get('id') in m.get('dependsOn', [])]
    review_roles = [_role_id(value) for value in shared.get('reviewRoles', [])]
    peers = sorted({'coordinator'} | {r['roleId'] for r in upstream + downstream if r.get('roleId')} | set(review_roles))
    return {
        'businessContext': {'projectId': pid, 'title': state.get('title'), 'goal': state.get('goal'), 'overview': business,
                            'documented': documented, 'notice': '统一业务资料已加载；未在资料中明确的事实仍需核实。' if documented else '尚未提供完整业务说明，仅使用项目目标和当前要求；不得声称已经了解未提供的业务事实。',
                            'projectRevision': revision, 'metadataVersion': meta.get('version', 0)},
        'roleAssignment': {'roleId': rid, 'title': _label(rid, definition), 'model': model, 'independentAgentId': agent_id,
                           'roleDefinition': copy.deepcopy(definition),
                           'roleSource': 'user-defined' if definition else 'project-role-or-execution-responsibility',
                           'guide': guide, 'workFramework': framework,
                           'guideInheritance': {'roleType': inherited, 'roleIds': role_ids,
                                                'precedence': '先继承岗位类型，再应用本项目具体岗位规范；同项以具体岗位约定为准，但不能覆盖用户要求和项目共同约束。'},
                           'identityPolicy': '角色属于项目；模型可更换。跨会话仅继承已验收结论和可追溯引用，不拼接其他模型的原始对话。'},
        'currentPosition': {'sessionId': s['id'], 'runId': run['id'], 'nodeId': current.get('id'),
                            'stage': shared.get('businessStage') or current.get('phase'), 'moduleId': module.get('id'),
                            'moduleTitle': module.get('title'), 'scope': copy.deepcopy(module.get('scope', [])),
                            'acceptance': copy.deepcopy(module.get('acceptance', [])), 'upstream': upstream, 'downstream': downstream,
                            'discussionPeers': peers, 'discussionPolicy': '仅围绕依赖、接口或证据定向提问；讨论轮数和接收对象由执行器校验，不由角色规范自行增加。'},
        'questionOrigin': ({'moduleId': (shared.get('currentModule') or {}).get('id'),
                            'roleId': (shared.get('currentModule') or {}).get('roleId'),
                            'question': copy.deepcopy(shared.get('question'))}
                           if shared.get('businessStage') == 'clarification' else None),
        'teamRoster': _roster(plan, config['roleGuides'], run.get('coordinatorModel', 'opus')),
        'projectRoleMemory': memories,
        'guidanceSnapshot': {'schema': SCHEMA, 'projectRevision': revision, 'metadataVersion': meta.get('version', 0),
                             'teamMemoryVersion': history.get('version', 0), 'sources': sources,
                             'missing': [src['path'] for src in sources if src['status'] == 'missing'],
                             'conflictPolicy': '资料相互矛盾时明确标注并向项目负责人澄清；角色规范不能扩大权限或覆盖用户要求与项目约束。',
                             'policy': '存在的共同规范与本角色规范全文加载；过长、凭据或无效文件会阻止该次请求。其他目录的角色规范不自动视为全局规则。'},
    }


def _acceptance(run, module_id):
    return next((row for row in run.get('final', {}).get('moduleAcceptance', []) if row.get('moduleId') == module_id), None)


def record_delivery(studio, s, run):
    """Promote accepted real outputs exactly once, after the revision was merged."""
    if (run.get('workflowVersion') != 2 or run.get('mode') != 'live' or run.get('status') != 'completed'
            or s.get('status') != 'completed' or run.get('final', {}).get('accepted') is not True
            or not run.get('mergedRevision') or any(check.get('pass') is not True for check in run.get('checks', []))):
        return {'recorded': False, 'reason': '只有已验收并合入项目的真实业务流程才会成为长期角色记忆'}
    pid = s['projectId']
    studio.store.revision_dir(pid, run['mergedRevision'])
    with studio.lock:
        ledger = _ledger(studio, pid)
        if run['id'] in ledger['recordedRuns']:
            return {'recorded': False, 'reused': True, 'version': ledger['version']}
        rows = []
        for module in (run.get('plan') or {}).get('modules', []):
            acceptance = _acceptance(run, module['id'])
            if not acceptance or acceptance.get('accepted') is not True:
                raise ValueError('缺少逐模块的明确验收，不能更新长期角色记忆')
            if acceptance.get('unmet') or any(c.get('status') != 'met' for c in acceptance.get('criteria', [])):
                raise ValueError('模块仍有未满足或未验证的验收条件，不能更新长期角色记忆')
            rid = _role_id(module.get('roleId') or 'module-' + module['id'])
            node = next((n for n in reversed(run.get('nodes', [])) if n.get('phase') == 'build' and n.get('status') == 'done' and
                         (n.get('moduleId') == module['id'] or n.get('id') == 'build-' + module['id'])), None)
            if node is None:
                raise ValueError('缺少已完成的模块交付节点，不能更新长期角色记忆')
            summary = _text(str(node.get('result', {}).get('summary', ''))[:2500], 2500, '角色交付摘要')
            rows.append({'id': run['id'] + ':' + node['id'], 'roleId': rid, 'title': _label(rid, module.get('roleDefinition')),
                         'roleDefinition': copy.deepcopy(module.get('roleDefinition')),
                         'model': node.get('agent') or module.get('assignment', {}).get('model'),
                         'workType': module.get('workType', 'general'), 'moduleId': module['id'], 'summary': summary,
                         'accepted': True, 'acceptance': copy.deepcopy(acceptance),
                         'evidenceKind': 'artifact_review', 'runtimeVerified': False,
                         'source': {'session': s['id'], 'run': run['id'], 'node': node['id'], 'revision': run['mergedRevision'], 'contextRef': node.get('contextRef')},
                         'artifactPaths': [f['path'] for f in node.get('result', {}).get('files', [])],
                         'recordedAt': stamp()})
        final_node = next((n for n in reversed(run.get('nodes', [])) if n.get('phase') == 'integration' and n.get('status') == 'done'), {})
        rows.append({'id': run['id'] + ':coordinator', 'roleId': 'coordinator', 'title': _label('coordinator'),
                     'model': run.get('coordinatorModel', 'opus'), 'workType': 'coordination', 'moduleId': None,
                     'summary': _text(str(run['final'].get('summary', ''))[:2500], 2500, '项目交付摘要'),
                     'accepted': True, 'acceptance': {'accepted': True, 'evidence': [a.get('path') for a in run.get('artifacts', [])]},
                     'evidenceKind': 'artifact_review', 'runtimeVerified': False,
                     'source': {'session': s['id'], 'run': run['id'], 'node': final_node.get('id'), 'revision': run['mergedRevision'], 'contextRef': final_node.get('contextRef')},
                     'artifactPaths': [a.get('path') for a in run.get('artifacts', [])], 'recordedAt': stamp()})
        ledger['memories'].extend(rows)
        for discussion in run.get('discussions', []):
            value = copy.deepcopy(discussion)
            value['source'] = {'session': s['id'], 'run': run['id'], 'revision': run['mergedRevision']}
            ledger['discussions'].append(value)
        # The complete history remains in source sessions; keep a bounded hot index.
        ledger['memories'] = ledger['memories'][-512:]
        ledger['discussions'] = ledger['discussions'][-512:]
        ledger['recordedRuns'].append(run['id'])
        ledger['version'] += 1
        ledger['updated'] = stamp()
        studio._write(_ledger_path(studio, pid), ledger)
        return {'recorded': True, 'version': ledger['version'], 'roles': len(rows)}


def read_summary(studio, pid):
    """Compact API/UI view and a deterministic snapshot for a new run."""
    with studio.lock:
        config = _config(studio.meta(pid))
        ledger = _ledger(studio, pid)
        memories = copy.deepcopy(ledger['memories'][-128:])
        role_ids = list(dict.fromkeys(['coordinator', *config['roleGuides'], *config['customRoles'], *(m['roleId'] for m in memories)]))
        rows = []
        for rid in role_ids:
            accepted = [row for row in memories if row['roleId'] == rid]
            models = list(dict.fromkeys(row['model'] for row in accepted if row.get('model')))
            definition = config['customRoles'].get(rid)
            rows.append({'roleId': rid, 'title': _label(rid, definition),
                         'guide': _custom_guide(definition) if definition else config['roleGuides'].get(rid, _default_guide(rid)),
                         'definition': copy.deepcopy(definition),
                         'models': models, 'acceptedSamplesInView': len(accepted), 'latest': accepted[-1] if accepted else None})
        return {'schema': SCHEMA, 'version': ledger['version'], 'updated': ledger.get('updated'),
                'config': config, 'roles': rows, 'memories': memories,
                'discussions': copy.deepcopy(ledger['discussions'][-80:]),
                'acceptedRunCount': len(ledger['recordedRuns']),
                'memoryNotice': '只展示已验收、合入项目的真实交付摘要。原始会话与完整讨论保留在来源记录；当前视图有数量上限。',
                'calibrationNotice': '已验收样本属于项目内的产物判断，不能当作运行测试通过率、官方能力榜或跨模型基准。'}
