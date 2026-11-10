"""Run submitted source through the real development grader, including broken variants."""
import copy
import os
import unittest

from development_evaluation import ARTIFACT_PATHS, case_definition, evaluate_delivery


# This independently written submission exercises the grader, not a second copy
# of grading logic. Mutations below prove the corresponding contracts fail.
SCHEDULER = '''
def schedule(payload):
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    tasks, workers = payload.get('tasks'), payload.get('workers')
    if not isinstance(tasks, list) or not isinstance(workers, list):
        raise ValueError('tasks and workers must be lists')
    ids, worker_ids = set(), set()
    for row in tasks:
        if not isinstance(row, dict):
            raise ValueError('invalid task')
        if not isinstance(row.get('id'), str) or not row['id'] or row['id'] in ids:
            raise ValueError('invalid task id')
        ids.add(row['id'])
        if not isinstance(row.get('skill'), str) or not row['skill']:
            raise ValueError('invalid skill')
        if type(row.get('duration')) is not int or row['duration'] <= 0:
            raise ValueError('invalid duration')
        if type(row.get('priority')) is not int or row['priority'] < 0:
            raise ValueError('invalid priority')
        deps = row.get('dependsOn')
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps) or len(set(deps)) != len(deps):
            raise ValueError('invalid dependencies')
        if type(row.get('cancelled', False)) is not bool:
            raise ValueError('invalid cancellation')
    for row in workers:
        if not isinstance(row, dict):
            raise ValueError('invalid worker')
        if not isinstance(row.get('id'), str) or not row['id'] or row['id'] in worker_ids:
            raise ValueError('invalid worker id')
        worker_ids.add(row['id'])
        skills = row.get('skills')
        if not isinstance(skills, list) or any(not isinstance(s, str) or not s for s in skills) or len(set(skills)) != len(skills):
            raise ValueError('invalid worker skills')
    unresolved = {t['id']: set(t['dependsOn']) for t in tasks}
    if any(deps - ids for deps in unresolved.values()):
        raise ValueError('missing dependency')
    while unresolved:
        ready = {tid for tid, deps in unresolved.items() if not deps}
        if not ready:
            raise ValueError('cycle')
        unresolved = {tid: deps - ready for tid, deps in unresolved.items() if tid not in ready}
    pending = [t for t in tasks if not t.get('cancelled', False)]
    cancelled = [t['id'] for t in tasks if t.get('cancelled', False)]
    assignments, active, done = [], {}, set()
    now = 0
    while pending or active:
        for worker_id, (task_id, end) in list(active.items()):
            if end <= now:
                done.add(task_id)
                del active[worker_id]
        for task in sorted(pending[:], key=lambda t: t['priority']):
            if not set(task['dependsOn']) <= done:
                continue
            available = sorted([w for w in workers if w['id'] not in active and task['skill'] in w['skills']], key=lambda w: w['id'])
            if not available:
                continue
            worker_id = available[0]['id']
            end = now + task['duration']
            active[worker_id] = (task['id'], end)
            assignments.append({'taskId': task['id'], 'workerId': worker_id, 'start': now, 'end': end})
            pending.remove(task)
        if not active:
            break
        now = min(end for _, end in active.values())
    return {'assignments': assignments, 'blocked': [t['id'] for t in pending], 'cancelled': cancelled}
'''

REPORTS = '''
def summarize(payload, result):
    rows = result['assignments']
    workers = []
    for worker in sorted(payload['workers'], key=lambda w: w['id']):
        assigned = [r for r in rows if r['workerId'] == worker['id']]
        workers.append({'id': worker['id'], 'busy': sum(r['end'] - r['start'] for r in assigned), 'taskCount': len(assigned)})
    return {'completed': len(rows), 'blocked': len(result['blocked']), 'cancelled': len(result['cancelled']),
            'makespan': max((r['end'] for r in rows), default=0),
            'totalBusy': sum(r['end'] - r['start'] for r in rows), 'workers': workers}
'''

CLI = '''
import json
import sys
from scheduler import schedule
from reports import summarize

def main():
    try:
        payload = json.load(sys.stdin)
        result = schedule(payload)
        print(json.dumps({'schedule': result, 'summary': summarize(payload, result)}))
    except ValueError as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2
    return 0

if __name__ == '__main__':
    sys.exit(main())
'''


class DevelopmentEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.files = [{'path': path, 'content': text}
                      for path, text in zip(ARTIFACT_PATHS, (SCHEDULER, REPORTS, CLI))]

    def test_complete_program_runs_and_passes_fixed_business_checks(self):
        before = copy.deepcopy(self.files)
        result = evaluate_delivery(self.files)
        self.assertTrue(result['accepted'], result['criteria'])
        self.assertEqual((result['passed'], result['total'], result['scorePct']), (16, 16, 100))
        self.assertEqual(result['runtime']['exitCode'], 0)
        self.assertGreater(result['runtime']['elapsedMs'], 0)
        self.assertEqual(self.files, before)

    def test_case_declares_source_artifacts_and_keeps_expected_answers_separate(self):
        case = case_definition()
        self.assertEqual(case['artifactPaths'], list(ARTIFACT_PATHS))
        self.assertEqual(case['evaluationKind'], 'python_runtime')
        self.assertEqual(len(case['rubric']), 16)
        self.assertIn('cases', case['reference'])
        self.assertEqual({f['path'] for f in case['files']}, {'inputs/example.json', 'specification.md'})

    def test_real_behavior_mutations_fail_corresponding_criteria(self):
        variants = [
            (0, "key=lambda t: t['priority']", "key=lambda t: -t['priority']", 'priority'),
            (0, "available[0]['id']", "available[-1]['id']", 'ties'),
            (0, "if not set(task['dependsOn']) <= done:", "if False:", 'dependencies'),
            (1, "'totalBusy': sum(r['end'] - r['start'] for r in rows)", "'totalBusy': 0", 'summary'),
            (2, 'return 2', 'return 0', 'cli_error'),
        ]
        for index, old, new, criterion in variants:
            with self.subTest(criterion=criterion):
                files = copy.deepcopy(self.files)
                self.assertIn(old, files[index]['content'])
                files[index]['content'] = files[index]['content'].replace(old, new)
                result = evaluate_delivery(files)
                self.assertFalse(result['accepted'])
                self.assertEqual(next(r['status'] for r in result['criteria'] if r['id'] == criterion), 'failed')

    def test_missing_duplicate_and_syntax_error_sources_do_not_pass(self):
        for files in ([], self.files[:2], self.files + [self.files[0]],
                      [{'path': 'scheduler.py', 'content': 'not python ???'}, *self.files[1:]]):
            result = evaluate_delivery(files)
            self.assertFalse(result['accepted'])
        self.assertEqual(evaluate_delivery([])['criteria'][0]['status'], 'not_run')

    def test_extra_files_are_neither_written_nor_executed(self):
        files = self.files + [{'path': 'sitecustomize.py', 'content': "raise RuntimeError('must not execute')"}]
        self.assertTrue(evaluate_delivery(files)['accepted'])

    def test_host_credentials_are_not_inherited_by_executed_sources(self):
        key = 'YUKI_TEST_PROVIDER_TOKEN'
        original = os.environ.get(key)
        try:
            os.environ[key] = 'private-test-value'
            self.files[0]['content'] = 'import os\nassert "' + key + '" not in os.environ\n' + SCHEDULER
            self.assertTrue(evaluate_delivery(self.files)['accepted'])
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


if __name__ == '__main__':
    unittest.main()
