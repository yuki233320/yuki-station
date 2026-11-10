"""Explicit protocol request controls; these are not a cost or task budget.

Policy validation checks the protocol vocabulary, not gateway/model capability.
A provider can reject a permitted effort or output size; readiness evidence must
record that result without silently downgrading or retrying. ``default`` means
omit the effort field. No alias, display name, secret, header, or endpoint is a
request-policy option. Images retain their existing payload and allow timeout
configuration only.

Protocol references checked 2026-11-03:
https://developers.openai.com/api/docs/guides/reasoning
https://platform.claude.com/docs/en/build-with-claude/effort
https://api-docs.deepseek.com/api/create-chat-completion/
"""
from copy import deepcopy
import hashlib
import json
import math
from urllib.parse import urlsplit

MAX_OUTPUT_TOKENS = 393216
TEXT_FIELDS = frozenset({'maxOutputTokens', 'reasoningEffort', 'jsonMode', 'timeoutSeconds'})
EFFORTS = frozenset({'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'})
PROTOCOL_EFFORTS = {'chat': EFFORTS, 'responses': EFFORTS,
                    'anthropic': frozenset({'default', 'low', 'medium', 'high', 'xhigh', 'max'})}
PROTOCOL_DEFAULTS = {
    'chat': {'maxOutputTokens': 16000, 'reasoningEffort': 'default', 'jsonMode': False},
    'responses': {'maxOutputTokens': 32768, 'reasoningEffort': 'medium', 'jsonMode': True},
    'anthropic': {'maxOutputTokens': 65536, 'reasoningEffort': 'medium', 'jsonMode': False},
    'image': {},
}


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError('timeoutSeconds 须为大于0且不超过3600的有限数字')
    return int(value) if int(value) == value else value


def _output_limit(value):
    if type(value) is not int or not 1 <= value <= MAX_OUTPUT_TOKENS:
        raise ValueError('maxOutputTokens 须为1至393216的整数；这是单次输出技术额度，不是费用预算')
    return value


def _deepseek_defaults(profile):
    # Preserve the previously explicit built-in provider behavior. Registered
    # aliases only inherit it at the exact official endpoint and model family.
    if profile.get('provider') == 'deepseek':
        return True
    base, model = profile.get('base'), profile.get('model')
    if not isinstance(base, str) or not isinstance(model, str) or not model.startswith('deepseek-'):
        return False
    try:
        endpoint = urlsplit(base)
        return (endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
                and endpoint.port in (None, 443) and endpoint.username is None and endpoint.password is None
                and endpoint.path.rstrip('/') in ('', '/v1') and not endpoint.query and not endpoint.fragment)
    except ValueError:
        return False


def effective_policy(profile, default_timeout=600):
    """Return a fresh complete policy. Partial explicit values override defaults."""
    if (not isinstance(profile, dict) or not isinstance(profile.get('protocol'), str)
            or profile['protocol'] not in PROTOCOL_DEFAULTS):
        raise ValueError('未知请求协议')
    protocol = profile['protocol']
    policy = {**PROTOCOL_DEFAULTS[protocol], 'timeoutSeconds': _timeout(default_timeout)}
    if protocol == 'chat' and _deepseek_defaults(profile):
        policy.update(maxOutputTokens=32768, reasoningEffort='low', jsonMode=True)
    supplied = profile.get('requestPolicy', {})
    allowed = {'timeoutSeconds'} if protocol == 'image' else TEXT_FIELDS
    if not isinstance(supplied, dict) or set(supplied) - allowed:
        raise ValueError('请求策略包含未知或不适用于该协议的字段；不接受headers、endpoint或密钥')
    policy.update(deepcopy(supplied))
    policy['timeoutSeconds'] = _timeout(policy['timeoutSeconds'])
    if protocol == 'image':
        return policy
    policy['maxOutputTokens'] = _output_limit(policy['maxOutputTokens'])
    effort = policy['reasoningEffort']
    if not isinstance(effort, str) or effort not in PROTOCOL_EFFORTS[protocol]:
        raise ValueError('reasoningEffort 不适用于所选协议')
    if type(policy['jsonMode']) is not bool:
        raise ValueError('jsonMode 须为布尔值')
    if protocol == 'anthropic' and policy['jsonMode']:
        raise ValueError('Anthropic协议不支持发送json_object；请关闭jsonMode并由输出合同要求JSON')
    return policy


def profile_fingerprint(profile):
    """Fingerprint only the public route, model, protocol and effective policy."""
    if not isinstance(profile, dict):
        raise ValueError('模型配置须为对象')
    public = {}
    for key in ('provider', 'base', 'model', 'protocol'):
        value = profile.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError('模型配置缺少公共字段：' + key)
        public[key] = value
    public['requestPolicy'] = effective_policy(profile)
    return hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def build_request(profile, messages, system, max_output_tokens=None, default_timeout=600):
    """Construct payload only; never send, infer an alias, mutate, or fall back."""
    policy = effective_policy(profile, default_timeout)
    protocol = profile['protocol']
    model = profile.get('model')
    if not isinstance(model, str) or not model:
        raise ValueError('请求缺少实际模型型号')
    if not isinstance(messages, list) or not messages:
        raise ValueError('请求需要非空消息列表')
    if not isinstance(system, str):
        raise ValueError('system须为字符串')
    if max_output_tokens is not None:
        if protocol == 'image':
            raise ValueError('图像协议不支持文字输出额度')
        policy['maxOutputTokens'] = _output_limit(max_output_tokens)
    content = deepcopy(messages)
    if protocol == 'image':
        if not isinstance(content[-1], dict) or not isinstance(content[-1].get('content'), str):
            raise ValueError('图像请求需要文字prompt')
        return '/images/generations', {'model': model, 'prompt': content[-1]['content'],
            'n': 1, 'size': '1536x1024', 'quality': 'medium', 'output_format': 'png'}, policy
    if protocol == 'anthropic':
        payload = {'model': model, 'system': system, 'messages': content,
                   'stream': True, 'max_tokens': policy['maxOutputTokens']}
        if policy['reasoningEffort'] != 'default':
            payload['output_config'] = {'effort': policy['reasoningEffort']}
        return '/messages', payload, policy
    if protocol == 'responses':
        payload = {'model': model, 'instructions': system, 'input': content, 'stream': True,
                   'store': False, 'max_output_tokens': policy['maxOutputTokens']}
        if policy['reasoningEffort'] != 'default':
            payload['reasoning'] = {'effort': policy['reasoningEffort']}
        if policy['jsonMode']:
            payload['text'] = {'format': {'type': 'json_object'}}
        return '/responses', payload, policy
    payload = {'model': model, 'messages': [{'role': 'system', 'content': system}, *content],
               'stream': True, 'stream_options': {'include_usage': True},
               'max_tokens': policy['maxOutputTokens']}
    if policy['reasoningEffort'] != 'default':
        payload['reasoning_effort'] = policy['reasoningEffort']
    if policy['jsonMode']:
        payload['response_format'] = {'type': 'json_object'}
    return '/chat/completions', payload, policy
