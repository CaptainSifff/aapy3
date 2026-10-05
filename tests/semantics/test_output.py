"""Commands.aap_print/_get_redir/_write2file; Message.msg_print.

Focused scenarios include rectest/test014.py's generated include redirections.
No host terminal, file write, compiler or external process is used.
"""
import os
import io
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, PrintRuntime, OutputPolicy,
    MemoryOutputSink, MemoryTextWriter, OutputResult, TextWriter,
    SemanticError, Unsupported, MemoryPathObserver, PathObservation)
from test_cd import Process, Loader
from aap_semantics.process import ProcessPolicy
from output_adapter import StdoutOutputSink


def program(text):
    return lower(parse(Source('/recipe/main.aap', text)))


class PrintTests(unittest.TestCase):
    def evaluate(self, text, variables=None, encoding='latin-1', writer=None, sink=None,
                 paths=None, **kwargs):
        scope = Scope.top_level()
        scope.local.update(variables or {})
        runtime = PrintRuntime(OutputPolicy(encoding, False), sink=sink, writer=writer)
        engine = Evaluator(scope, cwd='/recipe', output_runtime=runtime, path_observer=paths, **kwargs)
        result = engine.run(program(text))
        return result, runtime

    def test_literal_and_provenance(self):
        result, runtime = self.evaluate(':print hello\n', {'MESSAGE': 'error'})
        record = result.prints[0]
        self.assertEqual(record.status, 'COMPLETED')
        self.assertEqual(record.request.data, b'hello\n')
        self.assertEqual(record.request.raw, 'hello')
        self.assertEqual(record.span.start.line, 1)
        self.assertEqual(record.span.source_id, '/recipe/main.aap')
        self.assertIs(runtime.sink.events[0], record.request)
        self.assertEqual(record.request.log_text, 'hello')
        self.assertEqual(result.processes, [])

    def test_stdout_sink_writes_encoded_bytes_and_retains_events(self):
        stream = io.BytesIO()
        sink = StdoutOutputSink(lambda: stream)
        result, runtime = self.evaluate(':print caf\u00e9\n:print\n',
                                        encoding='latin-1', sink=sink)
        self.assertEqual(stream.getvalue(), b'caf\xe9\n\n')
        self.assertEqual(len(sink.events), 2)
        self.assertEqual([record.status for record in result.prints],
                         ['COMPLETED', 'COMPLETED'])

    def test_redirected_print_stays_file_only_with_stdout_sink(self):
        stream = io.BytesIO()
        sink = StdoutOutputSink(lambda: stream)
        writer = MemoryTextWriter(['/recipe'])
        result, runtime = self.evaluate(
            ':print >! file hello\n:print >>file world\n',
            sink=sink, writer=writer)
        self.assertEqual(stream.getvalue(), b'')
        self.assertEqual(sink.events, [])
        self.assertEqual(writer.files['/recipe/file'], b'hello\nworld\n')
        self.assertEqual([record.request.destination for record in result.prints],
                         ['overwrite', 'append'])

    def test_integer_variable_and_optional(self):
        result, runtime = self.evaluate(':print $VALUE $?MISSING\n', {'VALUE': 42})
        self.assertEqual(result.prints[0].request.data, b'42 \n')

    def test_undefined_and_deferred_expansion(self):
        for text in (':print $MISSING\n', 'X $= delayed\n:print $X\n'):
            sink = MemoryOutputSink()
            with self.assertRaises(SemanticError):
                self.evaluate(text, sink=sink)
            self.assertEqual(sink.events, [])

    def test_empty_and_no_no_newline_option(self):
        result, runtime = self.evaluate(':print\n:print -n text\n')
        self.assertEqual([x.request.data for x in result.prints], [b'\n', b'-n text\n'])

    def test_spaces_tabs_and_quotes_retained(self):
        result, runtime = self.evaluate(':print "a  b"\t\tc   \n')
        self.assertEqual(result.prints[0].request.data, b'"a  b"\t\tc\n')

    def test_expansion_whitespace_is_not_trimmed(self):
        result, runtime = self.evaluate(':print $X\n', {'X': '  a\n b \n'})
        self.assertEqual(result.prints[0].request.data, b'  a\n b \n\n')

    def test_continuation_joins_and_br(self):
        result, runtime = self.evaluate(':print one\n    two\n:print a$br\n    b\n')
        self.assertEqual([r.request.data for r in result.prints], [b'one two\n', b'a\nb\n'])

    def test_backtick_expansion_and_escaped_operator(self):
        result, runtime = self.evaluate(':print `"> file | text"`\n', {'gt': '>', 'bar': '|'})
        self.assertEqual(result.prints[0].request.data, b'> file | text\n')
        self.assertEqual(result.prints[0].request.destination, 'stdout')

    def test_variable_operators_are_not_rescanned(self):
        result, runtime = self.evaluate(':print $X\n', {'X': '>! file | :assign evil'})
        self.assertEqual(result.prints[0].request.destination, 'stdout')
        self.assertEqual(result.prints[0].request.text, '>! file | :assign evil')

    def test_literal_shell_text_and_attribute_text(self):
        result, runtime = self.evaluate(':print "|" ">!" {q} a;b && c\n')
        self.assertEqual(result.prints[0].request.data, b'"|" ">!" {q} a;b && c\n')

    def test_explicit_encoding(self):
        for encoding, expected in (('latin-1', b'caf\xe9\n'), ('utf-8', b'caf\xc3\xa9\n')):
            result, runtime = self.evaluate(':print caf\u00e9\n', encoding=encoding)
            self.assertEqual(result.prints[0].request.text, 'caf\u00e9')
            self.assertEqual(result.prints[0].request.data, expected)

    def test_bad_encoding_has_no_effect(self):
        for encoding in ('ascii', 'no-such-codec'):
            writer = MemoryTextWriter(['/recipe'])
            with self.assertRaises(SemanticError):
                self.evaluate(':print >! file caf\u00e9\n', encoding=encoding, writer=writer)
            self.assertEqual(writer.files, {})
            self.assertEqual(writer.requests, [])

    def test_statement_order(self):
        result, runtime = self.evaluate('@x = 1\n:print $x\n@x = 2\n:print $x\n')
        self.assertEqual([e.data for e in runtime.sink.events], [b'1\n', b'2\n'])
        self.assertEqual(result.scope.local['x'], 2)

    def test_only_selected_branch(self):
        for state, expected in (('EXISTS', b'yes\n'), ('MISSING', b'no\n')):
            paths = MemoryPathObserver({'/recipe/item': PathObservation(state)})
            result, runtime = self.evaluate('@if os.path.exists("item"):\n  :print yes\n@else:\n  :print no\n', paths=paths)
            self.assertEqual([e.data for e in runtime.sink.events], [expected])
            self.assertEqual(len(paths.requests), 1)

    def test_unselected_unknown_output_variable_is_not_evaluated(self):
        result, runtime = self.evaluate('@if False:\n  :print $MISSING\n:print after\n')
        self.assertEqual([e.data for e in runtime.sink.events], [b'after\n'])

    def test_overwrite_then_append(self):
        writer = MemoryTextWriter(['/recipe'], {'/recipe/file': b'old'})
        result, runtime = self.evaluate(':print >! file new\n:print >> file next\n:print >> file\n', writer=writer)
        self.assertEqual(writer.files['/recipe/file'], b'new\nnext\n\n')
        self.assertEqual([r.request.destination for r in result.prints], ['overwrite', 'append', 'append'])
        self.assertEqual(runtime.sink.events, [])

    def test_append_creates_file(self):
        writer = MemoryTextWriter(['/recipe'])
        self.evaluate(':print >> file new\n', writer=writer)
        self.assertEqual(writer.files['/recipe/file'], b'new\n')

    def test_redirect_does_not_add_second_newline(self):
        writer = MemoryTextWriter(['/recipe'])
        self.evaluate(':print >! file $X\n', {'X': 'one\n\n'}, writer=writer)
        self.assertEqual(writer.files['/recipe/file'], b'one\n\n')

    def test_trailing_redirect_from_test014(self):
        writer = MemoryTextWriter(['/recipe'])
        self.evaluate(':print $(#)include "file.h" >! out\n:print tail >>out\n', writer=writer)
        self.assertEqual(writer.files['/recipe/out'], b'#include "file.h"\ntail\n')

    def test_quoted_and_expanded_destination(self):
        writer = MemoryTextWriter(['/recipe', '/absolute'])
        self.evaluate(':print >! $NAME text\n:print >> "/absolute/a b" other\n',
                      {'NAME': '"a b"'}, writer=writer)
        self.assertEqual(writer.files, {'/recipe/a b': b'text\n', '/absolute/a b': b'other\n'})

    def test_bad_redirections_fail_before_writes(self):
        for raw in ('>!', '>>   ', '>! a >! b text', '>! "" text'):
            writer = MemoryTextWriter(['/recipe'])
            with self.assertRaises(SemanticError):
                self.evaluate(':print ' + raw + '\n', writer=writer)
            self.assertEqual(writer.requests, [])

    def test_unsupported_destinations_and_pipes(self):
        for raw in ('> file text', '>! ~/file text', '>! *.out text', '>! http://remote/file text',
                    'text | :assign x', '>! a text | :print next'):
            writer, sink = MemoryTextWriter(['/recipe']), MemoryOutputSink()
            with self.assertRaises(Unsupported):
                self.evaluate(':print ' + raw + '\n', writer=writer, sink=sink)
            self.assertEqual(writer.files, {})
            self.assertEqual(sink.events, [])

    def test_logging_gate(self):
        for logging in (True, None):
            runtime = PrintRuntime(OutputPolicy('latin-1', logging))
            with self.assertRaises(Unsupported):
                Evaluator(output_runtime=runtime).run(program(':print text\n'))
            self.assertEqual(runtime.sink.events, [])

    def test_cwd_include_nested_frame_and_persistence(self):
        loader = Loader({'/recipe/sub/part.aap': ':print >! child child\n'})
        from test_cd import CdTests
        driver, dirs, process, saved = CdTests().make(':cd sub\n:include part.aap\n:update B\n:print >> child after',
            extra='B {virtual}:\n  :print >! nested inner\n', loader=loader)
        writer = MemoryTextWriter(['/recipe', '/recipe/sub'])
        driver.capabilities = driver.capabilities.replace(output_runtime=
            PrintRuntime(OutputPolicy('latin-1', False), writer=writer))
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(writer.files, {'/recipe/sub/child': b'child\nafter\n', '/recipe/nested': b'inner\n'})
        self.assertEqual(writer.requests[0].span.source_id, '/recipe/sub/part.aap')
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertEqual(process.requests, [])
        self.assertEqual(saved.writes, [])

    def test_blocked_and_failed_writer_stop_same_body(self):
        class BadWriter(TextWriter):
            def write(self, request):
                return OutputResult(self.outcome)
        for backend, status in ((TextWriter(), 'BLOCKED'), (MemoryTextWriter(), 'FAILED'),
                                (BadWriter(), 'FAILED')):
            if isinstance(backend, BadWriter):
                backend.outcome = 'invalid'
            from test_cd import CdTests
            driver, dirs, process, saved = CdTests().make('BEFORE = yes\n:print >! file text\nAFTER = no')
            driver.capabilities = driver.capabilities.replace(output_runtime=
                PrintRuntime(OutputPolicy('latin-1', False), writer=backend))
            result = driver.build('all')
            self.assertEqual(result.status, status)
            self.assertEqual(result.bodies[0].scope.local['BEFORE'], 'yes')
            self.assertNotIn('AFTER', result.bodies[0].scope.local)
            self.assertEqual(result.bodies[0].evaluation.prints[0].status, status)
            self.assertEqual(result.span.start.line, 3)
            self.assertEqual(saved.writes, [])
            self.assertNotIn('/recipe/all', driver.completed)

    def test_partial_effects_are_not_rolled_back(self):
        writer = MemoryTextWriter(['/recipe'])
        with self.assertRaises(SemanticError):
            self.evaluate(':print >! first good\n:print >> absent/file bad\n', writer=writer)
        self.assertEqual(writer.files, {'/recipe/first': b'good\n'})

    def test_sink_failure_and_unavailable(self):
        class Sink(object):
            def emit(self, request):
                raise self.failure
        for failure, error in ((OSError('broken'), SemanticError), (NotImplementedError('offline'), Unsupported)):
            sink = Sink()
            sink.failure = failure
            with self.assertRaises(error):
                self.evaluate(':print hello\n', sink=sink)

    def test_print_sys_and_bare_syseval_are_distinct(self):
        process = Process()
        result, runtime = self.evaluate(':print hello\n:sys echo hello\nSEPARATOR = yes\n:syseval echo hello\n',
            process_backend=process, process_policy=ProcessPolicy('latin-1', sys_mode='unlogged'))
        self.assertEqual(len(result.prints), 1)
        self.assertEqual(len(process.requests), 2)
        self.assertEqual([e.data for e in runtime.sink.events], [b'hello\n'])
        self.assertEqual(result.processes[-1].print_text, 'captured\n')
        self.assertEqual(result.scope.local['sysresult'], 0)

    def test_print_write_does_not_infer_observation(self):
        writer = MemoryTextWriter(['/recipe'])
        paths = MemoryPathObserver({'/recipe/file': PathObservation('MISSING')})
        result, runtime = self.evaluate(':print >! file text\n@answer = os.path.exists("file")\n',
                                        writer=writer, paths=paths)
        self.assertEqual(writer.files['/recipe/file'], b'text\n')
        self.assertIs(result.scope.local['answer'], False)

    def test_action_uses_shared_output_runtime(self):
        from test_actions import ExtractTests
        writer = MemoryTextWriter(['/recipe/work'])
        driver, runtime, workspace, saved, definition = ExtractTests().make(
            action=':print >! result $OUTER\n')
        output = PrintRuntime(OutputPolicy('latin-1', False), writer=writer)
        driver.capabilities = driver.capabilities.replace(output_runtime=output)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(writer.files, {'/recipe/work/result': b'invoker\n'})
        self.assertEqual(writer.requests[0].span.source_id, '/definitions/actions.aap')
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertEqual(saved.writes, [])

    def test_sys_success_does_not_populate_writer_or_observer(self):
        writer = MemoryTextWriter()
        process = Process()
        with self.assertRaises(SemanticError):
            self.evaluate(':sys mkdir -p sub\n:print >! sub/file text\n', writer=writer,
                process_backend=process, process_policy=ProcessPolicy('latin-1', sys_mode='unlogged'))
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(writer.files, {})


