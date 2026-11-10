"""Execution boundary; acceptance and delivery remain owned by Studio workflows."""
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from live import Cancelled


@dataclass
class ExecutionRequest:
    session: dict
    run: dict
    node: dict
    context: dict
    messages: list
    shared: dict
    instruction: str
    cancel: Any
    on_delta: Callable
    previous_error: Any = None


@dataclass
class ExecutionResult:
    result: dict
    receipt: dict
    context: dict


class ExecutorRegistry:
    def __init__(self, adapters: Mapping[str, Callable]):
        self._adapters = dict(adapters)

    def select(self, request):
        if request.run['mode'] == 'demo':
            executor = 'demo'
        elif request.node['agent'] == 'native_codex':
            executor = 'native_codex'
        elif (request.node['phase'] == 'build'
              and 'workspace' in request.shared.get('module', {}).get('requiredCapabilities', [])):
            executor = 'workspace'
        elif (request.node['phase'] == 'build'
              and set(request.shared.get('module', {}).get('requiredCapabilities', []))
              & {'browser', 'plugins'}):
            executor = 'mcp'
        else:
            executor = 'api'
        if executor not in self._adapters:
            raise ValueError('执行器未接入：' + executor)
        return executor

    def execute(self, executor, request):
        if request.cancel.is_set():
            raise Cancelled()
        return self._adapters[executor](request)


def default_executors(studio):
    # Existing native/MCP bridges still depend on the legacy Studio host. Keep
    # that compatibility seam here; callers share one request/result contract.
    def api(request):
        return ExecutionResult(*studio.request_model_response(
            request.session, request.run, request.node, request.context,
            request.messages, request.cancel, request.on_delta))

    def native(request):
        from studio_native import execute_native
        result, receipt = execute_native(
            studio, request.session, request.run, request.node, request.context,
            request.messages, request.shared, request.cancel, request.on_delta)
        return ExecutionResult(result, receipt, request.context)

    def mcp(request):
        from studio_mcp import execute_mcp
        return ExecutionResult(*execute_mcp(
            studio, request.session, request.run, request.node, request.context,
            request.messages, request.shared, request.cancel, request.on_delta,
            request.previous_error))

    def workspace(request):
        from worker_execution import execute_worker
        return ExecutionResult(*execute_worker(
            studio, request.session, request.run, request.node, request.context,
            request.messages, request.shared, request.cancel, request.on_delta, request.previous_error))

    def demo(request):
        if request.run.get('workflowVersion') == 2:
            from studio_business_demo import respond
        else:
            from studio_demo import respond
        if request.cancel.wait(1.8):
            raise Cancelled()
        return ExecutionResult(
            respond(request.node, request.instruction, request.shared),
            {'mode': 'demo', 'source': '固定样例，未调用模型'}, request.context)

    return ExecutorRegistry({'api': api, 'native_codex': native, 'mcp': mcp, 'workspace': workspace, 'demo': demo})
