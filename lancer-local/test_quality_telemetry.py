"""Aggregate persisted Studio records without simulating the implementation."""
import tempfile
import unittest
from pathlib import Path

from app import Store
from quality_telemetry import project_metrics
from studio import Studio


def quality_row(mid, passed, model='sol', work='development'):
    return {'moduleId': mid, 'model': model, 'workType': work, 'gatePassed': passed}


def run_record(rid, history=None, **extra):
    return {'id': rid, 'mode': 'live', 'qualityVersion': 1, 'status': 'completed',
            'nodes': [], 'qualityHistory': history or [], **extra}


class QualityTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(Store(Path(self.tmp.name), seed=False))
        self.pid = self.studio.create_project('观测测试', '记录实际结果', '')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def save_session(self, number, runs):
        sid = format(number, '012x')
        self.studio.save({'id': sid, 'projectId': self.pid, 'runs': runs})
        return sid

    def test_module_samples_and_independent_sessions_have_different_denominators(self):
        self.save_session(1, [run_record('a', [
            {'round': 0, 'modules': [quality_row('web', False), quality_row('api', True)]},
            {'round': 1, 'modules': [quality_row('web', True), quality_row('api', True)]}],
            mergedRevision='real-revision', nodes=[
                {'id': 'repair-1-web', 'moduleId': 'web', 'repairRound': 1, 'started': '2026-10-31T10:00:00Z'},
                {'id': 'repair-2-web', 'moduleId': 'web', 'repairRound': 2, 'status': 'waiting'}]),
            run_record('b', [{'round': 0, 'modules': [quality_row('web', True)]}], mergedRevision='next-revision')])
        row = project_metrics(self.studio, self.pid)['rows'][0]
        self.assertEqual(row['qualitySamples'], 3)
        self.assertEqual(row['independentSessions'], 1)
        self.assertEqual(row['firstPassCount'], 2)
        self.assertEqual(row['finalPassCount'], 3)
        self.assertEqual(row['mergedCount'], 3)
        self.assertEqual(row['repairRounds'], 1)
        self.assertEqual(row['firstPassRatePct'], 66.7)
        self.assertEqual(row['finalPassRatePct'], 100)
        self.assertTrue(row['insufficientSamples'])

    def test_three_independent_sessions_are_required_for_sufficient_samples(self):
        for number in range(1, 4):
            self.save_session(number, [run_record(str(number), [
                {'round': 0, 'modules': [quality_row('web', True)]}], mergedRevision='revision')])
        row = project_metrics(self.studio, self.pid)['rows'][0]
        self.assertEqual(row['independentSessions'], 3)
        self.assertFalse(row['insufficientSamples'])

    def test_live_quality_failure_is_counted_but_not_claimed_as_merged(self):
        self.save_session(1, [run_record('a', [
            {'round': 0, 'modules': [quality_row('web', False)]},
            {'round': 2, 'modules': [quality_row('web', False)]}], status='failed', nodes=[
                {'id': 'repair-1-web', 'moduleId': 'web', 'repairRound': 1, 'started': '2026-10-31T10:00:00Z'},
                {'id': 'repair-2-web', 'moduleId': 'web', 'repairRound': 2, 'started': '2026-10-31T10:01:00Z'}])])
        row = project_metrics(self.studio, self.pid)['rows'][0]
        self.assertEqual(row['qualitySamples'], 1)
        self.assertEqual((row['firstPassCount'], row['finalPassCount'], row['mergedCount']), (0, 0, 0))
        self.assertEqual(row['repairRounds'], 2)
        self.assertEqual(row['mergedRatePct'], 0)

    def test_requests_without_quality_history_survive_and_adapters_stay_separate(self):
        calls = [{'id': '1', 'model': 'sol', 'adapter': 'api', 'outcome': 'error', 'elapsedMs': 1200},
                 {'id': '2', 'model': 'sol', 'adapter': 'api', 'outcome': 'returned', 'elapsedMs': 800},
                 {'id': '3', 'model': 'sol', 'adapter': 'api', 'outcome': 'cancelled', 'elapsedMs': 400},
                 {'id': '4', 'model': 'sol', 'adapter': 'native', 'outcome': 'returned', 'elapsedMs': 9200}]
        self.save_session(1, [run_record('a', status='failed', callMetrics=calls)])
        metrics = project_metrics(self.studio, self.pid)
        self.assertEqual(metrics['rows'], [])
        api, native = metrics['requests']
        self.assertEqual(api['adapter'], 'api')
        self.assertEqual((api['requestCount'], api['errorCount'], api['cancelledCount']), (3, 1, 1))
        self.assertEqual(api['averageElapsedMs'], 800)
        self.assertEqual(api['errorRatePct'], 50)
        self.assertEqual(native['adapter'], 'native')
        self.assertEqual(native['averageElapsedMs'], 9200)
        self.assertEqual(native['errorRatePct'], 0)

    def test_demo_and_old_runs_are_excluded_and_unknown_latency_stays_unknown(self):
        history = [{'round': 0, 'modules': [quality_row('web', True)]}]
        call = {'id': '1', 'model': 'gemini', 'adapter': 'api', 'outcome': 'returned'}
        self.save_session(1, [run_record('demo', history, mode='demo', callMetrics=[call]),
                              run_record('legacy', history, qualityVersion=0, callMetrics=[call]),
                              run_record('actual', callMetrics=[call], started='2026-10-31T10:00:00Z', ended='2026-10-31T10:05:00Z')])
        metrics = project_metrics(self.studio, self.pid)
        self.assertEqual(metrics['rows'], [])
        self.assertEqual(metrics['requests'][0]['requestCount'], 1)
        self.assertEqual(metrics['requests'][0]['latencySamples'], 0)
        self.assertIsNone(metrics['requests'][0]['averageElapsedMs'])

    def test_work_types_are_separate_and_reading_does_not_rewrite_sessions(self):
        sid = self.save_session(1, [run_record('a', [
            {'round': 0, 'modules': [quality_row('web', True), quality_row('guide', False, work='writing')]}])])
        path = self.studio.path(self.pid, sid)
        before = self.studio.load(self.pid, sid)
        metrics = project_metrics(self.studio, self.pid)
        self.assertEqual([row['workType'] for row in metrics['rows']], ['development', 'writing'])
        self.assertEqual(self.studio.load(self.pid, sid), before)
        self.assertEqual(metrics['schema'], 'yuki-quality-telemetry/v1')

    def test_project_exposes_separate_role_cooperation_without_creating_samples(self):
        project = self.studio.project(self.pid)
        self.assertEqual(project['roleCooperation']['rows'], [])
        self.assertEqual(project['roleCooperation']['interaction']['status'], 'missing_matched_ablations')
        self.assertNotIn('roleCooperation', project['qualityMetrics'])


if __name__ == '__main__':
    unittest.main()
