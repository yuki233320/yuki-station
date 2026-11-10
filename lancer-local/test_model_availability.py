"""In-memory readiness with fake HTTP only; no paid probes or real connections."""
from copy import deepcopy
import io
import json
import threading
import unittest
import urllib.error

from live import NoRedirect
from model_availability import CATALOG_TIMEOUT_SECONDS, MAX_CATALOG_BYTES, ModelAvailability


KEY = 'fixture-credential-do-not-retain'


def profile(**changes):
    return {'provider': 'provider-one', 'base': 'https://provider.invalid/v1',
            'model': 'exact-model', 'protocol': 'chat', **changes}


def receipt(p=None, **changes):
    p = p or profile()
    return {'requestedModel': p['model'], 'returnedModel': 'provider-reported-alias',
            'protocol': p['protocol'], 'requestPolicyFingerprint': 'actual-policy',
            'responseComplete': True, 'jsonValidated': True, 'finishReason': 'stop',
            'elapsedMs': 12.5, **changes}


class Response:
    def __init__(self, payload, url=None, status=200):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.url, self.status, self.read_limits, self.closed = url, status, [], False

    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def getcode(self): return self.status
    def geturl(self): return self.url
    def read(self, limit):
        self.read_limits.append(limit)
        return self.raw[:limit]


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.lock = threading.Lock()

    def open(self, request, timeout):
        with self.lock:
            self.requests.append((request, timeout))
            value = self.responses.pop(0)
        if callable(value): value = value(request)
        if isinstance(value, Exception): raise value
        if value.url is None: value.url = request.full_url
        return value


