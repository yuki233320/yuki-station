import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from studio_native_collection import archive_job, collection_info, configure_collection, load_collection


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.destination = self.root / 'codex-project'
        self.destination.mkdir()
        self.config = configure_collection(self.root / 'data', 'native-project-1', '雪站 · 原生 Codex 桥接', self.destination)
        self.job = self.root / 'job'
        (self.job / 'workspace' / 'report').mkdir(parents=True)
        data = '实际测试结果：通过。'.encode()
        (self.job / 'workspace/report/result.txt').write_bytes(data)
        self.receipt = {'status': 'completed', 'projectId': self.config['projectId'], 'threadId': 'actual-native-thread', 'turnId': 'turn1',
                        'verifiedArtifacts': [{'path': 'report/result.txt', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]}
        for name, value in [('task.json', {'task': {'id': 'task1', 'instruction': '测试'}}), ('receipt.json', self.receipt), ('input-manifest.json', {'inputs': []})]:
            (self.job / name).write_text(json.dumps(value))
        events = self.job / 'state/jobs/task1/events.jsonl'
        events.parent.mkdir(parents=True)
        events.write_text('{"type":"tool_completed","exitCode":0}\n')
        self.ids = ['project1', 'session1', 'run1', 'node1']

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_archive_idempotence_and_original_receipt_unchanged(self):
        before = (self.job / 'receipt.json').read_bytes()
        result = archive_job(self.config, self.ids, self.job)
        self.assertEqual(result, archive_job(self.config, self.ids, self.job))
        manifest = json.loads(Path(result['archivePath']).read_text())
        self.assertEqual(manifest['threadId'], 'actual-native-thread')
        self.assertEqual(len(manifest['files']), 5)
        self.assertEqual((self.job / 'receipt.json').read_bytes(), before)
        self.assertEqual(len(json.loads((self.destination / 'transfers/index.json').read_text())['entries']), 1)

    def test_legacy_job_requires_verified_host_membership(self):
        self.receipt.pop('projectId')
        with self.assertRaisesRegex(ValueError, '尚未核实'):
            archive_job(self.config, self.ids, self.job, self.receipt)
        result = archive_job(self.config, self.ids, self.job, self.receipt, {
            'threadId': 'actual-native-thread', 'projectId': 'native-project-1', 'source': 'thread/read'})
        self.assertEqual(result['status'], 'archived')

    def test_child_snapshot_is_archived_only_with_matching_receipt_hash(self):
        relative = 'children/child-1.json'
        child_path = self.job / 'state/jobs/task1' / relative
        child_path.parent.mkdir()
        payload = b'{"thread":{"id":"child-1","turns":[]}}'
        child_path.write_bytes(payload)
        self.receipt.update(childEvidenceStatus='verified', subAgents=[{
            'threadId': 'child-1', 'parentThreadId': 'actual-native-thread', 'parentTurnId': 'turn1',
            'status': 'completed', 'receiptRef': relative, 'receiptSha256': '0' * 64}])
        with self.assertRaisesRegex(ValueError, '子任务回执.*哈希'):
            archive_job(self.config, self.ids, self.job, self.receipt)
        self.assertFalse((self.destination / 'transfers').exists())
        self.receipt['subAgents'][0]['receiptSha256'] = hashlib.sha256(payload).hexdigest()
        (self.job / 'receipt.json').write_text(json.dumps(self.receipt))
        before = (self.job / 'receipt.json').read_bytes()
        result = archive_job(self.config, self.ids, self.job)
        manifest = json.loads(Path(result['archivePath']).read_text())
        self.assertEqual((Path(result['archivePath']).parent / relative).read_bytes(), payload)
        self.assertEqual(next(row['sha256'] for row in manifest['files'] if row['path'] == relative),
                         self.receipt['subAgents'][0]['receiptSha256'])
        self.assertEqual(result, archive_job(self.config, self.ids, self.job))
        self.assertEqual((self.job / 'receipt.json').read_bytes(), before)
        self.assertEqual(child_path.read_bytes(), payload)

    def test_unverified_child_without_snapshot_is_retained_without_inventing_file(self):
        self.receipt.update(childEvidenceStatus='unverified', subAgents=[{
            'threadId': 'missing-child', 'parentThreadId': 'actual-native-thread',
            'status': 'unverified', 'error': 'child thread unavailable'}])
        (self.job / 'receipt.json').write_text(json.dumps(self.receipt))
        result = archive_job(self.config, self.ids, self.job)
        destination = Path(result['archivePath']).parent
        self.assertFalse((destination / 'children').exists())
        self.assertEqual(json.loads((destination / 'receipt.json').read_text())['childEvidenceStatus'], 'unverified')

    def test_secrets_links_paths_and_hash_mismatch_do_not_copy(self):
        for ids in [['..', 'session1', 'run1', 'node1'], ['project1', 'session1', 'run1', '../escape']]:
            with self.assertRaises(ValueError):
                archive_job(self.config, ids, self.job)
        self.receipt['verifiedArtifacts'][0]['path'] = '../escape'
        with self.assertRaisesRegex(ValueError, '越界'):
            archive_job(self.config, self.ids, self.job, self.receipt)
        self.receipt['verifiedArtifacts'][0]['path'] = 'report/result.txt'
        self.receipt['verifiedArtifacts'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, '哈希'):
            archive_job(self.config, self.ids, self.job, self.receipt)
        (self.job / 'task.json').write_text(json.dumps({'task': {'id': 'task1', 'instruction': 'sk-' + 'a' * 20}}))
        with self.assertRaisesRegex(ValueError, '凭据'):
            archive_job(self.config, self.ids, self.job)
        self.assertFalse((self.destination / 'transfers').exists())

    def test_config_is_persistent_host_data_and_refuses_symlink(self):
        self.assertEqual(load_collection(self.root / 'data'), self.config)
        self.assertTrue(collection_info(self.root / 'data')['configured'])
        path = self.root / 'data/native-project.json'
        copy = self.root / 'copy.json'
        path.rename(copy)
        path.symlink_to(copy)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            load_collection(self.root / 'data')
        self.assertFalse(collection_info(self.root / 'data')['configured'])

    def test_existing_archive_corruption_is_not_overwritten(self):
        result = archive_job(self.config, self.ids, self.job)
        (Path(result['archivePath']).parent / 'outputs/report/result.txt').write_text('corrupted')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            archive_job(self.config, self.ids, self.job)

    def test_failed_receipt_keeps_events_but_omits_invalid_outputs(self):
        self.receipt.update(status='failed')
        self.receipt['verifiedArtifacts'].append({'path': '../escaped'})
        (self.job / 'receipt.json').write_text(json.dumps(self.receipt))
        result = archive_job(self.config, self.ids, self.job)
        manifest = json.loads(Path(result['archivePath']).read_text())
        self.assertEqual(manifest['status'], 'failed')
        self.assertFalse(manifest['artifactsAccepted'])
        self.assertEqual(len(manifest['omittedArtifacts']), 1)
        self.assertTrue((Path(result['archivePath']).parent / 'events.jsonl').is_file())


if __name__ == '__main__':
    unittest.main()