class NanoPrintTests(unittest.TestCase):
    def check_nano(self, exists):
        from test_port_runtime import NanoPortIntegration
        from aap_semantics import MemoryPortDirectories
        harness = NanoPortIntegration()
        base = '/authorized/ports/editors/nano/'
        paths = MemoryPathObserver({base + 'pack': PathObservation(exists),
            base + 'files/post_i': PathObservation('EXISTS'),
            base + 'work/post_i': PathObservation('EXISTS')})
        driver, runtime, saved, process = harness.setup_nano(actions=harness.extraction(), enable_sys=True,
            directories=MemoryPortDirectories([base + 'work/nano-7.1']), path_observer=paths)
        writer = MemoryTextWriter([base + 'work'])  # explicit writable parent; no inferred extraction effect
        output = PrintRuntime(OutputPolicy('latin-1', False), writer=writer)
        driver.capabilities = driver.capabilities.replace(output_runtime=output)
        self.assertEqual(driver.build().status, 'COMPLETE')
        result = driver.build('rpm')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'cat')
        self.assertEqual(result.span.start.line, 741)
        self.assertEqual([b.target.name for b in result.bodies],
                         ['testdepend', 'test', 'fake-install', 'prep-rpm', 'chkpost'])
        prep = result.bodies[-2]
        self.assertEqual(prep.status, 'COMPLETED')
        self.assertEqual(prep.context.cwd, base[:-1])
        self.assertEqual(len(prep.evaluation.prints), 27 if exists == 'EXISTS' else 25)
        first = prep.evaluation.prints[0].request
        self.assertEqual(first.span.start.line, 221)
        self.assertEqual(first.raw, '>! $file %define __find_requires %{nil}')
        self.assertEqual(first.text, '%define __find_requires %{nil}')
        self.assertEqual(first.data, b'%define __find_requires %{nil}\n')
        self.assertEqual(first.path, base + 'work/nano.spec')
        self.assertEqual(writer.files[first.path], b''.join(r.data for r in writer.requests))
        content = writer.files[first.path]
        self.assertIn(b'Name:\t\tcompany.nano\n', content)
        self.assertIn(b'Version:\t7.1\n', content)
        self.assertIn(b'%description\nnano is a small, free', content)
        self.assertEqual(b'%defattr(-, -, -)\n/*\n' in content, exists == 'EXISTS')
        self.assertTrue(content.endswith(b'\n'))
        self.assertIn(base + 'prep-rpm', driver.completed)
        self.assertNotIn(base + 'chkpost', driver.completed)
        self.assertEqual([r.path for r in paths.requests], [base+'pack', base+'files/post_i', base+'work/post_i'])
        self.assertEqual(len(process.requests), 9)
        self.assertEqual(output.sink.events, [])
        self.assertEqual(saved.writes, [])
        self.assertEqual(len(result.pending_signatures), 14)
        # The writer establishes spec bytes only. Observation fixtures remain unchanged.
        self.assertNotIn(first.path, paths.observations)

    def test_nano_existing_pkgdir(self):
        self.check_nano('EXISTS')

    def test_nano_missing_pkgdir(self):
        self.check_nano('MISSING')


if __name__ == '__main__':
    unittest.main()
