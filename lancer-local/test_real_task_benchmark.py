"""Deterministic observation-parser fixtures; none are real benchmark results."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import real_task_benchmark as bench


class RealTaskBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inputs = self.root / 'inputs'; self.inputs.mkdir()
        (self.inputs / 'request.txt').write_text('User-supplied task input for parser testing')
        self.criteria = self.root / 'criteria.md'; self.criteria.write_text('All requested rows reconcile.')
        self.script = self.root / 'accept.py'; self.script.write_text('raise RuntimeError("must never execute")')
        self.config = {'worker': {'model': 'provider/model-a', 'provider': 'provider', 'protocol': 'openai'}}
        self.manifest = bench.prepare_manifest('task-1', 'Reconcile actual inputs', self.inputs,
            self.criteria, {'baseline': self.config, 'current': self.config}, 'revision-old', 'revision-new',
            repetitions=2, seed=5, script=self.script)
        self.now = datetime.now(timezone.utc) + timedelta(minutes=1)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, value):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf8')
        return path

    def make_observation(self, pair=1, arm='baseline', status='completed', accepted=True, length=10, offset=0):
        run_id = 'run-%d-%s' % (pair, arm)
        start = self.now + timedelta(seconds=offset)
        end = start + timedelta(seconds=length)
        run = {'id': run_id, 'mode': 'live', 'status': status, 'started': start.isoformat(), 'ended': end.isoformat(),
               'runtimeBuild': {'fingerprint': self.manifest['arms'][arm]['revision']},
               'callMetrics': [{'id': 'call', 'nodeId': 'build', 'contextRef': 'context', 'model': 'worker',
                                'elapsedMs': 1200, 'outcome': 'returned'}],
               'nodes': [{'id': 'build', 'phase': 'build', 'contextRef': 'context',
                          'receipt': {'returnedModel': 'provider/model-a',
                                      'usage': {'input_tokens': 100, 'output_tokens': 20}}}]}
        session = self.write(run_id+'.json', {'id': run_id+'-session', 'projectId': 'project', 'runs': [run]})
        binding = {'runId': run_id, 'taskFingerprint': self.manifest['fingerprint'],
                   'inputFingerprint': self.manifest['inputs']['fingerprint'],
                   'acceptanceFingerprint': self.manifest['acceptance']['fingerprint'],
                   'modelConfigFingerprint': self.manifest['arms'][arm]['modelConfigFingerprint'],
                   'revision': self.manifest['arms'][arm]['revision']}
        assessment = None
        if accepted is not None:
            proof = self.root / (run_id+'-proof.txt'); proof.write_text('External acceptance evidence fixture')
            assessment = self.write(run_id+'-acceptance.json', {'runId': run_id,
                'acceptanceFingerprint': self.manifest['acceptance']['fingerprint'],
                'accepted': accepted, 'source': 'human', 'completedAt': (end+timedelta(seconds=1)).isoformat(),
                'evidence': [proof.name]})
        return bench.import_run(self.manifest, pair, arm, session, run_id, binding, self.inputs, assessment)

    def test_legacy_service_timezone_offsets_preserve_actual_instants(self):
        self.assertEqual(bench._duration('2026-11-02T23:26:39+0800', '2026-11-02T15:26:42.347+00:00'), 3347.0)
        self.assertEqual(bench._duration('2026-11-02T23:26:39+0800', '2026-11-02T23:26:39+08:00'), 0.0)
        self.assertIsNone(bench._duration('2026-11-02T23:26:39', '2026-11-02T23:26:40+0800'))

    def test_freezes_files_acceptance_script_config_and_reproducible_order(self):
        self.assertEqual(self.manifest['task']['origin'], 'user_real_task')
        self.assertEqual(self.manifest['inputs'], bench.input_manifest(self.inputs))
        self.assertEqual(self.manifest['acceptance']['script']['sha256'], bench.file_record(self.script)['sha256'])
        other = bench.prepare_manifest('task-1', 'Reconcile actual inputs', self.inputs, self.criteria,
            {'baseline': self.config, 'current': self.config}, 'revision-old', 'revision-new', 2, 5)
        self.assertEqual(other['pairs'], self.manifest['pairs'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            changed = copy.deepcopy(self.manifest); changed['task']['brief'] = 'different'; bench.validate_manifest(changed)

    def test_changed_inputs_or_acceptance_are_rejected_before_observation(self):
        (self.inputs / 'request.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'inputs changed'): self.make_observation()
        self.manifest['inputs'] = bench.input_manifest(self.inputs)
        self.manifest['fingerprint'] = bench.fingerprint({k:v for k,v in self.manifest.items() if k != 'fingerprint'})
        self.script.write_text('modified validation')
        with self.assertRaisesRegex(ValueError, 'acceptance changed'): self.make_observation()

    def test_no_model_credentials_in_frozen_config(self):
        for bad in ({'worker': {'model': 'm', 'apiKey': 'secret'}},
                    {'worker': {'model': 'm', 'endpoint': 'https://user:pass@example.test/api'}},
                    {'worker': {'model': 'm', 'parameters': {'authorization': 'private'}}}):
            with self.assertRaises(ValueError): bench.model_config(bad)
        bench.model_config({'worker': {'model': 'm', 'parameters': {'max_tokens': 1000}}})

    def test_unknown_fields_never_become_zero_cost_or_usage(self):
        value = bench.measure_run({})
        for key in ('workflowWallMs', 'requestMs', 'coordinationMs', 'repairMs', 'callCount'):
            self.assertIsNone(value[key])
        self.assertIsNone(value['usage']['inputTokens'])
        self.assertIsNone(value['actualPaid']['amount'])
        self.assertIsNone(value['estimatedCost']['amount'])

    def test_retry_receipts_align_by_unique_context_and_missing_total_stays_null(self):
        run = {'id': 'r', 'callMetrics': [
            {'id': 'c1', 'nodeId': 'n', 'contextRef': 'original', 'elapsedMs': 300, 'outcome': 'error'},
            {'id': 'c2', 'nodeId': 'n', 'contextRef': 'repair', 'elapsedMs': 700, 'outcome': 'returned'}],
            'nodes': [{'id': 'n', 'phase': 'plan', 'contextRef': 'repair',
                       'receipt': {'returnedModel': 'm', 'usage': {'input_tokens': 30, 'output_tokens': 4}}}]}
        partial = bench.measure_run(run)
        self.assertIsNone(partial['usage']['inputTokens'])
        self.assertEqual(partial['usage']['knownInputTokens'], 30)
        self.assertEqual(partial['usage']['knownCalls'], 1)
        full = bench.measure_run(run, [{'contextRef': 'original', 'receipt':
            {'returnedModel': 'm', 'usage': {'input_tokens': 10, 'output_tokens': 2}}}],
            {'original': {}, 'repair': {'formatRepair': 1}})
        self.assertEqual(full['usage']['inputTokens'], 40)
        self.assertEqual(full['usage']['outputTokens'], 6)
        self.assertEqual(full['requestMs'], 1000)
        self.assertEqual(full['coordinationMs'], 1000)
        self.assertEqual(full['repairMs'], 700)
        self.assertEqual(full['errorCallCount'], 1)
        run['callMetrics'][0]['contextRef'] = 'repair'
        ambiguous = bench.measure_run(run)
        self.assertIsNone(ambiguous['usage']['inputTokens'])
        self.assertEqual(ambiguous['usage']['knownCalls'], 0)

    def test_actual_paid_and_user_estimates_are_separate_with_cache_and_reasoning(self):
        run = {'id': 'r', 'callMetrics': [{'nodeId': 'n', 'contextRef': 'c'}],
               'nodes': [{'id': 'n', 'contextRef': 'c', 'receipt': {'returnedModel': 'm', 'protocol': 'anthropic',
                    'usage': {'input_tokens': 100, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 80,
                              'output_tokens': 30, 'output_tokens_details': {'reasoning_tokens': 10}}}}]}
        rates = {'currency': 'CNY', 'source': 'user tariff 2026-11-02',
                 'models': {'m': {'inputPerMillion': 2, 'outputPerMillion': 4}}}
        result = bench.measure_run(run, rates=rates)
        self.assertEqual(result['usage']['inputTokens'], 200)
        self.assertEqual(result['usage']['outputTokens'], 30)
        self.assertAlmostEqual(result['estimatedCost']['amount'], .00052)
        self.assertIsNone(result['actualPaid']['amount'])
        paid = {'runId': 'r', 'kind': 'paid_invoice', 'amount': 1.2, 'currency': 'USD', 'source': 'invoice line 42'}
        result = bench.measure_run(run, billing=paid, rates=rates)
        self.assertEqual(result['actualPaid']['currency'], 'USD')
        self.assertEqual(result['estimatedCost']['currency'], 'CNY')
        for bad in ({**paid, 'runId': 'other'}, {**paid, 'kind': 'estimated'}, {**paid, 'amount': True}):
            with self.assertRaises(ValueError): bench.measure_run(run, billing=bad)

    def test_cost_summary_keeps_partial_and_mixed_currencies_unknown(self):
        def row(amount, currency):
            return {'metrics': {'actualPaid': {'amount': amount, 'currency': currency}}}
        partial = bench.summarize_cost([row(1, 'CNY'), row(None, None)], 'actualPaid')
        self.assertIsNone(partial['amount'])
        self.assertEqual(partial['knownSubtotalsByCurrency'], {'CNY': 1})
        mixed = bench.summarize_cost([row(1, 'CNY'), row(2, 'USD')], 'actualPaid')
        self.assertIsNone(mixed['amount'])
        self.assertFalse(mixed['complete'])
        complete = bench.summarize_cost([row(1, 'CNY'), row(2, 'CNY')], 'actualPaid')
        self.assertEqual((complete['amount'], complete['currency']), (3, 'CNY'))

    def test_missing_repair_context_is_unknown_and_failed_resume_attempts_remain_visible(self):
        run = {'callMetrics': [{'nodeId': 'n', 'contextRef': 'c', 'elapsedMs': 200}],
               'nodes': [{'id': 'n', 'phase': 'build'}],
               'executionAttemptsHistory': 'complete',
               'executionAttempts': [{'status': 'failed'}, {'status': 'completed'}]}
        result = bench.measure_run(run)
        self.assertIsNone(result['repairMs'])
        self.assertFalse(result['repairClassificationComplete'])
        self.assertEqual(result['executionAttempts']['statuses'], {'failed': 1, 'completed': 1})
        self.assertIsNone(bench.measure_run({})['executionAttempts']['count'])

    def test_retrospective_run_is_visible_but_not_counted_as_prospective_gain(self):
        self.now = datetime.now(timezone.utc) - timedelta(days=1)
        first, second = self.manifest['pairs'][0]['order']
        rows = [self.make_observation(arm=first), self.make_observation(arm=second, offset=30)]
        report = bench.summarize(self.manifest, rows)
        self.assertEqual(report['pairs'][0]['reason'], 'retrospective_binding')
        self.assertIsNone(report['medianDurationReductionPct'])
        self.assertEqual(report['arms']['baseline']['successes'], 1)

    def test_failure_cancel_and_unknown_acceptance_stay_in_denominator(self):
        records = [self.make_observation(1, 'baseline', accepted=None),
                   self.make_observation(2, 'baseline', status='failed', accepted=None),
                   self.make_observation(1, 'current', status='stopped', accepted=None)]
        report = bench.summarize(self.manifest, records)
        self.assertEqual(report['arms']['baseline']['observed'], 2)
        self.assertEqual(report['arms']['baseline']['confirmedSuccessRate'], 0)
        self.assertEqual(report['arms']['current']['statuses'], {'stopped': 1})
        self.assertEqual(report['arms']['current']['pending'], 1)
        self.assertIsNone(report['medianDurationReductionPct'])
        self.assertIsNone(records[0]['metrics']['endToEndMs'])
        self.assertEqual(records[1]['metrics']['endToEndMs'], 10000)

    def test_paired_accepted_duration_includes_external_acceptance_and_requires_order(self):
        first, second = self.manifest['pairs'][0]['order']
        lengths = {'baseline': 19, 'current': 9}
        records = [self.make_observation(arm=first, length=lengths[first], offset=0),
                   self.make_observation(arm=second, length=lengths[second], offset=30)]
        report = bench.summarize(self.manifest, records)
        self.assertEqual(report['eligiblePairs'], 1)
        self.assertEqual(report['medianDurationReductionPct'], 50)
        self.assertEqual(report['pairs'][0]['speedupRatio'], 2)
        wrong = [self.make_observation(arm=first, length=lengths[first], offset=30),
                 self.make_observation(arm=second, length=lengths[second], offset=0)]
        report = bench.summarize(self.manifest, wrong)
        self.assertEqual(report['pairs'][0]['reason'], 'order_or_overlap')
        self.assertIsNone(report['medianDurationReductionPct'])

    def test_swapped_actual_models_for_same_aliases_cannot_claim_architecture_speedup(self):
        config = {'planner': {'model': 'A'}, 'worker': {'model': 'B'}}
        self.manifest = bench.prepare_manifest('task-1', 'Reconcile actual inputs', self.inputs,
            self.criteria, {'baseline': config, 'current': config}, 'revision-old', 'revision-new', 2, 5)
        first, second = self.manifest['pairs'][0]['order']
        rows = [self.make_observation(arm=first), self.make_observation(arm=second, offset=30)]
        for row in rows:
            returned = ['A', 'B'] if row['arm'] == 'baseline' else ['B', 'A']
            run = {'callMetrics': [{'model': alias, 'nodeId': alias, 'contextRef': alias}
                                   for alias in ('planner', 'worker')],
                   'nodes': [{'id': alias, 'contextRef': alias, 'receipt': {'returnedModel': model}}
                             for alias, model in zip(('planner', 'worker'), returned)]}
            measured = bench.measure_run(run)
            for key in ('actualModels', 'actualModelsByAlias', 'modelEvidenceComplete'):
                row['metrics'][key] = measured[key]
            row['fingerprint'] = bench.fingerprint({k:v for k,v in row.items() if k != 'fingerprint'})
        self.assertEqual(rows[0]['metrics']['actualModels'], rows[1]['metrics']['actualModels'])
        report = bench.summarize(self.manifest, rows)
        self.assertEqual(report['pairs'][0]['reason'], 'model_evidence_incomplete_or_different')
        self.assertIsNone(report['medianDurationReductionPct'])

    def test_duplicate_import_or_tampered_record_is_rejected(self):
        record = self.make_observation()
        with self.assertRaisesRegex(ValueError, 'Duplicate'): bench.summarize(self.manifest, [record, record])
        record['success'] = False
        with self.assertRaisesRegex(ValueError, 'changed'): bench.summarize(self.manifest, [record])

    def test_live_runtime_binding_required_and_demo_cannot_be_relabelled(self):
        original = bench.read_json
        for patch in ({'mode': 'demo'}, {'status': 'running'}, {'comparisonArm': 'single'},
                      {'runtimeBuild': {}}, {'sourceMatchesRuntimeBuild': False},
                      {'executionAttempts': [{'runtimeBuild': {'fingerprint': 'other'}}]}):
            def altered(path):
                value = original(path)
                if 'runs' in value: value['runs'][0].update(patch)
                return value
            with self.subTest(patch=patch), unittest.mock.patch.object(bench, 'read_json', side_effect=altered):
                with self.assertRaises(ValueError): self.make_observation()

    def test_cli_prepare_and_empty_summary_do_not_execute_acceptance_script(self):
        brief = self.root / 'brief.txt'; brief.write_text('Real task brief')
        configs = self.write('models.json', {'baseline': self.config, 'current': self.config})
        manifest = self.root / 'manifest.json'
        with patch('builtins.print'):
            bench.main(['prepare', '--task-id', 'real-task', '--brief', str(brief), '--inputs', str(self.inputs),
                        '--criteria', str(self.criteria), '--script', str(self.script), '--models', str(configs),
                        '--baseline-revision', 'old', '--current-revision', 'new', '--out', str(manifest)])
            report = self.root / 'report.json'
            bench.main(['summarize', '--manifest', str(manifest), '--records-dir', str(self.root / 'records'),
                        '--out', str(report)])
        result = bench.read_json(report)
        self.assertIsNone(result['arms']['baseline']['confirmedSuccessRate'])
        self.assertIsNone(result['medianDurationReductionPct'])
        with self.assertRaises(FileExistsError): bench.write_new(report, {})

    def test_cli_import_preserves_run_identity_and_refuses_duplicate_slot(self):
        observation = self.make_observation()
        manifest = self.write('frozen.json', self.manifest)
        binding = self.write('binding.json', observation['binding'])
        args = ['import', '--manifest', str(manifest), '--pair', '1', '--arm', 'baseline',
                '--session', str(self.root / 'run-1-baseline.json'), '--run-id', 'run-1-baseline',
                '--binding', str(binding), '--inputs', str(self.inputs),
                '--assessment', str(self.root / 'run-1-baseline-acceptance.json'),
                '--records-dir', str(self.root / 'records')]
        with patch('builtins.print'):
            bench.main(args)
            with self.assertRaisesRegex(ValueError, 'Duplicate'): bench.main(args)
        saved = bench.read_json(self.root / 'records' / 'pair-001-baseline.json')
        self.assertEqual(saved['runId'], 'run-1-baseline')
        self.assertTrue(saved['success'])
        self.assertEqual(saved['metrics']['endToEndMs'], 11000)


if __name__ == '__main__':
    unittest.main()
