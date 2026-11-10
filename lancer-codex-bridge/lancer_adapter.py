"""Adapter for the existing Python Lancer workbench; no third-party packages."""
import json
import os
import queue
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path


class NativeAdapterError(RuntimeError):
    def __init__(self, message, status='unknown'):
        super().__init__(message)
        self.status = status


def run_native_codex(task, workspace_root, state_dir, on_event=None,
                     node_bin=None, allow_workspace_write=False,
                     codex_bin=None, timeout_ms=600000, cancel=None):
    """Return a native receipt. on_event receives tool/progress/approval events.

    Configure workspace_root in the trusted host, not from an LLM tool argument.
    This CLI adapter declines pending native approvals and returns the request
    to the caller. The JavaScript API supports a host-owned approval UI callback.
    """
    node_bin = node_bin or shutil.which('node')
    if not node_bin:
        raise RuntimeError('Node.js 20+ is required')
    script = Path(__file__).with_name('cli.mjs')
    args = [node_bin, str(script), 'run', '--root', str(workspace_root),
            '--state', str(state_dir), '--timeout-ms', str(timeout_ms)]
    if codex_bin:
        args.extend(['--codex', str(codex_bin)])
    if allow_workspace_write:
        args.append('--allow-workspace-write')
    receipt, diagnostic = None, ''
    # stdin/stdout is the private local transport: no browser-held secret,
    # public HTTP listener, or copied Codex credential is needed.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding='utf8',
                             errors='replace', bufsize=1, start_new_session=True, env=env)
    events = queue.Queue(maxsize=128)

    def drain(stream, kind):
        # Both pipes must be drained concurrently, including when a tool is noisy.
        try:
            while True:
                line = stream.readline(2_000_001)
                if not line:
                    break
                events.put((kind, line))
        finally:
            events.put((kind + '_eof', None))

    def stop():
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        # The parent may have exited while a native child still owns a pipe.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    for stream, kind in ((child.stdout, 'stdout'), (child.stderr, 'stderr')):
        threading.Thread(target=drain, args=(stream, kind), daemon=True).start()
    def feed():
        try:
            child.stdin.write(json.dumps(task, ensure_ascii=False) + '\n')
            child.stdin.close()
        except (OSError, ValueError) as exc:
            events.put(('stdin_error', str(exc)))
    threading.Thread(target=feed, daemon=True).start()
    started, ended = time.monotonic(), set()
    try:
        while len(ended) < 2:
            if cancel is not None and cancel.is_set():
                raise NativeAdapterError('原生 Codex 已取消；本次执行结果未知，不会自动重试。', 'unknown')
            if time.monotonic() - started > timeout_ms / 1000 + 3:
                raise NativeAdapterError('原生 Codex 超时；本次执行结果未知，不会自动重试。', 'unknown')
            try:
                kind, line = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if kind.endswith('_eof'):
                ended.add(kind)
            elif kind == 'stdin_error':
                raise NativeAdapterError('原生桥接未接收完整任务：' + line)
            elif kind == 'stderr':
                diagnostic = (diagnostic + line)[-8000:]
            else:
                if len(line) > 2_000_000:
                    raise NativeAdapterError('原生桥接事件超过允许长度。')
                try:
                    event = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise NativeAdapterError('原生桥接返回了无效的 JSONL 事件。') from exc
                if event.get('type') == 'receipt':
                    receipt = event['receipt']
                elif on_event:
                    on_event(event)
        code = child.wait(timeout=2)
    finally:
        stop()
        for stream in (child.stdin, child.stdout, child.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    if receipt is None:
        raise NativeAdapterError('Native Codex returned no terminal receipt: ' + diagnostic)
    if code and receipt.get('status') == 'completed':
        raise NativeAdapterError('Native Codex exited unexpectedly after a completed receipt.')
    return receipt
