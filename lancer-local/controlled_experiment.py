"""Isolated SAS/MAS observations using each provider protocol's defaults."""
import copy
import hashlib
import io
import json
from pathlib import Path
import random
import re
import threading
import time
import zipfile

from app import Conflict, stamp, uid
from collaboration_evaluation import CASE_ID, DEVELOPMENT_CASE_ID, evaluate_delivery, public_case, public_cases
from live import Cancelled
from model_catalog import MODELS
from model_registry import validate_team_selection
from studio_client import SYSTEM
from work_policy import CODING_DISCIPLINE, FIRST_PRINCIPLES, read_policy


PROTOCOL = {
    'schema': 'yuki-controlled-protocol/v3',
    'single': [{'role': 'single', 'dependsOn': []}],
    'multi': [{'role': 'plan', 'dependsOn': []},
              {'role': 'worker1', 'dependsOn': ['plan']},
              {'role': 'worker2', 'dependsOn': ['plan']},
              {'role': 'aggregate', 'dependsOn': ['plan', 'worker1', 'worker2']}],
}
INVALID_PROTOCOL_NOTICE = ('旧实验协议未明确覆盖原业务案例中的文件归属与交接路径约束，'
                           '造成“各岗位完整候选”和“只有汇总岗位拥有最终文件”的契约冲突。'
                           '本记录仅保留原始请求、回复和评分供排错，不作为 MAS 失败或 SAS/MAS 优劣证据，'
                           '已排除同条件汇总；请使用当前协议新建实验。')
NOTICE = ('SAS 是单一上下文的一次产出；MAS 是规划、两个独立岗位候选、汇总四次调用。'
          'SAS 仅为一次产出基线，不代表允许自审或工具循环的最佳单智能体。'
          '不设置总额度或岗位固定配额；请求沿用各协议技术参数，无自动重试，费用由用户依据账单决定。'
          '两臂算力与费用不归一化，结果不能称为等预算优势。默认同型号控制模型差异，'
          '异构模式同时改变模型与架构，只能比较系统配置。两臂及重复实验依次运行、顺序随机；'
          '不接入生产调度，不修改用户项目文件，不读取用户历史或隐藏验收答案。')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


