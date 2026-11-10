"""Cancellation and post-commit recovery must preserve attempt outcomes."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest

from app import Store
from studio import Studio
from test_studio_business import BusinessClient


class AttemptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = BusinessClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('恢复履历', '核对取消与交付重入')['id']
        self.session = self.studio.create_session(self.pid, '任务', '整理资料并明确交付', 'general')

    def tearDown(self):
        for thread in self.studio.threads.values():
            if thread.ident is not None:
                thread.join(10)
        self.tmp.cleanup()

    def result(self):
        thread = self.studio.threads[self.session['id']]
        thread.join(10)
        self.assertFalse(thread.is_alive())
        return self.studio.load(self.pid, self.session['id'])

    def run_task(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume)
        return self.result()

    def test_cancelled_attempt_is_stopped_and_resume_keeps_completed_build(self):
        entered = threading.Event()
        def wait_for_stop():
            entered.set()
            if not self.studio.cancels[self.session['id']].wait(3):
                raise AssertionError('test did not cancel the final request')
        self.client.before_final = wait_for_stop
        self.studio.start(self.pid, self.session['id'])
        try:
            self.assertTrue(entered.wait(3))
        finally:
            self.studio.stop(self.pid, self.session['id'])
        stopped = self.result()
        self.assertEqual(stopped['status'], 'stopped', stopped.get('error'))
        old = copy.deepcopy(stopped['runs'][-1]['executionAttempts'][0])
        self.assertEqual(old['status'], 'stopped')
        before = [call['key'] for call in self.client.calls if call['key'][0] == 'build']
        finished = self.run_task(resume=True)
        self.assertEqual(finished['status'], 'completed', finished.get('error'))
        self.assertEqual(finished['runs'][-1]['executionAttempts'][0], old)
        self.assertEqual(finished['runs'][-1]['executionAttempts'][1]['status'], 'completed')
        self.assertEqual([call['key'] for call in self.client.calls if call['key'][0] == 'build'], before)

    def test_lost_commit_return_creates_new_attempt_without_repeating_commit_or_calls(self):
        commit = self.store.commit
        revisions = []
        def lose_return(*args, **kwargs):
            result = commit(*args, **kwargs)
            revisions.append(result['revision'])
            if len(revisions) == 1:
                raise OSError('lost reply after publishing HEAD')
            return result
        self.store.commit = lose_return
        failed = self.run_task()
        self.assertEqual(failed['status'], 'failed')
        first = copy.deepcopy(failed['runs'][-1]['executionAttempts'][0])
        self.assertEqual(first['status'], 'failed')
        self.assertTrue(failed['runs'][-1]['commitIntent'])
        calls = len(self.client.calls)
        self.client.available = lambda: []
        finished = self.run_task(resume=True)
        self.assertEqual(finished['status'], 'completed', finished.get('error'))
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(finished['runs'][-1]['mergedRevision'], revisions[0])
        self.assertEqual(finished['runs'][-1]['executionAttempts'][0], first)
        self.assertEqual(finished['runs'][-1]['executionAttempts'][1]['reason'], 'resume')
        self.assertEqual(finished['runs'][-1]['executionAttempts'][1]['status'], 'completed')

    def test_failed_terminal_save_does_not_prematurely_seal_successful_attempt(self):
        save = self.studio.save
        injected = []
        def fail_completed_save(session):
            if session['status'] == 'completed' and not injected:
                injected.append(True)
                raise OSError('terminal session write failed')
            return save(session)
        self.studio.save = fail_completed_save
        failed = self.run_task()
        self.assertEqual(failed['status'], 'failed')
        run = failed['runs'][-1]
        self.assertEqual(run['executionAttempts'][0]['status'], 'failed')
        first = copy.deepcopy(run['executionAttempts'][0])
        revision = self.store.current(self.pid)[0]['revision']
        calls = len(self.client.calls)
        finished = self.run_task(resume=True)
        self.assertEqual(finished['status'], 'completed', finished.get('error'))
        self.assertEqual(finished['runs'][-1]['executionAttempts'][0], first)
        self.assertEqual(finished['runs'][-1]['executionAttempts'][1]['status'], 'completed')
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(self.store.current(self.pid)[0]['revision'], revision)
        self.assertEqual(len(self.studio.meta(self.pid)['memory']), 1)


if __name__ == '__main__':
    unittest.main()
