"""Public connection metadata; API credentials remain in StudioClient memory."""
import copy
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from app import Conflict, uid
from live import PROFILES
from model_catalog import MODELS, SKILLS
from request_policy import effective_policy


PROVIDERS = {
    'opus': {'name': 'GrooRoute', 'baseUrl': 'https://grooroute.pro/v1'},
    'openai': {'name': 'Bang', 'baseUrl': 'https://api.bang.wiki/v1'},
    'deepseek': {'name': 'DeepSeek', 'baseUrl': 'https://api.deepseek.com'},
}
PRESETS = {
    'balanced': (85, 80, 80, 70, 85, 85, 0),
    'coding': (85, 95, 95, 65, 90, 75, 0),
    'reasoning': (90, 80, 90, 65, 95, 80, 0),
    'writing': (80, 50, 70, 60, 75, 95, 0),
    'vision': (80, 65, 75, 95, 80, 85, 0),
    'image': (0, 0, 0, 0, 0, 0, 100),
}


_POLICY_LOCKS, _POLICY_LOCKS_GUARD = {}, threading.Lock()


class ModelRegistry:
    def __init__(self, root):
        self.path = Path(root).resolve() / 'model-registry.json'
        with _POLICY_LOCKS_GUARD:
            self.lock = _POLICY_LOCKS.setdefault(str(self.path), threading.RLock())
        self.profiles = {}
        with self.lock:
            self._refresh()

    def _materialize(self, document):
        data = copy.deepcopy(document)
        for key in ('providers', 'models', 'requestPolicies', 'policyVersions'):
            data.setdefault(key, {})
            if not isinstance(data[key], dict):
                raise ValueError('模型注册表字段无效：' + key)
        profiles = copy.deepcopy(PROFILES)
        # Migrate built-in metadata explicitly; execution no longer branches on
        # the aliases "opus" or "gemini". Do not infer policies for new aliases.
        profiles['opus']['protocol'] = 'anthropic'
        profiles['gemini']['requestPolicy'] = {
            'maxOutputTokens': 65536, 'reasoningEffort': 'low',
            'jsonMode': False, 'timeoutSeconds': 1200,
        }
        for mid, item in data['models'].items():
            profiles[mid] = copy.deepcopy(item['connection'])
        if set(data['requestPolicies']) - set(profiles) or set(data['policyVersions']) - set(profiles):
            raise ValueError('请求策略引用了不存在的模型')
        for mid, profile in profiles.items():
            if mid in data['requestPolicies']:
                configured = data['requestPolicies'][mid]
                inherited = profile.get('requestPolicy', {})
                if not isinstance(configured, dict) or not isinstance(inherited, dict):
                    raise ValueError('请求策略须为对象')
                profile['requestPolicy'] = {**inherited, **configured}
            policy = effective_policy(profile)
            version = data['policyVersions'].get(mid, 0)
            if type(version) is not int or version < 0:
                raise ValueError('请求策略版本无效')
            profile['requestPolicy'] = copy.deepcopy(policy)
            data['requestPolicies'][mid] = policy
            data['policyVersions'][mid] = version
            # A legacy connection-local policy migrates into one authoritative
            # map; future writes do not leave two competing policy copies.
            if mid in data['models']:
                data['models'][mid]['connection'].pop('requestPolicy', None)
        return data, profiles

    def _apply(self, data, profiles):
        self.data = data
        # StudioClient may hold this mapping, so retain its identity. The run
        # owner freezes a copy for already-started work before invoking models.
        self.profiles.update(profiles)
        for stale in set(self.profiles) - set(profiles):
            del self.profiles[stale]
        for mid, item in data['models'].items():
            MODELS[mid] = copy.deepcopy(item['catalog'])

    def _refresh(self):
        document = (json.loads(self.path.read_text(encoding='utf8'),
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError('非法JSON数字')))
                    if self.path.exists() else {'providers': {}, 'models': {}})
        if not isinstance(document, dict):
            raise ValueError('模型注册表须为对象')
        self._apply(*self._materialize(document))

    def _publish(self, data):
        data, profiles = self._materialize(data)
        raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf8')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('.model-registry-' + uid() + '.pending')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                directory = os.open(self.path.parent, os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
            except OSError:
                pass
        finally:
            try: temporary.unlink()
            except FileNotFoundError: pass
        self._apply(data, profiles)

    def providers(self, connected):
        with self.lock:
            self._refresh()
            return [dict(id=pid, **provider,
                         models=[mid for mid, row in self.profiles.items() if row['provider'] == pid],
                         connected=any(mid in connected for mid, row in self.profiles.items() if row['provider'] == pid))
                    for pid, provider in {**PROVIDERS, **self.data['providers']}.items()]

    def policy_info(self, mid):
        with self.lock:
            self._refresh()
            if mid not in self.profiles:
                raise ValueError('模型不存在')
            return {'requestPolicy': copy.deepcopy(self.profiles[mid]['requestPolicy']),
                    'policyVersion': self.data['policyVersions'][mid]}

    def update_policy(self, mid, requestPolicy, version):
        """Versioned partial update; unspecified fields retain their values."""
        with self.lock:
            self._refresh()
            if mid not in self.profiles:
                raise ValueError('模型不存在')
            if type(version) is not int or version < 0:
                raise ValueError('请求策略版本须为非负整数')
            if version != self.data['policyVersions'][mid]:
                raise Conflict('模型请求策略已有更新，请刷新后再保存')
            if not isinstance(requestPolicy, dict):
                raise ValueError('请求策略须为对象')
            profile = copy.deepcopy(self.profiles[mid])
            profile['requestPolicy'].update(copy.deepcopy(requestPolicy))
            policy = effective_policy(profile)
            data = copy.deepcopy(self.data)
            data['requestPolicies'][mid] = policy
            data['policyVersions'][mid] = version + 1
            self._publish(data)
            return {'requestPolicy': copy.deepcopy(policy), 'policyVersion': version + 1}

    def register(self, request):
        with self.lock:
            self._refresh()
            values = validate_registration(request, {**PROVIDERS, **self.data['providers']})
            provider = values['providerId'] or 'provider-' + uid()
            if any(p['provider'] == provider and p['model'] == values['modelId'] and p['protocol'] == values['protocol']
                   for p in self.profiles.values()):
                raise ValueError('该连接已登记相同型号与协议，请直接选择已有模型')
            data = copy.deepcopy(self.data)
            if not values['providerId']:
                data['providers'][provider] = {'name': values['providerName'], 'baseUrl': values['baseUrl']}
            provider_row = {**PROVIDERS, **data['providers']}[provider]
            mid = 'model-' + uid()
            weights = dict(zip(SKILLS, PRESETS[values['profile']]))
            connection = dict(label=values['name'], model=values['modelId'], provider=provider,
                              base=provider_row['baseUrl'], protocol=values['protocol'], color='#708d84')
            if 'requestPolicy' in values:
                connection['requestPolicy'] = copy.deepcopy(values['requestPolicy'])
            policy = effective_policy(connection)
            connection.pop('requestPolicy', None)
            item = {
                'catalog': dict(name=values['name'], officialId=values['modelId'], verified=False, auto=True,
                                family=values['family'], providerId=provider, context=None,
                                summary='用户登记的型号；能力为所选用途的本地先验，未完成独立型号核验。实际型号以调用回执为准。',
                                sources=[], strengths=[values['profile']], weights=weights,
                                capabilitySource='user-selected-profile', profile=values['profile']),
                'connection': connection,
            }
            data['models'][mid] = item
            data['requestPolicies'][mid] = policy
            data['policyVersions'][mid] = 1
            self._publish(data)
            return mid


