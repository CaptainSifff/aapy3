"""Real bounded fetch adapter against local files and a disposable HTTP server."""
import hashlib
import os
import sys
import tempfile
import threading
import unittest

from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from aap_semantics import FetchRequest
from fetch_adapter import LocalFetchBackend


PAYLOAD = b'\x00\xffbinary\r\n'


class Origin(object):
    source = None
    span = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/payload')
            self.end_headers()
        elif self.path == '/payload':
            self.send_response(200)
            self.send_header('Content-Length', str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        elif self.path == '/partial':
            self.send_response(200)
            self.send_header('Content-Length', str(len(PAYLOAD) + 10))
            self.end_headers()
            self.wfile.write(PAYLOAD[:3])
            self.wfile.flush()
            self.close_connection = True
        elif self.path == '/fake404':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<html><title>404 Not Found</title></html>')
        else:
            self.send_response(404)
            self.end_headers()


class FetchAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = []
        self.backend = LocalFetchBackend(
            lambda kind, **fields: self.events.append((kind, fields)), timeout=3)

    def request(self, candidates):
        return FetchRequest(Origin(), os.path.join(self.temp.name, 'out', 'archive'),
                            candidates, self.temp.name, 'archive',
                            ' '.join(candidates))

    def test_local_file_relative_mapping_preserves_percent_bytes(self):
        source_dir = os.path.join(self.temp.name, 'files')
        os.mkdir(source_dir)
        with open(os.path.join(source_dir, 'a%20b'), 'wb') as stream:
            stream.write(PAYLOAD)
        request = self.request(('file://files/a%20b',))
        result = self.backend.fetch(request)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(result.selected, 'file://files/a%20b')
        with open(request.destination, 'rb') as stream:
            self.assertEqual(stream.read(), PAYLOAD)
        self.assertEqual(result.attempts[0].sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_missing_file_then_http_success_and_attempt_order(self):
        candidates = ('file://files/missing', self.base + '/payload',
                      self.base + '/unused')
        request = self.request(candidates)
        result = self.backend.fetch(request)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual([a.candidate for a in result.attempts], list(candidates[:2]))
        self.assertEqual([a.status for a in result.attempts], ['FAILED', 'COMPLETED'])
        self.assertEqual([fields['candidate'] for kind, fields in self.events
                          if kind == 'fetch_attempt'], list(candidates[:2]))
        with open(request.destination, 'rb') as stream:
            self.assertEqual(stream.read(), PAYLOAD)

    def test_http_404_then_redirect_success(self):
        request = self.request((self.base + '/missing', self.base + '/redirect'))
        result = self.backend.fetch(request)
        self.assertEqual([a.status for a in result.attempts], ['FAILED', 'COMPLETED'])
        self.assertIn('/payload', result.attempts[1].detail)
        self.assertEqual(result.attempts[1].count, len(PAYLOAD))

    def test_http_200_error_page_uses_historical_fallback(self):
        request = self.request((self.base + '/fake404', self.base + '/payload'))
        result = self.backend.fetch(request)
        self.assertEqual([a.status for a in result.attempts], ['FAILED', 'COMPLETED'])

    def test_partial_remote_failure_is_removed_before_fallback(self):
        request = self.request((self.base + '/partial', self.base + '/payload'))
        result = self.backend.fetch(request)
        self.assertEqual([a.status for a in result.attempts], ['FAILED', 'COMPLETED'])
        with open(request.destination, 'rb') as stream:
            self.assertEqual(stream.read(), PAYLOAD)

    def test_all_remote_candidates_fail_without_destination(self):
        request = self.request((self.base + '/partial', self.base + '/missing'))
        result = self.backend.fetch(request)
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(len(result.attempts), 2)
        self.assertTrue(os.path.isdir(os.path.dirname(request.destination)))
        self.assertFalse(os.path.exists(request.destination))

    def test_local_copy_error_ends_without_fallback(self):
        os.mkdir(os.path.join(self.temp.name, 'directory-source'))
        request = self.request(('file://directory-source', self.base + '/payload'))
        result = self.backend.fetch(request)
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual([a.status for a in result.attempts], ['FATAL'])
        self.assertIn('local copy failed', result.detail)


if __name__ == '__main__':
    unittest.main()
