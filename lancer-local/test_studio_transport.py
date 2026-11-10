import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from live import Cancelled
from studio_client import StudioClient


class ProviderServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        self.entered = threading.Event()
        self.disconnected = threading.Event()
        self.release = threading.Event()
        self.requests = 0
        super().__init__(('127.0.0.1', 0), ProviderHandler)


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def event(self, value):
        self.wfile.write(b'data: ' + json.dumps(value).encode() + b'\n\n')
        self.wfile.flush()

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.server.requests += 1
        mode = self.server.mode
        if mode == 'headers':
            self.server.entered.set()
            self.wait_for_disconnect()
            return
        if mode == 'redirect':
            self.send_response(307)
            self.send_header('Location', self.path)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if mode == 'image':
            self.wfile.write(b'{"data": [');self.wfile.flush()
        if mode in ('partial', 'complete'):
            text = '{"summary":"partial' if mode == 'partial' else '{"summary":"complete"}'
            self.event({'type': 'response.output_text.delta', 'delta': text})
        if mode == 'partial':
            self.wfile.write(b'data: {"type":')
            self.wfile.flush()
        if mode == 'complete':
            self.event({'type': 'response.completed', 'response': {'model': 'test', 'usage': {'output_tokens': 4}}})
            return
        self.server.entered.set()
        if mode == 'continuous':
            try:
                while not self.server.release.wait(0.02):
                    self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.server.disconnected.set()
        else:
            self.wait_for_disconnect()

    def wait_for_disconnect(self):
        self.connection.settimeout(0.1)
        while not self.server.release.is_set():
            try:
                if not self.connection.recv(1):
                    self.server.disconnected.set()
                    return
            except socket.timeout:
                continue
            except OSError:
                self.server.disconnected.set()
                return


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ProviderServer()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.client = StudioClient({'test': {'provider': 'test', 'protocol': 'responses', 'model': 'test', 'label': 'Local provider', 'base': base}})
        self.client.configure('test', 'local-test-placeholder')
        self.client.request_timeout_seconds = 5
        self.cancel = threading.Event()
        self.received = threading.Event()
        self.answer = None
        self.error = None
        self.thread = None

    def tearDown(self):
        self.cancel.set()
        self.server.release.set()
        if self.thread:
            self.thread.join(2)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(2)

    def invoke(self):
        try:
            self.answer = self.client.call('test', [{'role': 'user', 'content': 'Return JSON'}], self.cancel, lambda text: self.received.set())
        except Exception as exc:
            self.error = exc

    def start(self, mode):
        self.server.mode = mode
        self.thread = threading.Thread(target=self.invoke)
        self.thread.start()
        self.assertTrue(self.server.entered.wait(2), 'provider did not reach requested transport phase')
        if mode == 'partial':
            self.assertTrue(self.received.wait(2), 'client did not receive the complete preceding SSE event')

    def assert_interrupted(self, expected):
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive(), 'request remained blocked')
        self.assertIsInstance(self.error, expected)
        self.assertTrue(self.server.disconnected.wait(1), 'provider socket was not closed')
        self.assertEqual(self.server.requests, 1, 'an interrupted request was silently retried')
        self.assertIsNone(self.answer)
        timings = self.error.receipt['timings']
        self.assertIsNotNone(timings['totalMs'])
        self.assertLess(timings['totalMs'], 2000)

    def test_cancel_before_headers_closes_actual_connection(self):
        self.start('headers')
        self.cancel.set()
        self.assert_interrupted(Cancelled)
        self.assertIsNone(self.error.receipt['timings']['headersMs'])
        self.assertEqual(self.error.partial_text, '')

    def test_cancel_mid_sse_line_preserves_received_text_and_closes_socket(self):
        self.start('partial')
        self.cancel.set()
        self.assert_interrupted(Cancelled)
        self.assertEqual(self.error.partial_text, '{"summary":"partial')
        self.assertIsNotNone(self.error.receipt['timings']['headersMs'])
        self.assertIsNotNone(self.error.receipt['timings']['firstTextMs'])

    def test_cancel_image_response_body_closes_actual_connection(self):
        self.client.profiles['test']['protocol'] = 'image'
        self.start('image')
        self.cancel.set()
        self.assert_interrupted(Cancelled)

    def test_total_deadline_interrupts_continuous_heartbeats(self):
        self.client.request_timeout_seconds = 0.25
        self.start('continuous')
        self.assert_interrupted(TimeoutError)
        self.assertEqual(self.error.partial_text, '')

    def test_total_deadline_interrupts_header_stall(self):
        self.client.request_timeout_seconds = 0.25
        self.start('headers')
        self.assert_interrupted(TimeoutError)

    def test_complete_response_has_actual_phase_timings(self):
        self.server.mode = 'complete'
        self.invoke()
        self.assertIsNone(self.error)
        result, receipt = self.answer
        self.assertEqual(result, {'summary': 'complete'})
        self.assertEqual(receipt['usage']['output_tokens'], 4)
        timings = receipt['timings']
        self.assertLessEqual(timings['headersMs'], timings['firstTextMs'])
        self.assertLessEqual(timings['firstTextMs'], timings['totalMs'])
        self.assertEqual(self.server.requests, 1)

    def test_redirect_remains_blocked_without_replaying_credentials(self):
        self.server.mode = 'redirect'
        self.invoke()
        self.assertIsInstance(self.error, ValueError)
        self.assertIn('307', str(self.error))
        self.assertEqual(self.server.requests, 1)


if __name__ == '__main__':
    unittest.main()