def validate_registration(request, providers):
    if not isinstance(request, dict):
        raise ValueError('模型登记须为对象')
    allowed = {'providerId', 'providerName', 'baseUrl', 'modelId', 'name', 'family', 'protocol', 'profile', 'requestPolicy'}
    if set(request) - allowed:
        raise ValueError('模型登记包含未知字段；密钥应通过连接接口保存到内存')
    values = {}
    for key, limit in [('providerId', 80), ('providerName', 80), ('baseUrl', 500),
                       ('modelId', 160), ('name', 100), ('family', 80), ('protocol', 20), ('profile', 20)]:
        value = request.get(key, '')
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError('模型登记字段无效：' + key)
        if re.search(r'sk-[A-Za-z0-9_-]{16,}', value):
            raise ValueError('模型登记不能保存密钥，请使用连接输入框')
        values[key] = value.strip()
    if not all(values[k] for k in ('modelId', 'name', 'family')):
        raise ValueError('请填写实际型号ID、显示名称与模型厂商')
    if values['protocol'] not in ('chat', 'responses', 'anthropic', 'image') or values['profile'] not in PRESETS:
        raise ValueError('请选择支持的接口协议与能力用途')
    if (values['protocol'] == 'image') != (values['profile'] == 'image'):
        raise ValueError('图像协议只能对应图像用途')
    if values['providerId']:
        if values['providerId'] not in providers:
            raise ValueError('模型连接不存在')
    else:
        parsed = urlsplit(values['baseUrl'])
        if not values['providerName'] or parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('请填写连接名称和不含凭据、查询参数的 HTTP(S) 接口根地址')
        values['baseUrl'] = values['baseUrl'].rstrip('/')
    if 'requestPolicy' in request:
        if not isinstance(request['requestPolicy'], dict):
            raise ValueError('请求策略须为对象')
        values['requestPolicy'] = copy.deepcopy(request['requestPolicy'])
    return values


def validate_team_selection(coordinator, members):
    if not isinstance(coordinator, str) or coordinator not in MODELS:
        raise ValueError('请选择已登记的主模型')
    if coordinator == 'native_codex' or MODELS[coordinator]['weights'].get('image') or not MODELS[coordinator]['auto']:
        raise ValueError('主模型须为可调度的文字模型，原生执行器和图像模型不承担全局规划')
    if not isinstance(members, list) or not 1 <= len(members) <= 32 or any(not isinstance(mid, str) or mid not in MODELS for mid in members):
        raise ValueError('工作小组须包含1–32个已登记模型')
    if len(set(members)) != len(members) or coordinator not in members:
        raise ValueError('工作小组不能重复，且须包含主模型')
    return {'coordinatorModel': coordinator, 'teamModels': list(members)}
