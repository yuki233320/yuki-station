"""Host-owned checks are explicit, frozen and independent of model proposals."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from app import Conflict, Store
from studio import Studio
from test_studio_business import BusinessClient, module


class HostAcceptanceConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = BusinessClient([module('brief')])
        self.studio = Studio(Store(self.root / 'data', seed=False), self.client)
        self.pid = self.studio.create_project('宿主验收配置', '先验收，再交付')['id']
        self.marker = self.root / 'was-executed'
        self.script = self.root / 'accept.py'
        self.script.write_text('import json\nfrom pathlib import Path\n'
            + 'Path(' + repr(str(self.marker)) + ').write_text("executed")\n'
            + 'print(json.dumps({"accepted":True,"criteria":[{"criterionId":"HC-1","status":"passed"}]}))\n')
        self.request = {'name': '固定宿主检查', 'scriptPath': str(self.script),
                        'files': ['brief.md'], 'criteria': [{'id': 'HC-1', 'description': '实际运行验证'}],
                        'timeoutSeconds': 10}

    def tearDown(self):
        for thread in self.studio.threads.values():
            if thread.ident is not None:thread.join(15)
        self.tmp.cleanup()

    def register(self):
        entry = self.studio.host_acceptance.register(self.request)
        self.assertFalse(self.marker.exists(), 'Registration must not execute the script')
        return entry

    def select(self, entry):
        meta = self.studio.meta(self.pid)
        return self.studio.set_host_acceptance(self.pid, meta['version'], [entry['id']])

    def execute(self, session, resume=False):
        self.studio.start(self.pid, session['id'], resume=resume)
        self.studio.threads[session['id']].join(15)
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        return self.studio.load(self.pid, session['id'])

    def test_register_does_not_select_or_execute_and_selection_uses_project_version(self):
        entry = self.register()
        self.assertEqual(self.studio.project(self.pid)['hostAcceptanceIds'], [])
        old = self.studio.meta(self.pid)['version']
        selected = self.select(entry)
        self.assertEqual(selected['hostAcceptanceIds'], [entry['id']])
        with self.assertRaises(Conflict):self.studio.set_host_acceptance(self.pid, old, [])
        self.assertFalse(self.marker.exists())

    def test_missing_check_blocks_before_any_model_calls(self):
        entry = self.register();self.select(entry)
        self.studio.host_acceptance.remove(entry['id'])
        session = self.studio.create_session(self.pid, '任务', '整理项目说明', 'general')
        with self.assertRaises(ValueError):self.studio.start(self.pid, session['id'])
        self.assertFalse(self.client.calls)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.studio.load(self.pid, session['id'])['status'], 'ready')

    def test_registered_check_is_frozen_in_plan_context_and_module_contract(self):
        entry = self.register();self.select(entry)
        session = self.studio.create_session(self.pid, '任务', '整理项目说明', 'general')
        result = self.execute(session)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        run = result['runs'][-1]
        self.assertEqual(run['hostAcceptanceVersion'], 1)
        self.assertEqual(run['hostAcceptanceChecks'][0]['scriptSha256'], entry['scriptSha256'])
        self.assertNotIn('scriptPath', run['hostAcceptanceChecks'][0])
        self.assertEqual(run['taskContracts']['brief']['hostAcceptanceChecks'], run['hostAcceptanceChecks'])
        self.assertEqual(self.client.calls[0]['memory']['sharedEvidence']['hostAcceptanceChecks'], run['hostAcceptanceChecks'])
        self.assertTrue(self.marker.exists())

    def test_model_cannot_enable_a_registered_but_unselected_check(self):
        entry = self.register()
        self.client.mutate = lambda stage, result: dict(result, hostAcceptanceChecks=[entry]) if stage == 'plan' else result
        session = self.studio.create_session(self.pid, '任务', '整理项目说明', 'general')
        result = self.execute(session)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['runs'][-1]['hostAcceptanceChecks'], [])
        self.assertFalse(self.marker.exists())

    def test_plan_cannot_omit_registered_output_before_dispatching_workers(self):
        self.request['files'] = ['required.py']
        entry = self.register();self.select(entry)
        session = self.studio.create_session(self.pid, '任务', '整理项目说明', 'general')
        result = self.execute(session)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(any(call['key'][0] == 'build' for call in self.client.calls))
        self.assertFalse(self.marker.exists())

    def test_legacy_resume_does_not_gain_new_project_execution_authority(self):
        self.client.fail_once = ('integration', None, 'opus', 0)
        session = self.studio.create_session(self.pid, '任务', '整理项目说明', 'general')
        failed = self.execute(session)
        self.assertEqual(failed['status'], 'failed')
        failed['runs'][-1].pop('hostAcceptanceVersion', None)
        failed['runs'][-1].pop('hostAcceptanceChecks', None)
        self.studio.save(failed)
        entry = self.register();self.select(entry)
        resumed = self.execute(failed, resume=True)
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        self.assertNotIn('hostAcceptanceChecks', resumed['runs'][-1])
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
