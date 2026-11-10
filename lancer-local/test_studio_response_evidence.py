import base64
import json
import tempfile
import unittest
from pathlib import Path

from app import Store
from studio import Studio
from studio_client import ModelResponseError
from test_studio_business import BusinessClient


class MalformedResponseClient(BusinessClient):
    def __init__(self, failures, failure_kind='json'):
        super().__init__()
        self.failures = failures
        self.failure_kind = failure_kind
        self.integration_attempts = 0

    def call(self, model, messages, cancel, on_delta=None):
        task = next(message for message in reversed(messages)
                    if message['role'] == 'user' and '\n本轮角色任务：\n' in message['content'])
        result, receipt = super().call(model, [task], cancel, on_delta)
        if self.calls[-1]['key'][0] != 'integration':
            return result, receipt
        self.integration_attempts += 1
        if self.integration_attempts > self.failures:
            return result, receipt
        if self.failure_kind == 'network':
            raise ValueError('fixture network disconnected')
        if self.failure_kind == 'incomplete':
            raise ModelResponseError('fixture stream incomplete', dict(receipt, finishReason='length'))
        if self.failure_kind == 'partial':
            raise ModelResponseError('fixture connection closed', dict(receipt, finishReason=None, timings={'headersMs':10,'firstTextMs':20,'totalMs':30}), partial_text='{"unfinished":')
        raise ModelResponseError('fixture JSON extra data', dict(receipt, finishReason='stop'),
                                 json.dumps(result, ensure_ascii=False) + '\n额外说明')


class ResponseEvidenceTests(unittest.TestCase):
    def test_project_context_includes_javascript_module_files(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder), seed=False)
            studio = Studio(store, BusinessClient())
            pid = studio.create_project('modules', 'Native runtime verification')['id']
            revision = store.current(pid)[0]['revision']
            files = [{'path': name, 'data': base64.b64encode(text.encode()).decode()} for name, text in
                     [('engine.mjs', 'export const ok = true;'), ('loader.cjs', 'module.exports = true;')]]
            studio.import_files(pid, files, revision)
            context = studio.read_context(pid, 'engine loader', budget=100000)
            self.assertTrue({'engine.mjs', 'loader.cjs'} <= {item['path'] for item in context['files']})

    def test_business_rejection_preserves_answer_without_accepting_it(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder), seed=False)
            client = BusinessClient()
            studio = Studio(store, client)
            pid = studio.create_project('evidence', 'Preserve failed validation')['id']
            session = studio.create_session(pid, 'proof', '整理说明', 'general')
            before = store.current(pid)[0]['revision']
            def mutate(stage, result):
                if stage == 'integration':
                    result['moduleAcceptance'][0]['evidence'] = ['missing-report.json']
                return result
            client.mutate = mutate
            studio.start(pid, session['id'], 'live')
            studio.threads[session['id']].join(10)
            rejected = studio.load(pid, session['id'])
            self.assertEqual(rejected['status'], 'failed')
            node = rejected['runs'][-1]['nodes'][-1]
            self.assertEqual(node['responseValidation'], 'failed')
            self.assertNotIn('result', node)
            evidence_path = studio.project_dir(pid) / session['id'] / 'responses' / (node['responseRef'] + '.json')
            original = evidence_path.read_bytes()
            answer = json.loads(original)
            self.assertEqual(answer['result']['moduleAcceptance'][0]['evidence'], ['missing-report.json'])
            self.assertEqual(answer['receipt']['requestedModel'], 'opus')
            self.assertEqual(store.current(pid)[0]['revision'], before)
            client.mutate = None
            studio.start(pid, session['id'], 'live', True)
            studio.threads[session['id']].join(10)
            completed = studio.load(pid, session['id'])
            self.assertEqual(completed['status'], 'completed', completed.get('error'))
            self.assertEqual(completed['runs'][-1]['nodes'][-1]['responseValidation'], 'passed')
            self.assertEqual(evidence_path.read_bytes(), original)


class BusinessFormatRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, failures, failure_kind='json'):
        self.client = MalformedResponseClient(failures, failure_kind)
        self.studio = Studio(Store(Path(self.temp.name), seed=False), self.client)
        self.pid = self.studio.create_project('格式纠正证据', '完成实际交付')['id']
        session = self.studio.create_session(self.pid, '业务任务', '整理说明', 'general')
        self.before_revision = self.studio.store.current(self.pid)[0]['revision']
        self.studio.start(self.pid, session['id'], 'live', coordinator_model='opus', team_models=['opus'])
        self.studio.threads[session['id']].join(10)
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        self.session = self.studio.load(self.pid, session['id'])
        self.run = self.session['runs'][-1]
        self.node = next(node for node in self.run['nodes'] if node['id'] == 'integration')
        self.calls = [call for call in self.run['callMetrics'] if call['nodeId'] == 'integration']

    def test_two_format_corrections_keep_raw_evidence_and_context_chain(self):
        self.execute(2)
        self.assertEqual(self.session['status'], 'completed', self.session.get('error'))
        self.assertEqual(self.client.integration_attempts, 3)
        self.assertEqual([call['outcome'] for call in self.calls], ['error', 'error', 'returned'])
        self.assertEqual(len({call['contextRef'] for call in self.calls}), 3)
        response_root = self.studio.project_dir(self.pid) / self.session['id'] / 'responses'
        self.assertEqual(len(self.node['attempts']), 2)
        for index, attempt in enumerate(self.node['attempts']):
            original = json.loads((response_root / (attempt['responseRef'] + '.json')).read_text())
            self.assertTrue(original['rawText'].endswith('\n额外说明'))
            self.assertEqual(original['contextRef'], self.calls[index]['contextRef'])
            self.assertEqual(original['receipt']['finishReason'], 'stop')
            repaired = self.studio.context(self.pid, self.session['id'], self.calls[index + 1]['contextRef'])
            self.assertEqual(repaired['formatRepair'], index + 1)
            self.assertEqual(repaired['repairOf'], self.calls[index]['contextRef'])
            self.assertEqual(repaired['messages'][-2]['content'], original['rawText'])
            self.assertIn('只纠正格式', repaired['messages'][-1]['content'])
        self.assertEqual(self.node['responseValidation'], 'passed')
        self.assertEqual(len(self.session['agents']['coordinator']['turns']), 2)
        self.assertTrue((self.studio.store.current(self.pid)[1] / 'brief.md').exists())

    def test_invalid_json_stops_after_two_corrections(self):
        self.execute(10)
        self.assertEqual(self.session['status'], 'failed')
        self.assertEqual(self.client.integration_attempts, 3)
        self.assertEqual([call['outcome'] for call in self.calls], ['error'] * 3)
        self.assertEqual(len(self.node['attempts']), 3)
        repairs = [event for event in self.session['events'] if event['type'] == 'repair_requested']
        self.assertEqual(len(repairs), 2)
        self.assertNotIn('result', self.node)
        self.assertEqual(self.studio.store.current(self.pid)[0]['revision'], self.before_revision)

    def test_network_error_does_not_trigger_format_correction(self):
        self.execute(10, 'network')
        self.assertEqual(self.session['status'], 'failed')
        self.assertEqual(self.client.integration_attempts, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(event['type'] == 'repair_requested' for event in self.session['events']))

    def test_incomplete_stream_does_not_trigger_format_correction(self):
        self.execute(10, 'incomplete')
        self.assertEqual(self.session['status'], 'failed')
        self.assertEqual(self.client.integration_attempts, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(event['type'] == 'repair_requested' for event in self.session['events']))
        self.assertIsNone(self.node['attempts'][0]['responseRef'])

    def test_partial_network_output_is_saved_but_never_repaired_or_accepted(self):
        self.execute(10, 'partial')
        self.assertEqual(self.session['status'], 'failed')
        self.assertEqual(self.client.integration_attempts, 1)
        self.assertEqual(self.calls[0]['timings']['totalMs'], 30)
        self.assertEqual(self.calls[0]['phase'], 'integration')
        ref = self.node['attempts'][0]['responseRef']
        saved = json.loads((self.studio.project_dir(self.pid) / self.session['id'] / 'responses' / (ref + '.json')).read_text())
        self.assertEqual(saved['partialText'], '{"unfinished":')
        self.assertNotIn('rawText', saved)
        self.assertNotIn('result', self.node)
        self.assertFalse(any(event['type'] == 'repair_requested' for event in self.session['events']))


if __name__ == '__main__':
    unittest.main()
