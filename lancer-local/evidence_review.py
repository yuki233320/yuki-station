"""Re-review frozen outputs with evidence recovered from original tool events."""
import copy
import hashlib
import json
import threading
from pathlib import PurePosixPath
from urllib.parse import quote

from app import Conflict, safe_path, stamp, uid
from live import Cancelled
from execution_attempts import begin_attempt, finish_attempt
from runtime_versions import build_reference
from native_checks import bind_required_checks
from quality_assessment import artifact_manifest, build_assessment
from quality_workflow import _coordinator_acceptance, _record_quality
from studio_business import _check_business_snapshot, _commit_delivery, native_verification_checks


def start_evidence_review(studio, pid, sid, version):
    with studio.lock:
        session = studio.load(pid, sid)
        run, sources, previous = _validate_request(studio, session, version)
        review_id = uid()
        review = {'id': review_id, 'status': 'running', 'created': stamp(),
                  'sourceFinalNodeId': run['finalNodeId'], 'sourceQualityRound': previous['round'],
                  'sourceQualityEventSeq': previous['eventSeq'],
                  'sourceNodes': {mid: node['id'] for mid, node in sources.items()},
                  'nodeId': 'evidence-review-' + review_id,
                  'supplementRef': 'evidence-reviews/' + review_id + '/supplement.json',
                  'runtimeBuild': copy.deepcopy(getattr(studio, 'runtime_build', None))}
        begin_attempt(run, 'evidence_review', stamp(), uid(), build_reference(studio.runtime_build))
        run.setdefault('evidenceReviews', []).append(review)
        session['status'] = run['status'] = 'running'
        session['error'] = None
        studio.event(session, 'evidence_review_started', '从原始工具事件补证，仅由主模型独立复审已有产物。', review['nodeId'])
        review['eventSeq'] = session['events'][-1]['seq']
        studio.save(session)
        cancel = threading.Event()
        studio.cancels[sid] = cancel
        return studio.enqueue_existing(session, run, cancel, _execute_review,
                                       (studio, session, run, sources, previous, review, cancel))


def _validate_request(studio, session, version):
    if type(version) is not int or version != session['version']:
        raise Conflict('会话版本已更新，请刷新后再补证复审')
    sid = session['id']
    if (sid in studio.threads and studio.threads[sid].is_alive()) or session['status'] in ('queued', 'running'):
        raise ValueError('会话正在执行')
    if session['status'] not in ('failed', 'interrupted', 'stopped') or not session['runs']:
        raise ValueError('只能对最近一轮未完成验收的产物补证复审')
    run = session['runs'][-1]
    if (run.get('workflowVersion') != 2 or run.get('qualityVersion') != 1 or not run.get('taskContracts')
            or not run.get('qualityHistory') or not run.get('finalNodeId') or not run.get('artifacts')):
        raise ValueError('补证复审需要已有固定契约、候选产物和源验收记录')
    if not run.get('modelRequestVersion') and studio.registry.policy_info(run.get('coordinatorModel','opus'))['policyVersion'] > 0:
        raise ValueError('旧轮次未冻结模型参数且当前策略已变化；请开启新轮次复审，历史证据不改写')
    if run.get('coordinatorModel', 'opus') not in studio.available_models(run):
        raise ValueError('请先恢复本轮主模型连接；补证复审不调用执行模型')
    _check_business_snapshot(studio, session, run)
    state, _ = studio.store.current(session['projectId'])
    if state['revision'] != run['projectRevision']:
        raise Conflict('项目版本已更新，需基于新版本重新规划，不能复审旧产物后合入')
    sources = {}
    for module in run['plan']['modules']:
        node = next((n for n in reversed(run['nodes']) if n['phase'] == 'build'
                     and n.get('moduleId') == module['id'] and n['status'] == 'done'), None)
        if node is None:
            raise ValueError('模块尚无完成的产物：' + module['id'])
        sources[module['id']] = node
    if not any('nativeEvidence' in node['result'] for node in sources.values()):
        raise ValueError('没有可从原始工具事件补充的原生回执')
    return run, sources, run['qualityHistory'][-1]


