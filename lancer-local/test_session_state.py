"""Transactional session authority and dispatch recovery, without model calls."""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Conflict
from session_state import SessionStateStore, pending_dispatches, read_sessions


PROJECT = 'a' * 12
SESSION = 'b' * 12
RUN = 'c' * 12
ATTEMPT = 'd' * 12
NOW = '2026-11-02T12:00:00+00:00'


def session_record(sid=SESSION, pid=PROJECT, version=1, running=False, attempt_id=ATTEMPT):
    session = {'id': sid, 'projectId': pid, 'title': '本地事务测试', 'version': version,
               'status': 'running' if running else 'ready', 'created': NOW, 'updated': NOW,
               'events': [], 'messages': [], 'agents': {}, 'memory': [], 'runs': []}
    if running:
        session['runs'] = [{'id': RUN, 'status': 'running', 'started': NOW, 'mode': 'live',
                            'executionAttemptsHistory': 'complete', 'nodes': [
                                {'id': 'build-saved', 'status': 'done', 'result': {'summary': '已保存'},
                                 'receipt': {'status': 'completed', 'receiptRef': 'native/saved/receipt.json'}},
                                {'id': 'build-pending', 'status': 'running',
                                 'receipt': {'status': 'unknown', 'receiptRef': 'mcp/pending/receipt.json'}}],
                            'executionAttempts': [{'id': attempt_id, 'number': 1, 'reason': 'start',
                                                   'status': 'running', 'startedAt': NOW,
                                                   'runtimeBuild': {'fingerprint': 'fixture'}}]}]
    return session


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'project-sessions'
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def legacy(self, payload, path=None):
        path = path or self.root / payload['projectId'] / (payload['id'] + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ('  ' + json.dumps(payload, ensure_ascii=False, indent=3) + '\n\n').encode()
        path.write_bytes(raw)
        return path, raw

    def dispatch(self, store, attempt_id=ATTEMPT):
        return next(row for row in store.dispatches() if row['attemptId'] == attempt_id)

    def test_migration_is_once_json_bytes_stay_and_later_json_is_not_authority(self):
        original = session_record(version=7)
        path, raw = self.legacy(original)
        # Non-session metadata and names are outside the strict import set.
        self.legacy({'not': 'a session'}, self.root / PROJECT / 'project.json')
        self.legacy(session_record(), self.root / 'not-a-project' / (SESSION + '.json'))
        self.legacy(session_record(), self.root / PROJECT / 'not-a-session.json')
        store = SessionStateStore(self.root)
        self.assertEqual(store.load(PROJECT, SESSION), original)
        self.assertEqual(len(store.list()), 1)
        self.assertEqual(path.read_bytes(), raw)
        update = copy.deepcopy(original)
        update.update(version=8, title='数据库当前版本')
        store.write(update)
        self.assertEqual(path.read_bytes(), raw)
        # An external legacy edit and a newly added JSON must not backfill over
        # or alongside the migrated database on the next process initialization.
        path.write_text('{"old-file-is-no-longer-authority": true}', encoding='utf8')
        new_path, new_raw = self.legacy(session_record(sid='e' * 12))
        restarted = SessionStateStore(self.root)
        self.assertEqual(restarted.load(PROJECT, SESSION), update)
        self.assertEqual(restarted.list(), [update])
        self.assertEqual(new_path.read_bytes(), new_raw)
        self.assertEqual(read_sessions(self.root), [update])

    def test_sqlite_version_wal_and_full_synchronous(self):
        store = SessionStateStore(self.root)
        self.assertEqual(store.db_path, self.root / 'session-state.sqlite3')
        with sqlite3.connect(store.db_path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
        # synchronous is connection-local: inspect the connection used by an
        # actual public write, not sqlite's unrelated new-connection default.
        synchronous_at_begin = []
        connect = sqlite3.connect
        def observe_connection(*args, **kwargs):
            conn = connect(*args, **kwargs)
            def trace(statement):
                if statement.lstrip().upper().startswith('BEGIN'):
                    synchronous_at_begin.append(conn.execute('PRAGMA synchronous').fetchone()[0])
            conn.set_trace_callback(trace)
            return conn
        with patch('session_state.sqlite3.connect', side_effect=observe_connection):
            store.write(session_record())
        self.assertEqual(synchronous_at_begin, [2])

    def test_cas_rejects_stale_and_skipped_versions_without_changing_dispatch(self):
        store = SessionStateStore(self.root)
        original = session_record(running=True)
        store.write(original)
        dispatch = copy.deepcopy(store.dispatches())
        for version in (0, 1, 3):
            stale = copy.deepcopy(original)
            stale.update(version=version, title='不得覆盖')
            with self.subTest(version=version), self.assertRaises(Conflict):
                store.write(stale)
            self.assertEqual(store.load(PROJECT, SESSION), original)
            self.assertEqual(store.dispatches(), dispatch)
        update = copy.deepcopy(original)
        update.update(version=2, title='允许的下一版')
        store.write(update)
        self.assertEqual(store.load(PROJECT, SESSION), update)
        self.assertEqual(store.dispatches(), dispatch)
        with self.assertRaises(Conflict):
            store.write(session_record(sid='e' * 12, version=2))
        self.assertEqual(len(store.list()), 1)

    def test_dispatch_insert_failure_rolls_back_entire_session_write(self):
        store = SessionStateStore(self.root)
        original = session_record()
        store.write(original)
        with sqlite3.connect(store.db_path) as conn:
            conn.execute('''CREATE TRIGGER reject_dispatch BEFORE INSERT ON dispatches
                            BEGIN SELECT RAISE(ABORT, 'fixture dispatch insert failure'); END''')
        update = session_record(version=2, running=True)
        with self.assertRaises(Exception) as caught:
            store.write(update)
        self.assertIn('fixture dispatch insert failure', str(caught.exception))
        self.assertEqual(store.load(PROJECT, SESSION), original)
        self.assertEqual(store.dispatches(), [])
        with sqlite3.connect(store.db_path) as conn:
            conn.execute('DROP TRIGGER reject_dispatch')
        store.write(update)
        self.assertEqual(store.load(PROJECT, SESSION), update)
        intent = self.dispatch(store)
        self.assertEqual((intent['projectId'], intent['sessionId'], intent['runId'], intent['status']),
                         (PROJECT, SESSION, RUN, 'queued'))
        self.assertEqual(intent['id'], ATTEMPT)

    def test_two_instances_only_one_claim_and_repeat_save_does_not_requeue(self):
        first = SessionStateStore(self.root)
        original = session_record(running=True)
        first.write(original)
        second = SessionStateStore(self.root)
        barrier, results, errors = threading.Barrier(3), [], []
        def claim(store, owner):
            try:
                barrier.wait(timeout=3)
                results.append((owner, store.claim(ATTEMPT, owner)))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=claim, args=(first, 'worker-one')),
                   threading.Thread(target=claim, args=(second, 'worker-two'))]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(won for owner, won in results), [False, True])
        winner = next(owner for owner, won in results if won)
        claimed = copy.deepcopy(self.dispatch(first))
        self.assertEqual((claimed['status'], claimed['owner']), ('running', winner))
        self.assertFalse(first.claim(ATTEMPT, winner))
        update = copy.deepcopy(original)
        update['version'] = 2
        first.write(update)
        self.assertEqual(self.dispatch(second), claimed)
        update = copy.deepcopy(update)
        update.update(version=3, status='completed')
        update['runs'][0]['status'] = 'completed'
        update['runs'][0]['executionAttempts'][0].update(status='completed', endedAt=NOW)
        first.write(update)
        self.assertEqual(self.dispatch(first)['status'], 'settled')
        self.assertEqual(self.dispatch(first)['outcome'], 'completed')
        self.assertFalse(second.claim(ATTEMPT, 'late-worker'))

    def test_persisted_cancellation_prevents_claim_after_reopening(self):
        store = SessionStateStore(self.root)
        store.write(session_record(running=True))
        store.request_cancel(PROJECT, SESSION)
        reopened = SessionStateStore(self.root)
        self.assertTrue(self.dispatch(reopened)['cancelRequested'])
        self.assertFalse(reopened.claim(ATTEMPT, 'must-not-start'))
        self.assertNotEqual(self.dispatch(reopened)['status'], 'running')

    def test_recovery_reconciles_queued_and_running_without_execution_or_lost_evidence(self):
        store = SessionStateStore(self.root)
        queued = session_record(running=True)
        running = session_record(sid='e' * 12, running=True, attempt_id='f' * 12)
        terminal = session_record(sid='1' * 12, running=True, attempt_id='2' * 12)
        terminal['status'] = terminal['runs'][0]['status'] = 'completed'
        terminal['runs'][0]['nodes'][1]['status'] = 'done'
        terminal['runs'][0]['executionAttempts'][0].update(status='completed', endedAt=NOW)
        for record in (queued, running, terminal):
            store.write(record)
        self.assertTrue(store.claim('f' * 12, 'old-process'))
        receipt = self.root / PROJECT / SESSION / 'mcp' / 'pending' / 'receipt.json'
        receipt.parent.mkdir(parents=True)
        raw_receipt = b'{"status":"unknown","pending":{"tool":"external-write"}}\n'
        receipt.write_bytes(raw_receipt)
        with patch('subprocess.Popen') as process, patch('studio_client.StudioClient.call') as model, \
                patch('threading.Thread.start') as thread_start:
            changed = store.recover()
            process.assert_not_called()
            model.assert_not_called()
            thread_start.assert_not_called()
        self.assertGreaterEqual(changed, 2)
        for before in (queued, running):
            saved = store.load(PROJECT, before['id'])
            self.assertEqual(saved['status'], 'interrupted')
            self.assertEqual(saved['runs'][0]['status'], 'interrupted')
            self.assertEqual(saved['runs'][0]['nodes'][1]['status'], 'interrupted')
            self.assertEqual(saved['runs'][0]['nodes'][0], before['runs'][0]['nodes'][0])
            self.assertEqual(saved['runs'][0]['nodes'][1]['receipt'], before['runs'][0]['nodes'][1]['receipt'])
            self.assertEqual(saved['runs'][0]['executionAttempts'][0]['status'], 'interrupted')
            self.assertEqual(self.dispatch(store, before['runs'][0]['executionAttempts'][0]['id'])['status'],
                             'awaiting_resume')
        self.assertEqual(store.load(PROJECT, terminal['id']), terminal)
        self.assertEqual(receipt.read_bytes(), raw_receipt)
        after = copy.deepcopy(store.list())
        self.assertEqual(store.recover(), 0)
        self.assertEqual(store.list(), after)
        self.assertEqual(pending_dispatches(self.root), [])

    def test_read_helpers_filter_projects_and_do_not_recover_running_work(self):
        store = SessionStateStore(self.root)
        first = session_record(running=True)
        other = session_record(pid='3' * 12, sid='4' * 12, running=True, attempt_id='5' * 12)
        store.write(first)
        store.write(other)
        store.claim(ATTEMPT, 'reader-test')
        self.assertEqual(read_sessions(self.root, PROJECT), [first])
        self.assertEqual(store.list(PROJECT), [first])
        self.assertEqual(len(read_sessions(self.root)), 2)
        self.assertEqual({row['status'] for row in pending_dispatches(self.root)}, {'queued', 'running'})
        self.assertEqual([row['attemptId'] for row in pending_dispatches(self.root, PROJECT)], [ATTEMPT])
        self.assertEqual(store.load(PROJECT, SESSION), first)

    def test_recovery_failure_rolls_back_all_sessions_and_dispatch_intents(self):
        store = SessionStateStore(self.root)
        first = session_record(running=True)
        second = session_record(sid='e' * 12, running=True, attempt_id='f' * 12)
        store.write(first)
        store.write(second)
        store.claim('f' * 12, 'old-process')
        before_sessions, before_dispatches = copy.deepcopy(store.list()), copy.deepcopy(store.dispatches())
        with sqlite3.connect(store.db_path) as conn:
            conn.execute('''CREATE TRIGGER reject_recovery BEFORE UPDATE ON sessions
                            WHEN NEW.session_id = 'eeeeeeeeeeee'
                            BEGIN SELECT RAISE(ABORT, 'fixture recovery failure'); END''')
        with self.assertRaises(Exception) as caught:
            store.recover()
        self.assertIn('fixture recovery failure', str(caught.exception))
        self.assertEqual(store.list(), before_sessions)
        self.assertEqual(store.dispatches(), before_dispatches)
        with sqlite3.connect(store.db_path) as conn:
            conn.execute('DROP TRIGGER reject_recovery')
        self.assertEqual(store.recover(), 2)
        self.assertEqual({row['status'] for row in store.dispatches()}, {'awaiting_resume'})

    def test_corrupt_database_never_falls_back_to_legacy_json(self):
        path, raw = self.legacy(session_record())
        db = self.root / 'session-state.sqlite3'
        db.write_bytes(b'not a sqlite database\n')
        with self.assertRaises(Exception):
            SessionStateStore(self.root)
        self.assertEqual(db.read_bytes(), b'not a sqlite database\n')
        self.assertEqual(path.read_bytes(), raw)
        with self.assertRaises(Exception):
            read_sessions(self.root)

    def test_future_schema_is_rejected_without_fallback_or_downgrade(self):
        store = SessionStateStore(self.root)
        original = session_record()
        store.write(original)
        self.legacy(session_record(version=99))
        with sqlite3.connect(store.db_path) as conn:
            conn.execute('PRAGMA user_version=2')
        with self.assertRaises(Exception):
            SessionStateStore(self.root)
        with self.assertRaises(Exception):
            read_sessions(self.root)
        with sqlite3.connect(store.db_path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
