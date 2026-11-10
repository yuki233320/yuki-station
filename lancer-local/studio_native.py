"""Host-owned native Codex jobs. Original projects and upstream files are immutable."""
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from studio_native_collection import archive_job, load_collection

BRIDGE = Path(__file__).resolve().parent.parent / 'lancer-codex-bridge'
SECRET = re.compile(r'sk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


class NativeExecutionError(RuntimeError):
    def __init__(self, message, receipt=None):
        super().__init__(message)
        self.receipt = receipt or {}


def native_runtime_info():
    """Detect executables only; availability is enabled by the host after doctor."""
    unavailable = {'detected': False, 'available': False, 'capabilities': [], 'desktopVerified': False}
    if sys.platform not in ('darwin', 'linux'):
        return {**unavailable, 'reason': '此平台暂未接入原生 Codex 桥接；可继续使用模型 API 工作台。'}
    if not all((BRIDGE / name).is_file() for name in ('lancer_adapter.py', 'cli.mjs', 'bridge.mjs', 'rpc.mjs')):
        return {**unavailable, 'reason': '未安装可选的原生 Codex 桥接；可继续使用模型 API 工作台。'}
    def executable(candidates):
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return str(Path(candidate).resolve())
        return None
    node = executable([shutil.which('node'), str(Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')])
    codex = executable([shutil.which('codex'), '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'])
    return {'detected': bool(node and codex), 'available': bool(node and codex),
            'reason': None if node and codex else '未找到原生 Codex 或 Node 运行时。',
            'nodeBin': node, 'codexBin': codex,
            'capabilities': ['text', 'files', 'terminal'], 'desktopVerified': False}


def run_native_codex(*args, **kwargs):
    import importlib.util
    spec = importlib.util.spec_from_file_location('yuki_native_adapter', BRIDGE / 'lancer_adapter.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    return adapter.run_native_codex(*args, **kwargs)


def native_tool_info(runtime):
    script = ('import {NativeBridge} from ' + json.dumps((BRIDGE / 'bridge.mjs').as_uri()) + ';'
              'const bridge=new NativeBridge({workspaceRoot:process.argv[2],stateDir:process.argv[2],binary:process.argv[1]});'
              'console.log(JSON.stringify(await bridge.capabilities()));')
    try:
        process = subprocess.run([runtime['nodeBin'], '--input-type=module', '-e', script,
                                  runtime['codexBin'], str(BRIDGE)], capture_output=True, text=True, timeout=20)
        if process.returncode:
            raise ValueError('原生能力探测尚未完成，请确认 Codex 可正常启动。')
        return json.loads(process.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {'webSearch': {'available': False, 'verified': False, 'mode': 'unknown',
                              'reason': '原生搜索配置探测失败；尚不能分派联网搜索。'}}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _name(value, directory=False):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise ValueError('原生任务路径无效。')
    raw = value[:-1] if directory and value.endswith('/') else value
    p = PurePosixPath(raw)
    if p.is_absolute() or str(p) != raw or unicodedata.normalize('NFC', raw) != raw:
        raise ValueError('原生任务拒绝绝对路径、路径别名或非 NFC 路径：' + value)
    if any(part in ('.', '..') or part.startswith('.') or ':' in part for part in p.parts):
        raise ValueError('原生任务拒绝隐藏目录或越界路径：' + value)
    if p.name.lower() in ('credentials.json', 'auth.json', 'id_rsa', 'id_ed25519') or p.suffix.lower() in ('.pem', '.key', '.p12', '.pfx'):
        raise ValueError('原生任务不接收凭据文件：' + value)
    return raw + ('/' if directory and value.endswith('/') else '')


def _path(root, relative):
    _name(relative)
    p = root / relative
    for candidate in [p, *p.parents]:
        if candidate.is_symlink():
            raise ValueError('原生任务拒绝符号链接：' + relative)
        if candidate == root:
            break
    if root.resolve() not in p.resolve().parents:
        raise ValueError('原生任务路径越界。')
    return p


def _private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.pending')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    tmp.replace(path)
    path.chmod(0o600)


def _inventory(root):
    result, aliases = {}, set()
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        _name(name)
        key = name.casefold()
        if key in aliases:
            raise ValueError('原生工作区存在大小写别名：' + name)
        aliases.add(key)
        if path.is_symlink():
            raise ValueError('原生工作区存在符号链接：' + name)
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink != 1:
            raise ValueError('原生工作区只允许普通独立文件：' + name)
        if path.stat().st_size > 1_000_000:
            raise ValueError('原生文件超过单文件大小上限：' + name)
        data = path.read_bytes()
        result[name] = {'path': name, 'bytes': len(data), 'sha256': _sha(data)}
    if sum(f['bytes'] for f in result.values()) > 8_000_000:
        raise ValueError('原生工作区文件总量超过上限。')
    return result


def _inside(name, scopes):
    return any(name == scope or (scope.endswith('/') and name.startswith(scope)) for scope in scopes)


@contextmanager
def _serial_lock(path, cancel):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            if cancel.is_set():
                raise NativeExecutionError('原生任务在排队期间取消。', {'status': 'cancelled', 'executed': False})
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                cancel.wait(0.1)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _inputs(studio, s, run, context, shared):
    data, records, aliases = {}, [], set()
    def add(origin, path, content, source):
        path = _name(path)
        isolated = 'inputs/' + origin + '/' + path
        key = isolated.casefold()
        if key in aliases:
            # Identical upstream files may be handed off by multiple modules.
            if isolated in data and data[isolated] == content:
                return
            raise ValueError('输入文件冲突或大小写别名：' + isolated)
        aliases.add(key)
        if len(content) > 1_000_000:
            raise ValueError('输入文件超过原生任务上限：' + path)
        text = content.decode('utf8')
        if SECRET.search(text):
            raise ValueError('输入文件含凭据，拒绝复制：' + path)
        data[isolated] = content
        records.append({'path': isolated, 'originalPath': path, 'source': source,
                        'bytes': len(content), 'sha256': _sha(content)})
    project = (context.get('memory') or {}).get('project') or {}
    selected = project.get('files', [])
    if selected:
        revision = studio.store.revision_dir(s['projectId'], run['projectRevision']) / 'workspace'
        for entry in selected:
            source = _path(revision, entry['path'])
            content = source.read_bytes()
            if entry.get('sha256') and _sha(content) != entry['sha256']:
                raise ValueError('项目快照与检索文件哈希不同：' + entry['path'])
            add('project', entry['path'], content, 'project:' + run['projectRevision'])
    for mid, result in sorted((shared.get('upstream') or {}).items()):
        for entry in result.get('files', []):
            if not isinstance(entry.get('content'), str):
                raise ValueError('原生任务暂只接收实际 UTF-8 文本文件：' + str(entry.get('path')))
            add('upstream', entry['path'], entry['content'].encode('utf8'), 'module:' + mid)
    if sum(map(len, data.values())) > 6_000_000:
        raise ValueError('原生任务输入总量超过上限。')
    return data, records


def execute_native(studio, s, run, n, context, messages, shared, cancel, on_delta):
    module = shared.get('module') or {}
    required = set(module.get('requiredCapabilities') or [])
    if shared.get('businessStage') != 'build' or n.get('agent') != 'native_codex':
        raise NativeExecutionError('原生工具桥接只接受已分派的构建模块。')
    if not required.intersection({'files', 'terminal', 'web_search'}) or required - {'text', 'files', 'terminal', 'web_search'}:
        raise NativeExecutionError('原生任务只支持已接入的文件、终端和联网搜索能力。')
    scopes = [_name(scope, directory=True) for scope in module.get('scope', [])]
    if not scopes or any(x.casefold().startswith('inputs/') or x.casefold() == 'inputs' for x in scopes):
        raise NativeExecutionError('原生模块必须声明输出范围，且不得覆盖输入目录。')
    identifiers = [s['projectId'], s['id'], run['id'], n['id']]
    if any(not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', part) for part in identifiers):
        raise NativeExecutionError('原生任务编号无效。')
    root = studio.project_dir(s['projectId']).resolve()
    job = _path(root, '/'.join([s['id'], 'native', run['id'], n['id']]))
    workspace, state = job / 'workspace', job / 'state'
    receipt_path, manifest_path = job / 'receipt.json', job / 'input-manifest.json'
    receipt_ref = str(receipt_path.relative_to(root / s['id']))
    manifest_ref = str(manifest_path.relative_to(root / s['id']))
    evidence = {'receiptRef': receipt_ref, 'receiptPath': str(receipt_path),
                'manifestRef': manifest_ref, 'manifestPath': str(manifest_path)}
    receipt = {'engine': 'native-codex-app-server', 'status': 'preparing', **evidence}
    preserved_receipt = None
    collection, task = None, {}
    def clean(value):
        text = _json(value)
        cleaner = getattr(studio.client, 'clean', None)
        return json.loads(cleaner(text) if cleaner else text)
    def event(kind, detail, **extra):
        with studio.lock:
            studio.event(s, kind, clean(detail), n['id'], agent='native_codex',
                         receiptRef=receipt_ref, evidenceRef=extra.pop('evidenceRef', receipt_ref), **clean(extra))
            studio.save(s)
    try:
        runtime = native_runtime_info()
        if not runtime['detected']:
            raise NativeExecutionError(runtime.get('reason') or '本机没有可执行的原生 Codex 或 Node 运行时。')
        # All Studio instances use one cross-process lock before touching native state.
        with _serial_lock(Path(studio.root) / 'native-runtime.lock', cancel):
            collection = load_collection(Path(studio.root).parent)
            if receipt_path.exists():
                previous_receipt = json.loads(receipt_path.read_text(encoding='utf8'))
                if previous_receipt.get('status') == 'completed':
                    preserved_receipt = previous_receipt
            data, records = _inputs(studio, s, run, context, shared)
            semantic_memory = {k: v for k, v in (context.get('memory') or {}).items()
                               if k not in ('project', 'sharedEvidence')}
            binding = {'ids': identifiers, 'revision': run.get('projectRevision'), 'module': module,
                       'inputs': records, 'roleId': shared.get('roleId'), 'agentId': context.get('agentId'),
                       'businessMemory': semantic_memory, 'ownHistory': messages[:-1]}
            if 'repair' in shared:
                binding['repair'] = shared['repair']
            binding_hash = _sha(_json(binding).encode('utf8'))
            request_path = job / 'task.json'
            if request_path.exists():
                frozen = json.loads(request_path.read_text())
                if frozen['bindingHash'] != binding_hash:
                    raise NativeExecutionError('原生任务恢复时上下文或文件发生变化；拒绝复用任务编号。')
                task = frozen['task']
                manifest = json.loads(manifest_path.read_text())
                if (job / 'attempt.json').exists() and not receipt_path.exists():
                    raise NativeExecutionError('前一次原生执行没有确定结果；保留证据且不自动重跑。', {'status': 'unknown'})
            else:
                if job.exists() and any(job.iterdir()):
                    raise NativeExecutionError('原生任务目录已有未确认资料，不能覆盖。')
                workspace.mkdir(parents=True, mode=0o700)
                for path, content in data.items():
                    target = _path(workspace, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    target.chmod(0o444)
                memory = copy.deepcopy(context.get('memory') or {})
                memory.pop('sharedEvidence', None)
                memory['project'] = {'projectRevision': run.get('projectRevision'), 'files': records,
                                     'notice': '实际完整文件在隔离工作区；只读副本不得修改。'}
                memory['nativeModule'] = module
                memory['ownRecentConversation'] = messages[:-1]
                memory['upstreamSummaries'] = {mid: item.get('summary', '') for mid, item in (shared.get('upstream') or {}).items()}
                memory['handoff'] = {'nodeId': n['id'], 'roleId': shared.get('roleId'),
                                     'contextRef': context.get('id'), 'scope': scopes}
                if 'executionContract' in shared:
                    memory['executionContract'] = copy.deepcopy(shared['executionContract'])
                    memory['handoff']['packet'] = copy.deepcopy(shared['handoff'])
                if 'repair' in shared:
                    memory['repair'] = copy.deepcopy(shared['repair'])
                # Do not repeat the old JSON-files protocol: native files must exist on disk.
                task = {'id': 'yuki-' + '-'.join(identifiers),
                        'workspace': str(workspace), 'mode': 'workspace-write',
                        'contextKey': 'role-' + _sha((s['id'] + ':' + str(shared.get('roleId') or context.get('agentId'))).encode())[:32],
                        'requiredCapabilities': sorted(required - {'text'}),
                        'context': memory,
                        'instruction': '你是 yuki雪站项目专项小组的原生 Codex 工具执行者。结合 context 中完整业务要求和职责完成 nativeModule。'
                        '项目只读副本在 inputs/project/，上游本轮实际交付在 inputs/upstream/；运行上游代码时按这些根目录解析其相对路径。'
                        '实际文件地址只以 context.project.files[].path 的宿主清单为准，inputs/upstream/ 后直接接产物原路径，不加模块ID目录。'
                        '若既有计划举例写成 inputs/upstream/rules/contracts/、dashboard/web/ 或 guide/docs/，那是旧路径说明，分别应为 inputs/upstream/contracts/、inputs/upstream/web/、inputs/upstream/docs/。先读取清单确认存在路径，不要尝试旧目录。'
                        '只准在这些输出范围内创建或修改文件：' + _json(scopes) + '。不得修改 inputs 中任何文件，不得创建隐藏文件、符号链接或缓存。'
                        'Python 使用 -B 或 PYTHONDONTWRITEBYTECODE=1。测试临时文件、数据库和日志也必须放在你的输出范围内。'
                        '不得访问其他项目、真实源码、凭据、账户文件；不得连接外部服务或安装依赖。可读取并使用已有本机运行时。'
                        '本机已确认的 Node 可执行文件：' + runtime['nodeBin'] + '；运行 JavaScript 请直接使用此绝对路径，不要猜测 PATH。'
                        'terminal 能力必须实际运行命令并据实记录输出和退出码；不能把静态阅读写成运行成功。'
                        '预期的业务异常用 Node assert.throws 等断言捕获并核对错误内容；不要用会失败的路径或命令作探测，也不得通过 || true 等方式吞掉真实失败。'
                        '用原生工具实际写交付文件；最终严格按输出 schema 返回 summary,observations,artifacts(path,description),checks(name,status,evidence)。'
                        'artifacts 必须覆盖输出范围内全部新文件；不返回 files/content JSON，不提交输入副本。'
                        'checks 只记录当前模块被分派的必要验收项；分派范围外未执行的浏览器操作或部署应写入 observations 和报告的范围说明，不能虚构为 passed。'
                        '检查未运行标 not_run，失败标 failed，明确原因；不编造通过。'}
                if memory.get('globalWorkInstructions'):
                    task['instruction'] += '\n\n全局工作规范（本轮冻结）：\n' + memory['globalWorkInstructions']
                if shared.get('executionContract', {}).get('nativeCheckVersion') == 1:
                    task['instruction'] += ('本次执行前已在executionContract.requiredChecks固定必要验收命令，必须逐项使用完全相同argv，'
                        '工具workdir设为workspace与该项cwd组合的绝对路径；不要把cd、pwd或其他命令与必要命令拼接。'
                        '先实现对应检查脚本，再直接执行规定argv；不加env前缀、重定向或替换可执行路径。'
                        '宿主将从完整真实工具回执自动绑定，无需自行选择或报告工具ID。其他探索异常如实记入observations，不能称为成功。')
                if 'web_search' in required:
                    task['instruction'] += ('\n本模块要求真实联网检索。使用原生 web search 或已连接的 search_service.web_run；'
                        '搜索后打开相关来源，依据实际内容写入分派的报告文件，保留来源URL及查询时间。'
                        '不得用记忆、生成的链接或本地脚本冒充搜索。shell 网络权限保持原样。')
                if collection:
                    task['projectId'] = collection['projectId']
                    title = str(module.get('title') or n.get('label') or module.get('id') or n['id'])
                    task['threadName'] = ('雪站 · ' + ' '.join(title.split()) + ' · ' + run['id'][:8])[:160]
                if len(task['id']) > 80:
                    task['id'] = 'yuki-' + _sha('-'.join(identifiers).encode())[:64]
                if len(_json(memory)) > 145000 or len(_json(task).encode('utf8')) > 390000:
                    raise NativeExecutionError('原生任务业务上下文超出限制，需要由主模型重新划分范围。')
                manifest = {'version': 1, 'bindingHash': binding_hash, 'inputs': records,
                            'before': _inventory(workspace), 'scope': scopes, 'contextRef': context.get('id'),
                            'directories': [p.relative_to(workspace).as_posix() for p in workspace.rglob('*') if p.is_dir()]}
                _private(manifest_path, manifest)
                _private(request_path, {'bindingHash': binding_hash, 'task': task})
            before = manifest['before']
            current = _inventory(workspace)
            if any(current.get(path) != info for path, info in before.items()):
                raise NativeExecutionError('原生任务输入副本发生变化，停止执行。')
            def native_event(item):
                kind = item.get('type')
                events_ref = str((state / 'jobs' / task['id'] / 'events.jsonl').relative_to(root / s['id']))
                original_ref = events_ref + '#event-' + str(item.get('seq', 'unknown'))
                if kind in ('tool_started', 'tool_completed'):
                    tool = item.get('item') or {}
                    event('native_' + kind, '原生 Codex 工具' + ('开始执行' if kind.endswith('started') else '已返回'),
                          tool={k: tool[k] for k in ('id', 'type', 'status', 'exitCode', 'command', 'query', 'action', 'server', 'tool') if k in tool},
                          evidenceRef=original_ref)
                elif kind == 'tool_output':
                    event('tool_output', '原生 Codex 工具输出', toolId=item.get('itemId'),
                          text=str(item.get('text', ''))[-6000:], evidenceRef=original_ref)
                elif kind == 'sub_agent_activity':
                    event('native_sub_agent_activity', '原生 Codex 子智能体活动',
                          activity=item['item'], evidenceRef=original_ref)
                elif kind == 'execution_phase':
                    event('native_execution_phase', '原生执行阶段', phase=item['phase'],
                          elapsedMs=item['elapsedMs'], evidenceRef=original_ref)
                elif kind == 'runtime_error':
                    event('native_runtime_error', '原生连接异常；重连状态来自 Codex 回执，雪站未重复派发任务。',
                          runtimeError=item['error'], evidenceRef=original_ref)
                elif kind in ('approval_required', 'permission_required', 'input_required'):
                    event('native_approval_required', '原生 Codex 请求授权或补充信息，本次执行暂停。',
                          request=item.get('request') or item, evidenceRef=original_ref)
                elif kind == 'assistant_delta' and on_delta:
                    on_delta(str(item.get('text', '')))
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text())
                receipt['reused'] = True
            else:
                _private(job / 'attempt.json', {'startedAt': time.time(), 'taskId': task['id'], 'bindingHash': binding_hash})
                from app import stamp
                started_at, started_clock = stamp(), time.monotonic()
                try:
                    receipt = run_native_codex(task, workspace, state, on_event=native_event,
                                               node_bin=runtime['nodeBin'], codex_bin=runtime['codexBin'],
                                               allow_workspace_write=True, cancel=cancel,
                                               timeout_ms=getattr(studio, 'native_timeout_ms', 600000))
                except Exception:
                    if run.get('qualityVersion') == 1:
                        studio.record_call(s, run, n, context, 'native', started_at, started_clock,
                                           'cancelled' if cancel.is_set() else 'error')
                    raise
                if run.get('qualityVersion') == 1 and not receipt.get('reused'):
                    outcome = 'returned' if receipt.get('status') == 'completed' else 'cancelled' if cancel.is_set() else 'error'
                    studio.record_call(s, run, n, context, 'native', started_at, started_clock, outcome, receipt)
            receipt = {**receipt, **evidence, 'requestedModel': 'native_codex', 'mode': 'native'}
            if preserved_receipt is None:
                _private(receipt_path, clean(receipt))
            if receipt.get('status') != 'completed':
                raise NativeExecutionError('原生 Codex 未完成：' + str(receipt.get('error') or receipt.get('status')), receipt)
            if task.get('projectId') and receipt.get('projectId') != task['projectId']:
                raise NativeExecutionError('原生会话所属 Codex 项目未核实；保留执行回执。', receipt)
            after = _inventory(workspace)
            if any(after.get(path) != info for path, info in before.items()):
                raise NativeExecutionError('原生 Codex 修改了只读输入；本模块不予验收。', receipt)
            changed = {path: info for path, info in after.items() if path not in before}
            new_directories = {p.relative_to(workspace).as_posix() for p in workspace.rglob('*') if p.is_dir()} - set(manifest.get('directories', []))
            if any(not _inside(path + '/', scopes) and not any(scope.startswith(path + '/') for scope in scopes) for path in new_directories):
                raise NativeExecutionError('原生 Codex 创建了输出范围以外的目录；本模块不予验收。', receipt)
            if any(not _inside(path, scopes) for path in changed):
                raise NativeExecutionError('原生 Codex 生成了输出范围以外的文件；本模块不予验收。', receipt)
            output = receipt.get('output') or {}
            tools, checks = receipt.get('tools') or [], output.get('checks') or []
            if run.get('qualityVersion') != 1:
                if 'terminal' in required and not any(t.get('type') == 'commandExecution' and t.get('status') == 'completed' and type(t.get('exitCode')) is int and t['exitCode'] == 0 for t in tools):
                    raise NativeExecutionError('缺少真实成功完成的终端工具回执；文字声明不能代替执行。', receipt)
                if any(t.get('status') != 'completed' or type(t.get('exitCode')) is not int or t['exitCode'] != 0 for t in tools if t.get('type') == 'commandExecution'):
                    raise NativeExecutionError('原生终端回执存在失败或未完成的命令；本模块不予验收。', receipt)
                if not checks or any(c.get('status') != 'passed' for c in checks):
                    raise NativeExecutionError('原生检查包含失败或未运行项目，请由主模型处理。', receipt)
            artifacts = receipt.get('verifiedArtifacts') or []
            if not artifacts or len(artifacts) > 40:
                raise NativeExecutionError('原生任务没有有效的实际交付文件。', receipt)
            files, verified, seen = [], [], set()
            for artifact in artifacts:
                path = _name(artifact['path'])
                if path.casefold() in seen or not _inside(path, scopes) or path not in changed:
                    raise NativeExecutionError('原生交付路径重复或超出分派范围：' + path, receipt)
                seen.add(path.casefold())
                actual = changed[path]
                if any(artifact.get(k) != actual[k] for k in ('bytes', 'sha256')):
                    raise NativeExecutionError('原生交付文件与工具回执哈希不一致：' + path, receipt)
                content = (workspace / path).read_text(encoding='utf8')
                if SECRET.search(content):
                    raise NativeExecutionError('原生交付含凭据，不返回会话。', receipt)
                files.append({'path': path, 'content': content})
                verified.append(actual)
            if set(changed) != {a['path'] for a in artifacts}:
                raise NativeExecutionError('原生任务留下未申报的文件，请人工核对回执。', receipt)
            envelope = {**evidence, 'engine': receipt['engine'], 'status': 'completed',
                        'model': receipt.get('model'),
                        'eventsRef': str((state / 'jobs' / task['id'] / 'events.jsonl').relative_to(root / s['id'])),
                        'threadId': receipt.get('threadId'), 'turnId': receipt.get('turnId'),
                        'projectId': receipt.get('projectId'),
                        'tools': tools, 'checks': checks, 'verifiedArtifacts': verified, 'inputUnchanged': True}
            for key in ('receiptSchema', 'workerBuild', 'subAgents', 'childEvidenceStatus', 'childEvidenceError', 'timings'):
                if key in receipt:
                    envelope[key] = receipt[key]
            receipt['nativeEvidence'] = envelope
            if preserved_receipt is None:
                _private(receipt_path, clean(receipt))
            if collection and task.get('projectId') == collection['projectId']:
                try:
                    collected = archive_job(collection, identifiers, job, receipt)
                    envelope['collection'] = collected
                    receipt['collection'] = collected
                    # Archive copies stay immutable; the original execution receipt
                    # is also unchanged by organization-only collection metadata.
                    _private(job / 'collection.json', collected)
                    event('native_collection_completed', '原生会话与工具成果已收录到「' + collection['name'] + '」。', collection=collected)
                except (OSError, ValueError, TypeError) as exc:
                    collected = {'status': 'failed', 'projectId': collection['projectId'], 'error': str(exc)}
                    envelope['collection'] = collected
                    receipt['collection'] = collected
                    _private(job / 'collection.json', collected)
                    event('native_collection_failed', '原生任务已完成，但成果归档失败：' + str(exc), collection=collected)
            event('native_result', '原生 Codex 已返回完整产物；文件指纹已核对，命令及检查结果交由质量关卡验收。', status='completed')
            result = {'summary': output['summary'], 'files': files, 'notes': output.get('observations', []),
                    'checks': [c['name'] + '：' + c['status'] + '；' + c['evidence'] for c in checks],
                    'questions': [], 'nativeEvidence': envelope}
            searches = []
            for tool in tools:
                if tool.get('searchKind') not in ('native', 'mcp'):
                    continue
                row = {key: tool[key] for key in ('query', 'action', 'resultExcerpt', 'resultChars', 'resultTruncated') if key in tool}
                row.update(toolId=tool['id'], status=tool['searchStatus'], kind=tool['searchKind'])
                for source, target in (('completedRef', 'eventRef'), ('resultRef', 'resultRef')):
                    if tool.get(source):
                        file_ref, fragment = tool[source].split('#', 1)
                        row[target] = str(Path(envelope['eventsRef']).parent / file_ref) + '#' + fragment
                searches.append(row)
            if 'web_search' in required or searches:
                result['nativeSearchEvidence'] = {'schema': 'yuki-native-search/v1', 'searches': searches}
            if shared.get('executionContract', {}).get('nativeCheckVersion') == 1:
                from native_checks import bind_required_checks
                result['nativeCheckEvidence'] = bind_required_checks(shared['executionContract'], envelope, workspace)
            return result, receipt
    except Exception as exc:
        if isinstance(exc, NativeExecutionError) and exc.receipt:
            receipt = {**receipt, **exc.receipt}
        status = receipt.get('status')
        if status in ('preparing', 'completed', None):
            status = getattr(exc, 'status', 'failed')
        receipt = {**receipt, **evidence, 'status': status, 'validationError': str(exc),
                   'requestedModel': 'native_codex', 'mode': 'native'}
        if preserved_receipt is not None:
            recovery_path = job / 'recovery-error.json'
            receipt['recoveryErrorRef'] = str(recovery_path.relative_to(root / s['id']))
            receipt['recoveryErrorPath'] = str(recovery_path)
            receipt['originalReceiptStatus'] = preserved_receipt['status']
            _private(recovery_path, clean(receipt))
        else:
            _private(receipt_path, clean(receipt))
            if collection and receipt.get('threadId') and receipt.get('projectId') == collection['projectId']:
                try:
                    collected = archive_job(collection, identifiers, job, receipt)
                    receipt['collection'] = collected
                    _private(job / 'collection.json', collected)
                    event('native_collection_completed', '本次未通过的原生执行记录已收录到「' + collection['name'] + '」。', collection=collected)
                except (OSError, ValueError, TypeError) as archive_error:
                    collected = {'status': 'failed', 'projectId': collection['projectId'], 'error': str(archive_error)}
                    receipt['collection'] = collected
                    _private(job / 'collection.json', collected)
                    event('native_collection_failed', '原生执行未通过，且归档失败：' + str(archive_error), collection=collected)
        event('native_result', '原生 Codex 本模块未通过：' + str(exc), status=status)
        raise NativeExecutionError(str(exc), receipt) from exc
