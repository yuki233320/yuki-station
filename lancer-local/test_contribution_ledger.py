"""Contribution points require changed deliverables and independent evidence."""
import copy
import hashlib
import unittest

from contribution_ledger import build_contribution_ledger


def manifest(content):
    data = content.encode()
    return [{'path': 'answer.json', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]


def quality(round_no, status, content, passed):
    return {'round': round_no, 'eventSeq': 10 + round_no,
            'modules': [{'moduleId': 'report', 'title': '资料核对', 'reviewNodeId': 'artifact-review',
                         'criteria': [{'criterionId': 'report-c1', 'status': status, 'reason': '核对原始引用'}],
                         'artifactManifest': manifest(content), 'gatePassed': passed}]}


def run_record():
    return {'id': 'run', 'taskContracts': {'report': {'id': 'run:report', 'fingerprint': 'contract-hash',
            'criteria': [{'id': 'report-c1', 'text': '引用与原文一致'}]}},
            'qualityHistory': [quality(0, 'unmet', 'old', False), quality(1, 'met', 'new', True)],
            'nodes': [{'id': 'artifact-review', 'agent': 'sol', 'phase': 'artifact_review'},
                      {'id': 'repair-1', 'agent': 'gemini', 'phase': 'build', 'repairRound': 1},
                      {'id': 'review-1', 'agent': 'sol', 'phase': 'review'}]}


def add_suggestions(run, mapped=True):
    finding = {'id': 'finding-1', 'model': 'sol', 'claim': '补齐原文引用'}
    run['moduleReviews'] = {'report': {'evidence': [finding], 'decisions': [
        {'findingId': 'finding-1', 'decision': 'accept', **({'criterionId': 'report-c1'} if mapped else {})}]}}
    run['nodes'][-1]['result'] = {'findings': [finding]}
    run['discussions'] = [{'id': 'finding-1', 'eventSeq': 3}]


def add_reflection(run):
    add_suggestions(run)
    run['workPolicy'] = {'deepReview': True, 'deepReviewMultiplier': 2}
    review = run['moduleReviews']['report']
    review.update(roundLimit=3, reflection={'questionNodeId': 'reflect-question',
                                          'answerNodeIds': ['reflect-sol'], 'round': 3})
    finding = {'id': 'deep-1', 'model': 'sol', 'claim': '引用单位缺失源文档生效时间',
               'reflection': True, 'contributionWeight': 2}
    review['evidence'].append(finding)
    review['decisions'].append({'findingId': finding['id'], 'decision': 'accept', 'criterionId': 'report-c1'})
    run['nodes'].extend([
        {'id': 'reflect-question', 'phase': 'decision', 'agent': 'opus', 'status': 'done',
         'discussionKind': 'reflection_prompt', 'fixedPrompt': True},
        {'id': 'reflect-sol', 'moduleId': 'report', 'phase': 'review', 'agent': 'sol',
         'status': 'done', 'round': 3, 'discussionKind': 'reflection', 'result': {'findings': [finding]}}])
    run['discussions'].append({'id': 'reflect-sol-answer', 'eventSeq': 5})


def evaluations(cid='report-c1'):
    return [{'round': 0, 'moduleId': 'report', 'nodeId': 'build-report', 'eventSeq': 8,
             'evaluatorFingerprint': 'frozen-evaluator', 'artifactManifest': manifest('old'),
             'criteria': [{'id': cid, 'status': 'failed'}]},
            {'round': 1, 'moduleId': 'report', 'nodeId': 'repair-1', 'eventSeq': 9,
             'evaluatorFingerprint': 'frozen-evaluator', 'artifactManifest': manifest('new'),
             'criteria': [{'id': cid, 'status': 'passed'}]}]


def validation(record, verdict='verified', seq=20):
    return {'recordId': record['id'], 'evidenceFingerprint': record['evidenceFingerprint'],
            'verdict': verdict, 'beforeObservation': '旧引用与原文不符', 'afterObservation': '新引用逐条对应',
            'method': '人工逐条对照输入资料', 'eventSeq': seq, 'created': '2026-10-31', 'actor': 'user'}


class ContributionLedgerTests(unittest.TestCase):
    def test_actual_discussion_engine_binds_reflection_weight_and_event(self):
        from test_studio_business import BusinessWorkflowTests, module
        fixture = BusinessWorkflowTests()
        fixture.setUp()
        try:
            fixture.client.modules = [module('analysis', 'research', 4)]
            def bind_reflection(stage, result):
                if stage == 'decision':
                    for decision in result['decisions']:
                        decision['decision'] = 'accept' if decision['findingId'].startswith('reflect-') else 'reject'
                        decision['criterionId'] = 'analysis-c1'
                return result
            fixture.client.mutate = bind_reflection
            session = fixture.run_task(coordinator_model='opus', team_models=['opus', 'sol', 'deepseek'])
            self.assertEqual(session['status'], 'completed', session.get('error'))
            run = session['runs'][-1]
            ledger = build_contribution_ledger(run)
            record = ledger['records'][0]
            self.assertEqual(len(ledger['records']), 1)
            self.assertEqual((record['weight'], record['potentialPoints'], record['points']), (2, 2, 0))
            self.assertTrue(all(source['origin'] == 'reflection' for source in record['sources']))
            first_answer = min(row['eventSeq'] for row in run['discussions'] if row['kind'] == 'reflection')
            self.assertEqual(record['identifiedEventSeq'], first_answer)
            self.assertTrue(record['canVerify'])
            run['contributionValidations'] = [validation(record)]
            self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 2)
        finally:
            fixture.tearDown()

    def test_reflection_needs_independent_verification_before_weighted_points(self):
        run = run_record()
        add_reflection(run)
        pending = build_contribution_ledger(run)['records'][0]
        self.assertEqual((pending['weight'], pending['potentialPoints'], pending['points']), (2, 2, 0))
        self.assertEqual(pending['state'], 'pending')
        deep = next(source for source in pending['sources'] if source.get('origin') == 'reflection')
        self.assertEqual((deep['nodeId'], deep['questionNodeId'], deep['weight']),
                         ('reflect-sol', 'reflect-question', 2))
        run['contributionValidations'] = [validation(pending)]
        ledger = build_contribution_ledger(run)
        self.assertEqual((ledger['records'][0]['points'], ledger['summary']['verifiedPoints']), (2, 2))
        run['contributionValidations'].append(validation(pending, 'rejected', 21))
        ledger = build_contribution_ledger(run)
        self.assertEqual((ledger['records'][0]['state'], ledger['summary']['verifiedPoints']), ('rejected', 0))

    def test_reflection_duplicates_share_maximum_weight_not_sum(self):
        run = run_record()
        add_reflection(run)
        peer = copy.deepcopy(run['nodes'][-1])
        peer.update(id='reflect-gemini', agent='gemini')
        peer['result']['findings'][0].update(id='deep-2', model='gemini')
        run['nodes'].append(peer)
        review = run['moduleReviews']['report']
        review['reflection']['answerNodeIds'].append(peer['id'])
        review['evidence'].append(peer['result']['findings'][0])
        review['decisions'].append({'findingId': 'deep-2', 'decision': 'accept', 'criterionId': 'report-c1'})
        review['decisions'].append(copy.deepcopy(review['decisions'][-1]))
        run['objectiveEvaluations'] = evaluations()
        ledger = build_contribution_ledger(run)
        self.assertEqual(len(ledger['records']), 1)
        self.assertEqual(ledger['summary']['verifiedPoints'], 2)
        self.assertEqual(ledger['records'][0]['verification']['basis'], 'objective_evaluation')

    def test_model_supplied_reflection_weight_cannot_grant_privilege(self):
        run = run_record()
        add_suggestions(run)
        run['workPolicy'] = {'deepReview': True, 'deepReviewMultiplier': 2}
        finding = run['moduleReviews']['report']['evidence'][0]
        finding.update(reflection=True, contributionWeight=100, weight=100, origin='reflection')
        run['objectiveEvaluations'] = evaluations()
        record = build_contribution_ledger(run)['records'][0]
        self.assertEqual((record['weight'], record['points']), (1, 1))
        self.assertNotIn('origin', record['sources'][0])

    def test_reflection_weight_requires_host_question_and_answer_links(self):
        for change in ('no_policy', 'disabled', 'one_round', 'no_question', 'no_answer_link', 'wrong_module', 'unfinished'):
            with self.subTest(change=change):
                run = run_record()
                add_reflection(run)
                if change == 'no_policy':
                    run.pop('workPolicy')
                elif change == 'disabled':
                    run['workPolicy']['deepReview'] = False
                elif change == 'one_round':
                    run['moduleReviews']['report']['roundLimit'] = 1
                elif change == 'no_question':
                    run['nodes'][-2].pop('fixedPrompt')
                elif change == 'no_answer_link':
                    run['moduleReviews']['report']['reflection']['answerNodeIds'] = []
                elif change == 'wrong_module':
                    run['nodes'][-1]['moduleId'] = 'another'
                else:
                    run['nodes'][-1]['status'] = 'running'
                run['objectiveEvaluations'] = evaluations()
                self.assertEqual(build_contribution_ledger(run)['records'][0]['points'], 1)

    def test_reflection_weight_policy_change_invalidates_manual_proof(self):
        run = run_record()
        add_reflection(run)
        original = build_contribution_ledger(run)['records'][0]
        run['contributionValidations'] = [validation(original)]
        run['workPolicy']['deepReviewMultiplier'] = 3
        changed = build_contribution_ledger(run)['records'][0]
        self.assertNotEqual(changed['evidenceFingerprint'], original['evidenceFingerprint'])
        self.assertEqual((changed['weight'], changed['points']), (3, 0))
        run['contributionValidations'].append(validation(changed, seq=21))
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 3)

    def test_unrelated_objective_criterion_does_not_inherit_reflection_weight(self):
        run = run_record()
        add_reflection(run)
        run['objectiveCriteria'] = {'report': [{'id': 'report-objective-cites', 'text': '固定引用标准'}]}
        run['objectiveEvaluations'] = evaluations('report-objective-cites')
        ledger = build_contribution_ledger(run)
        objective = next(row for row in ledger['records'] if row['criterionSource'] == 'objective')
        contract = next(row for row in ledger['records'] if row['criterionSource'] == 'contract')
        self.assertEqual((objective['weight'], objective['points']), (1, 1))
        self.assertEqual((contract['weight'], contract['potentialPoints'], contract['points']), (2, 0, 0))
        self.assertEqual(ledger['summary']['verifiedPoints'], 1)

    def test_ordinary_legacy_record_keeps_one_point_and_frozen_history(self):
        run = run_record()
        record = build_contribution_ledger(run)['records'][0]
        run['contributionValidations'] = [validation(record)]
        run['contributionHistory'] = [copy.deepcopy(build_contribution_ledger(run))]
        before = copy.deepcopy(run)
        ledger = build_contribution_ledger(run)
        self.assertEqual((ledger['records'][0]['weight'], ledger['records'][0]['points']), (1, 1))
        self.assertEqual(run, before)

    def test_first_unmet_is_immediately_visible_without_claiming_correction_succeeded(self):
        run = run_record()
        run['qualityHistory'] = run['qualityHistory'][:1]
        record = build_contribution_ledger(run)['records'][0]
        self.assertEqual((record['kind'], record['state'], record['points']), ('correction', 'pending', 0))
        self.assertEqual(record['identifiedEventSeq'], 10)
        self.assertFalse(record['canVerify'])
        self.assertEqual(record['beforeManifest'], record['afterManifest'])

    def test_objective_covered_artifact_transition_cannot_also_earn_contract_point(self):
        run = run_record()
        run['objectiveCriteria'] = {'report': [{'id': 'report-objective-cites', 'text': '固定引用标准'}]}
        run['objectiveEvaluations'] = evaluations('report-objective-cites')
        ledger = build_contribution_ledger(run)
        contract = next(row for row in ledger['records'] if row['criterionSource'] == 'contract')
        objective = next(row for row in ledger['records'] if row['criterionSource'] == 'objective')
        self.assertEqual(contract['coveredBy'], [objective['id']])
        self.assertEqual(contract['verification']['basis'], 'covered_by_objective')
        self.assertFalse(contract['canVerify'])
        run['contributionValidations'] = [validation(contract)]
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 1)
        run['objectiveEvaluations'][0]['artifactManifest'] = manifest('different-before')
        ledger = build_contribution_ledger(run)
        contract = next(row for row in ledger['records'] if row['criterionSource'] == 'contract')
        self.assertTrue(contract['canVerify'])
        self.assertNotIn('coveredBy', contract)

    def test_model_assessment_and_command_success_cannot_award_points(self):
        run = run_record()
        run['qualityHistory'][-1]['modules'][0]['checks'] = [{'pass': True, 'basis': 'native_required_checks'}]
        record = build_contribution_ledger(run)['records'][0]
        self.assertEqual((record['state'], record['points']), ('pending', 0))
        self.assertTrue(record['canVerify'])
        self.assertEqual(record['sources'][0]['model'], 'sol')
        run['qualityHistory'][0]['modules'][0]['criteria'][0]['status'] = 'unverified'
        self.assertEqual(build_contribution_ledger(run)['records'], [])

    def test_accepted_suggestion_and_correction_share_one_fixed_unit(self):
        run = run_record()
        add_suggestions(run)
        run['objectiveEvaluations'] = evaluations()
        ledger = build_contribution_ledger(run)
        self.assertEqual(len(ledger['records']), 1)
        record = ledger['records'][0]
        self.assertEqual((record['kind'], record['points']), ('correction', 1))
        self.assertEqual({source['kind'] for source in record['sources']},
                         {'accepted_suggestion', 'identified_unmet_criterion'})
        self.assertEqual(record['identifiedEventSeq'], 3)
        run['moduleReviews']['report']['decisions'].append(copy.deepcopy(run['moduleReviews']['report']['decisions'][0]))
        self.assertEqual(build_contribution_ledger(run), ledger)

    def test_unmapped_accept_is_visible_but_never_verified(self):
        run = run_record()
        add_suggestions(run, mapped=False)
        record = next(item for item in build_contribution_ledger(run)['records'] if item['criterionId'] is None)
        self.assertFalse(record['canVerify'])
        run['contributionValidations'] = [validation(record)]
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 0)
        run['contributionValidations'] = [validation(record, 'rejected')]
        self.assertEqual(build_contribution_ledger(run)['summary']['rejectedCount'], 1)

    def test_objective_proof_requires_same_evaluator_criterion_artifacts_and_gate(self):
        for change in ('evaluator', 'criterion', 'manifest', 'gate', 'later_failure'):
            with self.subTest(change=change):
                run = run_record()
                run['objectiveEvaluations'] = evaluations()
                if change == 'evaluator':
                    run['objectiveEvaluations'][1]['evaluatorFingerprint'] = 'different'
                elif change == 'criterion':
                    run['objectiveEvaluations'][1]['criteria'][0]['id'] = 'other'
                elif change == 'manifest':
                    run['objectiveEvaluations'][1]['artifactManifest'] = manifest('unrelated')
                elif change == 'gate':
                    run['qualityHistory'][1]['modules'][0]['gatePassed'] = False
                else:
                    later = copy.deepcopy(run['objectiveEvaluations'][1])
                    later['round'] = 2
                    later['criteria'][0]['status'] = 'failed'
                    run['objectiveEvaluations'].append(later)
                self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 0)

    def test_fixed_objective_criterion_does_not_need_model_defined_criterion(self):
        run = run_record()
        run['qualityHistory'][0]['modules'][0]['criteria'][0]['status'] = 'met'
        run['objectiveCriteria'] = {'report': [{'id': 'report-objective-cites', 'text': '固定答案的引用匹配'}]}
        run['objectiveEvaluations'] = evaluations('report-objective-cites')
        record = build_contribution_ledger(run)['records'][0]
        self.assertEqual((record['criterionSource'], record['criterion'], record['points']),
                         ('objective', '固定答案的引用匹配', 1))
        self.assertEqual(record['verification']['basis'], 'objective_evaluation')
        run['objectiveEvaluations'][0]['criteria'][0]['status'] = 'passed'
        self.assertEqual(build_contribution_ledger(run)['records'], [])

    def test_manual_verification_is_current_and_rejection_overrides_objective(self):
        run = run_record()
        record = build_contribution_ledger(run)['records'][0]
        run['contributionValidations'] = [validation(record)]
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 1)
        stale = validation(record, seq=21)
        stale['evidenceFingerprint'] = 'different-evidence'
        run['contributionValidations'].append(stale)
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 0)
        run['objectiveEvaluations'] = evaluations()
        objective_record = build_contribution_ledger(run)['records'][0]
        run['contributionValidations'].append(validation(objective_record, 'rejected', 22))
        self.assertEqual(build_contribution_ledger(run)['records'][0]['state'], 'rejected')

    def test_later_artifact_changes_and_regressions_remove_old_points(self):
        run = run_record()
        record = build_contribution_ledger(run)['records'][0]
        run['contributionValidations'] = [validation(record)]
        run['qualityHistory'].append(quality(2, 'met', 'another', True))
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 0)
        run['qualityHistory'].append(quality(3, 'unmet', 'new', False))
        current = build_contribution_ledger(run)['records'][0]
        self.assertFalse(current['canVerify'])
        run['contributionValidations'].append(validation(current, seq=23))
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 0)
        run['qualityHistory'].append(quality(4, 'met', 'new', True))
        current = build_contribution_ledger(run)['records'][0]
        run['contributionValidations'].append(validation(current, seq=24))
        ledger = build_contribution_ledger(run)
        self.assertEqual((len(ledger['records']), ledger['summary']['verifiedPoints']), (1, 1))

    def test_no_change_cannot_be_manually_scored(self):
        run = run_record()
        add_suggestions(run)
        run['qualityHistory'][1]['modules'][0]['artifactManifest'] = manifest('old')
        record = build_contribution_ledger(run)['records'][0]
        self.assertFalse(record['canVerify'])
        run['contributionValidations'] = [validation(record)]
        self.assertEqual(build_contribution_ledger(run)['summary']['verifiedPoints'], 0)

    def test_original_plan_to_artifact_suggestion_allows_only_manual_verification(self):
        run = run_record()
        add_suggestions(run)
        run['qualityHistory'] = [quality(0, 'met', 'new', True)]
        self.assertFalse(build_contribution_ledger(run)['records'][0]['canVerify'])
        run['nodes'].append({'id': 'draft', 'result': {'modules': [{'id': 'report', 'instructions': '整理资料'}]}})
        record = build_contribution_ledger(run)['records'][0]
        self.assertTrue(record['canVerify'])
        self.assertEqual(record['beforeKind'], 'plan')
        self.assertEqual(record['beforeManifest'][0]['path'], '@plan/report.json')
        self.assertEqual(record['points'], 0)
        run['contributionValidations'] = [validation(record)]
        self.assertEqual(build_contribution_ledger(run)['records'][0]['verification']['basis'], 'human_review')

    def test_first_pass_and_actual_request_effort_are_separate(self):
        run = run_record()
        run['callMetrics'] = [{'nodeId': 'review-1', 'elapsedMs': 5, 'outcome': 'returned'},
                              {'nodeId': 'review-1', 'elapsedMs': 7, 'outcome': 'error'},
                              {'nodeId': 'repair-1', 'elapsedMs': 20, 'outcome': 'returned'}]
        summary = build_contribution_ledger(run)['summary']
        self.assertEqual(summary['firstPass'], {'passed': 0, 'total': 1, 'ratePct': 0.0})
        self.assertEqual((summary['discussionCalls'], summary['discussionElapsedMs']), (2, 12))
        self.assertEqual((summary['repairCalls'], summary['repairElapsedMs']), (1, 20))
        run['callMetrics'][-1].pop('elapsedMs')
        self.assertIsNone(build_contribution_ledger(run)['summary']['repairElapsedMs'])
        del run['callMetrics']
        self.assertIsNone(build_contribution_ledger(run)['summary']['discussionElapsedMs'])

    def test_repeated_reads_are_pure_and_legacy_missing_evidence_stays_empty(self):
        run = run_record()
        add_suggestions(run)
        before = copy.deepcopy(run)
        self.assertEqual(build_contribution_ledger(run), build_contribution_ledger(run))
        self.assertEqual(run, before)
        ledger = build_contribution_ledger({})
        self.assertEqual(ledger['records'], [])
        self.assertIsNone(ledger['summary']['firstPass']['ratePct'])


if __name__ == '__main__':
    unittest.main()
