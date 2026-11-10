"""Explicitly authorized, quality-gated draft PR effects with a durable journal.

This module never merges, deploys, force-pushes, or repeats an uncertain POST.
The caller supplies a host authorization callback which reads authoritative task
and source state. A model's JSON or a restored effect journal is not authority.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.parse

from live import Cancelled
from github_source import GitHubError, SHA, _hash, _iso, safe_repository_path, validate_branch, validate_repository_name


class _Stopped(Exception):
    pass


def _files(files):
    if not isinstance(files, list) or not files or len(files) > 1000:
        raise ValueError('draft PR requires a nonempty bounded file list')
    normalized, aliases, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'content'}:
            raise ValueError('draft PR files require exact path/content fields')
        path, content = item['path'], item['content']
        if not safe_repository_path(path) or not isinstance(content, str) or '\0' in content:
            raise ValueError('draft PR files must be safe UTF-8 text paths')
        if any(part.lower() in ('.git', '.env', '.ssh', '.aws') or part.lower().startswith('.env.') for part in path.split('/')):
            raise ValueError('credential or repository metadata paths are forbidden')
        if path.casefold() in aliases:
            raise ValueError('duplicate or aliased draft PR path')
        aliases.add(path.casefold())
        total += len(content.encode())
        if total > 16 * 1024 * 1024:
            raise ValueError('draft PR payload too large')
        normalized.append({'path': path, 'content': content})
    normalized.sort(key=lambda f: f['path'])
    return normalized


def artifact_fingerprint(files):
    return _hash([{'path': f['path'], 'bytes': len(f['content'].encode()),
                   'sha256': hashlib.sha256(f['content'].encode()).hexdigest()} for f in _files(files)])


def _check_parents(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('GitHub effect directory must not traverse symlinks')


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise GitHubError('invalid_git_object_response')
    return value


class GitHubPublisher:
    def __init__(self, client, outbox_dir, *, authorize, clock=None):
        if not callable(authorize):
            raise ValueError('a host authorization callback is required')
        self.client, self.root, self.authorize = client, Path(outbox_dir), authorize
        self.clock = clock or time.time
        _check_parents(self.root)

    def _binding(self, source, delivery, files, *, manual=False):
        if not isinstance(source, dict) or source.get('kind') != 'github' or source.get('enabled') is not True or (source.get('mode') != 'auto' and manual is not True):
            raise ValueError('draft PR requires an enabled explicitly automatic GitHub source')
        target = source.get('target') or {}
        if 'draft_pr' not in target.get('allowedActions', []):
            raise ValueError('source does not authorize draft PR publication')
        if not isinstance(delivery, dict) or delivery.get('schema') != 'yuki-github-delivery/v1' or delivery.get('qualityPassed') is not True:
            raise ValueError('quality-approved host delivery is required')
        config = source.get('config') or {}
        repo = validate_repository_name(delivery.get('repository'))
        branch = validate_branch(delivery.get('baseBranch'))
        if repo != config.get('repository') or branch != config.get('branch'):
            raise ValueError('delivery repository/branch differs from its source')
        if delivery.get('sourceId') != source.get('id') or delivery.get('sourceRevision') != source.get('revision'):
            raise ValueError('delivery source authorization revision changed')
        if delivery.get('projectId') != target.get('projectId'):
            raise ValueError('delivery project differs from its source')
        for field in ('deliveryId', 'sessionId', 'runId'):
            if not isinstance(delivery.get(field), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', delivery[field]):
                raise ValueError('invalid delivery identity')
        base = _sha(delivery.get('baseSHA'))
        allowed = target.get('allowedPaths')
        if not isinstance(allowed, list) or not allowed or any(not safe_repository_path(p.rstrip('/')) for p in allowed):
            raise ValueError('exact allowed draft PR paths are required')
        if any(not any(f['path'] == p or p.endswith('/') and f['path'].startswith(p) for p in allowed) for f in files):
            raise ValueError('draft PR file is outside the frozen source scope')
        fingerprint = artifact_fingerprint(files)
        if delivery.get('artifactFingerprint') != fingerprint:
            raise ValueError('draft PR files differ from the approved artifact fingerprint')
        if self.authorize(copy.deepcopy(source), copy.deepcopy(delivery), copy.deepcopy(files)) is not True:
            raise ValueError('authoritative host delivery authorization refused')
        frozen = {'sourceId': source['id'], 'sourceRevision': source['revision'], 'projectId': delivery['projectId'],
                  'sessionId': delivery['sessionId'], 'runId': delivery['runId'], 'deliveryId': delivery['deliveryId'],
                  'repository': repo, 'baseBranch': branch, 'baseSHA': base, 'allowedPaths': sorted(allowed),
                  'artifactFingerprint': fingerprint}
        effect = _hash({'sourceId': source['id'], 'deliveryId': delivery['deliveryId']})
        for optional in ('workProjectId', 'origin', 'effectId'):
            if optional in delivery:
                frozen[optional] = copy.deepcopy(delivery[optional])
        for key,limit in (('title',160),('summary',3000),('validation',2000)):
            value=delivery.get(key,'')
            if not isinstance(value,str) or len(value)>limit or '\0' in value or key=='title' and any(c in value for c in '\r\n'):
                raise ValueError('invalid host PR description')
            frozen[key]=value.replace('@','@\u200b')
        frozen['branch'] = 'yuki/trigger-' + effect[:24]
        return effect, frozen

    def _write(self, path, record):
        _check_parents(path)
        wrapper = {'record': record, 'sha256': _hash(record)}
        raw = json.dumps(wrapper, ensure_ascii=False, sort_keys=True).encode()
        temporary = path.with_suffix('.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read(self, path):
        _check_parents(path)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('GitHub effect journal too large')
        try:
            envelope = json.loads(path.read_text())
            record = envelope['record']
            if envelope.get('sha256') != _hash(record) or record.get('schema') != 'yuki-github-effect/v1':
                raise ValueError()
            return record
        except (ValueError, KeyError, TypeError):
            raise ValueError('GitHub effect journal failed integrity verification') from None

    def publish(self, source, delivery, files, *, manual=False):
        return self._deliver(source, delivery, files, manual=manual)

    def update(self, source, delivery, files, previous_effect_id, *, manual=False):
        return self._deliver(source, delivery, files, manual=manual, previous_effect_id=previous_effect_id)

    def _deliver(self, source, delivery, files, *, manual=False, previous_effect_id=None):
        files = _files(files)
        effect, binding = self._binding(source, delivery, files, manual=manual)
        if previous_effect_id is not None:
            if not isinstance(previous_effect_id, str) or not re.fullmatch('[0-9a-f]{64}', previous_effect_id):
                raise ValueError('Previous publication identity is invalid')
            previous = self._read(self.root / (previous_effect_id + '.json'))
            prior = previous.get('binding', {})
            if previous.get('status') != 'completed' or previous.get('effectId') != previous_effect_id or previous_effect_id == effect:
                raise ValueError('A distinct completed publication is required before an update')
            for key in ('sourceId', 'sourceRevision', 'projectId', 'sessionId', 'repository', 'baseBranch', 'baseSHA', 'allowedPaths', 'origin', 'workProjectId'):
                if prior.get(key) != binding.get(key):
                    raise ValueError('Follow-up publication changed its original binding')
            pr = previous.get('pullRequest') or {}
            if type(pr.get('number')) is not int or pr['number'] < 1 or pr.get('url') != 'https://github.com/' + binding['repository'] + '/pull/' + str(pr['number']):
                raise ValueError('Previous pull request identity is invalid')
            binding.update(branch=prior['branch'], previousEffectId=previous_effect_id,
                           previousHeadSHA=_sha(pr.get('headSHA')), pullRequestNumber=pr['number'])
        _check_parents(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / (effect + '.lock')
        _check_parents(lock_path)
        lock = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / (effect + '.json')
            if path.exists():
                record = self._read(path)
                if record.get('binding') != binding:
                    raise ValueError('existing GitHub effect has a different frozen delivery')
            else:
                record = {'schema': 'yuki-github-effect/v1', 'effectId': effect, 'binding': binding,
                          'status': 'prepared', 'createdAt': _iso(self.clock()), 'steps': {}}
                self._write(path, record)
            if record['status'] in ('completed', 'blocked'):
                return copy.deepcopy(record)
            try:
                self._publish(path, record, source, delivery, files)
            except _Stopped:
                pass
            return copy.deepcopy(record)
        finally:
            os.close(lock)

    def _publish(self, path, record, source, delivery, files):
        binding = record['binding']
        repo, branch = binding['repository'], binding['branch']
        updating = 'previousEffectId' in binding
        base = binding['previousHeadSHA'] if updating else binding['baseSHA']
        prefix = '/repos/' + repo

        def authorize():
            # This callback must read live source/task authority again before every POST.
            if self.authorize(copy.deepcopy(source), copy.deepcopy(delivery), copy.deepcopy(files)) is not True:
                raise ValueError('live host delivery authorization refused')
            if updating:
                verify_existing_pr()

        def stopped(error, status='blocked'):
            record['status'] = status
            record['error'] = error.public()
            self._write(path, record)
            raise _Stopped()

        def get(api_path):
            try:
                return self.client.request(api_path)['data']
            except GitHubError as error:
                stopped(error, 'unknown' if record.get('status') == 'unknown' else 'blocked')

        def base_unchanged():
            ref = get(prefix + '/git/ref/heads/' + urllib.parse.quote(binding['baseBranch'], safe='/'))
            if not isinstance(ref, dict) or ref.get('ref') != 'refs/heads/' + binding['baseBranch'] or ref.get('object', {}).get('sha') != binding['baseSHA']:
                stopped(GitHubError('base_changed'))

        def verify_existing_pr(expected_head=None):
            expected_head = expected_head or binding['previousHeadSHA']
            row = get(prefix + '/pulls/' + str(binding['pullRequestNumber']))
            if (not isinstance(row, dict) or row.get('number') != binding['pullRequestNumber']
                    or row.get('state') != 'open' or row.get('draft') is not True
                    or row.get('head', {}).get('ref') != branch or row.get('head', {}).get('sha') != expected_head
                    or str(row.get('head', {}).get('repo', {}).get('full_name', '')).lower() != repo
                    or row.get('base', {}).get('ref') != binding['baseBranch']
                    or str(row.get('base', {}).get('repo', {}).get('full_name', '')).lower() != repo):
                stopped(GitHubError('followup_pull_request_changed'))
            ref = get(prefix + '/git/ref/heads/' + branch)
            if not isinstance(ref, dict) or ref.get('ref') != 'refs/heads/' + branch or ref.get('object', {}).get('sha') != expected_head:
                stopped(GitHubError('followup_branch_changed'))
            return row

        def step(name, api_path, payload, validate, reconcile=None, method='POST', expected_status=201):
            previous = record['steps'].get(name)
            request_hash = _hash({'method': method, 'path': api_path, 'payload': payload})
            if previous:
                if previous.get('requestFingerprint') != request_hash:
                    raise ValueError('frozen GitHub effect request changed')
                if previous.get('status') == 'completed':
                    return previous['result']
                if previous.get('status') == 'blocked':
                    stopped(GitHubError('previous_effect_blocked'))
                # Running means a process may have crashed after the remote side effect.
                previous['status'] = record['status'] = 'unknown'
                self._write(path, record)
                if reconcile is not None:
                    try:
                        resolved = reconcile()
                    except GitHubError as error:
                        stopped(error, 'unknown')
                    if resolved is not None:
                        resolved = validate(resolved)
                        previous.update(status='completed', result=resolved, reconciledAt=_iso(self.clock()))
                        record['status'] = 'prepared'
                        record.pop('error', None)
                        self._write(path, record)
                        return resolved
                stopped(GitHubError('effect_result_unknown'), 'unknown')
            authorize()
            record['steps'][name] = {'status': 'running', 'requestFingerprint': request_hash, 'startedAt': _iso(self.clock())}
            record['status'] = 'running'
            self._write(path, record)  # Durable intent BEFORE the external write.
            try:
                response = self.client.request(api_path, method=method, data=payload)
                if response['status'] != expected_status:
                    raise GitHubError('unexpected_mutation_status', response['status'])
                value = validate(response['data'])
            except Cancelled:
                record['steps'][name]['status'] = 'unknown'
                record['status'] = 'unknown'
                record['error'] = {'category': 'cancelled_during_effect', 'httpStatus': None, 'retryAt': None}
                self._write(path, record)
                raise
            except GitHubError as error:
                definitive = error.status in (400, 401, 403, 404, 409, 422, 429)
                record['steps'][name]['status'] = 'blocked' if definitive else 'unknown'
                stopped(error, record['steps'][name]['status'])
            record['steps'][name].update(status='completed', result=value, completedAt=_iso(self.clock()))
            record['status'] = 'prepared'
            record.pop('error', None)
            self._write(path, record)
            return value

        # Do not mutate existing refs, even within the exclusive prefix.
        if updating:
            base_unchanged()
            # On uncertain branch writes the durable step reconciles the exact new SHA.
            previous_branch = record['steps'].get('branch', {})
            if previous_branch.get('status') not in ('running', 'unknown', 'completed'):
                verify_existing_pr()
        if not record['steps'] and not updating:
            base_unchanged()
            try:
                self.client.request(prefix + '/git/ref/heads/' + branch)
            except GitHubError as error:
                if error.status != 404:
                    stopped(error)
            else:
                stopped(GitHubError('branch_already_exists'))
        base_commit = get(prefix + '/git/commits/' + base)
        try:
            base_tree = _sha(base_commit['tree']['sha'])
        except (KeyError, TypeError):
            stopped(GitHubError('invalid_base_commit'))
        # A nested Git tree write can replace an existing directory or parent
        # blob. Reject those shape changes so a scoped edit cannot delete data
        # outside its exact approved paths. Preserve regular executable modes.
        base_listing = get(prefix + '/git/trees/' + base_tree + '?recursive=1')
        if not isinstance(base_listing, dict) or base_listing.get('truncated') is not False or not isinstance(base_listing.get('tree'), list):
            stopped(GitHubError('incomplete_base_tree'))
        existing = {}
        for row in base_listing['tree']:
            if not isinstance(row, dict) or not safe_repository_path(row.get('path')):
                stopped(GitHubError('unsafe_base_tree'))
            existing[row['path']] = row
        modes = {}
        for item in files:
            name = item['path']
            for old, row in existing.items():
                if old.casefold() == name.casefold() and old != name:
                    stopped(GitHubError('base_path_alias'))
                if old.startswith(name + '/') or (name.startswith(old + '/') and row.get('type') != 'tree'):
                    stopped(GitHubError('base_path_shape_conflict'))
                if name.casefold().startswith(old.casefold() + '/') and not name.startswith(old + '/'):
                    stopped(GitHubError('base_path_alias'))
            row = existing.get(name)
            if row and (row.get('type') != 'blob' or row.get('mode') not in ('100644', '100755')):
                stopped(GitHubError('base_path_not_regular_file'))
            modes[name] = row['mode'] if row else '100644'
        entries = []
        for index, item in enumerate(files):
            raw = item['content'].encode()
            blob_sha = hashlib.sha1(('blob ' + str(len(raw)) + '\0').encode() + raw).hexdigest()
            def validate_blob(data, expected=blob_sha):
                if not isinstance(data, dict) or data.get('sha') != expected:
                    raise GitHubError('blob_identity_mismatch')
                return {'sha': expected}
            def reconcile_blob(expected=blob_sha):
                try:
                    data = self.client.request(prefix + '/git/blobs/' + expected)['data']
                except GitHubError as error:
                    if error.status == 404:
                        return None
                    raise
                return data
            blob = step('blob-' + str(index), prefix + '/git/blobs',
                        {'content': base64.b64encode(raw).decode(), 'encoding': 'base64'}, validate_blob, reconcile_blob)
            entries.append({'path': item['path'], 'mode': modes[item['path']], 'type': 'blob', 'sha': blob['sha']})
        def validate_object(data):
            if not isinstance(data, dict):
                raise GitHubError('invalid_git_object_response')
            return {'sha': _sha(data.get('sha'))}
        tree = step('tree', prefix + '/git/trees', {'base_tree': base_tree, 'tree': entries}, validate_object)
        marker = '<!-- yuki-effect:' + record['effectId'] + ' -->'
        commit = step('commit', prefix + '/git/commits', {'message': 'Yuki quality-approved candidate\n\n' + marker,
                      'tree': tree['sha'], 'parents': [base]}, validate_object)
        expected_commit = commit['sha']
        def validate_ref(data):
            if not isinstance(data, dict) or data.get('ref') != 'refs/heads/' + branch or data.get('object', {}).get('sha') != expected_commit:
                raise GitHubError('branch_identity_mismatch')
            return {'ref': 'refs/heads/' + branch, 'sha': expected_commit}
        def reconcile_ref():
            try:
                return self.client.request(prefix + '/git/ref/heads/' + branch)['data']
            except GitHubError as error:
                if error.status == 404:
                    return None
                raise
        base_unchanged()
        if updating:
            step('branch', prefix + '/git/refs/heads/' + urllib.parse.quote(branch, safe='/'),
                 {'sha': expected_commit, 'force': False}, validate_ref, reconcile_ref, method='PATCH', expected_status=200)
            current = verify_existing_pr(expected_commit)
            url = current.get('html_url')
            if url != 'https://github.com/' + repo + '/pull/' + str(binding['pullRequestNumber']):
                stopped(GitHubError('pull_request_url_mismatch'))
            record.update(status='completed', pullRequest={'number':binding['pullRequestNumber'], 'url':url,
                          'draft':True, 'headSHA':expected_commit, 'repository':repo, 'headBranch':branch,
                          'baseBranch':binding['baseBranch']}, completedAt=_iso(self.clock()))
            record.pop('error', None)
            self._write(path, record)
            return
        step('branch', prefix + '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': expected_commit}, validate_ref, reconcile_ref)
        # Verify the branch has not been replaced by another actor before opening a PR.
        validate_ref(get(prefix + '/git/ref/heads/' + branch))
        base_unchanged()
        def validate_pr(data):
            if (not isinstance(data, dict) or data.get('draft') is not True or type(data.get('number')) is not int
                    or data.get('head', {}).get('ref') != branch or data.get('head', {}).get('sha') != expected_commit
                    or str(data.get('head', {}).get('repo', {}).get('full_name', '')).lower() != repo
                    or data.get('base', {}).get('ref') != binding['baseBranch']
                    or str(data.get('base', {}).get('repo', {}).get('full_name', '')).lower() != repo
                    or marker not in (data.get('body') or '')):
                raise GitHubError('pull_request_identity_mismatch')
            url = data.get('html_url')
            if url != 'https://github.com/' + repo + '/pull/' + str(data['number']):
                raise GitHubError('pull_request_url_mismatch')
            return {'number': data['number'], 'url': url, 'draft': True, 'headSHA': expected_commit,
                    'repository': repo, 'headBranch': branch, 'baseBranch': binding['baseBranch']}
        def reconcile_pr():
            query = urllib.parse.urlencode({'state': 'all', 'head': repo.split('/')[0] + ':' + branch,
                                           'base': binding['baseBranch'], 'per_page': 100})
            rows = self.client.request(prefix + '/pulls?' + query)['data']
            if not isinstance(rows, list):
                raise GitHubError('invalid_pull_request_list')
            matches = [row for row in rows if isinstance(row, dict) and marker in (row.get('body') or '')]
            if len(matches) > 1:
                raise GitHubError('ambiguous_pull_request_identity')
            return matches[0] if matches else None
        title = binding['title'] or 'Yuki candidate: ' + delivery['deliveryId']
        body = ((binding['summary'] or 'Quality-approved Yuki candidate.')+'\n\n'
                +(binding['validation'] or 'Host checks and independent artifact review passed.')
                +'\n\nHuman review is required before merging.\n\n'
                'Artifact fingerprint: `' + binding['artifactFingerprint'] + '`\n\n' + marker)
        pr = step('pull_request', prefix + '/pulls', {'title': title, 'body': body, 'head': branch,
                  'base': binding['baseBranch'], 'draft': True}, validate_pr, reconcile_pr)
        record.update(status='completed', pullRequest=pr, completedAt=_iso(self.clock()))
        record.pop('error', None)
        self._write(path, record)
