import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import Store
from business_routing import rank_task_models
from live import Cancelled
from studio import Studio
from studio_mcp import BRIDGE, MCPProcess, MCPProtocolError, MCPRegistry, _tool_action, runtime, verification_checks
from test_studio_business import BusinessClient, module


def server():
    return {'id': '123456abcdef', 'name': 'Real fixture', 'command': runtime()['nodeBin'],
            'args': [str(BRIDGE / 'test/fixture-server.mjs')], 'browser': False}


def configure_policy(studio, pid, policy):
    project = studio.project(pid)
    config = copy.deepcopy(project['teamConfig'])
    config['mcpPolicy'] = policy
    return studio.update_project(pid, project['instructions'], project['version'], config)


def run_workflow(studio, pid, session, resume=False, worker='sol'):
    studio.start(pid, session['id'], resume=resume, coordinator_model='opus', team_models=['opus', worker])
    studio.threads[session['id']].join(15)
    if studio.threads[session['id']].is_alive():
        raise AssertionError('MCP workflow did not finish within 15 seconds')
    return studio.load(pid, session['id'])


class RoleToolClient(BusinessClient):
    def __init__(self, forbidden=False):
        discover = module('discover'); discover['roleId'] = 'research'
        publish = module('publish', deps=['discover']); publish['roleId'] = 'writing'
        self.actions = {'discover': {'name': 'roots', 'arguments': {}},
                        'publish': {'name': 'write', 'arguments': {'value': 'delivered-by-role'}}}
        for task in (discover, publish):
            action = self.actions[task['id']]
            task['requiredCapabilities'] = ['text', 'plugins']
            task['requiredToolChecks'] = [{'id': task['id'] + '-actual', 'criterionIds': [task['id'] + '-c1'],
                'serverId': server()['id'], 'tool': action['name'], 'arguments': action['arguments'],
                'resultContains': ['saved output.txt'] if task['id'] == 'publish' else []}]
        super().__init__([discover, publish])
        self.catalogs = {}; self.forbidden = forbidden

    def call(self, model, messages, cancel, on_delta=None):
        task = next(m for m in reversed(messages) if m['role'] == 'user' and '\n本轮角色任务：\n' in m['content'])
        shared = json.loads(task['content'].split('：\n', 1)[1].split('\n本轮角色任务：\n', 1)[0])['sharedEvidence']
        result, receipt = super().call(model, [task], cancel, on_delta)
        if shared['businessStage'] != 'build':
            return result, receipt
        mid = shared['module']['id']
        catalog = next(m['content'].rsplit('工具目录：', 1)[1] for m in reversed(messages)
                       if m['role'] == 'user' and '工具目录：' in m['content'])
        self.catalogs[mid] = {'model': model, 'servers': json.loads(catalog)}
        actual = [json.loads(m['content'].split('：', 1)[1]) for m in messages
                  if m['role'] == 'user' and m['content'].startswith('实际 MCP 工具回执')]
        if not actual:
            action = self.actions['publish'] if self.forbidden else self.actions[mid]
            return {'tool': {'serverId': server()['id'], **action}}, receipt
        result['files'][0]['content'] = json.dumps(actual[-1]['result'], ensure_ascii=False)
        return result, receipt


class ToolClient(BusinessClient):
    def __init__(self, fail_after_tool=False, tool_name='state', invalid_final=False):
        task = module('proof'); task['requiredCapabilities'] = ['text', 'plugins']
        task['requiredToolChecks'] = [{'id': 'actual-state', 'criterionIds': ['proof-c1'], 'serverId': '123456abcdef',
                                      'tool': tool_name, 'arguments': {'value': 'received-from-real-server'}, 'resultContains': ['received-from-real-server']}]
        super().__init__([task]); self.received = []; self.fail_after_tool = fail_after_tool; self.tool_name = tool_name
        self.invalid_final = invalid_final

    def call(self, model, messages, cancel, on_delta=None):
        task = next(m for m in reversed(messages) if m['role'] == 'user' and '\n本轮角色任务：\n' in m['content'])
        result, receipt = super().call(model, [task], cancel, on_delta)
        if self.calls[-1]['key'][0] == 'build':
            actual = [json.loads(m['content'].split('：', 1)[1]) for m in messages
                      if m['role'] == 'user' and m['content'].startswith('实际 MCP 工具回执')]
            if not actual:
                return {'tool': {'serverId': '123456abcdef', 'name': self.tool_name, 'arguments': {'value': 'received-from-real-server'}}}, receipt
            self.received = actual
            if self.fail_after_tool:
                self.fail_after_tool = False
                raise ValueError('Interrupted after successful tool receipt')
            result['files'][0]['content'] = actual[-1]['result']['structuredContent']['value']
            if self.invalid_final:
                result['files'][0]['path'] = 'outside.md'; self.invalid_final = False
        return result, receipt


