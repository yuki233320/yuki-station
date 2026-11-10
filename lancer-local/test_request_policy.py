"""Pure request construction and validation; no transport or model calls."""
import copy
import unittest

from request_policy import build_request, effective_policy, profile_fingerprint


class RequestPolicyTests(unittest.TestCase):
    def profile(self, protocol='chat', **changes):
        return {'provider': 'provider-fixture', 'base': 'https://gateway.example.test/v1',
                'model': 'model-fixture', 'protocol': protocol, 'label': 'Fixture', **changes}

    def request(self, profile, **kwargs):
        return build_request(profile, [{'role': 'user', 'content': 'Return a JSON object'}], 'SYSTEM', **kwargs)

    def test_protocol_defaults_preserve_existing_payload_fields(self):
        path, payload, policy = self.request(self.profile())
        self.assertEqual(path, '/chat/completions')
        self.assertEqual(payload['max_tokens'], 16000)
        self.assertEqual(payload['stream_options'], {'include_usage': True})
        self.assertEqual(payload['messages'][0], {'role': 'system', 'content': 'SYSTEM'})
        self.assertNotIn('reasoning_effort', payload)
        self.assertNotIn('response_format', payload)
        self.assertEqual(policy, {'maxOutputTokens': 16000, 'reasoningEffort': 'default', 'jsonMode': False, 'timeoutSeconds': 600})
        path, payload, _ = self.request(self.profile('responses'))
        self.assertEqual(path, '/responses')
        self.assertFalse(payload['store'])
        self.assertEqual(payload['max_output_tokens'], 32768)
        self.assertEqual(payload['reasoning'], {'effort': 'medium'})
        self.assertEqual(payload['text']['format'], {'type': 'json_object'})
        path, payload, _ = self.request(self.profile('anthropic'))
        self.assertEqual(path, '/messages')
        self.assertEqual(payload['max_tokens'], 65536)
        self.assertEqual(payload['output_config'], {'effort': 'medium'})
        self.assertEqual(payload['system'], 'SYSTEM')
        self.assertNotIn('response_format', payload)

    def test_official_deepseek_defaults_and_explicit_overrides(self):
        for base in ('https://api.deepseek.com', 'https://api.deepseek.com/v1', 'https://API.DeepSeek.COM:443/v1'):
            with self.subTest(base=base):
                profile = self.profile(base=base, model='deepseek-v4-pro')
                policy = effective_policy(profile)
                self.assertEqual((policy['maxOutputTokens'], policy['reasoningEffort'], policy['jsonMode']), (32768, 'low', True))
                profile['requestPolicy'] = {'reasoningEffort': 'default', 'jsonMode': False, 'maxOutputTokens': 12000}
                _, payload, _ = self.request(profile)
                self.assertEqual(payload['max_tokens'], 12000)
                self.assertNotIn('reasoning_effort', payload)
                self.assertNotIn('response_format', payload)
        self.assertEqual(effective_policy(self.profile(provider='deepseek'))['reasoningEffort'], 'low')

    def test_custom_gemini_and_third_party_deepseek_are_not_guessed(self):
        for model in ('gemini-4-argon', 'deepseek-v4-pro'):
            self.assertEqual(effective_policy(self.profile(model=model))['reasoningEffort'], 'default')
        for base in ('https://api.deepseek.com.evil.test/v1', 'https://evil.test@api.deepseek.com',
                     'https://api.deepseek.com@evil.test', 'http://api.deepseek.com',
                     'https://api.deepseek.com/beta', 'https://api.deepseek.com/v1?key=x'):
            with self.subTest(base=base):
                self.assertEqual(effective_policy(self.profile(base=base, model='deepseek-flash'))['reasoningEffort'], 'default')
        self.assertEqual(effective_policy(self.profile(base='https://api.deepseek.com', model='other'))['reasoningEffort'], 'default')

    def test_effort_vocabulary_is_protocol_specific_without_capability_claim(self):
        for protocol in ('chat', 'responses'):
            for effort in ('default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
                profile = self.profile(protocol, requestPolicy={'reasoningEffort': effort})
                self.assertEqual(effective_policy(profile)['reasoningEffort'], effort)
        for effort in ('default', 'low', 'medium', 'high', 'xhigh', 'max'):
            profile = self.profile('anthropic', requestPolicy={'reasoningEffort': effort})
            _, payload, _ = self.request(profile)
            if effort == 'default': self.assertNotIn('output_config', payload)
            else: self.assertEqual(payload['output_config'], {'effort': effort})
        for effort in ('none', 'minimal', 'adaptive', None, {}, True):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                effective_policy(self.profile('anthropic', requestPolicy={'reasoningEffort': effort}))

    def test_json_mode_false_omits_format_and_anthropic_true_is_rejected(self):
        for protocol, field in (('chat', 'response_format'), ('responses', 'text')):
            _, payload, _ = self.request(self.profile(protocol, requestPolicy={'jsonMode': False}))
            self.assertNotIn(field, payload)
            _, payload, _ = self.request(self.profile(protocol, requestPolicy={'jsonMode': True}))
            self.assertIn(field, payload)
        with self.assertRaises(ValueError):
            effective_policy(self.profile('anthropic', requestPolicy={'jsonMode': True}))
        for value in (1, 0, 'true', None):
            with self.assertRaises(ValueError): effective_policy(self.profile(requestPolicy={'jsonMode': value}))

    def test_timeout_is_finite_positive_and_supports_fractional_test_deadlines(self):
        self.assertEqual(effective_policy(self.profile(), default_timeout=.25)['timeoutSeconds'], .25)
        self.assertEqual(effective_policy(self.profile(requestPolicy={'timeoutSeconds': .5}))['timeoutSeconds'], .5)
        self.assertEqual(effective_policy(self.profile(requestPolicy={'timeoutSeconds': 600.0}))['timeoutSeconds'], 600)
        for value in (True, False, 0, -1, float('nan'), float('inf'), 3600.1, '600', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_policy(self.profile(requestPolicy={'timeoutSeconds': value}))

    def test_output_tokens_are_strict_integer_technical_limits(self):
        for value in (1, 65536, 393216):
            profile = self.profile(requestPolicy={'maxOutputTokens': value})
            self.assertEqual(self.request(profile)[1]['max_tokens'], value)
        for value in (0, -1, 393217, 1.0, True, False, None, '16000'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_policy(self.profile(requestPolicy={'maxOutputTokens': value}))
        profile = self.profile('responses', requestPolicy={'maxOutputTokens': 10000})
        _, payload, policy = self.request(profile, max_output_tokens=20000)
        self.assertEqual(payload['max_output_tokens'], 20000)
        self.assertEqual(policy['maxOutputTokens'], 20000)
        self.assertEqual(profile['requestPolicy']['maxOutputTokens'], 10000)

    def test_image_only_accepts_timeout_and_retains_image_parameters(self):
        profile = self.profile('image', requestPolicy={'timeoutSeconds': 20})
        path, payload, policy = self.request(profile)
        self.assertEqual(path, '/images/generations')
        self.assertEqual(policy, {'timeoutSeconds': 20})
        self.assertEqual((payload['n'], payload['size'], payload['quality'], payload['output_format']),
                         (1, '1536x1024', 'medium', 'png'))
        for key, value in (('maxOutputTokens', 100), ('reasoningEffort', 'default'), ('jsonMode', False)):
            with self.assertRaises(ValueError): effective_policy(self.profile('image', requestPolicy={key: value}))
        with self.assertRaises(ValueError): self.request(profile, max_output_tokens=100)

    def test_unknown_parameters_headers_endpoints_and_secrets_are_rejected(self):
        for value in ({'headers': {}}, {'endpoint': 'https://example.test'}, {'apiKey': 'fixture'},
                      {'budget': 100}, {'temperature': .5}, {'reasoning': {'effort': 'low'}}, None, [], 'policy'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_policy(self.profile(requestPolicy=value))

    def test_builder_does_not_mutate_profile_or_messages(self):
        profile = self.profile('responses', requestPolicy={'reasoningEffort': 'default'})
        messages = [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'x'}]}]
        before = copy.deepcopy((profile, messages))
        _, payload, policy = build_request(profile, messages, 'system')
        payload['input'][0]['content'][0]['text'] = 'changed'
        policy['maxOutputTokens'] = 1
        self.assertEqual((profile, messages), before)
        self.assertNotIn('reasoning', payload)

    def test_fingerprint_ignores_presentation_and_credentials_but_tracks_route_and_policy(self):
        original = self.profile()
        fingerprint = profile_fingerprint(original)
        self.assertEqual(fingerprint, profile_fingerprint({**original, 'label': 'Renamed', 'color': '#000',
                                                          'apiKey': 'fixture', 'credentials': {'key': 'fixture'}}))
        for change in ({'provider': 'other'}, {'base': 'https://other.example/v1'}, {'model': 'other'},
                       {'protocol': 'responses'}, {'requestPolicy': {'timeoutSeconds': 10}},
                       {'requestPolicy': {'maxOutputTokens': 20000}}):
            self.assertNotEqual(fingerprint, profile_fingerprint({**original, **change}))
        self.assertEqual(fingerprint, profile_fingerprint({**original, 'requestPolicy': effective_policy(original)}))


if __name__ == '__main__':
    unittest.main()
