"""A fixed, multi-file development case graded against actual Python execution."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


CASE_ID = 'ticket-scheduler-dev-v1'
ARTIFACT_PATHS = ('scheduler.py', 'reports.py', 'cli.py')
RUBRIC = [
    ('parallel', '不同技能工单并行执行'), ('priority', '等待工单按优先级选择'),
    ('ties', '同级输入顺序与工人编号决定性选择'), ('dependencies', '依赖完成后才能启动'),
    ('reallocation', '完成事件后重新分配且同一工人不重叠'),
    ('unserviceable', '缺少技能及其下游明确阻塞'), ('cancellation', '取消工单不执行且阻塞其下游'),
    ('empty', '空任务和空工人结果'), ('invalid_ids', '重复任务或工人编号拒绝'),
    ('invalid_dependency', '不存在的依赖拒绝'), ('cycle', '依赖循环拒绝'),
    ('invalid_numbers', '无效时长和优先级拒绝'), ('immutable', '调用不改写输入'),
    ('summary', '报告统计由实际调度结果计算'), ('cli_success', '命令行输出完整JSON成果'),
    ('cli_error', '命令行错误有结构化结果与退出码'),
]


def case_definition():
    instructions = '''开发一个可复用的离线工单调度核心，交付 scheduler.py、reports.py、cli.py 三个完整 Python 文件。
只用 Python 3.9+ 标准库。不是按已知样例写 result.json；宿主会对源码运行额外输入，使用事先固定的16项标准评分。
建议分为三个有独立文件归属的模块：调度核心（development、complexity=4，最多2轮讨论），
统计报告（development、complexity=3，最多1轮），CLI集成（development、complexity=3，最多1轮，依赖前两者）。
主模型制定具体技术方案并进行代码审查；保留必要讨论，不为凑轮次重复发言。全部 requiredCapabilities 仅 text；
本次由文字API生成代码，宿主在交付后执行统一评测，不申请原生终端、联网或其他工具。不要声称已运行测试。
模型流程内各模块的完成条件仅为源码与接口检查，不能把“已经运行测试”列为API模块验收条件；
上述16项真实运行验收由宿主在整个流程结束后独立执行，任何模型自述都不能覆盖宿主结果。

固定接口与规则：
1. scheduler.schedule(payload) 接受 {tasks:[...],workers:[...]}，返回恰好含 assignments、blocked、cancelled 的对象。
任务含 id（非空字符串）、skill（非空字符串）、duration（正整数，bool不算整数）、priority（非负整数，bool不算整数）、
dependsOn（不重复的任务id数组），可选 cancelled（布尔，默认false）。工人含 id（非空字符串）、skills（不重复的非空字符串数组）。
任务和工人的编号各自唯一；依赖必须存在且整个依赖图无环（含取消任务）。不满足以上约束抛 ValueError。
tasks/workers可以为空。所有对象必须符合声明的类型；不修改调用者输入，额外字段无需拒绝。
2. 时间从整数0开始，工人一次执行一个任务，任务不可中断。工人只有包含任务skill才可执行。
同一时刻先处理全部完成事件，再按 priority 升序、同级按原tasks输入顺序，遍历所有已就绪未执行任务。
已就绪指所有依赖都已完成；无依赖立即就绪。有可用合适工人则选择id字典序最小者立即启动；
没有合适空闲工人时跳过该任务，继续尝试其他已就绪任务。时间推进至下一个完成事件。
3. cancelled=true的任务不运行。缺少技能、依赖取消或阻塞的任务最终列入blocked；没有运行事件且不能继续分配时停止。
assignments按实际启动顺序返回，每项恰好含 taskId、workerId、start、end；end=start+duration。
blocked与cancelled都按原tasks输入顺序列出id，两者互斥。所有可执行任务均须完成。
4. reports.summarize(payload,result) 接收有效输入和schedule返回值，不重复做输入验证。
返回恰好含 completed、blocked、cancelled（三个数量）、makespan（最大end，空为0）、totalBusy（sum(end-start)）、
workers（按worker id字典序，每项{id,busy,taskCount}，含未工作的工人）的对象。
5. python3 cli.py 从标准输入读取一个JSON对象，调用schedule及summarize；成功stdout输出唯一JSON对象
{schedule:调度结果,summary:统计结果}并exit0；无效JSON或ValueError时stdout为空，stderr输出唯一JSON对象
{error:非空错误说明}并exit2。模块导入不得读取stdin或执行CLI。
不要添加数据库、网页、框架或新功能，README不是必需交付。验收关注真实业务行为，模型自评不能替代运行结果。'''
    example = {'tasks': [_task('paint', 'design', 3, 1), _task('build', 'code', 4, 2, ['paint'])],
               'workers': [_worker('ada', 'design'), _worker('ben', 'code')]}
    return {'id': CASE_ID, 'title': '复杂开发：工单依赖调度、统计与命令行集成',
            'evaluationKind': 'python_runtime', 'artifactPaths': list(ARTIFACT_PATHS),
            'instructions': instructions, 'files': [
                {'path': 'inputs/example.json', 'content': json.dumps(example, ensure_ascii=False, indent=2)},
                {'path': 'specification.md', 'content': instructions}],
            'rubric': [{'id': key, 'title': title, 'required': True} for key, title in RUBRIC],
            'reference': {'cases': _checks(), 'notice': '固定预期只供宿主评分，不作为模型输入。'}}


def _task(task_id, skill='s', duration=1, priority=0, dependencies=None, cancelled=False):
    return {'id': task_id, 'skill': skill, 'duration': duration, 'priority': priority,
            'dependsOn': dependencies or [], 'cancelled': cancelled}


def _worker(worker_id, *skills):
    return {'id': worker_id, 'skills': list(skills)}


def _result(assignments=(), blocked=(), cancelled=()):
    return {'assignments': [dict(zip(('taskId', 'workerId', 'start', 'end'), row)) for row in assignments],
            'blocked': list(blocked), 'cancelled': list(cancelled)}


def _checks():
    def check(key, tasks, workers, expected, operation='schedule'):
        return {'criterion': key, 'operation': operation,
                'payload': {'tasks': tasks, 'workers': workers}, 'expected': expected}
    s = [_worker('w', 's')]
    rows = [
        check('parallel', [_task('A', 'api', 4, 2), _task('B', 'ui', 2, 1)],
              [_worker('w1', 'api'), _worker('w2', 'ui')], _result([('B', 'w2', 0, 2), ('A', 'w1', 0, 4)])),
        check('priority', [_task('A', duration=4, priority=2), _task('B', duration=2, priority=1)], s,
              _result([('B', 'w', 0, 2), ('A', 'w', 2, 6)])),
        check('ties', [_task('B', duration=2), _task('A', duration=3)], [_worker('z', 's'), _worker('a', 's')],
              _result([('B', 'a', 0, 2), ('A', 'z', 0, 3)])),
        check('dependencies', [_task('A', duration=3, priority=2), _task('B', duration=2, dependencies=['A']),
                               _task('C', duration=1, priority=1)], [_worker('a', 's'), _worker('b', 's')],
              _result([('C', 'a', 0, 1), ('A', 'b', 0, 3), ('B', 'a', 3, 5)])),
        check('reallocation', [_task('A', duration=2), _task('B', duration=2), _task('C'),
                               _task('D', dependencies=['A', 'B'])], [_worker('a', 's'), _worker('b', 's')],
              _result([('A', 'a', 0, 2), ('B', 'b', 0, 2), ('C', 'a', 2, 3), ('D', 'b', 2, 3)])),
        check('unserviceable', [_task('U', 'x'), _task('V', dependencies=['U']), _task('A')], s,
              _result([('A', 'w', 0, 1)], ['U', 'V'])),
        check('cancellation', [_task('A', cancelled=True), _task('B', dependencies=['A']), _task('C')], s,
              _result([('C', 'w', 0, 1)], ['B'], ['A'])),
        check('empty', [], [], _result()),
        check('empty', [_task('A')], [], _result(blocked=['A'])),
        check('invalid_ids', [_task('A'), _task('A')], s, 'ValueError', 'error'),
        check('invalid_ids', [_task('A')], [_worker('w', 's'), _worker('w', 's')], 'ValueError', 'error'),
        check('invalid_dependency', [_task('A', dependencies=['missing'])], s, 'ValueError', 'error'),
        check('cycle', [_task('A', dependencies=['B']), _task('B', dependencies=['A'])], s, 'ValueError', 'error'),
    ]
    for field, value in [('duration', 0), ('duration', True), ('duration', -1), ('priority', True), ('priority', -1)]:
        task = _task('A')
        task[field] = value
        rows.append(check('invalid_numbers', [task], s, 'ValueError', 'error'))
    basic = {'tasks': [_task('B', duration=2, priority=1), _task('A', duration=3, priority=0)],
             'workers': [_worker('z', 's'), _worker('a', 's'), _worker('idle', 'x')]}
    result = _result([('A', 'a', 0, 3), ('B', 'z', 0, 2)])
    summary = {'completed': 2, 'blocked': 0, 'cancelled': 0, 'makespan': 3, 'totalBusy': 5,
               'workers': [{'id': 'a', 'busy': 3, 'taskCount': 1}, {'id': 'idle', 'busy': 0, 'taskCount': 0},
                           {'id': 'z', 'busy': 2, 'taskCount': 1}]}
    rows.extend([
        {'criterion': 'immutable', 'operation': 'immutable', 'payload': basic, 'expected': True},
        {'criterion': 'summary', 'operation': 'summary', 'payload': basic, 'result': result, 'expected': summary},
        {'criterion': 'cli_success', 'operation': 'cli', 'payload': basic,
         'expected': {'schedule': result, 'summary': summary}},
        {'criterion': 'cli_error', 'operation': 'cli_error', 'raw': '{invalid', 'expected': True},
        {'criterion': 'cli_error', 'operation': 'cli_error',
         'payload': {'tasks': [_task('A', duration=0)], 'workers': s}, 'expected': True},
    ])
    return rows


RUNNER = r'''
import copy, importlib, json, os, subprocess, sys
sys.path.insert(0, os.getcwd())
queries = json.load(sys.stdin)
answers = []
for query in queries:
    try:
        op = query['operation']
        payload = copy.deepcopy(query.get('payload'))
        before = copy.deepcopy(payload)
        if op in ('cli', 'cli_error'):
            proc = subprocess.run([sys.executable, '-B', 'cli.py'],
                input=query.get('raw', json.dumps(payload)), text=True, capture_output=True, timeout=3)
            if op == 'cli':
                value = json.loads(proc.stdout) if proc.returncode == 0 and not proc.stderr.strip() else None
            else:
                error = json.loads(proc.stderr) if proc.stderr.strip() else {}
                value = (proc.returncode == 2 and not proc.stdout.strip() and isinstance(error, dict)
                         and set(error) == {'error'} and isinstance(error['error'], str) and bool(error['error'].strip()))
            answers.append({'value': value, 'exitCode': proc.returncode,
                            'stdout': proc.stdout[:4000], 'stderr': proc.stderr[:4000]})
            continue
        schedule = importlib.import_module('scheduler').schedule
        if op == 'summary':
            value = importlib.import_module('reports').summarize(payload, query['result'])
        else:
            value = schedule(payload)
        if op == 'immutable':
            value = payload == before
        answers.append({'value': value})
    except Exception as error:
        answers.append({'error': type(error).__name__, 'detail': str(error)[:1000]})
print(json.dumps(answers, ensure_ascii=False))
'''


def evaluate_delivery(files):
    selected = [item for item in files if item.get('path') in ARTIFACT_PATHS]
    paths = [item['path'] for item in selected]
    if not selected:
        return _evaluation({}, None, 'not_run', '未交付待运行的Python文件。')
    if len(paths) != len(set(paths)) or any(not isinstance(item.get('content'), str) for item in selected):
        return _evaluation({}, None, 'failed', '交付文件必须唯一且为UTF-8文本。')
    content = {item['path']: item['content'] for item in selected}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if set(paths) != set(ARTIFACT_PATHS):
        return _evaluation({}, digest, 'failed', '缺少文件：' + '、'.join(sorted(set(ARTIFACT_PATHS) - set(paths))))
    checks = _checks()
    queries = [{key: value for key, value in row.items() if key not in ('expected', 'criterion')} for row in checks]
    answers, runtime = _execute(content, queries)
    if answers is None:
        return _evaluation({}, digest, 'failed', runtime['error'], runtime)
    results = {}
    for index, (check, answer) in enumerate(zip(checks, answers)):
        correct = (answer.get('error') == check['expected'] if check['operation'] == 'error'
                   else 'value' in answer and _same_value(answer['value'], check['expected']))
        results.setdefault(check['criterion'], []).append({'passed': correct, 'caseIndex': index,
            'detail': '实际运行符合固定预期。' if correct else
                '固定预期：' + json.dumps(check['expected'], ensure_ascii=False) + '；实际：' + json.dumps(answer, ensure_ascii=False)})
    return _evaluation(results, digest, runtime=runtime)


def _same_value(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same_value(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_value(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _execute(content, queries):
    command = [sys.executable, '-I', '-B', '-c', RUNNER]
    runtime = {'command': [sys.executable, '-I', '-B', '-c', '<fixed development evaluator>'],
               'exitCode': None, 'stdout': '', 'stderr': '',
               'notice': '临时目录运行固定文件，环境不继承API凭据，限时10秒；这不是安全沙箱。'}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='yuki-development-') as directory:
        for path, text in content.items():
            (Path(directory) / path).write_text(text, encoding='utf8')
        try:
            process = subprocess.run(command, cwd=directory, input=json.dumps(queries),
                capture_output=True, text=True, timeout=10,
                env={'PATH': os.defpath, 'LANG': 'en_US.UTF-8'})
            runtime.update(exitCode=process.returncode, stdout=process.stdout[:24000], stderr=process.stderr[:8000])
            answers = json.loads(process.stdout) if process.returncode == 0 else None
            if (not isinstance(answers, list) or len(answers) != len(queries)
                    or any(not isinstance(row, dict) for row in answers)):
                raise ValueError('运行未返回完整的固定检查结果')
        except subprocess.TimeoutExpired:
            answers = None
            runtime['error'] = '开发成果运行超过10秒，未通过验收。'
        except (OSError, ValueError) as error:
            answers = None
            runtime['error'] = '无法完成开发成果验收：' + str(error)
    runtime['elapsedMs'] = round((time.monotonic() - started) * 1000)
    return answers, runtime


def _evaluation(results, fingerprint, forced=None, detail=None, runtime=None):
    criteria = []
    for key, title in RUBRIC:
        rows = results.get(key, [])
        passed = bool(rows) and all(row['passed'] for row in rows)
        criteria.append({'id': key, 'title': title, 'required': True,
            'status': forced or ('passed' if passed else 'failed'),
            'detail': detail or ('实际运行通过该标准下全部固定用例。' if passed else
                                 '\n'.join(row['detail'] for row in rows if not row['passed']))})
    count = sum(row['status'] == 'passed' for row in criteria)
    return {'criteria': criteria, 'passed': count, 'total': len(criteria),
            'scorePct': round(100 * count / len(criteria), 1), 'accepted': count == len(criteria),
            'artifactFingerprint': fingerprint, 'runtime': runtime}
