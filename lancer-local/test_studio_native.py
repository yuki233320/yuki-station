"""Native bridge tests execute local fixtures only; never native model calls."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import studio_native as native
from studio import Studio
from studio_client import StudioClient
from studio_business import native_verification_checks


class NativePortabilityTests(unittest.TestCase):
    def test_api_project_index_without_bridge_or_platform_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'lancer-local'
            package.mkdir()
            for source in Path(__file__).parent.glob('*.py'):
                shutil.copyfile(source, package / source.name)
            script = '''
import builtins, json, sys
from pathlib import Path
original_import = builtins.__import__
def portable_import(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('fcntl is not installed on this platform')
    return original_import(name, *args, **kwargs)
builtins.__import__ = portable_import
from app import Store
from studio import Studio
if sys.argv[1] == 'windows':
    # Restrict platform simulation to the optional adapter; the host ssl module
    # cannot use Windows certificate APIs on macOS/Python 3.12.
    from types import SimpleNamespace
    import studio_native
    studio_native.sys = SimpleNamespace(platform='win32')
studio = Studio(Store(Path(sys.argv[2]), seed=False))
studio.create_project('Portable API project', 'Use model APIs', '')
result = studio.index()
print(json.dumps({'projects': len(result['projects']), 'native': result['native']}))
'''
            for platform in ('missing-bridge', 'windows'):
                with self.subTest(platform=platform):
                    if platform == 'windows':
                        bridge = root / 'lancer-codex-bridge'
                        bridge.mkdir()
                        for name in ('lancer_adapter.py', 'cli.mjs', 'bridge.mjs', 'rpc.mjs'):
                            (bridge / name).write_text('raise RuntimeError("must not load optional bridge")')
                    run = subprocess.run([sys.executable, '-B', '-c', script, platform, str(root / platform)],
                                         cwd=package, capture_output=True, text=True, timeout=10)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    result = json.loads(run.stdout)
                    self.assertEqual(result['projects'], 1)
                    self.assertFalse(result['native']['available'])
                    self.assertFalse(result['native']['enabled'])
                    self.assertEqual(result['native']['capabilities'], [])
                    self.assertIn('API 工作台', result['native']['reason'])


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'sessions' / 'aaaaaaaaaaaa'
        self.revision = self.root / 'revision'
        (self.revision / 'workspace').mkdir(parents=True)
        (self.revision / 'workspace' / 'README.md').write_text('项目业务规范：只读审查。')
        self.project.mkdir(parents=True)
        self.s = {'projectId': 'aaaaaaaaaaaa', 'id': 'bbbbbbbbbbbb', 'events': []}
        self.run = {'id': 'cccccccccccc', 'projectRevision': 'dddddddddddd'}
        self.n = {'id': 'build-verification', 'agent': 'native_codex'}
        self.context = {'id': 'context-one', 'agentId': 'native-qa', 'memory': {
            'taskRequest': '用实际终端核验上游算法并产出报告。',
            'project': {'files': [{'path': 'README.md', 'text': '截断'}]}}}
        self.shared = {'businessStage': 'build', 'roleId': 'qa',
                       'module': {'id': 'verification', 'scope': ['verification/'],
                                  'requiredCapabilities': ['terminal', 'files'],
                                  'instructions': '运行上游算法并提供报告。'},
                       'upstream': {'algorithm': {'summary': '实际算法', 'files': [
                           {'path': 'src/algorithm.py', 'content': 'print(2 + 2)\n'}]}}}
        self.studio = SimpleNamespace(root=self.root / 'sessions', lock=threading.RLock(),
                                      client=SimpleNamespace(clean=lambda value: value),
                                      store=SimpleNamespace(revision_dir=lambda pid, rid: self.revision),
                                      project_dir=lambda pid: self.project,
                                      save=lambda s: None)
        self.studio.event = lambda s, kind, detail, node, **extra: s['events'].append(dict(type=kind, detail=detail, node=node, **extra))
        self.studio.record_call = lambda *args: Studio.record_call(self.studio, *args)
        self.cancel = threading.Event()
        self.runtime = patch.object(native, 'native_runtime_info', return_value={
            'detected': True, 'nodeBin': '/fixture/node', 'codexBin': '/fixture/codex'})
        self.runtime.start()
        self.calls = 0

    def tearDown(self):
        self.runtime.stop()
        self.tmp.cleanup()

    def fixture(self, task, workspace, state, on_event, **kwargs):
        self.calls += 1
        self.assertTrue(kwargs['allow_workspace_write'])
        self.assertEqual(task['mode'], 'workspace-write')
        self.assertEqual((workspace / 'inputs/project/README.md').read_text(), '项目业务规范：只读审查。')
        self.assertEqual((workspace / 'inputs/upstream/src/algorithm.py').read_text(), 'print(2 + 2)\n')
        path = workspace / 'verification/report.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text('# 核验\n实际输出为 4。\n')
        tool = {'id': 'cmd1', 'type': 'commandExecution', 'status': 'completed', 'exitCode': 0}
        on_event({'type': 'tool_started', 'item': {**tool, 'command': 'python3 -B inputs/upstream/src/algorithm.py'}})
        on_event({'type': 'tool_output', 'itemId': 'cmd1', 'text': '4\n'})
        on_event({'type': 'tool_completed', 'item': tool})
        raw = path.read_bytes()
        return {'status': 'completed', 'engine': 'native-codex-app-server', 'model': 'fixture-native',
                'tools': [tool], 'threadId': 'thread1', 'turnId': 'turn1',
                'output': {'summary': '已运行算法核验', 'observations': [],
                           'artifacts': [{'path': 'verification/report.md', 'description': '报告'}],
                           'checks': [{'name': '算法', 'status': 'passed', 'evidence': 'cmd1 exit 0, output 4'}]},
                'verifiedArtifacts': [{'path': 'verification/report.md', 'bytes': len(raw),
                                       'sha256': hashlib.sha256(raw).hexdigest()}]}

    def execute(self, runner=None):
        with patch.object(native, 'run_native_codex', side_effect=runner or self.fixture):
            return native.execute_native(self.studio, self.s, self.run, self.n, self.context,
                                         [], self.shared, self.cancel, lambda text: None)

    def test_actual_files_evidence_and_no_repeat_call(self):
        result, receipt = self.execute()
        self.assertTrue(result['nativeEvidence']['inputUnchanged'])
        self.assertEqual(result['nativeEvidence']['model'], 'fixture-native')
        self.assertEqual(result['files'][0]['path'], 'verification/report.md')
        self.assertTrue(Path(receipt['receiptPath']).is_file())
        self.assertEqual(os.stat(receipt['manifestPath']).st_mode & 0o777, 0o600)
        self.assertEqual([e['type'] for e in self.s['events']], [
            'native_tool_started', 'tool_output', 'native_tool_completed', 'native_result'])
        again, second = self.execute()
        self.assertEqual(again, result)
        self.assertTrue(second['reused'])
        self.assertEqual(self.calls, 1)
        self.assertEqual((self.revision / 'workspace/README.md').read_text(), '项目业务规范：只读审查。')

    def test_worker_version_is_preserved_in_evidence_and_reused_receipt(self):
        build = {'schema': 'yuki-runtime-build/v1', 'component': 'yuki-native-bridge',
                 'basis': 'worker_module_load_snapshot', 'capturedAt': '2026-10-31T01:00:00Z',
                 'fingerprint': 'a' * 64, 'files': {'bridge.mjs': 'b' * 64}}

        def versioned(*args, **kwargs):
            return {**self.fixture(*args, **kwargs),
                    'receiptSchema': 'yuki-native-receipt/v2', 'workerBuild': build}

        result, receipt = self.execute(versioned)
        saved = Path(receipt['receiptPath']).read_bytes()
        self.assertEqual(result['nativeEvidence']['workerBuild'], build)
        self.assertEqual(result['nativeEvidence']['receiptSchema'], 'yuki-native-receipt/v2')
        repeated, _ = self.execute()
        self.assertEqual(repeated['nativeEvidence']['workerBuild'], build)
        self.assertEqual(Path(receipt['receiptPath']).read_bytes(), saved)
        self.assertEqual(self.calls, 1)

    def test_native_connection_retry_is_retained_as_runtime_evidence(self):
        def reconnecting(*args, **kwargs):
            kwargs['on_event']({'type':'runtime_error','seq':5,'error':{'message':'Reconnecting 2/5','willRetry':True}})
            kwargs['on_event']({'type':'execution_phase','seq':6,'phase':'firstTool','elapsedMs':118000})
            return self.fixture(*args, **kwargs)
        self.execute(reconnecting)
        error = next(e for e in self.s['events'] if e['type'] == 'native_runtime_error')
        self.assertEqual(error['runtimeError']['message'], 'Reconnecting 2/5')
        self.assertTrue(error['runtimeError']['willRetry'])
        self.assertTrue(error['evidenceRef'].endswith('#event-5'))
        phase = next(e for e in self.s['events'] if e['type'] == 'native_execution_phase')
        self.assertEqual(phase['elapsedMs'], 118000)
        self.assertEqual(self.calls, 1)

    def test_historical_receipt_without_version_remains_unknown(self):
        result, receipt = self.execute()
        saved = Path(receipt['receiptPath']).read_bytes()
        repeated, _ = self.execute()
        for item in (result['nativeEvidence'], repeated['nativeEvidence'], json.loads(saved)):
            self.assertNotIn('receiptSchema', item)
            self.assertNotIn('workerBuild', item)
            self.assertNotIn('subAgents', item)
            self.assertNotIn('childEvidenceStatus', item)
        self.assertEqual(Path(receipt['receiptPath']).read_bytes(), saved)
        self.assertEqual(self.calls, 1)

    def test_child_evidence_and_search_references_survive_handoff_and_reuse(self):
        self.run['qualityVersion'] = 1
        self.shared['module'].update(title='子任务核验', requiredCapabilities=['files', 'terminal', 'web_search'])
        child_ref = 'children/child-1.json'
        tool_ref = child_ref + '#turn=child-turn-1&item=search-1'
        source = {'thread': {'id': 'child-1', 'source': {'subAgent': {'thread_spawn': {'parent_thread_id': 'thread1'}}},
                  'turns': [{'id': 'child-turn-1', 'rootTurnId': 'turn1', 'status': 'completed',
                             'items': [{'id': 'search-1', 'type': 'webSearch', 'results': [{'url': 'https://example.org/source'}]}]}]}}
        child_bytes = json.dumps(source).encode()
        subagents = [{'threadId': 'child-1', 'parentThreadId': 'thread1', 'parentTurnId': 'turn1',
                      'status': 'completed', 'turnIds': ['child-turn-1'], 'receiptRef': child_ref,
                      'receiptSha256': hashlib.sha256(child_bytes).hexdigest()}]

        def delegated(task, workspace, state, on_event, **kwargs):
            receipt = self.fixture(task, workspace, state, on_event, **kwargs)
            child_file = state / 'jobs' / task['id'] / child_ref
            child_file.parent.mkdir(parents=True)
            child_file.write_bytes(child_bytes)
            on_event({'type': 'sub_agent_activity', 'seq': 4,
                      'item': {'id': 'delegation-1', 'type': 'subAgentActivity', 'agentThreadId': 'child-1', 'status': 'completed'}})
            receipt.update(subAgents=subagents, childEvidenceStatus='verified')
            receipt['tools'].append({'id': 'child-1:search-1', 'type': 'webSearch', 'status': 'completed',
                'threadId': 'child-1', 'parentThreadId': 'thread1', 'originalToolId': 'search-1',
                'childReceiptRef': child_ref, 'searchKind': 'native', 'searchStatus': 'completed',
                'completedRef': tool_ref, 'resultRef': tool_ref, 'resultExcerpt': 'Actual source content',
                'resultChars': 21, 'resultTruncated': False})
            return receipt

        result, receipt = self.execute(delegated)
        envelope = result['nativeEvidence']
        self.assertEqual(envelope['subAgents'], subagents)
        self.assertEqual(envelope['childEvidenceStatus'], 'verified')
        search = result['nativeSearchEvidence']['searches'][0]
        expected_ref = str(Path(envelope['eventsRef']).parent / child_ref) + '#turn=child-turn-1&item=search-1'
        self.assertEqual(search['eventRef'], expected_ref)
        self.assertEqual(search['resultRef'], expected_ref)
        stored_child = self.project / self.s['id'] / expected_ref.split('#', 1)[0]
        self.assertEqual(stored_child.read_bytes(), child_bytes)
        activity = next(event for event in self.s['events'] if event['type'] == 'native_sub_agent_activity')
        self.assertEqual(activity['activity']['agentThreadId'], 'child-1')
        self.assertEqual(activity['evidenceRef'], envelope['eventsRef'] + '#event-4')
        checks = native_verification_checks(self.shared['module'], result)
        self.assertTrue(next(item['pass'] for item in checks if item['basis'] == 'native_child_evidence'))
        saved = Path(receipt['receiptPath']).read_bytes()
        repeated, _ = self.execute()
        self.assertEqual(repeated['nativeEvidence']['subAgents'], subagents)
        self.assertEqual(repeated['nativeSearchEvidence'], result['nativeSearchEvidence'])
        self.assertEqual(Path(receipt['receiptPath']).read_bytes(), saved)
        self.assertEqual(stored_child.read_bytes(), child_bytes)
        self.assertEqual(self.calls, 1)

    def test_unverified_child_receipt_remains_a_failed_host_check(self):
        self.run['qualityVersion'] = 1
        self.shared['module']['title'] = '子任务核验'

        def unavailable(*args, **kwargs):
            receipt = self.fixture(*args, **kwargs)
            receipt.update(childEvidenceStatus='unverified', childEvidenceError='child receipt unavailable', subAgents=[
                {'threadId': 'missing-child', 'parentThreadId': 'thread1', 'status': 'unverified',
                 'error': 'child thread unavailable'}])
            return receipt

        result, _ = self.execute(unavailable)
        checks = native_verification_checks(self.shared['module'], result)
        child = next(item for item in checks if item.get('basis') == 'native_child_evidence')
        self.assertFalse(child['pass'])
        self.assertEqual(child['subAgents'][0]['status'], 'unverified')
        self.assertEqual(result['nativeEvidence']['childEvidenceError'], 'child receipt unavailable')
        result['nativeEvidence']['childEvidenceStatus'] = 'unknown'
        self.assertFalse(next(item['pass'] for item in native_verification_checks(self.shared['module'], result)
                              if item.get('basis') == 'native_child_evidence'))

    def test_search_result_uses_tool_receipt_and_preserves_full_event_reference(self):
        self.run['qualityVersion'] = 1
        self.shared['module'].update(title='联网核验', requiredCapabilities=['files', 'web_search'])

        def search(task, workspace, state, on_event, **kwargs):
            receipt = self.fixture(task, workspace, state, on_event, **kwargs)
            self.assertIn('真实联网检索', task['instruction'])
            receipt['tools'].append({'id': 'search-1', 'type': 'webSearch', 'status': 'completed',
                'searchKind': 'native', 'searchStatus': 'completed', 'query': 'official documentation',
                'completedRef': 'events.jsonl#event-10', 'resultRef': 'events.jsonl#event-10',
                'resultExcerpt': '[{"url":"https://example.org/source"}]', 'resultChars': 38,
                'resultTruncated': False})
            return receipt

        result, _ = self.execute(search)
        evidence = result['nativeSearchEvidence']['searches'][0]
        self.assertEqual(evidence['toolId'], 'search-1')
        self.assertTrue(evidence['eventRef'].endswith('/events.jsonl#event-10'))
        self.assertIn('https://example.org/source', evidence['resultExcerpt'])
        checks = native_verification_checks(self.shared['module'], result)
        self.assertTrue(next(row['pass'] for row in checks if row['basis'] == 'native_search_receipt'))
        again, _ = self.execute()
        self.assertEqual(again['nativeSearchEvidence'], result['nativeSearchEvidence'])
        self.assertEqual(self.calls, 1)

    def test_model_claim_without_completed_search_cannot_pass_search_gate(self):
        self.run['qualityVersion'] = 1
        self.shared['module'].update(title='联网核验', requiredCapabilities=['files', 'web_search'])
        result, _ = self.execute()
        result['summary'] = '我已完成联网搜索。'
        for searches in ([], [{'toolId': 'failed-search', 'status': 'failed', 'eventRef': 'events.jsonl#event-10'}]):
            with self.subTest(searches=searches):
                result['nativeSearchEvidence']['searches'] = searches
                checks = native_verification_checks(self.shared['module'], result)
                self.assertFalse(next(row['pass'] for row in checks if row['basis'] == 'native_search_receipt'))

    def test_native_directory_caches_configuration_but_never_claims_verified_search(self):
        client = StudioClient()
        client.native_enabled = True
        info = {'available': True, 'nodeBin': '/fixture/node', 'codexBin': '/fixture/codex'}
        with patch.object(native, 'native_runtime_info', return_value=info), patch.object(native, 'native_tool_info',
                return_value={'webSearch': {'available': True, 'mode': 'live', 'verified': False}}) as probe:
            first = client.native_info()
            self.assertEqual(first, client.native_info())
            self.assertIn('web_search', first['capabilities'])
            self.assertFalse(first['webSearch']['verified'])
            self.assertEqual(probe.call_count, 1)

    def test_execution_contract_and_handoff_reach_native_without_repeating_cached_work(self):
        self.run.update(qualityVersion=1, nativeCheckVersion=1)
        contract = {'schema': 'yuki-task-contract/v1', 'id': 'contract-verification',
                    'moduleId': 'verification', 'sourceRevision': self.run['projectRevision'],
                    'criteria': [{'id': 'verification-c1', 'text': '实际运行算法并记录结果', 'required': True}],
                    'fingerprint': 'contract-fingerprint'}
        contract.update(nativeCheckVersion=1, requiredChecks=[{'id': 'run-algorithm',
            'criterionIds': ['verification-c1'], 'command': ['python3', '-B', 'inputs/upstream/src/algorithm.py'],
            'cwd': '.', 'expectedExitCode': 0}])
        self.shared['module'].update(nativeCheckVersion=1, title='算法核验', requiredChecks=contract['requiredChecks'])
        self.shared['module']['requiredCapabilities'].append('web_search')
        packet = {'schema': 'yuki-handoff/v1', 'id': 'handoff-first',
                  'contractId': contract['id'], 'contractFingerprint': contract['fingerprint'],
                  'created': '2026-10-31T01:00:00Z', 'sources': [{'moduleId': 'algorithm', 'nodeId': 'build-algorithm'}]}
        self.shared.update(executionContract=contract, handoff=packet)
        self.context['memory']['sharedEvidence'] = self.shared

        def inspect(task, workspace, state, on_event, **kwargs):
            memory = task['context']
            self.assertNotIn('sharedEvidence', memory)
            self.assertEqual(memory['executionContract'], contract)
            self.assertEqual(memory['handoff']['packet'], packet)
            self.assertEqual(memory['handoff']['nodeId'], self.n['id'])
            self.assertEqual(memory['handoff']['contextRef'], self.context['id'])
            self.assertEqual(memory['handoff']['scope'], self.shared['module']['scope'])
            result = self.fixture(task, workspace, state, on_event, **kwargs)
            result['tools'][0].update(command='python3 -B inputs/upstream/src/algorithm.py', cwd=str(workspace))
            result['tools'].insert(0, {'id': 'explore', 'type': 'commandExecution', 'status': 'failed',
                                      'exitCode': 2, 'command': 'rg missing inputs/', 'cwd': str(workspace)})
            result['tools'].append({'id': 'search-1', 'type': 'webSearch', 'status': 'completed',
                                    'searchKind': 'native', 'searchStatus': 'completed',
                                    'completedRef': 'events.jsonl#event-20'})
            return result

        result, receipt = self.execute(inspect)
        self.assertEqual(result['nativeCheckEvidence']['checks'][0]['status'], 'passed')
        self.assertEqual(result['nativeCheckEvidence']['checks'][0]['toolId'], 'cmd1')
        self.assertEqual(result['nativeCheckEvidence']['operationIssues'][0]['toolId'], 'explore')
        self.assertTrue(all(row['pass'] for row in native_verification_checks(self.shared['module'], result)))
        self.assertIn('native_search_receipt', [row['basis'] for row in native_verification_checks(self.shared['module'], result)])
        original_receipt = Path(receipt['receiptPath']).read_bytes()
        packet.update(id='handoff-retry', created='2026-10-31T01:01:00Z')
        again, reused = self.execute(inspect)
        self.assertEqual(again, result)
        self.assertTrue(reused['reused'])
        self.assertEqual(self.calls, 1)
        self.assertEqual(Path(receipt['receiptPath']).read_bytes(), original_receipt)

    def test_modified_input_is_rejected_and_original_unchanged(self):
        self.run['qualityVersion'] = 1
        def modify(*args, **kwargs):
            receipt = self.fixture(*args, **kwargs)
            p = args[1] / 'inputs/project/README.md'
            p.chmod(0o644)
            p.write_text('tampered')
            return receipt
        with self.assertRaisesRegex(native.NativeExecutionError, '修改了只读输入') as raised:
            self.execute(modify)
        self.assertEqual(raised.exception.receipt['status'], 'failed')
        self.assertEqual((self.revision / 'workspace/README.md').read_text(), '项目业务规范：只读审查。')

    def test_outside_scope_and_unreported_files_rejected(self):
        self.run['qualityVersion'] = 1
        def modify(*args, **kwargs):
            receipt = self.fixture(*args, **kwargs)
            (args[1] / 'outside.txt').write_text('outside')
            return receipt
        with self.assertRaisesRegex(native.NativeExecutionError, '范围以外'):
            self.execute(modify)

    def test_symlinks_and_case_aliases_rejected(self):
        def modify(*args, **kwargs):
            receipt = self.fixture(*args, **kwargs)
            (args[1] / 'verification/link.md').symlink_to(self.revision / 'workspace/README.md')
            return receipt
        with self.assertRaisesRegex(native.NativeExecutionError, '符号链接'):
            self.execute(modify)
        with self.assertRaises(ValueError):
            native._name('verification/e\u0301.md')
        self.shared['upstream']['algorithm']['files'].append({'path': 'src/ALGORITHM.py', 'content': 'different'})
        with self.assertRaisesRegex(native.NativeExecutionError, '别名'):
            self.execute()

    def test_text_checks_without_real_terminal_receipt_fail(self):
        def no_tool(*args, **kwargs):
            receipt = self.fixture(*args, **kwargs)
            receipt['tools'] = []
            return receipt
        with self.assertRaisesRegex(native.NativeExecutionError, '真实成功完成'):
            self.execute(no_tool)
        with self.assertRaises(native.NativeExecutionError):
            self.execute()
        self.assertEqual(self.calls, 1)

    def test_hash_tampering_fails(self):
        self.run['qualityVersion'] = 1
        def mismatch(*args, **kwargs):
            receipt = self.fixture(*args, **kwargs)
            receipt['verifiedArtifacts'][0]['sha256'] = '0' * 64
            return receipt
        with self.assertRaisesRegex(native.NativeExecutionError, '哈希不一致'):
            self.execute(mismatch)

    def test_changed_request_refuses_reuse(self):
        _, receipt = self.execute()
        receipt_path = Path(receipt['receiptPath'])
        original_receipt = receipt_path.read_bytes()
        self.shared['module']['instructions'] = '不同的工作'
        with self.assertRaisesRegex(native.NativeExecutionError, '上下文或文件发生变化') as raised:
            self.execute()
        self.assertEqual(self.calls, 1)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        recovery = json.loads(Path(raised.exception.receipt['recoveryErrorPath']).read_text())
        self.assertEqual(recovery['status'], 'failed')
        self.assertEqual(recovery['originalReceiptStatus'], 'completed')
        self.assertIn('上下文或文件发生变化', recovery['validationError'])

    def test_old_plan_module_directory_is_not_a_real_input_path(self):
        self.shared['module']['instructions'] += '旧计划输入路径：inputs/upstream/algorithm/src/algorithm.py'
        def inspect(task, workspace, state, on_event, **kwargs):
            self.assertIn('不加模块ID目录', task['instruction'])
            self.assertEqual([item['path'] for item in task['context']['project']['files']],
                             ['inputs/project/README.md', 'inputs/upstream/src/algorithm.py'])
            self.assertFalse((workspace / 'inputs/upstream/algorithm').exists())
            return self.fixture(task, workspace, state, on_event, **kwargs)
        self.execute(inspect)

    def test_changed_cached_artifact_preserves_original_success_receipt(self):
        _, receipt = self.execute()
        receipt_path = Path(receipt['receiptPath'])
        original_receipt = receipt_path.read_bytes()
        (receipt_path.parent / 'workspace/verification/report.md').write_text('changed after success')
        with self.assertRaisesRegex(native.NativeExecutionError, '哈希不一致') as raised:
            self.execute()
        self.assertEqual(self.calls, 1)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertTrue(Path(raised.exception.receipt['recoveryErrorPath']).exists())

    def test_unknown_failure_never_restarts(self):
        self.run['qualityVersion'] = 1
        def interrupted(*args, **kwargs):
            self.calls += 1
            error = RuntimeError('fixture interrupted')
            error.status = 'unknown'
            raise error
        with self.assertRaises(native.NativeExecutionError) as raised:
            self.execute(interrupted)
        self.assertEqual(raised.exception.receipt['status'], 'unknown')
        with self.assertRaises(native.NativeExecutionError):
            self.execute()
        self.assertEqual(self.calls, 1)
        self.assertEqual([record['outcome'] for record in self.run['callMetrics']], ['error'])

    def test_approval_status_is_preserved(self):
        self.run['qualityVersion'] = 1
        def approval(*args, **kwargs):
            kwargs['on_event']({'type': 'approval_required', 'request': {'method': 'approval'}})
            return {'status': 'awaiting_approval', 'engine': 'native-codex-app-server', 'pending': [{'method': 'approval'}]}
        # kwargs invocation differs from fixture positional binding; runner receives named event.
        with self.assertRaises(native.NativeExecutionError) as raised:
            self.execute(approval)
        self.assertEqual(raised.exception.receipt['status'], 'awaiting_approval')
        self.assertTrue(any(e['type'] == 'native_approval_required' for e in self.s['events']))

    def test_completed_failed_or_not_run_checks_reach_quality_gate(self):
        self.run['qualityVersion'] = 1
        self.shared['module']['title'] = '实际终端核验'
        for status in ('failed', 'not_run'):
            with self.subTest(status=status):
                self.n['id'] = 'build-verification-' + status.replace('_', '-')
                def completed_with_unsatisfied_check(*args, **kwargs):
                    receipt = self.fixture(*args, **kwargs)
                    receipt['output']['checks'][0]['status'] = status
                    receipt['output']['checks'][0]['evidence'] = 'cmd1 未满足必要验证项'
                    if status == 'failed':
                        receipt['tools'][0]['exitCode'] = 1
                    else:
                        receipt['tools'] = []
                    return receipt
                result, receipt = self.execute(completed_with_unsatisfied_check)
                self.assertEqual(receipt['status'], 'completed')
                self.assertEqual(result['files'][0]['content'], '# 核验\n实际输出为 4。\n')
                self.assertEqual(result['nativeEvidence']['checks'][0]['status'], status)
                checks = native_verification_checks(self.shared['module'], result)
                self.assertFalse(next(check for check in checks if check['basis'] == 'native_tool_receipt')['pass'])
                self.assertFalse(next(check for check in checks if check['basis'] == 'native_reported_checks')['pass'])
                cached, cached_receipt = self.execute()
                self.assertEqual(cached, result)
                self.assertTrue(cached_receipt['reused'])
        self.assertEqual(self.calls, 2)
        self.assertEqual([record['outcome'] for record in self.run['callMetrics']], ['returned', 'returned'])

    def test_legacy_still_rejects_completed_failed_checks(self):
        def failed_check(*args, **kwargs):
            receipt = self.fixture(*args, **kwargs)
            receipt['output']['checks'][0]['status'] = 'failed'
            return receipt
        with self.assertRaisesRegex(native.NativeExecutionError, '检查包含失败'):
            self.execute(failed_check)
        self.assertNotIn('callMetrics', self.run)

    def test_cached_native_result_does_not_add_call_metrics(self):
        self.run['qualityVersion'] = 1
        result, _ = self.execute()
        first = list(self.run['callMetrics'])
        again, receipt = self.execute()
        self.assertEqual(again, result)
        self.assertTrue(receipt['reused'])
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.run['callMetrics'], first)
        self.assertEqual(first[0]['adapter'], 'native')
        self.assertEqual(first[0]['outcome'], 'returned')
        self.assertGreaterEqual(first[0]['elapsedMs'], 0)
        self.assertEqual(first[0]['contextRef'], self.context['id'])

    def test_preflight_failure_does_not_add_call_metrics(self):
        self.run['qualityVersion'] = 1
        self.shared['module']['requiredCapabilities'].append('browser')
        with self.assertRaisesRegex(native.NativeExecutionError, '已接入的文件、终端和联网搜索能力'):
            self.execute()
        self.assertEqual(self.calls, 0)
        self.assertNotIn('callMetrics', self.run)

    def test_paths_and_hidden_credentials_refused(self):
        for path in ('../escape', '/escape', '.env', 'files/.secret', 'x/../y', 'x//y', 'x\\y', 'id_rsa'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                native._name(path)

    def test_host_owned_project_routing_collects_results_without_changing_workspace(self):
        from studio_native_collection import configure_collection
        archive_root = self.root / 'dedicated-project'
        archive_root.mkdir()
        configure_collection(self.root, 'host-project', '雪站桥接', archive_root)
        self.shared['module']['projectId'] = 'model-forged-project'
        def routed(task, workspace, state, on_event, **kwargs):
            self.assertEqual(task['projectId'], 'host-project')
            self.assertEqual(workspace, (self.project / self.s['id'] / 'native' / self.run['id'] / self.n['id'] / 'workspace').resolve())
            return {**self.fixture(task, workspace, state, on_event, **kwargs), 'projectId': task['projectId']}
        result, receipt = self.execute(routed)
        self.assertEqual(result['nativeEvidence']['collection']['status'], 'archived')
        self.assertTrue(Path(receipt['collection']['archivePath']).is_file())
        self.execute(routed)
        self.assertEqual(self.calls, 1)

    def test_unverified_project_membership_does_not_report_collection_success(self):
        from studio_native_collection import configure_collection
        archive_root = self.root / 'dedicated-project'
        archive_root.mkdir()
        configure_collection(self.root, 'host-project', '雪站桥接', archive_root)
        with self.assertRaisesRegex(native.NativeExecutionError, '所属 Codex 项目未核实'):
            self.execute()
        self.assertFalse((archive_root / 'transfers').exists())

    def test_failed_native_execution_is_collected_without_claiming_acceptance(self):
        from studio_native_collection import configure_collection
        archive_root = self.root / 'dedicated-project'
        archive_root.mkdir()
        configure_collection(self.root, 'host-project', '雪站桥接', archive_root)
        def failed(task, workspace, state, on_event, **kwargs):
            receipt = self.fixture(task, workspace, state, on_event, **kwargs)
            receipt.update(projectId=task['projectId'], status='failed', error='真实工具命令失败')
            receipt['tools'][0]['exitCode'] = 1
            return receipt
        with self.assertRaises(native.NativeExecutionError) as raised:
            self.execute(failed)
        result = raised.exception.receipt['collection']
        self.assertEqual(result['status'], 'archived')
        manifest = json.loads(Path(result['archivePath']).read_text())
        self.assertEqual(manifest['status'], 'failed')
        self.assertFalse(manifest['artifactsAccepted'])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, body):
        script = self.root / 'fake-node'
        script.write_text('#!' + sys.executable + '\n' + body)
        script.chmod(0o700)
        return str(script)

    def test_large_stderr_does_not_deadlock(self):
        executable = self.script('import sys,json\nsys.stdin.read()\nsys.stderr.write("diagnostic"*200000)\nsys.stderr.flush()\nprint(json.dumps({"type":"receipt","receipt":{"status":"completed"}}),flush=True)\n')
        receipt = native.run_native_codex({'id': 'fixture'}, self.root, self.root / 'state',
                                                   node_bin=executable, timeout_ms=3000)
        self.assertEqual(receipt['status'], 'completed')

    def test_cancellation_terminates_promptly(self):
        # Deliberately never consume stdin: cancellation must also interrupt a full pipe.
        executable = self.script('import time\ntime.sleep(60)\n')
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, '取消') as raised:
                native.run_native_codex({'id': 'fixture', 'context': 'x' * 390000}, self.root, self.root / 'state',
                                                 node_bin=executable, timeout_ms=10000, cancel=cancel)
            self.assertEqual(raised.exception.status, 'unknown')
            self.assertLess(time.monotonic() - started, 3)
        finally:
            timer.cancel()


if __name__ == '__main__':
    unittest.main()
