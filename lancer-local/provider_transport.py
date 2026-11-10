"""Interrupt an active provider socket without leaving a request reader running."""
import http.client
import socket
import threading
import time
import urllib.request

from live import Cancelled, NoRedirect


class ProviderTransport:
    def __init__(self, cancel, timeout):
        self.cancel = cancel
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.socket = None
        self.finished = threading.Event()
        self.interruption = None
        self.timings = {'headersMs': None, 'firstTextMs': None, 'totalMs': None}
        self.monitor = threading.Thread(target=self._watch, daemon=True)

    def __enter__(self):
        self.monitor.start()
        return self

    def __exit__(self, *exc):
        self.finished.set()
        self.monitor.join()
        self._close_socket()
        self.mark('totalMs')

    def mark(self, phase):
        if self.timings[phase] is None:
            self.timings[phase] = round((time.monotonic() - self.started) * 1000, 2)

    def attach(self, sock):
        self.socket = sock
        if self.interruption:
            self._close_socket()
            self.check()

    def check(self):
        if self.cancel.is_set() or self.interruption == 'cancelled':
            raise Cancelled('已取消模型请求，关闭本地连接；供应商是否停止计费由其服务决定。')
        if self.interruption == 'timeout' or time.monotonic() >= self.deadline:
            raise TimeoutError('模型请求超过等待时限，连接已关闭；保留未完成状态，未自动重试')

    def _watch(self):
        while not self.finished.wait(0.05):
            if self.cancel.is_set() or time.monotonic() >= self.deadline:
                self.interruption = 'cancelled' if self.cancel.is_set() else 'timeout'
                self._close_socket()
                return

    def _close_socket(self):
        sock = self.socket
        if sock is not None:
            try:
                # close() alone leaves HTTPResponse's buffered reader blocked.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class _TrackedConnection:
    def __init__(self, *args, control, **kwargs):
        self.control = control
        super().__init__(*args, **kwargs)
        original = self._create_connection
        def connect(address, timeout, source_address):
            sock = original(address, timeout, source_address)
            control.attach(sock)
            return sock
        self._create_connection = connect


class _HTTPConnection(_TrackedConnection, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_TrackedConnection, http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=hostname,
                                              do_handshake_on_connect=False)
        self.control.attach(self.sock)
        self.sock.do_handshake()


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(lambda *a, **kw: _HTTPConnection(*a, control=request.transport, **kw), request)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(lambda *a, **kw: _HTTPSConnection(*a, control=request.transport, **kw),
                            request, context=self._context)


def provider_opener():
    return urllib.request.build_opener(NoRedirect(), _HTTPHandler(), _HTTPSHandler())
