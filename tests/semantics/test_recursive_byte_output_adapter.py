"""Filesystem-level tests for the disposable integration byte writer."""
import os
import binascii
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from output_adapter import LocalByteWriter


class Request(object):
    def __init__(self, path, cwd, mode):
        self.path, self.cwd, self.mode = path, cwd, mode


class LocalByteWriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        os.mkdir(os.path.join(self.root, 'work'))
        self.events = []
        self.writer = LocalByteWriter(self.record)

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, kind, **fields):
        self.events.append((kind, fields))

    def test_write_truncates_and_preserves_ordered_bytes(self):
        path = os.path.join(self.root, 'work', 'post_i')
        with open(path, 'wb') as stream:
            stream.write(b'old contents')
        session = self.writer.open_bytes(Request('work/post_i', self.root, 'w'))
        self.assertTrue(callable(session.write))
        self.assertTrue(callable(session.close))
        session.write(b'head\r\n')
        session.write(b'caf\xe9\x00tail')
        session.close()
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), b'head\r\ncaf\xe9\x00tail')
        self.assertEqual([item[0] for item in self.events],
                         ['byte_output_open', 'byte_output_open',
                          'byte_output_write', 'byte_output_write',
                          'byte_output_close'])
        writes = [item[1] for item in self.events
                  if item[0] == 'byte_output_write']
        self.assertEqual([item['operation'] for item in writes], [1, 2])
        self.assertEqual([item['data_hex'] for item in writes],
                         [binascii.hexlify(b'head\r\n').decode('ascii'),
                          binascii.hexlify(b'caf\xe9\x00tail').decode('ascii')])
        self.assertEqual(self.events[-1][1]['status'], 'COMPLETED')

    def test_append_preserves_existing_bytes_and_uses_absolute_path(self):
        path = os.path.join(self.root, 'work', 'post_i')
        with open(path, 'wb') as stream:
            stream.write(b'header')
        session = self.writer.open_bytes(Request(path, self.root, 'a'))
        session.write(b'\n# MAIN\n')
        session.close()
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), b'header\n# MAIN\n')
        self.assertEqual(self.events[1][1]['path'], path)

    def test_missing_parent_and_open_failure_are_propagated(self):
        with self.assertRaises(IOError):
            self.writer.open_bytes(Request('missing/script', self.root, 'w'))
        self.assertEqual(self.events[-1][0], 'byte_output_open')
        self.assertEqual(self.events[-1][1]['status'], 'FAILED')

    def test_write_failure_and_close_failure_are_propagated(self):
        path = os.path.join(self.root, 'work', 'post_i')
        session = self.writer.open_bytes(Request(path, self.root, 'w'))
        session._stream.close()
        with self.assertRaises(ValueError):
            session.write(b'cannot write')
        self.assertEqual(self.events[-1][0], 'byte_output_write')
        self.assertEqual(self.events[-1][1]['status'], 'FAILED')

        class CloseFailure(object):
            def close(self):
                raise IOError('injected close failure')

        from output_adapter import LocalByteSession
        broken = LocalByteSession(CloseFailure(), path, 'w', self.record)
        with self.assertRaises(IOError):
            broken.close()
        self.assertEqual(self.events[-1][0], 'byte_output_close')
        self.assertEqual(self.events[-1][1]['status'], 'FAILED')

    def test_relative_path_requires_explicit_absolute_logical_cwd(self):
        with self.assertRaises(ValueError):
            self.writer.open_bytes(Request('work/post_i', None, 'w'))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'work', 'post_i')))


if __name__ == '__main__':
    unittest.main()
