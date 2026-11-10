import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from app import Store
from collaboration_evaluation import CASE_ID, DEVELOPMENT_CASE_ID, case_definition
from controlled_experiment import ControlledExperimentRunner
from studio import Studio
from studio_client import StudioClient


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = json.dumps({'data': [{'id': mid + '-fixture'} for mid in ('deepseek', 'sol')]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        prompt = json.loads(payload['messages'][-1]['content'])
        self.server.requests.append((payload, prompt))
        result, usage, finish = self.server.answer(prompt)
        event = {'choices': [{'delta': {'content': json.dumps(result)}, 'finish_reason': finish}],
                 'model': payload['model'], 'usage': usage}
        content = ('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class ControlledExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), ProviderHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        profiles = {mid: {'provider': 'fixture', 'label': 'local boundary fixture',
                         'base': 'http://127.0.0.1:' + str(self.server.server_port),
                         'model': mid + '-fixture', 'protocol': 'chat'} for mid in ('deepseek', 'sol')}
        self.client = StudioClient(profiles)
        self.client.configure('fixture', 'test-only-credential')
        self.client.discover('fixture')
        self.studio = Studio(Store(Path(self.tmp.name), seed=False), self.client)
        self.pid = self.studio.create_project('受控对照', '')['id']
        self.runner = ControlledExperimentRunner(self.studio)
        self.server.requests = []
        self.server.answer = self.answer

    def tearDown(self):
        self.runner.cancel.set()
        if self.runner.thread:
            self.runner.thread.join(20)
        self.tmp.cleanup()

    def answer(self, prompt):
        if prompt['role'] == 'plan':
            result = {'summary': '规则与结果分别核对', 'assignments': [
                {'agentId': role, 'title': role, 'task': '完成全部交付并核对业务规则'}
                for role in ('worker1', 'worker2')]}
        else:
            result = {'summary': '已生成候选，未运行工具', 'files': [{
                'path': 'result.json', 'content': json.dumps(case_definition()['reference']['result'])}]}
        return result, {'prompt_tokens': 120, 'completion_tokens': 80}, 'stop'

    def run_experiment(self, **kwargs):
        initial = self.runner.start(self.pid, 'deepseek', ['deepseek', 'sol'], **kwargs)
        self.runner.thread.join(30)
        self.assertFalse(self.runner.thread.is_alive())
        root = self.studio.project_dir(self.pid) / 'controlled-experiments' / initial['id']
        return json.loads((root / 'record.json').read_text()), root

    def test_controlled_calls_share_reserved_capacity_with_business_tasks(self):
        seen = []
        def answer(prompt):
            pool = 'control' if prompt['role'] == 'plan' else 'worker'
            state = self.studio.capacity.snapshot()
            self.assertGreater(state['pools'][pool]['active'], 0)
            self.assertLessEqual(state['totalActive'], 3)
            seen.append(prompt['role'])
            return self.answer(prompt)
        self.server.answer = answer
        record, _ = self.run_experiment()
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(set(seen), {'single', 'plan', 'worker1', 'worker2', 'aggregate'})
        for arm in record['arms'].values():
            for call in arm['calls']:
                self.assertEqual(call['executionPool'], 'control' if call['role'] == 'plan' else 'worker')
                self.assertGreaterEqual(call['capacityWaitMs'], 0)
        self.assertEqual(self.studio.capacity.snapshot()['totalActive'], 0)

    def test_true_sas_provider_defaults_context_isolation_and_real_grading(self):
        before = copy.deepcopy(self.studio.store.current(self.pid)[0])
        record, root = self.run_experiment()
        self.assertEqual(record['status'], 'completed')
        self.assertTrue(record['homogeneous'])
        self.assertEqual(record['selection']['activeModels'], ['deepseek'])
        self.assertEqual(len(record['arms']['single']['calls']), 1)
        multi = record['arms']['multi']
        self.assertTrue(all(call['cap'] is None for call in multi['calls']))
        for arm in record['arms'].values():
            self.assertIsNone(arm['configuredBudget'])
            self.assertTrue(all(call['cap'] is None for call in arm['calls']))
            self.assertEqual(arm['evaluation']['passed'], 15)
            self.assertTrue(arm['evaluation']['accepted'])
            self.assertTrue(arm['metrics']['usage']['complete'])
            self.assertIsNone(arm['metrics']['cost']['amount'])
            for call in arm['calls']:
                self.assertEqual(call['model'], 'deepseek')
                for ref, sha in [('inputRef', 'inputSha256'), ('outputRef', 'outputSha256')]:
                    path = self.studio.project_dir(self.pid) / call[ref]
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), call[sha])
        self.assertEqual(self.studio.store.current(self.pid)[0], before)
        self.assertEqual(len(self.studio.store.list_projects()), 1)
        requests = {prompt['role']: (body, prompt) for body, prompt in self.server.requests}
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(body['max_tokens'] == 16000 for body, _ in self.server.requests))
        for arm in record['arms'].values():
            for call in arm['calls']:
                request = json.loads((self.studio.project_dir(self.pid) / call['inputRef']).read_text())
                self.assertIsNone(request['max_output_tokens'])
                self.assertIn('技术默认值', request['budgetNotice'])
        self.assertEqual(len(requests['single'][0]['messages']), 2)
        self.assertEqual(requests['single'][1]['handoffs'], {})
        for role in ('worker1', 'worker2'):
            self.assertEqual(set(requests[role][1]['handoffs']), {'plan', 'assignment'})
            call = next(call for call in multi['calls'] if call['role'] == role)
            self.assertEqual(call['evaluation']['passed'], 15)
        self.assertEqual(set(requests['aggregate'][1]['handoffs']), {'plan', 'worker1', 'worker2'})
        for _, prompt in self.server.requests:
            self.assertNotIn('reference', prompt['task'])
            self.assertNotIn('evaluation', json.dumps(prompt['handoffs']))
            self.assertEqual(prompt['task']['allowedArtifactPaths'], ['result.json'])
            self.assertEqual(prompt['responseSchema']['allowedArtifactPaths'], ['result.json'])
            self.assertEqual(prompt['task']['instructions'], case_definition()['instructions'])
            self.assertIn('模块文件所有权和交接路径', prompt['experimentProtocol'])
            self.assertIn('隔离回复', prompt['experimentProtocol'])
        self.assertEqual(record['budget']['allocation']['schema'], 'yuki-controlled-protocol/v3')
        self.assertEqual(record['budget']['mode'], 'provider_default')
        self.assertIsNone(record['budget']['cap'])
        self.assertEqual(record['budget']['enforcement'], 'none')
        self.assertTrue(all('shares' not in item for name in ('single', 'multi')
                            for item in record['budget']['allocation'][name]))
        self.assertEqual(record['evaluationPolicy'], {'schema': 'yuki-egt-policy/v2', 'costControl': False})
        self.assertIn('不得指派任何其他文件或交接路径', requests['plan'][1]['instruction'])
        self.assertNotIn('files', requests['plan'][1]['responseSchema'])
        self.assertGreaterEqual(multi['metrics']['observedWallMs'], multi['metrics']['wallMs'])
        for path in root.rglob('*.json'):
            self.assertNotIn('test-only-credential', path.read_text())

    def test_incorrect_candidate_is_not_approved_by_model_summary(self):
        def wrong(prompt):
            result, usage, finish = self.answer(prompt)
            if 'files' in result:
                result['summary'] = '全部正确'
                result['files'][0]['content'] = '{}'
            return result, usage, finish
        self.server.answer = wrong
        record, _ = self.run_experiment()
        for arm in record['arms'].values():
            self.assertEqual(arm['status'], 'completed')
            self.assertFalse(arm['evaluation']['accepted'])
            self.assertEqual(arm['evaluation']['passed'], 0)

    def test_missing_usage_and_failed_output_are_retained_without_retry(self):
        def incomplete(prompt):
            result, usage, finish = self.answer(prompt)
            return result, {}, 'length' if prompt['role'] == 'single' else finish
        self.server.answer = incomplete
        record, _ = self.run_experiment()
        self.assertEqual(len(self.server.requests), 5)
        single = record['arms']['single']
        self.assertEqual(single['status'], 'failed')
        self.assertEqual(single['calls'][0]['receipt']['finishReason'], 'length')
        self.assertFalse(single['evaluation']['accepted'])
        response = json.loads((self.studio.project_dir(self.pid) / single['calls'][0]['outputRef']).read_text())
        self.assertIn('files', json.loads(response['partialText']))
        for arm in record['arms'].values():
            self.assertFalse(arm['metrics']['usage']['complete'])
            self.assertIsNone(arm['metrics']['usage']['totalTokens'])

    def test_nonhomogeneous_workers_use_selected_other_model(self):
        record, _ = self.run_experiment(homogeneous=False)
        self.assertFalse(record['homogeneous'])
        for call in record['arms']['multi']['calls']:
            self.assertEqual(call['model'], 'sol' if call['role'].startswith('worker') else 'deepseek')

    def test_validation_creates_no_experiment_or_api_request(self):
        requests = [{'budget': n} for n in (True, False, 0, 8000, 16000, 64000, 16000.0, '16000')]
        requests += [{'repetitions': n} for n in (True, 0, 6, 1.1)]
        requests += [{'homogeneous': 'true'}, {'case_id': 'missing'}]
        for arguments in requests:
            with self.assertRaises(ValueError):
                self.runner.start(self.pid, 'deepseek', ['deepseek'], **arguments)
        with self.assertRaises(ValueError):
            self.runner.start(self.pid, 'deepseek', ['deepseek'], homogeneous=False)
        self.assertFalse(list(self.studio.project_dir(self.pid).glob('controlled-experiments/*')))
        self.assertEqual(self.server.requests, [])

    def test_fixed_budget_is_explicitly_rejected_before_any_request(self):
        with self.assertRaisesRegex(ValueError, '固定额度分配已取消，请刷新'):
            self.runner.start(self.pid, 'deepseek', ['deepseek'], budget=16000)
        self.assertEqual(self.server.requests, [])

    def test_stop_keeps_first_request_and_does_not_start_more_or_drop_repetitions(self):
        entered, release = threading.Event(), threading.Event()
        def wait(prompt):
            entered.set()
            release.wait(10)
            return self.answer(prompt)
        self.server.answer = wait
        initial = self.runner.start(self.pid, 'deepseek', ['deepseek'], repetitions=2)
        self.assertTrue(entered.wait(10))
        self.runner.stop(self.pid, initial['id'])
        release.set()
        self.runner.thread.join(20)
        paths = list(self.studio.project_dir(self.pid).glob('controlled-experiments/*/record.json'))
        records = [json.loads(path.read_text()) for path in paths]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(row['status'] == 'stopped' for row in records))
        self.assertEqual(len(self.server.requests), 1)
        attempted = [arm for row in records for arm in row['arms'].values() if arm['calls']]
        self.assertEqual(len(attempted), 1)
        self.assertEqual(attempted[0]['calls'][0]['status'], 'stopped')

    def test_export_contains_receipts_and_hashes_but_no_project_history(self):
        record, root = self.run_experiment()
        with zipfile.ZipFile(io.BytesIO(self.runner.export(self.pid, record['id']))) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('record.json', archive.namelist())
            self.assertIn('task.json', archive.namelist())
            self.assertEqual(len(archive.namelist()), 12)
            self.assertFalse(any('project.json' in name for name in archive.namelist()))
            self.assertEqual(json.loads(archive.read('record.json')), record)
        for experiment_id in ('../secret', '', 1):
            with self.assertRaises(ValueError):
                self.runner.export(self.pid, experiment_id)

    def test_index_computes_both_algorithms_without_mutating_raw_evidence(self):
        record, root = self.run_experiment()
        before = (root / 'record.json').read_bytes()
        index = self.runner.index(self.pid)
        displayed = index['records'][0]
        self.assertEqual(displayed['id'], record['id'])
        self.assertEqual(displayed['quantification']['qualityDeltaPoints'], 0)
        self.assertFalse(displayed['quantification']['decision']['provesSuperiority'])
        self.assertEqual(displayed['quantification']['budgetAudit']['status'], 'not_controlled')
        self.assertFalse(displayed['quantification']['egt']['enabled'])
        self.assertIsNone(displayed['quantification']['egt']['lambda'])
        self.assertEqual(displayed['quantification']['decision']['status'], 'descriptive_uncontrolled')
        self.assertIn('lexicalDiversity', displayed['process'])
        self.assertEqual(index['summaries'][0]['assessedPairs'], 1)
        for arm in displayed['arms'].values():
            for call in arm['calls']:
                self.assertEqual(set(call['result']), {'summary'})
                self.assertIn('outputRef', call)
        self.assertEqual((root / 'record.json').read_bytes(), before)

    def test_legacy_conflicting_protocol_stays_visible_but_is_excluded(self):
        record, root = self.run_experiment()
        record['budget']['allocation']['schema'] = 'yuki-controlled-protocol/v1'
        del record['evaluationPolicy']
        self.studio._write(root / 'record.json', record)
        before = (root / 'record.json').read_bytes()
        index = self.runner.index(self.pid)
        displayed = index['records'][0]
        self.assertIn('契约冲突', displayed['evaluationNotice'])
        self.assertEqual(displayed['quantification']['decision']['status'], 'invalid_protocol')
        self.assertIsNone(displayed['quantification']['qualityDeltaPoints'])
        self.assertEqual(index['summaries'], [])
        self.assertEqual(displayed['arms']['single']['evaluation']['passed'], 15)
        self.assertEqual((root / 'record.json').read_bytes(), before)
        with zipfile.ZipFile(io.BytesIO(self.runner.export(self.pid, record['id']))) as archive:
            self.assertEqual(archive.read('record.json'), before)

    def test_v2_saved_budget_policy_is_read_only_and_still_audited(self):
        record, root = self.run_experiment()
        allocation = copy.deepcopy(record['budget']['allocation'])
        allocation['schema'] = 'yuki-controlled-protocol/v2'
        for name, shares in (('single', [8]), ('multi', [1, 2, 2, 3])):
            record['arms'][name]['configuredBudget'] = 16000
            for specification, call, share in zip(allocation[name], record['arms'][name]['calls'], shares):
                specification['shares'] = share
                call['cap'] = 16000 * share // 8
        record['budget'] = {'unit': 'provider_output_tokens', 'cap': 16000, 'allocation': allocation,
                            'enforcement': 'per_request_parameter', 'totalComputeMatched': False}
        record['evaluationPolicy'] = {'schema': 'yuki-egt-policy/v1', 'lambda': 1.0, 'corLimit': 0.25}
        self.studio._write(root / 'record.json', record)
        before = (root / 'record.json').read_bytes()
        displayed = self.runner.index(self.pid)['records'][0]
        self.assertEqual(displayed['quantification']['budgetAudit']['status'], 'output_allowance_only')
        self.assertTrue(displayed['quantification']['egt']['enabled'])
        self.assertEqual(displayed['quantification']['egt']['lambda'], 1.0)
        self.assertEqual((root / 'record.json').read_bytes(), before)

    def test_extra_handoff_path_is_rejected_and_raw_reply_is_retained(self):
        def conflicting_output(prompt):
            result, usage, finish = self.answer(prompt)
            if prompt['role'] == 'worker1':
                result['files'][0]['path'] = 'handoff/ledger.json'
            return result, usage, finish
        self.server.answer = conflicting_output
        record, _ = self.run_experiment()
        multi = record['arms']['multi']
        self.assertEqual(multi['status'], 'failed')
        self.assertEqual([call['role'] for call in multi['calls']], ['plan', 'worker1'])
        call = multi['calls'][-1]
        self.assertIn('不接受其他交接路径', call['error'])
        response = json.loads((self.studio.project_dir(self.pid) / call['outputRef']).read_text())
        self.assertEqual(response['result']['files'][0]['path'], 'handoff/ledger.json')
        self.assertTrue(multi['metrics']['usage']['complete'])
        self.assertEqual(len(self.server.requests), 3)

    def test_development_candidates_are_run_by_existing_host_grader(self):
        from test_development_evaluation import SCHEDULER, REPORTS, CLI
        from development_evaluation import ARTIFACT_PATHS
        def code(prompt):
            result, usage, finish = self.answer(prompt)
            if prompt['role'] != 'plan':
                result['files'] = [{'path': path, 'content': content}
                                   for path, content in zip(ARTIFACT_PATHS, (SCHEDULER, REPORTS, CLI))]
            return result, usage, finish
        self.server.answer = code
        record, _ = self.run_experiment(case_id=DEVELOPMENT_CASE_ID)
        for arm in record['arms'].values():
            self.assertTrue(arm['evaluation']['accepted'], arm['evaluation'])
            self.assertEqual(arm['evaluation']['runtime']['exitCode'], 0)
        for call in record['arms']['multi']['calls']:
            if call['role'].startswith('worker'):
                self.assertEqual(call['evaluation']['passed'], 16)
        for _, prompt in self.server.requests:
            self.assertEqual(prompt['task']['allowedArtifactPaths'], list(ARTIFACT_PATHS))
            self.assertEqual(prompt['responseSchema']['allowedArtifactPaths'], list(ARTIFACT_PATHS))
            if prompt['role'] != 'plan':
                self.assertEqual([item['path'] for item in prompt['responseSchema']['files']], list(ARTIFACT_PATHS))
        self.assertGreater(record['arms']['multi']['metrics']['diagnosticEvaluationMs'], 0)


if __name__ == '__main__':
    unittest.main()
