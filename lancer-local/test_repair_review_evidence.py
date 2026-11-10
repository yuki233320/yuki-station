"""Read-only follow-up reviews retain their own identity and original host round."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import Store
from studio import Studio
from test_review_evidence import ReviewClient
from review_evidence import binding_for, fingerprint, verify_evidence, review_version
from quality_workflow import verify_review_delivery


class RepairReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yuki-repair-review-proof-')
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = ReviewClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('复审身份隔离验证', 'fixture only')['id']
        self.session = self.studio.create_session(self.pid, '复审绑定', '按固定契约交付', 'general')
        self.no_network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network'))
        self.no_network.start()

    def tearDown(self):
        for event in self.studio.cancels.values():
            event.set()
        for thread in self.studio.threads.values():
            thread.join(10)
        self.no_network.stop()
        self.tmp.cleanup()

    def complete(self, followup=False):
        remaining = [1 if followup else 0]
        def mutate(stage, result):
            if stage == 'quality' and remaining[0]:
                remaining[0] -= 1
                for criterion in result['criteria']:
                    criterion.update(status='unverified', reason='需要核对已有证据，没有确定代码缺陷', evidence=[])
            return result
        self.client.mutate = mutate
        self.studio.start(self.pid, self.session['id'], coordinator_model='opus', team_models=['opus', 'sol', 'gemini'])
        self.studio.threads[self.session['id']].join(15)
        self.assertFalse(self.studio.threads[self.session['id']].is_alive())
        self.session = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(self.session['status'], 'completed', self.session.get('error'))
        run = self.session['runs'][-1]
        return run, run['qualityHistory'][-1]['modules'][0]

    def verify_row(self, run, row):
        return verify_evidence(self.studio, self.session, run, run['plan']['modules'][0],
                               run['taskContracts']['code'], row['independentReview'],
                               row['reviewEvidence'], row['artifactManifest'])

    def test_new_normal_review_has_explicit_null_attempt_and_v2_binding(self):
        run, row = self.complete()
        binding = row['reviewEvidence']['binding']
        self.assertEqual(binding['schema'], 'yuki-artifact-review-binding/v2')
        self.assertEqual(binding['repairRoutingVersion'], 1)
        self.assertIn('reviewAttemptId', binding)
        self.assertIsNone(binding['reviewAttemptId'])
        self.assertEqual(row['reviewNodeId'], 'verify-0-code')
        self.verify_row(run, row)

    def test_followup_uses_same_artifact_host_round_but_separate_context_and_node(self):
        run, row = self.complete(followup=True)
        first = run['qualityHistory'][0]['modules'][0]
        self.assertEqual(len([c for c in self.client.calls if c['key'][0] == 'build']), 1)
        self.assertEqual([h['round'] for h in run['qualityHistory']], [0, 0])
        self.assertFalse(run['qualityHistory'][0]['passed'])
        self.assertEqual(first['artifactManifest'], row['artifactManifest'])
        self.assertEqual(row['reviewNodeId'], 'verify-repair-review-r0-code')
        self.assertNotEqual(row['reviewEvidence']['contextRef'], first['reviewEvidence']['contextRef'])
        self.assertEqual(row['reviewEvidence']['binding']['qualityRound'], 0)
        self.assertEqual(row['reviewEvidence']['binding']['reviewAttemptId'], 'repair-review-r0')
        self.verify_row(run, first)
        self.verify_row(run, row)

    def test_deleting_or_changing_source_route_blocks_cached_followup(self):
        run, row = self.complete(followup=True)
        changed = copy.deepcopy(run)
        changed['qualityHistory'] = changed['qualityHistory'][1:]
        with self.assertRaises(ValueError):
            self.verify_row(changed, row)
        changed = copy.deepcopy(run)
        source = changed['qualityHistory'][0]
        source['modules'][0]['criteria'][0]['reason'] += ' different source rationale'
        from repair_routing import record_fingerprint
        source['repairRoutingFingerprint'] = record_fingerprint(source)
        with self.assertRaises(ValueError):
            self.verify_row(changed, row)
        changed = copy.deepcopy(run)
        changed['qualityHistory'][0]['modules'][0]['repairRoute']['action'] = 'repair'
        with self.assertRaises(ValueError):
            self.verify_row(changed, row)

    def test_followup_binding_cannot_be_downgraded_to_v1_or_renumbered(self):
        run, row = self.complete(followup=True)
        for mutation in ('schema', 'missing_marker', 'attempt'):
            changed = copy.deepcopy(row)
            binding = changed['reviewEvidence']['binding']
            if mutation == 'schema':
                binding['schema'] = 'yuki-artifact-review-binding/v1'
            elif mutation == 'missing_marker':
                binding.pop('reviewAttemptId')
            else:
                binding['reviewAttemptId'] = 'repair-review-r1'
            binding['fingerprint'] = fingerprint({k: v for k, v in binding.items() if k != 'fingerprint'})
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.verify_row(run, changed)

    def test_followup_context_must_identify_same_attempt_even_if_raw_sha_is_updated(self):
        run, row = self.complete(followup=True)
        changed = copy.deepcopy(row)
        evidence = changed['reviewEvidence']
        path = self.studio.project_dir(self.pid) / self.session['id'] / 'contexts' / (evidence['contextRef'] + '.json')
        context = json.loads(path.read_text())
        context['memory']['sharedEvidence']['qualityReviewId'] = None
        raw = json.dumps(context, ensure_ascii=False).encode()
        path.write_bytes(raw)
        evidence['contextSha256'] = hashlib.sha256(raw).hexdigest()
        with self.assertRaises(ValueError):
            self.verify_row(run, changed)

    def test_first_publication_checks_followup_originals(self):
        run, row = self.complete(followup=True)
        files = {item['path']: (self.store.current(self.pid)[1] / item['path']).read_bytes() for item in run['artifacts']}
        verify_review_delivery(self.studio, self.session, run, files)
        path = self.studio.project_dir(self.pid) / self.session['id'] / 'responses' / (row['reviewEvidence']['responseRef'] + '.json')
        path.write_text('{}')
        with self.assertRaises(ValueError):
            verify_review_delivery(self.studio, self.session, run, files)

    def test_changed_host_stdout_blocks_followup_before_new_model_or_test(self):
        script = Path(self.tmp.name) / 'host.py'
        script.write_text("import json\nprint(json.dumps({'accepted':True,'criteria':[{'criterionId':'H1','status':'passed','detail':'fixed fixture check'}]}))\n")
        entry = self.studio.host_acceptance.register({'name': 'frozen check', 'scriptPath': str(script),
            'files': ['code.md'], 'criteria': [{'id': 'H1', 'description': 'fixed fixture check'}]})
        self.studio.set_host_acceptance(self.pid, self.studio.meta(self.pid)['version'], [entry['id']])
        def mutate(stage, result):
            if stage == 'quality':
                result['criteria'][0].update(status='unverified', reason='查看现有证据', evidence=[])
            return result
        self.client.mutate = mutate
        original = self.studio.ask
        def ask(*args, **kwargs):
            result = original(*args, **kwargs)
            session, run, node = args[:3]
            if node['id'] == 'integration':
                receipt = run['hostAcceptanceHistory'][0]['checks'][0]['receipt']
                path = self.studio.project_dir(self.pid) / session['id'] / receipt['stdoutRef']
                path.write_text('damaged after first review')
            return result
        with patch.object(self.studio, 'ask', side_effect=ask):
            self.studio.start(self.pid, self.session['id'], coordinator_model='opus', team_models=['opus', 'sol', 'gemini'])
            self.studio.threads[self.session['id']].join(15)
        saved = self.studio.load(self.pid, self.session['id'])
        self.assertEqual(saved['status'], 'failed', saved.get('error'))
        self.assertEqual(len([c for c in self.client.calls if c['key'][0] == 'build']), 1)
        self.assertEqual(len([c for c in self.client.calls if c['key'][0] == 'quality']), 1,
                         'Invalid original host output must stop before another review call')
        self.assertEqual(len(saved['runs'][-1]['hostAcceptanceHistory']), 1)
        self.assertNotIn('mergedRevision', saved['runs'][-1])

    def test_legacy_binding_keeps_v1_shape_and_cannot_gain_followup_identity(self):
        requirement = {'mode': 'independent', 'criterionIds': ['c1'], 'reason': 'fixed'}
        schedule = {'schema': 'yuki-review-schedule/v1', 'mode': 'independent', 'author': 'sol',
                    'reviewer': 'gemini', 'authorFamily': 'openai', 'reviewerFamily': 'google',
                    'criterionIds': ['c1'], 'nodeId': 'verify-0-code'}
        module = {'id': 'code', 'assignment': {'model': 'sol'}, 'reviewRequirementVersion': 1,
                  'reviewRequirements': requirement, 'reviewSchedule': schedule}
        contract = {'id': 'contract', 'model': 'sol', 'criteria': [{'id': 'c1'}], 'reviewRequirementVersion': 1,
                    'reviewRequirements': requirement, 'reviewSchedule': schedule}
        contract['fingerprint'] = fingerprint(contract)
        run = {'reviewRequirementVersion': 1, 'plan': {'modules': [module]}, 'taskContracts': {'code': contract}, 'nodes': []}
        args = (run, module, contract, {'id': 'build-code', 'agent': 'sol'}, 'gemini', [], [], [], 0)
        binding = binding_for(*args)
        self.assertEqual(binding['schema'], 'yuki-artifact-review-binding/v1')
        self.assertNotIn('repairRoutingVersion', binding)
        self.assertNotIn('reviewAttemptId', binding)
        with self.assertRaises(ValueError):
            binding_for(*args, review_attempt='repair-review-r0')
        with self.assertRaises(ValueError):
            review_version({'nodes': [{'artifactReviewBinding': {'schema': 'yuki-artifact-review-binding/v2'}}]})


if __name__ == '__main__':
    unittest.main()
