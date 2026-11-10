import unittest

from cooperation_process import analyze_process
from collaboration_evaluation import case_definition, evaluate_delivery
import json


def call(identifier, text, deps=(), role='worker1'):
    return {'id': identifier, 'model': 'example', 'role': role,
            'result': {'summary': text}, 'deps': list(deps)}


class ProcessTests(unittest.TestCase):
    def test_repetition_is_not_a_proof_of_unnecessary_path(self):
        result = analyze_process({'calls': [call('a', '核对库存优先级'),
                                             call('b', '核对库存优先级', ['a'])]})
        self.assertEqual(result['lexicalDiversity']['value'], 0)
        self.assertTrue(result['lexicalDiversity']['pairs'][0]['repetitionCandidate'])
        self.assertIsNone(result['upr']['value'])

    def test_dag_counts_two_branches_without_inventing_communication(self):
        calls = [call('p', 'plan', role='plan'), call('a', 'stock', ['p']),
                 call('b', 'evidence', ['p'], 'worker2'), call('f', 'finish', ['a', 'b'], 'aggregate')]
        result = analyze_process({'calls': calls})
        self.assertEqual(result['graph']['pathCount'], 2)
        self.assertEqual(result['graph']['edges'], 4)
        self.assertEqual(result['lexicalDiversity']['value'], 1)
        self.assertEqual(result['lexicalDiversity']['peerValue'], 1)

    def test_no_messages_or_invalid_edges_remain_unknown(self):
        self.assertIsNone(analyze_process({'calls': []})['lexicalDiversity']['value'])
        result = analyze_process({'calls': [call('a', 'summary', ['future'])]})
        self.assertEqual(result['graph']['status'], 'invalid')
        self.assertIsNone(result['graph']['pathCount'])
        self.assertIsNone(result['lexicalDiversity']['value'])
        partial = analyze_process({'calls': [call('a', ''), call('b', 'ok', ['a'])]})
        self.assertEqual(partial['lexicalDiversity']['measuredEdges'], 0)

    def test_candidate_regression_uses_real_fixed_grader(self):
        reference = case_definition()['reference']['result']
        good = evaluate_delivery([{'path': 'result.json', 'content': json.dumps(reference)}])
        reference['remainingInventory']['A'] = 99
        bad = evaluate_delivery([{'path': 'result.json', 'content': json.dumps(reference)}])
        worker = call('a', '库存已核对')
        worker['evaluation'] = good
        diagnostics = analyze_process({'calls': [worker], 'evaluation': bad})
        gains = diagnostics['criterionGains']
        self.assertEqual(gains['netOverBestCandidate'], -1)
        self.assertEqual(gains['lostFromAnyCandidate'], ['remainingInventory'])
        self.assertFalse(gains['causalAttribution'])
        worker['evaluation'] = bad
        gains = analyze_process({'calls': [worker], 'evaluation': good})['criterionGains']
        self.assertEqual(gains['newBeyondAllCandidates'], ['remainingInventory'])
        self.assertEqual(gains['netOverBestCandidate'], 1)

    def test_different_criterion_sets_are_not_mixed(self):
        a = call('a', 'test')
        a['evaluation'] = {'criteria': [{'id': 'old', 'status': 'passed'}]}
        result = analyze_process({'calls': [a], 'evaluation': {'criteria': [{'id': 'new', 'status': 'passed'}]}})
        self.assertEqual(result['criterionGains']['status'], 'incomparable')


if __name__ == '__main__':
    unittest.main()
