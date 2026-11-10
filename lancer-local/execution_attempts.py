"""Execution history inside a run; never a scheduler or authority to replay work.

The run remains the authority for business status, nodes and delivery evidence.
An attempt records one explicitly requested start/resume of that same run.
Unknown external outcomes remain in the native/MCP receipts, not inferred here.
"""
from copy import deepcopy


TERMINAL_STATUSES = frozenset({'completed', 'failed', 'stopped', 'conflict', 'interrupted'})
ATTEMPT_STATUSES = TERMINAL_STATUSES | {'running'}


def _attempts(run):
    attempts = run.get('executionAttempts', [])
    if not isinstance(attempts, list) or any(
        not isinstance(item, dict) or item.get('status') not in ATTEMPT_STATUSES
        for item in attempts
    ):
        raise ValueError('执行履历格式无效；保留原记录，未重新执行')
    return attempts


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + '不能为空')


def begin_attempt(run, reason, now, attempt_id, runtime_build):
    """Append a running attempt after caller validation, before starting work.

    The caller must persist the run and this attempt together before launching
    its executor. Existing legacy runs gain no invented historical attempts.
    """
    if reason not in ('start', 'resume', 'evidence_review'):
        raise ValueError('执行原因必须是 start、resume 或 evidence_review')
    _text(now, '执行开始时间')
    _text(attempt_id, '执行编号')
    if not isinstance(runtime_build, dict):
        raise ValueError('运行版本引用无效')
    attempts = _attempts(run)
    if any(item['status'] == 'running' for item in attempts):
        raise ValueError('本轮仍有未结束的执行，不能重复启动')
    if any(item.get('id') == attempt_id for item in attempts):
        raise ValueError('执行编号已存在')
    attempt = {
        'id': attempt_id, 'number': len(attempts) + 1, 'reason': reason,
        'status': 'running', 'startedAt': now,
        'runtimeBuild': deepcopy({key: value for key, value in runtime_build.items() if key != 'files'}),
    }
    run.setdefault('executionAttemptsHistory', 'legacy_unknown' if not attempts and reason != 'start' else 'complete')
    run['executionAttempts'] = attempts
    attempts.append(attempt)
    # This belongs to the previous execution. Nodes, receipts, commitIntent and
    # the previous attempt's end time remain untouched.
    run.pop('ended', None)
    return attempt


def finish_attempt(run, now):
    """Settle the sole running attempt from the run's persisted business result.

    Repeated calls are no-ops. A still-running run cannot be made successful by
    this helper; its executor must first determine the actual outcome.
    """
    _text(now, '执行结束时间')
    attempts = _attempts(run)
    active = [item for item in attempts if item['status'] == 'running']
    if len(active) > 1:
        raise ValueError('本轮有多个未结束的执行，需要核对履历')
    if not active or run.get('status') not in TERMINAL_STATUSES:
        return None
    attempt = active[0]
    attempt.update(status=run['status'], endedAt=run.get('ended') or now)
    return attempt


def interrupt_attempts(run, now, reason):
    """Close unfinished attempts on host recovery, without dispatching anything.

    A terminal run may have been saved just before a crash cut off attempt
    finalization. Preserve that known result; otherwise record interruption.
    The caller separately reconciles the session/run/node statuses and saves.
    """
    _text(now, '恢复时间')
    _text(reason, '中断原因')
    changed = []
    for attempt in _attempts(run):
        if attempt['status'] != 'running':
            continue
        status = run.get('status')
        if status not in TERMINAL_STATUSES:
            status = 'interrupted'
        attempt.update(status=status, endedAt=run.get('ended') or now)
        if status == 'interrupted':
            attempt['endReason'] = reason
        changed.append(attempt)
    return changed
