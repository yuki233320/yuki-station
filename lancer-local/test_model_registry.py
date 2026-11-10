import copy
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from app import Conflict, Store
from live import PROFILES
from model_catalog import MODELS
from model_registry import ModelRegistry, validate_team_selection
from studio import Studio
from studio_client import StudioClient
from test_studio_business import BusinessClient


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.models = copy.deepcopy(MODELS)

    def tearDown(self):
        MODELS.clear()
        MODELS.update(self.models)
        self.tmp.cleanup()

    def register(self, registry, **changes):
        return registry.register({**dict(providerName='Local fixture', baseUrl='http://127.0.0.1:1234/v1',
                                         modelId='test-model', name='测试型号', family='Test Vendor',
                                         protocol='chat', profile='coding'), **changes})

    def test_public_registry_survives_restart_without_credentials(self):
        registry = ModelRegistry(self.root)
        mid = self.register(registry)
        client = StudioClient(registry.profiles)
        provider = registry.profiles[mid]['provider']
        client.configure(provider, 'fixture-credential-value')
        restored = ModelRegistry(self.root)
        self.assertEqual(restored.profiles[mid], registry.profiles[mid])
        self.assertIn(mid, client.configured_models())
        self.assertNotIn(mid, client.available())
        self.assertEqual(next(s for s in client.model_states() if s["id"]==mid)["callState"], "unverified")
        self.assertNotIn('fixture-credential-value', restored.path.read_text())
        self.assertEqual(StudioClient(restored.profiles).available(), [])
        self.assertEqual(MODELS[mid]['family'], 'Test Vendor')
        self.assertFalse(MODELS[mid]['verified'])
        self.assertEqual(restored.providers(client.available())[-1]['models'], [mid])

    def test_registered_endpoint_uses_actual_http_request_and_parser(self):
        observed = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.update(path=self.path, body=json.loads(self.rfile.read(int(self.headers['Content-Length']))),
                                authorization=self.headers['Authorization'])
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                packet = {'model': 'actual-test-model', 'choices': [{'delta': {'content': '{"summary":"ok"}'}, 'finish_reason': 'stop'}]}
                self.wfile.write(('data: ' + json.dumps(packet) + '\n\ndata: [DONE]\n\n').encode())

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                registry = ModelRegistry(self.root)
                request = dict(providerName='HTTP fixture', baseUrl='http://127.0.0.1:' + str(server.server_port) + '/v1',
                               modelId='test-model', name='HTTP', family='Test', protocol='chat', profile='balanced')
                mid = registry.register(request)
                client = StudioClient(registry.profiles)
                client.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                client.configure(registry.profiles[mid]['provider'], 'fixture-key')
                result, receipt = client.call(mid, [{'role': 'user', 'content': 'test'}], threading.Event())
                self.assertEqual(result, {'summary': 'ok'})
                self.assertEqual(receipt['returnedModel'], 'actual-test-model')
                self.assertEqual(observed['path'], '/v1/chat/completions')
                self.assertEqual(observed['body']['model'], 'test-model')
                self.assertEqual(observed['authorization'], 'Bearer fixture-key')
            finally:
                server.shutdown()
                worker.join()

    def test_selected_coordinator_and_members_reach_real_scheduler(self):
        studio = Studio(Store(self.root, seed=False), BusinessClient())
        pid = studio.create_project('小组测试', '')['id']
        session = studio.create_session(pid, '交付', '整理说明', 'general')
        session = studio.set_team(pid, session['id'], 'deepseek', ['deepseek', 'sol'], session['version'])
        studio.start(pid, session['id'])
        studio.threads[session['id']].join(10)
        saved = studio.load(pid, session['id'])
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        run = saved['runs'][-1]
        self.assertEqual(run['coordinatorModel'], 'deepseek')
        self.assertTrue(all(n['agent'] in ['deepseek', 'sol'] for n in run['nodes']))
        self.assertEqual([n['agent'] for n in run['nodes'] if n['phase'] in ('draft', 'integration')], ['deepseek', 'deepseek'])
        context = studio.context(pid, session['id'], run['nodes'][0]['contextRef'])
        coordinator = next(row for row in context['memory']['teamRoster'] if row['roleId'] == 'coordinator')
        self.assertEqual(coordinator['models'], ['deepseek'])
        memories = studio.project(pid)['team']['memories']
        self.assertEqual(next(row['model'] for row in memories if row['roleId'] == 'coordinator'), 'deepseek')
        studio.set_team(pid, session['id'], 'opus', ['opus'], saved['version'])
        self.assertEqual(studio.load(pid, session['id'])['runs'][-1]['teamModels'], ['deepseek', 'sol'])

    def test_selection_and_registration_reject_invalid_external_values(self):
        for coordinator, members in [('image', ['image']), ('native_codex', ['native_codex']), ('sol', ['deepseek']), ('sol', ['sol', 'sol'])]:
            with self.assertRaises(ValueError):
                validate_team_selection(coordinator, members)
        registry = ModelRegistry(self.root)
        for url in ['file:///tmp/model', 'https://user:pass@example.com/v1', 'https://example.com/v1?key=secret']:
            with self.assertRaises(ValueError):
                registry.register(dict(providerName='bad', baseUrl=url, modelId='model', name='Model', family='Test', protocol='chat', profile='coding'))

    def test_builtins_migrate_to_explicit_protocol_and_policies_without_mutating_global_profiles(self):
        before = copy.deepcopy(PROFILES)
        registry = ModelRegistry(self.root)
        self.assertEqual(registry.profiles['opus']['protocol'], 'anthropic')
        self.assertEqual(registry.profiles['opus']['requestPolicy']['reasoningEffort'], 'medium')
        self.assertEqual(registry.profiles['gemini']['requestPolicy'], {
            'maxOutputTokens': 65536, 'reasoningEffort': 'low', 'jsonMode': False, 'timeoutSeconds': 1200})
        self.assertEqual(registry.profiles['deepseek']['requestPolicy']['jsonMode'], True)
        self.assertTrue(all('requestPolicy' in profile for profile in registry.profiles.values()))
        self.assertEqual(PROFILES, before)
        self.assertEqual(registry.policy_info('opus')['policyVersion'], 0)
        self.assertFalse(registry.path.exists(), 'Reading defaults must not rewrite configuration')

    def test_registered_partial_policy_is_explicit_and_survives_restart(self):
        registry = ModelRegistry(self.root)
        mid = self.register(registry, requestPolicy={'maxOutputTokens': 24000, 'reasoningEffort': 'low', 'timeoutSeconds': .25})
        info = registry.policy_info(mid)
        self.assertEqual(info, {'requestPolicy': {'maxOutputTokens': 24000, 'reasoningEffort': 'low',
                                                'jsonMode': False, 'timeoutSeconds': .25}, 'policyVersion': 1})
        restored = ModelRegistry(self.root)
        self.assertEqual(restored.policy_info(mid), info)
        document = json.loads(registry.path.read_text())
        self.assertEqual(document['requestPolicies'][mid], info['requestPolicy'])
        self.assertNotIn('requestPolicy', document['models'][mid]['connection'])

    def test_versioned_partial_updates_preserve_other_fields_and_mapping_identity(self):
        registry = ModelRegistry(self.root)
        mapping = registry.profiles
        first = registry.update_policy('sol', {'maxOutputTokens': 50000}, 0)
        self.assertEqual(first['policyVersion'], 1)
        second = registry.update_policy('sol', {'timeoutSeconds': 20}, 1)
        self.assertEqual(second['policyVersion'], 2)
        self.assertEqual(second['requestPolicy']['maxOutputTokens'], 50000)
        self.assertEqual(second['requestPolicy']['reasoningEffort'], 'medium')
        self.assertIs(mapping, registry.profiles)
        self.assertEqual(mapping['sol']['requestPolicy'], second['requestPolicy'])
        self.assertEqual(ModelRegistry(self.root).policy_info('sol'), second)

    def test_stale_registry_instance_cannot_overwrite_a_newer_policy(self):
        first, stale = ModelRegistry(self.root), ModelRegistry(self.root)
        first.update_policy('sol', {'timeoutSeconds': 10}, 0)
        before = first.path.read_bytes()
        with self.assertRaises(Conflict): stale.update_policy('sol', {'maxOutputTokens': 1000}, 0)
        self.assertEqual(first.path.read_bytes(), before)
        self.assertEqual(stale.policy_info('sol')['requestPolicy']['timeoutSeconds'], 10)
        self.assertEqual(stale.policy_info('sol')['policyVersion'], 1)

    def test_invalid_policy_does_not_add_provider_model_or_rewrite_saved_data(self):
        registry = ModelRegistry(self.root)
        self.register(registry)
        before = registry.path.read_bytes()
        ids = set(registry.profiles)
        for policy in ({'headers': {'Authorization': 'fixture'}}, {'timeoutSeconds': float('nan')},
                       {'maxOutputTokens': True}, {'jsonMode': 1}, {'endpoint': 'https://elsewhere.test'}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.register(registry, requestPolicy=policy)
            self.assertEqual(registry.path.read_bytes(), before)
            self.assertEqual(set(registry.profiles), ids)
        for version in (True, -1, '0', None):
            with self.assertRaises(ValueError): registry.update_policy('sol', {'timeoutSeconds': 10}, version)
        with self.assertRaises(ValueError): self.register(registry, apiKey='fixture-secret')

    def test_failed_persistence_does_not_publish_new_policy(self):
        registry = ModelRegistry(self.root)
        registry.update_policy('sol', {'timeoutSeconds': 10}, 0)
        before = registry.path.read_bytes()
        old = copy.deepcopy(registry.profiles)
        with patch('model_registry.os.replace', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError): registry.update_policy('sol', {'timeoutSeconds': 20}, 1)
        self.assertEqual(registry.path.read_bytes(), before)
        self.assertEqual(registry.profiles, old)
        self.assertFalse(list(self.root.glob('.model-registry-*.pending')))

    def test_legacy_file_load_is_read_only_and_migrates_on_explicit_save(self):
        registry = ModelRegistry(self.root)
        mid = self.register(registry)
        document = json.loads(registry.path.read_text())
        document.pop('requestPolicies'); document.pop('policyVersions')
        document['models'][mid]['connection']['requestPolicy'] = {'timeoutSeconds': 17}
        registry.path.write_text(json.dumps(document))
        original = registry.path.read_bytes()
        restored = ModelRegistry(self.root)
        self.assertEqual(restored.path.read_bytes(), original)
        self.assertEqual(restored.policy_info(mid)['policyVersion'], 0)
        self.assertEqual(restored.profiles[mid]['requestPolicy']['timeoutSeconds'], 17)
        restored.update_policy(mid, {'maxOutputTokens': 12000}, 0)
        saved = json.loads(restored.path.read_text())
        self.assertNotIn('requestPolicy', saved['models'][mid]['connection'])
        self.assertEqual(saved['requestPolicies'][mid]['timeoutSeconds'], 17)

    def test_image_policy_only_timeout_and_custom_gemini_has_no_alias_shortcut(self):
        registry = ModelRegistry(self.root)
        image = self.register(registry, protocol='image', profile='image')
        self.assertEqual(registry.policy_info(image)['requestPolicy'], {'timeoutSeconds': 600})
        with self.assertRaises(ValueError): registry.update_policy(image, {'jsonMode': False}, 1)
        custom = self.register(registry, modelId='gemini-4-argon', name='Gemini custom')
        self.assertEqual(registry.profiles[custom]['requestPolicy'], {
            'maxOutputTokens': 16000, 'reasoningEffort': 'default', 'jsonMode': False, 'timeoutSeconds': 600})


if __name__ == '__main__':
    unittest.main()
