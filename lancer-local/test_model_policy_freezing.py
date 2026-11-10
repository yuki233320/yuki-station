"""Exercise request-policy snapshots through real Studio and both batch runners.

Providers are deterministic BusinessClient fixtures. No credentials, transport,
network requests, readiness probes or paid model calls are involved.
"""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Store
from collaboration_comparison import ComparisonRunner
from collaboration_evaluation import case_definition
from controlled_experiment import ControlledExperimentRunner
from request_policy import effective_policy, profile_fingerprint
from studio import Studio
from test_studio_business import BusinessClient, module


class PolicyCaptureClient(BusinessClient):
    def __init__(self, modules=None):
        super().__init__(modules)
        self.observed_scopes = []
        self.pause_next = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def call(self, model, messages, cancel, on_delta=None):
        with self.lock:
            pause, self.pause_next = self.pause_next, False
        if pause:
            self.entered.set()
            if not self.release.wait(8):
                raise AssertionError('Test did not release the blocked provider fixture')
        scope = getattr(self._request_local, 'profiles', None)
        observed = copy.deepcopy(scope)
        profile = copy.deepcopy(scope[model] if scope is not None else self.profiles[model])
        policy = effective_policy(profile)
        with self.lock:
            self.observed_scopes.append({'model': model, 'scope': observed, 'policy': copy.deepcopy(policy)})
        try:
            prompt = json.loads(messages[-1]['content'])
        except (ValueError, TypeError):
            prompt = None
        if isinstance(prompt, dict) and prompt.get('role') in ('single', 'plan', 'worker1', 'worker2', 'aggregate'):
            # ControlledExperimentRunner uses its own small prompt schema, not
            # the Studio business prompt parsed by BusinessClient.call.
            role = prompt['role']
            with self.lock:
                self.calls.append({'key': ('controlled-' + role, None, model, 0)})
            if role == 'plan':
                result = {'summary': 'Fixture plan', 'assignments': [
                    {'agentId': name, 'title': name, 'task': 'Produce and check the complete result'}
                    for name in ('worker1', 'worker2')]}
            else:
                result = {'summary': 'Deterministic candidate fixture', 'files': [
                    {'path': 'result.json', 'content': json.dumps(case_definition()['reference']['result'])}]}
            receipt = {'mode': 'fixture', 'requestedModel': model, 'returnedModel': profile['model']}
        else:
            result, receipt = super().call(model, messages, cancel, on_delta)
        # This records the policy actually visible inside the invocation's
        # scope, rather than copying an expected policy into the test receipt.
        receipt = {**receipt, 'requestPolicy': copy.deepcopy(policy),
                   'profileFingerprint': profile_fingerprint(profile)}
        return result, receipt


class ModelPolicyFreezingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.studios = []
        self.runners = []
        self.studio, self.client, self.pid = self.new_studio('main')

    def tearDown(self):
        for studio in self.studios:
            studio.client.release.set()
            for cancel in studio.cancels.values(): cancel.set()
        for runner in self.runners:
            runner.cancel.set()
            if runner.thread: runner.thread.join(10)
        for studio in self.studios:
            for thread in studio.threads.values():
                if thread.ident is not None: thread.join(10)
        self.temp.cleanup()

    def new_studio(self, folder, modules=None):
        client = PolicyCaptureClient(modules)
        studio = Studio(Store(self.root / folder, seed=False), client)
        # Match production's shared registry mapping while retaining the
        # existing fixture's availability override and deterministic answers.
        client.profiles = studio.registry.profiles
        self.studios.append(studio)
        pid = studio.create_project('Policy snapshot fixture', 'Verify configured request controls')['id']
        return studio, client, pid

    def session(self, studio=None, pid=None):
        studio, pid = studio or self.studio, pid or self.pid
        return studio.create_session(pid, 'Policy task', '整理资料并交付明确说明', 'general')

    def start(self, session, resume=False):
        return self.studio.start(self.pid, session['id'], resume=resume,
                                 coordinator_model='deepseek', team_models=['deepseek'])

    def finish(self, session, studio=None, pid=None):
        studio, pid = studio or self.studio, pid or self.pid
        worker = studio.threads[session['id']]
        worker.join(10)
        self.assertFalse(worker.is_alive(), 'Fixture worker did not settle')
        return studio.load(pid, session['id'])

    def change(self, studio, mid, limit):
        info = studio.registry.policy_info(mid)
        studio.update_model_policy(mid, {'maxOutputTokens': limit}, info['policyVersion'])
        return copy.deepcopy(studio.registry.profiles[mid]['requestPolicy'])

    def assert_scopes(self, client, observed, expected):
        self.assertTrue(observed)
        for call in observed:
            self.assertIsNotNone(call['scope'], 'Model invocation escaped its frozen request scope')
            self.assertEqual(call['policy'], expected[call['model']]['requestPolicy'])
        self.assertIsNone(getattr(client._request_local, 'profiles', None), 'Scope leaked into the caller thread')

    def test_running_task_keeps_actual_scope_and_receipts_then_next_run_uses_new_policy(self):
        session = self.session()
        old = self.client.snapshot_profiles(['deepseek'])
        self.client.pause_next = True
        self.start(session)
        try:
            self.assertTrue(self.client.entered.wait(5))
            persisted = self.studio.load(self.pid, session['id'])['runs'][-1]
            self.assertEqual(persisted['modelRequestProfiles'], old)
            self.assertEqual(persisted['modelRequestVersion'], 1)
            new_policy = self.change(self.studio, 'deepseek', 21001)
        finally:
            self.client.release.set()
        completed = self.finish(session)
        self.assertEqual(completed['status'], 'completed', completed.get('error'))
        run = completed['runs'][-1]
        self.assertEqual(run['modelRequestProfiles'], old)
        self.assert_scopes(self.client, self.client.observed_scopes, old)
        for node in run['nodes']:
            if node.get('receipt'):
                self.assertEqual(node['receipt']['requestPolicy'], old[node['agent']]['requestPolicy'])
        before = len(self.client.observed_scopes)
        next_session = self.session()
        self.start(next_session)
        next_result = self.finish(next_session)
        self.assertEqual(next_result['status'], 'completed', next_result.get('error'))
        next_run = next_result['runs'][-1]
        self.assertEqual(next_run['modelRequestProfiles']['deepseek']['requestPolicy'], new_policy)
        self.assertEqual(next_run['modelRequestPolicyVersions']['deepseek'], 1)
        self.assert_scopes(self.client, self.client.observed_scopes[before:], next_run['modelRequestProfiles'])

    def fail_before_final(self):
        session = self.session()
        def fail(): raise ValueError('Fixture final call interrupted')
        self.client.before_final = fail
        self.start(session)
        failed = self.finish(session)
        self.assertEqual(failed['status'], 'failed')
        return session, failed

    def test_explicit_resume_keeps_old_snapshot_after_policy_update(self):
        session, failed = self.fail_before_final()
        old = copy.deepcopy(failed['runs'][-1]['modelRequestProfiles'])
        first_attempt = copy.deepcopy(failed['runs'][-1]['executionAttempts'][0])
        self.change(self.studio, 'deepseek', 22002)
        before = len(self.client.observed_scopes)
        self.start(session, resume=True)
        completed = self.finish(session)
        self.assertEqual(completed['status'], 'completed', completed.get('error'))
        run = completed['runs'][-1]
        self.assertEqual(run['modelRequestProfiles'], old)
        self.assertEqual(run['executionAttempts'][0], first_attempt)
        self.assertEqual(run['executionAttempts'][-1]['reason'], 'resume')
        self.assert_scopes(self.client, self.client.observed_scopes[before:], old)
        self.assertNotEqual(self.client.profiles['deepseek']['requestPolicy'], old['deepseek']['requestPolicy'])

    def test_legacy_unfrozen_run_refuses_resume_after_explicit_policy_update(self):
        session, failed = self.fail_before_final()
        for key in ('modelRequestVersion', 'modelRequestProfiles', 'modelRequestPolicyVersions'):
            failed['runs'][-1].pop(key, None)
        self.studio.save(failed)
        self.change(self.studio, 'deepseek', 23003)
        before = self.studio.load(self.pid, session['id'])
        calls = len(self.client.calls)
        with self.assertRaisesRegex(ValueError, '未冻结请求参数'):
            self.start(session, resume=True)
        self.assertEqual(self.studio.load(self.pid, session['id']), before)
        self.assertEqual(len(self.client.calls), calls)
        self.assertNotIn('modelRequestProfiles', before['runs'][-1])

    def test_published_commit_intent_recovers_even_legacy_policy_changed_without_new_calls(self):
        session = self.session()
        original = self.studio.store.commit
        published = []
        def lose_return(*args, **kwargs):
            result = original(*args, **kwargs)
            published.append(result['revision'])
            raise OSError('Fixture lost return after publishing HEAD')
        with patch.object(self.studio.store, 'commit', side_effect=lose_return):
            self.start(session)
            failed = self.finish(session)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(len(published), 1)
        self.assertTrue(failed['runs'][-1]['commitIntent'])
        for key in ('modelRequestVersion', 'modelRequestProfiles', 'modelRequestPolicyVersions'):
            failed['runs'][-1].pop(key, None)
        self.studio.save(failed)
        self.change(self.studio, 'deepseek', 24004)
        calls = len(self.client.calls)
        with patch.object(self.client, 'available', return_value=[]), \
             patch.object(self.client, 'call', side_effect=AssertionError('Recovery must not call a model')), \
             patch.object(self.studio.store, 'commit', side_effect=AssertionError('Already published HEAD must not commit twice')):
            self.start(session, resume=True)
            completed = self.finish(session)
        self.assertEqual(completed['status'], 'completed', completed.get('error'))
        self.assertEqual(completed['runs'][-1]['mergedRevision'], published[0])
        self.assertEqual(self.studio.store.current(self.pid)[0]['revision'], published[0])
        self.assertEqual(len(self.client.calls), calls)
        self.assertNotIn('modelRequestProfiles', completed['runs'][-1], 'Recovery must not invent a historical policy snapshot')

    def test_both_comparison_runners_freeze_every_arm_and_repetition_before_policy_changes(self):
        for kind, runner_class in [('workflow', ComparisonRunner), ('controlled', ControlledExperimentRunner)]:
            with self.subTest(runner=kind):
                specification = module('result', 'data')
                specification.update(scope=['result.json'], roleType='data')
                studio, client, pid = self.new_studio(kind, [specification])
                def answer(stage, result):
                    if stage == 'build':
                        result['files'] = [{'path': 'result.json', 'content': json.dumps(case_definition()['reference']['result'])}]
                    return result
                client.mutate = answer
                old = client.snapshot_profiles(['deepseek', 'sol'])
                runner = runner_class(studio)
                self.runners.append(runner)
                client.pause_next = True
                options = {'repetitions': 2}
                if kind == 'controlled': options['homogeneous'] = False
                initial = runner.start(pid, 'deepseek', ['deepseek', 'sol'], **options)
                try:
                    self.assertTrue(client.entered.wait(5))
                    self.change(studio, 'deepseek', 25005)
                    self.change(studio, 'sol', 26006)
                finally:
                    client.release.set()
                runner.thread.join(15)
                self.assertFalse(runner.thread.is_alive())
                records = [r for r in runner.index(pid)['records'] if r['batchId'] == initial['batchId']]
                self.assertEqual(len(records), 2)
                for record in records:
                    self.assertEqual(record['status'], 'completed', record.get('error'))
                    self.assertEqual(record['modelRequestProfiles'], old)
                    for arm in record['arms'].values():
                        self.assertEqual(arm['status'], 'completed', arm.get('error'))
                        self.assertTrue(arm['evaluation']['accepted'])
                        if kind == 'controlled':
                            for call in arm['calls']:
                                self.assertEqual(call['receipt']['requestPolicy'], old[call['model']]['requestPolicy'])
                        else:
                            saved = studio.load(arm['projectId'], arm['sessionId'])['runs'][-1]
                            for mid, profile in saved['modelRequestProfiles'].items():
                                self.assertEqual(profile, old[mid])
                self.assert_scopes(client, client.observed_scopes, old)
                self.assertEqual({call['model'] for call in client.observed_scopes}, {'deepseek', 'sol'})
                self.assertEqual(client.profiles['deepseek']['requestPolicy']['maxOutputTokens'], 25005)
                self.assertEqual(client.profiles['sol']['requestPolicy']['maxOutputTokens'], 26006)


if __name__ == '__main__':
    unittest.main()
