"""Local HTTP proof for readiness, effective parameters and probe ownership."""
import json
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from app import Store, shutdown_background_workers
from studio import Studio
from model_catalog import MODELS


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.catalog_reads += 1
        raw = json.dumps({'data': [{'id': 'visible-model'}], 'has_more': False}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(value)
        self.server.entered.set()
        if self.server.mode == 'reject':
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'unsupported request parameter')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if self.server.mode == 'wait':
            self.server.release.wait(5)
        packet = {'model': 'visible-model-server-alias', 'choices': [
            {'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 4}}
        try:
            self.wfile.write(b'data: ' + json.dumps(packet).encode() + b'\n\ndata: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class ReadinessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.saved_models=copy.deepcopy(MODELS)
        self.tmp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.catalog_reads = 0
        self.server.calls = []
        self.server.mode = 'complete'
        self.server.entered = threading.Event()
        self.server.release = threading.Event()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.studio = Studio(Store(Path(self.tmp.name), seed=False))
        registry = self.studio.registry
        self.mid = registry.register({'providerName': 'Readiness fixture',
            'baseUrl': 'http://127.0.0.1:' + str(self.server.server_port) + '/v1',
            'modelId': 'visible-model', 'name': 'Visible', 'family': 'fixture',
            'protocol': 'chat', 'profile': 'coding'})
        self.provider = registry.profiles[self.mid]['provider']
        self.absent = registry.register({'providerId': self.provider, 'modelId': 'not-in-directory',
            'name': 'Absent', 'family': 'fixture', 'protocol': 'chat', 'profile': 'coding'})
        self.studio.client.configure(self.provider, 'test-only-credential')

    def tearDown(self):
        for event in self.studio.cancels.values():event.set()
        self.server.release.set()
        for worker in self.studio.threads.values():
            if worker.ident is not None:worker.join(5)
        self.server.shutdown();self.server.server_close();self.server_thread.join()
        self.tmp.cleanup()
        MODELS.clear();MODELS.update(self.saved_models)

    def state(self, mid=None):
        return next(row for row in self.studio.index()['modelStates'] if row['id'] == (mid or self.mid))

    def test_credentials_only_never_schedule_unknown_or_missing_models(self):
        self.assertEqual(self.studio.client.available(), [])
        self.assertTrue(self.state()['configured'])
        self.assertEqual(self.server.calls, [])
        self.studio.discover_models(self.provider)
        self.assertIn(self.mid, self.studio.client.available())
        self.assertNotIn(self.absent, self.studio.client.available())
        self.assertEqual(self.state()['callState'], 'unverified')
        self.assertEqual(self.state(self.absent)['catalogState'], 'not_listed')
        self.assertEqual(self.server.calls, [])

    def test_key_rotation_stops_new_task_requests_before_network_dispatch(self):
        self.studio.discover_models(self.provider)
        snapshot = self.studio.client.snapshot_profiles([self.mid])
        self.studio.client.configure(self.provider, 'replacement-test-credential')
        with self.studio.client.request_scope(snapshot, require_eligible=True):
            with self.assertRaises(ValueError) as error:
                self.studio.client.call(self.mid, [{'role': 'user', 'content': 'Return JSON'}], threading.Event())
        self.assertTrue(error.exception.dispatchBlocked)
        self.assertEqual(self.server.calls, [])

    def test_effective_wire_parameters_and_success_receipt_match(self):
        self.studio.discover_models(self.provider)
        self.studio.update_model_policy(self.mid, {'maxOutputTokens': 8192, 'reasoningEffort': 'low',
                                                 'jsonMode': True, 'timeoutSeconds': 12}, 1)
        result, receipt = self.studio.client.call(self.mid, [{'role': 'user', 'content': 'Return JSON'}], threading.Event())
        self.assertTrue(result['ok'])
        body = self.server.calls[0]
        self.assertEqual(body['max_tokens'], 8192)
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(receipt['requestPolicy']['timeoutSeconds'], 12)
        self.assertTrue(receipt['responseComplete'] and receipt['jsonValidated'])
        self.assertNotIn('messages', receipt['effectiveRequestParameters'])
        status = self.state()
        self.assertEqual(status['callState'], 'succeeded')
        self.assertEqual(status['lastCall']['returnedModel'], 'visible-model-server-alias')
        self.assertEqual(status['lastCall']['modelIdentityEvidence'], 'provider_declared')
        self.assertNotIn('test-only-credential', json.dumps(status))

    def test_hard_parameter_failure_blocks_only_current_policy(self):
        self.studio.discover_models(self.provider)
        self.server.mode = 'reject'
        with self.assertRaises(ValueError):
            self.studio.client.call(self.mid, [{'role': 'user', 'content': 'Return JSON'}], threading.Event())
        self.assertNotIn(self.mid, self.studio.client.available())
        self.assertEqual(self.state()['lastCall']['httpStatus'], 400)
        self.studio.update_model_policy(self.mid, {'reasoningEffort': 'low'}, 1)
        self.assertIn(self.mid, self.studio.client.available())
        self.assertEqual(self.state()['callState'], 'unverified')

    def test_probe_is_explicit_and_stale_completion_is_not_new_policy_proof(self):
        self.studio.discover_models(self.provider)
        self.server.mode = 'wait'
        initial = self.studio.probe_model(self.mid)
        self.assertTrue(self.server.entered.wait(3))
        self.assertEqual(self.state()['probeState'], 'running')
        self.studio.update_model_policy(self.mid, {'maxOutputTokens': 8192}, 1)
        self.server.release.set()
        self.studio.threads['model-probe:' + self.mid].join(5)
        current = self.state()
        self.assertEqual(current['probeState'], 'superseded')
        self.assertEqual(current['callState'], 'unverified')
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.server.calls[0]['max_tokens'], 16000)

    def test_shutdown_cancels_probe_under_same_worker_ownership(self):
        self.studio.discover_models(self.provider)
        self.server.mode = 'wait'
        self.studio.probe_model(self.mid)
        self.assertTrue(self.server.entered.wait(3))
        owners = [SimpleNamespace(cancel=threading.Event(), thread=None) for _ in range(2)]
        shutdown_background_workers(self.studio, *owners)
        self.assertFalse(self.studio.threads['model-probe:' + self.mid].is_alive())
        self.assertEqual(self.state()['probeState'], 'cancelled')
        self.assertEqual(len(self.server.calls), 1)


if __name__ == '__main__':
    unittest.main()
