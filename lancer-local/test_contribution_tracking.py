"""Contribution records follow actual scheduler output, not fixture self-scores."""
import copy
import hashlib
import io
import json
import unittest
import zipfile

from app import Conflict
from collaboration_evaluation import CASE_ID, case_definition, evaluate_delivery
from contribution_ledger import build_contribution_ledger
from contribution_tracking import record_objective_evaluations, verify_contribution
import test_quality_flow as fixtures
from test_studio_business import module


class ContributionTrackingTests(unittest.TestCase):
    setUp = fixtures.QualityFlowTests.setUp
    tearDown = fixtures.QualityFlowTests.tearDown
    execute = fixtures.QualityFlowTests.execute

    def repair_once(self, before='原始交付缺少依据。', after='修复交付补齐实际依据。', path='brief.md'):
        attempts = {'build': 0, 'integration': 0}

        def respond(stage, result):
            if stage in attempts:
                attempts[stage] += 1
            if stage == 'build':
                result['files'] = [{'path': path, 'content': before if attempts[stage] == 1 else after}]
            if stage == 'integration' and attempts[stage] == 1:
                result['accepted'] = False
                row = result['moduleAcceptance'][0]
                row.update(accepted=False, unmet=[row['criteria'][0]['criterion']])
                row['criteria'][0]['status'] = 'unmet'
            return result

        self.client.mutate = respond
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual(attempts, {'build': 2, 'integration': 2})
        return session

    def request(self, session, run, record, **changes):
        return dict(version=session['version'], runId=run['id'], recordId=record['id'],
                    evidenceFingerprint=record['evidenceFingerprint'], verdict='verified',
                    beforeObservation='旧文件缺少对应依据。', afterObservation='新文件已逐项补齐。',
                    method='人工读取前后文件并对照固定验收项。', **changes)

    def test_first_delivery_has_zero_points_and_persisted_exported_snapshot(self):
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(run['contributionVersion'], 1)
        self.assertEqual(len(run['contributionHistory']), 1)
        snapshot = run['contributionHistory'][0]
        self.assertEqual(snapshot['records'], [])
        self.assertEqual(snapshot['summary']['verifiedPoints'], 0)
        self.assertEqual(snapshot['summary']['firstPass'], {'passed': 1, 'total': 1, 'ratePct': 100})
        event = next(e for e in session['events'] if e['seq'] == snapshot['eventSeq'])
        self.assertEqual((event['type'], event['runId']), ('quality_passed', run['id']))
        with zipfile.ZipFile(io.BytesIO(self.studio.export(self.pid, session['id']))) as archive:
            saved = json.loads(archive.read('会话与执行记录.json'))
        self.assertEqual(saved['runs'][-1]['contributionHistory'], run['contributionHistory'])
        metrics = self.studio.project(self.pid)['contributionMetrics']
        self.assertEqual((metrics['verifiedPoints'], metrics['runCount']), (0, 1))

    def test_failed_repair_call_is_recorded_without_a_second_quality_result(self):
        def reject_then_interrupt(stage, result):
            if stage == 'integration':
                result['accepted'] = False
                row = result['moduleAcceptance'][0]
                row.update(accepted=False, unmet=[row['criteria'][0]['criterion']])
                row['criteria'][0]['status'] = 'unmet'
                self.client.fail_once = next(call['key'] for call in self.client.calls if call['key'][0] == 'build')
            return result

        self.client.mutate = reject_then_interrupt
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('fixture interrupted', session['error'])
        run = session['runs'][-1]
        self.assertEqual(len(run['qualityHistory']), 1)
        repair = next(call for call in run['callMetrics'] if call['nodeId'] == 'repair-1-brief')
        self.assertEqual(repair['outcome'], 'error')
        first, latest = run['contributionHistory'][0], run['contributionHistory'][-1]
        self.assertEqual(first['summary']['repairCalls'], 0)
        self.assertEqual((latest['summary']['repairCalls'], latest['summary']['verifiedPoints']), (1, 0))
        self.assertEqual(latest['summary']['repairElapsedMs'], repair.get('elapsedMs'))
        self.assertEqual(first['records'][0]['state'], 'pending')
        self.assertFalse(first['records'][0]['canVerify'])
        self.assertTrue(all(record['points'] == 0 for record in latest['records']))
        event = next(e for e in session['events'] if e['seq'] == latest['eventSeq'])
        self.assertEqual((event['type'], event['runId']), ('error', run['id']))

    def test_repair_requires_manual_verification_and_old_run_keeps_its_evidence(self):
        session = self.repair_once()
        old_run = copy.deepcopy(session['runs'][-1])
        pending = build_contribution_ledger(old_run)
        self.assertEqual(len(pending['records']), 1)
        candidate = pending['records'][0]
        self.assertEqual((candidate['state'], candidate['points'], candidate['canVerify']), ('pending', 0, True))
        self.assertEqual(candidate['sources'][0]['nodeId'], 'integration')
        self.assertEqual([h['summary']['verifiedPoints'] for h in old_run['contributionHistory']], [0, 0])
        self.assertEqual(pending['summary']['repairCalls'], 1)
        self.assertNotEqual(candidate['beforeManifest'], candidate['afterManifest'])
        next_session = self.studio.add_message(self.pid, session['id'], '继续整理同一份资料。', session['version'], 'general')
        session = self.execute(next_session)
        newer_run = copy.deepcopy(session['runs'][-1])
        request = self.request(session, old_run, candidate)
        with self.assertRaises(Conflict):
            verify_contribution(self.studio, self.pid, session['id'], {**request, 'version': session['version'] - 1})
        with self.assertRaises(Conflict):
            verify_contribution(self.studio, self.pid, session['id'], {**request, 'evidenceFingerprint': 'outdated'})
        unchanged = self.studio.load(self.pid, session['id'])
        self.assertEqual(unchanged, session)
        for verdict, points, state in [('verified', 1, 'verified'), ('verified', 1, 'verified'), ('rejected', 0, 'rejected')]:
            request = {**request, 'version': session['version'], 'verdict': verdict}
            session = verify_contribution(self.studio, self.pid, session['id'], request)
            run = session['runs'][0]
            self.assertEqual(run['contributionHistory'][-1]['summary']['verifiedPoints'], points)
            self.assertEqual(run['contributionHistory'][-1]['records'][0]['state'], state)
            self.assertEqual(session['events'][-1]['runId'], old_run['id'])
            self.assertEqual(run['contributionValidations'][-1]['actor'], 'user')
            self.assertEqual(run['nodes'], old_run['nodes'])
            self.assertEqual(run['qualityHistory'], old_run['qualityHistory'])
            self.assertEqual(run['contributionHistory'][:2], old_run['contributionHistory'])
            self.assertEqual(session['runs'][-1], newer_run)
            metrics = self.studio.project(self.pid)['contributionMetrics']
            self.assertEqual((metrics['verifiedPoints'], metrics['humanVerifiedPoints']), (points, points))
        self.assertEqual(self.studio.project(self.pid)['contributionMetrics']['rejectedCount'], 1)

    def test_objective_improvement_uses_actual_artifacts_and_fixed_case(self):
        answer = case_definition()['reference']['result']
        wrong = copy.deepcopy(answer)
        wrong['remainingInventory']['A'] = 1
        before, after = json.dumps(wrong), json.dumps(answer)
        self.client.modules = [dict(module('result', 'data'), scope=['result.json'])]
        session = self.repair_once(before, after, 'result.json')
        run = session['runs'][-1]
        old_history = copy.deepcopy(run['contributionHistory'])
        record_objective_evaluations(self.studio, session, run, CASE_ID, 'fixed-order-grader')
        loaded = self.studio.load(self.pid, session['id'])['runs'][-1]
        evaluations = loaded['objectiveEvaluations']
        self.assertEqual([e['nodeId'] for e in evaluations], ['build-result', 'repair-1-result'])
        self.assertEqual([e['round'] for e in evaluations], [0, 1])
        for raw, evaluation in zip((before, after), evaluations):
            graded = evaluate_delivery([{'path': 'result.json', 'content': raw}])
            self.assertEqual([c['status'] for c in evaluation['criteria']], [c['status'] for c in graded['criteria']])
            self.assertEqual(evaluation['artifactManifest'][0]['sha256'], hashlib.sha256(raw.encode()).hexdigest())
        delivered = (self.store.current(self.pid)[1] / 'result.json').read_bytes()
        self.assertEqual(delivered, after.encode())
        self.assertEqual(evaluations[-1]['artifactManifest'], loaded['qualityHistory'][-1]['modules'][0]['artifactManifest'])
        ledger = loaded['contributionHistory'][-1]
        objective = [r for r in ledger['records'] if r['criterionSource'] == 'objective']
        self.assertEqual(len(objective), 1)
        self.assertEqual(objective[0]['criterionId'], 'result-objective-remainingInventory')
        self.assertEqual((objective[0]['points'], objective[0]['verification']['basis']), (1, 'objective_evaluation'))
        self.assertEqual(objective[0]['afterManifest'][0]['sha256'], hashlib.sha256(delivered).hexdigest())
        self.assertEqual(ledger['summary']['verifiedPoints'], 1)
        self.assertEqual(loaded['contributionHistory'][:2], old_history)
        metrics = self.studio.project(self.pid)['contributionMetrics']
        self.assertEqual((metrics['objectiveVerifiedPoints'], metrics['humanVerifiedPoints']), (1, 0))
        with zipfile.ZipFile(io.BytesIO(self.studio.export(self.pid, session['id']))) as archive:
            saved = json.loads(archive.read('会话与执行记录.json'))['runs'][-1]
        self.assertEqual(saved['objectiveEvaluations'], evaluations)
        self.assertEqual(saved['contributionHistory'], loaded['contributionHistory'])

    def test_objective_first_pass_never_awards_points_and_recording_is_idempotent(self):
        answer = json.dumps(case_definition()['reference']['result'])
        self.client.modules = [dict(module('result', 'data'), scope=['result.json'])]

        def respond(stage, result):
            if stage == 'build':
                result['files'] = [{'path': 'result.json', 'content': answer}]
            return result

        self.client.mutate = respond
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        record_objective_evaluations(self.studio, session, run, CASE_ID, 'fixed-order-grader')
        ledger = run['contributionHistory'][-1]
        self.assertEqual((ledger['summary']['verifiedPoints'], ledger['records']), (0, []))
        self.assertTrue(all(c['status'] == 'passed' for c in run['objectiveEvaluations'][0]['criteria']))
        saved = copy.deepcopy(session)
        record_objective_evaluations(self.studio, session, run, CASE_ID, 'fixed-order-grader')
        self.assertEqual(session, saved)


if __name__ == '__main__':
    unittest.main()
