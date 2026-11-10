"""Isolated, sequential trials of one fixed business case through the real Studio."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import random
import re
import statistics
import threading
import time

from app import Conflict, stamp, uid
from collaboration_evaluation import CASE_ID, DEVELOPMENT_CASE_ID, case_definition, public_case, public_cases, evaluate_delivery, run_metrics, compare_arms
from collaboration_evaluation import duration_efficiency, efficiency_points
from model_catalog import MODELS
from model_registry import validate_team_selection
from project_team import team_defaults


NOTICE = ('同一案例的两个隔离项目使用相同输入、岗位规范和固定验收答案；执行顺序随机，依次运行。'
          '这是整个工作流的描述性对照，不是模型排行榜或普遍增益证明。单一模型臂允许同一型号承担多个岗位。'
          '模型只调用文字 API；开发案例另由宿主在临时目录运行交付代码验收。'
          '双臂复制项目设置中的岗位规范，不复制项目文件或历史记忆。')

INVALID_CASES = {'research-evidence-v1':
    '此版本的题目与评分标准冲突：材料明确未批准，回答“已确认未批准”却被误判。'
    '历史原始评分保留但无效，不作为模型失败或协作收益证据，已排除同条件汇总；请使用 v2 重跑。'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def comparison_summaries(records):
    groups = {}
    for record in records:
        if record['caseId'] in INVALID_CASES:
            continue
        selection = record['selection']
        key = fingerprint([record['caseId'], record['caseFingerprint'], record['graderFingerprint'],
                           record['inputFingerprint'], record.get('workflowFingerprint'),
                           record.get('profilesFingerprint'), record.get('workPolicy'), selection['coordinatorModel'],
                           sorted(selection['teamModels'])])
        groups.setdefault(key, []).append(record)
    result = []
    for key, rows in groups.items():
        first = rows[0]
        assessed = [r for r in rows if all('evaluation' in a for a in r['arms'].values())]
        row = {'groupId': key, 'caseId': first['caseId'], 'title': first['title'],
               **first['selection'], 'scheduledPairs': len(rows), 'assessedPairs': len(assessed),
               'incompletePairs': len(rows) - len(assessed), 'bothPassed': 0,
               'singleOnlyPassed': 0, 'multiOnlyPassed': 0, 'neitherPassed': 0,
               'recordIds': [r['id'] for r in rows]}
        deltas, ratios, reductions = [], [], []
        for sample in assessed:
            a, b = sample['arms']['single'], sample['arms']['multi']
            passed_a = a['status'] == 'completed' and a['evaluation']['accepted']
            passed_b = b['status'] == 'completed' and b['evaluation']['accepted']
            outcome = ('bothPassed' if passed_a and passed_b else 'singleOnlyPassed' if passed_a
                       else 'multiOnlyPassed' if passed_b else 'neitherPassed')
            row[outcome] += 1
            deltas.append(b['evaluation']['scorePct'] - a['evaluation']['scorePct'])
            reduction = duration_efficiency(a, b)['durationReductionPct']
            if reduction is not None:
                ratios.append(a['metrics']['wallMs'] / b['metrics']['wallMs'])
                reductions.append(reduction)
        median_reduction = statistics.median(reductions) if reductions else None
        row.update(meanQualityDeltaPoints=round(statistics.mean(deltas), 2) if deltas else None,
                   matchedSuccessTimingPairs=len(ratios),
                   medianSpeedupRatio=round(statistics.median(ratios), 3) if ratios else None,
                   medianDurationReductionPct=median_reduction,
                   globalPoints=efficiency_points(median_reduction),
                   notice='仅合并案例、输入、评测器、工作流及模型配置相同的记录。失败不剔除，未完成另列；'
                          '速度比仅使用双方达标且有真实历时的配对。重复同一案例不是多个独立业务任务，'
                          '整体用时降幅取配对百分比的中位数，按未舍入中位数每减少10%计1分，负值不加分、未知不记0。'
                          '局部效率基线待建立，不叠加计分。描述性积分不代表统计显著性或普遍收益；不自动调整岗位能力权重。')
        result.append(row)
    return result


class ComparisonRunner:
    def __init__(self, studio):
        self.studio = studio
        self.thread = None
        self.cancel = threading.Event()
        self.active = None
        for path in studio.root.glob('*/comparisons/*.json'):
            record = json.loads(path.read_text())
            if record['status'] in ('running', 'queued'):
                record.update(status='interrupted', ended=stamp(), error='服务已重启；保留两组会话，不自动续跑或再次收费。')
                studio._write(path, record)

    def index(self, pid):
        root = self.studio.project_dir(pid) / 'comparisons'
        records = [json.loads(p.read_text()) for p in root.glob('*.json')]
        for record in records:
            if record['caseId'] in INVALID_CASES:
                record['evaluationNotice'] = INVALID_CASES[record['caseId']]
            elif record.get('comparison'):
                record['comparison']['efficiency'] = duration_efficiency(record['arms']['single'], record['arms']['multi'])
        return {'case': public_case(), 'cases': public_cases(),
                'records': sorted(records, key=lambda r: (r['created'], r.get('repeatIndex', 1)), reverse=True),
                'summaries': comparison_summaries(records),
                'notice': NOTICE}

    def start(self, pid, coordinator, members, case_id=CASE_ID, repetitions=1):
        self.studio.project_dir(pid)
        selection = self._selection(coordinator, members)
        case = case_definition(case_id)
        if case_id == DEVELOPMENT_CASE_ID:
            from model_catalog import reviewers
            reviewers(selection['teamModels'], coordinator)
        if type(repetitions) is not int or not 1 <= repetitions <= 5:
            raise ValueError('重复次数须为1至5的整数')
        with self.studio.lock:
            if self.thread and self.thread.is_alive():
                raise Conflict('请等待当前对照结束，避免两次实验争用模型请求额度。')
            config = self.studio.meta(pid).get('teamConfig', team_defaults())
            role_config = {k: copy.deepcopy(config.get(k, {})) for k in ('roleGuides', 'customRoles')}
            batch_id = uid()
            records = [self._prepare_record(pid, case, selection, role_config, batch_id, i + 1, repetitions)
                       for i in range(repetitions)]
            self.cancel = threading.Event()
            self.active = records[0]
            self.thread = threading.Thread(target=self._execute_batch, args=(records,), daemon=True)
            self.thread.start()
            return copy.deepcopy(records[0])

    def _prepare_record(self, pid, case, selection, role_config, batch_id, repeat_index, repeat_count):
        source = Path(__file__).parent
        workflow = ['studio.py', 'studio_business.py', 'project_team.py', 'task_contracts.py',
                    'quality_workflow.py', 'quality_assessment.py', 'business_routing.py', 'model_catalog.py',
                    'studio_client.py', 'collaboration_comparison.py', 'contribution_ledger.py', 'contribution_tracking.py', 'work_policy.py']
        from work_policy import read_policy
        profiles = {m: {k: self.studio.client.profiles[m].get(k) for k in ('provider', 'base', 'model', 'protocol', 'requestPolicy')}
                    for m in selection['teamModels']}
        record = {'schema': 'yuki-collaboration-comparison/v1', 'id': uid(), 'projectId': pid,
                  'title': case['title'], 'caseId': case['id'], 'caseFingerprint': fingerprint(case),
                  'graderFingerprint': fingerprint({name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                      for name in (['collaboration_evaluation.py', 'development_evaluation.py']
                                   if case['id'] == DEVELOPMENT_CASE_ID else ['collaboration_evaluation.py'])}),
                  'workflowFingerprint': fingerprint({p: hashlib.sha256((source / p).read_bytes()).hexdigest() for p in workflow}),
                  'profilesFingerprint': fingerprint(profiles),
                  'modelRequestProfiles': self.studio.client.snapshot_profiles(selection['teamModels']),
                  'created': stamp(), 'status': 'queued', 'arms': {}, 'selection': selection,
                  'workPolicy': read_policy(self.studio),
                  'batchId': batch_id, 'repeatIndex': repeat_index, 'repeatCount': repeat_count,
                  'order': random.sample(['single', 'multi'], 2), 'notice': NOTICE}
        for arm in ('single', 'multi'):
            record['arms'][arm] = self._prepare(case, arm, selection, role_config)
        inputs = [arm['inputFingerprint'] for arm in record['arms'].values()]
        if len(set(inputs)) != 1:
            raise ValueError('对照输入不一致，未发起模型调用。')
        record['inputFingerprint'] = inputs[0]
        self._save(record)
        return record

    def stop(self, pid, comparison_id):
        with self.studio.lock:
            record = self._load(pid, comparison_id)
            if self.active and self.active.get('batchId', self.active['id']) == record.get('batchId', record['id']) and self.active['projectId'] == pid:
                self.cancel.set()
                for arm in self.active['arms'].values():
                    self.studio.stop(arm['projectId'], arm['sessionId'])
            return record

    def _selection(self, coordinator, members):
        selection = validate_team_selection(coordinator, members)
        if len(selection['teamModels']) < 2:
            raise ValueError('对照工作小组至少需要主模型和另一位文字模型。')
        if any(m == 'native_codex' or MODELS[m]['weights'].get('image', 0) for m in selection['teamModels']):
            raise ValueError('本对照只支持文字 API 模型；请在工作小组中移除原生执行器和图像模型。')
        missing = set(selection['teamModels']) - set(self.studio.client.available())
        if missing:
            raise ValueError('请先连接选中的模型：' + '、'.join(MODELS[m]['name'] for m in sorted(missing)))
        return selection

    def _prepare(self, case, arm, selection, role_config):
        studio = self.studio
        pid = studio.create_project('协作对照 · ' + case['title'], case['instructions'][:5000],
                                    '独立对照案例。只使用本项目给定输入；不请求外部资料，不扩大任务范围。')['id']
        meta = studio.meta(pid)
        meta['teamConfig'].update(copy.deepcopy(role_config))
        studio._write(studio.project_dir(pid) / 'project.json', meta)
        files = [{'path': f['path'], 'data': base64.b64encode(f['content'].encode()).decode()} for f in case['files']]
        revision = studio.store.current(pid)[0]['revision']
        project = studio.import_files(pid, files, revision)
        label = '单一模型' if arm == 'single' else '多模型小组'
        session = studio.create_session(pid, label + ' · ' + case['title'], case['instructions'],
                                        'development' if case['id'] == DEVELOPMENT_CASE_ID else 'general')
        team = [selection['coordinatorModel']] if arm == 'single' else selection['teamModels']
        studio.set_team(pid, session['id'], selection['coordinatorModel'], team, session['version'])
        return {'projectId': pid, 'sessionId': session['id'], 'runId': None, 'status': 'waiting',
                'teamModels': team, 'sourceRevision': project['revision'],
                'inputFingerprint': self._input_fingerprint(pid),
                'url': '/studio?project=' + pid + '&session=' + session['id']}

    def _input_fingerprint(self, pid):
        studio = self.studio
        state, workspace = studio.store.current(pid)
        meta = studio.meta(pid)
        inputs = {p.relative_to(workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(workspace.rglob('*')) if p.is_file()}
        return fingerprint({'files': inputs, 'goal': state['goal'], 'title': state['title'],
                            'instructions': meta['instructions'], 'teamConfig': meta['teamConfig'],
                            'memory': meta['memory']})

    def _execute_batch(self, records):
        for record in records:
            self.active = record
            if self.cancel.is_set():
                record.update(status='stopped', ended=stamp())
                self._save(record)
            else:
                self._execute(record)

    def _execute(self, record):
        studio = self.studio
        try:
            record.update(status='running', started=stamp())
            self._save(record)
            for name in record['order']:
                if self.cancel.is_set():
                    break
                arm = record['arms'][name]
                with studio.lock:
                    if self._input_fingerprint(arm['projectId']) != record['inputFingerprint']:
                        raise ValueError('实验输入已被更改，停止对照；请重新创建案例。')
                    session = studio.start(arm['projectId'], arm['sessionId'], work_policy=record.get('workPolicy'),
                                           comparison_arm=name, model_profiles=record.get('modelRequestProfiles'))
                    arm.update(status='running', runId=session['runs'][-1]['id'])
                    self._save(record)
                studio.threads[arm['sessionId']].join()
                session = studio.load(arm['projectId'], arm['sessionId'])
                run = session['runs'][-1]
                files = self._artifacts(session, run, record['caseId'])
                evaluation_started = time.monotonic()
                evaluation = evaluate_delivery(files, record['caseId'])
                metrics = run_metrics(run)
                if record['caseId'] == DEVELOPMENT_CASE_ID:
                    metrics['workflowWallMs'] = metrics['wallMs']
                    metrics['evaluationMs'] = round((time.monotonic() - evaluation_started) * 1000, 2)
                    if metrics['wallMs'] is not None:
                        metrics['wallMs'] += metrics['evaluationMs']
                arm.update(status=run['status'], error=session.get('error'),
                           evaluation=evaluation, metrics=metrics)
                if record['caseId'] != DEVELOPMENT_CASE_ID:
                    from contribution_tracking import record_objective_evaluations
                    record_objective_evaluations(studio, session, run, record['caseId'], record['graderFingerprint'])
                self._save(record)
            record['status'] = 'stopped' if self.cancel.is_set() else 'completed'
            if all('evaluation' in arm for arm in record['arms'].values()):
                record['comparison'] = compare_arms(record['arms']['single'], record['arms']['multi'], record['caseId'])
        except Exception as exc:
            record.update(status='failed', error=studio.client.clean(str(exc)))
        finally:
            record['ended'] = stamp()
            self._save(record)

    def _artifacts(self, session, run, case_id=CASE_ID):
        root = self.studio.project_dir(session['projectId']) / session['id'] / 'artifacts' / run['id']
        # Candidate files from failed gates are also evaluated; a failed run is
        # never silently dropped to improve the comparison's success rate.
        paths = ([root / name for name in case_definition(case_id)['artifactPaths']]
                 if case_id == DEVELOPMENT_CASE_ID else root.rglob('*.json'))
        return [{'path': p.relative_to(root).as_posix(), 'content': p.read_text(encoding='utf8')}
                for p in paths if p.is_file()]

    def _load(self, pid, comparison_id):
        if not isinstance(comparison_id, str) or not re.fullmatch('[a-f0-9]{12}', comparison_id):
            raise ValueError('无效对照编号')
        return json.loads((self.studio.project_dir(pid) / 'comparisons' / (comparison_id + '.json')).read_text())

    def _save(self, record):
        with self.studio.lock:
            self.studio._write(self.studio.project_dir(record['projectId']) / 'comparisons' / (record['id'] + '.json'), record)
