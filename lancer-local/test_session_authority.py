"""A missing/mismatched authority database must never revive import originals."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from session_state import DATABASE, SessionStateStore, read_dispatches, read_sessions


PROJECT = '111111111111'
SESSION = '222222222222'
MARKER = 'session-state.authority.json'


def record(version=1, title='current state'):
    return {'projectId': PROJECT, 'id': SESSION, 'version': version,
            'status': 'ready', 'title': title, 'runs': []}


class SessionAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def legacy(self):
        directory = self.root / PROJECT
        directory.mkdir(exist_ok=True)
        path = directory / (SESSION + '.json')
        path.write_text(json.dumps(record(7, 'immutable import original')))
        return path

    def assert_authority_rejected(self):
        for operation in (lambda: read_sessions(self.root),
                          lambda: read_dispatches(self.root),
                          lambda: SessionStateStore(self.root)):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()

    def test_first_store_binds_marker_to_database_identity(self):
        store = SessionStateStore(self.root)
        marker = json.loads((self.root / MARKER).read_text())
        self.assertEqual(marker['schema'], 'yuki-session-authority/v1')
        self.assertEqual(marker['database'], DATABASE)
        self.assertTrue(marker['authorityId'])
        with sqlite3.connect(store.db_path) as connection:
            authority_id = connection.execute("SELECT value FROM metadata WHERE key='authority_id'").fetchone()[0]
        self.assertEqual(marker['authorityId'], authority_id)

    def test_lost_database_never_falls_back_to_old_json_or_recreates_it(self):
        original = self.legacy()
        original_bytes = original.read_bytes()
        store = SessionStateStore(self.root)
        updated = store.load(PROJECT, SESSION)
        updated.update(version=8, title='new authoritative value')
        store.write(updated)
        self.assertTrue((self.root / MARKER).is_file())
        store.db_path.unlink()
        self.assert_authority_rejected()
        self.assertFalse(store.db_path.exists())
        self.assertEqual(original.read_bytes(), original_bytes)

    def test_marker_tampering_is_not_repaired_by_silently_trusting_database(self):
        store = SessionStateStore(self.root)
        store.write(record())
        path = self.root / MARKER
        marker = json.loads(path.read_text())
        for changes in ({'authorityId': 'different-database'},
                        {'schema': 'unsupported-authority'},
                        {'database': 'other.sqlite3'}):
            with self.subTest(changes=changes):
                altered = dict(marker, **changes)
                path.write_text(json.dumps(altered))
                self.assert_authority_rejected()
                self.assertEqual(json.loads(path.read_text()), altered)
                path.write_text(json.dumps(marker))
        self.assertEqual(store.load(PROJECT, SESSION), record())

    def test_database_identity_mismatch_is_rejected_without_changing_session(self):
        store = SessionStateStore(self.root)
        store.write(record())
        marker = (self.root / MARKER).read_bytes()
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("UPDATE metadata SET value='other-identity' WHERE key='authority_id'")
            before = connection.execute('SELECT payload FROM sessions').fetchone()[0]
        self.assert_authority_rejected()
        self.assertEqual((self.root / MARKER).read_bytes(), marker)
        with sqlite3.connect(store.db_path) as connection:
            self.assertEqual(connection.execute('SELECT payload FROM sessions').fetchone()[0], before)

    def test_never_migrated_legacy_read_is_non_mutating(self):
        path = self.legacy()
        before = path.read_bytes()
        self.assertEqual(read_sessions(self.root), [record(7, 'immutable import original')])
        self.assertEqual(read_dispatches(self.root), [])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / DATABASE).exists())
        self.assertFalse((self.root / MARKER).exists())

    def test_marker_does_not_dispatch_work_when_store_is_reopened(self):
        store = SessionStateStore(self.root)
        session = record()
        session.update(status='running', runs=[{
            'id': '333333333333', 'status': 'running', 'nodes': [],
            'executionAttempts': [{'id': '444444444444', 'number': 1, 'reason': 'start',
                                   'status': 'running', 'startedAt': 't1'}],
        }])
        store.write(session)
        before = read_dispatches(self.root)
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]['status'], 'queued')
        self.assertIsNone(before[0]['owner'])
        SessionStateStore(self.root)
        self.assertEqual(read_dispatches(self.root), before)
        self.assertEqual(read_sessions(self.root), [session])


if __name__ == '__main__':
    unittest.main()