def _execute_review(studio, session, run, sources, previous, review, cancel):
    try:
        root = studio.project_dir(session['projectId']) / session['id']
        complete = {mid: copy.deepcopy(node['result']) for mid, node in sources.items()}
        data_files = _candidate_files(root, run, complete)
        supplement = {'schema': 'yuki-evidence-supplement/v1', 'reviewId': review['id'],
                      'created': review['created'], 'sourceRunId': run['id'],
                      'sourceFinalNodeId': review['sourceFinalNodeId'],
                      'sourceQualityEventSeq': review['sourceQualityEventSeq'], 'modules': []}
        budget = [128000]
        for mid, output in complete.items():
            if 'nativeEvidence' in output:
                supplement['modules'].append(_supplement_native(studio, root, mid, output, run['taskContracts'][mid], budget))
        studio._write(safe_path(root, review['supplementRef']), supplement)
        with studio.lock:
            review.update(toolCount=sum(len(m['tools']) for m in supplement['modules']),
                          missingCount=sum(len(m['missing']) for m in supplement['modules']))
            studio.save(session)
        assessments = _assess_outputs(studio, session, run, complete, previous, supplement)
        if cancel.is_set():
            raise Cancelled()
        shared = {**copy.deepcopy(review), 'supplement': supplement}
        final = _coordinator_acceptance(studio, session, run, run['plan'], complete, assessments,
                                        previous['round'], cancel, evidence_review=shared)
        quality = _record_quality(studio, session, run, assessments, final, previous['round'], review_id=review['id'])
        review.update(qualityPassed=quality['passed'], qualityEventSeq=quality['eventSeq'])
        if not quality['passed']:
            raise ValueError('补证复审未通过；原始失败与缺失仍保留，本次不自动执行或修复模块。')
        if cancel.is_set():
            raise Cancelled()
        _commit_delivery(studio, session, run, data_files)
        studio.promote_role_memory(session, run)
        review['status'] = 'completed'
    except Exception as exc:
        with studio.lock:
            status = 'stopped' if isinstance(exc, Cancelled) else 'conflict' if isinstance(exc, Conflict) else 'failed'
            session['status'] = run['status'] = review['status'] = status
            session['error'] = review['error'] = studio.client.clean(str(exc)) or '补证复审已停止'
            run['ended'] = stamp()
            node = next((n for n in run['nodes'] if n['id'] == review['nodeId']), None)
            if node and node['status'] == 'running':
                node.update(status='interrupted' if status == 'stopped' else 'failed', error=review['error'])
                if node.get('responseValidation') == 'pending':
                    node['responseValidation'] = 'failed'
            studio.event(session, 'error', review['error'], review['nodeId'])
    finally:
        with studio.lock:
            node = next((n for n in run['nodes'] if n['id'] == review['nodeId']), {})
            review.update(contextRef=node.get('contextRef'), responseRef=node.get('responseRef'), completed=stamp())
            studio.event(session, 'evidence_review_completed', '补证复审完成' if review['status'] == 'completed' else review['error'], review['nodeId'], status=review['status'])
            review['completedEventSeq'] = session['events'][-1]['seq']
            finish_attempt(run, stamp())
            studio.save(session)


def _candidate_files(root, run, complete):
    files = {}
    for output in complete.values():
        for item in output['files']:
            data = safe_path(root, item['binaryRef']).read_bytes() if 'binaryRef' in item else item['content'].encode()
            path = safe_path(root / 'artifacts' / run['id'], item['path'])
            if not path.is_file() or path.is_symlink() or path.read_bytes() != data:
                raise ValueError('候选产物缺失或已变化：' + item['path'])
            files[item['path']] = data
    expected = {item['path']: item['sha256'] for item in run['artifacts']}
    if {name: hashlib.sha256(data).hexdigest() for name, data in files.items()} != expected:
        raise ValueError('候选产物与原验收清单不一致')
    return files


