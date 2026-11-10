import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from github_delivery import GitHubPublisher, artifact_fingerprint
from github_source import GitHubSource
from test_github_source import LocalGitHub, REPO, SHA


class GitHubDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.http = LocalGitHub()
        self.addCleanup(self.http.close)
        self.http.handler = self.handler
        self.client = GitHubSource('fake-delivery-credential', api_base=self.http.base)
        self.allowed = True
        self.auth_calls = 0
        def authorize(*args):
            self.auth_calls += 1
            return self.allowed
        self.publisher = GitHubPublisher(self.client, Path(self.temp.name).resolve() / 'outbox', authorize=authorize)
        self.source = {'id': 'source-one', 'revision': 3, 'kind': 'github', 'enabled': True, 'mode': 'auto',
                       'config': {'repository': REPO, 'branch': 'main'},
                       'target': {'projectId': '0123456789ab', 'allowedActions': ['create_task', 'draft_pr'], 'allowedPaths': ['src/']}}
        self.files = [{'path': 'src/app.py', 'content': 'print("candidate")\n'}]
        self.delivery = {'schema': 'yuki-github-delivery/v1', 'sourceId': 'source-one', 'sourceRevision': 3,
                         'projectId': '0123456789ab', 'deliveryId': 'delivery-one', 'sessionId': 'session-one', 'runId': 'run-one',
                         'repository': REPO, 'baseBranch': 'main', 'baseSHA': SHA, 'qualityPassed': True,
                         'artifactFingerprint': artifact_fingerprint(self.files)}
        self.base_sha = SHA
        self.base_tree = 'd' * 40
        self.tree_sha = 'b' * 40
        self.commit_sha = 'c' * 40
        self.base_rows = [{'path': 'src', 'type': 'tree', 'mode': '040000', 'sha': 'e' * 40},
                          {'path': 'src/app.py', 'type': 'blob', 'mode': '100755', 'sha': 'f' * 40}]
        self.blobs, self.refs, self.prs = {}, {}, []
        self.fail = None
        self.fail_before = False
    def handler(self, path, headers, method, body):
        suffix = path.split('/repos/' + REPO, 1)[-1]
        if method == 'GET':
            if suffix == '/git/ref/heads/main':
                return 200, {'ref': 'refs/heads/main', 'object': {'sha': self.base_sha}}, {}
            if suffix.startswith('/git/ref/heads/'):
                branch = suffix.removeprefix('/git/ref/heads/')
                if branch not in self.refs:
                    return 404, {'message': 'missing'}, {}
                return 200, {'ref': 'refs/heads/' + branch, 'object': {'sha': self.refs[branch]}}, {}
            if suffix == '/git/commits/' + SHA:
                return 200, {'sha': SHA, 'tree': {'sha': self.base_tree}}, {}
            if suffix.startswith('/git/trees/'):
                return 200, {'truncated': False, 'tree': self.base_rows}, {}
            if suffix.startswith('/git/blobs/'):
                blob = self.blobs.get(suffix.rsplit('/', 1)[1])
                return (200, blob, {}) if blob else (404, {}, {})
            if suffix.startswith('/pulls?'):
                return 200, self.prs, {}
            return 404, {}, {}
        if self.fail == suffix and self.fail_before:
            return 403, {'message': 'fake-delivery-credential'}, {}
        if suffix == '/git/blobs':
            raw = base64.b64decode(body['content'])
            sha = hashlib.sha1(('blob ' + str(len(raw)) + '\0').encode() + raw).hexdigest()
            result = {'sha': sha, 'encoding': 'base64', 'content': body['content']}
            self.blobs[sha] = result
        elif suffix == '/git/trees':
            result = {'sha': self.tree_sha}
        elif suffix == '/git/commits':
            result = {'sha': self.commit_sha}
        elif suffix == '/git/refs':
            branch = body['ref'].removeprefix('refs/heads/')
            self.refs[branch] = body['sha']
            result = {'ref': body['ref'], 'object': {'sha': body['sha']}}
        elif suffix == '/pulls':
            result = {'number': 7, 'draft': body['draft'], 'body': body['body'], 'html_url': 'https://github.com/' + REPO + '/pull/7',
                      'head': {'ref': body['head'], 'sha': self.commit_sha, 'repo': {'full_name': REPO}},
                      'base': {'ref': body['base'], 'sha': SHA, 'repo': {'full_name': REPO}}}
            self.prs.append(result)
        else:
            return 404, {}, {}
        if self.fail == suffix:
            return 500, {'message': 'fake-delivery-credential was echoed'}, {}
        return 201, result, {}
    def publish(self):
        return self.publisher.publish(self.source, self.delivery, self.files)
    def writes(self):
        return [r for r in self.http.requests if r[2] != 'GET']
    def test_success_is_draft_scope_bound_and_idempotent(self):
        result = self.publish()
        self.assertEqual('completed', result['status'])
        self.assertTrue(result['pullRequest']['draft'])
        self.assertTrue(result['binding']['branch'].startswith('yuki/trigger-'))
        self.assertEqual(5, len(self.writes()))
        tree = next(r[3] for r in self.writes() if r[0].endswith('/git/trees'))
        self.assertEqual('100755', tree['tree'][0]['mode'])
        self.assertGreaterEqual(self.auth_calls, 6)
        count = len(self.http.requests)
        self.assertEqual(result, self.publish())
        self.assertEqual(count, len(self.http.requests))
        self.assertFalse(any(r[2] in ('PATCH', 'DELETE') for r in self.http.requests))
    def test_policy_quality_identity_and_artifact_are_all_required(self):
        variants = [('mode', 'observe'), ('enabled', False)]
        for key, value in variants:
            original = self.source[key]
            self.source[key] = value
            with self.assertRaises(ValueError):
                self.publish()
            self.source[key] = original
        for key, value in [('qualityPassed', False), ('sourceRevision', 2), ('projectId', 'other'), ('baseBranch', 'other'), ('artifactFingerprint', 'wrong')]:
            original = self.delivery[key]
            self.delivery[key] = value
            with self.assertRaises(ValueError):
                self.publish()
            self.delivery[key] = original
        self.allowed = False
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual([], self.http.requests)
    def test_scope_escape_or_directory_replacement_is_refused(self):
        self.files[0]['path'] = 'other.py'
        self.delivery['artifactFingerprint'] = artifact_fingerprint(self.files)
        with self.assertRaises(ValueError):
            self.publish()
        self.files[0]['path'] = 'src/app.py'
        self.delivery['artifactFingerprint'] = artifact_fingerprint(self.files)
        self.base_rows.append({'path': 'src/app.py/private.txt', 'type': 'blob', 'mode': '100644'})
        result = self.publish()
        self.assertEqual('base_path_shape_conflict', result['error']['category'])
        self.assertEqual([], self.writes())
    def test_unknown_pr_response_is_reconciled_without_second_post(self):
        self.fail = '/pulls'
        first = self.publish()
        self.assertEqual('unknown', first['status'])
        self.assertEqual(1, len(self.prs))
        self.fail = None
        result = self.publish()
        self.assertEqual('completed', result['status'])
        self.assertEqual(1, len([r for r in self.writes() if r[0].endswith('/pulls')]))
        self.assertNotIn('fake-delivery-credential', json.dumps(result))
    def test_unknown_pr_not_found_is_not_replayed(self):
        self.fail = '/pulls'
        first = self.publish()
        self.assertEqual('unknown', first['status'])
        self.prs.clear()
        self.fail = None
        result = self.publish()
        self.assertEqual('unknown', result['status'])
        self.assertEqual(1, len([r for r in self.writes() if r[0].endswith('/pulls')]))
    def test_unknown_tree_is_not_recreated_or_followed_by_commit(self):
        self.fail = '/git/trees'
        first = self.publish()
        self.assertEqual('unknown', first['status'])
        self.fail = None
        result = self.publish()
        self.assertEqual('unknown', result['status'])
        self.assertEqual(2, len(self.writes()))
    def test_unknown_blob_and_ref_can_be_confirmed_then_continue(self):
        self.fail = '/git/blobs'
        first = self.publish()
        self.assertEqual('unknown', first['status'])
        self.fail = '/git/refs'
        second = self.publish()
        self.assertEqual('unknown', second['status'])
        self.fail = None
        third = self.publish()
        self.assertEqual('completed', third['status'])
        self.assertEqual(5, len(self.writes()))
    def test_403_is_blocked_and_does_not_rerun_or_reveal_error_body(self):
        self.fail, self.fail_before = '/git/blobs', True
        first = self.publish()
        self.assertEqual('blocked', first['status'])
        self.assertEqual(403, first['error']['httpStatus'])
        count = len(self.http.requests)
        self.fail = None
        self.assertEqual(first, self.publish())
        self.assertEqual(count, len(self.http.requests))
        contents = ''.join(p.read_text() for p in (Path(self.temp.name).resolve() / 'outbox').glob('*.json'))
        self.assertNotIn('fake-delivery-credential', contents)
    def test_existing_branch_or_changed_base_blocks_without_write(self):
        effect, binding = self.publisher._binding(self.source, self.delivery, self.files)
        self.refs[binding['branch']] = 'e' * 40
        result = self.publish()
        self.assertEqual('branch_already_exists', result['error']['category'])
        self.assertEqual([], self.writes())
    def test_source_revocation_is_rechecked_before_each_write(self):
        def authorize(*args):
            return len(self.writes()) < 1
        self.publisher.authorize = authorize
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(1, len(self.writes()))
        self.assertEqual({}, self.refs)
    def test_journal_integrity_and_symlink_paths_are_not_authority(self):
        self.publish()
        path = next((Path(self.temp.name).resolve() / 'outbox').glob('*.json'))
        envelope = json.loads(path.read_text())
        envelope['record']['binding']['baseSHA'] = 'e' * 40
        path.write_text(json.dumps(envelope))
        with self.assertRaises(ValueError):
            self.publish()
        link = Path(self.temp.name).resolve() / 'link'
        link.symlink_to(Path(self.temp.name).resolve() / 'outbox', target_is_directory=True)
        with self.assertRaises(ValueError):
            GitHubPublisher(self.client, link, authorize=lambda *args: True)
    def test_manual_observe_publish_is_explicit_and_not_stored_as_authority(self):
        self.source['mode'] = 'observe'
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual([], self.writes())
        result = self.publisher.publish(self.source, self.delivery, self.files, manual=True)
        self.assertEqual('completed', result['status'])
        self.assertNotIn('manual', result['binding'])

    def test_observe_automatic_publish_is_refused_even_with_valid_quality(self):
        self.source['mode'] = 'observe'
        with self.assertRaises(ValueError):
            self.publisher.publish(self.source, self.delivery, self.files)
        self.assertEqual([], self.http.requests)

    def test_cancelled_post_stays_unknown_until_read_only_reconciliation(self):
        from live import Cancelled
        cancel = threading.Event()
        self.client.cancel = cancel
        original = self.http.handler
        def handler(path, headers, method, body):
            response = original(path, headers, method, body)
            if method == 'POST' and path.endswith('/pulls'):
                cancel.set()
            return response
        self.http.handler = handler
        with self.assertRaises(Cancelled):
            self.publish()
        path = next((Path(self.temp.name).resolve() / 'outbox').glob('*.json'))
        self.assertEqual('unknown', json.loads(path.read_text())['record']['status'])
        before = len(self.writes())
        cancel.clear()
        self.http.handler = original
        self.assertEqual('completed', self.publish()['status'])
        self.assertEqual(before, len(self.writes()))

    def test_work_project_identity_is_frozen_in_effect(self):
        self.delivery['workProjectId'] = '0123456789aa'
        first = self.publish()
        self.assertEqual('0123456789aa', first['binding']['workProjectId'])
        self.delivery['workProjectId'] = '0123456789bb'
        with self.assertRaises(ValueError):
            self.publish()

    def test_concurrent_same_effect_does_not_duplicate_external_writes(self):
        results, errors = [], []
        def run():
            try:
                results.append(self.publish())
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(2, len(results))
        self.assertEqual(5, len(self.writes()))


if __name__ == '__main__':
    unittest.main()
