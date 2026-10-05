"""Commands.aap_cat/_get_redir; append scenario from rectest/test003.py.

Only injected bytes and controlled processes; no legacy driver is executed.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, CatRuntime, PrintRuntime,
    OutputPolicy, MemoryTextWriter, TextWriter, SemanticError, Unsupported,
    MemoryPathObserver, PathObservation, MemoryPortDirectories)
from aap_semantics.checksum import MemoryArtifacts, ArtifactBackend
from aap_semantics.process import ProcessPolicy
from test_cd import Process, Loader


def program(text):
    return lower(parse(Source('/recipe/main.aap', text)))


class CatTests(unittest.TestCase):
    def engine(self, files=None, variables=None, reader=None, writer=None, **kwargs):
        writer = writer if writer is not None else MemoryTextWriter(['/recipe', '/absolute'], files)
        reader = reader if reader is not None else MemoryArtifacts(writer.files, shared=True)
        scope = Scope.top_level()
        scope.local.update(variables or {})
        policy = OutputPolicy('latin-1', False)
        runtime = CatRuntime(policy, reader, writer)
        engine = Evaluator(scope, cwd='/recipe', cat_runtime=runtime,
                           output_runtime=PrintRuntime(policy, writer=writer), **kwargs)
        return engine, reader, writer

    def test_reached_append_variables_and_record(self):
        engine, reader, writer = self.engine({'/recipe/in': b'abc', '/recipe/out': b'old'},
                                             {'script': 'out', 'file': 'in'})
        result = engine.run(program(':cat >>$script $file\nAFTER = yes\n'))
        record = result.cats[0]
        request = record.request
        self.assertEqual(request.raw, '>>$script $file')
        self.assertEqual((request.raw_destination, request.raw_sources), ('$script', '$file'))
        self.assertEqual((request.expanded_destination, request.expanded_sources), ('out', 'in'))
        self.assertEqual(request.paths, ('/recipe/in',))
        self.assertEqual(request.path, '/recipe/out')
        self.assertEqual(request.destination, 'append')
        self.assertEqual((record.bytes_read, record.bytes_written), (3, 3))
        self.assertEqual(record.reads, [('/recipe/in', 3)])
        self.assertEqual(record.status, 'COMPLETED')
        self.assertTrue(record.closed)
        self.assertEqual(record.span.source_id, '/recipe/main.aap')
        self.assertEqual(record.span.start.line, 1)
        self.assertEqual(writer.files['/recipe/out'], b'oldabc')
        self.assertEqual(reader.observations, [('read', '/recipe/in')])
        self.assertEqual(result.scope.local['AFTER'], 'yes')
        self.assertEqual(result.processes, [])

    def test_absolute_and_quoted_paths(self):
        engine, reader, writer = self.engine({'/absolute/in file': b'x'})
        result = engine.run(program(':cat >>"/absolute/out file" "/absolute/in file"\n'))
        self.assertEqual(writer.files['/absolute/out file'], b'x')
        self.assertEqual(result.cats[0].request.source_items, ('/absolute/in file',))

    def test_bytes_newlines_and_empty_files(self):
        for data in (b'', b'abc', b'abc\n', b'\x00\xff\xfe\r\n\r\x80'):
            engine, reader, writer = self.engine({'/recipe/in': data})
            engine.run(program(':cat >>out in\n'))
            self.assertEqual(writer.files['/recipe/out'], data)

    def test_multiple_sources_and_duplicates_no_separator(self):
        engine, reader, writer = self.engine({'/recipe/a': b'abc', '/recipe/b': b'def'})
        result = engine.run(program(':cat >>out a b a\n'))
        self.assertEqual(writer.files['/recipe/out'], b'abcdefabc')
        self.assertEqual(result.cats[0].reads, [('/recipe/a', 3), ('/recipe/b', 3), ('/recipe/a', 3)])

    def test_overwrite_and_trailing_redirection(self):
        engine, reader, writer = self.engine({'/recipe/a': b'new', '/recipe/out': b'old'})
        engine.run(program(':cat a >!out\n'))
        self.assertEqual(writer.files['/recipe/out'], b'new')

    def test_self_append_reads_snapshot_and_duplicate_reads_live(self):
        engine, reader, writer = self.engine({'/recipe/a': b'x'})
        engine.run(program(':cat >>a a a\n'))
        self.assertEqual(writer.files['/recipe/a'], b'xxxx')

    def test_self_overwrite_truncates_before_read(self):
        engine, reader, writer = self.engine({'/recipe/a': b'x'})
        engine.run(program(':cat >!a a\n'))
        self.assertEqual(writer.files['/recipe/a'], b'')

    def test_empty_source_list_and_malformed_redirect(self):
        for command in (':cat >>out', ':cat >>', ':cat >>out >!other in', ':cat >>out "bad'):
            engine, reader, writer = self.engine()
            with self.assertRaises(SemanticError):
                engine.run(program(command + '\n'))
            self.assertEqual(writer.files, {})
            self.assertEqual(reader.observations, [])

    def test_unsupported_features_gate_before_effects(self):
        for command in (':cat >out in', ':cat in', ':cat >>out -', ':cat >>out *.in',
                        ':cat >>out ~/in', ':cat >>out http://remote/file', ':cat >>out in {x}',
                        ':cat >>out in | :print', ':cat >>http://remote/out in'):
            engine, reader, writer = self.engine()
            with self.assertRaises(Unsupported):
                engine.run(program(command + '\n'))
            self.assertEqual(writer.requests, [])
            self.assertEqual(reader.observations, [])

    def test_nonrecursive_values(self):
        engine, reader, writer = self.engine({'/recipe/$NAME': b'yes'}, {'X': '$NAME'})
        engine.run(program(':cat >>out $X\n'))
        self.assertEqual(writer.files['/recipe/out'], b'yes')

    def test_destination_expansion_precedes_source_expansion(self):
        engine, reader, writer = self.engine()
        with self.assertRaises(SemanticError) as error:
            engine.run(program(':cat >>$DEST $SOURCE\n'))
        self.assertIn('DEST', str(error.exception))
        self.assertEqual(writer.requests, [])

    def test_path_encoding_does_not_encode_content(self):
        engine, reader, writer = self.engine({'/recipe/caf\u00e9': b'\xff\x00'})
        result = engine.run(program(':cat >>out caf\u00e9\n'))
        self.assertEqual(result.cats[0].request.source_path_bytes, (b'/recipe/caf\xe9',))
        self.assertEqual(writer.files['/recipe/out'], b'\xff\x00')

    def test_invalid_path_encoding_before_open(self):
        engine, reader, writer = self.engine()
        with self.assertRaises(SemanticError):
            engine.run(program(':cat >>out \u20ac\n'))
        self.assertEqual(writer.requests, [])

    def test_nul_source_path_before_open(self):
        engine, reader, writer = self.engine(variables={'X': 'a\x00b'})
        with self.assertRaises(SemanticError):
            engine.run(program(':cat >>out $X\n'))
        self.assertEqual(writer.requests, [])

    def body(self, runtime, text='BEFORE = yes\n:cat >>out in\nAFTER = no'):
        from test_cd import CdTests
        driver, dirs, process, saved = CdTests().make(text)
        driver.capabilities = driver.capabilities.replace(cat_runtime=runtime)
        return driver, process, saved

    def test_missing_source_fails_after_destination_open(self):
        writer = MemoryTextWriter(['/recipe'], {'/recipe/out': b'old'})
        driver, process, saved = self.body(CatRuntime(OutputPolicy('latin-1'), MemoryArtifacts(), writer),
                                         ':cat >!out absent\nAFTER = no')
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(writer.files['/recipe/out'], b'')
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        record = result.bodies[0].evaluation.cats[0]
        self.assertEqual((record.status, record.phase), ('FAILED', 'read'))
        self.assertEqual(record.active_source, '/recipe/absent')
        self.assertTrue(record.closed)
        self.assertEqual(result.span.start.line, 2)
        self.assertEqual(saved.writes, [])
        self.assertNotIn('/recipe/all', driver.completed)

    def test_unavailable_reader_is_not_missing(self):
        writer = MemoryTextWriter(['/recipe'])
        driver, process, saved = self.body(CatRuntime(OutputPolicy('latin-1'), ArtifactBackend(), writer))
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(writer.files, {'/recipe/out': b''})  # upstream opens destination first
        self.assertEqual(result.bodies[0].scope.local['BEFORE'], 'yes')
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(saved.writes, [])

    def test_unavailable_writer_precedes_read_without_mutation(self):
        reader = MemoryArtifacts({'/recipe/in': b'x'})
        driver, process, saved = self.body(CatRuntime(OutputPolicy('latin-1'), reader, TextWriter()))
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(reader.observations, [])
        self.assertFalse(result.bodies[0].evaluation.cats[0].opened)
        self.assertEqual(process.requests, [])

    def test_writer_open_failure_precedes_reads(self):
        reader = MemoryArtifacts({'/recipe/in': b'x'})
        driver, process, saved = self.body(CatRuntime(OutputPolicy('latin-1'), reader, MemoryTextWriter()))
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.bodies[0].evaluation.cats[0].phase, 'open')
        self.assertEqual(reader.observations, [])

    def test_partial_earlier_file_survives_later_missing_source(self):
        engine, reader, writer = self.engine({'/recipe/a': b'abc', '/recipe/out': b'old'})
        with self.assertRaises(SemanticError):
            engine.run(program(':cat >>out a absent\n'))
        self.assertEqual(writer.files['/recipe/out'], b'oldabc')

    def test_read_error_mid_file_writes_none_of_that_file(self):
        class Reader(ArtifactBackend):
            def chunks(self, path):
                yield b'partial'
                raise OSError('read failed')
        engine, reader, writer = self.engine({'/recipe/out': b'old'}, reader=Reader())
        with self.assertRaises(SemanticError):
            engine.run(program(':cat >>out in\n'))
        self.assertEqual(writer.files['/recipe/out'], b'old')

    def test_invalid_reader_result_fails(self):
        class Reader(ArtifactBackend):
            def chunks(self, path):
                yield 'not bytes'
        engine, reader, writer = self.engine(reader=Reader())
        with self.assertRaises(SemanticError):
            engine.run(program(':cat >>out in\n'))
        self.assertEqual(writer.files['/recipe/out'], b'')

    def test_write_and_close_error_records(self):
        class Writer(TextWriter):
            def open_bytes(self, request):
                return self
            def write(self, data):
                if self.fail == 'write':
                    raise OSError('write failure')
            def close(self):
                if self.fail == 'close':
                    raise OSError('close failure')
        for phase in ('write', 'close'):
            writer = Writer()
            writer.fail = phase
            driver, process, saved = self.body(CatRuntime(OutputPolicy('latin-1'),
                MemoryArtifacts({'/recipe/in': b'abc'}), writer))
            result = driver.build('all')
            record = result.bodies[0].evaluation.cats[0]
            self.assertEqual((result.status, record.phase), ('FAILED', phase))
            self.assertEqual(saved.writes, [])
            self.assertNotIn('/recipe/all', driver.completed)

    def test_unselected_cat_does_not_read_or_write(self):
        engine, reader, writer = self.engine()
        result = engine.run(program('@if False:\n  :cat >>out $UNDEFINED\nX = yes\n'))
        self.assertEqual(result.cats, [])
        self.assertEqual(writer.requests, [])
        self.assertEqual(reader.observations, [])

    def test_write_error_can_leave_partial_backend_effect(self):
        class Writer(MemoryTextWriter):
            def open_bytes(self, request):
                session = super(Writer, self).open_bytes(request)
                class Partial(object):
                    def write(self, data):
                        session.write(data[:1])
                        raise OSError('partial write')
                    def close(self):
                        session.close()
                return Partial()
        writer = Writer(['/recipe'], {'/recipe/in': b'abc', '/recipe/out': b'old'})
        engine, reader, writer = self.engine(writer=writer)
        with self.assertRaises(SemanticError):
            engine.run(program(':cat >>out in\n'))
        self.assertEqual(writer.files['/recipe/out'], b'olda')

    def test_action_uses_same_capability_and_cause_location(self):
        from test_actions import ExtractTests
        for available in (True, False):
            driver, runtime, workspace, saved, definition = ExtractTests().make(
                action=':cat >>out $OUTER\n')
            files = {'/recipe/work/invoker': b'action'} if available else {}
            writer = MemoryTextWriter(['/recipe/work'], files)
            driver.capabilities = driver.capabilities.replace(cat_runtime=
                CatRuntime(OutputPolicy('latin-1'),
                    MemoryArtifacts(writer.files, shared=True), writer))
            result = driver.build('all')
            self.assertEqual(result.status, 'COMPLETE' if available else 'FAILED')
            self.assertEqual(writer.requests[0].cwd, '/recipe/work')
            self.assertEqual(writer.requests[0].span.source_id, '/definitions/actions.aap')
            self.assertEqual(driver.cwd, '/recipe')
            self.assertEqual(saved.writes, [])
            if available:
                self.assertEqual(writer.files['/recipe/work/out'], b'action')
            else:
                self.assertEqual(result.span.source_id, '/definitions/actions.aap')
                self.assertNotIn('/recipe/done/extract', runtime.markers.files)

    def test_print_then_cat_and_cat_then_print_share_bytes(self):
        engine, reader, writer = self.engine({'/recipe/in': b'\xffno-newline'})
        result = engine.run(program(':print >! out caf\u00e9\n:cat >>out in\n:print >>out tail\n'))
        self.assertEqual(writer.files['/recipe/out'], b'caf\xe9\n\xffno-newlinetail\n')
        self.assertEqual(len(result.prints), 2)
        self.assertEqual(len(result.cats), 1)

    def test_print_generated_source_is_read_live(self):
        engine, reader, writer = self.engine()
        engine.run(program(':print >! in hello\n:cat >>out in\n'))
        self.assertEqual(writer.files['/recipe/out'], b'hello\n')

    def test_process_success_is_effect_opaque(self):
        process = Process()
        engine, reader, writer = self.engine({'/recipe/in': b'x', '/recipe/out': b'old'},
            process_backend=process, process_policy=ProcessPolicy('latin-1', sys_mode='unlogged'))
        result = engine.run(program(':sys cat in\n:cat >>out in\n'))
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].command, 'cat in')
        self.assertEqual(writer.files['/recipe/out'], b'oldx')
        self.assertEqual(len(result.cats), 1)

    def test_shell_append_redirect_stays_external_to_aap_cat(self):
        process = Process()
        engine, reader, writer = self.engine({'/recipe/out': b'old'}, process_backend=process,
            process_policy=ProcessPolicy('latin-1', sys_mode='unlogged'))
        engine.run(program(':sys cat in >>out\n'))
        self.assertEqual(writer.files['/recipe/out'], b'old')
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].command, 'cat in >>out')

    def test_cd_include_nested_shared_capability_and_restoration(self):
        from test_cd import CdTests
        loader = Loader({'/recipe/sub/part.aap': ':cat >>out in\n'})
        driver, dirs, process, saved = CdTests().make(':cd sub\n:include part.aap\n:update B\n:cat >>out in',
            extra='B {virtual}:\n  :cat >>out in\n', loader=loader)
        writer = MemoryTextWriter(['/recipe', '/recipe/sub'],
            {'/recipe/in': b'root', '/recipe/sub/in': b'sub'})
        reader = MemoryArtifacts(writer.files, shared=True)
        driver.capabilities = driver.capabilities.replace(cat_runtime=
            CatRuntime(OutputPolicy('latin-1'), reader, writer))
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(writer.files['/recipe/out'], b'root')
        self.assertEqual(writer.files['/recipe/sub/out'], b'subsub')
        self.assertEqual(writer.requests[0].span.source_id, '/recipe/sub/part.aap')
        self.assertEqual([r.cwd for r in writer.requests], ['/recipe/sub', '/recipe', '/recipe/sub'])
        self.assertEqual(driver.cwd, '/recipe')
        self.assertEqual(process.requests, [])
        self.assertEqual(saved.writes, [])


class NanoCatTests(unittest.TestCase):
    def setup_nano(self, source=True, writer_available=True):
        from test_port_runtime import NanoPortIntegration
        harness = NanoPortIntegration()
        base = '/authorized/ports/editors/nano/'
        facts = {'pack': 'EXISTS', 'files/post_i': 'EXISTS', 'work/post_i': 'EXISTS',
                 'files/unpost_i': 'MISSING', 'files/pre_i': 'MISSING',
                 'work/pre_i': 'MISSING', 'work/unpost_i': 'MISSING'}
        paths = MemoryPathObserver(dict((base + k, PathObservation(v)) for k, v in facts.items()))
        driver, runtime, saved, process = harness.setup_nano(actions=harness.extraction(), enable_sys=True,
            directories=MemoryPortDirectories([base + 'work/nano-7.1']), path_observer=paths)
        files = {base + 'work/post_i': b'#!/bin/sh\n'}
        if source:
            files[base + 'files/post_i'] = b'# controlled post\n\x00\xfftail'
        writer = MemoryTextWriter([base + 'work'], files)
        reader = MemoryArtifacts(writer.files, shared=True)
        policy = OutputPolicy('latin-1', False)
        driver.capabilities = driver.capabilities.replace(
            output_runtime=PrintRuntime(policy, writer=writer),
            cat_runtime=CatRuntime(policy, reader,
                                   writer if writer_available else TextWriter()))
        self.assertEqual(driver.build().status, 'COMPLETE')
        return driver, writer, reader, saved, process, base

    def test_nano_cat_and_prepared_rpm_signature(self):
        driver, writer, reader, saved, process, base = self.setup_nano()
        result = driver.build('rpm')
        self.assertEqual([b.target.name for b in result.bodies],
            ['testdepend', 'test', 'fake-install', 'prep-rpm', 'chkpost', 'local-rpm', 'build-rpm', 'rpm'])
        self.assertTrue(all(b.status == 'COMPLETED' for b in result.bodies))
        body = result.bodies[4]
        record = body.evaluation.cats[0]
        self.assertEqual(record.span.source_id, '/authorized/ports/globals.aap')
        self.assertEqual(record.span.start.line, 741)
        self.assertEqual(record.request.cwd, base[:-1])
        self.assertEqual(record.request.raw, '>>$script $file')
        self.assertEqual(record.request.expanded_destination, 'work/post_i')
        self.assertEqual(record.request.expanded_sources, 'files/post_i')
        self.assertEqual(record.request.path, base + 'work/post_i')
        self.assertEqual(record.request.paths, (base + 'files/post_i',))
        self.assertEqual((record.bytes_read, record.bytes_written), (24, 24))
        self.assertEqual(writer.files[record.request.path], b'#!/bin/sh\n# controlled post\n\x00\xfftail')
        self.assertEqual(len(writer.files[record.request.path]), 34)
        self.assertEqual(reader.observations, [('read', base + 'files/post_i')])
        self.assertEqual(body.evaluation.processes, [])
        self.assertIn(b'Name:\t\tcompany.nano\n', writer.files[base + 'work/nano.spec'])
        self.assertEqual((result.status, result.reason), ('COMPLETE', 'requested_targets_complete'))
        self.assertIn(base + 'rpm', driver.completed)
        self.assertIn(base + 'chkpost', driver.completed)
        self.assertEqual(len(process.requests), 10)  # no cat process; final supported sys batch is opaque
        self.assertEqual(saved.writes, [])
        self.assertEqual(len(result.pending_signatures), 18)
        prepared = [p for p in driver.observations.preparations if p.target.name == 'rpm']
        self.assertTrue(prepared)
        self.assertEqual(prepared[0].commands, '\t:pass\n')
        self.assertEqual(prepared[0].signature, '3966681317ecb02ebc59fd8d3ae5a72f')
        self.assertEqual(result.bodies[-1].context.prepared_buildcheck,
                         prepared[0].signature)

    def test_nano_missing_source_does_not_continue_or_invent_file(self):
        driver, writer, reader, saved, process, base = self.setup_nano(source=False)
        result = driver.build('rpm')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.span.start.line, 741)
        self.assertEqual(result.bodies[-1].target.name, 'chkpost')
        self.assertNotIn(base + 'files/post_i', writer.files)
        self.assertEqual(writer.files[base + 'work/post_i'], b'#!/bin/sh\n')
        self.assertNotIn(base + 'chkpost', driver.completed)
        self.assertEqual(len(process.requests), 9)
        self.assertEqual(saved.writes, [])

    def test_nano_writer_unavailable_does_not_read(self):
        driver, writer, reader, saved, process, base = self.setup_nano(writer_available=False)
        result = driver.build('rpm')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.span.start.line, 741)
        self.assertEqual(reader.observations, [])
        self.assertEqual(writer.files[base + 'work/post_i'], b'#!/bin/sh\n')
        self.assertEqual(saved.writes, [])


if __name__ == '__main__':
    unittest.main()
