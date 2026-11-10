"""Bounded file tools in one frozen task copy; no arbitrary command execution.

The registered host grader remains trusted local code, not an OS sandbox.
Worker tests use a separate host-run namespace and never create formal quality
history. Only text files collected from the declared scope become outputs.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
import unicodedata

from app import Conflict
from live import Cancelled


MAX_FILE = 1_000_000
MAX_TOTAL = 80 * 1024 * 1024
MAX_FILES = 1000
MAX_READ = 20000
EXCLUDED = {'.git', '.lancer-data', 'node_modules', '.venv', 'venv', '__pycache__',
            '.aws', '.ssh', '.codex', 'dist', 'build', 'receipts'}
SECRET = re.compile(r'sk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
_LOCKS, _GUARD = {}, threading.Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(value):
    return _sha(_json(value).encode())


def _part(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', value):
        raise ValueError('Worker记录编号无效')
    return value


def _name(value):
    if (not isinstance(value, str) or not value or len(value) > 240 or '\\' in value or '\0' in value
            or unicodedata.normalize('NFC', value) != value):
        raise ValueError('Worker路径必须是规范相对路径')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or value == '.' or '..' in path.parts
            or any(part.startswith('.') or part in EXCLUDED for part in path.parts)
            or path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}):
        raise ValueError('Worker不访问路径别名、隐藏文件、依赖目录或凭据文件')
    return value


def _inside(path, scopes):
    return any(path == scope or scope.endswith('/') and path.startswith(scope) for scope in scopes)


def _aliases(paths):
    seen = {}
    for name in paths:
        parts = PurePosixPath(name).parts
        for count in range(1, len(parts) + 1):
            path = '/'.join(parts[:count]); key = unicodedata.normalize('NFC', path).casefold()
            if key in seen and seen[key] != path:
                raise ValueError('Worker文件存在大小写或Unicode路径别名')
            seen[key] = path
    files = set(paths)
    if any(str(parent) in files for name in files for parent in PurePosixPath(name).parents if str(parent) != '.'):
        raise ValueError('Worker文件与目录存在父子冲突')


def _atomic(path, raw, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix='.worker-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode); os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


class WorkerWorkspace:
    def __init__(self, studio, session, run, node, shared, binding):
        self.studio, self.session, self.run, self.node = studio, session, run, node
        self.shared = shared
        self.module = deepcopy(shared.get('module') or {})
        self.binding = deepcopy(binding)
        self.data_root = Path(studio.store.root)
        self.session_root = self.data_root / 'project-sessions' / _part(session['projectId']) / _part(session['id'])
        self.root = self.session_root / 'worker-workspaces' / _part(run['id']) / _part(node['id'])
        self.workspace = self.root / 'workspace'
        self.meta_path, self.state_path = self.root / 'binding.json', self.root / 'state.json'
        self.scopes = list(self.module.get('scope') or [])
        self._validate_binding()
        from worker_policy import bind_role
        if (run.get('workerToolsVersion') != 1 or self.module.get('workerBinding') != self.binding
                or bind_role(self.module, run.get('workerTools'), run.get('hostAcceptanceChecks', [])) != self.binding):
            raise ValueError('Worker授权不属于本轮冻结的项目与岗位')
        contract = shared.get('executionContract') or {}
        if (contract.get('workerToolsVersion') != 1 or contract.get('workerBinding') != self.binding or contract.get('scope') != self.scopes
                or contract.get('moduleId') != self.module.get('id') or not contract.get('fingerprint')):
            raise ValueError('Worker工具必须绑定本模块执行前冻结的合同')
        self.identity = {'schema': 'yuki-worker-workspace/v1', 'projectId': session['projectId'],
                         'sessionId': session['id'], 'runId': run['id'], 'nodeId': node['id'],
                         'projectRevision': run['projectRevision'], 'moduleId': self.module['id'],
                         'roleId': self.module['roleId'], 'contractFingerprint': contract['fingerprint'],
                         'bindingFingerprint': self.binding['fingerprint'], 'scope': self.scopes}
        self.test_run_id = 'worker-test-' + _fingerprint(self.identity)[:32]
        with _GUARD:
            self.lock = _LOCKS.setdefault(str(self.root), threading.RLock())

    def _validate_binding(self):
        b = self.binding
        if (not isinstance(b, dict) or b.get('schema') != 'yuki-worker-binding/v1' or b.get('enabled') is not True
                or b.get('fingerprint') != _fingerprint({k: v for k, v in b.items() if k != 'fingerprint'})
                or b.get('roleId') != self.module.get('roleId') or b.get('scope') != self.scopes
                or b.get('capabilities') != ['read', 'search', 'write', 'run_check']
                or b.get('roleId') in ('coordinator', 'reviewer') or str(b.get('roleId')).startswith('reviewer-')
                or not self.scopes or not b.get('checkIds')):
            raise ValueError('Worker缺少可信的岗位工具授权')
        for scope in self.scopes:
            _name(scope[:-1] if scope.endswith('/') else scope)
            if scope.rstrip('/') == 'inputs' or scope.startswith('inputs/'):
                raise ValueError('Worker不能写入只读输入目录')
        checks = b.get('checks')
        if not isinstance(checks, list) or [row.get('id') for row in checks] != b['checkIds'] or len(set(b['checkIds'])) != len(checks):
            raise ValueError('Worker固定检查授权不一致')
        for check in checks:
            if any(not _inside(_name(path), self.scopes) for path in check['files']):
                raise ValueError('Worker预检只能接收本模块scope产物')

    def _safe(self, path):
        path = Path(path)
        try: path.relative_to(self.data_root)
        except ValueError: raise ValueError('Worker记录不得越出数据目录') from None
        for item in (path, *path.parents):
            if item.is_symlink(): raise ValueError('Worker不跟随符号链接')
            if item == self.data_root: break
        return path

    def _read_regular(self, path, limit=MAX_FILE):
        path = self._safe(path)
        if not path.is_file(): raise ValueError('Worker文件不存在或不是普通文件')
        with path.open('rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError('Worker只读取普通文件')
            raw = stream.read(limit + 1)
        if len(raw) > limit: raise ValueError('Worker文件超过读取上限')
        return raw

    def _load(self, path):
        return json.loads(self._read_regular(path, MAX_TOTAL).decode('utf8'))

    def _save(self, path, value):
        self._safe(path); _atomic(path, (_json(value) + '\n').encode())

    def _secret(self, text):
        clean = getattr(self.studio.client, 'clean', lambda value: value)
        return bool(SECRET.search(text)) or clean(text) != text

    def _manifest(self, files):
        return [{'path': path, 'bytes': len(raw), 'sha256': _sha(raw)} for path, raw in sorted(files.items())]

    def _scan(self, directory):
        directory = self._safe(directory)
        if not directory.is_dir(): raise ValueError('Worker副本目录缺失')
        files = {}
        for base, directories, names in os.walk(directory, followlinks=False):
            for name in directories:
                self._safe(Path(base) / name)
                _name((Path(base) / name).relative_to(directory).as_posix())
            for name in names:
                path = Path(base) / name
                relative = _name(path.relative_to(directory).as_posix())
                files[relative] = self._read_regular(path)
        _aliases(files)
        if len(files) > MAX_FILES or sum(len(raw) for raw in files.values()) > MAX_TOTAL:
            raise ValueError('Worker副本超过文件数量或总量上限')
        return files

    def _seed(self):
        revision = self.studio.store.revision_dir(self.session['projectId'], self.run['projectRevision']) / 'workspace'
        self._safe(revision)
        files, sources, excluded = {}, {}, []
        for base, directories, names in os.walk(revision, followlinks=False):
            for name in list(directories):
                relative = (Path(base) / name).relative_to(revision).as_posix()
                try: _name(relative)
                except ValueError: directories.remove(name); continue
                self._safe(Path(base) / name)
            for name in names:
                path = Path(base) / name; relative = path.relative_to(revision).as_posix()
                try: _name(relative)
                except ValueError: continue
                self._safe(path)
                size = path.stat().st_size
                if size > MAX_FILE:
                    excluded.append({'path': relative, 'bytes': size, 'reason': 'size_limit'}); continue
                raw = self._read_regular(path)
                try: text = raw.decode('utf8')
                except UnicodeError:
                    excluded.append({'path': relative, 'bytes': size, 'reason': 'non_utf8'}); continue
                if self._secret(relative) or self._secret(text): continue
                files[relative] = raw; sources['project/' + relative] = raw
        upstream = self.shared.get('upstream')
        if not isinstance(upstream, dict) or set(upstream) != set(self.module.get('dependsOn', [])):
            raise ValueError('Worker只接收完整的直接上游交付')
        upstream_paths = set()
        for mid, output in sorted(upstream.items()):
            for item in output.get('files', []):
                path = _name(item.get('path'))
                if path in upstream_paths: raise ValueError('直接上游文件路径重复')
                upstream_paths.add(path)
                text = item.get('content')
                if not isinstance(text, str) or self._secret(text): raise ValueError('Worker上游须为无凭据UTF-8文本')
                raw = text.encode()
                if len(raw) > MAX_FILE: raise ValueError('Worker上游文件过大')
                files[path] = raw; sources['upstream/' + path] = raw
        for item in (self.shared.get('repair') or {}).get('previousFiles', []):
            path = _name(item.get('path')); text = item.get('content')
            if not _inside(path, self.scopes) or not isinstance(text, str) or self._secret(text):
                raise ValueError('Worker修复前态越界或包含不可用内容')
            raw = text.encode()
            if len(raw) > MAX_FILE: raise ValueError('Worker修复前态文件过大')
            files[path] = raw; sources['previous/' + path] = raw
        _aliases(files); _aliases(sources)
        if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL:
            raise ValueError('Worker输入超过数量或总量上限')
        return files, sources, excluded

    def prepare(self):
        with self.lock:
            self._safe(self.root)
            if self.root.exists():
                self.verify_inputs()
                _, sources, excluded = self._seed()
                meta = self._load(self.meta_path)
                if self._manifest(sources) != meta['sources'] or excluded != meta['excluded']:
                    raise ValueError('Worker冻结输入已经变化，不重建已有任务副本')
                return self._view()
            files, sources, excluded = self._seed()
            self.root.parent.mkdir(parents=True, exist_ok=True); self._safe(self.root.parent)
            self.root.mkdir(mode=0o700)
            self.workspace.mkdir()
            (self.root / 'inputs').mkdir()
            for root, rows in ((self.workspace, files), (self.root / 'inputs', sources)):
                for name, raw in rows.items():
                    path = self._safe(root / name); path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic(path, raw, 0o600 if root == self.workspace and _inside(name, self.scopes) else 0o400)
            meta = {'identity': self.identity, 'sources': self._manifest(sources),
                    'initialManifest': self._manifest(files), 'excluded': excluded}
            self._save(self.meta_path, meta)
            self._save(self.state_path, {'schema': 'yuki-worker-state/v1', 'bindingSha256': _sha(self.meta_path.read_bytes()),
                                        'revision': 0, 'manifest': self._manifest(files), 'pendingWrite': None, 'tests': {}})
            return self._view()

    def verify_inputs(self):
        with self.lock:
            meta, state = self._load(self.meta_path), self._load(self.state_path)
            if (meta.get('identity') != self.identity or state.get('schema') != 'yuki-worker-state/v1'
                    or state.get('bindingSha256') != _sha(self.meta_path.read_bytes())):
                raise ValueError('Worker副本绑定或原件已变化')
            if state.get('pendingWrite') is not None:
                raise ValueError('前次Worker写入未确认；保留原件，不自动重放或修复')
            if self._manifest(self._scan(self.root / 'inputs')) != meta['sources']:
                raise ValueError('Worker只读输入副本被修改')
            current = self._manifest(self._scan(self.workspace))
            if current != state['manifest']:
                raise ValueError('Worker工作副本发生未记录修改')
            immutable = [row for row in meta['initialManifest'] if not _inside(row['path'], self.scopes)]
            if [row for row in current if not _inside(row['path'], self.scopes)] != immutable:
                raise ValueError('Worker不得修改范围外文件')

    def _view(self):
        self.verify_inputs(); state = self._load(self.state_path)
        return {'revision': state['revision'], 'manifest': deepcopy(state['manifest']),
                'workspaceFingerprint': _fingerprint(state['manifest'])}

    def manifest(self):
        with self.lock: return self._view()['manifest']

    def fingerprint(self):
        with self.lock: return self._view()['workspaceFingerprint']

    def candidate_files(self):
        with self.lock:
            self.verify_inputs(); return self._scan(self.workspace)

    def _scope_files(self):
        return {path: raw for path, raw in self.candidate_files().items() if _inside(path, self.scopes)}

    def files(self):
        with self.lock:
            files = self._scope_files()
            if not files or len(files) > 40 or sum(map(len, files.values())) > 8 * 1024 * 1024:
                raise ValueError('Worker最终scope须包含1–40个文件且不超过8 MiB')
            return [{'path': path, 'content': raw.decode('utf8')} for path, raw in sorted(files.items())]

    def list(self, path='.', offset=0, limit=200):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Worker目录分页须为非负offset和1–200的limit')
        with self.lock:
            prefix = '' if path == '.' else _name(path).rstrip('/')
            rows = [dict(row, writable=_inside(row['path'], self.scopes), readable=True) for row in self.manifest()
                    if not prefix or row['path'] == prefix or row['path'].startswith(prefix + '/')]
            present = {row['path'] for row in rows}
            rows += [dict(row, readable=False, writable=_inside(row['path'], self.scopes))
                     for row in self._load(self.meta_path)['excluded'] if row['path'] not in present
                     and (not prefix or row['path'] == prefix or row['path'].startswith(prefix + '/'))]
            rows.sort(key=lambda row: row['path'])
            page = rows[offset:offset+limit]
            next_offset = offset + len(page) if offset + len(page) < len(rows) else None
            return {'files': page, 'total': len(rows), 'offset': offset, 'limit': limit,
                    'nextOffset': next_offset, 'truncated': next_offset is not None}

    def read(self, path, startLine=1, endLine=None):
        with self.lock:
            path = _name(path); self.verify_inputs()
            excluded = next((row for row in self._load(self.meta_path)['excluded'] if row['path'] == path), None)
            if excluded and not (self.workspace / path).exists():
                return {'path': path, 'readable': False, 'reason': excluded['reason'], 'bytes': excluded['bytes']}
            raw = self._read_regular(self.workspace / path); text = raw.decode('utf8')
            if self._secret(text): raise ValueError('Worker文件包含凭据，拒绝读取')
            lines = text.splitlines(keepends=True)
            if type(startLine) is not int or startLine < 1 or (endLine is not None and (type(endLine) is not int or endLine < startLine)):
                raise ValueError('Worker读取行范围无效')
            end = min(len(lines), endLine if endLine is not None else startLine + 199)
            selected = ''.join(lines[startLine-1:end]); content = selected[:MAX_READ]
            return {'path': path, 'readable': True, 'content': content, 'sha256': _sha(raw), 'bytes': len(raw),
                    'lineCount': len(lines), 'startLine': startLine, 'endLine': end,
                    'truncated': len(selected) > MAX_READ or end < len(lines), 'writable': _inside(path, self.scopes)}

    def search(self, query, path='.', maxResults=50):
        if not isinstance(query, str) or not query or len(query) > 1000 or type(maxResults) is not int or not 1 <= maxResults <= 200:
            raise ValueError('Worker搜索须为非空字面量与1–200条上限')
        with self.lock:
            prefix = '' if path == '.' else _name(path)
            results = []
            for name, raw in sorted(self.candidate_files().items()):
                if prefix and name != prefix and not name.startswith(prefix + '/'): continue
                text = raw.decode('utf8')
                if self._secret(text): raise ValueError('Worker文件包含凭据，拒绝搜索')
                for line, content in enumerate(text.splitlines(), 1):
                    if query in content:
                        results.append({'path': name, 'line': line, 'text': content[:2000], 'lineTruncated': len(content) > 2000})
                        if len(results) > maxResults:
                            return {'matches': results[:maxResults], 'truncated': True}
            return {'matches': results, 'truncated': False}

    def write(self, path, content, expectedSha256=None):
        with self.lock:
            path = _name(path)
            if not _inside(path, self.scopes): raise ValueError('Worker写入越出本模块scope')
            if not isinstance(content, str) or len(content.encode()) > MAX_FILE or self._secret(content):
                raise ValueError('Worker写入须为限长且不含凭据的UTF-8文本')
            files = self.candidate_files(); _aliases([*files, *([] if path in files else [path])])
            old = _sha(files[path]) if path in files else None
            if expectedSha256 != old or expectedSha256 is not None and not re.fullmatch('[a-f0-9]{64}', str(expectedSha256)):
                raise Conflict('Worker文件版本已变化，请先读取完整文件hash')
            raw = content.encode(); state = self._load(self.state_path)
            if files.get(path) == raw:
                return {'path': path, 'sha256': old, 'changed': False, 'revision': state['revision'],
                        'workspaceFingerprint': _fingerprint(state['manifest'])}
            files[path] = raw
            if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL:
                raise ValueError('Worker工作副本超过总量上限')
            state['pendingWrite'] = {'path': path, 'before': old, 'after': _sha(raw)}
            self._save(self.state_path, state)
            target = self._safe(self.workspace / path); target.parent.mkdir(parents=True, exist_ok=True)
            _atomic(target, raw)
            state.update(revision=state['revision'] + 1, manifest=self._manifest(files), pendingWrite=None)
            self._save(self.state_path, state)
            view = self._view()
            return {'path': path, 'sha256': _sha(raw), 'changed': True, 'revision': view['revision'],
                    'workspaceFingerprint': view['workspaceFingerprint']}

    def _check(self, check_id):
        check = next((row for row in self.binding['checks'] if row['id'] == check_id), None)
        if check is None: raise ValueError('Worker只能运行已冻结授权的checkId')
        return check

    def accept_changes(self, changes, expectedFingerprint):
        """Apply a verified child proposal as one durable parent revision.

        All CAS and scope checks precede writes. A crash during any write keeps
        pendingWrite set, so recovery refuses to replay a partial adoption.
        """
        with self.lock:
            if not isinstance(changes, list) or len(changes) > 40:
                raise ValueError('子任务变更必须是最多40个文件的列表')
            files = self.candidate_files()
            if _fingerprint(self._manifest(files)) != expectedFingerprint:
                raise Conflict('父Worker副本已变化，不能接纳旧子任务结果')
            proposed = dict(files); seen = set(); mutations = []
            for change in changes:
                if not isinstance(change, dict) or set(change) != {'path', 'content', 'beforeSha256'}:
                    raise ValueError('子任务变更须包含path/content/beforeSha256')
                path = _name(change['path'])
                if path in seen or not _inside(path, self.scopes):
                    raise ValueError('子任务变更重复或超出父Worker范围')
                seen.add(path); content = change['content']
                if not isinstance(content, str) or len(content.encode()) > MAX_FILE or self._secret(content):
                    raise ValueError('子任务文件不是有效的无凭据文本')
                old = _sha(files[path]) if path in files else None
                if change['beforeSha256'] != old:
                    raise Conflict('子任务文件前态与父Worker不一致')
                proposed[path] = content.encode()
                if proposed[path] != files.get(path):mutations.append((path, proposed[path]))
            _aliases(proposed)
            if len(proposed) > MAX_FILES or sum(map(len, proposed.values())) > MAX_TOTAL:
                raise ValueError('接纳子任务后超过工作副本容量')
            state = self._load(self.state_path)
            if mutations:
                state['pendingWrite'] = {'kind': 'accept_child', 'beforeFingerprint': expectedFingerprint,
                                         'afterFingerprint': _fingerprint(self._manifest(proposed)),
                                         'paths': [path for path, _ in mutations]}
                self._save(self.state_path, state)
                for path, raw in mutations:
                    destination = self._safe(self.workspace / path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _atomic(destination, raw)
                state.update(revision=state['revision'] + 1, manifest=self._manifest(proposed), pendingWrite=None)
                self._save(self.state_path, state)
            return {'changed': bool(mutations), 'paths': [path for path, _ in mutations], **self._view()}

    def run_test(self, checkId, step_id, cancel):
        from host_acceptance import run_check, verify_receipt
        with self.lock:
            check = self._check(checkId); self.verify_inputs()
            state = self._load(self.state_path); key = str(state['revision']) + ':' + checkId
            previous = state['tests'].get(key)
            if any(row['checkId'] == checkId and row['status'] in ('running', 'unknown') for row in state['tests'].values()):
                raise ValueError('前次Worker测试结果未知，不自动重放')
            if previous:
                if self.verify_test(previous): return deepcopy(previous)
                raise ValueError('Worker测试原件已变化，不自动重放')
            record = {'schema': 'yuki-worker-test/v1', 'checkId': checkId, 'stepId': str(step_id),
                      'revision': state['revision'], 'workspaceFingerprint': self.fingerprint(),
                      'testRunId': self.test_run_id, 'status': 'running', 'passed': False,
                      'receiptRef': 'host-acceptance/' + self.test_run_id + '/round-' + str(state['revision']) + '/' + checkId + '/receipt.json'}
            state['tests'][key] = record; self._save(self.state_path, state)
            receipt = None
            try:
                files = self._scope_files()
                receipt = run_check(self.studio, self.session, {'id': self.test_run_id}, check, files, state['revision'], cancel)
                self.verify_inputs()
                valid = verify_receipt(self.studio, self.session, {'id': self.test_run_id}, check, files, state['revision'], receipt)
                record.update(status=receipt['status'] if valid else 'unknown', passed=valid and receipt.get('passed') is True,
                              receipt=receipt, receiptSha256=_sha(self._read_regular(self.session_root / receipt['receiptRef'], MAX_TOTAL)))
            except Exception as error:
                record.update(status='unknown', passed=False, error=self.studio.client.clean(str(error)))
                if receipt is not None: record['receipt'] = receipt
                elif (self.session_root / record['receiptRef']).exists():
                    raw = self._read_regular(self.session_root / record['receiptRef'], MAX_TOTAL)
                    record.update(receipt=json.loads(raw), receiptSha256=_sha(raw))
                if isinstance(error, Cancelled): record['cancelled'] = True
                raise
            finally:
                state['tests'][key] = record; self._save(self.state_path, state)
            return deepcopy(record)

    def verify_test(self, record):
        from host_acceptance import verify_receipt
        with self.lock:
            try:
                self.verify_inputs(); state = self._load(self.state_path)
                if (not isinstance(record, dict) or record.get('schema') != 'yuki-worker-test/v1'
                        or record.get('testRunId') != self.test_run_id or record.get('revision') != state['revision']
                        or record.get('workspaceFingerprint') != self.fingerprint()
                        or record.get('status') not in ('passed', 'failed', 'error', 'timeout')
                        or state['tests'].get(str(state['revision']) + ':' + record['checkId']) != record):
                    return False
                receipt = record['receipt']; raw = self._read_regular(self.session_root / receipt['receiptRef'], MAX_TOTAL)
                return (record['receiptSha256'] == _sha(raw) and json.loads(raw) == receipt
                        and record['status'] == receipt['status'] and record['passed'] is receipt['passed']
                        and verify_receipt(self.studio, self.session, {'id': self.test_run_id}, self._check(record['checkId']),
                                           self._scope_files(), state['revision'], receipt))
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                return False
