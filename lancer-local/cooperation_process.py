"""Observed message diversity and fixed-grader gains; neither implies causality."""
from collections import Counter
import math
import re


def analyze_process(arm):
    calls = arm.get('calls', [])
    by_id = {call['id']: call for call in calls}
    positions = {call['id']: i for i, call in enumerate(calls)}
    edges = [(dependency, call['id']) for call in calls for dependency in call.get('deps', [])]
    invalid = len(by_id) != len(calls) or any(a not in positions or positions[a] >= positions[b]
                                           for a, b in edges)
    text = {call['id']: _message(call.get('result', {})) for call in calls}
    vectors = _tfidf(text)
    similarities = []
    if not invalid:
        for source, target in edges:
            if vectors.get(source) and vectors.get(target):
                similarity = _cosine(vectors[source], vectors[target])
                similarities.append({'source': source, 'target': target,
                                     'similarity': round(similarity, 6),
                                     'repetitionCandidate': similarity >= .9})
    workers = [call for call in calls if call.get('role') in ('worker1', 'worker2')]
    candidates = [call for call in workers if 'evaluation' in call]
    final = arm.get('evaluation')
    gains = _criterion_gains(candidates, final) if final and candidates else None
    peer = None
    if len(workers) == 2 and all(vectors.get(call['id']) for call in workers):
        peer = 1 - _cosine(vectors[workers[0]['id']], vectors[workers[1]['id']])
    return {'schema': 'yuki-process-diagnostics/v1',
            'graph': {'status': 'invalid' if invalid else 'observed', 'nodes': len(calls),
                      'edges': len(edges), 'pathCount': None if invalid else _paths(calls),
                      'pathDefinition': 'root_to_leaf',
                      'basis': 'actual_request_dependencies'},
            'lexicalDiversity': {
                'value': (round(sum(1 - row['similarity'] for row in similarities) / len(similarities), 6)
                          if similarities else None),
                'peerValue': round(peer, 6) if peer is not None else None,
                'measuredEdges': len(similarities), 'totalEdges': len(edges),
                'method': 'smoothed_tfidf_cosine_edge_mean',
                'tokenizer': 'lowercase_ascii_words_and_cjk_bigrams',
                'scope': '公开结构摘要与论据，不含代码正文、隐藏思考或完整消息语义',
                'pairs': similarities, 'repetitionThreshold': .9,
                'notice': 'GEMMAS启发的词法代理；未使用语义编码器，不是完整IDS。高相似只标候选，'
                          '可能是正确交接或复核；高多样性也可能是跑题，不能直接加分或删路径。'},
            'criterionGains': gains,
            'upr': {'value': None, 'status': 'unmeasured',
                    'reason': '未执行路径消融或获得有效的路径贡献标签，不能把词法相似度当作UPR。'},
            'roleAdherence': {'value': None, 'status': 'unmeasured',
                              'reason': '岗位标签不等于实际遵循，尚无独立行为标注。'},
            'notice': '所有候选使用同一固定验收器；新增/回退是产物差异，未证明某位成员造成该变化。'
                      '与单智能体的最终结果和完整资源账一起看，不将这些指标合成奖励分。'}


def _message(result):
    pieces = []
    for key in ('summary', 'approach', 'notes', 'findings', 'requirements', 'risks', 'workers'):
        value = result.get(key)
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, str):
                    pieces.append(row)
                elif isinstance(row, dict):
                    pieces.extend(v for k, v in row.items() if k in
                                  ('claim', 'evidence', 'proposal', 'instructions', 'task', 'role')
                                  and isinstance(v, str))
    return '\n'.join(pieces)


def _terms(text):
    terms = re.findall(r'[a-z0-9_]+', text.lower())
    for part in re.findall(r'[\u3400-\u9fff]+', text):
        terms.extend(part[i:i + 2] for i in range(len(part) - 1))
        if len(part) == 1:
            terms.append(part)
    return Counter(terms)


def _tfidf(messages):
    counts = {key: _terms(text) for key, text in messages.items() if text.strip()}
    frequencies = Counter(term for values in counts.values() for term in values)
    count = len(counts)
    return {key: {term: frequency * (math.log((1 + count) / (1 + frequencies[term])) + 1)
                  for term, frequency in values.items()} for key, values in counts.items()}


def _cosine(left, right):
    product = sum(value * right.get(term, 0) for term, value in left.items())
    norm = math.sqrt(sum(value * value for value in left.values()) *
                     sum(value * value for value in right.values()))
    return min(1, max(0, product / norm))


def _paths(calls):
    counts, parents = {}, set()
    for call in calls:
        dependencies = call.get('deps', [])
        counts[call['id']] = sum(counts[key] for key in dependencies) if dependencies else 1
        parents.update(dependencies)
    return sum(count for key, count in counts.items() if key not in parents)


def _criterion_gains(candidates, final):
    final_ids = {row['id'] for row in final['criteria']}
    if any({row['id'] for row in call['evaluation']['criteria']} != final_ids for call in candidates):
        return {'status': 'incomparable', 'reason': '候选与最终产物未使用相同验收项。'}
    final_pass = {row['id'] for row in final['criteria'] if row['status'] == 'passed'}
    passed = {call['id']: {row['id'] for row in call['evaluation']['criteria'] if row['status'] == 'passed'}
              for call in candidates}
    union = set().union(*passed.values())
    best = max(map(len, passed.values()))
    return {'status': 'observed', 'candidateCount': len(candidates),
            'totalCriteria': len(final_ids), 'finalPassed': len(final_pass), 'bestCandidatePassed': best,
            'netOverBestCandidate': len(final_pass) - best,
            'newBeyondAllCandidates': sorted(final_pass - union),
            'lostFromAnyCandidate': sorted(union - final_pass),
            'perCandidate': [{'callId': call['id'], 'model': call['model'],
                              'passed': len(passed[call['id']]),
                              'gainedInFinal': sorted(final_pass - passed[call['id']]),
                              'lostInFinal': sorted(passed[call['id']] - final_pass)}
                             for call in candidates],
            'basis': 'same_fixed_grader_on_saved_candidate_and_final_artifacts',
            'notice': '候选可能只承担局部职责；完整验收项的新增不等于修复该成员错误，缺失不等于失职。',
            'causalAttribution': False}