def _supplement_native(studio, root, mid, output, contract, budget):
    evidence = output['nativeEvidence']
    receipt_path = safe_path(root, evidence['receiptRef'])
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    events_ref = evidence['eventsRef']
    event_bytes = safe_path(root, events_ref).read_bytes()
    events = [json.loads(line) for line in event_bytes.splitlines() if line]
    if any(receipt.get(key) != evidence.get(key) for key in ('threadId', 'turnId')):
        raise ValueError('原回执会话标识不一致：' + mid)
    completed = {e['item']['id']: e for e in events if e['type'] == 'tool_completed' and e.get('taskId') == receipt['taskId']}
    started = {e['item']['id']: e for e in events if e['type'] == 'tool_started' and e.get('taskId') == receipt['taskId']}
    missing, tool_refs = [], []
    _include_child_tools(root, evidence, receipt, events, completed, missing)
    for tool in evidence['tools']:
        event = completed.get(tool['id'])
        if event is None:
            missing.append({'toolId': tool['id'], 'field': 'completedEvent'})
            continue
        item = event['item']
        ref = event.get('sourceRef') or events_ref + '#event-' + str(event['seq'])
        observed_status = 'completed' if item['type'] == 'webSearch' else item.get('status')
        if observed_status != tool['status']:
            missing.append({'toolId': tool['id'], 'field': 'statusMismatch', 'eventRef': ref})
        row = {'toolId': tool['id'], 'eventSeq': event.get('seq'), 'eventRef': ref,
               'status': observed_status, 'eventSha256': hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
        if event.get('childReceiptSha256'):
            row.update(childReceiptRef=event['childReceiptRef'], childReceiptSha256=event['childReceiptSha256'],
                       threadId=tool['threadId'], parentThreadId=tool['parentThreadId'], turnId=tool['turnId'])
        if tool['id'] in started:
            row['startedEventRef'] = events_ref + '#event-' + str(started[tool['id']]['seq'])
        tool_refs.append(row)
        if item['type'] not in ('mcpToolCall', 'dynamicToolCall', 'webSearch'):
            continue
        is_search = bool(tool.get('searchKind')) or item['type'] == 'webSearch'
        arguments = item.get('arguments', (started.get(tool['id'], {}).get('item') or {}).get('arguments'))
        if item['type'] == 'webSearch':
            arguments = item.get('action') or item.get('query')
        if arguments is None:
            missing.append({'toolId': tool['id'], 'field': 'arguments', 'eventRef': ref})
        else:
            _excerpt(studio, tool, 'input', json.dumps(arguments, ensure_ascii=False), 8192, ref, budget)
            if is_search:
                tool['action'] = {'type': 'observedRequest', 'argumentsExcerpt': tool['inputExcerpt'],
                                  'argumentsRef': ref, 'truncated': tool['inputTruncated']}
        result = item.get('results') if item['type'] == 'webSearch' else item.get('result')
        if item.get('error') is not None:
            result = {'error': item['error'], 'result': result}
        if result is None:
            missing.append({'toolId': tool['id'], 'field': 'result', 'eventRef': ref})
        else:
            text = _search_text(result) if is_search else json.dumps(result, ensure_ascii=False)
            _excerpt(studio, tool, 'result' if is_search else 'output', text, 18000 if is_search else 8192, ref, budget)
    workspace = receipt_path.parent / 'workspace'
    manifest_bytes = safe_path(root, evidence['manifestRef']).read_bytes()
    manifest = json.loads(manifest_bytes)
    for entry in manifest['inputs'] + evidence['verifiedArtifacts']:
        file = safe_path(workspace, entry['path'])
        if not file.is_file() or file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
            missing.append({'field': 'fileHash', 'path': entry['path'], 'expectedSha256': entry['sha256']})
    if contract.get('nativeCheckVersion') == 1:
        output['nativeCheckEvidence'] = bind_required_checks(contract, evidence, workspace)
    if 'nativeSearchEvidence' in output:
        for search in output['nativeSearchEvidence']['searches']:
            tool = next(t for t in evidence['tools'] if t['id'] == search['toolId'])
            search.update({key: copy.deepcopy(tool[key]) for key in ('action', 'resultChars', 'resultTruncated', 'resultRef') if key in tool})
            # The same search evidence also appears in host checks and assessments.
            # Keep its body once, in nativeEvidence.tools, with stable references here.
            search.pop('resultExcerpt', None)
    return {'moduleId': mid, 'receiptRef': evidence['receiptRef'],
            'receiptSha256': hashlib.sha256(receipt_bytes).hexdigest(), 'eventsRef': events_ref,
            'eventsSha256': hashlib.sha256(event_bytes).hexdigest(), 'manifestRef': evidence['manifestRef'],
            'manifestSha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'artifacts': copy.deepcopy(evidence['verifiedArtifacts']), 'tools': tool_refs, 'missing': missing}


def _include_child_tools(root, evidence, receipt, events, completed, missing):
    children = {child['threadId']: child for child in receipt.get('subAgents', [])}
    observed = {e['item']['agentThreadId'] for e in events if e['type'] == 'sub_agent_activity'
                and e.get('taskId') == receipt['taskId']}
    snapshots = {}
    for tool in evidence['tools']:
        if not tool.get('childReceiptRef'):
            continue
        thread_id = tool['threadId']
        child = children.get(thread_id)
        if not child or thread_id not in observed or child.get('parentThreadId') != receipt['threadId']:
            missing.append({'toolId': tool['id'], 'field': 'childParentActivity'})
            continue
        if thread_id not in snapshots:
            ref = str(PurePosixPath(evidence['eventsRef']).parent / child['receiptRef'])
            raw = safe_path(root, ref).read_bytes()
            thread = json.loads(raw)['thread']
            parent = thread.get('source', {}).get('subAgent', {}).get('thread_spawn', {}).get('parent_thread_id')
            if (hashlib.sha256(raw).hexdigest() != child['receiptSha256'] or thread['id'] != thread_id
                    or parent != receipt['threadId'] or thread['cwd'] != receipt.get('workspace')
                    or child.get('parentTurnId') != receipt['turnId']):
                missing.append({'toolId': tool['id'], 'field': 'childReceiptBinding', 'receiptRef': ref})
                continue
            snapshots[thread_id] = (ref, thread)
        ref, thread = snapshots[thread_id]
        turn = next((t for t in thread['turns'] if t['id'] == tool['turnId'] and t.get('rootTurnId') == receipt['turnId']), None)
        item = next((i for i in turn['items'] if i['id'] == tool['originalToolId']), None) if turn else None
        if (item is None or turn['status'] != 'completed' or tool['parentThreadId'] != receipt['threadId']
                or tool['childReceiptRef'] != child['receiptRef'] or tool['id'] != thread_id + ':' + tool['originalToolId']):
            missing.append({'toolId': tool['id'], 'field': 'childToolBinding', 'receiptRef': ref})
            continue
        completed[tool['id']] = {'item': item, 'sourceRef': ref + '#turn=' + quote(turn['id'], safe='') + '&item=' + quote(item['id'], safe=''),
                                 'childReceiptRef': ref, 'childReceiptSha256': child['receiptSha256']}


def _excerpt(studio, tool, prefix, text, limit, ref, budget):
    text = studio.client.clean(text)
    limit = min(limit, budget[0])
    excerpt = text[:limit]
    if len(text) > limit and limit > 200:
        excerpt = text[:limit * 2 // 3] + '\n[…中间省略，完整内容见原始事件…]\n' + text[-(limit // 3 - 40):]
    tool.update({prefix + 'Excerpt': excerpt, prefix + 'Chars': len(text),
                 prefix + 'Truncated': len(text) > limit, prefix + 'Ref': ref})
    budget[0] -= len(excerpt)


def _search_text(result):
    if isinstance(result, dict) and isinstance(result.get('content'), list):
        return '\n'.join(c['text'] for c in result['content'] if c.get('type') == 'text')
    return json.dumps(result, ensure_ascii=False)


def _assess_outputs(studio, session, run, complete, previous, supplement):
    from review_evidence import review_version
    strict = review_version(run) == 1
    rows = []
    for module in run['plan']['modules']:
        mid = module['id']
        output = complete[mid]
        old = next(row for row in previous['modules'] if row['moduleId'] == mid)
        checks = studio.static_checks({f['path']: f for f in output['files']})
        # Supplementing old native evidence does not waive an independently
        # registered host gate and must never launch its checker again.
        from host_quality import module_checks
        checks += module_checks(run, previous['round'], mid)
        from worker_integration import checks as worker_checks
        checks += worker_checks(studio, session, run, module, output)
        if 'nativeEvidence' in output:
            checks += native_verification_checks(module, output)
            evidence = next(m for m in supplement['modules'] if m['moduleId'] == mid)
            checks.append({'name': '原始补证完整与产物指纹一致 · ' + mid, 'pass': not evidence['missing'],
                           'detail': json.dumps(evidence['missing'], ensure_ascii=False) if evidence['missing'] else '参数与返回值均来自原始事件，输入与产物 SHA-256 一致'})
        contract = run['taskContracts'][mid]
        manifest = artifact_manifest(studio, session, output)
        proof = None
        if strict:
            from review_evidence import validate_contract, verify_evidence
            if contract.get('reviewRequirementVersion') != 1:
                raise ValueError('补证复审缺少冻结的复核义务，不能补写或降级')
            schedule = validate_contract(module, contract)
            same = schedule['mode'] == 'same_model'
            review = old.get('sameModelReview' if same else 'independentReview')
            if (old.get('reviewSchedule') != schedule or old.get('reviewRequirements') != contract['reviewRequirements']
                    or old.get('reviewer') != schedule['reviewer']
                    or old.get('independentReview' if same else 'sameModelReview') is not None):
                raise ValueError('补证不能改变原复核身份、义务或审查模式')
            proof = copy.deepcopy(old.get('reviewEvidence'))
            verify_evidence(studio, session, run, module, contract, review, proof, manifest)
        else:
            review = old.get('independentReview') or old.get('sameModelReview')
        row = build_assessment(module, contract, output, manifest, checks, review, review_evidence=proof)
        if output.get('workerEvidence'):
            row['workerEvidence'] = copy.deepcopy(output['workerEvidence'])
        row.update(reviewer=old['reviewer'], reviewNodeId=old['reviewNodeId'],
                   criteria=copy.deepcopy(review['criteria']) if review else [],
                   findings=copy.deepcopy(review['findings']) if review else [])
        rows.append(row)
    return rows
