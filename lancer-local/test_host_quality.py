"""Host evidence attribution, stale-cache rejection, and fail-closed persistence."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from host_quality import (execute_round, validate_plan_checks, evidence_files, evidence_paths,
                          module_checks, summaries, bind_review, verify_delivery, history_fingerprint)
from live import Cancelled


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def check_spec():
    return {'id': 'accept-code', 'name': '固定业务检查', 'files': ['src/code.py'],
            'criteria': [{'id': 'case-1', 'description': '返回业务期望'}],
            'scriptSha256': 'a' * 64, 'timeoutSeconds': 5}


class FakeStudio:
    def __init__(self, root):
        self.root, self.lock, self.saves = root, threading.RLock(), []
        self.fail_save = False

    def project_dir(self, pid):
        path = self.root / pid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, session):
        if self.fail_save:
            raise OSError('save failed')
        self.saves.append(copy.deepcopy(session))

    def event(self, session, kind, detail, **extra):
        session.setdefault('events', []).append({'type': kind, 'detail': detail, **extra})


class HostQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = FakeStudio(Path(self.tmp.name))
        self.run = {'id': 'run-one', 'hostAcceptanceChecks': [check_spec()]}
        self.session = {'projectId': 'project', 'id': 'session', 'runs': [self.run]}
        self.plan = {'modules': [{'id': 'code', 'scope': ['src/']}, {'id': 'guide', 'scope': ['guide.md']}]}
        self.files = {'src/code.py': b'result = 42\n', 'guide.md': b'Guide'}
        self.complete = {'code': {'files': [{'path': 'src/code.py', 'content': 'result = 42\n'}]},
                         'guide': {'files': [{'path': 'guide.md', 'content': 'Guide'}]}}

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self, files=None, round_no=0, status='passed'):
        files = self.files if files is None else files
        manifest = [{'path': path, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                    for path, data in sorted(files.items())]
        receipt = {'id': 'accept-code', 'round': round_no, 'status': status, 'passed': status == 'passed',
                   'executed': True, 'exitCode': 0, 'scriptSha256': 'a' * 64, 'artifactManifest': manifest,
                   'artifactFingerprint': fingerprint(manifest), 'bindingFingerprint': 'binding',
                   'receiptRef': 'host/round-%d/receipt.json' % round_no,
                   'criteria': [{'criterionId': 'case-1', 'description': '返回业务期望',
                                 'status': 'passed' if status == 'passed' else 'failed'}]}
        path = self.studio.project_dir('project') / 'session' / receipt['receiptRef']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf8')
        return receipt

    def execute(self, receipt, round_no=0, cancel=None):
        runner = types.SimpleNamespace(run_check=lambda *args: copy.deepcopy(receipt))
        with patch.dict('sys.modules', {'host_acceptance': runner}):
            return execute_round(self.studio, self.session, self.run, self.plan, self.complete,
                                 self.files, round_no, cancel or threading.Event())

    def test_scope_mapping_is_unique_and_missing_outputs_remain_owned(self):
        self.assertEqual(validate_plan_checks(self.plan, self.run['hostAcceptanceChecks']), {'accept-code': ['code']})
        with self.assertRaises(ValueError):
            validate_plan_checks({'modules': [{'id': 'other', 'scope': ['elsewhere/']}]}, self.run['hostAcceptanceChecks'])
        with self.assertRaises(ValueError):
            validate_plan_checks({'modules': [*self.plan['modules'], {'id': 'other', 'scope': ['src/']}]}, self.run['hostAcceptanceChecks'])
        del self.files['src/code.py']
        row = self.receipt(status='failed')
        row['executed'] = False
        path = self.studio.project_dir('project') / 'session' / row['receiptRef']
        path.write_text(json.dumps(row), encoding='utf8')
        self.execute(row)
        checks = module_checks(self.run, 0, 'code')
        self.assertFalse(checks[0]['pass'])
        self.assertEqual(checks[0]['repairAction'], 'repair')
        self.assertEqual(module_checks(self.run, 0, 'guide'), [])

    def test_reports_are_scoped_read_only_evidence_not_delivery_artifacts(self):
        self.execute(self.receipt())
        reports = evidence_files(self.run, 0, 'code')
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0]['content'])
        self.assertEqual(report['receipt']['artifactManifest'], self.run['hostAcceptanceHistory'][0]['artifactManifest'])
        self.assertTrue(report['passed'])
        self.assertEqual(evidence_files(self.run, 0, 'guide'), [])
        self.assertEqual(evidence_paths(self.run, 0, self.plan, self.complete)['code'], [reports[0]['path']])
        self.assertNotIn(reports[0]['path'], self.files)
        self.assertTrue(summaries(self.run, 0, 'code')[0]['receiptSha256'])

    def test_stale_receipt_or_unknown_runner_cannot_pass_the_gate(self):
        old = self.receipt()
        self.files['src/code.py'] = b'result = 0\n'
        self.execute(old)
        check = module_checks(self.run, 0, 'code')[0]
        self.assertFalse(check['pass'])
        self.assertEqual(check['status'], 'unknown')
        self.assertEqual(check['repairAction'], 'needs_input')
        self.execute({**old, 'status': 'unknown', 'passed': False})
        self.assertEqual(module_checks(self.run, 0, 'code')[0]['repairAction'], 'needs_input')

    def test_passed_label_requires_actual_execution_and_integer_zero_exit(self):
        for fields in ({'executed': False}, {'exitCode': 1}, {'exitCode': False}, {'exitCode': None}):
            with self.subTest(fields=fields):
                receipt = {**self.receipt(), **fields}
                path = self.studio.project_dir('project') / 'session' / receipt['receiptRef']
                path.write_text(json.dumps(receipt), encoding='utf8')
                self.execute(receipt)
                self.assertFalse(module_checks(self.run, 0, 'code')[0]['pass'])
                self.assertEqual(module_checks(self.run, 0, 'code')[0]['repairAction'], 'needs_input')

    def test_model_report_limits_details_but_retains_raw_receipt(self):
        receipt = self.receipt()
        receipt['criteria'][0]['detail'] = 'x' * 20000
        path = self.studio.project_dir('project') / 'session' / receipt['receiptRef']
        path.write_text(json.dumps(receipt), encoding='utf8')
        self.execute(receipt)
        summary = summaries(self.run, 0)[0]
        self.assertEqual(len(summary['criteria'][0]['detail']), 600)
        self.assertTrue(summary['criteria'][0]['detailTruncated'])
        report = json.loads(evidence_files(self.run, 0)[0]['content'])
        self.assertTrue(report['receipt']['criteria'][0]['detailTruncated'])
        self.assertEqual(json.loads(path.read_text())['criteria'][0]['detail'], 'x' * 20000)
        self.assertEqual(self.session['events'][-1]['hostAcceptanceFingerprint'], history_fingerprint(self.run, 0))
        receipt['criteria'][0].update(detail='already shortened by runner', detailTruncated=True)
        path.write_text(json.dumps(receipt), encoding='utf8')
        self.execute(receipt)
        self.assertTrue(summaries(self.run, 0)[0]['criteria'][0]['detailTruncated'])

    def test_failed_criteria_are_retained_in_bounded_repair_feedback(self):
        receipt = self.receipt(status='failed')
        self.run['hostAcceptanceChecks'][0]['criteria'] = [
            {'id': 'case-' + str(i), 'description': '登记断言 ' + str(i)} for i in range(8)]
        receipt['criteria'] = [
            {'criterionId': 'case-' + str(i), 'description': '登记断言 ' + str(i), 'status': 'failed',
             'detail': 'Candidate must contain GOOD ' + 'x' * 1000, 'detailTruncated': True}
            for i in range(8)]
        # Missing detailed output falls back to the registered description.
        receipt['criteria'][0].pop('detail')
        path = self.studio.project_dir('project') / 'session' / receipt['receiptRef']
        path.write_text(json.dumps(receipt), encoding='utf8')
        self.execute(receipt)
        check = module_checks(self.run, 0, 'code')[0]
        self.assertTrue(check['executed'])
        self.assertEqual(check['repairAction'], 'repair')
        self.assertIn('case-0：登记断言 0', check['detail'])
        self.assertIn('case-1：Candidate must contain GOOD', check['detail'])
        self.assertLessEqual(len(check['detail']), 3000)
        self.assertTrue(check['detailTruncated'])
        self.assertEqual(len(check['criteria']), 8)
        self.assertTrue(check['criteria'][1]['detailTruncated'])
        self.assertLessEqual(len(check['criteria'][1]['detail']), 600)

    def test_cancellation_and_failed_save_do_not_publish_a_verified_shortcut(self):
        stop = threading.Event(); stop.set()
        with self.assertRaises(Cancelled):
            self.execute(self.receipt(), cancel=stop)
        self.assertNotIn('hostAcceptanceHistory', self.run)
        receipt = self.receipt()
        self.studio.fail_save = True
        with self.assertRaisesRegex(OSError, 'save failed'):
            self.execute(receipt)
        self.studio.fail_save = False
        self.execute(receipt)
        self.assertEqual(len(self.run['hostAcceptanceHistory']), 1)
        self.assertEqual(len(self.studio.saves), 1)

    def test_cached_model_review_is_bound_to_artifacts_and_host_receipt(self):
        self.execute(self.receipt())
        node = {'id': 'verify', 'status': 'waiting'}
        reports = evidence_files(self.run, 0)
        bind_review(self.studio, self.session, self.run, node, 0, reports)
        node['status'] = 'done'
        bind_review(self.studio, self.session, self.run, node, 0, reports)
        with self.assertRaisesRegex(ValueError, '缓存'):
            bind_review(self.studio, self.session, self.run, node, 0, reports + [{'path': 'extra', 'content': 'changed'}])
        del node['hostEvidenceBinding']
        with self.assertRaisesRegex(ValueError, '缓存'):
            bind_review(self.studio, self.session, self.run, node, 0, reports)

    def test_precommit_verifies_files_and_raw_receipts_without_running_checker(self):
        receipt = self.receipt()
        self.execute(receipt)
        self.run['qualityHistory'] = [{'round': 0, 'passed': True,
                                      'hostAcceptanceFingerprint': history_fingerprint(self.run, 0)}]
        with patch.dict('sys.modules', {'host_acceptance': types.SimpleNamespace(
                run_check=lambda *args: self.fail('commit must not execute host checker'),
                verify_receipt=lambda *args: True)}):
            verify_delivery(self.studio, self.session, self.run, self.files)
            self.run['qualityHistory'].append({'round': 1, 'passed': False})
            with self.assertRaises(ValueError):
                verify_delivery(self.studio, self.session, self.run, self.files)
            self.run['qualityHistory'].pop()
            with self.assertRaises(ValueError):
                verify_delivery(self.studio, self.session, self.run, {**self.files, 'src/code.py': b'changed'})
            raw = self.studio.project_dir('project') / 'session' / receipt['receiptRef']
            raw.write_text('{}', encoding='utf8')
            with self.assertRaises(ValueError):
                verify_delivery(self.studio, self.session, self.run, self.files)

    def test_legacy_run_is_unchanged_and_does_not_need_runner(self):
        legacy = {'id': 'old'}
        self.assertIsNone(execute_round(self.studio, self.session, legacy, self.plan, {}, {}, 0, threading.Event()))
        self.assertEqual(evidence_files(legacy, 0), [])
        self.assertEqual(module_checks(legacy, 0, 'code'), [])
        verify_delivery(self.studio, self.session, legacy, {})
        self.assertEqual(legacy, {'id': 'old'})


if __name__ == '__main__':
    unittest.main()
