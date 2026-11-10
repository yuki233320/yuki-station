"""Bounded, completion-driven execution for an already planned module graph.

Workers own their durable evidence. This scheduler only admits ready modules,
passes private upstream snapshots, and drains admitted work before returning or
raising. It never retries a worker or cancels a sibling on another's failure.
"""
import concurrent.futures
import copy
import threading

from live import Cancelled


def execute_dag(modules, execute, cancel, max_workers=3):
    """Call execute(module, upstream_snapshot), returning outputs keyed by ID.

    A worker may reuse persisted results; that recovery policy stays with the
    caller. Admission follows priority, descending complexity, then module ID.
    A failure closes admission immediately, while previously admitted workers
    finish and save their evidence before the exception reaches the caller.
    """
    if type(max_workers) is not int or max_workers < 1:
        raise ValueError('并发执行数量须为正整数')
    pending = {module['id']: module for module in modules}
    if len(pending) != len(modules):
        raise ValueError('模块编号重复')
    reachable = set()
    while len(reachable) < len(pending):
        ready = {mid for mid, module in pending.items()
                 if mid not in reachable and set(module['dependsOn']) <= reachable}
        if not ready:
            raise ValueError('模块包含循环或未知依赖')
        reachable.update(ready)

    completed, running, failures = {}, {}, []
    admission = threading.Lock()

    def invoke(module, upstream):
        try:
            return execute(module, upstream)
        except BaseException as exc:
            # Serialize failure publication with admission: a new worker cannot
            # slip in after this failure has closed the gate.
            with admission:
                if not failures:
                    failures.append(exc)
            raise

    # shutdown(wait=True), including on error/cancellation, is deliberate. The
    # caller must not mark the run terminal while workers can still save data.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending or running:
            with admission:
                if failures:
                    raise failures[0]
                if cancel.is_set():
                    raise Cancelled()
            ready = sorted((module for module in pending.values()
                            if set(module['dependsOn']) <= completed.keys()),
                           key=lambda module: (module['priority'], -module['complexity'], module['id']))
            for module in ready:
                if len(running) >= max_workers:
                    break
                upstream = copy.deepcopy({mid: completed[mid] for mid in module['dependsOn']})
                with admission:
                    if failures:
                        raise failures[0]
                    if cancel.is_set():
                        raise Cancelled()
                    future = pool.submit(invoke, module, upstream)
                    running[future] = module['id']
                    del pending[module['id']]
            if not running:
                if pending:
                    raise ValueError('模块依赖无法继续')
                break
            finished, _ = concurrent.futures.wait(
                running, timeout=0.05, return_when=concurrent.futures.FIRST_COMPLETED)
            # Harvest every completed future before refilling slots, so a
            # simultaneous success cannot conceal a sibling's failure.
            finished.update(future for future in running if future.done())
            for future in finished:
                mid = running.pop(future)
                completed[mid] = future.result()
        if cancel.is_set():
            raise Cancelled()
    return completed
