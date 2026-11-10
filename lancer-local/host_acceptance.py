"""Locally approved host graders and durable evidence, never model commands.

A registered Python script is TRUSTED LOCAL CODE, not an OS sandbox. Registration
copies one explicit file without executing it. Execution uses a private artifact
copy, isolated Python, an environment without inherited credentials and (on
macOS/Linux) a process group. A grader can still exercise the local user's OS
permissions; do not register untrusted/model-proposed scripts. Windows execution
is currently rejected because group cleanup has not been implemented there.

The service's data-directory lock owns cross-process serialization. Registry
operations additionally coordinate instances within the process. Job creation
is exclusive, and an existing job is never automatically dispatched again.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time

from live import Cancelled

SCHEMA = 'yuki-host-acceptance/v1'
MAX_OUTPUT = 1024 * 1024
MAX_SCRIPT = 2 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
MAX_FILES = 80 * 1024 * 1024
CHECK_KEYS = ('id', 'name', 'files', 'criteria', 'scriptSha256', 'timeoutSeconds')
TERMINAL = {'passed', 'failed', 'error', 'timeout'}
TRUST_NOTICE = '宿主验收脚本是本机明确批准的可信代码，不是操作系统沙箱；不执行模型提出的命令。'
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(value):
    return _sha(_json(value).encode('utf8'))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON contains duplicate object keys')
        result[key] = value
    return result


def _read_json(path, limit=8 * 1024 * 1024):
    raw = _read_regular(path, limit)
    return json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid JSON number')))


def _read_regular(path, limit):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Evidence files must not be symlinks')
    with path.open('rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Evidence must be a regular file')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Evidence file exceeds its size limit')
    return raw


def _sync_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError:
        pass


def _atomic(path, raw):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def _write_json(path, value):
    _atomic(path, (_json(value) + '\n').encode('utf8'))


def _id(value, label='id'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', value):
        raise ValueError('Invalid host acceptance ' + label)
    return value


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise ValueError('Invalid host acceptance ' + label)
    return value


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise ValueError('Artifact path must be a canonical relative path')
    rel = PurePosixPath(value)
    if rel.is_absolute() or '..' in rel.parts or str(rel) != value or value == '.':
        raise ValueError('Artifact path must stay inside its workspace')
    return value


def _child(root, relative):
    path = Path(root) / _relative(relative)
    for parent in (path, *path.parents):
        if parent == root: break
        if parent.is_symlink(): raise ValueError('Host acceptance paths must not contain symlinks')
    return path


def _check(value):
    if not isinstance(value, dict) or set(value) != set(CHECK_KEYS):
        raise ValueError('Frozen host acceptance fields are invalid')
    _id(value['id']); _text(value['name'], 'name', 160)
    if (not isinstance(value['files'], list) or not 1 <= len(value['files']) <= 256
            or any(not isinstance(path, str) for path in value['files'])
            or len(set(value['files'])) != len(value['files'])):
        raise ValueError('Host acceptance files must be unique nonempty relative paths')
    for path in value['files']: _relative(path)
    criteria = value['criteria']
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 256:
        raise ValueError('Host acceptance criteria are required')
    ids = []
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != {'id', 'description'}:
            raise ValueError('Each criterion needs only id and description')
        ids.append(_id(criterion['id'], 'criterion id'))
        _text(criterion['description'], 'criterion description', 4000)
    if len(set(ids)) != len(ids): raise ValueError('Duplicate host acceptance criterion id')
    if type(value['timeoutSeconds']) is not int or not 1 <= value['timeoutSeconds'] <= 300:
        raise ValueError('Host acceptance timeout must be an integer from 1 to 300 seconds')
    if not isinstance(value['scriptSha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['scriptSha256']):
        raise ValueError('Invalid approved script hash')
    return deepcopy(value)


class HostAcceptanceRegistry:
    def __init__(self, data_root):
        self.root = Path(data_root).resolve()
        self.path = self.root / 'host-acceptance-registry.json'
        self.scripts = self.root / 'host-acceptance-scripts'
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.root), threading.RLock())

    def _load(self):
        if not self.path.exists(): return []
        document = _read_json(self.path)
        if not isinstance(document, dict) or set(document) != {'schema', 'checks'} or document['schema'] != SCHEMA + '/registry':
            raise ValueError('Unsupported host acceptance registry')
        entries = document['checks']
        if not isinstance(entries, list): raise ValueError('Invalid host acceptance registry')
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {*CHECK_KEYS, 'registeredAt'}:
                raise ValueError('Invalid host acceptance registry entry')
            _check({key: entry[key] for key in CHECK_KEYS})
            if entry['id'] in seen: raise ValueError('Duplicate registered check id')
            seen.add(entry['id'])
        return entries

    def register(self, request):
        if not isinstance(request, dict) or set(request) - {'id', 'name', 'scriptPath', 'files', 'criteria', 'timeoutSeconds'}:
            raise ValueError('Register only a local script and explicit acceptance metadata')
        path = Path(_text(request.get('scriptPath'), 'absolute script path', 4000))
        if not path.is_absolute() or path.suffix.lower() != '.py':
            raise ValueError('Select an absolute single Python script path')
        raw = _read_regular(path, MAX_SCRIPT)
        raw.decode('utf8')
        check = _check({'id': request.get('id') or secrets.token_hex(6), 'name': request.get('name'),
                        'files': request.get('files'), 'criteria': request.get('criteria'),
                        'scriptSha256': _sha(raw), 'timeoutSeconds': request.get('timeoutSeconds', 30)})
        with self.lock:
            entries = self._load()
            self.root.mkdir(parents=True, exist_ok=True)
            if self.scripts.is_symlink(): raise ValueError('Approved script directory cannot be a symlink')
            self.scripts.mkdir(exist_ok=True)
            approved = self.scripts / (check['scriptSha256'] + '.py')
            if approved.exists():
                if _read_regular(approved, MAX_SCRIPT) != raw:
                    raise ValueError('Existing approved script copy has changed')
            else:
                _atomic(approved, raw)
            entry = {**check, 'registeredAt': _now()}
            entries = [row for row in entries if row['id'] != entry['id']] + [entry]
            _write_json(self.path, {'schema': SCHEMA + '/registry', 'checks': entries})
            return deepcopy(entry)

    def info(self):
        with self.lock:
            return deepcopy(self._load())

    def snapshot(self, ids):
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('Host acceptance selection must be a unique id list')
        with self.lock:
            entries = {entry['id']: entry for entry in self._load()}
            checks = []
            for check_id in ids:
                if check_id not in entries: raise ValueError('Host acceptance check is not registered: ' + check_id)
                check = {key: deepcopy(entries[check_id][key]) for key in CHECK_KEYS}
                self.authorize(check)
                checks.append(check)
            return checks

    def authorize(self, check):
        frozen = _check(check)
        with self.lock:
            entry = next((entry for entry in self._load() if entry['id'] == frozen['id']), None)
            if entry is None or {key: entry[key] for key in CHECK_KEYS} != frozen:
                raise ValueError('Host acceptance authorization was removed or changed: ' + frozen['id'])
            path = _child(self.root, 'host-acceptance-scripts/' + frozen['scriptSha256'] + '.py')
            if _sha(_read_regular(path, MAX_SCRIPT)) != frozen['scriptSha256']:
                raise ValueError('Approved host acceptance script hash changed')
            return path

    def remove(self, check_id):
        _id(check_id)
        with self.lock:
            entries = self._load()
            entry = next((entry for entry in entries if entry['id'] == check_id), None)
            if entry is None: raise ValueError('Host acceptance check is not registered')
            _write_json(self.path, {'schema': SCHEMA + '/registry',
                                    'checks': [row for row in entries if row['id'] != check_id]})
            return deepcopy(entry)


def _artifacts(files):
    if not isinstance(files, dict) or len(files) > 10000 or any(not isinstance(path, str) for path in files):
        raise ValueError('Host acceptance candidates must be a path-to-bytes mapping')
    manifest = []
    for path, raw in sorted(files.items()):
        _relative(path)
        if type(raw) is not bytes or len(raw) > MAX_FILE:
            raise ValueError('Candidate files must be bytes and at most 20 MiB each')
        manifest.append({'path': path, 'bytes': len(raw), 'sha256': _sha(raw)})
    if sum(row['bytes'] for row in manifest) > MAX_FILES:
        raise ValueError('Candidate files exceed 80 MiB')
    return manifest


def _workspace_state(workspace):
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError('Candidate workspace must remain an ordinary directory')
    rows = []
    for path in sorted(workspace.rglob('*')):
        if path.is_symlink(): raise ValueError('Grader introduced a workspace symlink')
        info = path.stat()
        row = {'path': path.relative_to(workspace).as_posix(), 'mode': stat.S_IMODE(info.st_mode)}
        if path.is_file():
            raw = _read_regular(path, MAX_FILE)
            row.update(kind='file', bytes=len(raw), sha256=_sha(raw))
        elif path.is_dir(): row['kind'] = 'directory'
        else: raise ValueError('Grader introduced a non-regular workspace entry')
        rows.append(row)
    return rows


def _unknown(base, detail):
    return {**deepcopy(base), 'status': 'unknown', 'passed': False, 'executed': None,
            'startedAt': None, 'endedAt': None, 'elapsedMs': None, 'error': detail}


def _seal(job, receipt):
    raw = (_json(receipt) + '\n').encode('utf8')
    _atomic(job / 'receipt.json', raw)
    _atomic(job / 'receipt.sha256', (_sha(raw) + '\n').encode('ascii'))
    return receipt


def _cached(job, expected):
    try:
        raw = _read_regular(job / 'receipt.json', 8 * 1024 * 1024)
        if _read_regular(job / 'receipt.sha256', 128).decode('ascii').strip() != _sha(raw):
            raise ValueError('Saved receipt checksum mismatch')
        receipt = _read_json(job / 'receipt.json')
        if receipt.get('status') not in TERMINAL:
            raise ValueError('Existing execution has no safely reusable terminal outcome')
        if (not isinstance(receipt.get('endedAt'), str) or not receipt['endedAt']
                or type(receipt.get('elapsedMs')) not in (int, float)
                or not math.isfinite(receipt['elapsedMs']) or receipt['elapsedMs'] < 0):
            raise ValueError('Saved receipt has no complete terminal timing')
        for key in ('id', 'round', 'bindingFingerprint', 'artifactFingerprint', 'artifactManifest',
                    'scriptSha256', 'receiptRef', 'stdoutRef', 'stderrRef'):
            if receipt.get(key) != expected[key]: raise ValueError('Frozen execution binding changed')
        manifest_raw = _read_regular(job / 'artifacts.json', 8 * 1024 * 1024)
        if _sha(manifest_raw) != receipt['artifactManifestSha256']:
            raise ValueError('Saved artifact manifest changed')
        artifacts = _read_json(job / 'artifacts.json')
        if artifacts['artifactManifest'] != expected['artifactManifest']:
            raise ValueError('Saved artifact list changed')
        if _workspace_state(job / 'workspace') != artifacts['workspaceState']:
            raise ValueError('Saved candidate workspace changed')
        for name in ('stdout', 'stderr'):
            if _sha(_read_regular(job / (name + '.log'), MAX_OUTPUT)) != receipt[name + 'Sha256']:
                raise ValueError('Saved grader output changed')
        if type(receipt.get('passed')) is not bool or receipt['passed'] != (receipt['status'] == 'passed'):
            raise ValueError('Saved grader outcome is inconsistent')
        if receipt['status'] in ('passed', 'failed') and receipt.get('executed'):
            status, criteria = _report(job / 'stdout.log', expected['check'], receipt['exitCode'])
            if status != receipt['status'] or criteria != receipt['criteria'] or receipt.get('workspaceUnchanged') is not True:
                raise ValueError('Saved receipt does not match the raw grader report')
        if receipt['status'] == 'passed' and receipt.get('executed') is not True:
            raise ValueError('A passed receipt needs actual grader execution')
        return receipt
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        return _unknown(expected, 'Existing host acceptance cannot be replayed: ' + str(error))


def _stop_group(process):
    """Terminate descendants too, including ones outliving an exited leader."""
    clean = True
    began = time.monotonic()
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    except OSError: clean = False
    # macOS returns EPERM for killpg on a group containing only our unreaped
    # zombie. Reap the leader before escalation; still signal the group below
    # because descendants can remain after the leader exits.
    try: process.wait(timeout=0.1)
    except subprocess.TimeoutExpired: pass
    remaining = 0.1 - (time.monotonic() - began)
    if remaining > 0: time.sleep(remaining)
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    except OSError:
        clean = False
        if process.poll() is None:
            try: process.kill()
            except OSError: pass
    try: process.wait(timeout=2)
    except subprocess.TimeoutExpired: return False
    return clean


class _LaunchFailed(RuntimeError):
    pass


def _execute(script, job, timeout, cancel):
    process = None
    reason = None
    counts = {'stdout': 0, 'stderr': 0}
    cleanup = True
    with (job / 'stdout.log').open('wb') as stdout, (job / 'stderr.log').open('wb') as stderr:
        streams = {'stdout': stdout, 'stderr': stderr}
        selector = selectors.DefaultSelector()
        try:
            if cancel.is_set(): return None, 'cancelled', counts, True
            environment = {'PATH': os.defpath, 'HOME': str(job / 'home'), 'TMPDIR': str(job / 'tmp'),
                           'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
            try:
                process = subprocess.Popen([sys.executable, '-I', '-B', str(script), '--workspace', str(job / 'workspace')],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(job), env=environment, close_fds=True, start_new_session=True)
            except OSError as error:
                raise _LaunchFailed(str(error)) from error
            for name, pipe in [('stdout', process.stdout), ('stderr', process.stderr)]:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            deadline = time.monotonic() + timeout
            while selector.get_map() or process.poll() is None:
                if cancel.is_set(): reason = 'cancelled'; break
                if time.monotonic() >= deadline: reason = 'timeout'; break
                for key, _ in selector.select(0.05):
                    try: chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError: continue
                    if not chunk:
                        selector.unregister(key.fileobj); key.fileobj.close(); continue
                    name = key.data
                    remaining = max(0, MAX_OUTPUT - counts[name])
                    streams[name].write(chunk[:remaining]); counts[name] += len(chunk)
                    if counts[name] > MAX_OUTPUT: reason = 'output_limit'; break
                if reason: break
        finally:
            if process is not None:
                cleanup = _stop_group(process)
                # Preserve all output that fits after termination, without an
                # unbounded communicate() allocation or inherited-pipe wait.
                for key in list(selector.get_map().values()):
                    try:
                        while True:
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk: break
                            remaining = max(0, MAX_OUTPUT - counts[key.data])
                            streams[key.data].write(chunk[:remaining]); counts[key.data] += len(chunk)
                    except (BlockingIOError, OSError): pass
                    finally: key.fileobj.close()
            selector.close()
            for stream in streams.values(): stream.flush(); os.fsync(stream.fileno())
    if any(count > MAX_OUTPUT for count in counts.values()) and reason != 'cancelled': reason = 'output_limit'
    return process.returncode if process else None, reason, counts, cleanup


def _report(stdout, check, exit_code):
    raw = _read_regular(stdout, MAX_OUTPUT)
    report = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid JSON number')))
    if not isinstance(report, dict) or type(report.get('accepted')) is not bool or not isinstance(report.get('criteria'), list):
        raise ValueError('Grader must return accepted:boolean and a criteria list')
    rows = {}
    for row in report['criteria']:
        if not isinstance(row, dict): raise ValueError('Invalid grader criterion row')
        key = row.get('criterionId', row.get('id'))
        if not isinstance(key, str) or key in rows or row.get('status') not in ('passed', 'failed'):
            raise ValueError('Grader criterion ids/statuses must be unique and explicit')
        if 'id' in row and 'criterionId' in row and row['id'] != row['criterionId']:
            raise ValueError('Ambiguous grader criterion id')
        rows[key] = row
    if set(rows) != {criterion['id'] for criterion in check['criteria']}:
        raise ValueError('Grader criteria do not exactly cover the frozen criteria')
    all_passed = all(row['status'] == 'passed' for row in rows.values())
    passed_count = sum(row['status'] == 'passed' for row in rows.values())
    for key, expected in [('passed', passed_count), ('total', len(rows))]:
        if key in report and (type(report[key]) is not int or report[key] != expected):
            raise ValueError('Grader summary counts disagree with its criteria')
    if report['accepted'] != all_passed or exit_code not in (0, 1) or (exit_code != 0 and report['accepted']):
        raise ValueError('Grader accepted/status/exit code are inconsistent')
    criteria = []
    detail_budget = 8000
    for criterion in check['criteria']:
        raw_row = rows[criterion['id']]
        detail = raw_row.get('detail', raw_row.get('failures', raw_row.get('reason')))
        row = {'criterionId': criterion['id'], 'description': criterion['description'][:300], 'status': raw_row['status']}
        if len(criterion['description']) > 300: row['descriptionTruncated'] = True
        if detail is not None:
            detail = detail if isinstance(detail, str) else _json(detail)
            take = min(1000, detail_budget)
            row['detail'] = detail[:take]
            detail_budget -= len(row['detail'])
            if len(detail) > take: row['detailTruncated'] = True
        criteria.append(row)
    return 'passed' if all_passed else 'failed', criteria


def run_check(studio, session, run, check, files, round_no, cancel):
    frozen = _check(check)
    if type(round_no) is not int or round_no < 0 or round_no > 100000:
        raise ValueError('Invalid host acceptance round')
    registry = HostAcceptanceRegistry(studio.store.root)
    script = registry.authorize(frozen)
    project_id, session_id, run_id = (_id(session['projectId'], 'project id'),
                                     _id(session['id'], 'session id'), _id(run['id'], 'run id'))
    session_root = _child(Path(studio.project_dir(project_id)), session_id)
    session_root.mkdir(parents=True, exist_ok=True)
    relative = 'host-acceptance/' + run_id + '/round-' + str(round_no) + '/' + frozen['id']
    job = _child(session_root, relative)
    manifest = _artifacts(files)
    files = dict(files)
    binding = {'projectId': project_id, 'sessionId': session_id, 'runId': run_id,
               'round': round_no, 'check': frozen, 'artifactManifest': manifest}
    receipt = {'schema': SCHEMA + '/receipt', 'id': frozen['id'], 'name': frozen['name'],
               'status': 'running', 'passed': False, 'executed': False, 'exitCode': None,
               'criteria': [], 'scriptSha256': frozen['scriptSha256'], 'round': round_no,
               'artifactManifest': manifest, 'artifactFingerprint': _fingerprint(manifest),
               'bindingFingerprint': _fingerprint(binding), 'startedAt': _now(), 'endedAt': None, 'elapsedMs': None,
               'receiptRef': relative + '/receipt.json', 'stdoutRef': relative + '/stdout.log',
               'stderrRef': relative + '/stderr.log', 'workspaceUnchanged': None}
    job.parent.mkdir(parents=True, exist_ok=True)
    try: job.mkdir()
    except FileExistsError:
        if cancel.is_set(): raise Cancelled('宿主验收已取消；未重新执行历史检查')
        return _cached(job, {**receipt, 'check': frozen})
    started = time.monotonic()
    workspace = job / 'workspace'
    try:
        workspace.mkdir(); (job / 'home').mkdir(); (job / 'tmp').mkdir()
        for path, raw in files.items():
            target = _child(workspace, path); target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream: stream.write(raw)
        state = _workspace_state(workspace)
        _write_json(job / 'artifacts.json', {'artifactManifest': manifest, 'workspaceState': state})
        receipt['artifactManifestSha256'] = _sha((job / 'artifacts.json').read_bytes())
        for name in ('stdout', 'stderr'): (job / (name + '.log')).touch(mode=0o600)
        # Durable intent precedes even Popen. If this process dies now, future
        # calls observe unknown work and never dispatch it automatically.
        _write_json(job / 'receipt.json', receipt)
        missing = sorted(set(frozen['files']) - set(files))
        if cancel.is_set():
            receipt.update(status='unknown', cancelled=True, error='Host acceptance cancelled before dispatch')
        elif missing:
            receipt.update(status='failed', basis='preflight_missing_artifacts', missingArtifacts=missing,
                           criteria=[{'criterionId': c['id'], 'description': c['description'][:300],
                                      'descriptionTruncated': len(c['description']) > 300, 'status': 'failed',
                                      'detail': ('Missing candidate files: ' + ', '.join(missing))[:min(1000, 8000 // len(frozen['criteria']))],
                                      'detailTruncated': len('Missing candidate files: ' + ', '.join(missing)) > min(1000, 8000 // len(frozen['criteria']))}
                                     for c in frozen['criteria']])
        elif os.name != 'posix':
            receipt.update(status='error', error='Host acceptance process-group execution currently requires macOS/Linux')
        else:
            registry.authorize(frozen)
            receipt['executed'] = True
            exit_code, reason, counts, cleanup = _execute(script, job, frozen['timeoutSeconds'], cancel)
            receipt.update(exitCode=exit_code, outputBytes=counts, processGroupReaped=cleanup)
            if reason == 'cancelled' or cancel.is_set():
                receipt.update(status='unknown', cancelled=True, error='Host acceptance cancelled; process group stopped')
            elif not cleanup:
                receipt.update(status='unknown', error='Host acceptance process cleanup could not be confirmed')
            elif reason == 'timeout': receipt.update(status='timeout', error='Host acceptance exceeded its time limit')
            elif reason == 'output_limit': receipt.update(status='error', error='Grader stdout/stderr exceeded 1 MiB', outputTruncated=True)
            else:
                receipt['status'], receipt['criteria'] = _report(job / 'stdout.log', frozen, exit_code)
        receipt['workspaceUnchanged'] = _workspace_state(workspace) == state
        if not receipt['workspaceUnchanged'] and not receipt.get('cancelled'):
            receipt.update(status='error', error='Grader modified its candidate workspace')
        registry.authorize(frozen)
    except Exception as error:
        cancelled = receipt.get('cancelled') or cancel.is_set()
        receipt.update(status='unknown' if cancelled else 'error', error=str(error)[:4000])
        if isinstance(error, _LaunchFailed): receipt['executed'] = False
        if cancelled: receipt['cancelled'] = True
    finally:
        receipt.update(endedAt=_now(), elapsedMs=round((time.monotonic() - started) * 1000, 3))
        receipt['passed'] = receipt['status'] == 'passed'
        for name in ('stdout', 'stderr'):
            path = job / (name + '.log')
            if not path.exists(): path.touch(mode=0o600)
            receipt[name + 'Sha256'] = _sha(_read_regular(path, MAX_OUTPUT))
        _seal(job, receipt)
    if receipt.get('cancelled'):
        raise Cancelled('宿主验收已取消，已保存回执；未知执行不自动重放')
    return receipt


def verify_receipt(studio, session, run, check, files, round_no, receipt):
    """Purely read saved evidence; missing evidence never launches a grader."""
    try:
        frozen = _check(check)
        if type(round_no) is not int or not 0 <= round_no <= 100000:
            return False
        registry = HostAcceptanceRegistry(studio.store.root)
        registry.authorize(frozen)
        project_id, session_id, run_id = (_id(session['projectId']), _id(session['id']), _id(run['id']))
        session_root = _child(registry.root, 'project-sessions/' + project_id + '/' + session_id)
        relative = 'host-acceptance/' + run_id + '/round-' + str(round_no) + '/' + frozen['id']
        job = _child(session_root, relative)
        manifest = _artifacts(files)
        binding = {'projectId': project_id, 'sessionId': session_id, 'runId': run_id,
                   'round': round_no, 'check': frozen, 'artifactManifest': manifest}
        expected = {'id': frozen['id'], 'round': round_no, 'check': frozen,
                    'bindingFingerprint': _fingerprint(binding), 'artifactFingerprint': _fingerprint(manifest),
                    'artifactManifest': manifest, 'scriptSha256': frozen['scriptSha256'],
                    'receiptRef': relative + '/receipt.json', 'stdoutRef': relative + '/stdout.log',
                    'stderrRef': relative + '/stderr.log'}
        cached = _cached(job, expected)
        return cached.get('status') in TERMINAL and cached == receipt
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False
