"""Run/attempt crash boundaries without model calls or external actions."""
import copy
import unittest

from execution_attempts import begin_attempt, finish_attempt, interrupt_attempts


BUILD = {'schema': 'yuki-runtime-build/v1', 'fingerprint': 'build-1',
         'files': {'studio.py': 'hash'}, 'capturedAt': 'startup'}


class ExecutionAttemptTests(unittest.TestCase):
    def begin(self, run, reason='start', attempt_id='one', now='t1'):
        return begin_attempt(run, reason, now, attempt_id, BUILD)

    def test_resume_appends_history_without_replacing_business_evidence(self):
        run = {'id': 'run', 'status': 'running', 'nodes': [{'id': 'n', 'status': 'done'}],
               'commitIntent': {'fingerprint': 'delivery'}, 'error': 'old failure'}
        first = self.begin(run)
        run.update(status='failed', ended='t2')
        finish_attempt(run, 't3')
        saved_first = copy.deepcopy(first)
        run['status'] = 'running'
        second = self.begin(run, 'resume', 'two', 't4')
        self.assertEqual(first, saved_first)
        self.assertEqual(second['number'], 2)
        self.assertEqual(second['reason'], 'resume')
        self.assertEqual(run['executionAttemptsHistory'], 'complete')
        self.assertNotIn('ended', run)
        self.assertEqual(run['nodes'], [{'id': 'n', 'status': 'done'}])
        self.assertEqual(run['commitIntent'], {'fingerprint': 'delivery'})
        self.assertEqual(run['error'], 'old failure')

    def test_legacy_resume_marks_history_unknown_without_inventing_attempts(self):
        run = {'status': 'interrupted', 'ended': 'old'}
        self.begin(run, 'resume')
        self.assertEqual(run['executionAttemptsHistory'], 'legacy_unknown')
        self.assertEqual(len(run['executionAttempts']), 1)
        finish_attempt(run, 't2')
        self.begin(run, 'resume', 'two')
        self.assertEqual(run['executionAttemptsHistory'], 'legacy_unknown')

    def test_rejected_start_is_atomic_and_keeps_evidence(self):
        run = {'status': 'running'}
        self.begin(run)
        before = copy.deepcopy(run)
        for reason, attempt_id in [('resume', 'two'), ('automatic', 'three')]:
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.begin(run, reason, attempt_id)
            self.assertEqual(run, before)
        run['status'] = 'failed'
        finish_attempt(run, 't2')
        before = copy.deepcopy(run)
        with self.assertRaises(ValueError):
            self.begin(run, 'resume', 'one')
        self.assertEqual(run, before)

    def test_evidence_review_preserves_legacy_history_and_previous_attempts(self):
        legacy = {'status': 'completed', 'ended': 'old', 'mergedRevision': 'known'}
        review = self.begin(legacy, 'evidence_review')
        self.assertEqual(review['reason'], 'evidence_review')
        self.assertEqual(legacy['executionAttemptsHistory'], 'legacy_unknown')
        self.assertEqual(legacy['mergedRevision'], 'known')
        finish_attempt(legacy, 't2')
        first = copy.deepcopy(review)
        next_review = self.begin(legacy, 'evidence_review', 'two', 't3')
        self.assertEqual(review, first)
        self.assertEqual(next_review['number'], 2)
        self.assertEqual(legacy['executionAttemptsHistory'], 'legacy_unknown')

    def test_each_existing_terminal_status_settles_and_is_immutable(self):
        for status in ('completed', 'failed', 'stopped', 'conflict', 'interrupted'):
            with self.subTest(status=status):
                run = {'status': 'running'}
                self.begin(run)
                run.update(status=status, ended='actual-end')
                self.assertEqual(finish_attempt(run, 'later')['endedAt'], 'actual-end')
                before = copy.deepcopy(run['executionAttempts'])
                run.update(status='completed', ended='different')
                self.assertIsNone(finish_attempt(run, 'later-again'))
                self.assertEqual(interrupt_attempts(run, 'restart', 'host_restarted'), [])
                self.assertEqual(run['executionAttempts'], before)

    def test_running_run_cannot_be_finalized_as_success(self):
        run = {'status': 'running'}
        self.begin(run)
        before = copy.deepcopy(run)
        self.assertIsNone(finish_attempt(run, 't2'))
        self.assertEqual(run, before)

    def test_restart_records_interruption_but_never_changes_run_or_receipts(self):
        run = {'status': 'running', 'nodes': [{'receipt': {'status': 'unknown'}}]}
        self.begin(run)
        changed = interrupt_attempts(run, 't2', 'host_restarted')
        self.assertEqual(changed[0]['status'], 'interrupted')
        self.assertEqual(changed[0]['endReason'], 'host_restarted')
        self.assertEqual(run['status'], 'running')
        self.assertEqual(run['nodes'], [{'receipt': {'status': 'unknown'}}])
        self.assertEqual(interrupt_attempts(run, 't3', 'host_restarted'), [])

    def test_crash_after_terminal_run_save_preserves_known_result(self):
        run = {'status': 'running'}
        self.begin(run)
        run.update(status='completed', ended='t2')
        attempt = interrupt_attempts(run, 't3', 'host_restarted')[0]
        self.assertEqual(attempt['status'], 'completed')
        self.assertEqual(attempt['endedAt'], 't2')
        self.assertNotIn('endReason', attempt)

    def test_legacy_recovery_does_not_fabricate_history(self):
        run = {'status': 'running', 'nodes': []}
        before = copy.deepcopy(run)
        self.assertEqual(interrupt_attempts(run, 't2', 'host_restarted'), [])
        self.assertIsNone(finish_attempt(run, 't2'))
        self.assertEqual(run, before)

    def test_build_reference_excludes_files_and_does_not_alias_caller(self):
        build = copy.deepcopy(BUILD)
        build['extra'] = {'revision': 1}
        run = {'status': 'running'}
        attempt = begin_attempt(run, 'start', 't1', 'one', build)
        build['extra']['revision'] = 2
        self.assertNotIn('files', attempt['runtimeBuild'])
        self.assertEqual(attempt['runtimeBuild']['fingerprint'], 'build-1')
        self.assertEqual(attempt['runtimeBuild']['extra']['revision'], 1)

    def test_invalid_history_is_not_silently_replaced(self):
        for history in ({'one': 'running'}, [None], [{'status': 'unknown'}]):
            run = {'status': 'running', 'executionAttempts': history}
            before = copy.deepcopy(run)
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.begin(run)
            self.assertEqual(run, before)


if __name__ == '__main__':
    unittest.main()
