import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app import Store
from collaboration_comparison import ComparisonRunner, comparison_summaries
from collaboration_evaluation import case_definition, DEVELOPMENT_CASE_ID
from studio import Studio
from test_studio_business import BusinessClient, module


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        spec = module('result', 'data')
        spec.update(scope=['result.json'], roleType='data')
        self.client = BusinessClient([spec])
        self.client.mutate = self.answer
        self.studio = Studio(Store(Path(self.temp.name), seed=False), self.client)
        self.owner = self.studio.create_project('对照记录', '')['id']
        self.runner = ComparisonRunner(self.studio)

    def tearDown(self):
        if self.runner.thread:
            self.runner.thread.join(20)
        self.temp.cleanup()

    def answer(self, stage, result):
        if stage == 'build':
            result['files'] = [{'path': 'result.json', 'content': json.dumps(case_definition()['reference']['result'])}]
        return result

    def test_isolated_real_workflow_and_frozen_objective_grader(self):
        record = self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'])
        self.runner.thread.join(20)
        saved = self.runner.index(self.owner)['records'][0]
        self.assertEqual(saved['id'], record['id'])
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        arms = saved['arms']
        self.assertNotEqual(arms['single']['projectId'], arms['multi']['projectId'])
        self.assertEqual(arms['single']['inputFingerprint'], arms['multi']['inputFingerprint'])
        self.assertEqual(saved['comparison']['qualityDeltaPoints'], 0)
        for name, arm in arms.items():
            self.assertEqual(arm['evaluation']['passed'], 15)
            self.assertEqual(arm['metrics']['callCount'], 3)
            session = self.studio.load(arm['projectId'], arm['sessionId'])
            run = session['runs'][0]
            self.assertEqual(run['workPolicy'], saved['workPolicy'])
            self.assertEqual(run['teamSnapshot']['memories'], [])
            self.assertTrue(all(n['status'] == 'done' for n in run['nodes']))
            actual = {n['agent'] for n in run['nodes']}
            self.assertEqual(actual, {'deepseek'} if name == 'single' else {'deepseek', 'sol'})
            for node in run['nodes']:
                context = self.studio.context(arm['projectId'], arm['sessionId'], node['contextRef'])
                self.assertNotIn('reference', context['memory']['sharedEvidence'])
        self.assertNotIn('reference', self.runner.index(self.owner)['case'])
        self.assertEqual(self.studio.load(arms['single']['projectId'], arms['single']['sessionId'])['status'], 'completed')
        del saved['comparison']['efficiency']
        self.runner._save(saved)
        path = self.studio.project_dir(self.owner) / 'comparisons' / (saved['id'] + '.json')
        before = path.read_bytes()
        self.assertIn('efficiency', self.runner.index(self.owner)['records'][0]['comparison'])
        self.assertEqual(path.read_bytes(), before)

    def test_model_approval_does_not_override_wrong_result(self):
        def wrong(stage, result):
            self.answer(stage, result)
            if stage == 'build':
                result['files'][0]['content'] = '{}'
            return result
        self.client.mutate = wrong
        self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'])
        self.runner.thread.join(20)
        saved = self.runner.index(self.owner)['records'][0]
        self.assertEqual(saved['status'], 'completed')
        for arm in saved['arms'].values():
            self.assertEqual(arm['status'], 'completed')
            self.assertFalse(arm['evaluation']['accepted'])
            self.assertEqual(arm['evaluation']['passed'], 0)
        self.assertIsNone(saved['comparison']['speedupRatio'])

    def test_invalid_team_creates_no_trials(self):
        for team in [['deepseek'], ['deepseek', 'native_codex'], ['deepseek', 'image']]:
            with self.assertRaises(ValueError):
                self.runner.start(self.owner, 'deepseek', team)
        self.assertEqual(len(self.studio.store.list_projects()), 1)

    def test_stop_keeps_first_trial_and_does_not_start_second(self):
        entered, release = threading.Event(), threading.Event()
        def wait_answer(stage, result):
            if stage == 'plan':
                entered.set()
                release.wait(10)
            return self.answer(stage, result)
        self.client.mutate = wait_answer
        record = self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'], repetitions=3)
        self.assertTrue(entered.wait(10))
        self.runner.stop(self.owner, record['id'])
        release.set()
        self.runner.thread.join(20)
        data = self.runner.index(self.owner)
        self.assertEqual(len(data['records']), 3)
        self.assertTrue(all(r['status'] == 'stopped' for r in data['records']))
        saved = next(r for r in data['records'] if r['id'] == record['id'])
        self.assertEqual(saved['status'], 'stopped')
        self.assertEqual(sum(a['status'] == 'waiting' for a in saved['arms'].values()), 1)
        attempted = next(a for a in saved['arms'].values() if a['status'] != 'waiting')
        run = self.studio.load(attempted['projectId'], attempted['sessionId'])['runs'][0]
        self.assertIn('ended', run)

    def test_repeated_trials_freeze_user_roles_and_form_one_matched_group(self):
        project = self.studio.save_custom_role(self.owner, None, {
            'title': '履约核算员', 'businessContext': '库存订单履约环节',
            'responsibilities': '核对订单分配与库存变动', 'deliverables': 'result.json',
            'acceptance': '满足给定订单规则，不能凭自述通过'}, self.studio.meta(self.owner)['version'])
        rid = next(iter(project['teamConfig']['customRoles']))
        self.client.modules[0]['roleId'] = rid
        self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'], repetitions=2)
        self.runner.thread.join(20)
        data = self.runner.index(self.owner)
        self.assertEqual(len(data['records']), 2)
        self.assertEqual(len({r['batchId'] for r in data['records']}), 1)
        self.assertEqual({r['repeatIndex'] for r in data['records']}, {1, 2})
        self.assertEqual(len(data['summaries']), 1)
        self.assertEqual(data['summaries'][0]['assessedPairs'], 2)
        self.assertEqual(data['summaries'][0]['bothPassed'], 2)
        project_ids = set()
        for record in data['records']:
            for arm in record['arms'].values():
                project_ids.add(arm['projectId'])
                session = self.studio.load(arm['projectId'], arm['sessionId'])
                run = session['runs'][0]
                self.assertEqual(run['projectSnapshot']['teamConfig']['customRoles'][rid]['title'], '履约核算员')
                contract = run['taskContracts']['result']
                self.assertEqual(contract['workFramework']['roleDefinition']['title'], '履约核算员')
        self.assertEqual(len(project_ids), 4)

    def test_summaries_keep_failures_and_separate_changed_conditions(self):
        self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'])
        self.runner.thread.join(20)
        original = self.runner.index(self.owner)['records'][0]
        failure = copy.deepcopy(original)
        failure['id'] = 'failure'
        failure['arms']['multi']['status'] = 'failed'
        failure['arms']['multi']['evaluation'].update(accepted=False, scorePct=0)
        changed = copy.deepcopy(original)
        changed['id'] = 'changed'
        changed['profilesFingerprint'] = 'different-provider-or-model'
        pending = copy.deepcopy(original)
        pending['id'] = 'pending'
        pending['status'] = 'stopped'
        del pending['arms']['multi']['evaluation']
        summaries = comparison_summaries([original, failure, changed, pending])
        self.assertEqual(len(summaries), 2)
        group = next(s for s in summaries if 'failure' in s['recordIds'])
        self.assertEqual(group['scheduledPairs'], 3)
        self.assertEqual(group['assessedPairs'], 2)
        self.assertEqual(group['incompletePairs'], 1)
        self.assertEqual(group['bothPassed'], 1)
        self.assertEqual(group['singleOnlyPassed'], 1)
        self.assertEqual(group['meanQualityDeltaPoints'], -50)

    def test_case_and_repeat_count_are_validated_before_creating_projects(self):
        for repeat in (0, 6, True, 1.5):
            with self.assertRaises(ValueError):
                self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'], repetitions=repeat)
        with self.assertRaises(ValueError):
            self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'], case_id='unknown-case')
        self.assertEqual(len(self.studio.store.list_projects()), 1)

    def test_development_requires_discussion_members_before_creating_trials(self):
        with self.assertRaises(ValueError):
            self.runner.start(self.owner, 'opus', ['opus', 'sol'], DEVELOPMENT_CASE_ID)
        self.assertEqual(len(self.studio.store.list_projects()), 1)

    def test_development_runs_actual_sources_and_preserves_comparison_evidence(self):
        from test_development_evaluation import SCHEDULER, REPORTS, CLI
        from development_evaluation import ARTIFACT_PATHS
        spec = module('software', 'development', 4)
        spec['scope'] = list(ARTIFACT_PATHS)
        self.client.modules = [spec]
        def code_output(stage, result):
            if stage == 'build':
                result['files'] = [{'path': p, 'content': c} for p, c in
                                   zip(ARTIFACT_PATHS, (SCHEDULER, REPORTS, CLI))]
            return result
        self.client.mutate = code_output
        self.runner.start(self.owner, 'opus', ['opus', 'sol', 'deepseek'], DEVELOPMENT_CASE_ID)
        self.runner.thread.join(20)
        saved = self.runner.index(self.owner)['records'][0]
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        self.assertIn('实际交付代码', saved['comparison']['notice'])
        for name, arm in saved['arms'].items():
            self.assertEqual(arm['status'], 'completed', arm.get('error'))
            self.assertEqual(arm['evaluation']['passed'], 16, arm['evaluation'])
            self.assertEqual(arm['evaluation']['runtime']['exitCode'], 0)
            self.assertGreater(arm['metrics']['evaluationMs'], 0)
            self.assertAlmostEqual(arm['metrics']['wallMs'],
                                   arm['metrics']['workflowWallMs'] + arm['metrics']['evaluationMs'])
            run = self.studio.load(arm['projectId'], arm['sessionId'])['runs'][0]
            self.assertEqual(run['comparisonArm'], name)
            self.assertNotIn('objectiveEvaluations', run)
            self.assertTrue(all(n['agent'] == 'opus' for n in run['nodes']) if name == 'single'
                            else len({n['agent'] for n in run['nodes']}) > 1)

    def test_summary_efficiency_scores_the_unrounded_median_of_eligible_pairs(self):
        self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'])
        self.runner.thread.join(20)
        original = self.runner.index(self.owner)['records'][0]
        records = []
        for duration in (90001, 80000, 110000):
            record = copy.deepcopy(original)
            record['id'] = str(duration)
            record['arms']['single']['metrics']['wallMs'] = 100000
            record['arms']['multi']['metrics']['wallMs'] = duration
            records.append(record)
        group = comparison_summaries(records)[0]
        self.assertEqual(group['matchedSuccessTimingPairs'], 3)
        self.assertAlmostEqual(group['medianDurationReductionPct'], 9.999)
        self.assertEqual(group['globalPoints'], 0)
        records[0]['arms']['multi']['metrics']['wallMs'] = 90000
        group = comparison_summaries(records)[0]
        self.assertEqual(group['medianDurationReductionPct'], 10)
        self.assertEqual(group['globalPoints'], 1)
        negative = comparison_summaries([records[-1]])[0]
        self.assertEqual((negative['medianDurationReductionPct'], negative['globalPoints']), (-10, 0))
        records[0]['arms']['multi']['status'] = 'failed'
        records[1]['arms']['multi']['metrics']['wallMs'] = None
        records[2]['arms']['single']['metrics']['wallMs'] = 0
        unknown = comparison_summaries(records)[0]
        self.assertEqual(unknown['matchedSuccessTimingPairs'], 0)
        self.assertIsNone(unknown['medianDurationReductionPct'])
        self.assertIsNone(unknown['globalPoints'])

    def test_invalid_case_is_labeled_and_excluded_without_rewriting_history(self):
        self.runner.start(self.owner, 'deepseek', ['deepseek', 'sol'])
        self.runner.thread.join(20)
        record = self.runner.index(self.owner)['records'][0]
        record['caseId'] = 'research-evidence-v1'
        self.runner._save(record)
        path = self.studio.project_dir(self.owner) / 'comparisons' / (record['id'] + '.json')
        original = path.read_bytes()
        data = self.runner.index(self.owner)
        self.assertEqual(data['summaries'], [])
        self.assertIn('评分标准冲突', data['records'][0]['evaluationNotice'])
        self.assertEqual(data['records'][0]['arms'], record['arms'])
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
