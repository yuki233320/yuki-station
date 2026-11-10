"""Capture request construction only; no sockets, keys, or paid calls.

These regressions pass after the separately staged deepseek-policy.patch is
applied. They intentionally expose the registered-provider bug before that.
"""
import json
import threading
import unittest
from unittest.mock import patch

from studio_client import StudioClient


class CapturedResponse:
    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def __iter__(self):
        yield b'data: {"model":"fixture-model","choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}\n'
        yield b'data: [DONE]\n'


class CaptureOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return CapturedResponse()


class RegisteredDeepSeekPolicyTests(unittest.TestCase):
    def payload(self, base='https://api.deepseek.com', model='deepseek-v4-pro',
                provider='provider-registered-fixture', limit=None):
        capture = CaptureOpener()
        client = StudioClient({'registered': {'provider': provider, 'protocol': 'chat',
                             'model': model, 'label': 'Local request fixture', 'base': base}})
        client.configure(provider, 'fixture-not-a-real-api-key')
        client.opener = capture
        # A future regression cannot accidentally turn this into a live call.
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')):
            result, _ = client.call('registered', [{'role': 'user', 'content': 'Return JSON'}],
                                    threading.Event(), max_output_tokens=limit)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(len(capture.requests), 1)
        return json.loads(capture.requests[0].data)

    def assert_deepseek(self, payload, limit=32768):
        self.assertEqual(payload['max_tokens'], limit)
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['response_format'], {'type': 'json_object'})

    def assert_generic(self, payload):
        self.assertEqual(payload['max_tokens'], 16000)
        self.assertNotIn('reasoning_effort', payload)
        self.assertNotIn('response_format', payload)

    def test_registered_official_root_and_v1_use_existing_policy(self):
        for base in ('https://api.deepseek.com', 'https://api.deepseek.com/v1',
                     'https://api.deepseek.com/', 'https://api.deepseek.com/v1/',
                     'https://API.DeepSeek.COM:443/v1'):
            for model in ('deepseek-v4-pro', 'deepseek-flash', 'deepseek-chat'):
                with self.subTest(base=base, model=model):
                    self.assert_deepseek(self.payload(base=base, model=model))

    def test_explicit_builtin_provider_behavior_is_preserved(self):
        self.assert_deepseek(self.payload(provider='deepseek'))
        # Existing built-in identity remains authoritative, as before the fix.
        self.assert_deepseek(self.payload(provider='deepseek', base='https://gateway.example.test/v1'))

    def test_similar_domains_and_url_credentials_do_not_match(self):
        for base in ('https://api.deepseek.com.evil.test', 'https://api-deepseek.com',
                     'https://evil.test/api.deepseek.com', 'https://api.deepseek.com@evil.test',
                     'https://evil.test@api.deepseek.com', 'https://api.deepseek.com./v1'):
            with self.subTest(base=base):
                self.assert_generic(self.payload(base=base))

    def test_third_party_gateways_are_not_inferred_from_model_name(self):
        for base in ('https://gateway.example.test/v1', 'https://api.bang.wiki/v1',
                     'https://grooroute.pro/v1'):
            with self.subTest(base=base):
                self.assert_generic(self.payload(base=base))

    def test_other_schemes_ports_and_api_paths_remain_generic(self):
        for base in ('http://api.deepseek.com', 'https://api.deepseek.com:8443/v1',
                     'https://api.deepseek.com/beta', 'https://api.deepseek.com/v2',
                     'https://api.deepseek.com/v1?target=other', 'https://api.deepseek.com/v1#fragment'):
            with self.subTest(base=base):
                self.assert_generic(self.payload(base=base))

    def test_official_domain_without_deepseek_model_is_not_enough(self):
        for model in ('other-model', 'not-deepseek-v4-pro', 'deepseek', 'DeepSeek-v4-pro'):
            with self.subTest(model=model):
                self.assert_generic(self.payload(model=model))

    def test_explicit_output_override_still_wins(self):
        self.assert_deepseek(self.payload(limit=4096), limit=4096)
        self.assert_deepseek(self.payload(provider='deepseek', limit=8192), limit=8192)


if __name__ == '__main__':
    unittest.main()
