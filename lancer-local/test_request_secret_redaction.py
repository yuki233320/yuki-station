"""Request-local credential redaction under rotation; fake HTTP only."""
import base64
import io
import json
import threading
import unittest
import urllib.error

from live import Cancelled
from studio_client import ModelResponseError, StudioClient


OLD = 'fixture-old-credential-without-sk-prefix-111'
OTHER_OLD = 'fixture-other-old-credential-222'
NEW = 'fixture-new-credential-333'
OTHER_NEW = 'fixture-other-new-credential-444'
MASK = '[已隐藏]'


def event(text=None, finish=None, **extra):
    value = {'model': 'provider-model', 'choices': [{'delta': {'content': text or ''}, 'finish_reason': finish}], **extra}
    return b'data: ' + json.dumps(value).encode() + b'\n\n'


class Response:
    def __init__(self, frames=(), body=b''):
        self.frames, self.body, self.closed = frames, body, False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def __iter__(self):
        for frame in self.frames:
            if isinstance(frame, Exception): raise frame
            yield frame
    def read(self, limit): return self.body[:limit]


class Opener:
    def __init__(self, client, response=None, error=None, forget=False):
        self.client, self.response, self.error, self.forget = client, response, error, forget
        self.count = 0
    def open(self, request, timeout):
        self.count += 1
        if self.forget:
            self.client.forget()
        else:
            self.client.configure('p', NEW)
            self.client.configure('q', OTHER_NEW)
        if self.error is not None: raise self.error
        return self.response


