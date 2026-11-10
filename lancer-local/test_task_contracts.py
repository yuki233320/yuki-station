"""Exercise contracts through the real scheduler and store with fixed model replies."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import Store
from studio import Studio
from studio_business import validate_business_response
from test_studio_business import BusinessClient, module


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = BusinessClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('验收契约测试', '交付可核对的成果', '')['id']

    def tearDown(self):
        for thread in self.studio.threads.values():
            thread.join(15)
        self.tmp.cleanup()

    def run_task(self, sid=None, resume=False):
        if sid is None:
            sid = self.studio.create_session(self.pid, '契约与交接', '整理资料并交付成果', 'general')['id']
        self.studio.start(self.pid, sid, 'live', resume)
        self.studio.threads[sid].join(15)
        self.assertFalse(self.studio.threads[sid].is_alive())
        return self.studio.load(self.pid, sid)

    def test_ordinary_contract_matches_request_acceptance_and_export(self):
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        contract = run['taskContracts']['brief']
        self.assertEqual(contract['sourceRevision'], run['projectRevision'])
        self.assertEqual(contract['sourceNodeId'], 'draft')
        self.assertEqual(contract['criteria'], [{'id': 'brief-c1', 'text': '明确说明 brief 的成果', 'required': True}])
        self.assertEqual([call['key'][0] for call in self.client.calls], ['plan', 'build', 'integration'])
        self.assertEqual(len(run['handoffs']), 1)
        handoff = run['handoffs'][0]
        self.assertEqual(handoff['sources'], [])
        self.assertEqual(handoff['contractFingerprint'], contract['fingerprint'])
        context = self.studio.context(self.pid, session['id'], handoff['contextRef'])
        shared = context['memory']['sharedEvidence']
        self.assertEqual(shared['executionContract'], contract)
        self.assertEqual(shared['handoff']['id'], handoff['id'])
        self.assertEqual(shared, self.client.calls[1]['memory']['sharedEvidence'])
        event = next(event for event in session['events'] if event['seq'] == handoff['eventSeq'])
        self.assertEqual(event['handoffId'], handoff['id'])
        assessment = run['final']['moduleAcceptance'][0]
        self.assertEqual(assessment['contractId'], contract['id'])
        self.assertEqual(assessment['contractFingerprint'], contract['fingerprint'])
        self.assertEqual(assessment['criteria'][0]['criterionId'], 'brief-c1')

        with zipfile.ZipFile(io.BytesIO(self.studio.export(self.pid, session['id']))) as archive:
            exported = json.loads(archive.read('会话与执行记录.json'))['runs'][-1]
            self.assertEqual(exported['taskContracts'], run['taskContracts'])
            self.assertEqual(exported['handoffs'], run['handoffs'])
            saved_context = json.loads(archive.read('contexts/' + handoff['contextRef'] + '.json'))
            self.assertEqual(saved_context, context)

    def test_fan_in_records_each_actual_source_and_file_bytes(self):
        self.client.modules = [module('left'), module('right'), module('joined', deps=['left', 'right'])]
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        handoff = next(item for item in run['handoffs'] if item['nodeId'] == 'build-joined')
        self.assertEqual({source['moduleId'] for source in handoff['sources']}, {'left', 'right'})
        context = self.studio.context(self.pid, session['id'], handoff['contextRef'])
        upstream = context['memory']['sharedEvidence']['upstream']
        root = self.studio.project_dir(self.pid) / session['id']
        for source in handoff['sources']:
            node = next(node for node in run['nodes'] if node['id'] == source['nodeId'])
            response = json.loads((root / 'responses' / (source['responseRef'] + '.json')).read_text())
            self.assertEqual(response['node'], node['id'])
            self.assertEqual(source['model'], node['agent'])
            self.assertEqual(response['result'], upstream[source['moduleId']])
            file = source['files'][0]
            delivered = (self.store.current(self.pid)[1] / file['path']).read_bytes()
            self.assertEqual(file['size'], len(delivered))
            self.assertEqual(file['sha256'], hashlib.sha256(delivered).hexdigest())
        self.assertEqual(len(self.client.calls), 5)

    def test_decision_cannot_remove_or_rewrite_planned_acceptance(self):
        self.client.modules = [module('software', 'development', 3)]
        self.client.modules[0]['acceptance'].append('保留原始业务约束')
        for change in ('remove', 'rewrite'):
            with self.subTest(change=change):
                def mutate(stage, result):
                    if stage == 'decision':
                        criteria = result['moduleUpdate']['acceptance']
                        if change == 'remove':
                            criteria.pop()
                        else:
                            criteria[0] = '降低后的验收要求'
                    return result
                self.client.mutate = mutate
                session = self.run_task()
                self.assertEqual(session['status'], 'failed')
                self.assertIn('保留全部原定验收标准', session['error'])
                run = session['runs'][-1]
                self.assertFalse(any(node['phase'] == 'build' for node in run['nodes']))
                self.assertNotIn('taskContracts', run)

    def test_decision_can_add_a_criterion_before_contract_freezes(self):
        self.client.modules = [module('software', 'development', 3)]
        def append_criterion(stage, result):
            if stage == 'decision':
                result['moduleUpdate']['acceptance'].append('明确接口约束')
            return result
        self.client.mutate = append_criterion
        session = self.run_task()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        contract = run['taskContracts']['software']
        self.assertEqual(contract['sourceNodeId'], 'decision-software')
        self.assertEqual([criterion['text'] for criterion in contract['criteria']], ['明确说明 software 的成果', '明确接口约束'])
        self.assertEqual([criterion['criterionId'] for criterion in run['final']['moduleAcceptance'][0]['criteria']], ['software-c1', 'software-c2'])

    def test_resume_keeps_contract_and_handoff_history_without_repeating_upstream(self):
        self.client.modules = [module('upstream'), module('downstream', deps=['upstream'])]
        pending_failure = [True]
        def fail_downstream_once(stage, result):
            if stage == 'build' and result['files'][0]['path'] == 'downstream.md' and pending_failure[0]:
                pending_failure[0] = False
                raise ValueError('provider interruption')
            return result
        self.client.mutate = fail_downstream_once
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        before = copy.deepcopy(session['runs'][-1])
        old_handoff = next(item for item in before['handoffs'] if item['nodeId'] == 'build-downstream')
        old_context = self.studio.context(self.pid, session['id'], old_handoff['contextRef'])
        resumed = self.run_task(session['id'], resume=True)
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        run = resumed['runs'][-1]
        self.assertEqual(run['taskContracts'], before['taskContracts'])
        self.assertEqual(run['handoffs'][:len(before['handoffs'])], before['handoffs'])
        attempts = [item for item in run['handoffs'] if item['nodeId'] == 'build-downstream']
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0]['contextRef'], attempts[1]['contextRef'])
        self.assertNotEqual(attempts[0]['id'], attempts[1]['id'])
        self.assertEqual(attempts[0]['sources'], attempts[1]['sources'])
        self.assertEqual(self.studio.context(self.pid, session['id'], old_handoff['contextRef']), old_context)
        self.assertEqual(sum(call['key'][:2] == ('build', 'upstream') for call in self.client.calls), 1)
        self.assertEqual(sum(event['type'] == 'contract_frozen' for event in resumed['events']), 2)

    def test_old_run_resumes_without_backfilling_new_records(self):
        self.client.fail_once = ('plan', None, 'opus', 0)
        session = self.run_task()
        self.assertEqual(session['status'], 'failed')
        session['runs'][-1].pop('collaborationVersion')
        session['runs'][-1].pop('qualityVersion')
        session['runs'][-1].pop('reviewRequirementVersion')
        self.studio.save(session)
        resumed = self.run_task(session['id'], resume=True)
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        run = resumed['runs'][-1]
        self.assertNotIn('collaborationVersion', run)
        self.assertNotIn('reviewRequirementVersion', run)
        self.assertNotIn('taskContracts', run)
        self.assertNotIn('handoffs', run)
        self.assertNotIn('contractId', run['final']['moduleAcceptance'][0])
        self.assertFalse(any(event['type'] == 'contract_frozen' for event in resumed['events']))

    def test_final_validation_uses_frozen_contract_instead_of_changed_plan(self):
        session = self.run_task()
        run = session['runs'][-1]
        plan = copy.deepcopy(run['plan'])
        plan['modules'][0]['acceptance'] = ['另一个标准']
        shared = {'businessStage': 'integration', 'plan': plan, 'taskContracts': run['taskContracts'],
                  'modules': {'brief': next(node['result'] for node in run['nodes'] if node['id'] == 'build-brief')},
                  'checks': run['checks']}
        valid = copy.deepcopy(run['final'])
        validate_business_response({'agent': 'opus'}, valid, shared, self.client.available())
        changed = copy.deepcopy(run['final'])
        changed['moduleAcceptance'][0]['criteria'][0]['criterion'] = '另一个标准'
        with self.assertRaisesRegex(ValueError, '每一条原定验收标准'):
            validate_business_response({'agent': 'opus'}, changed, shared, self.client.available())

    def test_repair_action_is_validated_before_acceptance_is_saved(self):
        session = self.run_task()
        run = session['runs'][-1]
        shared = {'businessStage': 'integration', 'plan': run['plan'], 'taskContracts': run['taskContracts'],
                  'modules': {'brief': next(node['result'] for node in run['nodes'] if node['id'] == 'build-brief')},
                  'checks': run['checks']}
        for action in ('repair', 'replan', 'needs_input'):
            with self.subTest(action=action):
                result = copy.deepcopy(run['final'])
                result['accepted'] = False
                result['moduleAcceptance'][0].update(accepted=False, unmet=['固定输入缺少必要字段'], repairAction=action)
                validate_business_response({'agent': 'opus'}, result, shared, self.client.available())
        for accepted, unmet, action in ((False, ['原因'], 'skip'), (False, [], 'replan'),
                                        (True, [], 'needs_input'), (False, [''], 'repair')):
            with self.subTest(accepted=accepted, unmet=unmet, action=action):
                result = copy.deepcopy(run['final'])
                result['accepted'] = False
                result['moduleAcceptance'][0].update(accepted=accepted, unmet=unmet, repairAction=action)
                with self.assertRaises(ValueError):
                    validate_business_response({'agent': 'opus'}, result, shared, self.client.available())


if __name__ == '__main__':
    unittest.main()
