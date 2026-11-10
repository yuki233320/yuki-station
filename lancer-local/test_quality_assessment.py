"""Host quality calculations against real file content; no provider calls."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from app import Store
from quality_assessment import artifact_manifest, build_assessment, reviewer_for, validate_review
from studio import Studio


class QualityAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.module = {'id': 'booking', 'title': '预约页面', 'workType': 'development',
                       'assignment': {'model': 'sol'}, 'reviewPolicy': {'required': True}}
        self.contract = {'id': 'run:booking', 'fingerprint': 'fixed-contract',
                         'criteria': [{'id': 'booking-c1', 'text': '预约容量检查'},
                                      {'id': 'booking-c2', 'text': '输入使用安全文本显示'}]}
        self.output = {'files': [{'path': 'booking.js', 'content': 'const capacity = 2;\nview.textContent = input;\n'}]}
        self.shared = {'qualityContract': self.contract, 'reviewFiles': self.output['files']}
        self.review = {'summary': '按源码核对，未运行浏览器', 'criteria': [
            {'criterionId': 'booking-c1', 'status': 'met', 'reason': '容量常量存在',
             'evidence': [{'path': 'booking.js', 'startLine': 1, 'endLine': 1}]},
            {'criterionId': 'booking-c2', 'status': 'met', 'reason': '使用文本属性',
             'evidence': [{'path': 'booking.js', 'startLine': 2, 'endLine': 2}]}], 'findings': []}
        self.checks = [{'name': '文件范围', 'pass': True}]

    def assess(self, review=None):
        return build_assessment(self.module, self.contract, self.output, [], self.checks, review)

    def test_reviewer_is_a_different_model_family_and_excludes_unverified_aliases(self):
        available = ['opus', 'native_codex', 'codex', 'image', 'sol', 'gemini', 'deepseek']
        self.assertEqual(reviewer_for(self.module, available), 'gemini')
        self.module['assignment']['model'] = 'gemini'
        self.assertEqual(reviewer_for(self.module, available), 'sol')
        self.module['assignment']['model'] = 'native_codex'
        self.assertEqual(reviewer_for(self.module, available), 'gemini')
        with self.assertRaisesRegex(ValueError, '不同系列'):
            reviewer_for(self.module, ['opus', 'native_codex', 'codex', 'image', 'sol'])

    def test_manifest_uses_actual_utf8_and_binary_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            studio = Studio(Store(Path(temporary), seed=False))
            project = studio.create_project('质量证据测试', '检查实际字节', '')
            session = {'projectId': project['id'], 'id': 'a' * 12}
            binary = studio.project_dir(project['id']) / session['id'] / 'images' / 'build-icon.png'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'\x89PNG\x00\xff')
            output = {'files': [{'path': '说明.md', 'content': '雪站\n'},
                                {'path': 'icon.png', 'binaryRef': 'images/build-icon.png'}]}
            records = artifact_manifest(studio, session, output)
            for record, actual in zip(records, ['雪站\n'.encode(), binary.read_bytes()]):
                self.assertEqual(record['size'], len(actual))
                self.assertEqual(record['sha256'], hashlib.sha256(actual).hexdigest())
            self.assertEqual([r['path'] for r in records], ['说明.md', 'icon.png'])

    def test_valid_review_is_bound_to_real_file_lines_and_all_criteria(self):
        self.assertIs(validate_review(self.review, self.shared), self.review)
        row = self.assess(self.review)
        self.assertTrue(row['gatePassed'])
        self.assertEqual(row['basis'], 'independent_artifact_review')
        self.assertEqual(row['metrics']['criterionCoveragePct'], 100)
        self.assertEqual(row['metrics']['criteriaMet'], 2)
        self.assertEqual(row['contractId'], 'run:booking')

    def test_missing_duplicate_or_unknown_criteria_are_rejected(self):
        for replacement in ([], [self.review['criteria'][0]] * 2,
                            [dict(self.review['criteria'][0], criterionId='invented'), self.review['criteria'][1]]):
            with self.subTest(criteria=replacement), self.assertRaisesRegex(ValueError, '固定验收标准编号'):
                validate_review(dict(self.review, criteria=replacement), self.shared)

    def test_evidence_cannot_invent_paths_lines_or_success(self):
        bad_refs = [[], [{'path': 'other.js', 'startLine': 1, 'endLine': 1}],
                    [{'path': 'booking.js', 'startLine': 0, 'endLine': 1}],
                    [{'path': 'booking.js', 'startLine': 1, 'endLine': 3}],
                    [{'path': 'booking.js', 'startLine': True, 'endLine': 1}],
                    [{'path': 'booking.js', 'startLine': 2, 'endLine': 1}]]
        for refs in bad_refs:
            review = copy.deepcopy(self.review)
            review['criteria'][0]['evidence'] = refs
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                validate_review(review, self.shared)
        with self.assertRaisesRegex(ValueError, '自定总分'):
            validate_review(dict(self.review, score=99), self.shared)

    def test_unverified_is_visible_and_blocks_required_review(self):
        self.review['criteria'][0].update(status='unverified', reason='没有运行证据', evidence=[])
        validate_review(self.review, self.shared)
        row = self.assess(self.review)
        self.assertFalse(row['gatePassed'])
        self.assertEqual(row['metrics']['criteriaUnverified'], 1)
        self.assertEqual(row['metrics']['criterionCoveragePct'], 50)
        no_review = self.assess()
        self.assertFalse(no_review['gatePassed'])
        self.assertEqual(no_review['metrics']['criteriaUnverified'], 2)
        self.assertEqual(no_review['basis'], 'coordinator_pending')

    def test_project_context_file_is_not_artifact_review_evidence(self):
        self.shared['sourceFiles'] = [{'path': 'inputs/example.json', 'text': '{}',
                                      'startLine': 1, 'endLine': 1}]
        project_ref = {'path': 'inputs/example.json', 'startLine': 1, 'endLine': 1}
        original = self.review['criteria'][0]['evidence']
        for evidence in ([project_ref], [*original, project_ref]):
            self.review['criteria'][0]['evidence'] = evidence
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, '不存在的文件或无效行范围'):
                validate_review(self.review, self.shared)

    def test_blocking_findings_and_failed_checks_cannot_be_averaged_away(self):
        finding = {'priority': 'P1', 'claim': '容量常量未参与判断', 'proposal': '提交前比较容量',
                   'evidence': [{'path': 'booking.js', 'startLine': 1, 'endLine': 1}]}
        self.review['findings'] = [finding]
        validate_review(self.review, self.shared)
        row = self.assess(self.review)
        self.assertFalse(row['gatePassed'])
        self.assertEqual(row['metrics']['blockingFindings'], 1)
        self.assertEqual(row['metrics']['criterionCoveragePct'], 100)
        finding['priority'] = 'P2'
        self.assertTrue(self.assess(self.review)['gatePassed'])
        self.checks.append({'name': 'JSON 语法', 'pass': False})
        row = self.assess(self.review)
        self.assertFalse(row['gatePassed'])
        self.assertEqual(row['metrics']['checksPassed'], 1)
        self.assertEqual(row['metrics']['checksTotal'], 2)

    def test_ordinary_module_waits_for_coordinator_without_fabricating_review(self):
        self.module.update(workType='writing', reviewPolicy={'required': False})
        row = self.assess()
        self.assertTrue(row['gatePassed'])
        self.assertIsNone(row['independentReview'])
        self.assertEqual(row['basis'], 'coordinator_pending')
        self.assertEqual(row['metrics']['criteriaMet'], 0)
        self.checks[0]['pass'] = False
        self.assertFalse(self.assess()['gatePassed'])


if __name__ == '__main__':
    unittest.main()
