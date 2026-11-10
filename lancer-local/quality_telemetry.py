"""Project-local observations from recorded quality gates and actual calls."""
from role_cooperation import build_role_cooperation


def project_metrics(studio, pid):
    rows, requests = {}, {}
    sessions = studio.list_sessions(pid)
    for session in sessions:
        for run in session['runs']:
            if run.get('mode') != 'live' or run.get('qualityVersion') != 1:
                continue
            _quality_samples(rows, session['id'], run)
            _request_samples(requests, run.get('callMetrics', []))
    return {
        'schema': 'yuki-quality-telemetry/v1',
        'rows': [_quality_row(row) for _, row in sorted(rows.items())],
        'requests': [_request_row(row) for _, row in sorted(requests.items())],
        'roleCooperation': build_role_cooperation(sessions),
        'notice': '观测不是对照实验或模型能力排名，不自动调整路由。首次/最终通过指最早/最新已记录的质量关卡；'
                  '合入另以实际提交为准。请求返回不等于质量通过，错误率排除取消请求。调用耗时包含已记录的成功、错误和取消请求；'
                  '未记录实际调用耗时的历史不会用节点或回放时间补算。独立会话少于 3 个时样本不足。',
    }


def _quality_samples(rows, session_id, run):
    modules = {}
    for record in sorted(run.get('qualityHistory', []), key=lambda item: item['round']):
        for assessment in record['modules']:
            mid = assessment['moduleId']
            if mid not in modules:
                modules[mid] = {'first': assessment, 'last': assessment}
            else:
                modules[mid]['last'] = assessment
    for mid, sample in modules.items():
        first, last = sample['first'], sample['last']
        key = (first['model'], first['workType'], run.get('nativeCheckVersion', 0))
        row = rows.setdefault(key, {
            'model': key[0], 'workType': key[1], 'qualitySamples': 0, 'sessionIds': set(),
            'policyVersion': key[2], 'policyLabel': '必要命令验收' if key[2] == 1 else '全部命令严格验收',
            'firstPassCount': 0, 'finalPassCount': 0, 'mergedCount': 0, 'repairRounds': 0,
        })
        row['qualitySamples'] += 1
        row['sessionIds'].add(session_id)
        row['firstPassCount'] += first['gatePassed']
        row['finalPassCount'] += last['gatePassed']
        row['mergedCount'] += bool(last['gatePassed'] and run['status'] == 'completed' and run.get('mergedRevision'))
        row['repairRounds'] += sum(1 for node in run['nodes']
                                   if node.get('moduleId') == mid and node.get('repairRound', 0) > 0
                                   and node.get('started'))


def _request_samples(requests, records):
    for record in records:
        key = (record['model'], record['adapter'])
        row = requests.setdefault(key, {
            'model': key[0], 'adapter': key[1], 'requestCount': 0,
            'errorCount': 0, 'cancelledCount': 0, 'elapsedTotalMs': 0, 'latencySamples': 0,
        })
        row['requestCount'] += 1
        row['errorCount'] += record['outcome'] == 'error'
        row['cancelledCount'] += record['outcome'] == 'cancelled'
        if record.get('elapsedMs') is not None:
            row['elapsedTotalMs'] += record['elapsedMs']
            row['latencySamples'] += 1


def _quality_row(row):
    result = {key: value for key, value in row.items() if key != 'sessionIds'}
    result['independentSessions'] = len(row['sessionIds'])
    result['insufficientSamples'] = result['independentSessions'] < 3
    count = row['qualitySamples']
    for metric in ('firstPass', 'finalPass', 'merged'):
        result[metric + 'RatePct'] = round(row[metric + 'Count'] * 100 / count, 1) if count else None
    return result


def _request_row(row):
    result = {key: value for key, value in row.items() if key != 'elapsedTotalMs'}
    count = row['latencySamples']
    result['averageElapsedMs'] = round(row['elapsedTotalMs'] / count, 1) if count else None
    finished = row['requestCount'] - row['cancelledCount']
    result['errorRatePct'] = round(row['errorCount'] * 100 / finished, 1) if finished else None
    return result
