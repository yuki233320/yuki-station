"""Startup rejection must precede any recovery writes to active sessions."""
from contextlib import contextmanager
import concurrent.futures
import json
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import app
from app import Store
from service_lock import LOCK_FILENAME, ServiceDataLock
from studio import Studio


@contextmanager
def running_server(root):
    """A real isolated server, with no model request or production directory."""
    with tempfile.TemporaryFile(mode='w+b') as output:
        process = subprocess.Popen(
            [sys.executable, '-B', str(Path(__file__).with_name('app.py')),
             '--port', '0', '--data-dir', str(root)],
            stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                output.seek(0)
                logs = output.read().decode('utf8', errors='replace')
                match = re.search(r'yuki雪站: http://127\.0\.0\.1:(\d+)/studio', logs)
                if match:
                    yield process, int(match.group(1))
                    return
                if process.poll() is not None:
                    raise AssertionError(f'Server exited before readiness: {logs}')
                time.sleep(0.03)
            raise AssertionError(f'Server did not become ready: {logs}')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class AppStartupTests(unittest.TestCase):
    def test_desktop_health_binds_exact_data_directory_pid_and_current_source(self):
        from runtime_versions import source_snapshot
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / 'isolated-user-data'
            with running_server(data) as (process,port):
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/desktop/status',timeout=10) as response:
                    status = json.load(response)
                self.assertEqual(status['schema'],'yuki-desktop-service/v1')
                self.assertEqual(status['dataDir'],str(data.resolve()))
                self.assertEqual(status['pid'],process.pid)
                self.assertEqual(status['runtimeBuild']['fingerprint'],source_snapshot()['fingerprint'])
                self.assertTrue(status['sourceMatchesRuntimeBuild'])
                self.assertNotIn('files',status['runtimeBuild'])

    def test_occupied_port_exits_before_modifying_running_session(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            root = Path(directory).resolve()
            studio = Studio(Store(root, seed=False))
            pid = studio.create_project('启动回归', '保留运行会话', '')['id']
            session = studio.create_session(pid, '运行任务', '持续处理', 'general')
            session['status'] = 'running'
            session['runs'] = [{'id': 'running-run', 'status': 'running',
                                'nodes': [{'id': 'draft', 'status': 'running'}]}]
            studio.save(session)
            path = studio.path(pid, session['id'])
            original = studio.load(pid, session['id'])
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            result = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).with_name('app.py')),
                 '--port', str(port), '--data-dir', str(root)],
                capture_output=True, text=True, timeout=10)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Address already in use', result.stderr)
            self.assertEqual(studio.load(pid, session['id']), original)
            self.assertFalse((root / LOCK_FILENAME).exists())

    def test_different_port_cannot_recover_live_directory_then_restart_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            studio = Studio(Store(root, seed=False))
            pid = studio.create_project('目录锁回归', '保留运行会话', '')['id']
            session = studio.create_session(pid, '运行任务', '持续处理', 'general')
            path = studio.path(pid, session['id'])
            with running_server(root) as (first, first_port):
                # Represent an in-flight persisted task after the first server
                # has completed its legitimate startup recovery. No model runs.
                session['status'] = 'running'
                session['runs'] = [{'id': 'running-run', 'status': 'running',
                                    'nodes': [{'id': 'draft', 'status': 'running'}]}]
                studio.save(session)
                original_session = studio.load(pid, session['id'])
                original = {p.relative_to(root): p.read_bytes()
                            for p in root.rglob('*') if p.is_file()}
                with socket.socket() as available:
                    available.bind(('127.0.0.1', 0))
                    other_port = available.getsockname()[1]
                self.assertNotEqual(other_port, first_port)
                rejected = subprocess.run(
                    [sys.executable, '-B', str(Path(__file__).with_name('app.py')),
                     '--port', str(other_port), '--data-dir', str(root / '.')],
                    capture_output=True, text=True, timeout=10)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn('data directory in use', rejected.stderr)
                self.assertIsNone(first.poll())
                self.assertEqual({p.relative_to(root): p.read_bytes()
                                  for p in root.rglob('*') if p.is_file()}, original)
                # Failed startup must also close its already-bound port.
                with socket.socket() as available:
                    available.bind(('127.0.0.1', other_port))

            self.assertTrue((root / LOCK_FILENAME).is_file())
            with running_server(root):
                # Ownership is released on process exit. Only the legitimate
                # replacement now gets to mark interrupted work as interrupted.
                recovered = studio.load(pid, session['id'])
                self.assertEqual(recovered['status'], 'interrupted')
                self.assertEqual(recovered['runs'][0]['status'], 'interrupted')
                self.assertEqual(recovered['runs'][0]['nodes'][0]['status'], 'interrupted')
                self.assertNotEqual(recovered, original_session)

    def test_initialization_failure_releases_directory_lock_and_listener(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
            available.close()
            argv = ['app.py', '--port', str(port), '--data-dir', directory]
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(app, 'make_handler', side_effect=RuntimeError('init failed')):
                with self.assertRaisesRegex(RuntimeError, 'init failed'):
                    app.main()
            # In the same surviving process: this catches a leaked descriptor,
            # rather than relying on process exit to hide an initialization leak.
            with ServiceDataLock(directory):
                pass
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', port))

    def test_shutdown_holds_lock_until_accepted_worker_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            finish = threading.Event()
            cancelled = threading.Event()
            studio = SimpleNamespace(lock=threading.RLock(), cancels={'job': cancelled}, threads={})
            comparison = SimpleNamespace(cancel=threading.Event(), thread=None)
            controlled = SimpleNamespace(cancel=threading.Event(), thread=None)
            failures = []
            live_holder = []

            def pending_call():
                cancelled.wait(5)
                if not finish.wait(5):
                    raise AssertionError('Worker was not released by test')
                (root / 'finished-evidence.txt').write_text('accepted work settled')

            def worker():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(pending_call).result()

            def handler_factory(store, token, live):
                live_holder.append(live)
                thread = threading.Thread(target=worker, daemon=True)
                studio.threads['job'] = thread
                thread.start()

                class Handler(BaseHTTPRequestHandler):
                    @staticmethod
                    def shutdown_background():
                        app.shutdown_background_workers(studio, comparison, controlled, live)
                return Handler

            def run_main():
                try:
                    app.main()
                except BaseException as exc:
                    failures.append(exc)

            argv = ['app.py', '--port', '0', '--data-dir', str(root)]
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(app, 'make_handler', side_effect=handler_factory), \
                 mock.patch.object(app.ThreadingHTTPServer, 'serve_forever', side_effect=KeyboardInterrupt):
                main_thread = threading.Thread(target=run_main)
                main_thread.start()
                try:
                    self.assertTrue(cancelled.wait(5), failures)
                    self.assertTrue(main_thread.is_alive())
                    rejected = subprocess.run(
                        [sys.executable, '-B', str(Path(__file__).with_name('app.py')),
                         '--port', '0', '--data-dir', str(root)],
                        capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn('data directory in use', rejected.stderr)
                    self.assertFalse((root / 'finished-evidence.txt').exists())
                finally:
                    finish.set()
                    main_thread.join(5)
                self.assertFalse(main_thread.is_alive())
                self.assertEqual(failures, [])
                self.assertTrue(comparison.cancel.is_set())
                self.assertTrue(controlled.cancel.is_set())
                self.assertTrue(live_holder[0].cancel.is_set())
                self.assertEqual((root / 'finished-evidence.txt').read_text(), 'accepted work settled')
                with ServiceDataLock(root):
                    pass

    def test_shutdown_rechecks_child_created_after_parent_snapshot(self):
        spawn = threading.Event()
        finish = threading.Event()
        child_cancel = threading.Event()
        failures = []
        controlled = SimpleNamespace(cancel=threading.Event(), thread=None)
        comparison = SimpleNamespace(cancel=threading.Event(), thread=None)
        studio = SimpleNamespace(cancels={}, threads={})

        class SpawnAfterSnapshotLock:
            """Force the comparison to exit between snapshot and unlocked use."""
            def __init__(self):
                self.lock = threading.RLock()
                self.triggered = False

            def __enter__(self):
                self.lock.acquire()

            def __exit__(self, *exc):
                trigger = (threading.current_thread().name == 'shutdown-test'
                           and not self.triggered)
                if trigger:
                    self.triggered = True
                self.lock.release()
                if trigger:
                    spawn.set()
                    comparison.thread.join(3)

        studio.lock = SpawnAfterSnapshotLock()

        def parent():
            if not spawn.wait(3):
                failures.append('parent was not released')
                return
            with studio.lock:
                # Mirrors studio.start under Studio's lock after a comparison
                # has passed its cancellation check. A later _save failure can
                # terminate the parent before its usual join of this child.
                child = threading.Thread(target=lambda: finish.wait(5), daemon=True)
                studio.cancels['late'] = child_cancel
                studio.threads['late'] = child
                child.start()

        def shutdown():
            try:
                app.shutdown_background_workers(studio, comparison, controlled)
            except BaseException as exc:
                failures.append(exc)

        comparison.thread = threading.Thread(target=parent, daemon=True)
        comparison.thread.start()
        shutdown_thread = threading.Thread(target=shutdown, name='shutdown-test')
        shutdown_thread.start()
        try:
            self.assertTrue(child_cancel.wait(3), 'shutdown missed the late child')
            self.assertFalse(comparison.thread.is_alive())
            self.assertTrue(shutdown_thread.is_alive())
        finally:
            finish.set()
            spawn.set()
            comparison.thread.join(3)
            shutdown_thread.join(3)
            for child in studio.threads.values():
                child.join(3)
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(failures, [])


    def test_script_entry_returns_conflict_for_stale_host_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with running_server(Path(directory)) as (_, port):
                base = 'http://127.0.0.1:' + str(port)
                with urllib.request.urlopen(base + '/studio', timeout=5) as response:
                    token = re.search(r'name="local-token" content="([^"]+)"', response.read().decode())[1]
                def post(route, payload):
                    request = urllib.request.Request(base + '/api/studio/' + route,
                        data=json.dumps(payload).encode(),
                        headers={'Content-Type': 'application/json', 'X-Lancer-Token': token})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.load(response)
                project = post('project/create', {'title': 'Host version guard', 'goal': 'No calls'})
                selected = post('project/host-acceptance', {'project': project['id'], 'version': 1, 'checkIds': []})
                self.assertEqual(selected['version'], 2)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post('project/host-acceptance', {'project': project['id'], 'version': 1, 'checkIds': []})
                self.assertEqual(error.exception.code, 409)


if __name__ == '__main__':
    unittest.main()
