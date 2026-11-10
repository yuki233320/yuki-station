import copy
import tempfile
import unittest
from pathlib import Path

from app import Store
from studio import Studio
from test_studio_business import BusinessClient, module
from role_cooperation import build_role_cooperation


class RoleCooperationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.client = BusinessClient()
        cls.studio = Studio(Store(Path(cls.tmp.name), seed=False), cls.client)
        cls.baseline = cls.execute([module('upstream'), module('downstream', deps=['upstream'])])
        cls.second = cls.execute([module('upstream'), module('downstream', deps=['upstream'])])
        cls.third = cls.execute([module('upstream'), module('downstream', deps=['upstream'])])
        cls.client.question = True
        cls.clarified = cls.execute([module('upstream'), module('downstream', deps=['upstream'])])
        cls.client.question = False
        cls.fanin = cls.execute([module('left'), module('right'), module('joined', deps=['left', 'right'])])

    @classmethod
    def execute(cls, modules):
        cls.client.modules = modules
        pid = cls.studio.create_project('岗位交接', '处理相同业务规范', '')['id']
        sid = cls.studio.create_session(pid, '岗位链路', '按模块交付成果', 'general')['id']
        cls.studio.start(pid, sid, 'live')
        cls.studio.threads[sid].join(20)
        session = cls.studio.load(pid, sid)
        if session['status'] != 'completed':
            raise AssertionError(session.get('error'))
        return session

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.session = copy.deepcopy(self.baseline)
        self.run = self.session['runs'][-1]

    def row(self, session=None):
        return build_role_cooperation([session or self.session])['rows'][0]

    def test_real_scheduler_contracts_source_bytes_and_gate_are_traced(self):
        result = build_role_cooperation([self.session])
        row = result['rows'][0]
        self.assertEqual(row['sourceRole'], 'upstream-owner')
        self.assertEqual(row['targetRole'], 'downstream-owner')
        self.assertFalse(row['sameRole'])
        self.assertEqual(row['opportunities'], 1)
        self.assertEqual(row['handoff']['verified'], 1)
        self.assertEqual(row['firstGate']['passed'], 1)
        self.assertEqual(row['finalGate']['passed'], 1)
        self.assertEqual(row['sessionSuccess']['passed'], 1)
        self.assertEqual(row['repairs']['startedBuilds'], 0)
        self.assertEqual(len(row['evidence']), 1)
        self.assertTrue(row['evidence'][0]['packetId'])
        self.assertIsNone(result['interaction']['value'])

    def test_old_missing_contract_is_unknown_not_failure_or_success(self):
        del self.run['taskContracts']
        row = self.row()
        self.assertEqual(row['handoff']['unknown'], 1)
        self.assertEqual(row['handoff']['mismatched'], 0)
        self.assertIsNone(row['handoff']['ratePct'])
        self.assertEqual(row['firstGate']['unknown'], 1)
        self.assertIsNone(row['sessionSuccess']['wilson95'])

    def test_changed_manifest_is_known_mismatch(self):
        packet = next(p for p in self.run['handoffs'] if p['target']['moduleId'] == 'downstream')
        packet['sources'][0]['files'][0]['sha256'] = '0' * 64
        row = self.row()
        self.assertEqual(row['handoff']['mismatched'], 1)
        self.assertEqual(row['sessionSuccess']['failed'], 1)

    def test_packet_context_and_revision_must_match(self):
        for key in ('contextRef', 'runId', 'projectRevision'):
            with self.subTest(key=key):
                session = copy.deepcopy(self.session)
                packet = next(p for p in session['runs'][-1]['handoffs'] if p['target']['moduleId'] == 'downstream')
                packet[key] = 'wrong'
                self.assertEqual(self.row(session)['handoff']['mismatched'], 1)

    def test_source_cannot_follow_target_in_recorded_dependency_sequence(self):
        nodes = self.run['nodes']
        source = next(n for n in nodes if n['id'] == 'build-upstream')
        nodes.remove(source)
        nodes.append(source)
        self.assertEqual(self.row()['handoff']['mismatched'], 1)

    def test_missing_context_and_binary_body_are_unknown(self):
        packet = next(p for p in self.run['handoffs'] if p['target']['moduleId'] == 'downstream')
        del packet['contextRef']
        self.assertEqual(self.row()['handoff']['unknown'], 1)
        self.run = copy.deepcopy(self.baseline['runs'][-1])
        self.session['runs'][-1] = self.run
        source = next(n for n in self.run['nodes'] if n['id'] == 'build-upstream')
        source['result']['files'][0].pop('content')
        source['result']['files'][0]['binaryRef'] = 'images/unknown.png'
        self.assertEqual(self.row()['handoff']['unknown'], 1)

    def test_repeated_handoff_and_repair_do_not_add_opportunities(self):
        packet = next(p for p in self.run['handoffs'] if p['target']['moduleId'] == 'downstream')
        later = copy.deepcopy(packet)
        later['id'] = 'later'
        later['eventSeq'] += 1000
        later['sources'][0]['files'][0]['sha256'] = '0' * 64
        self.run['handoffs'].append(later)
        target = next(n for n in self.run['nodes'] if n['id'] == 'build-downstream')
        repair = copy.deepcopy(target)
        repair.update(id='repair-1-downstream', repairRound=1)
        self.run['nodes'].extend([repair, copy.deepcopy(repair)])
        row = self.row()
        self.assertEqual(row['opportunities'], 1)
        self.assertEqual(row['handoff']['verified'], 1)
        self.assertEqual(row['repairs']['startedBuilds'], 1)

    def test_resumed_node_keeps_first_call_context_evidence(self):
        target = next(n for n in self.run['nodes'] if n['id'] == 'build-downstream')
        old_context = target['contextRef']
        target['contextRef'] = 'resumed-context'
        self.run['callMetrics'].append({'nodeId': target['id'], 'contextRef': target['contextRef']})
        packet = next(p for p in self.run['handoffs'] if p['target']['moduleId'] == 'downstream')
        self.assertEqual(packet['contextRef'], old_context)
        self.assertEqual(self.row()['handoff']['verified'], 1)

    def test_quality_first_and_final_follow_events_even_same_round(self):
        original = self.run['qualityHistory'][0]
        early = copy.deepcopy(original)
        early['eventSeq'] -= 1
        next(r for r in early['modules'] if r['moduleId'] == 'downstream')['gatePassed'] = False
        self.run['qualityHistory'] = [original, early]
        row = self.row()
        self.assertEqual(row['firstGate']['failed'], 1)
        self.assertEqual(row['finalGate']['passed'], 1)
        self.assertEqual(row['sessionSuccess']['failed'], 1)

    def test_missing_first_round_does_not_turn_repair_into_first_pass(self):
        self.run['qualityHistory'][0]['round'] = 1
        self.assertEqual(self.row()['firstGate']['unknown'], 1)
        self.assertEqual(self.row()['finalGate']['passed'], 1)

    def test_known_failure_dominates_unknown_in_session(self):
        self.run['handoffs'] = []
        next(r for r in self.run['qualityHistory'][0]['modules'] if r['moduleId'] == 'downstream')['gatePassed'] = False
        row = self.row()
        self.assertEqual(row['handoff']['unknown'], 1)
        self.assertEqual(row['sessionSuccess']['failed'], 1)
        self.assertEqual(row['sessionSuccess']['unknown'], 0)

    def test_three_sessions_not_three_edges_drive_wilson_interval(self):
        rows = build_role_cooperation([self.baseline, self.second, self.third])['rows']
        self.assertEqual(len(rows), 1)
        result = rows[0]['sessionSuccess']
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['passed'], 3)
        self.assertFalse(result['insufficientSamples'])
        self.assertAlmostEqual(result['wilson95']['lowPct'], 43.8503, places=3)
        self.assertAlmostEqual(result['wilson95']['highPct'], 100)
        clone_run = copy.deepcopy(self.run)
        clone_run['id'] = 'second-run'
        self.session['runs'].append(clone_run)
        row = self.row()
        self.assertEqual(row['opportunities'], 2)
        self.assertEqual(row['sessionSuccess']['total'], 1)

    def test_fanin_exposes_shared_downstream_outcome_without_credit(self):
        result = build_role_cooperation([self.fanin])
        self.assertEqual(result['coverage']['opportunities'], 2)
        self.assertTrue(all(r['sharedDownstreamOpportunities'] == 1 for r in result['rows']))
        self.assertIn('不能归功', result['notice'])

    def test_clarification_respondsto_deduplicates_and_keeps_unresolved(self):
        session = copy.deepcopy(self.clarified)
        run = session['runs'][-1]
        answer = next(d for d in run['discussions'] if d['kind'] == 'answer')
        run['discussions'].append(copy.deepcopy(answer))
        row = self.row(session)
        self.assertEqual(row['clarifications']['questions'], 1)
        self.assertEqual(row['clarifications']['answered'], 1)
        self.assertEqual(row['clarifications']['answeredRatePct'], 100)
        for item in run['discussions']:
            if item['kind'] == 'answer':
                item['status'] = 'unresolved'
        row = self.row(session)
        self.assertEqual(row['clarifications']['unresolved'], 1)
        self.assertEqual(row['clarifications']['answered'], 0)
        self.assertEqual(row['clarifications']['answeredRatePct'], 100)
        run['discussions'] = [d for d in run['discussions'] if d['kind'] != 'answer']
        self.assertEqual(self.row(session)['clarifications']['unanswered'], 1)
        self.assertEqual(self.row(session)['clarifications']['answeredRatePct'], 0)

    def test_no_questions_is_not_perfect_and_missing_history_not_zero(self):
        self.run['discussions'] = []
        self.assertEqual(self.row()['clarifications']['questions'], 0)
        self.assertIsNone(self.row()['clarifications']['answeredRatePct'])
        del self.run['discussions']
        self.assertIsNone(self.row()['clarifications']['questions'])
        self.assertEqual(self.row()['clarifications']['unknownOpportunities'], 1)
        del self.run['qualityVersion']
        self.assertIsNone(self.row()['repairs']['startedBuilds'])

    def test_not_reached_dependencies_stay_in_coverage_without_failure(self):
        target = next(n for n in self.run['nodes'] if n['id'] == 'build-downstream')
        target.pop('started')
        result = build_role_cooperation([self.session])
        self.assertEqual(result['coverage']['plannedEdges'], 1)
        self.assertEqual(result['coverage']['notReachedEdges'], 1)
        self.assertEqual(result['coverage']['opportunities'], 0)
        self.assertEqual(result['rows'], [])

    def test_policy_change_stratifies_but_task_instructions_do_not(self):
        changed = copy.deepcopy(self.second)
        changed['runs'][-1]['plan']['modules'][0]['instructions'] = '不同的任务文字'
        self.assertEqual(len(build_role_cooperation([self.baseline, changed])['rows']), 1)
        changed['runs'][-1]['workPolicy']['firstPrinciples'] = False
        self.assertEqual(len(build_role_cooperation([self.baseline, changed])['rows']), 2)

    def test_demo_and_legacy_workflow_are_excluded_without_mutating_history(self):
        original = copy.deepcopy(self.session)
        demo = copy.deepcopy(self.session)
        demo['runs'][-1]['mode'] = 'demo'
        legacy = copy.deepcopy(self.session)
        legacy['runs'][-1]['workflowVersion'] = 1
        result = build_role_cooperation([self.session, demo, legacy])
        self.assertEqual(result['coverage']['eligibleRuns'], 1)
        self.assertEqual(self.session, original)


if __name__ == '__main__':
    unittest.main()