class ControlledExperimentRunner:
    def __init__(self, studio):
        self.studio = studio
        self.thread = None
        self.cancel = threading.Event()
        self.active = None
        for path in studio.root.glob('*/controlled-experiments/*/record.json'):
            record = json.loads(path.read_text())
            if record['status'] in ('running', 'queued'):
                record.update(status='interrupted', ended=stamp(), error='服务重启，未自动重试或续费。')
                for arm in record['arms'].values():
                    if arm['status'] == 'running':
                        arm['status'] = 'interrupted'
                    for call in arm['calls']:
                        if call['status'] == 'running':
                            call['status'] = 'interrupted'
                studio._write(path, record)

    def index(self, pid):
        from cooperation_metrics import evaluate_pair, summarize_pairs
        from cooperation_process import analyze_process
        records = [json.loads(p.read_text()) for p in
                   (self.studio.project_dir(pid) / 'controlled-experiments').glob('*/record.json')]
        for record in records:
            if record['budget']['allocation']['schema'] == 'yuki-controlled-protocol/v1':
                record['evaluationNotice'] = INVALID_PROTOCOL_NOTICE
            record['quantification'] = evaluate_pair(record)
            record['process'] = analyze_process(record['arms']['multi'])
            for arm in record['arms'].values():
                for call in arm['calls']:
                    if 'result' in call:
                        call['result'] = {'summary': call['result'].get('summary', '')}
        return {'cases': public_cases(), 'records': sorted(records, key=lambda r: (r['created'], r['repeatIndex']),
                                                        reverse=True), 'summaries': summarize_pairs(records), 'notice': NOTICE}

    def export(self, pid, experiment_id):
        if not isinstance(experiment_id, str) or not re.fullmatch('[a-f0-9]{12}', experiment_id):
            raise ValueError('无效实验编号')
        root = self.studio.project_dir(pid) / 'controlled-experiments' / experiment_id
        if not (root / 'record.json').is_file():
            raise ValueError('实验不存在')
        output = io.BytesIO()
        with self.studio.lock, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file() and path.suffix != '.pending':
                    archive.write(path, path.relative_to(root).as_posix())
        return output.getvalue()

    def start(self, pid, coordinator, members, case_id=CASE_ID, budget=None,
              repetitions=1, homogeneous=True):
        self.studio.project_dir(pid)
        selection = self._selection(coordinator, members, budget, repetitions, homogeneous)
        case = public_case(case_id)
        with self.studio.lock:
            if self.thread and self.thread.is_alive():
                raise Conflict('请等待当前受控对照结束。')
            batch_id = uid()
            records = [self._prepare(pid, case, selection, budget, homogeneous, batch_id, i + 1, repetitions)
                       for i in range(repetitions)]
            self.cancel = threading.Event()
            self.active = records[0]
            self.thread = threading.Thread(target=self._execute_batch, args=(records, case), daemon=True)
            self.thread.start()
            return copy.deepcopy(records[0])

    def stop(self, pid, experiment_id):
        if not isinstance(experiment_id, str) or not re.fullmatch('[a-f0-9]{12}', experiment_id):
            raise ValueError('无效实验编号')
        with self.studio.lock:
            record = json.loads((self.studio.project_dir(pid) / 'controlled-experiments' /
                                 experiment_id / 'record.json').read_text())
            if (self.active and self.active['projectId'] == pid
                    and self.active['batchId'] == record['batchId']):
                self.cancel.set()
            return record

    def _selection(self, coordinator, members, budget, repetitions, homogeneous):
        selection = validate_team_selection(coordinator, members)
        if budget is not None:
            raise ValueError('固定额度分配已取消，请刷新页面后重新发起实验。')
        if type(repetitions) is not int or not 1 <= repetitions <= 5:
            raise ValueError('重复次数须为 1–5 的整数')
        if type(homogeneous) is not bool:
            raise ValueError('同型号开关须为布尔值')
        if any(mid == 'native_codex' or MODELS[mid]['weights'].get('image', 0) for mid in members):
            raise ValueError('受控对照只支持文字 API，不支持原生执行器或图像模型')
        selected = [coordinator] if homogeneous else members
        if not homogeneous and len(selected) < 2:
            raise ValueError('异构对照至少需要两个模型配置')
        missing = set(selected) - set(self.studio.client.available())
        if missing:
            raise ValueError('请先连接实际参与的模型：' + '、'.join(sorted(missing)))
        return {**selection, 'activeModels': selected}

    def _profiles(self, selection, include_policy=True):
        frozen=self.studio.client.snapshot_profiles(selection['activeModels'])
        fields=('provider','base','model','protocol','requestPolicy') if include_policy else ('provider','base','model','protocol')
        return {mid:{key:profile[key] for key in fields} for mid,profile in frozen.items()}

    def _prepare(self, pid, case, selection, budget, homogeneous, batch_id, repeat_index, repeat_count):
        source = Path(__file__).parent
        grader_files = ['collaboration_evaluation.py']
        if case['id'] == DEVELOPMENT_CASE_ID:
            grader_files.append('development_evaluation.py')
        policy = read_policy(self.studio)
        evaluation_policy = {'schema': 'yuki-egt-policy/v2', 'costControl': False}
        record = {'schema': 'yuki-controlled-comparison/v1', 'id': uid(), 'projectId': pid,
                  'caseId': case['id'], 'title': case['title'], 'created': stamp(), 'status': 'queued',
                  'caseFingerprint': fingerprint(case), 'inputFingerprint': fingerprint(case['files']),
                  'graderFingerprint': fingerprint({name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                                    for name in grader_files}),
                  'protocolFingerprint': fingerprint({'protocol': PROTOCOL, 'policy': policy,
                      'evaluationPolicy': evaluation_policy,
                      'source': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      'client': hashlib.sha256((source / 'studio_client.py').read_bytes()).hexdigest()}),
                  'profilesFingerprint': fingerprint(self._profiles(selection)),
                  'modelRequestProfiles': self.studio.client.snapshot_profiles(selection['activeModels']),
                  'selection': selection, 'homogeneous': homogeneous, 'workPolicy': policy,
                  'evaluationPolicy': evaluation_policy,
                  'budget': {'mode': 'provider_default', 'unit': None, 'cap': None, 'allocation': PROTOCOL,
                             'enforcement': 'none', 'totalComputeMatched': False},
                  'batchId': batch_id, 'repeatIndex': repeat_index, 'repeatCount': repeat_count,
                  'order': random.sample(['single', 'multi'], 2), 'notice': NOTICE,
                  'arms': {name: {'status': 'waiting', 'calls': [], 'configuredBudget': budget,
                                 'inputFingerprint': fingerprint(case['files'])} for name in ('single', 'multi')}}
        ref, sha = self._evidence(record, 'task.json', case)
        record.update(taskRef=ref, taskSha256=sha)
        self._save(record)
        return record

    def _execute_batch(self, records, case):
        for record in records:
            self.active = record
            if self.cancel.is_set():
                record.update(status='stopped', ended=stamp())
                self._save(record)
                continue
            record.update(status='running', started=stamp())
            self._save(record)
            for name in record['order']:
                if self.cancel.is_set():
                    break
                self._execute_arm(record, name, case)
            record.update(status='stopped' if self.cancel.is_set() else 'completed', ended=stamp())
            self._save(record)

    def _execute_arm(self, record, name, case):
        arm = record['arms'][name]
        started = time.monotonic()
        arm.update(status='running', started=stamp())
        self._save(record)
        final_files = []
        diagnostic_ms = 0
        try:
            for specification in PROTOCOL[name]:
                if self.cancel.is_set():
                    raise Cancelled()
                call = self._call(record, name, case, specification)
                if call['status'] != 'completed':
                    arm.update(status=call['status'], error=call.get('error'))
                    break
                if specification['role'] in ('worker1', 'worker2'):
                    diagnostic_started = time.monotonic()
                    call['evaluation'] = evaluate_delivery(call['result']['files'], case['id'])
                    call['diagnosticEvaluationMs'] = round((time.monotonic() - diagnostic_started) * 1000, 2)
                    diagnostic_ms += call['diagnosticEvaluationMs']
                if specification['role'] in ('single', 'aggregate'):
                    final_files = call['result']['files']
            else:
                arm['status'] = 'completed'
        except Cancelled:
            arm['status'] = 'stopped'
        except Exception as exc:
            arm.update(status='failed', error=self.studio.client.clean(str(exc)))
        evaluation_started = time.monotonic()
        arm['evaluation'] = evaluate_delivery(final_files, case['id'])
        observed_ms = (time.monotonic() - started) * 1000
        arm['metrics'] = _metrics(arm['calls'], observed_ms - diagnostic_ms)
        arm['metrics'].update(observedWallMs=round(observed_ms, 2), diagnosticEvaluationMs=diagnostic_ms,
                              evaluationMs=round((time.monotonic() - evaluation_started) * 1000, 2))
        arm['ended'] = stamp()
        self._save(record)

    def _call(self, record, arm_name, case, specification):
        arm = record['arms'][arm_name]
        role = specification['role']
        previous = {call['role']: call for call in arm['calls']}
        model = record['selection']['coordinatorModel']
        workers = [mid for mid in record['selection']['activeModels'] if mid != model]
        if workers and role in ('worker1', 'worker2'):
            model = workers[(int(role[-1]) - 1) % len(workers)]
        call = {'id': uid(), 'agentId': arm_name + '-' + role, 'role': role, 'model': model,
                'cap': None,
                'deps': [previous[dependency]['id'] for dependency in specification['dependsOn']],
                'status': 'running', 'started': stamp(), 'receipt': {}}
        messages = self._messages(record, case, role, previous)
        ref, sha = self._evidence(record, arm_name + '/' + call['id'] + '.request.json',
                                  {'model': model, 'system': SYSTEM, 'messages': messages,
                                   'max_output_tokens': None,
                                   'budgetNotice': '未覆盖输出参数，沿用 StudioClient 协议技术默认值；这不是总费用或岗位额度预算。'})
        call.update(inputRef=ref, inputSha256=sha)
        arm['calls'].append(call)
        self._save(record)
        started = time.monotonic()
        output = {}
        try:
            frozen=record.get('modelRequestProfiles')
            if frozen is None:
                if fingerprint(self._profiles(record['selection'],False)) != record['profilesFingerprint']:
                    raise ValueError('模型连接配置已变化，保留记录且不发起本次请求；请新建对照。')
            else:
                current=self._profiles(record['selection'],False)
                if any(current[mid]!={key:profile[key] for key in ('provider','base','model','protocol')} for mid,profile in frozen.items()):
                    raise ValueError('冻结的对照连接已变化，请新建对照。')
            pool = 'control' if role == 'plan' else 'worker'
            with self.studio.capacity.slot(pool, 'controlled:' + record['id'], self.cancel):
                call.update(executionPool=pool, capacityWaitMs=round((time.monotonic() - started) * 1000, 2))
                with self.studio.client.request_scope(frozen,require_eligible=True):
                    result, receipt = self.studio.client.call(model, messages, self.cancel)
            output = {'result': result, 'receipt': receipt}
            call['receipt'] = receipt
            _validate_result(result, role, case)
            call.update(status='completed', result=result)
        except Exception as exc:
            call.update(status='stopped' if isinstance(exc, Cancelled) else 'failed',
                        error=self.studio.client.clean(str(exc)))
            if getattr(exc, 'receipt', None):
                call['receipt'] = exc.receipt
            output.update(error=call['error'], receipt=call['receipt'])
            if getattr(exc, 'raw_text', None) is not None:
                output['rawText'] = self.studio.client.clean(exc.raw_text)
            if getattr(exc, 'partial_text', None) is not None:
                output['partialText'] = self.studio.client.clean(exc.partial_text)
        call.update(elapsedMs=round((time.monotonic() - started) * 1000, 2), ended=stamp())
        ref, sha = self._evidence(record, arm_name + '/' + call['id'] + '.response.json', output)
        call.update(outputRef=ref, outputSha256=sha)
        self._save(record)
        return call

    def _messages(self, record, case, role, previous):
        guidance = []
        if record['workPolicy']['firstPrinciples']:
            guidance.append(FIRST_PRINCIPLES)
        if record['workPolicy']['codingDiscipline'] and case['id'] == DEVELOPMENT_CASE_ID:
            guidance.append(CODING_DISCIPLINE)
        allowed_paths = list(case.get('artifactPaths', ['result.json']))
        task = {'instructions': case['instructions'], 'files': case['files'], 'rubric': case['rubric'],
                'allowedArtifactPaths': allowed_paths}
        common = ('这是独立受控实验。以下实验角色协议仅覆盖任务文字及资料中关于调度、讨论轮次、'
                  '模块文件所有权和交接路径的生产工作流安排；业务规则、接口和验收标准仍完全适用。'
                  '模型不实际写共享目录，files 只是隔离回复中的候选数据，同名候选之间没有文件写入冲突。'
                  '除 plan 只返回规划外，single、worker1、worker2、aggregate 每个回复都必须独立包含完整交付，'
                  'files 路径恰好覆盖 allowedArtifactPaths，不得新增或改为 handoff、ledger 等交接文件路径。'
                  '只给简短工作摘要和完整可验收结果，不要输出隐藏思考过程，不要声称运行了工具或测试。'
                  '只能使用给定资料。')
        schema = {'summary': '简短工作结果与依据', 'allowedArtifactPaths': allowed_paths,
                  'files': [{'path': path, 'content': '完整文件内容'} for path in allowed_paths]}
        evidence = {}
        if role == 'plan':
            instruction = ('你是主规划者。为 worker1、worker2 指定两个互补岗位的名称和具体工作要求。'
                           '两岗位均独立产出 allowedArtifactPaths 中全部文件的完整候选；岗位差异是核验重点，'
                           '不是最终文件归属拆分。不得指派任何其他文件或交接路径。你只返回规划，不输出 files、'
                           '不写文件；之后由主模型汇总，不增加讨论或调用。')
            schema = {'summary': '全局方案', 'allowedArtifactPaths': allowed_paths, 'assignments': [
                {'agentId': name, 'title': '岗位名', 'task': '针对完整候选的具体职责和核验重点'}
                for name in ('worker1', 'worker2')]}
        elif role in ('worker1', 'worker2'):
            plan = previous['plan']['result']
            assignment = next(item for item in plan['assignments'] if item['agentId'] == role)
            instruction = '你承担已分配岗位。独立完成完整候选交付，不依赖另一岗位未提供的结果。'
            evidence = {'plan': plan, 'assignment': assignment}
        elif role == 'aggregate':
            instruction = '你是最终汇总者。核对两个岗位候选，采纳有依据的改进，输出唯一完整交付，不继续讨论。'
            evidence = {key: previous[key]['result'] for key in ('plan', 'worker1', 'worker2')}
        else:
            instruction = '你是单智能体，在这一个上下文中独立完成全部业务任务，直接提交完整交付。'
        payload = {'experimentProtocol': common, 'role': role, 'instruction': instruction,
                   'workGuidance': guidance, 'task': task, 'handoffs': evidence, 'responseSchema': schema}
        return [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]

    def _evidence(self, record, name, value):
        relative = Path('controlled-experiments') / record['id'] / name
        path = self.studio.project_dir(record['projectId']) / relative
        self.studio._write(path, value)
        return relative.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()

    def _save(self, record):
        with self.studio.lock:
            self.studio._write(self.studio.project_dir(record['projectId']) /
                               'controlled-experiments' / record['id'] / 'record.json', record)


