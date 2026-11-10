"""Offline paired observations of explicitly supplied real Studio tasks.

No models, acceptance scripts, servers, or historical synthetic benchmarks are
executed here. CLI: ``prepare --help``, ``import --help``, ``summarize --help``.
Prepare before running either revision. Keep inputs/criteria/scripts immutable;
run each planned slot separately through Studio and import its terminal run.

The import binding JSON must identify runId, taskFingerprint (manifest
fingerprint), inputFingerprint, acceptanceFingerprint, modelConfigFingerprint,
and revision. It is an operator attestation of the frozen inputs/configuration,
not proof that an old runtime recorded those inputs. Runtime revision is checked
against the actual run/attempt metadata. Acceptance JSON is optional and must
contain runId, acceptanceFingerprint, accepted (boolean), source (script/human),
completedAt (timezone-aware ISO), and evidence (a nonempty list of local files,
relative to that JSON). The scripts are hashed, never executed by this module.

Billing JSON: runId, amount, currency, source, kind="paid_invoice". Rates JSON:
currency, source, models: {actual_model: {inputPerMillion, outputPerMillion}}.
Rates are user-supplied blended prices over ALL reported input/output tokens;
cached tokens are not separately discounted, and reasoning tokens already in
output are not counted twice. Estimates never become billed amounts or budgets.
Imported evidence is local and trusted only at the explicitly recorded level.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import secrets
import statistics
from urllib.parse import urlsplit

SCHEMA = 'yuki-real-task-benchmark/v1'
TERMINAL = {'completed', 'failed', 'stopped', 'cancelled', 'interrupted', 'conflict'}
COORDINATION = {'draft', 'plan', 'review', 'decision', 'clarification', 'verification', 'integration'}
NOTICE = ('真实任务配对观察；导入不发起调用、不设置费用上限。失败、取消和未知验收均保留在已观察分母；'
          '未执行槽位另列。并行调用耗时之和不是关键路径，不能与端到端时间相加。'
          '同任务重复不是独立业务样本，不证明普遍增益；输入与模型绑定含操作者声明，历史未知不补造。')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf8'),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid JSON number')))


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' is required')
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _time(value):
    try:
        normalized = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value.replace('Z', '+00:00'))
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _duration(start, end):
    a, b = _time(start), _time(end)
    return round((b-a).total_seconds()*1000, 3) if a and b and b >= a else None


def file_record(path, name=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Evidence must be a regular non-symlink file: ' + str(path))
    raw = path.read_bytes()
    return {'path': name if name is not None else str(path.resolve()),
            'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def input_manifest(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Inputs must be an explicit non-symlink directory')
    records = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symlinks are not frozen inputs')
        if path.is_file():
            records.append(file_record(path, path.relative_to(root).as_posix()))
    if not records:
        raise ValueError('Real task inputs must not be empty')
    return {'files': records, 'fingerprint': fingerprint(records)}


def model_config(value):
    if not isinstance(value, dict) or not value:
        raise ValueError('Explicit model configuration is required')
    allowed = {'model', 'provider', 'protocol', 'endpoint', 'parameters'}
    for alias, profile in value.items():
        _text(alias, 'model alias')
        if not isinstance(profile, dict) or set(profile) - allowed:
            raise ValueError('Model profiles accept only public model/provider/protocol/endpoint/parameters')
        _text(profile.get('model'), 'model')
        if 'endpoint' in profile:
            url = urlsplit(profile['endpoint'])
            if url.username or url.password or url.query or url.fragment:
                raise ValueError('Endpoint must not contain credentials or query parameters')
        parameters = profile.get('parameters', {})
        if not isinstance(parameters, dict) or set(parameters) - {
                'temperature', 'top_p', 'max_tokens', 'max_output_tokens', 'max_completion_tokens',
                'reasoning_effort', 'thinking', 'seed', 'parallel_tool_calls', 'tool_choice'}:
            raise ValueError('Only explicit public generation parameters may be frozen')
        def check(obj):
            if isinstance(obj, dict):
                for key, val in obj.items():
                    normalized = key.lower().replace('_', '').replace('-', '')
                    if any(word in normalized for word in ('secret', 'token', 'password', 'authorization', 'apikey')):
                        # max_tokens is a model parameter, not a credential.
                        if key not in ('max_tokens', 'max_output_tokens', 'max_completion_tokens', 'budget_tokens'):
                            raise ValueError('Do not include credentials in model configuration')
                    check(val)
            elif isinstance(obj, list):
                for val in obj: check(val)
        check(profile)
    fingerprint(value)
    return deepcopy(value)


def prepare_manifest(task_id, brief, input_root, criteria, models, baseline_revision,
                     current_revision, repetitions=1, seed=None, script=None):
    _text(task_id, 'task id'); _text(brief, 'task brief')
    if type(repetitions) is not int or not 1 <= repetitions <= 100:
        raise ValueError('repetitions must be 1..100')
    if set(models) != {'baseline', 'current'}:
        raise ValueError('models must specify baseline and current')
    acceptance = {'criteria': file_record(criteria)}
    if script is not None: acceptance['script'] = file_record(script)
    acceptance['fingerprint'] = fingerprint(acceptance)
    seed = secrets.randbits(63) if seed is None else seed
    if type(seed) is not int: raise ValueError('seed must be an integer')
    rng = random.Random(seed)
    arms = {}
    for arm, revision in [('baseline', baseline_revision), ('current', current_revision)]:
        config = model_config(models[arm])
        arms[arm] = {'revision': _text(revision, 'runtime revision'), 'models': config,
                     'modelConfigFingerprint': fingerprint(config)}
    if baseline_revision == current_revision:
        raise ValueError('Baseline and current runtime revisions must differ')
    manifest = {'schema': SCHEMA, 'createdAt': datetime.now(timezone.utc).isoformat(),
                'task': {'id': task_id, 'brief': brief, 'origin': 'user_real_task'},
                'inputs': input_manifest(input_root), 'acceptance': acceptance, 'arms': arms,
                'seed': seed, 'pairs': [{'id': i+1, 'order': rng.sample(['baseline', 'current'], 2)}
                                       for i in range(repetitions)], 'notice': NOTICE}
    manifest['fingerprint'] = fingerprint(manifest)
    return manifest


def validate_manifest(manifest):
    if (manifest.get('schema') != SCHEMA or manifest.get('fingerprint') !=
            fingerprint({k: v for k, v in manifest.items() if k != 'fingerprint'})):
        raise ValueError('Manifest changed or has an unsupported schema')
    return manifest


def _usage(receipt):
    usage = receipt.get('usage') or {}
    incoming = usage.get('input_tokens', usage.get('prompt_tokens'))
    outgoing = usage.get('output_tokens', usage.get('completion_tokens'))
    if not all(type(v) is int and v >= 0 for v in (incoming, outgoing)):
        return None
    if receipt.get('protocol') == 'anthropic':
        caches = [usage.get(k, 0) for k in ('cache_creation_input_tokens', 'cache_read_input_tokens')]
        if not all(type(v) is int and v >= 0 for v in caches): return None
        incoming += sum(caches)
    return incoming, outgoing


def measure_run(run, response_rows=(), contexts=None, billing=None, rates=None):
    """Missing/ambiguous call evidence remains unknown, never borrowed from a retry."""
    contexts = contexts or {}
    tracked = isinstance(run.get('callMetrics'), list)
    calls = run.get('callMetrics', [])
    nodes = {n['id']: n for n in run.get('nodes', [])}
    by_context = {}
    for row in response_rows:
        if isinstance(row, dict) and row.get('contextRef') and isinstance(row.get('receipt'), dict):
            by_context.setdefault(row['contextRef'], {})[fingerprint(row['receipt'])] = row['receipt']
    counts = Counter(c.get('contextRef') for c in calls)
    receipts, usages, repair, coordination = [], [], [], []
    repair_classified, coordination_classified = 0, 0
    for call in calls:
        node = nodes.get(call.get('nodeId'), {})
        context = call.get('contextRef')
        candidates = dict(by_context.get(context, {}))
        if context and context == node.get('contextRef') and isinstance(node.get('receipt'), dict):
            candidates[fingerprint(node['receipt'])] = node['receipt']
        receipt = next(iter(candidates.values())) if counts[context] == 1 and len(candidates) == 1 else {}
        receipts.append(receipt); usages.append(_usage(receipt))
        phase = call.get('phase', node.get('phase'))
        if isinstance(phase, str) and phase: coordination_classified += 1
        if phase in COORDINATION: coordination.append(call)
        ctx = contexts.get(context, {})
        if node.get('repairRound', 0) > 0 or ctx.get('formatRepair') or ctx.get('validationRepair'):
            repair.append(call)
            repair_classified += 1
        elif context in contexts:
            repair_classified += 1
    known = [u for u in usages if u is not None]
    complete = tracked and bool(calls) and len(known) == len(calls)
    actual = [r.get('returnedModel') or r.get('model') for r in receipts]
    actual = [m if isinstance(m, str) and m else None for m in actual]
    actual_by_alias = {}
    aliases_complete = all(isinstance(c.get('model'), str) and c['model'] for c in calls)
    for call, model in zip(calls, actual):
        alias = call.get('model')
        if isinstance(alias, str) and alias and model:
            actual_by_alias.setdefault(alias, set()).add(model)
    estimated = {'amount': None, 'currency': None, 'source': None, 'complete': False,
                 'basis': 'user_blended_input_output_rates'}
    if rates is not None:
        currency = _text(rates.get('currency'), 'rate currency')
        source = _text(rates.get('source'), 'rate source')
        prices = rates.get('models', {})
        amounts = []
        for usage, model in zip(usages, actual):
            rate = prices.get(model, {})
            values = [rate.get('inputPerMillion'), rate.get('outputPerMillion')]
            if usage is not None and all(_number(v) for v in values):
                amounts.append((usage[0]*values[0]+usage[1]*values[1])/1000000)
        estimated.update(currency=currency, source=source, ratesFingerprint=fingerprint(rates),
                         knownAmount=sum(amounts) if amounts else None,
                         complete=complete and len(amounts) == len(calls))
        if estimated['complete']: estimated['amount'] = sum(amounts)
    paid = {'amount': None, 'currency': None, 'source': None, 'kind': 'unknown'}
    if billing is not None:
        if (billing.get('runId') != run.get('id') or billing.get('kind') != 'paid_invoice'
                or not _number(billing.get('amount'))):
            raise ValueError('Billing must be paid_invoice evidence for this run')
        paid = {k: billing[k] for k in ('amount', 'currency', 'source', 'kind')}
        _text(paid['currency'], 'billing currency'); _text(paid['source'], 'billing source')
    total = lambda rows: (sum(c['elapsedMs'] for c in rows)
                          if tracked and all(_number(c.get('elapsedMs')) for c in rows) else None)
    return {'workflowWallMs': _duration(run.get('started'), run.get('ended')),
            'requestMs': total(calls),
            'coordinationMs': total(coordination) if coordination_classified == len(calls) else None,
            'repairMs': total(repair) if repair_classified == len(calls) else None,
            'knownRepairMs': total(repair),
            'repairClassificationComplete': tracked and repair_classified == len(calls),
            'coordinationClassificationComplete': tracked and coordination_classified == len(calls),
            'callCount': len(calls) if tracked else None, 'coordinationCallCount': len(coordination) if tracked else None,
            'repairCallCount': len(repair) if tracked else None,
            'errorCallCount': sum(c.get('outcome') == 'error' for c in calls) if tracked else None,
            'cancelledCallCount': sum(c.get('outcome') == 'cancelled' for c in calls) if tracked else None,
            'usage': {'inputTokens': sum(u[0] for u in known) if complete else None,
                      'outputTokens': sum(u[1] for u in known) if complete else None,
                      'knownInputTokens': sum(u[0] for u in known) if known else None,
                      'knownOutputTokens': sum(u[1] for u in known) if known else None,
                      'knownCalls': len(known), 'totalCalls': len(calls) if tracked else None, 'complete': complete},
            'actualModels': sorted({m for m in actual if m}),
            'actualModelsByAlias': {alias: sorted(models) for alias, models in sorted(actual_by_alias.items())},
            'modelEvidenceComplete': bool(calls) and all(actual) and aliases_complete,
            'requestedModels': sorted({c['model'] for c in calls if isinstance(c.get('model'), str)}),
            'actualPaid': paid, 'estimatedCost': estimated,
            'executionAttempts': {
                'count': len(run['executionAttempts']) if 'executionAttempts' in run else None,
                'statuses': dict(Counter(a.get('status', 'unknown') for a in run.get('executionAttempts', []))),
                'history': run.get('executionAttemptsHistory', 'unknown'),
                'notice': '成功率以计划槽位/run为分母；同一run续跑的失败尝试另列，端到端历时包含等待与续跑。'},
            'timingNotice': 'request/coordination/repair是调用耗时之和，分类可重叠；不是关键路径分解。'}


def _evidence_rows(directory, contexts=False):
    if directory is None: return ({}, []) if contexts else ([], [])
    rows, files = [], []
    for path in sorted(Path(directory).glob('*.json')):
        row = read_json(path); files.append(file_record(path))
        if contexts: rows.append((row.get('id'), row))
        else:
            rows.append(row)
            rows.extend(row.get('modelCalls', []))
    return (dict(rows) if contexts else rows), files


def import_run(manifest, pair_id, arm, session_path, run_id, binding, input_root,
               assessment_path=None, responses=None, contexts=None, billing=None, rates=None):
    validate_manifest(manifest)
    if arm not in manifest['arms'] or pair_id not in {p['id'] for p in manifest['pairs']}:
        raise ValueError('Unknown planned pair/arm')
    spec = manifest['arms'][arm]
    expected = {'runId': run_id, 'taskFingerprint': manifest['fingerprint'],
                'inputFingerprint': manifest['inputs']['fingerprint'],
                'acceptanceFingerprint': manifest['acceptance']['fingerprint'],
                'modelConfigFingerprint': spec['modelConfigFingerprint'], 'revision': spec['revision']}
    if any(binding.get(k) != value for k, value in expected.items()):
        raise ValueError('Import binding does not match the frozen manifest')
    if input_manifest(input_root) != manifest['inputs']:
        raise ValueError('Frozen inputs changed')
    for name in ('criteria', 'script'):
        if name in manifest['acceptance']:
            frozen = manifest['acceptance'][name]
            if file_record(frozen['path']) != frozen: raise ValueError('Frozen acceptance changed')
    session = read_json(session_path)
    selected = [r for r in session.get('runs', []) if r.get('id') == run_id]
    if len(selected) != 1: raise ValueError('Expected exactly one matching Studio run')
    run = selected[0]
    if run.get('mode') != 'live' or run.get('status') not in TERMINAL:
        raise ValueError('Import only terminal live Studio runs; demo/running runs are not real observations')
    if run.get('comparisonArm') or session.get('demoTemplate'):
        raise ValueError('Synthetic comparison/demo runs cannot become real-task observations')
    builds = [run.get('runtimeBuild', {}).get('fingerprint')]
    builds.extend(a.get('runtimeBuild', {}).get('fingerprint') for a in run.get('executionAttempts', []))
    if any(build != spec['revision'] for build in builds) or run.get('sourceMatchesRuntimeBuild') is False:
        raise ValueError('Actual runtime revision missing, mixed, or mismatched')
    assessment, proof = None, []
    if assessment_path is not None:
        assessment = read_json(assessment_path)
        if (assessment.get('runId') != run_id or assessment.get('acceptanceFingerprint') != expected['acceptanceFingerprint']
                or type(assessment.get('accepted')) is not bool or assessment.get('source') not in ('script', 'human')
                or _duration(run.get('ended'), assessment.get('completedAt')) is None
                or not isinstance(assessment.get('evidence'), list) or not assessment['evidence']):
            raise ValueError('Acceptance must identify this run, frozen criteria, completion time and evidence')
        proof = [file_record(Path(assessment_path).parent / name) for name in assessment['evidence']]
        proof.append(file_record(assessment_path))
    response_rows, response_files = _evidence_rows(responses)
    context_rows, context_files = _evidence_rows(contexts, contexts=True)
    metrics = measure_run(run, response_rows, context_rows, billing, rates)
    if set(metrics['requestedModels']) - set(spec['models']):
        raise ValueError('Observed model aliases are absent from the frozen configuration')
    metrics['endToEndMs'] = _duration(run.get('started'), assessment.get('completedAt') if assessment else run.get('ended'))
    if run['status'] == 'completed' and assessment is None: metrics['endToEndMs'] = None
    record = {'schema': SCHEMA + '/observation', 'manifestFingerprint': manifest['fingerprint'],
              'pairId': pair_id, 'arm': arm, 'runId': run_id, 'sessionId': session.get('id'),
              'projectId': session.get('projectId'), 'status': run['status'],
              'startedAt': run.get('started'), 'endedAt': run.get('ended'),
              'acceptanceCompletedAt': assessment.get('completedAt') if assessment else None,
              'accepted': assessment['accepted'] if assessment else None,
              'success': run['status'] == 'completed' and bool(assessment and assessment['accepted']),
              'binding': expected, 'bindingBasis': 'operator_attestation_plus_runtime_revision',
              'prospective': _duration(manifest['createdAt'], run.get('started')) is not None,
              'metrics': metrics, 'evidence': [file_record(session_path), *proof, *response_files, *context_files]}
    record['fingerprint'] = fingerprint(record)
    return record


def summarize(manifest, records):
    validate_manifest(manifest)
    slots, run_ids = {}, set()
    for record in records:
        if (record.get('manifestFingerprint') != manifest['fingerprint'] or
                record.get('fingerprint') != fingerprint({k:v for k,v in record.items() if k != 'fingerprint'})):
            raise ValueError('Observation changed or belongs to a different manifest')
        slot = (record['pairId'], record['arm'])
        if (record.get('schema') != SCHEMA + '/observation' or record['status'] not in TERMINAL
                or record['pairId'] not in {p['id'] for p in manifest['pairs']}
                or record['arm'] not in manifest['arms']):
            raise ValueError('Observation is outside the planned terminal slots')
        identity = (record.get('projectId'), record.get('sessionId'), record['runId'])
        if slot in slots or identity in run_ids: raise ValueError('Duplicate observation/run')
        slots[slot] = record; run_ids.add(identity)
    arms = {}
    for arm in ('baseline', 'current'):
        rows = [r for (_, a), r in slots.items() if a == arm]
        success = sum(r['success'] for r in rows)
        arms[arm] = {'planned': len(manifest['pairs']), 'observed': len(rows),
                     'pending': len(manifest['pairs'])-len(rows), 'successes': success,
                     'confirmedSuccessRate': success/len(rows) if rows else None,
                     'acceptanceUnknown': sum(r['accepted'] is None for r in rows),
                     'statuses': dict(Counter(r['status'] for r in rows)),
                     'actualPaid': summarize_cost(rows, 'actualPaid'),
                     'estimatedCost': summarize_cost(rows, 'estimatedCost')}
    pairs = []
    for plan in manifest['pairs']:
        pair = {'id': plan['id'], 'order': plan['order'], 'speedupRatio': None,
                'durationReductionPct': None, 'comparisonEligible': False, 'reason': 'incomplete_pair'}
        rows = {arm: slots.get((plan['id'], arm)) for arm in ('baseline', 'current')}
        if all(rows.values()):
            first, second = [rows[arm] for arm in plan['order']]
            first_end = first.get('acceptanceCompletedAt') or first['endedAt']
            order_ok = _duration(first_end, second['startedAt']) is not None
            pair['observedOrderMatches'] = order_ok
            a, b = [rows[arm] for arm in ('baseline', 'current')]
            same_config = manifest['arms']['baseline']['modelConfigFingerprint'] == manifest['arms']['current']['modelConfigFingerprint']
            model_ok = (a['metrics']['modelEvidenceComplete'] and b['metrics']['modelEvidenceComplete']
                        and a['metrics']['actualModelsByAlias'] == b['metrics']['actualModelsByAlias'])
            times = [r['metrics']['endToEndMs'] for r in (a,b)]
            reason = ('not_both_accepted' if not a['success'] or not b['success'] else
                      'retrospective_binding' if not a['prospective'] or not b['prospective'] else
                      'order_or_overlap' if not order_ok else 'different_model_configs' if not same_config else
                      'model_evidence_incomplete_or_different' if not model_ok else
                      'unknown_timing' if not all(_number(t) and t > 0 for t in times) else None)
            pair.update(reason=reason, comparisonEligible=reason is None)
            if reason is None:
                pair.update(speedupRatio=times[0]/times[1], durationReductionPct=(times[0]-times[1])*100/times[0])
        pairs.append(pair)
    reductions = [p['durationReductionPct'] for p in pairs if p['comparisonEligible']]
    return {'schema': SCHEMA + '/summary', 'manifestFingerprint': manifest['fingerprint'],
            'taskId': manifest['task']['id'], 'arms': arms, 'pairs': pairs,
            'eligiblePairs': len(reductions),
            'medianDurationReductionPct': statistics.median(reductions) if reductions else None,
            'observations': records, 'notice': NOTICE}


def summarize_cost(rows, field):
    """Never turn partial bills into full cost or silently convert currencies."""
    known = [r['metrics'][field] for r in rows if _number(r['metrics'][field].get('amount'))]
    totals = {}
    for cost in known:
        currency = cost['currency']
        totals[currency] = totals.get(currency, 0) + cost['amount']
    complete = bool(rows) and len(known) == len(rows) and len(totals) == 1
    return {'amount': next(iter(totals.values())) if complete else None,
            'currency': next(iter(totals)) if complete else None,
            'complete': complete, 'knownRuns': len(known), 'observedRuns': len(rows),
            'knownSubtotalsByCurrency': totals,
            'notice': '实付与估算分别汇总；不跨币种相加，不将缺失费用视作零。'}


def write_new(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    with path.open('x', encoding='utf8') as stream:
        stream.write(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='freeze a real task and randomized paired plan')
    for key in ('task-id', 'brief', 'inputs', 'criteria', 'models', 'baseline-revision', 'current-revision', 'out'):
        prepare.add_argument('--'+key, required=True)
    prepare.add_argument('--script'); prepare.add_argument('--repetitions', type=int, default=1)
    prepare.add_argument('--seed', type=int)
    ingest = commands.add_parser('import', help='import a terminal Studio run; never execute it')
    for key in ('manifest', 'arm', 'session', 'run-id', 'binding', 'inputs', 'records-dir'):
        ingest.add_argument('--'+key, required=True)
    ingest.add_argument('--pair', type=int, required=True)
    for key in ('assessment', 'responses', 'contexts', 'billing', 'rates'): ingest.add_argument('--'+key)
    report = commands.add_parser('summarize', help='include failures, cancellations, and pending slots')
    for key in ('manifest', 'records-dir', 'out'): report.add_argument('--'+key, required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        value = prepare_manifest(args.task_id, Path(args.brief).read_text(encoding='utf8'), args.inputs,
                                 args.criteria, read_json(args.models), args.baseline_revision,
                                 args.current_revision, args.repetitions, args.seed, args.script)
        write_new(args.out, value)
    elif args.command == 'import':
        value = import_run(read_json(args.manifest), args.pair, args.arm, args.session, args.run_id,
                           read_json(args.binding), args.inputs, args.assessment, args.responses, args.contexts,
                           read_json(args.billing) if args.billing else None,
                           read_json(args.rates) if args.rates else None)
        directory = Path(args.records_dir)
        existing = [read_json(p) for p in sorted(directory.glob('*.json'))]
        summarize(read_json(args.manifest), existing+[value])
        write_new(directory / ('pair-%03d-%s.json' % (args.pair, args.arm)), value)
    else:
        value = summarize(read_json(args.manifest), [read_json(p) for p in sorted(Path(args.records_dir).glob('*.json'))])
        write_new(args.out, value)
    print(json.dumps({'command': args.command, 'fingerprint': value.get('fingerprint', value.get('manifestFingerprint'))}))


if __name__ == '__main__':
    main()
