"""Aggregates must use the authoritative session reader, not import originals."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from app import Store
from contribution_tracking import project_contribution_metrics
from quality_telemetry import project_metrics
from studio import Studio


class SessionConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(Store(Path(self.tmp.name), seed=False))
        self.pid = self.studio.create_project('权威会话', 'SQLite记录用于统计')['id']
        self.session = self.studio.create_session(self.pid, 'actual session', '真实观测', 'general')
        self.session['runs'] = [{
            'id': 'actual-run', 'mode': 'live', 'workflowVersion': 2,
            'qualityVersion': 1, 'status': 'completed', 'mergedRevision': 'revision', 'nodes': [],
            'qualityHistory': [{'round': 0, 'modules': [{
                'moduleId': 'brief', 'model': 'sol', 'workType': 'writing', 'gatePassed': True}]}],
            'contributionHistory': [{'summary': {'verifiedPoints': 1, 'pendingCount': 0, 'rejectedCount': 0},
                'records': [{'points': 1, 'verification': {'basis': 'human_review'}}]}],
        }]
        self.studio.save(self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def test_metrics_ignore_stale_import_original_and_do_not_change_database_state(self):
        before = copy.deepcopy(self.studio.load(self.pid, self.session['id']))
        original = self.studio.path(self.pid, self.session['id'])
        original.write_text(json.dumps(dict(before, runs=[])))
        raw = original.read_bytes()
        quality = project_metrics(self.studio, self.pid)
        contribution = project_contribution_metrics(self.studio, self.pid)
        self.assertEqual(quality['rows'][0]['qualitySamples'], 1)
        self.assertEqual(quality['rows'][0]['mergedCount'], 1)
        self.assertEqual(contribution['runCount'], 1)
        self.assertEqual(contribution['humanVerifiedPoints'], 1)
        self.assertEqual(self.studio.load(self.pid, self.session['id']), before)
        self.assertEqual(original.read_bytes(), raw)

    def test_new_sessions_are_visible_without_json_files_and_project_scope_is_kept(self):
        self.assertFalse(self.studio.path(self.pid, self.session['id']).exists())
        other_pid = self.studio.create_project('different project', 'outside selected project')['id']
        other = self.studio.create_session(other_pid, 'other', 'outside', 'general')
        other['runs'] = copy.deepcopy(self.session['runs'])
        self.studio.save(other)
        self.assertEqual(len(self.studio.list_sessions(self.pid)), 1)
        self.assertEqual(project_metrics(self.studio, self.pid)['rows'][0]['qualitySamples'], 1)
        self.assertEqual(project_contribution_metrics(self.studio, self.pid)['runCount'], 1)


if __name__ == '__main__':
    unittest.main()
