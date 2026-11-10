"""Current-process model evidence, separate from saved connection metadata.

Credentials are used only by the local refresh request, never retained here.
Directory listings and returned model names are provider declarations, not
independent identity verification. Refresh never makes a generation request.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

from live import Cancelled
from provider_transport import ProviderTransport, provider_opener
from request_policy import profile_fingerprint


MAX_CATALOG_BYTES = 2 * 1024 * 1024
CATALOG_TIMEOUT_SECONDS = 30
HARD_HTTP_STATUSES = frozenset({400, 401, 403, 404, 422})
INCOMPLETE_REASONS = frozenset({'length', 'max_tokens', 'response.incomplete',
                               'response.failed', 'content_filter', 'tool_calls', 'function_call'})
DECLARED_FIELDS = ('max_output_tokens', 'max_tokens', 'context_length',
                   'supported_reasoning_efforts', 'reasoning_efforts', 'reasoning_effort')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _http_status(error, receipt=None):
    value = (receipt or {}).get('httpStatus')
    if type(value) is int and 100 <= value <= 599:
        return value
    for name in ('code', 'status_code'):
        try:
            value = getattr(error, name, None)
        except (AttributeError, KeyError):
            continue
        if type(value) is int and 100 <= value <= 599:
            return value
    return None


def _error_class(error, receipt=None):
    receipt = receipt or {}
    if _http_status(error, receipt) is not None:
        return 'http_error'
    if isinstance(error, (TimeoutError, socket.timeout)) or (isinstance(error, urllib.error.URLError)
            and isinstance(error.reason, (TimeoutError, socket.timeout))):
        return 'timeout'
    if isinstance(error, Cancelled):
        return 'cancelled'
    if isinstance(error, (urllib.error.URLError, OSError)):
        return 'network_error'
    reason = receipt.get('finishReason')
    if (isinstance(reason, str) and reason in INCOMPLETE_REASONS) or receipt.get('responseComplete') is not True:
        return 'incomplete_response'
    if receipt.get('jsonValidated') is not True:
        return 'invalid_json'
    return type(error).__name__ if error is not None else 'call_error'


def _parse_catalog(raw):
    document = json.loads(raw.decode('utf8'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid JSON number')))
    if not isinstance(document, dict) or not isinstance(document.get('data'), list):
        raise ValueError('Catalog must contain a data list')
    models, metadata = [], {}
    for row in document['data']:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
            # A malformed row must not make an incomplete directory look like
            # proof that another model is absent.
            raise ValueError('Catalog model id is invalid')
        mid = row['id']
        if mid not in metadata:
            models.append(mid)
        metadata[mid] = {key: deepcopy(row[key]) for key in DECLARED_FIELDS if key in row}
    # Only an explicit boolean false closes has_more. Truthy/falsy non-booleans
    # cannot turn an unknown page into authoritative proof of absence.
    no_more = ('has_more' not in document or
               (type(document['has_more']) is bool and document['has_more'] is False))
    next_page = document.get('next')
    complete = no_more and (next_page is None or (type(next_page) is str and next_page == ''))
    return {'modelIds': models, 'declaredMetadata': metadata, 'complete': complete}


class ModelAvailability:
    def __init__(self, *, opener=None, clock=None):
        self._lock = threading.RLock()
        self._opener = opener if opener is not None else provider_opener()
        self._clock = clock or _now
        self._serial = 0
        self._generations = {}
        self._bindings = {}
        self._catalogs = {}
        self._complete_catalogs, self._last_listed = {}, {}
        self._refresh_versions = {}
        self._calls, self._successes, self._blocked = {}, {}, {}

    def _next(self):
        self._serial += 1
        return self._serial

    def generation(self, provider):
        with self._lock:
            return self._generations.setdefault(provider, 0)

    def reset(self, provider):
        with self._lock:
            generation = self._next()
            self._generations[provider] = generation
            for mid, binding in list(self._bindings.items()):
                if binding[0] == provider:
                    self._bindings.pop(mid, None)
                    for records in (self._calls, self._successes, self._blocked):
                        records.pop(mid, None)
            for records in (self._catalogs, self._complete_catalogs, self._last_listed, self._refresh_versions):
                for key in list(records):
                    if key[0] == provider:
                        records.pop(key, None)
            return generation

    def forget(self):
        with self._lock:
            generation = self._next()
            for provider in self._generations:
                self._generations[provider] = generation
            self._bindings.clear()
            self._catalogs.clear()
            self._complete_catalogs.clear()
            self._last_listed.clear()
            self._refresh_versions.clear()
            self._calls.clear()
            self._successes.clear()
            self._blocked.clear()

    def _observe(self, mid, profile):
        binding = (profile['provider'], profile_fingerprint(profile))
        if self._bindings.get(mid) != binding:
            self._bindings[mid] = binding
            for records in (self._calls, self._successes, self._blocked):
                records.pop(mid, None)
        return binding

    def capture(self, mid, profile):
        """Call before I/O with the exact effective profile used by the request."""
        with self._lock:
            self._observe(mid, profile)
            return self.generation(profile['provider'])

    def _fetch_catalog(self, base, protocols, key):
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError('Invalid configured endpoint')
        url = base + '/models'
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json',
                   'User-Agent': 'YukiModelDirectory/1.0'}
        if 'anthropic' in protocols:
            headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        request = urllib.request.Request(url, method='GET', headers=headers)
        control = ProviderTransport(threading.Event(), CATALOG_TIMEOUT_SECONDS)
        request.transport = control
        with control:
            with self._opener.open(request, timeout=CATALOG_TIMEOUT_SECONDS) as response:
                status = response.getcode()
                if type(status) is int and 300 <= status < 400:
                    raise urllib.error.HTTPError(url, status, 'Redirect refused', None, None)
                if response.geturl() != url:
                    raise ValueError('Redirected directory response refused')
                if status != 200:
                    raise urllib.error.HTTPError(url, status, 'Directory request failed', None, None)
                raw = response.read(MAX_CATALOG_BYTES + 1)
                control.check()
                if len(raw) > MAX_CATALOG_BYTES:
                    raise ValueError('Model catalog exceeds 2 MiB')
                return _parse_catalog(raw)

    def refresh(self, provider, profiles, key, *, generation=None):
        """GET exact configured base/models only; no lock is held during I/O."""
        with self._lock:
            current_generation = self.generation(provider)
            if generation is not None and generation != current_generation:
                return {'provider': provider, 'generation': generation, 'discarded': True, 'catalogs': []}
            generation = current_generation
            selected = {mid: deepcopy(profile) for mid, profile in list(profiles.items())
                        if profile.get('provider') == provider}
            groups = {}
            for mid, profile in selected.items():
                binding = self._observe(mid, profile)
                group = groups.setdefault(profile['base'], {'profiles': {}, 'protocols': set()})
                group['profiles'][mid] = binding
                group['protocols'].add(profile['protocol'])
            for base, group in groups.items():
                group['version'] = self._next()
                self._refresh_versions[(provider, base)] = group['version']
        results, discarded = [], False
        for base, group in groups.items():
            with self._lock:
                if self.generation(provider) != generation:
                    discarded = True
                    break
            try:
                result = {'base': base, 'url': base + '/models', 'state': 'success',
                          **self._fetch_catalog(base, group['protocols'], key), 'errorClass': None, 'httpStatus': None}
            except Exception as error:
                code = _http_status(error)
                category = ('redirect_refused' if code is not None and 300 <= code < 400 else
                            'http_error' if code is not None else
                            _error_class(error) if isinstance(error, (urllib.error.URLError, OSError)) else
                            'invalid_catalog' if isinstance(error, (ValueError, UnicodeError)) else type(error).__name__)
                # Never retain a provider error body or exception text: either
                # may echo the supplied credential or authorization headers.
                result = {'base': base, 'url': base + '/models', 'state': 'error', 'complete': False,
                          'modelIds': [], 'declaredMetadata': {}, 'errorClass': category, 'httpStatus': code}
            result['checkedAt'] = self._clock()
            with self._lock:
                current = all(mid in profiles and profiles[mid].get('provider') == provider
                              and (provider, profile_fingerprint(profiles[mid])) == binding
                              and self._bindings.get(mid) == binding
                              for mid, binding in group['profiles'].items())
                if (self.generation(provider) != generation or not current
                        or self._refresh_versions.get((provider, base)) != group['version']):
                    discarded = True
                    results.append({'base': base, 'discarded': True})
                    continue
                observed = {**deepcopy(result), '_generation': generation, '_sequence': self._next()}
                self._catalogs[(provider, base)] = observed
                if result['state'] == 'success':
                    if result['complete']:
                        self._complete_catalogs[(provider, base)] = observed
                    for model_id in result['modelIds']:
                        self._last_listed[(provider, base, model_id)] = observed['_sequence']
                results.append(deepcopy(result))
        return {'provider': provider, 'generation': generation, 'discarded': discarded, 'catalogs': results}

    def record_call(self, mid, profile, receipt, error=None, generation=None):
        """Record only the captured profile/generation; callers validate JSON first."""
        receipt = receipt if isinstance(receipt, dict) else {}
        provider = profile['provider']
        binding = (provider, profile_fingerprint(profile))
        with self._lock:
            current = self.generation(provider)
            if generation is not None and generation != current:
                return False
            if mid in self._bindings and self._bindings[mid] != binding:
                return False
            self._bindings[mid] = binding
            matching = receipt.get('requestedModel') == profile['model'] and receipt.get('protocol') == profile['protocol']
            reason = receipt.get('finishReason')
            incomplete = isinstance(reason, str) and reason in INCOMPLETE_REASONS
            success = (error is None and matching and receipt.get('responseComplete') is True
                       and receipt.get('jsonValidated') is True and not incomplete)
            code = _http_status(error, receipt)
            if code is not None and code >= 400:
                success = False
            elapsed = receipt.get('elapsedMs')
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                elapsed = None
            returned = receipt.get('returnedModel')
            call = {'checkedAt': self._clock(), 'requestedModel': profile['model'],
                    'returnedModel': returned[:500] if isinstance(returned, str) else None,
                    'protocol': profile['protocol'], 'requestPolicyFingerprint': receipt.get('requestPolicyFingerprint'),
                    'elapsedMs': elapsed, 'errorClass': None if success else
                    ('binding_mismatch' if not matching else _error_class(error, receipt)),
                    'httpStatus': code, 'responseComplete': receipt.get('responseComplete') is True,
                    'jsonValidated': receipt.get('jsonValidated') is True,
                    'availabilityProbe': receipt.get('availabilityProbe') is True,
                    'modelIdentityEvidence': 'provider_declared'}
            record = {'binding': binding, 'generation': current, 'sequence': self._next(),
                      'success': success, 'call': call}
            self._calls[mid] = record
            if success:
                self._successes[mid] = record
                if call['availabilityProbe']:
                    self._blocked.pop(mid, None)
            elif code in HARD_HTTP_STATUSES:
                self._blocked[mid] = record
            return True

    def status(self, mid, profile, configured, *, bind=True):
        with self._lock:
            binding = self._observe(mid, profile) if bind else (profile['provider'], profile_fingerprint(profile))
            generation = self.generation(profile['provider'])
            catalog = self._catalogs.get((profile['provider'], profile['base']))
            if catalog and catalog['_generation'] != generation:
                catalog = None
            state, listed = 'unknown', None
            if catalog:
                if catalog['state'] == 'error':
                    state = 'error'
                elif profile['model'] in catalog['modelIds']:
                    state, listed = 'listed', True
                elif catalog['complete']:
                    state, listed = 'not_listed', False
            def matching(records):
                value = records.get(mid)
                return value if value and value['binding'] == binding and value['generation'] == generation else None
            call = matching(self._calls)
            success, blocked = matching(self._successes), matching(self._blocked)
            complete = self._complete_catalogs.get((profile['provider'], profile['base']))
            absent = bool(complete and profile['model'] not in complete['modelIds']
                          and self._last_listed.get((profile['provider'], profile['base'], profile['model']), 0)
                          <= complete['_sequence'])
            # A failed/incomplete refresh is not evidence that a previous
            # authoritative deletion has been reversed.
            recent_probe = bool(success and success['call']['availabilityProbe'] and absent
                                and success['sequence'] > complete['_sequence'])
            eligible = False
            if configured is not True:
                reason = '尚未配置本次服务凭据'
            elif blocked:
                reason = '调用被接口拒绝（HTTP %s）；需修改参数、更新凭据或显式验证' % blocked['call']['httpStatus']
            elif absent and not recent_probe:
                reason = '最近完整目录未列出该精确模型 ID；需目录重新列出或显式验证'
            elif absent:
                eligible, reason = True, '目录未列出，但随后显式验证成功'
            elif state == 'listed':
                eligible, reason = True, '当前目录已列出；目录记录不等同于实际调用验证'
            elif success:
                eligible, reason = True, '目录未确认；当前配置已有完整 JSON 调用成功记录'
            else:
                reason = '凭据已配置，但目录和当前参数的实际调用均未确认'
            return {'configured': configured is True, 'catalogState': state, 'listed': listed,
                    'catalogCheckedAt': catalog['checkedAt'] if catalog else None,
                    'catalogComplete': catalog['complete'] if catalog else None,
                    'catalogError': ({'errorClass': catalog['errorClass'], 'httpStatus': catalog['httpStatus']}
                                     if catalog and state == 'error' else None),
                    'providerDeclaredMetadata': deepcopy((catalog or {}).get('declaredMetadata', {}).get(profile['model'], {})),
                    'callState': 'unverified' if call is None else 'succeeded' if call['success'] else 'failed',
                    'lastCall': deepcopy(call['call']) if call else None, 'eligible': eligible, 'reason': reason}
