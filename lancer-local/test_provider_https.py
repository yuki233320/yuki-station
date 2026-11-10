"""Actual local TLS checks; trust a temporary CA without weakening verification."""
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The standalone compatibility candidate is tested before promotion to the app.
if not (Path(__file__).parent / 'live.py').is_file():
    sys.path.insert(1, str(Path(__file__).parent.parent / 'source/lancer-local'))
from live import NoRedirect
from provider_transport import ProviderTransport, _HTTPSHandler


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.received += 1
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'OK')


class ProviderHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which('openssl')
        if not openssl:
            raise RuntimeError('openssl required for actual TLS regression; do not skip verification')
        cls.directory = tempfile.TemporaryDirectory(prefix='yuki-local-tls-')
        cls.root = Path(cls.directory.name)
        ca_config = cls.root / 'ca.cnf'
        ca_config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n'
                             '[dn]\nCN=Yuki temporary test CA\n[ca]\nbasicConstraints=critical,CA:TRUE\n'
                             'keyUsage=critical,keyCertSign,cRLSign\n')
        leaf_config = cls.root / 'leaf.cnf'
        leaf_config.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                               'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n')
        commands = [
            ['req', '-new', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-config', str(ca_config),
             '-keyout', 'ca.key', '-out', 'ca.pem'],
            ['req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=localhost',
             '-keyout', 'leaf.key', '-out', 'leaf.csr'],
            ['x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '1',
             '-days', '1', '-extfile', str(leaf_config), '-out', 'leaf.pem'],
        ]
        try:
            for command in commands:
                subprocess.run([openssl] + command, cwd=cls.root, check=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=15)
        except Exception:
            cls.directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.daemon_threads = True
        self.server.received = 0
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(self.root / 'leaf.pem'), str(self.root / 'leaf.key'))
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(3)
        self.assertFalse(self.server_thread.is_alive())

    def get(self, hostname, context):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), _HTTPSHandler(context=context))
        request = urllib.request.Request('https://' + hostname + ':' + str(self.server.server_port) + '/test')
        with ProviderTransport(threading.Event(), 5) as transport:
            request.transport = transport
            with opener.open(request, timeout=5) as response:
                return response.read()

    def trusted_context(self):
        context = ssl.create_default_context(cafile=str(self.root / 'ca.pem'))
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        return context

    def test_trusted_ca_and_matching_hostname_succeed(self):
        context = self.trusted_context()
        self.assertEqual(self.get('localhost', context), b'OK')
        self.assertEqual(self.server.received, 1)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_trusted_ca_but_wrong_hostname_is_rejected(self):
        with self.assertRaises(urllib.error.URLError) as caught:
            self.get('127.0.0.1', self.trusted_context())
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
        self.assertEqual(caught.exception.reason.verify_code, 64)  # IP address mismatch, not a network error.
        self.assertEqual(self.server.received, 0)

    def test_untrusted_ca_is_rejected_with_default_verification(self):
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with self.assertRaises(urllib.error.URLError) as caught:
            self.get('localhost', context)
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
        self.assertNotIn(caught.exception.reason.verify_code, (62, 64))
        self.assertEqual(self.server.received, 0)


if __name__ == '__main__':
    unittest.main()
