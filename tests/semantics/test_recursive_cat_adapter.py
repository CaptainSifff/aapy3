"""Real local adapter wiring for the existing bounded :cat runtime."""
import binascii
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from aap_frontend import Source, parse
from aap_semantics import (CatRuntime, Evaluator, OutputPolicy, Scope,
                           SemanticError, lower)
from aap_cli import Files
from output_adapter import LocalByteWriter


def program(text):
    return lower(parse(Source('/recipe/main.aap', text)))


class Request(object):
    def __init__(self, path, cwd, mode):
        self.path, self.cwd, self.mode = path, cwd, mode


class RecursiveCatAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cwd = self.temporary.name
        os.mkdir(os.path.join(self.cwd, 'work'))
        os.mkdir(os.path.join(self.cwd, 'files'))
        self.events = []
        self.writer = LocalByteWriter(self.record)
        self.reader = Files(self.record)
        self.runtime = CatRuntime(OutputPolicy('latin-1'), self.reader,
                                  self.writer)

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, kind, **fields):
        self.events.append((kind, fields))

    def execute(self, text, variables=None):
        scope = Scope.top_level()
        scope.local.update(variables or {})
        return Evaluator(scope, cwd=self.cwd, cat_runtime=self.runtime).run(
            program(text))

    def put_bytes(self, relative, data):
        session = self.writer.open_bytes(Request(relative, self.cwd, 'w'))
        session.write(data)
        session.close()

    def test_append_reads_live_writer_file_after_opening_destination(self):
        self.put_bytes('files/source', b'\x00source\r\n\xff')
        self.put_bytes('work/post_i', b'header\n')
        self.events = []
        result = self.execute(':cat >>$script $file\n',
                              {'script': 'work/post_i', 'file': 'files/source'})
        output = os.path.join(self.cwd, 'work', 'post_i')
        with open(output, 'rb') as stream:
            self.assertEqual(stream.read(), b'header\n\x00source\r\n\xff')
        record = result.cats[0]
        self.assertEqual((record.status, record.reads),
                         ('COMPLETED', [(os.path.join(self.cwd, 'files', 'source'),
                                         len(b'\x00source\r\n\xff'))]))
        names = [item[0] for item in self.events]
        self.assertLess(names.index('byte_output_open'), names.index('artifact_read'))
        self.assertLess(names.index('artifact_read'), names.index('byte_output_write'))
        self.assertLess(names.index('byte_output_write'), names.index('byte_output_close'))
        opening = [fields for kind, fields in self.events
                   if kind == 'byte_output_open' and fields['status'] == 'STARTED'][0]
        closing = [fields for kind, fields in self.events
                   if kind == 'byte_output_close' and fields['status'] == 'COMPLETED'][0]
        self.assertEqual(opening['before']['data_hex'],
                         binascii.hexlify(b'header\n').decode('ascii'))
        self.assertEqual(closing['after']['data_hex'],
                         binascii.hexlify(b'header\n\x00source\r\n\xff').decode('ascii'))

    def test_missing_and_non_regular_sources_fail_after_destination_open(self):
        for name, kind in (('files/missing', 'missing'), ('files/directory', 'non-regular')):
            if kind == 'non-regular':
                os.mkdir(os.path.join(self.cwd, name))
            self.events = []
            with self.assertRaises(SemanticError):
                self.execute(':cat >>work/post_i ' + name + '\n')
            record = [fields for event, fields in self.events
                      if event == 'artifact_read' and fields['status'] == 'FAILED'][-1]
            self.assertEqual(record['path_kind'], kind)
            self.assertLess([event for event, unused in self.events].index('byte_output_open'),
                            [event for event, unused in self.events].index('artifact_read'))


if __name__ == '__main__':
    unittest.main()