class MCPConfigTests(unittest.TestCase):
    def test_config_boundaries_and_api_only_without_node(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = MCPRegistry(folder)
            with self.assertRaises(ValueError): registry.save({'name': 'a', 'command': 'node', 'args': 'shell string'})
            with self.assertRaises(ValueError): registry.save({'name': 'a', 'command': 'node', 'args': ['sk-abcdefghijklmnopqrstuvwxyz']})
            with patch.dict(os.environ, {'YUKI_NODE': '/missing/yuki-node'}):
                studio = Studio(Store(Path(folder), seed=False), BusinessClient())
                self.assertFalse(studio.index()['mcp']['detected'])
                self.assertTrue(studio.index()['available'])

    def test_routing_only_grants_configured_tools_to_text_models(self):
        task = module('web'); task['requiredCapabilities'] = ['text', 'browser']
        with self.assertRaises(ValueError): rank_task_models(task, ['opus', 'sol', 'native_codex'])
        rows = rank_task_models(task, ['opus', 'sol', 'native_codex', 'image'], mcp_capabilities=['browser'])
        self.assertEqual(rows[0]['model'], 'sol')
        self.assertNotIn('native_codex', [row['model'] for row in rows])
        self.assertFalse(all(c['pass'] for c in verification_checks(task, {'summary': 'I used a browser'})))


@unittest.skipUnless(runtime()['detected'], 'optional MCP dependencies not installed')
class MCPIntegrationTests(unittest.TestCase):
    def test_real_discovery_persistent_calls_errors_and_unknown_tool(self):
        with tempfile.TemporaryDirectory() as folder, MCPProcess(server(), Path(folder), threading.Event()) as client:
            self.assertIn('roots', {t['name'] for t in client.tools})
            client.request('call', name='state', arguments={'value': 'persisted'})
            self.assertEqual(client.request('call', name='state', arguments={})['structuredContent']['value'], 'persisted')
            self.assertTrue(client.request('call', name='fail', arguments={})['isError'])
            with self.assertRaises(MCPProtocolError) as error: client.request('call', name='missing', arguments={})
            self.assertEqual(error.exception.error['code'], -32602)
            with self.assertRaisesRegex(ValueError, '不存在'):
                _tool_action({'serverId': server()['id'], 'name': 'missing', 'arguments': {}}, {server()['id']: client})
            client.request('call', name='write', arguments={'value': 'actual-file'})
            self.assertEqual((Path(folder) / 'output.txt').read_text(), 'actual-file')

    def test_required_failure_cannot_hide_behind_success_and_recovery_is_explicit(self):
        task = module('proof'); task['requiredCapabilities'] = ['plugins']
        task['requiredToolChecks'] = [{'id': 'required', 'criterionIds': ['proof-c1'], 'serverId': server()['id'],
                                      'tool': 'state', 'arguments': {}, 'resultContains': ['verified']}]
        evidence = {'status': 'completed', 'receiptRef': 'actual-receipt.json', 'tools': []}
        with tempfile.TemporaryDirectory() as folder, MCPProcess(server(), Path(folder), threading.Event()) as client:
            for value, expected in [('verified', True), (3, False), ('wrong-result', False), ('verified', True)]:
                args = {'value': value}; result = client.request('call', name='state', arguments=args)
                evidence['tools'].append({'id': str(len(evidence['tools'])), 'serverId': server()['id'], 'name': 'state',
                                          'arguments': args, 'result': result, 'status': 'tool_error' if result.get('isError') else 'completed'})
                self.assertEqual(all(c['pass'] for c in verification_checks(task, {'mcpEvidence': evidence})), expected)

    def test_cancel_actual_child_session(self):
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            started = time.monotonic()
            with self.assertRaises(Cancelled):
                with MCPProcess(server(), Path(folder), cancel) as client:
                    timer = threading.Timer(.15, cancel.set); timer.start()
                    client.request('call', name='slow', arguments={})
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNotNone(client.process.poll())

    def test_real_results_return_to_role_and_survive_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ToolClient(fail_after_tool=True)
            studio = Studio(Store(Path(folder), seed=False), client)
            studio.mcp.save(server()); studio.mcp.probe(server()['id'])
            self.assertEqual(studio.mcp.snapshot()[0]['status'], 'ready')
            project = studio.create_project('MCP', 'Actual server evidence')['id']
            configure_policy(studio, project, {'mode': 'workspace', 'serverIds': [], 'roleTools': {}})
            session = studio.create_session(project, 'MCP', 'Read tool output', 'general')
            studio.start(project, session['id'], coordinator_model='opus', team_models=['opus', 'sol'])
            studio.threads[session['id']].join(10)
            failed = studio.load(project, session['id'])
            self.assertEqual(failed['status'], 'failed', failed.get('error'))
            node = next(n for n in failed['runs'][-1]['nodes'] if n['phase'] == 'build')
            receipt = studio.project_dir(project) / session['id'] / node['mcpEvidence']['receiptRef']
            previous = json.loads(receipt.read_text())['tools']
            studio.start(project, session['id'], resume=True)
            studio.threads[session['id']].join(10)
            final = studio.load(project, session['id'])
            self.assertEqual(final['status'], 'completed', final.get('error'))
            self.assertEqual(json.loads(receipt.read_text())['tools'], previous)
            node = next(n for n in final['runs'][-1]['nodes'] if n['phase'] == 'build')
            self.assertEqual(node['result']['files'][0]['content'], 'received-from-real-server')
            self.assertTrue(client.received)
            self.assertEqual(node['result']['mcpEvidence']['tools'][0]['status'], 'completed')
            restored = MCPRegistry(Path(folder) / 'new-machine');
            with self.assertRaisesRegex(ValueError, '恢复'): restored.authorize(studio.mcp.snapshot())

    def test_disconnected_action_stays_unknown_and_is_not_reissued(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ToolClient(tool_name='crash')
            studio = Studio(Store(Path(folder), seed=False), client)
            studio.mcp.save(server()); studio.mcp.probe(server()['id'])
            project = studio.create_project('MCP disconnect', 'Unknown effect')['id']
            configure_policy(studio, project, {'mode': 'workspace', 'serverIds': [], 'roleTools': {}})
            session = studio.create_session(project, 'disconnect', 'Run tool', 'general')
            studio.start(project, session['id'], coordinator_model='opus', team_models=['opus', 'sol'])
            studio.threads[session['id']].join(10)
            failed = studio.load(project, session['id'])
            self.assertEqual(failed['status'], 'failed')
            node = next(n for n in failed['runs'][-1]['nodes'] if n['phase'] == 'build')
            path = studio.project_dir(project) / session['id'] / node['mcpEvidence']['receiptRef']
            original = path.read_bytes(); journal = json.loads(original)
            self.assertEqual(journal['pending']['status'], 'unknown')
            count = len(client.calls)
            studio.start(project, session['id'], resume=True); studio.threads[session['id']].join(10)
            self.assertEqual(len(client.calls), count)
            self.assertEqual(path.read_bytes(), original)

    def test_resume_corrects_final_content_without_repeating_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            client = ToolClient(invalid_final=True)
            studio = Studio(Store(Path(folder), seed=False), client)
            studio.mcp.save(server()); studio.mcp.probe(server()['id'])
            project = studio.create_project('MCP correction', 'Preserve tools')['id']
            configure_policy(studio, project, {'mode': 'workspace', 'serverIds': [], 'roleTools': {}})
            session = studio.create_session(project, 'correction', 'Run tool', 'general')
            studio.start(project, session['id'], coordinator_model='opus', team_models=['opus', 'sol'])
            studio.threads[session['id']].join(10)
            failed = studio.load(project, session['id'])
            self.assertEqual(failed['status'], 'failed')
            node = next(n for n in failed['runs'][-1]['nodes'] if n['phase'] == 'build')
            path = studio.project_dir(project) / session['id'] / node['mcpEvidence']['receiptRef']
            tools = json.loads(path.read_text())['tools']; old_context = node['contextRef']
            studio.mcp.remove(server()['id'])
            studio.start(project, session['id'], resume=True); studio.threads[session['id']].join(10)
            final = studio.load(project, session['id'])
            self.assertEqual(final['status'], 'completed', final.get('error'))
            node = next(n for n in final['runs'][-1]['nodes'] if n['phase'] == 'build')
            self.assertEqual(json.loads(path.read_text())['tools'], tools)
            self.assertNotEqual(node['contextRef'], old_context)
            context = studio.context(project, session['id'], node['contextRef'])
            self.assertTrue(any('实际 MCP 工具回执' in m['content'] for m in context['messages']))


@unittest.skipUnless(runtime()['detected'], 'optional MCP dependencies not installed')
class MCPRoleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = RoleToolClient()
        self.studio = Studio(Store(Path(self.tmp.name), seed=False), self.client)
        self.studio.mcp.save(server()); self.studio.mcp.probe(server()['id'])
        self.pid = self.studio.create_project('Role tools', 'Verify actual role-scoped tools')['id']
        self.policy = {'mode': 'roles', 'serverIds': [server()['id']], 'roleTools': {
            'research': [{'serverId': server()['id'], 'tools': ['roots']}],
            'writing': [{'serverId': server()['id'], 'tools': ['write']}]}}

    def tearDown(self):
        for thread in self.studio.threads.values():
            thread.join(15)
        self.tmp.cleanup()

    def test_same_model_different_roles_and_model_swap_preserve_tool_binding(self):
        configure_policy(self.studio, self.pid, self.policy)
        other = {**server(), 'id': 'abcdef123456', 'name': 'Excluded fixture'}
        self.studio.mcp.save(other); self.studio.mcp.probe(other['id'])
        bindings = []
        for worker in ('sol', 'deepseek'):
            with self.subTest(worker=worker):
                session = self.studio.create_session(self.pid, worker, 'Discover then publish actual evidence', 'general')
                final = run_workflow(self.studio, self.pid, session, worker=worker)
                self.assertEqual(final['status'], 'completed', final.get('error'))
                run = final['runs'][-1]
                self.assertEqual(run['mcpPolicy'], self.policy)
                self.assertEqual([s['id'] for s in run['mcpServers']], [server()['id']])
                for mid, tool, role in (('discover', 'roots', 'research'), ('publish', 'write', 'writing')):
                    catalog = self.client.catalogs[mid]
                    self.assertEqual(catalog['model'], worker)
                    self.assertEqual([s['serverId'] for s in catalog['servers']], [server()['id']])
                    self.assertEqual([t['name'] for t in catalog['servers'][0]['tools']], [tool])
                    node = next(n for n in run['nodes'] if n['phase'] == 'build' and n['moduleId'] == mid)
                    self.assertEqual([t['name'] for t in node['result']['mcpEvidence']['tools']], [tool])
                    contract = run['taskContracts'][mid]
                    self.assertEqual(contract['mcpBinding']['sourceRoleId'], role)
                    self.assertEqual(contract['mcpBinding']['servers'], [{'serverId': server()['id'], 'tools': [tool]}])
                bindings.append({mid: c['mcpBinding'] for mid, c in run['taskContracts'].items()})
                node = next(n for n in run['nodes'] if n['phase'] == 'build' and n['moduleId'] == 'publish')
                receipt = self.studio.project_dir(self.pid) / session['id'] / node['mcpEvidence']['receiptRef']
                self.assertEqual((receipt.parent / 'workspace/output.txt').read_text(), 'delivered-by-role')
        self.assertEqual(bindings[0], bindings[1])

    def test_dispatch_rejects_tool_that_role_cannot_use_before_side_effect(self):
        configure_policy(self.studio, self.pid, self.policy)
        self.client.forbidden = True
        session = self.studio.create_session(self.pid, 'Blocked write', 'Discover actual roots', 'general')
        failed = run_workflow(self.studio, self.pid, session)
        self.assertEqual(failed['status'], 'failed')
        node = next(n for n in failed['runs'][-1]['nodes'] if n['phase'] == 'build')
        self.assertEqual(node['mcpEvidence']['tools'], [])
        self.assertTrue(node.get('error'))
        self.assertFalse(list((self.studio.project_dir(self.pid) / session['id']).rglob('output.txt')))
        self.assertEqual([t['name'] for t in self.client.catalogs['discover']['servers'][0]['tools']], ['roots'])

    def test_ungranted_required_tool_is_rejected_during_planning(self):
        configure_policy(self.studio, self.pid, self.policy)
        self.client.modules[0]['requiredToolChecks'][0]['tool'] = 'write'
        session = self.studio.create_session(self.pid, 'Invalid plan', 'Discover actual roots', 'general')
        failed = run_workflow(self.studio, self.pid, session)
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(any(n['phase'] == 'build' for n in failed['runs'][-1]['nodes']))
        self.assertFalse(self.client.catalogs)

    def test_project_save_rejects_unknown_server_and_unknown_tool(self):
        for invalid in (
            {'mode': 'roles', 'serverIds': ['abcdef123456'], 'roleTools': {}},
            {'mode': 'roles', 'serverIds': [server()['id']], 'roleTools': {
                'research': [{'serverId': server()['id'], 'tools': ['missing-tool']}]}}
        ):
            with self.subTest(policy=invalid):
                before = self.studio.project(self.pid)
                with self.assertRaises(ValueError):
                    configure_policy(self.studio, self.pid, invalid)
                after = self.studio.project(self.pid)
                self.assertEqual(after['teamConfig'], before['teamConfig'])
                self.assertEqual(after['version'], before['version'])

    def test_legacy_project_without_policy_keeps_workspace_compatibility(self):
        self.studio.client = ToolClient()
        metadata = self.studio.meta(self.pid)
        metadata['teamConfig'].pop('mcpPolicy')
        self.studio._write(self.studio.project_dir(self.pid) / 'project.json', metadata)
        session = self.studio.create_session(self.pid, 'Legacy project', 'Read actual server state', 'general')
        final = run_workflow(self.studio, self.pid, session)
        self.assertEqual(final['status'], 'completed', final.get('error'))
        run = final['runs'][-1]
        self.assertIsNone(run.get('mcpPolicy'))
        self.assertNotIn('mcpBinding', run['taskContracts']['proof'])
        self.assertNotIn('mcpPolicy', self.studio.project(self.pid)['teamConfig'])

    def test_resume_keeps_frozen_policy_and_receipts_after_project_edit(self):
        client = ToolClient(fail_after_tool=True)
        self.studio.client = client
        original = {'mode': 'roles', 'serverIds': [server()['id']], 'roleTools': {
            'proof-owner': [{'serverId': server()['id'], 'tools': ['state']}]}}
        configure_policy(self.studio, self.pid, original)
        session = self.studio.create_session(self.pid, 'Frozen scope', 'Read actual server state', 'general')
        failed = run_workflow(self.studio, self.pid, session)
        self.assertEqual(failed['status'], 'failed', failed.get('error'))
        before = copy.deepcopy(failed['runs'][-1])
        node = next(n for n in before['nodes'] if n['phase'] == 'build')
        receipt = self.studio.project_dir(self.pid) / session['id'] / node['mcpEvidence']['receiptRef']
        tools = json.loads(receipt.read_text())['tools']
        configure_policy(self.studio, self.pid, {'mode': 'roles', 'serverIds': [], 'roleTools': {}})
        final = run_workflow(self.studio, self.pid, session, resume=True)
        self.assertEqual(final['status'], 'conflict', final.get('error'))
        after = final['runs'][-1]
        self.assertEqual(next(n for n in after['nodes'] if n['phase'] == 'build')['status'], 'done')
        self.assertEqual(after['mcpPolicy'], original)
        self.assertEqual(after['taskContracts'], before['taskContracts'])
        self.assertEqual(json.loads(receipt.read_text())['tools'], tools)
        self.assertEqual(after['taskContracts']['proof']['mcpBinding']['servers'], [
            {'serverId': server()['id'], 'tools': ['state']}])


if __name__ == '__main__':
    unittest.main()