class ModelAvailabilityTests(unittest.TestCase):
    def manager(self, *responses):
        self.http = FakeHTTP(*responses)
        return ModelAvailability(opener=self.http, clock=lambda: '2026-11-03T12:00:00Z')

    def refresh(self, manager, p=None):
        p = p or profile()
        return manager.refresh(p['provider'], {'local': p}, KEY)

    def status(self, manager, p=None, configured=True):
        return manager.status('local', p or profile(), configured)

    def test_credential_alone_and_new_process_never_imply_readiness(self):
        manager = self.manager()
        for configured in (False, True):
            value = self.status(manager, configured=configured)
            self.assertEqual(value['catalogState'], 'unknown')
            self.assertEqual(value['callState'], 'unverified')
            self.assertIsNone(value['listed']); self.assertIsNone(value['lastCall'])
            self.assertFalse(value['eligible'])
        self.assertEqual(self.http.requests, [])

    def test_directory_uses_exact_ids_and_listing_is_not_actual_call_validation(self):
        manager = self.manager(Response({'data': [{'id': 'exact-model'}, {'id': 'other'}]}))
        self.refresh(manager)
        value = self.status(manager)
        self.assertEqual((value['catalogState'], value['listed'], value['eligible']), ('listed', True, True))
        self.assertEqual(value['callState'], 'unverified')
        self.assertIsNotNone(value['catalogCheckedAt'])
        for name in ('exact', 'Exact-model', 'exact-model-extra'):
            state = manager.status(name, profile(model=name), True)
            self.assertEqual(state['catalogState'], 'not_listed')
            self.assertFalse(state['eligible'])

    def test_missing_models_are_negative_only_for_an_unpaginated_complete_catalog(self):
        for body, state in (({'data': []}, 'not_listed'),
                            ({'data': [], 'has_more': False}, 'not_listed'),
                            ({'data': [], 'has_more': False, 'next': None}, 'not_listed'),
                            ({'data': [], 'has_more': False, 'next': ''}, 'not_listed'),
                            ({'data': [], 'has_more': True}, 'unknown'),
                            ({'data': [], 'next': None}, 'not_listed'),
                            ({'data': [], 'next': 'cursor'}, 'unknown')):
            with self.subTest(body=body):
                manager = self.manager(Response(body)); self.refresh(manager)
                value = self.status(manager)
                self.assertEqual(value['catalogState'], state)
                self.assertFalse(value['eligible'])
                self.assertEqual(len(self.http.requests), 1)

    def test_nonboolean_pagination_flags_never_establish_model_absence(self):
        for value in (None, 0, 1, 'false', [], {}):
            with self.subTest(value=value):
                manager = self.manager(Response({'data': [], 'has_more': value, 'next': None}))
                self.refresh(manager)
                value = self.status(manager)
                self.assertEqual(value['catalogState'], 'unknown')
                self.assertFalse(value['catalogComplete']); self.assertIsNone(value['listed'])

    def test_network_failures_are_not_reported_as_response_truncation(self):
        for error, category in ((urllib.error.URLError('socket unavailable'), 'network_error'),
                                (ConnectionResetError('reset'), 'network_error'),
                                (OSError('connection failed'), 'network_error'),
                                (urllib.error.URLError(TimeoutError('timed out')), 'timeout')):
            with self.subTest(error=error):
                manager = self.manager(error); self.refresh(manager)
                self.assertEqual(self.status(manager)['catalogError']['errorClass'], category)
                generation = manager.capture('local', profile())
                manager.record_call('local', profile(), receipt(responseComplete=False, jsonValidated=False), error, generation)
                value = self.status(manager)
                self.assertEqual(value['lastCall']['errorClass'], category)
                self.assertEqual(value['callState'], 'failed'); self.assertFalse(value['eligible'])

    def test_multi_base_grouping_uses_only_exact_models_endpoints_and_needed_headers(self):
        manager = self.manager(Response({'data': [{'id': 'exact-model'}]}), Response({'data': []}))
        profiles = {'local': profile(), 'anthropic': profile(protocol='anthropic', model='claude'),
                    'other-base': profile(base='https://another.invalid/api', model='elsewhere'),
                    'other-provider': profile(provider='skip')}
        manager.refresh('provider-one', profiles, KEY)
        self.assertEqual([request.full_url for request, _ in self.http.requests],
                         ['https://provider.invalid/v1/models', 'https://another.invalid/api/models'])
        first, second = [dict((k.lower(), v) for k, v in request.header_items()) for request, _ in self.http.requests]
        self.assertEqual(first['authorization'], 'Bearer ' + KEY)
        self.assertEqual(first['x-api-key'], KEY)
        self.assertEqual(first['anthropic-version'], '2023-06-01')
        self.assertNotIn('x-api-key', second)
        for request, timeout in self.http.requests:
            self.assertEqual(request.get_method(), 'GET'); self.assertIsNone(request.data)
            self.assertEqual(timeout, CATALOG_TIMEOUT_SECONDS)

    def test_redirects_are_refused_without_a_second_request(self):
        default = ModelAvailability()
        self.assertTrue(any(isinstance(handler, NoRedirect) for handler in default._opener.handlers))
        manager = self.manager(Response({}, status=302))
        result = self.refresh(manager)
        self.assertEqual(result['catalogs'][0]['errorClass'], 'redirect_refused')
        self.assertEqual(len(self.http.requests), 1)
        self.assertFalse(self.status(manager)['eligible'])

    def test_malformed_oversized_and_foreign_endpoint_results_cannot_list_models(self):
        for response in (Response({'models': ['exact-model']}), Response({'data': [{'name': 'exact-model'}]}),
                         Response(b'not json'), Response(b'x' * (MAX_CATALOG_BYTES + 3)),
                         Response({'data': [{'id': 'exact-model'}]}, url='https://foreign.invalid/models')):
            with self.subTest(response=response):
                manager = self.manager(response); self.refresh(manager)
                self.assertEqual(self.status(manager)['catalogState'], 'error')
                self.assertFalse(self.status(manager)['eligible'])
                self.assertTrue(response.closed)
                if response.read_limits:
                    self.assertEqual(response.read_limits, [MAX_CATALOG_BYTES + 1])

    def test_unsupported_catalog_does_not_generate_a_probe_or_keep_error_body(self):
        error = urllib.error.HTTPError('https://provider.invalid/v1/models', 404, KEY, {}, io.BytesIO(KEY.encode()))
        manager = self.manager(error)
        result = self.refresh(manager)
        self.assertEqual(self.status(manager)['catalogState'], 'error')
        self.assertEqual(result['catalogs'][0]['httpStatus'], 404)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(KEY, json.dumps(self.status(manager)))
        self.assertEqual(len(self.http.requests), 1)

    def test_actual_success_needs_complete_validated_json_not_truncation_or_truthy_flags(self):
        for changes in ({'responseComplete': False}, {'jsonValidated': False}, {'jsonValidated': 1},
                        {'responseComplete': 1}, {'finishReason': 'length'},
                        {'requestedModel': 'different'}, {'protocol': 'responses'}):
            with self.subTest(changes=changes):
                manager = self.manager(); generation = manager.capture('local', profile())
                manager.record_call('local', profile(), receipt(**changes), generation=generation)
                value = self.status(manager)
                self.assertEqual(value['callState'], 'failed')
                self.assertFalse(value['eligible'])

    def test_success_is_bound_to_current_parameters_and_keeps_identity_as_provider_claim(self):
        manager = self.manager(); generation = manager.capture('local', profile())
        self.assertTrue(manager.record_call('local', profile(), receipt(), generation=generation))
        value = self.status(manager)
        self.assertTrue(value['eligible']); self.assertEqual(value['callState'], 'succeeded')
        self.assertEqual(value['lastCall']['returnedModel'], 'provider-reported-alias')
        self.assertEqual(value['lastCall']['modelIdentityEvidence'], 'provider_declared')
        self.assertEqual(value['lastCall']['requestPolicyFingerprint'], 'actual-policy')
        self.assertFalse(self.status(manager, configured=False)['eligible'])

    def test_hard_call_errors_remain_blocked_until_explicit_probe_succeeds(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                manager = self.manager(Response({'data': [{'id': 'exact-model'}]}), Response({'data': [{'id': 'exact-model'}]}))
                self.refresh(manager); generation = manager.capture('local', profile())
                manager.record_call('local', profile(), receipt(), generation=generation)
                manager.record_call('local', profile(), receipt(httpStatus=code), error=ValueError(KEY), generation=generation)
                self.assertFalse(self.status(manager)['eligible'])
                manager.record_call('local', profile(), receipt(), generation=generation)
                self.refresh(manager)
                self.assertFalse(self.status(manager)['eligible'], 'ordinary success or listing must not clear a hard rejection')
                manager.record_call('local', profile(), receipt(availabilityProbe=True), generation=generation)
                self.assertTrue(self.status(manager)['eligible'])

    def test_transient_failure_is_visible_without_becoming_success_or_revoking_prior_evidence(self):
        manager = self.manager(); generation = manager.capture('local', profile())
        manager.record_call('local', profile(), receipt(responseComplete=False, jsonValidated=False), TimeoutError(), generation)
        self.assertFalse(self.status(manager)['eligible'])
        manager.record_call('local', profile(), receipt(), generation=generation)
        manager.record_call('local', profile(), receipt(responseComplete=False, jsonValidated=False), TimeoutError(), generation)
        value = self.status(manager)
        self.assertEqual(value['callState'], 'failed'); self.assertEqual(value['lastCall']['errorClass'], 'timeout')
        self.assertTrue(value['eligible'])

    def test_new_complete_absence_overrides_old_success_but_a_new_explicit_probe_can_override_it(self):
        manager = self.manager(Response({'data': []}), Response({'data': []}))
        generation = manager.capture('local', profile())
        manager.record_call('local', profile(), receipt(availabilityProbe=True), generation=generation)
        self.refresh(manager)
        self.assertFalse(self.status(manager)['eligible'])
        manager.record_call('local', profile(), receipt(), generation=generation)
        self.assertFalse(self.status(manager)['eligible'])
        manager.record_call('local', profile(), receipt(availabilityProbe=True), generation=generation)
        value = self.status(manager)
        self.assertTrue(value['eligible']); self.assertEqual(value['catalogState'], 'not_listed')
        self.assertIn('目录未列出，但随后显式验证成功', value['reason'])
        self.refresh(manager)
        self.assertFalse(self.status(manager)['eligible'])

    def test_endpoint_change_invalidates_call_evidence_and_old_calls_cannot_rebind(self):
        old = profile(); new = profile(base='https://replacement.invalid/v1')
        manager = self.manager(); generation = manager.capture('local', old)
        manager.record_call('local', old, receipt(old), generation=generation)
        manager.capture('local', new)
        self.assertFalse(manager.record_call('local', old, receipt(old), generation=generation))
        self.assertEqual(self.status(manager, new)['callState'], 'unverified')
        self.assertFalse(self.status(manager, new)['eligible'])
        self.assertTrue(manager.record_call('local', new, receipt(new), generation=generation))
        self.assertTrue(self.status(manager, new)['eligible'])

    def test_directory_errors_or_partial_absence_cannot_revive_success_older_than_deletion(self):
        manager = self.manager(Response({'data': []}), TimeoutError(), Response({'data': [], 'next': 'page2'}))
        generation = manager.capture('local', profile())
        manager.record_call('local', profile(), receipt(), generation=generation)
        self.refresh(manager); self.assertFalse(self.status(manager)['eligible'])
        self.refresh(manager)
        self.assertEqual(self.status(manager)['catalogState'], 'error')
        self.assertFalse(self.status(manager)['eligible'])
        self.refresh(manager)
        self.assertEqual(self.status(manager)['catalogState'], 'unknown')
        self.assertFalse(self.status(manager)['eligible'])

    def test_a_later_explicit_listing_or_probe_overrides_a_retained_directory_deletion(self):
        manager = self.manager(Response({'data': []}), Response({'data': [{'id': 'exact-model'}], 'next': 'page2'}),
                               Response({'data': []}), TimeoutError())
        generation = manager.capture('local', profile())
        self.refresh(manager); self.assertFalse(self.status(manager)['eligible'])
        self.refresh(manager); self.assertTrue(self.status(manager)['eligible'])
        self.refresh(manager); self.assertFalse(self.status(manager)['eligible'])
        manager.record_call('local', profile(), receipt(availabilityProbe=True), generation=generation)
        self.refresh(manager)
        self.assertEqual(self.status(manager)['catalogState'], 'error')
        self.assertTrue(self.status(manager)['eligible'])

    def test_parameter_update_clears_only_old_call_rejection_and_display_rename_does_not(self):
        manager = self.manager(Response({'data': [{'id': 'exact-model'}]})); self.refresh(manager)
        generation = manager.capture('local', profile())
        manager.record_call('local', profile(), receipt(httpStatus=400), ValueError(), generation)
        self.assertFalse(self.status(manager, profile(label='renamed'))['eligible'])
        updated = profile(requestPolicy={'maxOutputTokens': 8192, 'reasoningEffort': 'low'})
        manager.capture('local', updated)
        value = manager.status('local', updated, True)
        self.assertTrue(value['eligible']); self.assertEqual(value['callState'], 'unverified')
        self.assertFalse(manager.record_call('local', profile(), receipt(), generation=generation))

    def test_reset_and_forget_monotonically_invalidate_old_call_evidence(self):
        manager = self.manager(Response({'data': [{'id': 'exact-model'}]})); self.refresh(manager)
        old = manager.capture('local', profile()); manager.record_call('local', profile(), receipt(), generation=old)
        reset = manager.reset('provider-one'); self.assertGreater(reset, old)
        self.assertFalse(manager.record_call('local', profile(), receipt(), generation=old))
        self.assertFalse(self.status(manager)['eligible'])
        manager.forget(); forgotten = manager.generation('provider-one')
        self.assertGreater(forgotten, reset)
        newer = manager.reset('provider-one'); self.assertGreater(newer, forgotten)
        self.assertFalse(manager.record_call('local', profile(), receipt(), generation=forgotten))

    def test_frozen_profile_status_never_rebinds_current_configuration(self):
        old = profile(); current = profile(base='https://current.invalid/v1')
        manager = self.manager(); generation = manager.capture('local', current)
        manager.record_call('local', current, receipt(current), generation=generation)
        frozen = manager.status('local', old, True, bind=False)
        self.assertEqual(frozen['callState'], 'unverified')
        self.assertFalse(frozen['eligible'])
        self.assertFalse(manager.record_call('local', old, receipt(old), generation=generation))
        self.assertTrue(manager.record_call('local', current, receipt(current), generation=generation))
        self.assertEqual(manager.status('local', current, True)['callState'], 'succeeded')

    def test_returned_model_can_be_unknown_without_fabricating_identity(self):
        manager = self.manager(); generation = manager.capture('local', profile())
        manager.record_call('local', profile(), receipt(returnedModel=None), generation=generation)
        value = self.status(manager)
        self.assertTrue(value['eligible']); self.assertIsNone(value['lastCall']['returnedModel'])
        self.assertEqual(value['lastCall']['modelIdentityEvidence'], 'provider_declared')

    def test_refresh_does_not_hold_lock_and_discards_results_after_key_rotation(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(request):
            entered.set()
            if not release.wait(3): raise AssertionError('test response not released')
            return Response({'data': [{'id': 'exact-model'}]})
        manager = self.manager(delayed); results = []
        thread = threading.Thread(target=lambda: results.append(self.refresh(manager)))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            reset_finished = threading.Event()
            resetter = threading.Thread(target=lambda: (manager.reset('provider-one'), reset_finished.set()))
            resetter.start()
            self.assertTrue(reset_finished.wait(1), 'network I/O held state lock')
            resetter.join(3)
        finally:
            release.set(); thread.join(3)
        self.assertTrue(results[0]['discarded'])
        self.assertEqual(self.status(manager)['catalogState'], 'unknown')

    def test_refresh_with_stale_credential_captured_before_entry_never_sends_get(self):
        manager = self.manager()
        captured = manager.generation('provider-one')
        manager.reset('provider-one')
        result = manager.refresh('provider-one', {'local': profile()}, KEY, generation=captured)
        self.assertTrue(result['discarded']); self.assertEqual(result['generation'], captured)
        self.assertEqual(result['catalogs'], []); self.assertEqual(self.http.requests, [])
        self.assertEqual(self.status(manager)['catalogState'], 'unknown')

    def test_refresh_discards_changed_endpoint_bindings(self):
        p = profile(); profiles = {'local': p}
        def changed(request):
            profiles['local'] = profile(base='https://changed.invalid/v1')
            return Response({'data': [{'id': 'exact-model'}]})
        manager = self.manager(changed)
        result = manager.refresh('provider-one', profiles, KEY)
        self.assertTrue(result['discarded'])
        self.assertEqual(manager.status('local', profiles['local'], True)['catalogState'], 'unknown')

    def test_older_directory_request_cannot_overwrite_a_newer_observation(self):
        entered, release = threading.Event(), threading.Event()
        def old_response(request):
            entered.set()
            if not release.wait(3): raise AssertionError('test response not released')
            return Response({'data': [{'id': 'exact-model'}]})
        manager = self.manager(old_response, Response({'data': []})); old_results = []
        thread = threading.Thread(target=lambda: old_results.append(self.refresh(manager)))
        thread.start()
        try:
            self.assertTrue(entered.wait(3)); self.refresh(manager)
        finally:
            release.set(); thread.join(3)
        self.assertTrue(old_results[0]['discarded'])
        self.assertEqual(self.status(manager)['catalogState'], 'not_listed')

    def test_provider_metadata_is_only_a_declaration_and_unknown_fields_are_not_retained(self):
        manager = self.manager(Response({'data': [{'id': 'exact-model', 'max_output_tokens': 4096,
                                                 'supported_reasoning_efforts': ['low', 'high'], 'secret_note': KEY}]}))
        result = self.refresh(manager); value = self.status(manager)
        self.assertEqual(value['providerDeclaredMetadata'], {'max_output_tokens': 4096, 'supported_reasoning_efforts': ['low', 'high']})
        self.assertNotIn(KEY, json.dumps(result)); self.assertNotIn(KEY, json.dumps(value))
        value['providerDeclaredMetadata']['supported_reasoning_efforts'].append('invented')
        self.assertEqual(self.status(manager)['providerDeclaredMetadata']['supported_reasoning_efforts'], ['low', 'high'])


if __name__ == '__main__':
    unittest.main()