def _validate_result(result, role, case):
    if not isinstance(result, dict) or not isinstance(result.get('summary'), str):
        raise ValueError('实验回复须包含 summary 字符串')
    if role == 'plan':
        assignments = result.get('assignments')
        if (not isinstance(assignments, list) or len(assignments) != 2
                or any(not isinstance(item, dict) for item in assignments)
                or sorted(item.get('agentId', '') for item in assignments) != ['worker1', 'worker2']
                or any(not isinstance(item.get(key), str) or not item[key].strip()
                       for item in assignments for key in ('title', 'task'))):
            raise ValueError('规划须为 worker1 和 worker2 各提供一个名称和明确职责')
        return
    files = result.get('files')
    allowed = set(case.get('artifactPaths', ['result.json']))
    if (not isinstance(files, list) or len(files) != len(allowed)
            or any(not isinstance(item, dict) or item.get('path') not in allowed
                   or not isinstance(item.get('content'), str) for item in files)
            or len({item['path'] for item in files}) != len(files)):
        raise ValueError('交付 files 须完整且唯一覆盖允许路径，内容必须是 UTF-8 字符串，不接受其他交接路径')


def _metrics(calls, wall_ms):
    known = []
    for call in calls:
        usage = call['receipt'].get('usage', {})
        incoming = usage.get('input_tokens', usage.get('prompt_tokens'))
        outgoing = usage.get('output_tokens', usage.get('completion_tokens'))
        if type(incoming) is int and incoming >= 0 and type(outgoing) is int and outgoing >= 0:
            if call['receipt'].get('protocol') == 'anthropic':
                incoming += sum(usage.get(key, 0) for key in ('cache_creation_input_tokens', 'cache_read_input_tokens')
                                if type(usage.get(key, 0)) is int and usage.get(key, 0) >= 0)
            known.append((incoming, outgoing))
    return {'wallMs': round(wall_ms, 2), 'callCount': len(calls),
            'errorCount': sum(call['status'] == 'failed' for call in calls),
            'cancelledCount': sum(call['status'] == 'stopped' for call in calls),
            'usage': {'inputTokens': sum(row[0] for row in known) if known else None,
                      'outputTokens': sum(row[1] for row in known) if known else None,
                      'totalTokens': sum(sum(row) for row in known) if known else None,
                      'knownCalls': len(known), 'totalCalls': len(calls),
                      'complete': bool(calls) and len(known) == len(calls)},
            'cost': {'status': 'unknown', 'complete': False, 'amount': None, 'currency': None},
            'requestedModels': sorted({call['model'] for call in calls}),
            'actualModels': sorted({call['receipt']['returnedModel'] for call in calls
                                    if isinstance(call['receipt'].get('returnedModel'), str)}),
            'notice': 'token 是供应商回执，含其计入 output 的推理；不另加 reasoning 明细，未知费用不填零。'}
