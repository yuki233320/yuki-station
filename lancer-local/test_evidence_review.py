"""Evidence re-review exercises the real workflow with one deterministic provider."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Conflict, Store
from evidence_review import start_evidence_review
from native_checks import bind_required_checks
from studio import Studio
from test_studio_business import BusinessClient, module, native_result


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = BusinessClient([dict(module('verification', 'general'), requiredCapabilities=['files', 'terminal'])])
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('补证测试', '核对工具实际参数与结果', '')['id']
        self.native_calls = 0

    def tearDown(self):
        for thread in self.studio.threads.values():
            thread.join(15)
        self.tmp.cleanup()

    def native_execute(self, studio, session, run, node, context, messages, shared, cancel, delta):
        self.native_calls += 1
        root = studio.project_dir(self.pid) / session['id']
        ref = 'native/' + run['id'] + '/' + node['id']
        job = root / ref
        workspace = job / 'workspace'
        workspace.mkdir(parents=True)
        output = native_result()
        evidence = output['nativeEvidence']
        evidence.update(receiptRef=ref + '/receipt.json', manifestRef=ref + '/input-manifest.json',
                        eventsRef=ref + '/events.jsonl', threadId='thread-1', turnId='turn-1')
        evidence['tools'][0]['cwd'] = str(workspace)
        evidence['tools'][0]['exitCode'] = getattr(self, 'command_exit', 0)
        raw_tools = [copy.deepcopy(evidence['tools'][0])]
        for ident, status, arguments, result in (
            ('expected-error', 'failed', {'code': "throw new Error('expected-recovery-probe')"}, {'content': [{'type': 'text', 'text': 'expected-recovery-probe'}]}),
            ('recovered', 'completed', {'code': 'nodeRepl.write({recovered:true})'}, {'content': [{'type': 'text', 'text': '{recovered:true}'}]}),
            ('search', 'completed', {'search_query': [{'q': 'official docs'}]}, {'content': [{'type': 'text', 'text': 'Official document\n' + 'x' * 19000 + '\nEnd source.'}]}),
        ):
            raw = {'id': ident, 'type': 'mcpToolCall', 'server': 'fixture', 'tool': 'search' if ident == 'search' else 'js',
                   'status': status, 'arguments': arguments, 'result': result}
            if ident == 'search' and getattr(self, 'native_search', False):
                raw.update(type='webSearch', action=raw.pop('arguments'),
                           results=[{'url': 'https://example.test/docs', 'text': 'Document body'}])
                del raw['result']
            raw_tools.append(raw)
            compact = {k: raw[k] for k in ('id', 'type', 'server', 'tool', 'status')}
            if ident == 'search':
                compact.update(searchKind='mcp', searchStatus='completed', resultExcerpt='old summary')
            evidence['tools'].append(compact)
            if raw['type'] == 'webSearch':
                del raw['status']
        events = []
        for index, item in enumerate(raw_tools):
            seq = 2 * index + 1
            events += [{'taskId': 'task-1', 'seq': seq, 'type': 'tool_started', 'item': dict(item, status='inProgress')},
                       {'taskId': 'task-1', 'seq': seq + 1, 'type': 'tool_completed', 'item': item}]
        (job / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        (job / 'input-manifest.json').write_text(json.dumps({'inputs': []}))
        for item in output['files']:
            (workspace / item['path']).write_text(item['content'])
        receipt = {'taskId': 'task-1', 'threadId': 'thread-1', 'turnId': 'turn-1', 'tools': evidence['tools'], 'workspace': str(workspace)}
        if getattr(self, 'with_child', False):
            child_ref = 'children/child-1.json'
            child_item = {'id': 'child-search', 'type': 'webSearch', 'action': {'query': 'child official source'},
                          'results': [{'url': 'https://example.test/child', 'text': 'Child document body'}]}
            child_raw = json.dumps({'thread': {'id': 'child-1', 'cwd': str(workspace),
                'source': {'subAgent': {'thread_spawn': {'parent_thread_id': 'thread-1'}}},
                'turns': [{'id': 'child-turn', 'rootTurnId': 'turn-1', 'status': 'completed', 'items': [child_item]}]}}).encode()
            (job / 'children').mkdir()
            (job / child_ref).write_bytes(child_raw)
            receipt['subAgents'] = [{'threadId': 'child-1', 'parentThreadId': 'thread-1', 'parentTurnId': 'turn-1',
                                    'receiptRef': child_ref, 'receiptSha256': hashlib.sha256(child_raw).hexdigest(), 'status': 'completed'}]
            evidence.update(subAgents=copy.deepcopy(receipt['subAgents']), childEvidenceStatus='verified')
            evidence['tools'].append({'id': 'child-1:child-search', 'originalToolId': 'child-search', 'type': 'webSearch',
                'status': 'completed', 'threadId': 'child-1', 'parentThreadId': 'thread-1', 'turnId': 'child-turn',
                'childReceiptRef': child_ref, 'searchKind': 'native', 'searchStatus': 'completed'})
            events.append({'taskId': 'task-1', 'seq': 9, 'type': 'sub_agent_activity',
                           'item': {'type': 'subAgentActivity', 'agentThreadId': 'child-1', 'kind': 'started'}})
            (job / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        (job / 'receipt.json').write_text(json.dumps(receipt))
        output['nativeSearchEvidence'] = {'schema': 'yuki-native-search/v1', 'searches': [
            {'toolId': 'search', 'status': 'completed', 'kind': 'mcp', 'eventRef': ref + '/events.jsonl#event-8', 'resultExcerpt': 'old summary'}]}
        output['nativeCheckEvidence'] = bind_required_checks(shared['executionContract'], evidence, workspace)
        return output, {'mode': 'fixture-native'}

    def failed_workflow(self):
        self.client.reject = True
        def needs_input(stage, result):
            if stage == 'integration':
                for item in result['moduleAcceptance']:
                    item['repairAction'] = 'needs_input'
            return result
        self.client.mutate = needs_input
        session = self.studio.create_session(self.pid, '工具验收', '核对原生工具实际参数与结果', 'general')
        session['workflowVersion'] = 2
        self.studio.save(session)
        with patch('studio_native.execute_native', side_effect=self.native_execute):
            self.studio.start(self.pid, session['id'])
            self.studio.threads[session['id']].join(15)
        session = self.studio.load(self.pid, session['id'])
        self.assertEqual(session['status'], 'failed', session.get('error'))
        self.assertEqual(len(session['runs'][-1]['qualityHistory']), 1)
        self.client.reject = False
        self.client.mutate = None
        return session

    def review(self, session):
        before = len(self.client.calls)
        start_evidence_review(self.studio, self.pid, session['id'], session['version'])
        self.studio.threads[session['id']].join(15)
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        return self.studio.load(self.pid, session['id']), self.client.calls[before:]

    def test_re_review_calls_only_coordinator_preserves_originals_and_commits(self):
        session = self.failed_workflow()
        old = copy.deepcopy(session['runs'][-1])
        root = self.studio.project_dir(self.pid) / session['id']
        old_bytes = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        result, calls = self.review(session)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(self.native_calls, 1)
        self.assertEqual([c['key'][0] for c in calls], ['integration'])
        run = result['runs'][-1]
        review = run['evidenceReviews'][-1]
        self.assertTrue(review['qualityPassed'])
        self.assertGreater(review['completedEventSeq'], review['eventSeq'])
        self.assertTrue(review['contextRef'])
        self.assertEqual(run['qualityHistory'][0], old['qualityHistory'][0])
        self.assertEqual(run['nodes'][:len(old['nodes'])], old['nodes'])
        self.assertEqual(run['qualityHistory'][-1]['reviewId'], review['id'])
        self.assertEqual(review['sourceFinalNodeId'], old['finalNodeId'])
        for ref, data in old_bytes.items():
            self.assertEqual((root / ref).read_bytes(), data, ref)
        shared = calls[0]['memory']['sharedEvidence']
        tools = shared['modules']['verification']['nativeEvidence']['tools']
        self.assertIn('expected-recovery-probe', tools[1]['inputExcerpt'])
        self.assertEqual(tools[1]['status'], 'failed')
        self.assertIn('recovered:true', tools[2]['outputExcerpt'])
        self.assertIn('official docs', tools[3]['action']['argumentsExcerpt'])
        self.assertTrue(tools[3]['resultTruncated'])
        self.assertIn('End source.', tools[3]['resultExcerpt'])
        supplement = json.loads((root / review['supplementRef']).read_text())
        source = supplement['modules'][0]
        self.assertEqual(source['eventsSha256'], hashlib.sha256((root / source['eventsRef']).read_bytes()).hexdigest())
        self.assertEqual(source['missing'], [])
        self.assertEqual(source['tools'][1]['eventSeq'], 4)
        self.assertEqual(source['artifacts'], old['nodes'][1]['result']['nativeEvidence']['verifiedArtifacts'])
        state, workspace = self.store.current(self.pid)
        self.assertTrue((workspace / 'verification.md').is_file())

    def test_missing_raw_result_blocks_even_if_coordinator_accepts(self):
        session = self.failed_workflow()
        root = self.studio.project_dir(self.pid) / session['id']
        path = next(root.rglob('events.jsonl'))
        events = [json.loads(line) for line in path.read_text().splitlines()]
        next(e for e in events if e['seq'] == 6)['item']['result'] = None
        path.write_text('\n'.join(json.dumps(e) for e in events))
        result, calls = self.review(session)
        run = result['runs'][-1]
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(run['evidenceReviews'][-1]['missingCount'], 1)
        self.assertFalse(run['final']['accepted'])
        self.assertNotIn('commitIntent', run)
        self.assertEqual(len(run['qualityHistory']), 1)
        self.assertIn('本地检查失败', result['error'])
        self.client.reject = True
        second, second_calls = self.review(result)
        reviews = second['runs'][-1]['evidenceReviews']
        self.assertNotEqual(reviews[0]['nodeId'], reviews[1]['nodeId'])
        self.assertEqual(len(second_calls), 1)
        self.assertEqual(len(second['runs'][-1]['qualityHistory']), 2)
        self.assertFalse(second['runs'][-1]['evidenceReviews'][-1]['qualityPassed'])

    def test_failed_required_command_still_blocks(self):
        self.command_exit = 1
        session = self.failed_workflow()
        result, calls = self.review(session)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('commitIntent', result['runs'][-1])
        checks = calls[0]['memory']['sharedEvidence']['checks']
        required = next(c for c in checks if c.get('basis') == 'native_required_checks')
        self.assertFalse(required['pass'])
        self.assertEqual(required['bindingEvidence']['checks'][0]['exitCode'], 1)

    def test_native_search_plural_results_are_recovered(self):
        self.native_search = True
        session = self.failed_workflow()
        result, calls = self.review(session)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['runs'][-1]['evidenceReviews'][-1]['missingCount'], 0)
        search = calls[0]['memory']['sharedEvidence']['modules']['verification']['nativeEvidence']['tools'][-1]
        self.assertIn('https://example.test/docs', search['resultExcerpt'])
        self.assertIn('Document body', search['resultExcerpt'])

    def test_child_snapshot_binds_parent_turn_and_preserves_exact_source(self):
        self.with_child = True
        session = self.failed_workflow()
        result, calls = self.review(session)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        tool = calls[0]['memory']['sharedEvidence']['modules']['verification']['nativeEvidence']['tools'][-1]
        self.assertIn('Child document body', tool['resultExcerpt'])
        self.assertTrue(tool['resultRef'].endswith('children/child-1.json#turn=child-turn&item=child-search'))
        self.assertEqual(result['runs'][-1]['evidenceReviews'][-1]['missingCount'], 0)

    def test_changed_child_snapshot_cannot_supply_positive_evidence(self):
        self.with_child = True
        session = self.failed_workflow()
        root = self.studio.project_dir(self.pid) / session['id']
        path = next(root.rglob('child-1.json'))
        path.write_text(path.read_text().replace('Child document body', 'Forged body'))
        result, calls = self.review(session)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('commitIntent', result['runs'][-1])
        tool = calls[0]['memory']['sharedEvidence']['modules']['verification']['nativeEvidence']['tools'][-1]
        self.assertNotIn('resultExcerpt', tool)
        self.assertGreater(result['runs'][-1]['evidenceReviews'][-1]['missingCount'], 0)

    def test_changed_candidate_fails_before_model(self):
        session = self.failed_workflow()
        run = session['runs'][-1]
        root = self.studio.project_dir(self.pid) / session['id']
        (root / 'artifacts' / run['id'] / 'verification.md').write_text('changed')
        result, calls = self.review(session)
        self.assertEqual(calls, [])
        self.assertIn('候选产物缺失或已变化', result['error'])
        self.assertEqual(len(result['runs'][-1]['qualityHistory']), 1)

    def test_version_and_running_checks_prevent_extra_calls(self):
        session = self.failed_workflow()
        with self.assertRaises(Conflict):
            start_evidence_review(self.studio, self.pid, session['id'], session['version'] - 1)
        session['status'] = 'running'
        self.studio.save(session)
        with self.assertRaisesRegex(ValueError, '正在执行'):
            start_evidence_review(self.studio, self.pid, session['id'], session['version'])


if __name__ == '__main__':
    unittest.main()
