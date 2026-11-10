"""Exercise quality gates and bounded repair through the real scheduler/store."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import Store
from model_catalog import MODELS
from quality_telemetry import project_metrics
from studio import Studio
from test_studio_business import BusinessClient, bind_native_result, module, native_result


class QualityFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), seed=False)
        self.client = BusinessClient()
        self.studio = Studio(self.store, self.client)
        self.pid = self.studio.create_project('质量验收测试', '按固定标准交付', '不虚构运行测试')['id']

    def tearDown(self):
        for thread in self.studio.threads.values():
            thread.join(15)
        self.tmp.cleanup()

    def execute(self, session=None, resume=False):
        session = session or self.studio.create_session(self.pid, '质量关卡', '完成项目交付', 'general')
        self.studio.start(self.pid, session['id'], 'live', resume)
        self.studio.threads[session['id']].join(15)
        self.assertFalse(self.studio.threads[session['id']].is_alive())
        return self.studio.load(self.pid, session['id'])

    def calls(self, stage, mid=None):
        return [call for call in self.client.calls if call['key'][0] == stage and (mid is None or call['key'][1] == mid)]

    def software(self):
        self.client.modules = [module('software', 'development', 4)]

    def test_ordinary_work_keeps_three_calls_and_exports_computed_record(self):
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        self.assertEqual([c['key'][0] for c in self.client.calls], ['plan', 'build', 'integration'])
        run = session['runs'][-1]
        self.assertEqual(run['qualityVersion'], 1)
        record = run['qualityHistory'][0]
        self.assertTrue(record['passed'])
        row = record['modules'][0]
        self.assertEqual(row['basis'], 'coordinator_review')
        self.assertIsNone(row['reviewer'])
        self.assertEqual(row['metrics']['criterionCoveragePct'], 100)
        self.assertEqual(row['metrics']['criteriaTotal'], 1)
        content = (self.store.current(self.pid)[1] / 'brief.md').read_bytes()
        self.assertEqual(row['artifactManifest'][0]['sha256'], hashlib.sha256(content).hexdigest())
        event = next(e for e in session['events'] if e['seq'] == record['eventSeq'])
        self.assertEqual(event['type'], 'quality_passed')
        with zipfile.ZipFile(io.BytesIO(self.studio.export(self.pid, session['id']))) as archive:
            saved = json.loads(archive.read('会话与执行记录.json'))
        self.assertEqual(saved['runs'][-1]['qualityHistory'], run['qualityHistory'])

    def test_live_mode_fixture_records_actual_requests_and_project_metrics(self):
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        calls = run['callMetrics']
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({call['id'] for call in calls}), 3)
        self.assertEqual([call['nodeId'] for call in calls], ['draft', 'build-brief', 'integration'])
        for call in calls:
            self.assertRegex(call['id'], r'^[a-f0-9]{12}$')
            self.assertEqual(call['outcome'], 'returned')
            self.assertEqual(call['adapter'], 'api')
            self.assertGreaterEqual(call['elapsedMs'], 0)
            node = next(node for node in run['nodes'] if node['id'] == call['nodeId'])
            self.assertEqual(call['model'], node['agent'])
            self.assertEqual(call['contextRef'], node['contextRef'])
            context = self.studio.context(self.pid, session['id'], call['contextRef'])
            self.assertEqual(context['model'], call['model'])
        metrics = project_metrics(self.studio, self.pid)
        self.assertEqual(sum(row['requestCount'] for row in metrics['requests']), 3)
        self.assertTrue(all(row['errorCount'] == 0 and row['averageElapsedMs'] >= 0 for row in metrics['requests']))
        self.assertEqual(len(metrics['rows']), 1)
        self.assertEqual(metrics['rows'][0]['qualitySamples'], 1)
        self.assertEqual(metrics['rows'][0]['mergedCount'], 1)
        self.assertTrue(metrics['rows'][0]['insufficientSamples'])
        project = self.studio.project(self.pid)
        self.assertEqual(project['roleCooperation'], metrics.pop('roleCooperation'))
        self.assertEqual(project['qualityMetrics'], metrics)

    def test_provider_error_and_resume_preserve_call_attempts_without_counting_reuse(self):
        self.client.fail_once = ('integration', None, 'opus', 0)
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        first = copy.deepcopy(session['runs'][-1]['callMetrics'])
        self.assertEqual([call['outcome'] for call in first], ['returned', 'returned', 'error'])
        pending_metrics = project_metrics(self.studio, self.pid)
        self.assertEqual(pending_metrics['rows'], [])
        self.assertEqual(sum(row['errorCount'] for row in pending_metrics['requests']), 1)
        resumed = self.execute(session, resume=True)
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        calls = resumed['runs'][-1]['callMetrics']
        self.assertEqual(calls[:3], first)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(calls), len(self.client.calls))
        self.assertEqual([call['nodeId'] for call in calls], ['draft', 'build-brief', 'integration', 'integration'])
        self.assertEqual(calls[-1]['outcome'], 'returned')
        self.assertNotEqual(calls[-1]['id'], calls[-2]['id'])
        self.assertNotEqual(calls[-1]['contextRef'], calls[-2]['contextRef'])
        metrics = self.studio.project(self.pid)['qualityMetrics']
        opus = next(row for row in metrics['requests'] if row['model'] == 'opus')
        self.assertEqual(opus['requestCount'], 3)
        self.assertEqual(opus['errorCount'], 1)
        self.assertEqual(opus['latencySamples'], 3)
        self.assertEqual(metrics['rows'][0]['qualitySamples'], 1)

    def test_development_has_separate_reviewer_and_actual_source_context(self):
        self.software()
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(len(self.calls('review')), 6)
        self.assertEqual(len(self.calls('quality')), 1)
        author = next(n for n in run['nodes'] if n['id'] == 'build-software')
        reviewer = next(n for n in run['nodes'] if n['phase'] == 'verification')
        self.assertNotEqual(MODELS[author['agent']]['family'], MODELS[reviewer['agent']]['family'])
        self.assertNotEqual(reviewer['agent'], 'opus')
        self.assertNotEqual(author['agentId'], reviewer['agentId'])
        context = self.studio.context(self.pid, session['id'], reviewer['contextRef'])
        shared = context['memory']['sharedEvidence']
        self.assertEqual(shared['reviewFiles'], author['result']['files'])
        self.assertEqual(shared['qualityContract'], run['taskContracts']['software'])
        self.assertNotIn('checks', shared['reviewFiles'][0])
        self.assertNotIn('summary', shared['reviewFiles'][0])
        row = run['qualityHistory'][0]['modules'][0]
        self.assertEqual(row['basis'], 'independent_artifact_review')
        self.assertTrue(row['gatePassed'])
        self.assertTrue(all(c['type'] in {'scope', 'syntax'} for c in row['checks']))
        self.assertIn('没有运行', run['final']['verificationNotice'])

    def test_independent_blocker_overrules_coordinator_and_stops_after_two_repairs(self):
        self.software()
        def block(stage, result):
            if stage == 'quality':
                result['findings'] = [{'priority': 'P1', 'claim': '接口未满足约定', 'proposal': '补齐接口',
                                       'evidence': [{'path': 'software.md', 'startLine': 1, 'endLine': 1}]}]
            return result
        self.client.mutate = block
        before = self.store.current(self.pid)[0]['revision']
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('两轮', session['error'])
        run = session['runs'][-1]
        self.assertEqual([r['round'] for r in run['qualityHistory']], [0, 1, 2])
        self.assertTrue(all(not r['passed'] for r in run['qualityHistory']))
        self.assertEqual(len(self.calls('quality')), 3)
        self.assertEqual(len(self.calls('build', 'software')), 3)
        self.assertFalse(any(n.get('repairRound', 0) > 2 for n in run['nodes']))
        self.assertTrue(run['final']['coordinatorAccepted'])
        self.assertFalse(run['final']['accepted'])
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        final_node = next(n for n in run['nodes'] if n['id'] == run['finalNodeId'])
        self.assertTrue(final_node['result']['accepted'])
        calls_before = len(self.client.calls)
        resumed = self.execute(session, resume=True)
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(len(self.client.calls), calls_before)
        self.assertFalse(resumed['runs'][-1]['final']['accepted'])
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)

    def test_replan_and_missing_input_stop_repairs_and_resume_without_new_calls(self):
        for action, reason in [('replan', '固定脚本没有该输出字段，需修订下一版契约'),
                               ('needs_input', '缺少用户确认的字段定义')]:
            with self.subTest(action=action):
                def blocked(stage, result):
                    if stage == 'integration':
                        result['accepted'] = False
                        row = result['moduleAcceptance'][0]
                        row.update(accepted=False, unmet=[reason], repairAction=action)
                        row['criteria'][0]['status'] = 'unmet'
                    return result
                self.client.mutate = blocked
                before = self.store.current(self.pid)[0]['revision']
                call_start = len(self.client.calls)
                session = self.execute()
                self.assertEqual(session['status'], 'failed')
                self.assertIn('停止自动返工', session['error'])
                self.assertIn('模块 brief', session['error'])
                self.assertIn(reason, session['error'])
                run = session['runs'][-1]
                self.assertEqual([r['round'] for r in run['qualityHistory']], [0])
                record = run['qualityHistory'][0]
                self.assertEqual(record['repairBlocked'], [
                    {'moduleId': 'brief', 'action': action, 'unmet': [reason]}])
                self.assertEqual(record['modules'][0]['repairAction'], action)
                self.assertFalse(record['passed'])
                self.assertFalse(any(n.get('repairRound') for n in run['nodes']))
                self.assertEqual([c['key'][0] for c in self.client.calls[call_start:]],
                                 ['plan', 'build', 'integration'])
                self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
                self.assertTrue(run['artifacts'])
                calls_before_resume = len(self.client.calls)
                resumed = self.execute(session, resume=True)
                self.assertEqual(resumed['status'], 'failed')
                self.assertEqual(resumed['error'], session['error'])
                self.assertEqual(len(self.client.calls), calls_before_resume)
                self.assertEqual(resumed['runs'][-1]['qualityHistory'], run['qualityHistory'])
                self.assertEqual(self.store.current(self.pid)[0]['revision'], before)

    def test_unrepairable_module_stops_before_spending_calls_on_other_repairs(self):
        self.client.modules = [module('blocked'), module('fixable')]
        def reject_both(stage, result):
            if stage == 'integration':
                result['accepted'] = False
                for row in result['moduleAcceptance']:
                    row.update(accepted=False, unmet=['验收未满足'], repairAction='repair')
                    row['criteria'][0]['status'] = 'unmet'
                    if row['moduleId'] == 'blocked':
                        row.update(unmet=['需要新增输入文件'], repairAction='replan')
            return result
        self.client.mutate = reject_both
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('模块 blocked', session['error'])
        self.assertEqual(len(self.calls('build')), 2)
        self.assertEqual(len(self.calls('integration')), 1)
        self.assertFalse(any(n.get('repairRound') for n in session['runs'][-1]['nodes']))
        self.assertEqual(session['runs'][-1]['qualityHistory'][0]['repairBlocked'], [
            {'moduleId': 'blocked', 'action': 'replan', 'unmet': ['需要新增输入文件']}])

    def test_explicit_repair_action_keeps_bounded_repair_flow(self):
        integrations = [0]
        def repair_once(stage, result):
            if stage == 'integration':
                integrations[0] += 1
                if integrations[0] == 1:
                    result['accepted'] = False
                    row = result['moduleAcceptance'][0]
                    row.update(accepted=False, unmet=['需要补充模块说明'], repairAction='repair')
                    row['criteria'][0]['status'] = 'unmet'
            return result
        self.client.mutate = repair_once
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual([r['passed'] for r in run['qualityHistory']], [False, True])
        self.assertTrue(all(r['repairBlocked'] == [] for r in run['qualityHistory']))
        self.assertEqual(len(self.calls('build')), 2)
        self.assertEqual(len(self.calls('integration')), 2)
        self.assertEqual([n['id'] for n in run['nodes'] if n.get('repairRound')], ['repair-1-brief'])

    def test_review_context_lists_only_delivered_evidence_paths_and_actual_line_counts(self):
        self.software()
        revision = self.store.current(self.pid)[0]['revision']
        self.store.save_file(self.pid, 'inputs/example.json', b'{\n  "tasks": []\n}\n', revision)
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        reviewer = next(n for n in session['runs'][-1]['nodes'] if n['phase'] == 'verification')
        context = self.studio.context(self.pid, session['id'], reviewer['contextRef'])
        memory = context['memory']
        self.assertIn('inputs/example.json', [f['path'] for f in memory['project']['files']])
        allowed = [{'path': 'software.md', 'lineCount': 3}]
        self.assertEqual(memory['sharedEvidence']['allowedEvidenceFiles'], allowed)
        self.assertEqual(self.calls('quality')[0]['memory']['sharedEvidence']['allowedEvidenceFiles'], allowed)
        instruction = context['messages'][-1]['content'].split('\n本轮角色任务：\n', 1)[1]
        self.assertIn('evidence.path 只能取 reviewFiles 中的路径', instruction)
        self.assertIn('输入示例、需求文件可在 reason 中说明', instruction)

    def test_repair_rebuilds_only_failed_module_and_transitive_receivers(self):
        self.client.modules = [module('software', 'development', 4), module('guide', deps=['software']),
                               module('release', deps=['guide']), module('unrelated')]
        quality_calls = [0]
        builds = {}
        def repair_fixture(stage, result):
            if stage == 'quality':
                quality_calls[0] += 1
                if quality_calls[0] == 1:
                    result['criteria'][0].update(status='unmet', reason='缺少接口说明')
                    result['findings'] = [{'priority': 'P1', 'claim': '缺少接口说明', 'proposal': '修复明确产物缺陷',
                                           'evidence': copy.deepcopy(result['criteria'][0]['evidence'])}]
            if stage == 'build':
                mid = result['files'][0]['path'].split('.')[0]
                builds[mid] = builds.get(mid, 0) + 1
                if builds[mid] > 1:
                    result['files'][0]['content'] += '\n修复后的完整交付。'
            return result
        self.client.mutate = repair_fixture
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(builds, {'software': 2, 'guide': 2, 'release': 2, 'unrelated': 1})
        self.assertEqual([r['passed'] for r in run['qualityHistory']], [False, True])
        self.assertEqual({n['moduleId'] for n in run['nodes'] if n.get('repairRound') == 1}, {'software', 'guide', 'release'})
        original = self.calls('build', 'software')[0]['memory']['sharedEvidence']['executionContract']
        repaired = self.calls('build', 'software')[1]['memory']['sharedEvidence']['executionContract']
        self.assertEqual(original, repaired)
        repaired_guide = self.calls('build', 'guide')[1]['memory']['sharedEvidence']
        upstream = repaired_guide['upstream']['software']['files'][0]['content']
        self.assertIn('修复后的', upstream)
        self.assertEqual(repaired_guide['handoff']['sources'][0]['nodeId'], 'repair-1-software')
        self.assertEqual(repaired_guide['handoff']['sources'][0]['files'][0]['sha256'], hashlib.sha256(upstream.encode()).hexdigest())
        final = run['qualityHistory'][-1]
        delivered = (self.store.current(self.pid)[1] / 'software.md').read_bytes()
        row = next(row for row in final['modules'] if row['moduleId'] == 'software')
        self.assertEqual(row['artifactManifest'][0]['sha256'], hashlib.sha256(delivered).hexdigest())

    def test_resume_failed_repair_reuses_prior_discussion_and_quality_evidence(self):
        self.software()
        attempts = {'quality': 0, 'build': 0}
        def interrupt_repair(stage, result):
            if stage == 'quality':
                attempts['quality'] += 1
                if attempts['quality'] == 1:
                    result['criteria'][0].update(status='unmet', reason='补齐说明')
                    result['findings'] = [{'priority': 'P1', 'claim': '补齐说明', 'proposal': '修复明确产物缺陷',
                                           'evidence': copy.deepcopy(result['criteria'][0]['evidence'])}]
            if stage == 'build':
                attempts['build'] += 1
                if attempts['build'] == 2:
                    raise ValueError('repair provider interrupted')
            return result
        self.client.mutate = interrupt_repair
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('repair provider interrupted', session['error'])
        first_record = copy.deepcopy(session['runs'][-1]['qualityHistory'][0])
        resumed = self.execute(session, resume=True)
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        run = resumed['runs'][-1]
        self.assertEqual(run['qualityHistory'][0], first_record)
        self.assertEqual(len(self.calls('review')), 6)
        self.assertEqual(len(self.calls('quality')), 2)
        self.assertEqual(len(self.calls('integration')), 2)
        self.assertEqual(len(self.calls('build')), 3)
        self.assertEqual([r['round'] for r in run['qualityHistory']], [0, 1])

    def test_replaced_candidate_is_not_exported_as_current_delivery(self):
        self.software()
        self.client.modules[0]['scope'] = ['src/']
        attempts = {'build': 0, 'quality': 0}
        def rename_on_repair(stage, result):
            if stage == 'build':
                attempts['build'] += 1
                result['files'][0]['path'] = 'src/version-' + str(attempts['build']) + '.md'
            if stage == 'quality':
                attempts['quality'] += 1
                if attempts['quality'] == 1:
                    result['criteria'][0].update(status='unmet', reason='重命名交付文件')
                    result['findings'] = [{'priority': 'P1', 'claim': '重命名交付文件', 'proposal': '修复明确产物缺陷',
                                           'evidence': copy.deepcopy(result['criteria'][0]['evidence'])}]
            return result
        self.client.mutate = rename_on_repair
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual([item['path'] for item in run['artifacts']], ['src/version-2.md'])
        with zipfile.ZipFile(io.BytesIO(self.studio.export(self.pid, session['id']))) as archive:
            self.assertIn('artifacts/' + run['id'] + '/src/version-2.md', archive.namelist())
            self.assertNotIn('artifacts/' + run['id'] + '/src/version-1.md', archive.namelist())
            original = next(n for n in run['nodes'] if n['id'] == 'build-software')
            saved = json.loads(archive.read('responses/' + original['responseRef'] + '.json'))
            self.assertEqual(saved['result']['files'][0]['path'], 'src/version-1.md')

    def test_missing_runtime_evidence_cannot_be_treated_as_met(self):
        self.software()
        def unverified(stage, result):
            if stage == 'quality':
                result['criteria'][0].update(status='unverified', reason='只有源码，尚无要求的运行证据', evidence=[])
            return result
        self.client.mutate = unverified
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        row = session['runs'][-1]['qualityHistory'][-1]['modules'][0]
        self.assertEqual(row['metrics']['criteriaUnverified'], 1)
        self.assertEqual(row['metrics']['criterionCoveragePct'], 0)
        self.assertFalse(row['gatePassed'])

    def test_image_repair_persists_new_binary_and_commits_exact_bytes(self):
        import struct
        import zlib
        def png(pixel):
            def chunk(kind, data):
                return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
            return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
                    + chunk(b'IDAT', zlib.compress(b'\x00' + bytes(pixel))) + chunk(b'IEND', b''))
        images = [png([255, 0, 0, 255]), png([0, 0, 255, 255])]
        self.client.modules = [dict(module('poster', 'image'), skills={'image': 1},
                                    requiredCapabilities=['image'], scope=['art/hero.png'])]
        attempts = {'build': 0, 'integration': 0}
        def image_provider(stage, result):
            if stage == 'build':
                index = attempts['build']
                attempts['build'] += 1
                return {'summary': '固定图片测试素材', 'imageBytes': images[index], 'checks': [], 'questions': []}
            if stage == 'integration':
                attempts['integration'] += 1
                if attempts['integration'] == 1:
                    result['accepted'] = False
                    result['moduleAcceptance'][0].update(accepted=False, unmet=['请改用蓝色素材'])
                    result['moduleAcceptance'][0]['criteria'][0]['status'] = 'unmet'
            return result
        self.client.mutate = image_provider
        session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        self.assertEqual(attempts, {'build': 2, 'integration': 2})
        self.assertEqual(self.calls('quality'), [])
        initial = next(n for n in run['nodes'] if n['id'] == 'build-poster')
        repaired = next(n for n in run['nodes'] if n['id'] == 'repair-1-poster')
        self.assertEqual(initial['result']['files'][0]['binaryRef'], 'images/build-poster.png')
        self.assertEqual(repaired['result']['files'][0]['binaryRef'], 'images/repair-1-poster.png')
        root = self.studio.project_dir(self.pid) / session['id']
        self.assertEqual((root / 'images/build-poster.png').read_bytes(), images[0])
        self.assertEqual((root / 'images/repair-1-poster.png').read_bytes(), images[1])
        self.assertEqual((self.store.current(self.pid)[1] / 'art/hero.png').read_bytes(), images[1])
        self.assertEqual(run['artifacts'][0]['sha256'], hashlib.sha256(images[1]).hexdigest())
        self.assertEqual(run['qualityHistory'][-1]['modules'][0]['artifactManifest'][0]['sha256'],
                         hashlib.sha256(images[1]).hexdigest())

    def test_independent_review_receives_only_related_native_verification_receipts(self):
        self.client.modules = [module('software', 'development', 4),
                               dict(module('verification', 'general', deps=['software']),
                                    requiredCapabilities=['text', 'files', 'terminal']),
                               dict(module('unrelated', 'general'), requiredCapabilities=['text', 'files', 'terminal'])]
        def native_provider(studio, session, run, node, context, messages, shared, cancel, delta):
            return native_result(node['moduleId'], shared.get('executionContract')), {'mode': 'fixture-native', 'requestedModel': 'native_codex',
                                                    'returnedModel': 'gpt-6.1-codex'}
        with patch('studio_native.execute_native', side_effect=native_provider):
            session = self.execute()
        self.assertEqual(session['status'], 'completed', session.get('error'))
        run = session['runs'][-1]
        review_node = next(n for n in run['nodes'] if n['id'] == 'verify-0-software')
        shared = self.studio.context(self.pid, session['id'], review_node['contextRef'])['memory']['sharedEvidence']
        verifier = next(n for n in run['nodes'] if n['id'] == 'build-verification')
        self.assertEqual(shared['verificationEvidence'],
                         [{'moduleId': 'verification', 'nativeEvidence': verifier['result']['nativeEvidence']}])
        self.assertEqual({f['path'] for f in shared['reviewFiles']}, {'software.md', 'verification.md'})
        self.assertEqual(set(review_node['deps']), {'build-software', 'build-verification'})
        self.assertEqual(shared['verificationEvidence'][0]['nativeEvidence']['tools'][0]['exitCode'], 0)

    def test_failed_native_report_reaches_review_as_counter_evidence(self):
        self.client.modules = [module('software', 'development', 4),
                               dict(module('verification', 'general', deps=['software']),
                                    requiredCapabilities=['text', 'files', 'terminal'])]
        def failed_native_provider(studio, session, run, node, context, messages, shared, cancel, delta):
            result = native_result()
            result['nativeEvidence']['tools'][0]['exitCode'] = 1
            result['nativeEvidence']['checks'][0].update(status='failed', evidence='command-1 exit 1')
            bind_native_result(result, shared.get('executionContract'))
            return result, {'mode': 'fixture-native', 'requestedModel': 'native_codex', 'returnedModel': 'gpt-6.1-codex'}
        with patch('studio_native.execute_native', side_effect=failed_native_provider):
            session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('原生执行尚有失败', session['error'])
        run = session['runs'][-1]
        review = next(node for node in run['nodes'] if node['id'] == 'verify-0-software')
        shared = self.studio.context(self.pid, session['id'], review['contextRef'])['memory']['sharedEvidence']
        self.assertIn('verification.md', {item['path'] for item in shared['reviewFiles']})
        self.assertIn('build-verification', review['deps'])
        counter_evidence = shared['verificationEvidence'][0]['nativeEvidence']
        self.assertEqual(counter_evidence['tools'][0]['exitCode'], 1)
        self.assertEqual(counter_evidence['checks'][0]['status'], 'failed')
        final_input = self.calls('integration')[0]['memory']['sharedEvidence']
        self.assertNotIn('verification.md', final_input['acceptanceEvidencePathsByModule']['software'])

    def test_invalid_review_evidence_stops_before_acceptance(self):
        self.software()
        def invalid_evidence(stage, result):
            if stage == 'quality':
                result['criteria'][0]['evidence'][0]['endLine'] = 99999
            return result
        self.client.mutate = invalid_evidence
        before = self.store.current(self.pid)[0]['revision']
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        self.assertIn('行范围', session['error'])
        self.assertEqual(self.calls('integration'), [])
        self.assertEqual(self.store.current(self.pid)[0]['revision'], before)
        node = next(n for n in session['runs'][-1]['nodes'] if n['phase'] == 'verification')
        self.assertTrue(node['responseRef'])
        self.assertNotEqual(node['responseValidation'], 'passed')

    def test_historical_run_resumes_without_adding_quality_gate(self):
        self.software()
        self.client.fail_once = ('plan', None, 'opus', 0)
        session = self.execute()
        self.assertEqual(session['status'], 'failed')
        session['runs'][-1].pop('qualityVersion')
        session['runs'][-1].pop('reviewRequirementVersion')
        self.studio.save(session)
        resumed = self.execute(session, resume=True)
        self.assertEqual(resumed['status'], 'completed', resumed.get('error'))
        self.assertEqual(self.calls('quality'), [])
        self.assertNotIn('qualityHistory', resumed['runs'][-1])
        self.assertNotIn('reviewRequirementVersion', resumed['runs'][-1])


if __name__ == '__main__':
    unittest.main()
