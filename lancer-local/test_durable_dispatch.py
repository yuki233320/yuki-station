import copy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Store
from execution_attempts import begin_attempt
from studio import Studio
from test_studio_business import BusinessClient


class DurableDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = BusinessClient()
        self.studio = Studio(Store(Path(self.tmp.name), seed=False), self.client)
        self.pid = self.studio.create_project('事务派工', '验证真实状态边界')['id']
        self.session = self.studio.create_session(self.pid, '任务', '整理项目说明并交付文件', 'general')

    def tearDown(self):
        for thread in self.studio.threads.values():
            if thread.ident is not None:thread.join(10)
        self.tmp.cleanup()

    def test_provider_observes_durable_claim_before_first_call(self):
        actual = self.client.call
        claims = []
        def call(*args, **kwargs):
            rows = self.studio.state.dispatches(self.pid)
            claims.append(copy.deepcopy(rows))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['status'], 'running')
            self.assertEqual(rows[0]['owner'], self.studio.instance_id)
            return actual(*args, **kwargs)
        with patch.object(self.client, 'call', side_effect=call):
            self.studio.start(self.pid, self.session['id'])
            self.studio.threads[self.session['id']].join(10)
        self.assertTrue(claims)
        result = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        row = self.studio.state.dispatches(self.pid)[0]
        self.assertEqual((row['status'], row['outcome']), ('settled', 'completed'))
        self.assertFalse(self.studio.path(self.pid, self.session['id']).exists())

    def test_failed_claim_is_resumable_without_provider_calls(self):
        with patch.object(self.studio.state, 'claim', side_effect=sqlite3.OperationalError('busy')):
            self.studio.start(self.pid, self.session['id'])
            self.studio.threads[self.session['id']].join(10)
        result = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['runs'][-1]['executionAttempts'][0]['status'], 'interrupted')
        self.assertTrue(result['recovery']['requiresExplicitResume'])
        self.assertFalse(self.client.calls)
        self.studio.start(self.pid, self.session['id'], resume=True)
        self.studio.threads[self.session['id']].join(10)
        result = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(len(self.studio.state.dispatches(self.pid)), 2)

    def test_queued_stop_survives_restart_without_claim(self):
        session = self.session
        run = {'id': 'run', 'status': 'running', 'nodes': []}
        begin_attempt(run, 'start', 'start', 'attempt', {})
        session.update(status='running', runs=[run])
        self.studio.save(session)
        self.studio.stop(self.pid, session['id'])
        restored = Studio(self.studio.store, self.client)
        saved = restored.load(self.pid, session['id'])
        self.assertEqual(saved['status'], 'stopped')
        self.assertEqual(saved['runs'][0]['executionAttempts'][0]['status'], 'stopped')
        row = restored.state.dispatches(self.pid)[0]
        self.assertTrue(row['cancelRequested'])
        self.assertEqual(row['status'], 'settled')
        self.assertFalse(self.client.calls)

    def test_save_return_lost_keeps_cas_version_recoverable(self):
        actual = self.studio._write
        def lost_return(path, value):
            actual(path, value)
            raise OSError('lost return after commit')
        session = self.session
        session['title'] = 'committed title'
        with patch.object(self.studio, '_write', side_effect=lost_return):
            with self.assertRaises(OSError):self.studio.save(session)
        saved = self.studio.load(self.pid, session['id'])
        self.assertEqual(session['version'], saved['version'])
        session['title'] = 'next valid version'
        self.studio.save(session)
        self.assertEqual(self.studio.load(self.pid, session['id'])['title'], session['title'])


if __name__ == '__main__':
    unittest.main()
