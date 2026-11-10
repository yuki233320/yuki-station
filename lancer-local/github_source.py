"""Read-only GitHub REST source. Credentials exist only on this process-local client.

Polling observes current revisions, not every intermediate GitHub edit. Initial
pagination is an observation baseline and never emits historical work. CI runs
which were active at baseline remain individually tracked after the create cursor
advances. All network errors are deliberately body-free.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import ipaddress
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from live import Cancelled

API_VERSION = '2022-11-28'
EVENT_TYPES = ('github.issue', 'github.comment', 'github.workflow_run')
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PAGES = 2
PAGE_SIZE = 100
MAX_ACTIVE_RUNS = 1000
SHA = re.compile(r'^[0-9a-f]{40}$')
REPOSITORY = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _epoch(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid GitHub timestamp') from None


def validate_repository_name(value):
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value) or value.split('/')[1] in ('.', '..'):
        raise ValueError('repository must be an exact owner/repository name')
    return value.lower()


def validate_branch(value):
    if (not isinstance(value, str) or not value or len(value) > 200
            or value != value.strip() or value in ('@', '.', '..')
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(c in value for c in ('..', '@{', '//', '~', '^', ':', '?', '*', '[', '\\'))
            or value.startswith('/') or value.endswith(('/', '.'))
            or any(p.startswith('.') or p.endswith('.lock') for p in value.split('/'))):
        raise ValueError('invalid exact GitHub branch')
    return value


def validate_source_config(kind, config):
    if kind != 'github' or not isinstance(config, dict):
        raise ValueError('expected github source config')
    if set(config) == {'github'}:
        config = config['github']
    if not isinstance(config, dict) or set(config) - {'repository', 'branch', 'pollSeconds'}:
        raise ValueError('unknown GitHub source config fields')
    seconds = config.get('pollSeconds', 120)
    if type(seconds) is not int or not 60 <= seconds <= 86400:
        raise ValueError('pollSeconds must be an integer between 60 and 86400')
    return {'repository': validate_repository_name(config.get('repository')),
            'branch': validate_branch(config.get('branch', 'main')), 'pollSeconds': seconds}


class GitHubError(RuntimeError):
    def __init__(self, category, status=None, retry_at=None):
        self.category, self.status, self.retry_at = category, status, retry_at
        super().__init__('GitHub ' + category + ((' (HTTP %s)' % status) if status else ''))

    def public(self):
        return {'category': self.category, 'httpStatus': self.status, 'retryAt': self.retry_at}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_at(headers, now, status):
    retry = headers.get('Retry-After')
    if retry:
        try:
            return now + max(1, int(retry))
        except (TypeError, ValueError):
            try:
                return max(now + 1, parsedate_to_datetime(retry).timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
    if headers.get('X-RateLimit-Remaining') == '0':
        try:
            return max(now + 1, float(headers.get('X-RateLimit-Reset')))
        except (TypeError, ValueError):
            pass
    return now + (60 if status in (403, 429) else 30)


class GitHubSource:
    def __init__(self, credential, *, api_base='https://api.github.com', opener=None, clock=None, cancel=None):
        if not isinstance(credential, str) or not credential or len(credential) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in credential):
            raise ValueError('GitHub credential required')
        parsed = urllib.parse.urlsplit(api_base)
        local = parsed.hostname == 'localhost'
        try:
            local = local or ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            pass
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or not ((parsed.scheme == 'https' and parsed.hostname == 'api.github.com' and parsed.port in (None, 443))
                        or (parsed.scheme == 'http' and local))):
            raise ValueError('only GitHub API or explicit loopback test API is supported')
        self.cancel = cancel
        self._credential = credential
        self.api_base = api_base.rstrip('/')
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._clock = clock or time.time
        self._poll_lock = threading.Lock()

    def _clean(self, value):
        if isinstance(value, str):
            return value.replace(self._credential, '[REDACTED]')
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        if isinstance(value, dict):
            return {self._clean(k): self._clean(v) for k, v in value.items()}
        return value

    def request(self, path, *, method='GET', data=None, etag=None):
        """Bounded authenticated REST transport; never follow or return remote URLs."""
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled('GitHub request cancelled')
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '\\' in path:
            raise ValueError('GitHub request must use an internal API path')
        headers = {'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + self._credential,
                   'X-GitHub-Api-Version': API_VERSION, 'User-Agent': 'YukiStation-GitHub-Source/1'}
        if etag and isinstance(etag, str) and len(etag) <= 300 and not any(c in etag for c in '\r\n'):
            headers['If-None-Match'] = etag
        payload = None if data is None else json.dumps(data, ensure_ascii=False).encode()
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.api_base + path, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=15) as response:
                status = response.status
                result_headers = dict(response.headers.items())
                raw = response.read(MAX_RESPONSE + 1)
                if self.cancel is not None and self.cancel.is_set():
                    raise Cancelled('GitHub request cancelled')
                if len(raw) > MAX_RESPONSE:
                    raise GitHubError('response_too_large', status)
                try:
                    result = json.loads(raw) if raw else None
                except (ValueError, UnicodeError):
                    raise GitHubError('invalid_json', status) from None
                return {'status': status, 'data': self._clean(result), 'headers': self._clean(result_headers)}
        except urllib.error.HTTPError as error:
            status, h = error.code, error.headers or {}
            error.close()
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled('GitHub request cancelled')
            if status == 304:
                return {'status': 304, 'data': None, 'headers': self._clean(dict(h.items()))}
            category = ('redirect_refused' if 300 <= status < 400 else 'rate_limited' if status == 429 or
                        (status == 403 and (h.get('Retry-After') or h.get('X-RateLimit-Remaining') == '0')) else
                        'authentication' if status == 401 else 'forbidden' if status == 403 else
                        'not_found' if status == 404 else 'http_error')
            raise GitHubError(category, status, _retry_at(h, self._clock(), status)) from None
        except (GitHubError, Cancelled):
            raise
        except Exception:
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled('GitHub request cancelled') from None
            raise GitHubError('network_error', retry_at=self._clock() + 30) from None

    def validate_repository(self, repository, branch='main'):
        repository, branch = validate_repository_name(repository), validate_branch(branch)
        repo = self.request('/repos/' + repository)['data']
        if not isinstance(repo, dict) or str(repo.get('full_name', '')).lower() != repository or type(repo.get('id')) is not int:
            raise GitHubError('repository_mismatch')
        ref = self.request('/repos/' + repository + '/git/ref/heads/' + urllib.parse.quote(branch, safe='/'))['data']
        sha = ref.get('object', {}).get('sha') if isinstance(ref, dict) else None
        if not isinstance(sha, str) or not SHA.fullmatch(sha) or ref.get('ref') != 'refs/heads/' + branch:
            raise GitHubError('invalid_branch_response')
        return {'repository': repository, 'repositoryId': repo['id'], 'private': repo.get('private') is True,
                'defaultBranch': repo.get('default_branch'), 'branch': branch, 'baseSHA': sha,
                'permissions': {k: v is True for k, v in (repo.get('permissions') or {}).items() if k in ('pull', 'push', 'admin')},
                'checkedAt': _iso(self._clock())}

    def _event(self, repository, branch, feed, row, source, bootstrap_at):
        if not isinstance(row, dict) or type(row.get('id')) is not int:
            raise GitHubError('invalid_event')
        if feed == 'issues' and 'pull_request' in row:
            return None
        user = (row.get('triggering_actor') or row.get('actor')) if feed == 'runs' else row.get('user')
        if not isinstance(user, dict) or not isinstance(user.get('login'), str):
            raise GitHubError('invalid_event_actor')
        updated = row.get('updated_at') or row.get('created_at')
        _epoch(updated)
        actor = user['login']
        actor_basis = 'workflow_actor'
        if feed != 'runs':
            try:
                actor_basis = 'creator' if _epoch(row.get('created_at')) == _epoch(updated) else 'author_unverified_editor'
            except ValueError:
                actor_basis = 'author_unverified_editor'
        origin = {'kind': 'github', 'repository': repository, 'objectId': row['id'],
                  'revision': updated, 'branch': None, 'headSHA': None, 'bootstrapAt': bootstrap_at, 'actorBasis': actor_basis}
        if feed == 'issues':
            number = row.get('number')
            if type(number) is not int:
                raise GitHubError('invalid_issue_number')
            thread, title, body = 'issue:' + str(number), row.get('title', ''), row.get('body') or ''
            origin['state'] = row.get('state')
        elif feed == 'comments':
            issue_url = row.get('issue_url', '')
            match = re.fullmatch(re.escape(self.api_base + '/repos/' + repository + '/issues/') + r'([1-9][0-9]*)', issue_url, re.I)
            if not match:
                raise GitHubError('invalid_comment_container')
            number = int(match.group(1))
            thread, title, body = 'issue:' + str(number), 'Comment on #' + str(number), row.get('body') or ''
        else:
            if row.get('head_branch') != branch:
                return None
            if not isinstance(row.get('head_sha'), str) or not SHA.fullmatch(row['head_sha']):
                raise GitHubError('invalid_workflow_sha')
            origin.update(branch=row['head_branch'], headSHA=row['head_sha'], status=row.get('status'),
                          conclusion=row.get('conclusion'), runAttempt=row.get('run_attempt', 1))
            thread = 'workflow:' + str(row.get('workflow_id')) + ':' + row['head_branch']
            title = row.get('display_title') or row.get('name') or 'GitHub Actions run'
            body = json.dumps({'status': row.get('status'), 'conclusion': row.get('conclusion'),
                               'headSHA': row['head_sha'], 'runAttempt': row.get('run_attempt', 1)}, sort_keys=True)
        if not isinstance(title, str) or not isinstance(body, str):
            raise GitHubError('invalid_event_text')
        event_type = {'issues': EVENT_TYPES[0], 'comments': EVENT_TYPES[1], 'runs': EVENT_TYPES[2]}[feed]
        revision = _hash({'updated': updated, 'actor': actor, 'title': title, 'body': body, 'origin': {k: v for k, v in origin.items() if k != 'bootstrapAt'}})[:24]
        self_actors = (source.get('policy') or {}).get('selfActors') or []
        url = row.get('html_url') or ''
        if url and (not isinstance(url, str) or not url.lower().startswith('https://github.com/' + repository.lower() + '/')):
            raise GitHubError('invalid_event_url')
        return {'externalId': repository + ':' + feed + ':' + str(row['id']) + ':' + revision,
                'container': repository, 'threadKey': thread, 'eventType': event_type, 'actor': actor,
                'isBot': user.get('type') == 'Bot' or actor.lower().endswith('[bot]'),
                'isSelfEcho': actor.lower() in {str(a).lower() for a in self_actors},
                'title': title[:1000], 'body': body[:100000], 'url': url, 'occurredAt': updated, 'origin': origin}

    def poll(self, source, cursor=None, *, subscriptions=()):
        with self._poll_lock:
            result = self._poll(source, cursor)
            # Subscribed PR feeds have their own watermarks and finite per-poll work.
            if subscriptions:
                self._poll_subscriptions(source, result, subscriptions, cursor)
            return result

    def _subscription_binding(self, source, value):
        expected = {'threadKey','repository','number','headBranch','headSHA','baseBranch','registeredAt'}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError('Exact host pull request subscription binding required')
        config = validate_source_config('github', source['config'])
        number = value['number']
        if (type(number) is not int or number <= 0 or value['threadKey'] != 'issue:' + str(number)
                or value['repository'] != config['repository'] or value['baseBranch'] != config['branch']
                or not isinstance(value['headSHA'], str) or not SHA.fullmatch(value['headSHA'])):
            raise ValueError('Pull request subscription differs from source')
        validate_branch(value['headBranch'])
        _epoch(value['registeredAt'])
        return copy.deepcopy(value)

    def _subscription_pr(self, binding):
        repo, number = binding['repository'], binding['number']
        row = self.request('/repos/' + repo + '/pulls/' + str(number))['data']
        if (not isinstance(row, dict) or row.get('number') != number
                or row.get('head', {}).get('ref') != binding['headBranch']
                or row.get('head', {}).get('sha') != binding['headSHA']
                or str(row.get('head', {}).get('repo', {}).get('full_name', '')).lower() != repo
                or row.get('base', {}).get('ref') != binding['baseBranch']
                or str(row.get('base', {}).get('repo', {}).get('full_name', '')).lower() != repo):
            raise GitHubError('subscribed_pull_request_changed')
        return row

    def _subscription_event(self, source, binding, feed, row):
        if not isinstance(row, dict):
            raise GitHubError('invalid_subscribed_event')
        repo, number = binding['repository'], binding['number']
        if feed == 'comments':
            row = copy.deepcopy(row)
            row.setdefault('issue_url', self.api_base + '/repos/' + repo + '/issues/' + str(number))
            event = self._event(repo, binding['baseBranch'], 'comments', row, source, binding['registeredAt'])
            if event['threadKey'] != binding['threadKey']:
                raise GitHubError('subscribed_comment_thread_mismatch')
        elif feed == 'runs':
            if row.get('head_branch') != binding['headBranch'] or row.get('head_sha') != binding['headSHA']:
                return None
            linked = row.get('pull_requests')
            if not isinstance(linked, list):
                return None
            matches = [p for p in linked if isinstance(p, dict) and p.get('number') == number
                       and p.get('head', {}).get('sha') == binding['headSHA']
                       and p.get('head', {}).get('ref') == binding['headBranch']
                       and p.get('base', {}).get('ref') == binding['baseBranch']
                       and p.get('url') == self.api_base + '/repos/' + repo + '/pulls/' + str(number)]
            if len(matches) != 1:
                return None
            event = self._event(repo, binding['headBranch'], 'runs', row, source, binding['registeredAt'])
            event['externalId'] += ':pr:' + str(number)
            event['threadKey'] = binding['threadKey']
        else:
            if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
                raise GitHubError('invalid_subscribed_review')
            user = row.get('user') or {}
            actor = user.get('login')
            if not isinstance(actor, str):
                raise GitHubError('invalid_event_actor')
            at = row.get('updated_at') or row.get('submitted_at') or row.get('created_at')
            _epoch(at)
            body = row.get('body') or ''
            if not isinstance(body, str):
                raise GitHubError('invalid_event_text')
            url = row.get('html_url') or ''
            expected_url = 'https://github.com/' + repo + '/pull/' + str(number)
            if url and not (url == expected_url or url.startswith(expected_url + '#')):
                raise GitHubError('subscribed_review_thread_mismatch')
            if feed == 'review_comments' and row.get('pull_request_url') != self.api_base + '/repos/' + repo + '/pulls/' + str(number):
                raise GitHubError('subscribed_review_thread_mismatch')
            origin = {'kind':'github','repository':repo,'objectId':row['id'],'revision':at,
                      'actorBasis':'creator','branch':None,'headSHA':binding['headSHA'],
                      'bootstrapAt':binding['registeredAt'], 'reviewState':row.get('state')}
            if feed == 'review_comments' and row.get('created_at') != at:
                origin['actorBasis'] = 'author_unverified_editor'
            event = {'externalId':repo+':'+feed+':'+str(row['id'])+':'+_hash({'updated':at,'body':body,'state':row.get('state')})[:24],
                     'container':repo,'threadKey':binding['threadKey'],'eventType':'github.comment','actor':actor,
                     'isBot':user.get('type')=='Bot' or actor.lower().endswith('[bot]'),
                     'isSelfEcho':actor.lower() in {str(a).lower() for a in source.get('policy',{}).get('selfActors',[])},
                     'title':('PR review: ' if feed == 'reviews' else 'PR inline comment: ') + str(row.get('state') or ''),
                     'body':body[:100000], 'url':url, 'occurredAt':at, 'origin':origin}
        event['origin'].update(subscriptionBindingFingerprint=_hash(binding), subscriptionThreadKey=binding['threadKey'],
                               pullRequest=number, feedbackKind=feed)
        return event

    def _poll_subscriptions(self, source, result, subscriptions, previous_cursor):
        bindings = [self._subscription_binding(source, row) for row in subscriptions]
        if len(bindings) > 100 or len({row['number'] for row in bindings}) != len(bindings):
            raise ValueError('Pull request subscription list is excessive or ambiguous')
        now = self._clock()
        if previous_cursor and now < previous_cursor.get('nextPollAt', 0):
            return
        if any(e['category'] in ('rate_limited','authentication','forbidden') for e in result['errors']):
            return
        cursor = result['cursor']
        states = cursor.setdefault('subscriptions', {})
        live = {str(row['number']):row for row in bindings}
        for key in list(states):
            if key not in live:
                del states[key]
        ordered = sorted(bindings, key=lambda row:row['number'])
        offset = cursor.get('subscriptionOffset', 0) % max(1,len(ordered))
        selected = (ordered[offset:] + ordered[:offset])[:2]
        cursor['subscriptionOffset'] = (offset + len(selected)) % max(1,len(ordered))
        for binding in selected:
            key = str(binding['number']); fingerprint = _hash(binding)
            state = states.setdefault(key, {'binding':fingerprint,'feeds':{}})
            if state.get('binding') != fingerprint:
                # A new host-published head keeps the comment watermarks. CI starts
                # at the host registration time for the newly accepted commit.
                state['binding'] = fingerprint
                state['feeds'].pop('runs', None)
            if now < state.get('retryAt',0):
                continue
            try:
                self._subscription_pr(binding)
                for feed in ('comments','review_comments','reviews','runs'):
                    watermark = state['feeds'].setdefault(feed, {'page':1,'since':binding['registeredAt'],'seen':{}})
                    watermark.setdefault('scanStarted',_iso(now))
                    for _ in range(MAX_PAGES):
                        query={'per_page':PAGE_SIZE,'page':watermark['page']}
                        prefix='/repos/'+binding['repository']
                        if feed == 'comments':
                            path=prefix+'/issues/'+key+'/comments';query['since']=watermark['since']
                        elif feed == 'review_comments':
                            path=prefix+'/pulls/'+key+'/comments';query.update(since=watermark['since'],sort='updated',direction='asc')
                        elif feed == 'reviews':path=prefix+'/pulls/'+key+'/reviews'
                        else:
                            path=prefix+'/actions/runs';query.update(branch=binding['headBranch'],head_sha=binding['headSHA'])
                        response=self.request(path+'?'+urllib.parse.urlencode(query))
                        rows=response['data'].get('workflow_runs') if feed=='runs' and isinstance(response['data'],dict) else response['data']
                        if not isinstance(rows,list):raise GitHubError('invalid_event_list')
                        headers={str(k).lower():v for k,v in response['headers'].items()}
                        more=bool(re.search(r'rel=[\"\']?next\b',headers.get('link','')))
                        for row in rows:
                            event=self._subscription_event(source,binding,feed,row)
                            if event is None:continue
                            eid=event['externalId']
                            if _epoch(event['occurredAt'])>=_epoch(watermark['since']) and eid not in watermark['seen']:
                                result['events'].append(event)
                            watermark['seen'][eid]=event['occurredAt']
                        if len(watermark['seen'])>2000:
                            watermark['seen']=dict(sorted(watermark['seen'].items(),key=lambda item:item[1])[-2000:])
                        if more:watermark['page']+=1
                        else:
                            watermark['page']=1;watermark['since']=_iso(_epoch(watermark.pop('scanStarted'))-2);break
                state.pop('retryAt',None)
            except GitHubError as error:
                state['retryAt']=error.retry_at or now+60
                result['errors'].append(dict(error.public(),feed='subscription:'+key))
                if error.category in ('rate_limited','authentication','forbidden'):
                    cursor['nextPollAt']=max(cursor.get('nextPollAt',0),state['retryAt']);break
        result['events']=list({row['externalId']:row for row in result['events']}.values())

    def _poll(self, source, cursor):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled('GitHub poll cancelled')
        config = validate_source_config('github', source.get('config', {}))
        repo, branch, now = config['repository'], config['branch'], self._clock()
        binding = _hash({'repository': repo, 'branch': branch})
        if cursor is None:
            cursor = {'schema': 'yuki-github-cursor/v1', 'binding': binding, 'bootstrapAt': _iso(now),
                      'feeds': {}, 'activeRuns': {}, 'activeOffset': 0, 'nextPollAt': 0}
        cursor = copy.deepcopy(cursor)
        if cursor.get('schema') != 'yuki-github-cursor/v1' or cursor.get('binding') != binding:
            raise ValueError('GitHub cursor belongs to a different repository or branch')
        result = {'events': [], 'cursor': cursor, 'errors': [], 'bootstrap': any(not cursor['feeds'].get(f, {}).get('ready') for f in ('issues', 'comments', 'runs'))}
        if now < cursor.get('nextPollAt', 0):
            return result
        for feed in ('issues', 'comments', 'runs'):
            state = cursor['feeds'].setdefault(feed, {'ready': False, 'since': None, 'page': 1, 'seen': {}, 'etags': {}})
            if now < state.get('retryAt', 0):
                continue
            state.setdefault('scanStarted', _iso(now))
            try:
                for _ in range(MAX_PAGES):
                    query = {'per_page': PAGE_SIZE, 'page': state['page']}
                    if feed != 'runs':
                        query.update(sort='updated', direction='asc')
                        if feed == 'issues':
                            query['state'] = 'all'
                        if state['since']:
                            query['since'] = state['since']
                    # Workflow lists have no updated-since filter. Scan bounded pages
                    # and filter locally; created-since would miss reruns of old runs.
                    path = '/repos/' + repo + {'issues': '/issues', 'comments': '/issues/comments', 'runs': '/actions/runs'}[feed]
                    path += '?' + urllib.parse.urlencode(query)
                    cached = state['etags'].get(path, {})
                    response = self.request(path, etag=cached.get('etag'))
                    response_headers={str(key).lower():value for key,value in response['headers'].items()}
                    try:
                        interval=int(response_headers.get('x-poll-interval',0))
                        if interval>0:cursor['nextPollAt']=max(cursor.get('nextPollAt',0),now+interval)
                    except (TypeError,ValueError):pass
                    if response['status'] == 304:
                        more = cached.get('more', False)
                        rows = []
                    else:
                        data = response['data']
                        rows = data.get('workflow_runs') if feed == 'runs' and isinstance(data, dict) else data
                        if not isinstance(rows, list):
                            raise GitHubError('invalid_event_list')
                        # Never follow Link URLs: page increments stay on the configured host/path.
                        link = response_headers.get('link') or ''
                        more = bool(re.search(r'rel=[\"\']?next\b', link))
                        etag = response_headers.get('etag')
                        if etag:
                            state['etags'][path] = {'etag': etag[:300], 'more': more}
                        for row in rows:
                            event = self._event(repo, branch, feed, row, source, cursor['bootstrapAt'])
                            if not event:
                                continue
                            eid = event['externalId']
                            recent = not state['since'] or _epoch(event['occurredAt']) >= _epoch(state['since'])
                            if state['ready'] and recent and eid not in state['seen']:
                                result['events'].append(event)
                            state['seen'][eid] = event['occurredAt']
                            if len(state['seen']) > 2000:
                                state['seen'] = dict(sorted(state['seen'].items(), key=lambda x: x[1])[-2000:])
                            if feed == 'runs':
                                run_id = str(row['id'])
                                if row.get('status') != 'completed':
                                    cursor['activeRuns'][run_id] = eid
                                    if len(cursor['activeRuns']) > MAX_ACTIVE_RUNS:
                                        raise GitHubError('too_many_active_workflows')
                                else:
                                    cursor['activeRuns'].pop(run_id, None)
                    if more:
                        state['page'] += 1
                    else:
                        state['ready'] = True
                        state['page'] = 1
                        state['since'] = _iso(_epoch(state.pop('scanStarted')) - 2)
                        cutoff = _epoch(state['since']) - 2
                        state['seen'] = {k: v for k, v in state['seen'].items() if _epoch(v) >= cutoff}
                        # Keep the last scan's exact conditional requests only; no unbounded URL history.
                        state['etags'] = {path: state['etags'][path]} if path in state['etags'] else {}
                        break
                state.pop('retryAt', None)
            except GitHubError as error:
                state['retryAt'] = error.retry_at or now + 60
                result['errors'].append(dict(error.public(), feed=feed))
                if error.category in ('rate_limited', 'authentication', 'forbidden'):
                    cursor['nextPollAt'] = state['retryAt']
                    break
        # Track old in-progress CI independently of its creation-time list watermark.
        if cursor['feeds'].get('runs', {}).get('ready') and not result['errors']:
            active = sorted(cursor['activeRuns'])
            offset = cursor.get('activeOffset', 0) % max(1, len(active))
            selected = (active[offset:] + active[:offset])[:10]
            cursor['activeOffset'] = (offset + len(selected)) % max(1, len(active))
            for run_id in selected:
                try:
                    row = self.request('/repos/' + repo + '/actions/runs/' + run_id)['data']
                    if not isinstance(row, dict) or str(row.get('id')) != run_id:
                        raise GitHubError('workflow_identity_changed')
                    event = self._event(repo, branch, 'runs', row, source, cursor['bootstrapAt'])
                    if not event:
                        raise GitHubError('workflow_branch_changed')
                    if not result['bootstrap'] and event['externalId'] != cursor['activeRuns'][run_id]:
                        result['events'].append(event)
                    cursor['activeRuns'][run_id] = event['externalId']
                    if row.get('status') == 'completed':
                        cursor['activeRuns'].pop(run_id)
                except GitHubError as error:
                    result['errors'].append(dict(error.public(), feed='active_runs'))
                    cursor['nextPollAt'] = error.retry_at or now + 60
                    break
        if result['bootstrap']:
            # This whole call belongs to baseline, even when its final page makes it ready.
            result['events'] = []
        result['events'] = list({e['externalId']: e for e in result['events']}.values())
        cursor['nextPollAt'] = max(cursor.get('nextPollAt', 0), now + config['pollSeconds'])
        return result

    def snapshot(self, repository, base_sha, *, max_files=5000, max_bytes=16 * 1024 * 1024):
        """Read an exact commit into a text-only import, never execute repository code.

        Symlinks/submodules, credential paths, oversized and binary files are
        explicitly reported as skipped. A truncated tree is rejected outright.
        """
        repository = validate_repository_name(repository)
        if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
            raise ValueError('snapshot requires a full commit SHA')
        tree = self.request('/repos/' + repository + '/git/trees/' + base_sha + '?recursive=1')['data']
        if not isinstance(tree, dict) or tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
            raise GitHubError('incomplete_repository_tree')
        remote_tree_sha = tree.get('sha')
        if not isinstance(remote_tree_sha, str) or not SHA.fullmatch(remote_tree_sha):
            raise GitHubError('invalid_repository_tree_identity')
        rows = tree['tree']
        if len(rows) > max_files * 2:
            raise GitHubError('repository_too_large')
        files, skipped, aliases, total = [], [], set(), 0
        for row in rows:
            path = row.get('path')
            if not isinstance(path, str) or not safe_repository_path(path):
                raise GitHubError('unsafe_repository_path')
            alias = unicodedata.normalize('NFC', path).casefold()
            if alias in aliases:
                raise GitHubError('repository_path_alias')
            aliases.add(alias)
            if row.get('type') == 'tree':
                continue
            reason = None
            if row.get('mode') not in ('100644', '100755') or row.get('type') != 'blob':
                reason = 'symlink_or_submodule'
            elif any(p.startswith('.') for p in path.split('/')) or re.search(r'(?i)(?:^|/)(?:credentials?|secrets?)(?:[./]|$)|\.(?:pem|key|p12|pfx)$', path):
                reason = 'hidden_or_credential_path'
            elif type(row.get('size')) is not int or row['size'] > 1024 * 1024:
                reason = 'file_too_large'
            if reason:
                skipped.append({'path': path, 'reason': reason})
                continue
            sha = row.get('sha')
            if not isinstance(sha, str) or not SHA.fullmatch(sha):
                raise GitHubError('invalid_blob_sha')
            blob = self.request('/repos/' + repository + '/git/blobs/' + sha)['data']
            try:
                if blob.get('encoding') != 'base64' or blob.get('sha') != sha:
                    raise ValueError()
                raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
                if hashlib.sha1(('blob ' + str(len(raw)) + '\0').encode() + raw).hexdigest() != sha or len(raw) != row['size']:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError):
                raise GitHubError('invalid_blob_content') from None
            try:
                content = raw.decode('utf-8')
                if '\0' in content:
                    raise UnicodeError()
            except UnicodeError:
                skipped.append({'path': path, 'reason': 'non_utf8_or_binary'})
                continue
            if self._clean(content) != content:
                raise GitHubError('credential_in_repository_content')
            total += len(raw)
            if len(files) >= max_files or total > max_bytes:
                raise GitHubError('repository_too_large')
            files.append({'path': path, 'content': content})
        files.sort(key=lambda f: f['path'])
        manifest = [{'path': f['path'], 'bytes': len(f['content'].encode()), 'sha256': hashlib.sha256(f['content'].encode()).hexdigest()} for f in files]
        return {'schema': 'yuki-github-snapshot/v1', 'repository': repository, 'baseSHA': base_sha,
                'treeSHA': remote_tree_sha, 'files': files, 'manifest': manifest, 'fingerprint': _hash(manifest), 'skipped': skipped}


def safe_repository_path(path):
    return (isinstance(path, str) and 0 < len(path) <= 500 and path == unicodedata.normalize('NFC', path)
            and not path.startswith('/') and not any(c in path for c in ('\\', '\0', ':'))
            and not any(ord(c) < 32 or ord(c) == 127 for c in path)
            and all(p not in ('', '.', '..') and not p.endswith((' ', '.')) for p in path.split('/')))
