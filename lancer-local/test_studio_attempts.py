"""Exercise attempts through real Studio entry points with no model traffic."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Store
from execution_attempts import begin_attempt
from studio import Studio
from test_studio_business import BusinessClient


class StudioAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = BusinessClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('执行履历', '保留实际交付与恢复证据')['id']
        self.session = self.studio.create_session(self.pid, '任务', '整理资料形成明确成果', 'general')

    def tearDown(self):
        for thread in self.studio.threads.values():
            if thread.ident is not None:
                thread.join(10)
        self.tmp.cleanup()

    def run_task(self, resume=False):
        self.studio.start(self.pid, self.session['id'], resume=resume)
        worker = self.studio.threads[self.session['id']]
        worker.join(10)
        self.assertFalse(worker.is_alive())
        return self.studio.load(self.pid, self.session['id'])

    def test_failed_attempt_resume_reuses_nodes_and_keeps_both_outcomes(self):
        def fail():
            raise ValueError('final response interrupted')
        self.client.before_final = fail
        first = self.run_task()
        run = first['runs'][-1]
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(run['executionAttempts'][0]['status'], 'failed')
        saved_attempt = copy.deepcopy(run['executionAttempts'][0])
        saved_build = copy.deepcopy(next(n for n in run['nodes'] if n['phase'] == 'build'))
        before = [c['key'] for c in self.client.calls if c['key'][0] in ('plan', 'build')]
        final = self.run_task(resume=True)
        run = final['runs'][-1]
        self.assertEqual(final['status'], 'completed', final.get('error'))
        self.assertEqual(run['executionAttempts'][0], saved_attempt)
        self.assertEqual([a['reason'] for a in run['executionAttempts']], ['start', 'resume'])
        self.assertEqual(run['executionAttempts'][1]['status'], 'completed')
        self.assertEqual(next(n for n in run['nodes'] if n['phase'] == 'build'), saved_build)
        self.assertEqual([c['key'] for c in self.client.calls if c['key'][0] in ('plan', 'build')], before)
        self.assertEqual({n['executorId'] for n in run['nodes']}, {'api'})
        self.assertTrue(run['mergedRevision'])

    def test_restart_reconciles_running_attempt_without_calls(self):
        session = self.session
        run = {'id': 'interrupted-run', 'status': 'running', 'nodes': [{'status': 'running'}]}
        begin_attempt(run, 'start', 'start-time', 'attempt', {})
        session.update(status='running', runs=[run])
        self.studio.save(session)
        recovered = Studio(self.store, self.client).load(self.pid, session['id'])
        self.assertEqual(recovered['status'], 'interrupted')
        attempt = recovered['runs'][0]['executionAttempts'][0]
        self.assertEqual(attempt['status'], 'interrupted')
        self.assertEqual(attempt['endReason'], 'service_restart')
        self.assertFalse(self.client.calls)

    def test_terminal_run_saved_before_attempt_finalization_recovers_known_result(self):
        session = self.run_task()
        run = session['runs'][-1]
        run['executionAttempts'][0]['status'] = 'running'
        run['executionAttempts'][0].pop('endedAt')
        self.studio.save(session)
        count = len(self.client.calls)
        recovered = Studio(self.store, self.client).load(self.pid, session['id'])
        self.assertEqual(recovered['runs'][-1]['executionAttempts'][0]['status'], 'completed')
        self.assertEqual(len(self.client.calls), count)

    def test_thread_start_failure_is_durable_and_has_no_provider_call(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('cannot start')):
            with self.assertRaisesRegex(RuntimeError, 'cannot start'):
                self.studio.start(self.pid, self.session['id'])
        saved = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['runs'][-1]['executionAttempts'][0]['status'], 'failed')
        self.assertFalse(self.client.calls)
        self.assertNotIn(self.session['id'], self.studio.threads)

    def test_failed_intent_persistence_never_starts_a_thread(self):
        before = self.studio.load(self.pid, self.session['id'])
        with patch.object(self.studio, 'save', side_effect=OSError('disk full')):
            with patch.object(threading.Thread, 'start') as start:
                with self.assertRaisesRegex(OSError, 'disk full'):
                    self.studio.start(self.pid, self.session['id'])
                start.assert_not_called()
        self.assertEqual(self.studio.load(self.pid, self.session['id']), before)
        self.assertFalse(self.client.calls)


if __name__ == '__main__':
    unittest.main()
