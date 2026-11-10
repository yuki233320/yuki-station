"""Real process-death checks for committed and uncommitted local session state."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from session_state import SessionStateStore, pending_dispatches
from test_session_state import ATTEMPT, PROJECT, SESSION, session_record


CRASH_CHILD = r'''
import json
import os
from pathlib import Path
import sys
from session_state import SessionStateStore

root, mode = Path(sys.argv[1]), sys.argv[2]
payload = json.load(sys.stdin)
store = SessionStateStore(root)
if mode == 'uncommitted':
    # Exercise the same write/dispatch code as public write(), but die after
    # both SQL mutations and before the transaction context can COMMIT.
    with store._transaction() as db:
        store._write(db, payload)
        row = db.execute('SELECT version FROM sessions WHERE project_id=? AND session_id=?',
                         (payload['projectId'], payload['id'])).fetchone()
        assert row[0] == payload['version']
        assert db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0] == 1
        os._exit(73)
store.write(payload)
if mode == 'running':
    aid = payload['runs'][0]['executionAttempts'][0]['id']
    assert store.claim(aid, 'crashed-child')
    assert store.dispatches()[0]['status'] == 'running'
    os._exit(72)
assert mode == 'queued'
assert store.dispatches()[0]['status'] == 'queued'
os._exit(71)
'''


class SessionCrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'project-sessions'
        self.legacy_path = self.root / PROJECT / (SESSION + '.json')
        self.legacy_path.parent.mkdir(parents=True)
        self.original = session_record(version=3)
        self.legacy_bytes = ('  ' + json.dumps(self.original, ensure_ascii=False, indent=3) + '\n\n').encode()
        self.legacy_path.write_bytes(self.legacy_bytes)
        # The parent completes migration before crashing a separate writer.
        self.store = SessionStateStore(self.root)
        self.pending = session_record(version=4, running=True)

    def tearDown(self):
        self.tmp.cleanup()

    def crash(self, mode, expected_code):
        result = subprocess.run(
            [sys.executable, '-B', '-c', CRASH_CHILD, str(self.root), mode],
            input=json.dumps(self.pending), text=True, capture_output=True,
            cwd=str(Path(__file__).resolve().parent), timeout=10)
        self.assertEqual(result.returncode, expected_code, result.stderr)
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)

    def recover_without_execution(self, store):
        with patch('studio_client.StudioClient.call') as model, \
                patch('execution_adapters.ExecutorRegistry.execute') as executor, \
                patch('subprocess.Popen') as process, patch('threading.Thread.start') as thread:
            result = store.recover()
            model.assert_not_called()
            executor.assert_not_called()
            process.assert_not_called()
            thread.assert_not_called()
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)
        return result

    def assert_committed_crash_recovery(self, prior_status):
        reopened = SessionStateStore(self.root)
        self.assertEqual(reopened.load(PROJECT, SESSION), self.pending)
        dispatch = reopened.dispatches()[0]
        self.assertEqual(dispatch['status'], prior_status)
        self.assertEqual(dispatch['owner'], 'crashed-child' if prior_status == 'running' else None)
        self.assertEqual(self.recover_without_execution(reopened), 1)
        saved = reopened.load(PROJECT, SESSION)
        self.assertEqual(saved['status'], 'interrupted')
        self.assertEqual(saved['runs'][0]['status'], 'interrupted')
        self.assertEqual(saved['runs'][0]['executionAttempts'][0]['status'], 'interrupted')
        self.assertEqual(saved['runs'][0]['nodes'][0], self.pending['runs'][0]['nodes'][0])
        self.assertEqual(saved['runs'][0]['nodes'][1]['receipt'], self.pending['runs'][0]['nodes'][1]['receipt'])
        self.assertEqual(saved['runs'][0]['nodes'][1]['status'], 'interrupted')
        self.assertEqual(reopened.dispatches()[0]['status'], 'awaiting_resume')
        self.assertFalse(reopened.claim(ATTEMPT, 'must-not-replay'))
        self.assertEqual(pending_dispatches(self.root), [])
        self.assertEqual(self.recover_without_execution(reopened), 0)
        self.assertEqual(reopened.load(PROJECT, SESSION), saved)

    def test_process_dies_after_queued_intent_commit_then_requires_explicit_resume(self):
        self.crash('queued', 71)
        self.assert_committed_crash_recovery('queued')

    def test_process_dies_after_claim_then_preserves_running_evidence_without_replay(self):
        self.crash('running', 72)
        self.assert_committed_crash_recovery('running')

    def test_process_dies_inside_transaction_then_original_session_survives(self):
        self.crash('uncommitted', 73)
        reopened = SessionStateStore(self.root)
        self.assertEqual(reopened.load(PROJECT, SESSION), self.original)
        self.assertEqual(reopened.dispatches(), [])
        self.assertEqual(self.recover_without_execution(reopened), 0)
        self.assertEqual(reopened.load(PROJECT, SESSION), self.original)
        # The crashed write consumed neither its version nor its attempt ID.
        reopened.write(self.pending)
        self.assertEqual(reopened.load(PROJECT, SESSION), self.pending)
        self.assertEqual(reopened.dispatches()[0]['status'], 'queued')
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)


if __name__ == '__main__':
    unittest.main()
