"""Fixed native acceptance commands bind to real receipt fields, not check names."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Store
from native_checks import bind_required_checks, validate_required_checks
from quality_telemetry import project_metrics
from studio import Studio
from studio_business import native_verification_checks, validate_business_plan, validate_business_response
from test_studio_business import BusinessClient, module, native_result, required_checks


class RequiredNativeCheckTests(unittest.TestCase):
    def setUp(self):
        self.module = dict(module('verification', 'general'), nativeCheckVersion=1,
                           requiredCapabilities=['files', 'terminal'], requiredChecks=required_checks('verification'))
        self.contract = {'id': 'fixed-verification', 'fingerprint': 'f' * 64,
                         'requiredChecks': copy.deepcopy(self.module['requiredChecks'])}

    def binding(self, output):
        return bind_required_checks(self.contract, output['nativeEvidence'], Path('/fixture-workspace'))

    def test_successful_acceptance_ignores_but_retains_exploration_failure(self):
        output = native_result()
        output['nativeEvidence']['tools'].insert(0, {'id': 'search', 'type': 'commandExecution',
            'status': 'failed', 'exitCode': 2, 'command': 'rg missing inputs/', 'cwd': '/fixture-workspace'})
        output['nativeCheckEvidence'] = self.binding(output)
        binding = output['nativeCheckEvidence']
        self.assertEqual(binding['checks'][0]['toolId'], 'command-1')
        self.assertEqual(binding['checks'][0]['status'], 'passed')
        self.assertEqual(binding['operationIssues'][0]['toolId'], 'search')
        self.assertTrue(all(row['pass'] for row in native_verification_checks(self.module, output)))
        legacy = {key: value for key, value in self.module.items() if key != 'nativeCheckVersion'}
        self.assertFalse(all(row['pass'] for row in native_verification_checks(legacy, output)))
        for status in ('failed', 'not_run'):
            with self.subTest(required_check_self_report=status):
                output['nativeEvidence']['checks'][0]['status'] = status
                self.assertFalse(all(row['pass'] for row in native_verification_checks(self.module, output)))

    def test_same_name_unrelated_success_does_not_hide_missing_or_failed_acceptance(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                output = native_result()
                if missing:
                    output['nativeEvidence']['tools'] = []
                else:
                    output['nativeEvidence']['tools'][0].update(status='failed', exitCode=2)
                output['nativeEvidence']['tools'].append({'id': 'unrelated', 'type': 'commandExecution',
                    'name': 'verification-run', 'status': 'completed', 'exitCode': 0,
                    'command': 'python3 -B other.py', 'cwd': '/fixture-workspace'})
                output['nativeCheckEvidence'] = self.binding(output)
                self.assertEqual(output['nativeCheckEvidence']['checks'][0]['status'], 'not_run' if missing else 'failed')
                self.assertFalse(all(row['pass'] for row in native_verification_checks(self.module, output)))

    def test_search_without_terminal_keeps_exploration_failures_separate(self):
        item = dict(module('research', 'research'), nativeCheckVersion=1,
                    requiredCapabilities=['text', 'web_search'], requiredChecks=[])
        output = native_result('research')
        output['nativeEvidence']['tools'] = [
            {'id': 'exploration', 'type': 'commandExecution', 'status': 'failed', 'exitCode': 2,
             'command': 'rg missing inputs/', 'cwd': '/fixture-workspace'},
            {'id': 'search', 'type': 'webSearch', 'status': 'completed'}]
        output['nativeCheckEvidence'] = bind_required_checks(
            {**self.contract, 'requiredChecks': []}, output['nativeEvidence'], Path('/fixture-workspace'))
        output['nativeSearchEvidence'] = {'schema': 'yuki-native-search/v1',
                                         'searches': [{'status': 'completed', 'eventRef': 'events.jsonl#2'}]}
        self.assertTrue(all(row['pass'] for row in native_verification_checks(item, output)))
        self.assertEqual(output['nativeCheckEvidence']['operationIssues'][0]['toolId'], 'exploration')
        for searches in ([], [{'status': 'failed', 'eventRef': 'events.jsonl#2'}]):
            with self.subTest(searches=searches):
                output['nativeSearchEvidence']['searches'] = searches
                self.assertFalse(all(row['pass'] for row in native_verification_checks(item, output)))

    def test_nonterminal_policy_requires_complete_tool_receipts(self):
        item = dict(module('verification'), nativeCheckVersion=1,
                    requiredCapabilities=['files'], requiredChecks=[])
        output = native_result()
        output['nativeEvidence']['tools'][0].pop('exitCode')
        output['nativeCheckEvidence'] = bind_required_checks(
            {**self.contract, 'requiredChecks': []}, output['nativeEvidence'], Path('/fixture-workspace'))
        self.assertFalse(all(row['pass'] for row in native_verification_checks(item, output)))

    def test_required_commands_cannot_be_hidden_in_nonterminal_module(self):
        item = dict(module('verification'), requiredCapabilities=['files'],
                    requiredChecks=required_checks('verification'))
        plan = {'summary': '验证', 'businessUnderstanding': '执行真实检查', 'modules': [item], 'integration': '汇总'}
        with self.assertRaisesRegex(ValueError, 'terminal'):
            validate_business_plan(plan, ['opus', 'native_codex'], 1)
        item['requiredCapabilities'] = ['terminal']
        item['requiredChecks'] = []
        with self.assertRaises(ValueError):
            validate_business_plan(plan, ['opus', 'native_codex'], 1)

    def test_receipt_command_and_cwd_are_both_required(self):
        cases = [('python3 -B verification/check.py', '/fixture-workspace', 'passed'),
                 ("/bin/zsh -lc 'python3 -B verification/check.py'", '/fixture-workspace', 'passed'),
                 ('python3 -B verification/check.py', '/fixture-workspace/elsewhere', 'not_run'),
                 ('python3 -B verification/check.py', None, 'not_run'),
                 ('python3 -B verification/check.py || true', '/fixture-workspace', 'not_run')]
        for command, cwd, expected in cases:
            with self.subTest(command=command, cwd=cwd):
                output = native_result()
                output['nativeEvidence']['tools'][0].update(command=command, cwd=cwd)
                self.assertEqual(self.binding(output)['checks'][0]['status'], expected)

    def test_unknown_command_status_cannot_be_treated_as_completed_exploration(self):
        for status in ('inProgress', 'failed', 'completed'):
            with self.subTest(status=status):
                output = native_result()
                output['nativeEvidence']['tools'].append({'id': 'unfinished', 'type': 'commandExecution',
                    'status': status, 'command': 'rg check inputs/', 'cwd': '/fixture-workspace'})
                output['nativeCheckEvidence'] = self.binding(output)
                self.assertFalse(output['nativeCheckEvidence']['allOperationsComplete'])
                self.assertFalse(all(row['pass'] for row in native_verification_checks(self.module, output)))

    def test_finished_tool_error_is_retained_without_becoming_an_unfinished_operation(self):
        output = native_result()
        output['nativeEvidence']['tools'].extend([
            {'id': 'js-check-failed', 'type': 'mcpToolCall', 'server': 'node_repl',
             'tool': 'js', 'status': 'failed', 'completedRef': 'events.jsonl#4'},
            {'id': 'js-check-retry', 'type': 'mcpToolCall', 'server': 'node_repl',
             'tool': 'js', 'status': 'completed', 'completedRef': 'events.jsonl#6'}])
        output['nativeCheckEvidence'] = self.binding(output)
        self.assertTrue(output['nativeCheckEvidence']['allOperationsComplete'])
        self.assertEqual(output['nativeCheckEvidence']['operationIssues'][0]['toolId'], 'js-check-failed')
        self.assertTrue(all(row['pass'] for row in native_verification_checks(self.module, output)))
        output['nativeEvidence']['checks'][0]['status'] = 'failed'
        self.assertFalse(all(row['pass'] for row in native_verification_checks(self.module, output)))
        output['nativeEvidence']['tools'][-1]['status'] = 'inProgress'
        self.assertFalse(self.binding(output)['allOperationsComplete'])

    def test_invalid_planned_specs_fail_at_entry(self):
        validate_required_checks(self.module)
        for key, value in [('command', 'python3 check.py'), ('command', [1]), ('cwd', '../outside'),
                           ('expectedExitCode', True), ('expectedExitCode', 1), ('criterionIds', ['other-c1'])]:
            with self.subTest(key=key, value=value):
                invalid = copy.deepcopy(self.module)
                invalid['requiredChecks'][0][key] = value
                with self.assertRaises(ValueError):
                    validate_required_checks(invalid)

    def test_decision_cannot_reorder_bound_criterion_ids(self):
        self.module['acceptance'] = ['原标准A', '原标准B']
        shared = {'businessStage': 'decision', 'collaborationVersion': 1, 'currentModule': self.module, 'evidence': []}
        result = {'summary': '完善说明', 'decisions': [], 'moduleUpdate': {'instructions': '完整说明', 'acceptance': ['原标准B', '原标准A']}}
        with self.assertRaisesRegex(ValueError, '不能重排'):
            validate_business_response({}, result, shared, [])
        result['moduleUpdate']['acceptance'] = ['原标准A', '原标准B', '追加标准C']
        validate_business_response({}, result, shared, [])

    def test_contract_and_receipts_survive_resume_and_versioned_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BusinessClient([copy.deepcopy(self.module)])
            studio = Studio(Store(Path(directory), seed=False), client)
            pid = studio.create_project('固定命令测试', '保留验收标准', '')['id']
            session = studio.create_session(pid, '命令验收', '执行验收', 'general')
            client.fail_once = ('integration', None, 'opus', 0)
            def fixture(studio, session, run, node, context, messages, shared, cancel, delta):
                return native_result(contract=shared['executionContract']), {'mode': 'fixture-native'}
            with patch('studio_native.execute_native', side_effect=fixture) as provider:
                studio.start(pid, session['id'], 'live')
                studio.threads[session['id']].join(15)
                first = studio.load(pid, session['id'])
                self.assertEqual(first['status'], 'failed')
                contract = first['runs'][-1]['taskContracts']['verification']
                self.assertEqual(contract['requiredChecks'], self.module['requiredChecks'])
                node = next(n for n in first['runs'][-1]['nodes'] if n['phase'] == 'build')
                response = studio.project_dir(pid) / session['id'] / 'responses' / (node['responseRef'] + '.json')
                original = response.read_bytes()
                studio.start(pid, session['id'], 'live', resume=True)
                studio.threads[session['id']].join(15)
                final = studio.load(pid, session['id'])
            self.assertEqual(final['status'], 'completed', final.get('error'))
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(response.read_bytes(), original)
            self.assertEqual(final['runs'][-1]['taskContracts']['verification'], contract)
            old = copy.deepcopy(final)
            old['id'] = 'eeeeeeeeeeee'
            old['version'] = 0
            old['runs'][-1].pop('executionAttempts', None)
            old['runs'][-1].pop('executionAttemptsHistory', None)
            old['runs'][-1].pop('nativeCheckVersion')
            studio.save(old)
            metrics = project_metrics(studio, pid)
            self.assertEqual(len(metrics['rows']), 2)
            self.assertEqual({row['policyVersion'] for row in metrics['rows']}, {0, 1})
            self.assertEqual(sum(row['qualitySamples'] for row in metrics['rows']), 2)


if __name__ == '__main__':
    unittest.main()
