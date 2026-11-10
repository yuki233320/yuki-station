import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from execution_adapters import ExecutionRequest, ExecutorRegistry, default_executors
from live import Cancelled


class ExecutorBoundaryTests(unittest.TestCase):
    def request(self, model='sol', capabilities=(), phase='build', mode='live'):
        return ExecutionRequest(
            {'id': 'session'}, {'mode': mode, 'workflowVersion': 2},
            {'agent': model, 'phase': phase}, {'messages': ['original']}, ['original'],
            {'module': {'requiredCapabilities': list(capabilities)}},
            'deliver', threading.Event(), lambda text: None, 'old validation error')

    def test_routes_by_execution_requirements_not_provider_brand(self):
        registry = ExecutorRegistry(dict.fromkeys(('api', 'mcp', 'native_codex', 'demo')))
        for request, expected in (
            (self.request(), 'api'),
            (self.request(model='deepseek'), 'api'),
            (self.request(capabilities=['browser']), 'mcp'),
            (self.request(capabilities=['plugins']), 'mcp'),
            (self.request(capabilities=['browser'], phase='integration'), 'api'),
            (self.request(model='native_codex'), 'native_codex'),
            (self.request(model='native_codex', mode='demo'), 'demo'),
        ):
            self.assertEqual(registry.select(request), expected)

    def test_unknown_adapter_is_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, '未接入'):
            ExecutorRegistry({}).select(self.request())

    def test_cancelled_request_never_calls_adapter(self):
        call = Mock()
        request = self.request()
        request.cancel.set()
        with self.assertRaises(Cancelled):
            ExecutorRegistry({'api': call}).execute('api', request)
        call.assert_not_called()

    def test_provider_and_mcp_preserve_updated_context_and_receipt(self):
        updated = {'messages': ['repaired', 'actual receipt']}
        response = ({'files': []}, {'mode': 'fixture'}, updated)
        host = SimpleNamespace(request_model_response=Mock(return_value=response))
        registry = default_executors(host)
        for executor in ('api', 'mcp'):
            request = self.request(capabilities=['plugins'] if executor == 'mcp' else [])
            with patch('studio_mcp.execute_mcp', return_value=response) as mcp:
                output = registry.execute(executor, request)
                self.assertIs(output.context, updated)
                self.assertIs(output.receipt, response[1])
                if executor == 'mcp':
                    self.assertEqual(mcp.call_args.args[-1], request.previous_error)

    def test_native_keeps_raw_receipt_and_context(self):
        request = self.request(model='native_codex')
        receipt = {'status': 'completed', 'tools': [{'id': 'actual-tool'}]}
        with patch('studio_native.execute_native', return_value=({'summary': 'done'}, receipt)):
            output = default_executors(object()).execute('native_codex', request)
        self.assertIs(output.receipt, receipt)
        self.assertIs(output.context, request.context)


if __name__ == '__main__':
    unittest.main()
