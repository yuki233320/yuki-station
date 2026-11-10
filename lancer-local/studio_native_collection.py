"""Host-owned Codex project routing and immutable copies of tool handoffs.

This archive complements the native thread; it does not recreate its history.
Configuration is local host data and is never accepted from a model response.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
_SECRET = re.compile(rb'sk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in ('.', '..'):
        raise ValueError('原生归档编号无效。')
    return value


def _no_links(path):
    for part in [path, *path.parents]:
        if part.is_symlink():
            if (str(part), str(part.resolve())) in (('/var', '/private/var'), ('/tmp', '/private/tmp')):
                continue
            raise ValueError('原生归档拒绝符号链接。')


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('原生产物归档路径无效。')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in ('.', '..') or p.startswith('.') for p in path.parts):
        raise ValueError('原生产物归档路径越界。')
    return value


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf8')


def _write(path, value):
    _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _no_links(path)
    fd, name = tempfile.mkstemp(prefix='.collection-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_bytes(value))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_collection(data_root):
    path = Path(data_root) / 'native-project.json'
    _no_links(path)
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding='utf8'))
    _identifier(config.get('projectId'))
    root = Path(config.get('root', ''))
    if not root.is_absolute() or str(root) != config.get('root') or '..' in root.parts:
        raise ValueError('原生归档项目目录必须是规范的绝对路径。')
    _no_links(root)
    if not root.is_dir() or not isinstance(config.get('name'), str) or not config['name'].strip():
        raise ValueError('原生归档项目不存在或名称无效。')
    return {'version': 1, 'projectId': config['projectId'], 'name': config['name'], 'root': str(root)}


def configure_collection(data_root, project_id, name, root):
    """Called by the trusted host after actual native project creation."""
    value = {'version': 1, 'projectId': _identifier(project_id), 'name': name, 'root': str(root)}
    destination = Path(root)
    _no_links(destination)
    if not destination.is_absolute() or '..' in destination.parts or not destination.is_dir() or not isinstance(name, str) or not name.strip():
        raise ValueError('请选择已创建的原生 Codex 项目及目录。')
    _write(Path(data_root) / 'native-project.json', value)
    return load_collection(data_root)


def collection_info(data_root):
    try:
        config = load_collection(data_root)
        return {'configured': bool(config), **(config or {})}
    except (OSError, ValueError, TypeError) as exc:
        return {'configured': False, 'error': str(exc)}


def archive_job(config, identifiers, job, receipt=None, membership=None):
    """Copy exact nonsecret evidence + verified outputs; never edit original receipts.

    Historical jobs may supply host-verified membership from thread/read after a
    metadata move. New jobs use the projectId verified by the native bridge.
    """
    identifiers = [_identifier(item) for item in identifiers]
    if len(identifiers) != 4:
        raise ValueError('原生归档需要项目、会话、轮次、节点编号。')
    project_id = _identifier(config['projectId'])
    root, job = Path(config['root']), Path(job)
    _no_links(root)
    _no_links(job)
    receipt = receipt or json.loads((job / 'receipt.json').read_text(encoding='utf8'))
    thread_id = receipt.get('threadId')
    if not thread_id:
        raise ValueError('原生归档缺少真实会话编号。')
    member = membership or {**(receipt.get('projectCollection') or {}), 'threadId': thread_id,
                            'projectId': receipt.get('projectId'), 'source': 'native-bridge-receipt'}
    if member.get('threadId') != thread_id or member.get('projectId') != project_id:
        raise ValueError('原生会话尚未核实归入指定 Codex 项目。')
    files = {}
    def add(relative, source):
        relative = _relative(relative)
        source = Path(source)
        _no_links(source)
        if not source.is_file() or source.stat().st_nlink != 1 or source.stat().st_size > 16_000_000:
            raise ValueError('原生归档只接受有界普通文件。')
        payload = source.read_bytes()
        if _SECRET.search(payload):
            raise ValueError('原生归档发现凭据，拒绝复制。')
        files[relative] = payload
    for name in ('task.json', 'input-manifest.json', 'receipt.json'):
        add(name, job / name)
    task = json.loads(files['task.json'])['task']
    task_id = _identifier(task['id'])
    events = job / 'state' / 'jobs' / task_id / 'events.jsonl'
    if events.exists():
        add('events.jsonl', events)
    for child in receipt.get('subAgents', []):
        if not child.get('receiptRef'):
            continue
        path = _relative(child['receiptRef'])
        add(path, events.parent / path)
        if hashlib.sha256(files[path]).hexdigest() != child.get('receiptSha256'):
            raise ValueError('原生子任务回执与归集哈希不一致。')
    omitted = []
    for artifact in receipt.get('verifiedArtifacts') or []:
        path = artifact.get('path')
        try:
            path = _relative(path)
            add('outputs/' + path, job / 'workspace' / path)
            data = files['outputs/' + path]
            if len(data) != artifact.get('bytes') or hashlib.sha256(data).hexdigest() != artifact.get('sha256'):
                raise ValueError('原生归档产物与回执哈希不一致。')
        except (OSError, ValueError) as exc:
            if isinstance(path, str):
                files.pop('outputs/' + path, None)
            if receipt.get('status') == 'completed':
                raise
            omitted.append({'path': path, 'reason': str(exc)})
    if sum(map(len, files.values())) > 32_000_000:
        raise ValueError('原生归档超过大小上限。')
    records = [{'path': path, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
               for path, payload in sorted(files.items())]
    digest = hashlib.sha256(_bytes(records)).hexdigest()
    destination = root / 'transfers' / Path(*identifiers)
    _no_links(destination)
    manifest = {'version': 1, 'desktopProjectId': project_id, 'desktopProjectName': config['name'],
                'threadId': thread_id, 'turnId': receipt.get('turnId'), 'nativeModel': receipt.get('model'),
                'status': receipt.get('status'), 'yukiProjectId': identifiers[0], 'sessionId': identifiers[1],
                'runId': identifiers[2], 'nodeId': identifiers[3], 'sourceJob': str(job),
                'sourceReceipt': str(job / 'receipt.json'), 'membership': member,
                'notice': '原生对话保存在 Codex 项目中；此目录为雪站交接请求、工具事件、回执及产物的证据副本。',
                'evidenceHash': digest, 'files': records, 'omittedArtifacts': omitted,
                'artifactsAccepted': receipt.get('status') == 'completed'}
    manifest_path = destination / 'archive.json'
    if destination.exists():
        old = json.loads(manifest_path.read_text(encoding='utf8'))
        if old.get('evidenceHash') != digest or old.get('threadId') != thread_id or old.get('desktopProjectId') != project_id:
            raise ValueError('原生历史归档已存在，拒绝覆盖不同证据。')
        for item in old['files']:
            actual = destination / _relative(item['path'])
            _no_links(actual)
            if hashlib.sha256(actual.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('原生历史归档文件发生变化。')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix='.archive-', dir=str(destination.parent)))
        try:
            for name, payload in files.items():
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(payload)
                target.chmod(0o600)
            (staging / 'archive.json').write_bytes(_bytes(manifest))
            (staging / 'archive.json').chmod(0o600)
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    # Scan manifests rather than an unguarded read/modify/write index. Individual
    # archives remain the source of truth and are atomically created above.
    index = []
    for path in sorted((root / 'transfers').glob('*/*/*/*/archive.json')):
        _no_links(path)
        item = json.loads(path.read_text(encoding='utf8'))
        index.append({key: item.get(key) for key in ('threadId', 'turnId', 'nativeModel', 'status', 'yukiProjectId', 'sessionId', 'runId', 'nodeId')}
                     | {'archive': str(path.relative_to(root))})
    _write(root / 'transfers' / 'index.json', {'projectId': project_id, 'name': config['name'], 'entries': index})
    return {'status': 'archived', 'projectId': project_id, 'name': config['name'], 'root': str(root),
            'archivePath': str(manifest_path), 'threadId': thread_id, 'evidenceHash': digest}