class RequestSecretRedactionTests(unittest.TestCase):
    def client(self, protocol='chat', secret=OLD):
        profiles = {mid: {'provider': mid, 'base': 'https://provider.invalid/v1',
                         'model': 'model-' + mid, 'protocol': protocol if mid == 'p' else 'chat', 'label': mid}
                    for mid in ('p', 'q')}
        client = StudioClient(profiles)
        client.configure('p', secret); client.configure('q', OTHER_OLD)
        return client

    def call(self, client, on_delta=None, cancel=None):
        return client.call('p', [{'role': 'user', 'content': 'Return JSON'}], cancel or threading.Event(), on_delta)

    def assert_clean(self, value, secrets=(OLD, OTHER_OLD)):
        text = json.dumps(value, ensure_ascii=False, default=str)
        for secret in secrets:
            self.assertNotIn(secret, text)
            self.assertNotIn(json.dumps(secret)[1:-1], text)

    def assert_no_retained_snapshot(self, client, error=None):
        self.assertEqual(set(client.credentials.values()).intersection({OLD, OTHER_OLD}), set())
        self.assertFalse(any('redactor' in name or 'captured_keys' in name for name in vars(client)))
        self.assert_clean(client.model_states())
        if error is not None:
            self.assert_clean({'args': error.args, 'attributes': vars(error), 'rendered': str(error)})

    def test_http_error_after_rotation_cleans_both_captured_provider_credentials(self):
        client = self.client()
        error = urllib.error.HTTPError('https://provider.invalid/v1/chat/completions', 403,
                                      'Denied ' + OLD, {}, io.BytesIO((OLD + ' / ' + OTHER_OLD).encode()))
        client.opener = Opener(client, error=error)
        with self.assertRaises(ValueError) as raised: self.call(client)
        observed = raised.exception
        self.assertIn('403', str(observed)); self.assertIn(MASK, str(observed))
        self.assertEqual(observed.receipt['httpStatus'], 403)
        self.assertFalse(observed.receipt['availabilityEvidenceApplied'])
        self.assertEqual(client.availability.status('p', client._profile(client.profiles['p']), True)['callState'], 'unverified')
        self.assert_no_retained_snapshot(client, observed)

    def test_sse_error_payload_is_cleaned_before_it_can_become_a_saved_error(self):
        client = self.client()
        response = Response([b'data: ' + json.dumps({'error': {'message': OLD, 'detail': OTHER_OLD}}).encode()])
        client.opener = Opener(client, response=response)
        with self.assertRaises(ValueError) as raised: self.call(client)
        self.assertIn(MASK, str(raised.exception))
        self.assertFalse(raised.exception.receipt['jsonValidated'])
        self.assertTrue(response.closed); self.assert_no_retained_snapshot(client, raised.exception)

    def test_stream_redaction_handles_secrets_split_across_many_deltas(self):
        client = self.client(); raw = '{"value":"before ' + OLD + ' after","other":"' + OTHER_OLD + '"}'
        response = Response([event(raw[i:i+3]) for i in range(0, len(raw), 3)] + [event(finish='stop'), b'data: [DONE]\n'])
        client.opener = Opener(client, response=response); deltas = []
        result, receipt = self.call(client, deltas.append)
        self.assertIn('', deltas, 'withheld text still signals stream progress')
        self.assert_clean(deltas); self.assert_clean(''.join(deltas))
        self.assertEqual(json.loads(''.join(deltas)), result)
        self.assertEqual(result, {'value': 'before ' + MASK + ' after', 'other': MASK})
        self.assertTrue(receipt['responseComplete']); self.assertTrue(receipt['jsonValidated'])
        self.assertFalse(receipt['availabilityEvidenceApplied'])

    def test_success_cleans_nested_values_keys_and_receipt_metadata_before_return(self):
        client = self.client()
        body = {OLD: {'rows': [OTHER_OLD, {'nested': 'echo ' + OLD}], 'count': 3, 'ok': True}}
        response = Response([event(json.dumps(body), finish='stop', model=OLD, usage={'provider_note': OTHER_OLD})])
        client.opener = Opener(client, response=response)
        result, receipt = self.call(client)
        self.assertEqual(result[MASK]['rows'], [MASK, {'nested': 'echo ' + MASK}])
        self.assertIs(result[MASK]['ok'], True); self.assertEqual(result[MASK]['count'], 3)
        self.assertEqual(receipt['returnedModel'], MASK)
        self.assertEqual(receipt['usage']['provider_note'], MASK)
        self.assert_clean((result, receipt)); self.assert_no_retained_snapshot(client)

    def test_invalid_complete_json_and_incomplete_response_keep_only_clean_raw_evidence(self):
        for finish, raw_field, body in (
            ('stop', 'raw_text', '{"value":"' + OLD + '"} trailing ' + OTHER_OLD),
            ('length', 'partial_text', '{"value":"' + OLD + ' ' + OTHER_OLD),
        ):
            with self.subTest(finish=finish):
                client = self.client(); deltas = []
                client.opener = Opener(client, response=Response([event(body, finish=finish)]))
                with self.assertRaises(ModelResponseError) as raised: self.call(client, deltas.append)
                error = raised.exception
                self.assertIn(MASK, getattr(error, raw_field))
                self.assertEqual(error.receipt['finishReason'], finish)
                self.assertEqual(error.receipt['responseComplete'], finish == 'stop')
                self.assertFalse(error.receipt['jsonValidated'])
                self.assert_clean(''.join(deltas)); self.assert_no_retained_snapshot(client, error)

    def test_url_error_nested_reason_and_oserror_slots_keep_types_without_credentials(self):
        client = self.client()
        cause = OSError(5, 'network reason ' + OLD, '/tmp/' + OTHER_OLD)
        client.opener = Opener(client, error=urllib.error.URLError(cause))
        with self.assertRaises(urllib.error.URLError) as raised: self.call(client)
        error = raised.exception
        self.assertIsInstance(error.reason, OSError); self.assertEqual(error.reason.errno, 5)
        self.assertIn(MASK, str(error.reason)); self.assert_clean(str(error.reason))
        self.assert_clean((error.reason.strerror, error.reason.filename))
        self.assert_no_retained_snapshot(client, error)

    def test_invalid_sse_json_scrubs_decode_error_doc_without_replacing_exception_type(self):
        client = self.client()
        response = Response([b'data: ' + ('{"message":"' + OLD).encode()])
        client.opener = Opener(client, response=response)
        with self.assertRaises(json.JSONDecodeError) as raised: self.call(client)
        self.assertIn(MASK, raised.exception.doc)
        self.assert_no_retained_snapshot(client, raised.exception)

    def test_cancellation_flushes_safe_progress_and_preserves_clean_partial_text_after_forget(self):
        client = self.client(); cancel = threading.Event(); deltas = []
        response = Response([event('{"value":"' + OLD + ' ' + OTHER_OLD), event(finish='stop')])
        client.opener = Opener(client, response=response, forget=True)
        def progress(text):
            deltas.append(text); cancel.set()
        with self.assertRaises(Cancelled) as raised: self.call(client, progress, cancel)
        self.assertIn(MASK, raised.exception.partial_text)
        self.assert_clean(''.join(deltas)); self.assertFalse(raised.exception.receipt['availabilityEvidenceApplied'])
        self.assertEqual(client.credentials, {}); self.assert_no_retained_snapshot(client, raised.exception)

    def test_json_escaped_credential_spellings_are_redacted_in_stream_and_parsed_result(self):
        secret = 'fixture-"quoted\\credential-111'
        client = self.client(secret=secret); body = json.dumps({'value': secret}); deltas = []
        client.opener = Opener(client, response=Response([event(body[:10]), event(body[10:], finish='stop')]))
        result, receipt = self.call(client, deltas.append)
        self.assertEqual(result, {'value': MASK})
        self.assert_clean(''.join(deltas), (secret,)); self.assert_clean(receipt, (secret,))

    def test_stream_does_not_mask_sk_prefix_early_and_expose_the_suffix(self):
        secret = 'sk-fixturelongoldcredential1234567890'
        client = self.client(secret=secret); raw = json.dumps({'value': secret}); deltas = []
        client.opener = Opener(client, response=Response([event(raw[i:i+2]) for i in range(0,len(raw),2)] + [event(finish='stop')]))
        result, _ = self.call(client, deltas.append)
        self.assertEqual(json.loads(''.join(deltas)), {'value': MASK})
        self.assertEqual(result, {'value': MASK})

    def test_stream_waits_for_a_longer_key_when_another_captured_key_is_its_prefix(self):
        short = 'sharedcredential'
        long = short + '-long-private-suffix'
        client = self.client(secret=long); client.configure('q', short)
        raw = json.dumps({'value': long}); deltas = []
        client.opener = Opener(client, response=Response([event(raw[i:i+2]) for i in range(0,len(raw),2)] + [event(finish='stop')]))
        result, _ = self.call(client, deltas.append)
        self.assertEqual(json.loads(''.join(deltas)), {'value': MASK})
        self.assertEqual(result, {'value': MASK})

    def test_image_bytes_are_unchanged_while_json_receipt_strings_are_cleaned(self):
        client = self.client(protocol='image')
        image = b'\x89PNG\r\n\x1a\n' + OLD.encode()
        payload = {'model': OLD, 'usage': {'note': OTHER_OLD},
                   'data': [{'b64_json': base64.b64encode(image).decode()}]}
        client.opener = Opener(client, response=Response(body=json.dumps(payload).encode()))
        result, receipt = self.call(client)
        self.assertEqual(result['imageBytes'], image)
        self.assertEqual(receipt['returnedModel'], MASK); self.assert_clean(receipt)


if __name__ == '__main__':
    unittest.main()
