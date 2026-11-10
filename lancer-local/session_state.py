"""Authoritative session snapshots and durable dispatch intents in one database.

Legacy session JSON is imported once, never maintained as a second writable
truth. Tool receipts and published project HEAD retain their existing owners.
Recovery records an explicit-resume requirement; it never calls an executor.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import uuid
from pathlib import Path
import re
import sqlite3

from app import Conflict
from execution_attempts import interrupt_attempts, finish_attempt


SCHEMA_VERSION = 1
RECOVERY_MESSAGE = '服务已重启，已保留完成工作与派工记录。请核对回执并恢复连接后显式继续；未知外部动作不会自动重放。'
DATABASE = 'session-state.sqlite3'
AUTHORITY = 'session-state.authority.json'
IDENTIFIER = re.compile(r'[a-f0-9]{12}')
TERMINAL = {'completed', 'failed', 'stopped', 'conflict', 'interrupted'}
DISPATCH_COLUMNS = {
    'id': 'attempt_id', 'attemptId': 'attempt_id', 'projectId': 'project_id',
    'sessionId': 'session_id', 'runId': 'run_id', 'kind': 'kind',
    'status': 'status', 'cancelRequested': 'cancel_requested', 'owner': 'owner',
    'outcome': 'outcome', 'createdAt': 'created_at', 'claimedAt': 'claimed_at',
    'endedAt': 'ended_at',
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _identity(session):
    pid, sid = session.get('projectId'), session.get('id')
    if not all(isinstance(value, str) and IDENTIFIER.fullmatch(value) for value in (pid, sid)):
        raise ValueError('会话标识无效')
    if type(session.get('version')) is not int or session['version'] < 0:
        raise ValueError('会话版本无效')
    return pid, sid


def _dispatch(row):
    result = {key: row[column] for key, column in DISPATCH_COLUMNS.items()}
    result['cancelRequested'] = bool(result['cancelRequested'])
    return result


def _check_authority(root, connection=None):
    marker = Path(root) / AUTHORITY
    if not marker.exists():
        return
    if not (Path(root) / DATABASE).exists():
        raise ValueError('会话数据库缺失；已切换 SQLite 权威，拒绝回灌旧 JSON')
    identity = json.loads(marker.read_text())
    if identity.get('schema') != 'yuki-session-authority/v1' or identity.get('database') != DATABASE:
        raise ValueError('会话权威标记无效')
    if connection is not None:
        row = connection.execute("SELECT value FROM metadata WHERE key='authority_id'").fetchone()
        if row is None or row[0] != identity.get('authorityId'):
            raise ValueError('会话数据库与权威标记不匹配')


def _readonly(root):
    _check_authority(root)
    path = Path(root) / DATABASE
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        connection.close()
        raise ValueError('会话数据库版本不受支持；未回退到旧 JSON')
    try:_check_authority(root, connection)
    except Exception:
        connection.close()
        raise
    return connection


def _legacy_sessions(root, project_id=None):
    root = Path(root)
    paths = (root / project_id).glob('*.json') if project_id else root.glob('*/*.json')
    for path in sorted(paths):
        if not IDENTIFIER.fullmatch(path.parent.name) or not IDENTIFIER.fullmatch(path.stem):
            continue
        data = path.read_bytes()
        session = json.loads(data)
        pid, sid = _identity(session)
        if (pid, sid) != (path.parent.name, path.stem):
            raise ValueError('旧会话路径与内容不一致：' + str(path))
        yield path, data, session


def read_sessions(root, project_id=None):
    """Read one authoritative snapshot; no DB creation or JSON fallback on error."""
    _check_authority(root)
    if not (Path(root) / DATABASE).exists():
        return [session for _, _, session in _legacy_sessions(root, project_id)]
    connection = _readonly(root)
    try:
        query = 'SELECT payload FROM sessions'
        args = ()
        if project_id is not None:
            query += ' WHERE project_id=?'
            args = (project_id,)
        return [json.loads(row[0]) for row in connection.execute(query + ' ORDER BY project_id, session_id', args)]
    finally:
        connection.close()


def read_dispatches(root, project_id=None):
    _check_authority(root)
    if not (Path(root) / DATABASE).exists():
        return []
    connection = _readonly(root)
    try:
        query = 'SELECT * FROM dispatches'
        args = ()
        if project_id is not None:
            query += ' WHERE project_id=?'
            args = (project_id,)
        return [_dispatch(row) for row in connection.execute(query + ' ORDER BY created_at, attempt_id', args)]
    finally:
        connection.close()


def pending_dispatches(root, project_id=None):
    return [row for row in read_dispatches(root, project_id) if row['status'] in ('queued', 'running')]


class SessionStateStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / DATABASE
        _check_authority(self.root)
        with self._transaction(initialize=True) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError('会话数据库版本不受支持；未回退到旧 JSON')
            if version == 0:
                for statement in (
                    'CREATE TABLE sessions(project_id TEXT NOT NULL, session_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(project_id,session_id))',
                    'CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
                    'CREATE TABLE legacy_imports(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)',
                    '''CREATE TABLE dispatches(attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                       session_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                       status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                       owner TEXT, outcome TEXT, created_at TEXT NOT NULL, claimed_at TEXT, ended_at TEXT,
                       FOREIGN KEY(project_id,session_id) REFERENCES sessions(project_id,session_id))''',
                ):
                    db.execute(statement)
                db.execute('PRAGMA user_version=1')
            identity = db.execute("SELECT value FROM metadata WHERE key='authority_id'").fetchone()
            if identity is None:
                authority_id = uuid.uuid4().hex
                db.execute("INSERT INTO metadata VALUES('authority_id',?)", (authority_id,))
            else:authority_id = identity[0]
            _check_authority(self.root, db)
            if not db.execute("SELECT 1 FROM metadata WHERE key='legacy_import_complete'").fetchone():
                for path, data, session in _legacy_sessions(self.root):
                    pid, sid = _identity(session)
                    db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (pid, sid, session['version'], _encoded(session)))
                    db.execute('INSERT INTO legacy_imports VALUES(?,?)',
                               (str(path.relative_to(self.root)), hashlib.sha256(data).hexdigest()))
                    self._sync_dispatches(db, session)
                self._restore_dispatches(db)
                db.execute("INSERT INTO metadata VALUES('legacy_import_complete',?)", (_now(),))
        marker = self.root / AUTHORITY
        if not marker.exists():
            temporary = marker.with_suffix('.pending-' + uuid.uuid4().hex)
            with temporary.open('x') as output:
                output.write(_encoded({'schema': 'yuki-session-authority/v1', 'database': DATABASE, 'authorityId': authority_id}))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
            if os.name == 'posix':
                descriptor = os.open(self.root, os.O_RDONLY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)

    @contextmanager
    def _transaction(self, initialize=False):
        _check_authority(self.root)
        db = (sqlite3.connect(self.db_path, timeout=5, isolation_level=None) if initialize else
              sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=rw', uri=True, timeout=5, isolation_level=None))
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            if not initialize and db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise ValueError('会话数据库版本不受支持')
            if not initialize:_check_authority(self.root, db)
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def list(self, project_id=None):
        return read_sessions(self.root, project_id)

    def load(self, project_id, session_id):
        db = _readonly(self.root)
        try:
            row = db.execute('SELECT payload FROM sessions WHERE project_id=? AND session_id=?',
                             (project_id, session_id)).fetchone()
            if row is None:
                raise ValueError('会话不存在')
            return json.loads(row[0])
        finally:
            db.close()

    def write(self, session):
        with self._transaction() as db:
            self._write(db, session)

    def _write(self, db, session):
        pid, sid = _identity(session)
        row = db.execute('SELECT version FROM sessions WHERE project_id=? AND session_id=?', (pid, sid)).fetchone()
        expected = row[0] if row else 0
        if session['version'] != expected + 1:
            raise Conflict('会话状态已更新，请重新读取后操作')
        db.execute('INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(project_id,session_id) DO UPDATE SET version=excluded.version,payload=excluded.payload',
                   (pid, sid, session['version'], _encoded(session)))
        self._sync_dispatches(db, session)

    def _sync_dispatches(self, db, session):
        for run in session.get('runs', []):
            for attempt in run.get('executionAttempts', []):
                aid = attempt.get('id')
                if not isinstance(aid, str) or not aid:
                    raise ValueError('执行履历缺少编号')
                row = db.execute('SELECT * FROM dispatches WHERE attempt_id=?', (aid,)).fetchone()
                binding = (session['projectId'], session['id'], run['id'])
                if row and (row['project_id'], row['session_id'], row['run_id']) != binding:
                    raise Conflict('执行编号已绑定另一任务')
                if row is None:
                    active = attempt.get('status') == 'running'
                    db.execute('''INSERT INTO dispatches(attempt_id,project_id,session_id,run_id,kind,status,outcome,created_at,ended_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)''',
                               (aid, *binding, 'evidence_review' if attempt.get('reason') == 'evidence_review' else 'run',
                                'queued' if active else 'settled', None if active else attempt.get('status'),
                                attempt.get('startedAt') or _now(), attempt.get('endedAt')))
                elif attempt.get('status') in TERMINAL and row['status'] in ('queued', 'running'):
                    db.execute('UPDATE dispatches SET status=?, outcome=?, ended_at=? WHERE attempt_id=?',
                               ('settled', attempt['status'], attempt.get('endedAt') or _now(), aid))

    def dispatches(self, project_id=None):
        return read_dispatches(self.root, project_id)

    def queue_rows(self, attempt_ids):
        """Read current DB facts for this instance's explicitly registered intents."""
        ids = list(set(attempt_ids))
        if not ids: return []
        db = _readonly(self.root)
        try:
            result = []
            for offset in range(0, len(ids), 400):
                batch = ids[offset:offset+400]
                query = ("SELECT d.*,d.rowid AS queue_order,s.payload AS session_payload FROM dispatches d "
                         "JOIN sessions s ON s.project_id=d.project_id AND s.session_id=d.session_id "
                         "WHERE d.attempt_id IN (" + ','.join('?' for _ in batch) + ')')
                for row in db.execute(query, batch):
                    item = _dispatch(row)
                    session = json.loads(row['session_payload'])
                    run = next((r for r in session.get('runs', []) if r['id'] == item['runId']), {})
                    attempt = next((a for a in run.get('executionAttempts', []) if a['id'] == item['attemptId']), {})
                    item.update(queueOrder=row['queue_order'], queuedAt=attempt.get('queuedAt', item['createdAt']),
                                admittedAt=attempt.get('admittedAt'), queueWaitMs=attempt.get('queueWaitMs'))
                    result.append(item)
            return sorted(result, key=lambda item: item['queueOrder'])
        finally: db.close()

    def claim(self, attempt_id, owner, session_update=None):
        if not owner:
            raise ValueError('派工缺少运行实例编号')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM dispatches WHERE attempt_id=?', (attempt_id,)).fetchone()
            if row is None or row['status'] != 'queued' or row['cancel_requested']:
                return False
            session = None
            if session_update is not None:
                saved = db.execute('SELECT payload FROM sessions WHERE project_id=? AND session_id=?',
                                   (row['project_id'], row['session_id'])).fetchone()
                session = json.loads(saved[0])
                run = next(r for r in session['runs'] if r['id'] == row['run_id'])
                attempt = next(a for a in run['executionAttempts'] if a['id'] == attempt_id)
                if attempt['status'] != 'running': return False
                # Validation and admission metadata share the claim transaction.
                # The callback may only inspect local state and mutate this copy.
                session_update(session, run, attempt)
                session['version'] += 1
                session['updated'] = _now()
            changed = db.execute('''UPDATE dispatches SET status='running',owner=?,claimed_at=?
                                    WHERE attempt_id=? AND status='queued' AND cancel_requested=0''',
                                 (owner, _now(), attempt_id)).rowcount
            if changed and session is not None:
                self._write(db, session)
                return deepcopy(session)
            return changed == 1

    def settle_dispatch(self, attempt_id, status, error, owner=None, queued_only=False, cancelled=False):
        """Settle without executing; conditional status/owner checks prevent revival."""
        if status not in TERMINAL: raise ValueError('Invalid dispatch terminal outcome')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM dispatches WHERE attempt_id=?', (attempt_id,)).fetchone()
            if row is None or row['status'] not in ('queued', 'running'):
                return None
            if queued_only and row['status'] != 'queued': return None
            if row['status'] == 'running' and row['owner'] != owner: return None
            saved = db.execute('SELECT payload FROM sessions WHERE project_id=? AND session_id=?',
                               (row['project_id'], row['session_id'])).fetchone()
            session = json.loads(saved[0])
            run = next(r for r in session['runs'] if r['id'] == row['run_id'])
            attempt = next(a for a in run['executionAttempts'] if a['id'] == attempt_id)
            now = _now()
            session.update(status=status, error=error)
            run.update(status=status, ended=now)
            for node in run.get('nodes', []):
                if node.get('status') == 'running':
                    node.update(status='interrupted' if status in ('stopped','interrupted') else 'failed', error=error)
            if attempt.get('admissionVersion') == 1: attempt['admissionState'] = status
            for review in run.get('evidenceReviews', []):
                if review.get('status') in ('queued', 'running'):
                    review.update(status=status, error=error, completed=now)
            finish_attempt(run, now)
            if status == 'interrupted':
                session['recovery'] = {'schema':'yuki-recovery/v1','requiresExplicitResume':True,
                                       'nextAction':'retry_dispatch_after_storage_recovery'}
            session.setdefault('events', []).append({'seq':len(session.get('events', []))+1,
                'type':'task_cancelled' if cancelled else 'task_admission_failed', 'detail':error,
                'node':None,'runId':run['id'],'time':now})
            if cancelled:
                db.execute('UPDATE dispatches SET cancel_requested=1 WHERE attempt_id=?', (attempt_id,))
            session['version'] += 1; session['updated'] = now
            self._write(db, session)
            return deepcopy(session)

    def request_cancel(self, project_id, session_id):
        with self._transaction() as db:
            return db.execute("UPDATE dispatches SET cancel_requested=1 WHERE project_id=? AND session_id=? AND status IN ('queued','running')",
                              (project_id, session_id)).rowcount

    def recover(self):
        """Atomically reconcile persisted work. Explicit resume creates a new intent."""
        changed = 0
        now = _now()
        with self._transaction() as db:
            for row in db.execute('SELECT payload FROM sessions').fetchall():
                session = json.loads(row[0])
                dirty = False
                for run in session.get('runs', []):
                    was_running = run.get('status') in ('queued', 'running')
                    cancelled = was_running and db.execute("SELECT 1 FROM dispatches WHERE project_id=? AND session_id=? AND run_id=? AND cancel_requested=1 AND status IN ('queued','running')", (session['projectId'],session['id'],run['id'])).fetchone()
                    if cancelled:run.update(status='stopped',ended=now)
                    updates = interrupt_attempts(run, now, 'service_restart')
                    dirty = bool(updates) or dirty
                    if was_running:
                        run['status'] = 'stopped' if cancelled else 'interrupted'
                        dirty = True
                    for node in run.get('nodes', []):
                        if was_running and node.get('status') == 'running':
                            node['status'] = 'interrupted'
                    for review in run.get('evidenceReviews', []):
                        if review.get('status') in ('queued', 'running'):
                            review.update(status='interrupted', interruptedAt=now, error=RECOVERY_MESSAGE)
                            dirty = True
                    for attempt in updates:
                        if attempt.get('admissionVersion') == 1:
                            attempt['admissionState'] = attempt['status']
                        if attempt['status'] == 'interrupted':
                            db.execute("UPDATE dispatches SET status='awaiting_resume',outcome='interrupted',ended_at=? WHERE attempt_id=? AND status IN ('queued','running')",
                                       (now, attempt['id']))
                if session.get('status') in ('queued', 'running'):
                    last = (session.get('runs') or [{}])[-1]
                    session['status'] = 'stopped' if last.get('status') == 'stopped' else 'interrupted'
                    session['error'] = RECOVERY_MESSAGE
                    session['recovery'] = {'schema': 'yuki-recovery/v1', 'at': now, 'requiresExplicitResume': True,
                                           'nextAction': 'reconcile_delivery' if last.get('commitIntent') else 'reconnect_and_verify_receipts'}
                    dirty = True
                if dirty:
                    session['version'] += 1
                    session['updated'] = now
                    self._write(db, session)
                    changed += 1
        return changed

    def _restore_dispatches(self, db):
        for path in self.root.glob('*/dispatch-state.json'):
            payload = json.loads(path.read_text())
            if payload.get('schema') != 'yuki-dispatch-export/v1' or not isinstance(payload.get('dispatches'), list):
                raise ValueError('备份派工记录格式无效')
            for item in payload['dispatches']:
                if item.get('projectId') != path.parent.name or item.get('status') not in ('settled', 'awaiting_resume'):
                    raise ValueError('备份不能携带活动或跨项目派工授权')
                row = db.execute('SELECT * FROM dispatches WHERE attempt_id=?', (item.get('attemptId'),)).fetchone()
                if row is None or (row['project_id'], row['session_id'], row['run_id']) != (item['projectId'], item.get('sessionId'), item.get('runId')):
                    raise ValueError('备份派工记录没有匹配的会话执行履历')
                db.execute('UPDATE dispatches SET status=?,cancel_requested=?,owner=NULL,outcome=?,claimed_at=?,ended_at=? WHERE attempt_id=?',
                           (item['status'], int(bool(item.get('cancelRequested'))), item.get('outcome'),
                            item.get('claimedAt'), item.get('endedAt'), item['attemptId']))
