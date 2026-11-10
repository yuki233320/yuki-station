import base64
import copy
import hashlib
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from github_source import GitHubError, GitHubSource, validate_source_config

REPO = 'example-owner/example-repository'
SHA = 'a' * 40
NOW = 1793635200.0


class LocalGitHub:
    def __init__(self):
        self.requests = []
        self.handler = lambda path, headers, method, body: (200, [], {})
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply()
            def do_POST(self):
                self.reply()
            def do_PATCH(self):
                self.reply()
            def reply(self):
                size = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(size)
                body = json.loads(raw) if raw else None
                outer.requests.append((self.path, dict(self.headers), self.command, body))
                status, data, headers = outer.handler(self.path, self.headers, self.command, body)
                payload = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class GitHubSourceTests(unittest.TestCase):
    def setUp(self):
        self.http = LocalGitHub()
        self.addCleanup(self.http.close)
        self.now = NOW
        self.client = GitHubSource('fake-local-credential', api_base=self.http.base, clock=lambda: self.now)
        self.source = {'kind': 'github', 'config': {'repository': REPO, 'branch': 'main', 'pollSeconds': 60}, 'policy': {'selfActors': ['YukiBot']}}
        self.rows = {'issues': [], 'comments': [], 'runs': []}
        self.http.handler = self.default
    def default(self, path, headers, method, body):
        parsed = urllib.parse.urlsplit(path)
        if parsed.path.endswith('/issues/comments'):
            return 200, self.rows['comments'], {}
        if parsed.path.endswith('/issues'):
            return 200, self.rows['issues'], {}
        if parsed.path.endswith('/actions/runs'):
            return 200, {'workflow_runs': self.rows['runs']}, {}
        if '/actions/runs/' in parsed.path:
            return 200, next(r for r in self.rows['runs'] if str(r['id']) == parsed.path.rsplit('/', 1)[1]), {}
        return 404, {'message': 'fake-local-credential should not leak'}, {}
    def issue(self, ident=1, updated='2026-11-02T16:00:00Z', **kwargs):
        return dict({'id': ident, 'number': ident, 'title': 'Fix report', 'body': 'details', 'updated_at': updated,
                     'user': {'login': 'alice', 'type': 'User'}, 'state': 'open', 'html_url': 'https://github.com/' + REPO + '/issues/' + str(ident)}, **kwargs)
    def workflow(self, ident=10, status='in_progress', updated='2026-11-02T16:00:00Z', **kwargs):
        return dict({'id': ident, 'workflow_id': 3, 'head_branch': 'main', 'head_sha': SHA, 'status': status,
                     'updated_at': updated, 'created_at': '2020-01-01T00:00:00Z', 'run_attempt': 1,
                     'conclusion': None if status != 'completed' else 'success', 'actor': {'login': 'alice'},
                     'html_url': 'https://github.com/' + REPO + '/actions/runs/' + str(ident)}, **kwargs)
    def tick(self):
        self.now += 61
    def test_config_and_credentials_are_not_public_state(self):
        self.assertEqual(REPO, validate_source_config('github', {'repository': REPO.upper()})['repository'])
        for value in ({'repository': '../repo'}, {'repository': REPO, 'token': 'secret'}, {'repository': REPO, 'pollSeconds': True}, {'repository': REPO, 'branch': '../main'}):
            with self.assertRaises(ValueError):
                validate_source_config('github', value)
        result = self.client.poll(self.source)
        self.assertNotIn('fake-local-credential', json.dumps(result))
        self.assertEqual('Bearer fake-local-credential', self.http.requests[0][1]['Authorization'])
    def test_initial_observation_never_dispatches_history_then_new_revisions(self):
        self.rows['issues'] = [self.issue()]
        first = self.client.poll(self.source)
        self.assertTrue(first['bootstrap'])
        self.assertEqual([], first['events'])
        saved = copy.deepcopy(first['cursor'])
        self.tick()
        self.rows['issues'][0]['body'] = 'new request'
        self.rows['issues'][0]['updated_at'] = '2026-11-02T16:01:00Z'
        second = self.client.poll(self.source, first['cursor'])
        self.assertEqual(saved, first['cursor'])
        self.assertFalse(second['bootstrap'])
        self.assertEqual(1, len(second['events']))
        self.assertEqual('github.issue', second['events'][0]['eventType'])
        self.tick()
        third = self.client.poll(self.source, second['cursor'])
        self.assertEqual([], third['events'])
    def test_actor_basis_does_not_claim_original_author_is_editor(self):
        created = self.issue(created_at='2026-11-02T16:00:00Z')
        event = self.client._event(REPO, 'main', 'issues', created, self.source, 'baseline')
        self.assertEqual('creator', event['origin']['actorBasis'])
        created['updated_at'] = '2026-11-02T16:01:00Z'
        event = self.client._event(REPO, 'main', 'issues', created, self.source, 'baseline')
        self.assertEqual('author_unverified_editor', event['origin']['actorBasis'])
        self.assertEqual('alice', event['actor'])
        del created['created_at']
        event = self.client._event(REPO, 'main', 'issues', created, self.source, 'baseline')
        self.assertEqual('author_unverified_editor', event['origin']['actorBasis'])
        workflow = self.workflow(triggering_actor={'login': 'rerunner', 'type': 'User'})
        event = self.client._event(REPO, 'main', 'runs', workflow, self.source, 'baseline')
        self.assertEqual('workflow_actor', event['origin']['actorBasis'])
        self.assertEqual('rerunner', event['actor'])

    def test_stable_event_identity_does_not_depend_on_bootstrap_time(self):
        row = self.issue()
        one = self.client._event(REPO, 'main', 'issues', row, self.source, 'one')
        two = self.client._event(REPO, 'main', 'issues', row, self.source, 'two')
        self.assertEqual(one['externalId'], two['externalId'])
    def test_pagination_is_bounded_and_initial_history_never_leaks(self):
        def handler(path, headers, method, body):
            if '/issues?' in path:
                page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['page'][0])
                return 200, [self.issue(page)], {'Link': '<https://evil.invalid/token>; rel="next"'} if page < 3 else {}
            return self.default(path, headers, method, body)
        self.http.handler = handler
        first = self.client.poll(self.source)
        self.assertEqual(3, first['cursor']['feeds']['issues']['page'])
        self.assertFalse(first['cursor']['feeds']['issues']['ready'])
        self.assertEqual([], first['events'])
        self.tick()
        second = self.client.poll(self.source, first['cursor'])
        self.assertTrue(second['cursor']['feeds']['issues']['ready'])
        self.assertEqual([], second['events'])
        self.assertFalse(any('evil.invalid' in r[0] for r in self.http.requests))
    def test_etag_304_and_poll_interval(self):
        def handler(path, headers, method, body):
            if '/actions/runs?' in path:
                if headers.get('If-None-Match') == '"runs-v1"':
                    return 304, b'', {}
                return 200, {'workflow_runs': []}, {'ETag': '"runs-v1"'}
            return self.default(path, headers, method, body)
        self.http.handler = handler
        first = self.client.poll(self.source)
        count = len(self.http.requests)
        self.client.poll(self.source, first['cursor'])
        self.assertEqual(count, len(self.http.requests))
        self.tick()
        second = self.client.poll(self.source, first['cursor'])
        self.assertEqual([], second['errors'])
        self.assertEqual('"runs-v1"', [r[1] for r in self.http.requests if '/actions/runs?' in r[0]][-1]['If-None-Match'])
    def test_rate_limit_does_not_leak_body_or_poll_during_backoff(self):
        self.http.handler = lambda *args: (429, {'message': 'fake-local-credential'}, {'Retry-After': '300'})
        first = self.client.poll(self.source)
        self.assertEqual('rate_limited', first['errors'][0]['category'])
        self.assertNotIn('fake-local-credential', json.dumps(first))
        self.assertEqual(NOW + 300, first['cursor']['nextPollAt'])
        self.tick()
        self.client.poll(self.source, first['cursor'])
        self.assertEqual(1, len(self.http.requests))

    def test_lowercase_etag_and_server_poll_interval_are_preserved(self):
        def handler(path,headers,method,body):
            status,result,_=self.default(path,headers,method,body)
            return status,result,{'etag':'"lowercase"','x-poll-interval':'300'}
        self.http.handler=handler
        first=self.client.poll(self.source)
        self.assertEqual(first['cursor']['nextPollAt'],NOW+300)
        self.assertTrue(all(any(row['etag']=='"lowercase"' for row in feed['etags'].values()) for feed in first['cursor']['feeds'].values()))
        count=len(self.http.requests);self.tick();self.client.poll(self.source,first['cursor'])
        self.assertEqual(len(self.http.requests),count)
    def test_redirect_is_refused_without_forwarding_token(self):
        self.http.handler = lambda *args: (302, {}, {'Location': self.http.base + '/leak'})
        with self.assertRaises(GitHubError) as caught:
            self.client.request('/repos/' + REPO)
        self.assertEqual('redirect_refused', caught.exception.category)
        self.assertEqual(1, len(self.http.requests))
    def test_response_limit_and_invalid_json_fail_closed(self):
        self.http.handler = lambda *args: (200, b'x' * 40, {})
        with patch('github_source.MAX_RESPONSE', 10), self.assertRaises(GitHubError) as caught:
            self.client.request('/repos/' + REPO)
        self.assertEqual('response_too_large', caught.exception.category)
        self.http.handler = lambda *args: (200, b'fake-local-credential', {})
        with self.assertRaises(GitHubError) as caught:
            self.client.request('/repos/' + REPO)
        self.assertNotIn('fake-local-credential', str(caught.exception))
    def test_metadata_requires_exact_repo_and_ref(self):
        def handler(path, *args):
            if '/git/ref/' in path:
                return 200, {'ref': 'refs/heads/main', 'object': {'sha': SHA}}, {}
            return 200, {'id': 42, 'full_name': REPO, 'private': True, 'default_branch': 'main', 'permissions': {'push': True}}, {}
        self.http.handler = handler
        result = self.client.validate_repository(REPO)
        self.assertEqual(SHA, result['baseSHA'])
        self.assertTrue(result['private'])
        self.http.handler = lambda *args: (200, {'id': 42, 'full_name': 'someone/else'}, {})
        with self.assertRaises(GitHubError):
            self.client.validate_repository(REPO)
    def test_issue_pr_filter_comment_origin_and_self_echo(self):
        first = self.client.poll(self.source)
        self.tick()
        self.rows['issues'] = [self.issue(pull_request={})]
        self.rows['comments'] = [{'id': 2, 'body': 'Please fix', 'updated_at': '2026-11-02T16:01:00Z',
                                 'issue_url': self.http.base + '/repos/' + REPO + '/issues/7', 'user': {'login': 'YukiBot', 'type': 'Bot'}}]
        second = self.client.poll(self.source, first['cursor'])
        self.assertEqual(1, len(second['events']))
        event = second['events'][0]
        self.assertEqual('issue:7', event['threadKey'])
        self.assertTrue(event['isBot'])
        self.assertTrue(event['isSelfEcho'])
        self.assertIsNone(event['origin']['branch'])
    def test_old_active_ci_completion_and_old_run_rerun_are_observed(self):
        self.rows['runs'] = [self.workflow()]
        first = self.client.poll(self.source)
        self.assertIn('10', first['cursor']['activeRuns'])
        self.tick()
        self.rows['runs'] = [self.workflow(status='completed', updated='2026-11-02T16:01:00Z')]
        second = self.client.poll(self.source, first['cursor'])
        self.assertEqual(1, len(second['events']))
        self.assertEqual('success', second['events'][0]['origin']['conclusion'])
        self.assertEqual({}, second['cursor']['activeRuns'])
        self.tick()
        self.rows['runs'] = [self.workflow(updated='2026-11-02T16:02:00Z', run_attempt=2)]
        third = self.client.poll(self.source, second['cursor'])
        self.assertEqual(1, len(third['events']))
        self.assertEqual(2, third['events'][0]['origin']['runAttempt'])
    def test_cancel_before_and_after_http_does_not_become_retry(self):
        from live import Cancelled
        cancel = threading.Event()
        self.client.cancel = cancel
        cancel.set()
        with self.assertRaises(Cancelled):
            self.client.poll(self.source)
        self.assertEqual([], self.http.requests)
        cancel.clear()
        original = self.http.handler
        def handler(*args):
            response = original(*args)
            cancel.set()
            return response
        self.http.handler = handler
        with self.assertRaises(Cancelled):
            self.client.poll(self.source)
        self.assertEqual(1, len(self.http.requests))

    def test_credential_whitespace_control_and_non_ascii_are_rejected_without_echo(self):
        for value in ('secret key', 'secret\tkey', 'secret\nkey', 'secret\x7fkey', 'secret密钥'):
            with self.assertRaises(ValueError) as caught:
                GitHubSource(value, api_base=self.http.base)
            self.assertNotIn(value, str(caught.exception))

    def test_cursor_cannot_be_rebound_to_other_repo_or_branch(self):
        first = self.client.poll(self.source)
        self.source['config']['branch'] = 'other'
        with self.assertRaises(ValueError):
            self.client.poll(self.source, first['cursor'])
    def test_snapshot_validates_blob_hash_skips_secrets_and_symlinks(self):
        raw = b'print("hello")\n'
        blobsha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        def handler(path, *args):
            if '/git/trees/' in path:
                return 200, {'sha': SHA, 'truncated': False, 'tree': [
                    {'path': 'main.py', 'mode': '100644', 'type': 'blob', 'sha': blobsha, 'size': len(raw)},
                    {'path': '.env', 'mode': '100644', 'type': 'blob', 'sha': blobsha, 'size': len(raw)},
                    {'path': 'link', 'mode': '120000', 'type': 'blob', 'sha': blobsha, 'size': len(raw)}]}, {}
            return 200, {'sha': blobsha, 'encoding': 'base64', 'content': base64.b64encode(raw).decode()}, {}
        self.http.handler = handler
        result = self.client.snapshot(REPO, SHA)
        self.assertEqual([{'path': 'main.py', 'content': raw.decode()}], result['files'])
        self.assertEqual(2, len(result['skipped']))
        self.assertEqual(2, len(self.http.requests))
    def test_blob_decoded_credential_is_refused_by_request_local_client(self):
        raw = b'old value fake-local-credential'
        blobsha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        def handler(path, *args):
            if '/git/trees/' in path:
                return 200, {'sha': SHA, 'truncated': False, 'tree': [
                    {'path': 'main.txt', 'mode': '100644', 'type': 'blob', 'sha': blobsha, 'size': len(raw)}]}, {}
            return 200, {'sha': blobsha, 'encoding': 'base64', 'content': base64.b64encode(raw).decode()}, {}
        self.http.handler = handler
        with self.assertRaises(GitHubError) as caught:
            self.client.snapshot(REPO, SHA)
        self.assertEqual('credential_in_repository_content', caught.exception.category)
        self.assertNotIn('fake-local-credential', str(caught.exception))

    def test_snapshot_rejects_traversal_case_alias_truncation_and_bad_hash(self):
        for tree in ({'truncated': True, 'tree': []}, {'truncated': False, 'tree': [{'path': '../outside'}]},
                     {'truncated': False, 'tree': [{'path': 'A', 'type': 'tree'}, {'path': 'a', 'type': 'tree'}]}):
            self.http.handler = lambda *args, tree=tree: (200, tree, {})
            with self.assertRaises(GitHubError):
                self.client.snapshot(REPO, SHA)


if __name__ == '__main__':
    unittest.main()
