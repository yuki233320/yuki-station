"""Execution feasibility is checked before automatic triage can incur a call."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from model_catalog import MODELS
from trigger_policy import normalize_source
import test_trigger_service as fixture


class TriggerReadinessTests(unittest.TestCase):
    tearDown = fixture.TriggerServiceTests.tearDown
    configure = fixture.TriggerServiceTests.configure
    ingest = fixture.TriggerServiceTests.ingest

    def setUp(self):
        fixture.TriggerServiceTests.setUp(self)
        self.source_payload['target']['taskRoute'] = {'mode':'direct','contract':{
            'roleId':'backend','workType':'development','scope':['brief.md'],
            'instructions':'Update only the project brief and retain independent artifact review.',
            'acceptance':['The project brief states the requested result accurately.'],
            'complexity':2,'skills':{'coding':1},'requiredCapabilities':['text']}}

    def models_called(self):
        return self.client.evaluations + len(self.client.calls)

    def files(self):
        # SQLite's read-lock shared-memory bytes are not application writes.
        return {p.relative_to(self.tmp.name).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in Path(self.tmp.name).rglob('*') if p.is_file() and not p.name.endswith('-shm')}

    def grant_workspace(self):
        script=Path(self.tmp.name)/'never-execute-readiness.py'
        script.write_text("raise AssertionError('Readiness must not execute a checker')\n")
        entry=self.studio.host_acceptance.register({'id':'readiness-check','name':'Never run during readiness',
            'scriptPath':str(script),'files':['brief.md'],
            'criteria':[{'id':'READY-1','description':'The requested brief exists'}]})
        project=self.studio.project(self.pid)
        project=self.studio.set_host_acceptance(self.pid,project['version'],[entry['id']])
        config=deepcopy(project['teamConfig']);config['workerTools']={'version':1,'roleGrants':{
            'backend':{'enabled':True,'checkIds':[entry['id']],'childExecution':True}}}
        self.studio.update_project(self.pid,'Readiness fixture',project['version'],config)
        self.source_payload['target']['taskRoute']['contract']['requiredCapabilities']=['text','workspace']
        return entry['id']

    def assert_no_paid_attempt_or_task(self):
        self.assertEqual(self.models_called(),0)
        self.assertEqual(self.service.snapshot(self.pid)['stats']['modelCalls'],0)
        self.assertFalse(self.service.store.effects())
        self.assertFalse(self.studio.list_sessions())
        self.assertEqual(len(self.studio.store.list_projects()),1)

    def test_auto_direct_without_independent_reviewer_rejects_save_before_model(self):
        self.source_payload['target']['teamModels']=['opus','sol']
        with self.assertRaisesRegex(ValueError,'独立|复核|审查'):self.configure('auto')
        self.assertFalse(self.service.store.sources())
        self.assert_no_paid_attempt_or_task()

    def test_same_family_multiple_models_do_not_count_as_independent_review(self):
        replacement={mid:{**deepcopy(MODELS[mid]),'family':'Same fixture family'} for mid in ('opus','sol','gemini')}
        with patch.dict(MODELS,replacement):
            with self.assertRaisesRegex(ValueError,'独立|复核|审查'):self.configure('auto')
        self.assertFalse(self.service.store.sources())
        self.assert_no_paid_attempt_or_task()

    def test_only_different_family_coordinator_cannot_review_worker(self):
        replacement={mid:{**deepcopy(MODELS[mid]),'family':'Worker fixture family'} for mid in ('sol','gemini')}
        with patch.dict(MODELS,replacement):
            with self.assertRaisesRegex(ValueError,'独立|复核|审查'):self.configure('auto')
        self.assert_no_paid_attempt_or_task()

    def test_aliases_with_same_request_model_cannot_fake_distinct_review(self):
        profiles=self.client.snapshot_profiles(['opus','sol','gemini'])
        profiles['sol']['model']=profiles['gemini']['model']='same-underlying-fixture-model'
        with patch.object(self.client,'snapshot_profiles',side_effect=lambda mids:{mid:deepcopy(profiles[mid]) for mid in mids}):
            with self.assertRaisesRegex(ValueError,'同一请求型号|独立|复核'):self.configure('auto')
        self.assert_no_paid_attempt_or_task()

    def test_valid_three_role_direct_preflight_is_readonly_and_returns_actual_schedule(self):
        from trigger_tasks import direct_target_readiness
        self.grant_workspace()
        source=normalize_source({**deepcopy(self.source_payload),'mode':'auto'})
        original=deepcopy(source);before=self.files()
        with patch('host_acceptance.run_check',side_effect=AssertionError('Readiness executed a checker')):
            plan=direct_target_readiness(self.studio,source,['opus','sol','gemini'])
        self.assertEqual(self.files(),before)
        self.assertEqual(source,original)
        self.assert_no_paid_attempt_or_task()
        self.assertEqual(len(plan['modules']),1)
        module=plan['modules'][0];schedule=module['reviewSchedule']
        self.assertEqual(schedule['mode'],'independent')
        self.assertNotEqual(schedule['authorFamily'],schedule['reviewerFamily'])
        self.assertNotEqual(schedule['reviewer'],'opus')
        self.assertEqual(schedule['author'],module['assignment']['model'])
        self.assertTrue(module['workerBinding']['enabled'])
        source_saved=self.configure('auto')
        self.assertEqual(source_saved['mode'],'auto')
        self.assert_no_paid_attempt_or_task()

    def test_manual_dispatch_checks_execution_even_when_source_is_observe(self):
        self.source_payload['target']['teamModels']=['opus','sol']
        source=self.configure('observe')
        with self.assertRaisesRegex(ValueError,'独立|复核|审查'):
            self.service.validate_target(source,execution=True)
        self.assert_no_paid_attempt_or_task()

    def test_request_model_alias_changed_after_save_is_rechecked_before_automatic_triage(self):
        source=self.configure('auto');batch=self.ingest(source)
        profiles=self.client.snapshot_profiles(['opus','sol','gemini'])
        profiles['sol']['model']=profiles['gemini']['model']='changed-to-same-request-model'
        with patch.object(self.client,'snapshot_profiles',side_effect=lambda mids:{mid:deepcopy(profiles[mid]) for mid in mids}):
            with self.assertRaisesRegex(ValueError,'同一请求型号|独立|复核'):
                self.service.engine.evaluate(batch['id'],manual=False)
        self.assert_no_paid_attempt_or_task()

    def test_saved_source_lost_reviewer_connection_blocks_automatic_triage_without_call(self):
        source=self.configure('auto');batch=self.ingest(source)
        with patch.object(self.client,'available',return_value=['opus','sol']):
            with self.assertRaises(ValueError):self.service.engine.evaluate(batch['id'],manual=False)
        self.assert_no_paid_attempt_or_task()

    def test_template_worker_revoked_after_save_blocks_automatic_triage_without_call(self):
        self.grant_workspace();source=self.configure('auto');batch=self.ingest(source)
        project=self.studio.project(self.pid);config=deepcopy(project['teamConfig'])
        config['workerTools']['roleGrants']['backend']['enabled']=False
        self.studio.update_project(self.pid,'Revoked worker for this fixture',project['version'],config)
        with self.assertRaisesRegex(ValueError,'Worker|岗位|授权'):self.service.engine.evaluate(batch['id'],manual=False)
        self.assert_no_paid_attempt_or_task()

    def test_waiting_for_capacity_rechecks_template_before_model_dispatch(self):
        self.grant_workspace();source=self.configure('auto');batch=self.ingest(source)
        @contextmanager
        def slot(*args,**kwargs):
            # Simulate a template update while the assessment waits for its slot.
            project=self.studio.project(self.pid);config=deepcopy(project['teamConfig'])
            config['workerTools']['roleGrants']['backend']['enabled']=False
            self.studio.update_project(self.pid,'Changed while waiting',project['version'],config)
            yield
        with patch.object(self.studio.capacity,'slot',side_effect=slot):
            with self.assertRaisesRegex(ValueError,'Worker|岗位|授权'):self.service.engine.evaluate(batch['id'],manual=False)
        self.assert_no_paid_attempt_or_task()

    def test_review_mode_allows_assessment_without_executable_team(self):
        self.source_payload['target']['teamModels']=['opus','sol']
        source=self.configure('review');batch=self.ingest(source)
        self.service.engine.evaluate(batch['id'],manual=False)
        self.assertEqual(self.client.evaluations,1)
        self.assertFalse(self.service.store.effects())
        self.assertFalse(self.studio.list_sessions())

    def test_manual_assessment_of_saved_auto_source_does_not_require_execution_readiness(self):
        self.grant_workspace();source=self.configure('auto');batch=self.ingest(source)
        project=self.studio.project(self.pid);config=deepcopy(project['teamConfig'])
        config['workerTools']['roleGrants']['backend']['enabled']=False
        self.studio.update_project(self.pid,'Revoked worker; manual assessment still allowed',project['version'],config)
        with patch.object(self.client,'available',return_value=['opus']):
            self.service.engine.evaluate(batch['id'],manual=True)
        self.assertEqual(self.client.evaluations,1)
        self.assertFalse(self.service.store.effects())
        self.assertFalse(self.studio.list_sessions())

    def test_single_model_non_development_direct_contract_needs_no_unpromised_review(self):
        self.source_payload['target']['teamModels']=['opus']
        self.source_payload['target']['taskRoute']['contract'].update(workType='writing',roleId='writing',
            instructions='Update the brief accurately.',skills={'writing':1})
        source=self.configure('auto')
        self.assertEqual(source['mode'],'auto')
        self.assert_no_paid_attempt_or_task()


if __name__=='__main__':unittest.main()
