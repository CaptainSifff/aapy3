"""Capture characterization from Commands.aap_syseval/_get_redir/_pipe_assign.

Production: process-forms/summary.json and process-pipelines.tsv; ports/globals.aap.
The supplied upstream rectests have no direct syseval assertions. These small
source-derived fixtures use only a fake backend and never invoke a shell.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse
from aap_semantics import (lower, Evaluator, Scope, SemanticError, Unsupported,
                           ProcessBackend, ProcessBackendError, ProcessResult,
                           ProcessPolicy)
from check_frontend_py34 import check_text


class FakeBackend(ProcessBackend):
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if not self.results:
            raise AssertionError('unexpected process request')
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def program(text, name='/recipes/main.aap'):
    return lower(parse(Source(name, text)))


def capture(text, results=None, scope=None, encoding='utf-8', cwd=None):
    backend = FakeBackend([ProcessResult(0, b'answer\n')] if results is None else results)
    evaluator = Evaluator(scope, process_backend=backend,
                          process_policy=ProcessPolicy(encoding), cwd=cwd)
    result = evaluator.run(program(text))
    return result, backend


class ProcessTests(unittest.TestCase):
    def test_request_policy_source_and_pipeline_representation(self):
        text = ':syseval inspect -v | :assign rel\n'
        result, backend = capture(text)
        request = backend.requests[0]
        self.assertEqual(request.command, 'inspect -v')
        self.assertEqual(request.command_bytes, b'inspect -v')
        self.assertEqual(request.shell_command, '(inspect -v)')
        self.assertEqual(request.shell_command_bytes, b'(inspect -v)')
        self.assertEqual(request.cwd, '/recipes')
        self.assertEqual(request.cwd_bytes, b'/recipes')
        self.assertEqual(request.shell_mode, 'posix-sh')
        self.assertTrue(request.capture_stdout)
        self.assertEqual(request.stderr_policy, 'inherit')
        self.assertEqual(request.stdin_policy, 'inherit')
        self.assertIsNone(request.environment)
        self.assertFalse(request.echo)
        self.assertFalse(request.skip_in_dry_run)
        self.assertEqual(request.source.slice(request.span), text)
        record = result.processes[0]
        self.assertEqual(record.pipeline.target, 'rel')
        self.assertEqual(record.pipeline.shell_source, 'inspect -v')
        self.assertIs(record.request, request)
        self.assertEqual(record.output, 'answer')
        self.assertIsNone(record.print_text)
        self.assertEqual(result.scope.local, {'_prevdir': None, 'exit': 0, 'rel': 'answer'})
        self.assertTrue(result.complete)

    def test_exact_expansion_without_recipe_scope_export(self):
        result, backend = capture('TOOL = /chosen/tool\nARG = "two words"\n'
                    ':syseval $TOOL $ARG $$HOME 2>&1 | :assign rel\n')
        self.assertEqual(backend.requests[0].command, '/chosen/tool "two words" $HOME 2>&1')
        self.assertIsNone(backend.requests[0].environment)
        self.assertEqual(result.scope.local['rel'], 'answer')

    def test_explicit_cwd_passed_exactly(self):
        result, backend = capture(':syseval inspect | :assign rel\n', cwd='/work/context')
        self.assertEqual(backend.requests[0].cwd, '/work/context')
        self.assertTrue(result.complete)

    def test_missing_directory_or_backend_policy_is_explicit(self):
        backend = FakeBackend([])
        with self.assertRaises(ValueError):
            Evaluator(process_backend=backend)
        with self.assertRaises(Unsupported):
            Evaluator(process_backend=backend, process_policy=ProcessPolicy('utf-8')).run(
                program(':syseval inspect\n', '<string>'))
        self.assertEqual(backend.requests, [])

    def test_precise_byte_whitespace_normalization(self):
        cases = [(b'value', 'value'), (b'value\n', 'value'),
                 (b'value\n\n', 'value'), (b'value  \t', 'value'),
                 (b'', ''), (b'\t\r\n\v\f ', ''),
                 (b' \nfirst \n second\n\n', 'first \n second'),
                 (b'\r\nvalue\r\n', 'value'),
                 (b' \xa0value\xa0\n', '\u00a0value\u00a0')]
        for raw, expected in cases:
            result, unused = capture(':syseval inspect | :assign rel\n',
                         [ProcessResult(0, raw)], encoding='latin-1')
            self.assertEqual(result.scope.local['rel'], expected)

    def test_text_backend_output_has_explicit_byte_policy(self):
        result, unused = capture(':syseval inspect | :assign rel\n',
                         [ProcessResult(0, '\n\u00e9\u00a0 \n')], encoding='latin-1')
        self.assertEqual(result.scope.local['rel'], '\u00e9\u00a0')

    def test_output_is_not_expanded_or_converted_to_a_list(self):
        result, unused = capture(':syseval inspect | :assign rel\n',
                       [ProcessResult(0, b' $MISSING `bad()` {attr} "x y" \n')])
        self.assertEqual(result.scope.local['rel'], '$MISSING `bad()` {attr} "x y"')

    def test_nonzero_wait_status_still_assigns_and_continues(self):
        result, unused = capture(':syseval inspect | :assign rel\nAFTER = yes\n',
                                 [ProcessResult(7 << 8, b'partial\n')])
        self.assertEqual(result.scope.local['exit'], 1792)
        self.assertEqual(result.scope.local['rel'], 'partial')
        self.assertEqual(result.scope.local['AFTER'], 'yes')
        self.assertTrue(result.complete)

    def test_failed_empty_capture_overwrites_previous_value(self):
        result, unused = capture('rel = old\n:syseval inspect | :assign rel\n',
                                 [ProcessResult(256)])
        self.assertEqual(result.scope.local['rel'], '')
        self.assertEqual(result.scope.local['exit'], 256)

    def test_later_capture_overwrites_exit_but_never_sysresult(self):
        result, unused = capture('@sysresult = 9\n:syseval one | :assign one\n'
                    ':syseval two | :assign two\n',
                    [ProcessResult(256, b'failed'), ProcessResult(0, b'ok')])
        self.assertEqual(result.scope.local['exit'], 0)
        self.assertEqual(result.scope.local['sysresult'], 9)
        self.assertEqual([r.result.wait_status for r in result.processes], [256, 0])

    def test_stderr_observation_is_not_merged_into_capture(self):
        result, backend = capture(':syseval inspect | :assign rel\n',
                         [ProcessResult(0, b'out\n', b'error\n')])
        self.assertEqual(result.scope.local['rel'], 'out')
        self.assertEqual(result.processes[0].result.stderr, b'error\n')
        self.assertEqual(backend.requests[0].stderr_policy, 'inherit')

    def test_inline_stderr_redirect_remains_shell_text(self):
        result, backend = capture(':syseval inspect 2>&1 | :assign rel\n',
                         [ProcessResult(0, b'out\nerror\n')])
        self.assertEqual(backend.requests[0].command, 'inspect 2>&1')
        self.assertEqual(result.scope.local['rel'], 'out\nerror')

    def test_shell_pipe_inside_token_is_retained_aap_stage_removed(self):
        result, backend = capture(':syseval one|two | :assign rel\n')
        self.assertEqual(backend.requests[0].command, 'one|two')
        self.assertEqual(result.scope.local['rel'], 'answer')

    def test_expansion_introduced_operators_are_not_reparsed_as_aap_syntax(self):
        for command in ('one | two', 'one && two || three; four', 'one >out',
                        'one | :assign literal_shell_text'):
            scope = Scope.top_level()
            scope.store('CMD', command, program(''))
            result, backend = capture(':syseval $CMD | :assign rel\n', scope=scope)
            self.assertEqual(backend.requests[0].command, command)
            self.assertEqual(result.scope.local['rel'], 'answer')

    def test_quoted_operators_are_not_aap_pipeline_syntax(self):
        result, backend = capture(':syseval inspect "| :assign fake" ">data" | :assign rel\n')
        self.assertEqual(backend.requests[0].command, 'inspect "| :assign fake" ">data"')
        self.assertNotIn('fake', result.scope.local)

    def test_bare_pipe_at_token_boundary_requires_aap_colon_historically(self):
        backend = FakeBackend([])
        for text in (':syseval one | two\n', ':syseval one |\n', ':syseval one || two\n'):
            with self.assertRaises(SemanticError) as error:
                Evaluator(process_backend=backend, process_policy=ProcessPolicy('utf-8')).run(program(text))
            self.assertIn("missing ':'", str(error.exception))
        self.assertEqual(backend.requests, [])

    def test_assign_current_explicit_and_created_scopes(self):
        outer = Scope.top_level()
        outer.store('rel', 'outer', program(''))
        inner = Scope((outer,))
        inner.namespaces['_recipe'] = outer.namespaces['_recipe']
        result, unused = capture(':syseval one | :assign rel\n'
                    ':syseval two | :assign _recipe.rel\n'
                    ':syseval three | :assign user_scope.rel\n',
                    [ProcessResult(0, b'local'), ProcessResult(0, b'recipe'),
                     ProcessResult(0, b'user')], scope=inner)
        self.assertEqual(inner.local['rel'], 'local')
        self.assertEqual(outer.local['rel'], 'recipe')
        self.assertEqual(inner.read('user_scope.rel', program('')), 'user')
        self.assertTrue(result.complete)

    def test_assign_name_not_expanded_and_failure_occurs_after_capture(self):
        for target in ('$TARGET', 'rel +=', 'one two', ''):
            backend = FakeBackend([ProcessResult(256, b'out')])
            scope = Scope.top_level()
            with self.assertRaises(SemanticError):
                Evaluator(scope, process_backend=backend, process_policy=ProcessPolicy('utf-8')).run(
                    program(':syseval inspect | :assign ' + target + '\n'))
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(scope.local, {'_prevdir': None, 'exit': 256})

    def test_backend_failure_has_source_location_and_stops_metadata(self):
        for failure in (ProcessBackendError('offline'), OSError('cannot launch')):
            backend = FakeBackend([failure])
            scope = Scope.top_level()
            with self.assertRaises(SemanticError) as error:
                Evaluator(scope, process_backend=backend, process_policy=ProcessPolicy('utf-8')).run(
                    program('BEFORE = yes\n:syseval inspect | :assign rel\nAFTER = no\n'))
            self.assertIn('/recipes/main.aap:2:1: process backend failed', str(error.exception))
            self.assertEqual(scope.local, {'_prevdir': None, 'BEFORE': 'yes'})

    def test_invalid_backend_result_is_diagnosed(self):
        for value in (None, ProcessResult('0', b'out'), ProcessResult(0, None)):
            with self.assertRaises(SemanticError):
                capture(':syseval inspect | :assign rel\n', [value])

    def test_strict_encoding_and_decoding_failures(self):
        backend = FakeBackend([])
        scope = Scope.top_level()
        scope.store('CMD', '\u00e9', program(''))
        with self.assertRaises(SemanticError):
            Evaluator(scope, process_backend=backend, process_policy=ProcessPolicy('ascii')).run(
                program(':syseval $CMD | :assign rel\n'))
        self.assertEqual(backend.requests, [])
        with self.assertRaises(SemanticError):
            Evaluator(process_backend=backend, process_policy=ProcessPolicy('utf-16')).run(
                program(':syseval inspect | :assign rel\n'))
        self.assertEqual(backend.requests, [])
        backend = FakeBackend([ProcessResult(256, b'\xff')])
        with self.assertRaises(SemanticError) as error:
            Evaluator(scope, process_backend=backend, process_policy=ProcessPolicy('utf-8')).run(
                program(':syseval inspect | :assign rel\nAFTER = no\n'))
        self.assertIn('decoding error', str(error.exception))
        self.assertEqual(scope.local['exit'], 256)
        self.assertNotIn('rel', scope.local)
        self.assertNotIn('AFTER', scope.local)

    def test_nonascii_command_encoded_explicitly(self):
        result, backend = capture(':syseval inspect \u00e9 | :assign rel\n', encoding='latin-1')
        self.assertEqual(backend.requests[0].command_bytes, b'inspect \xe9')
        self.assertTrue(result.complete)

    def test_bare_capture_retains_print_event_without_host_output(self):
        for stdout, expected in ((b' value\n', 'value\n'), (b'', '\n')):
            result, unused = capture(':syseval inspect\n', [ProcessResult(0, stdout)])
            self.assertEqual(result.processes[0].print_text, expected)
            self.assertEqual(result.scope.local, {'_prevdir': None, 'exit': 0})

    def test_backtick_then_pipeline_split_then_dollar_expansion(self):
        result, backend = capture('@flag = "-v"\nCMD = inspect\n'
                    ':syseval $CMD `flag` | :assign rel\n')
        self.assertEqual(backend.requests[0].command, 'inspect -v')
        self.assertEqual(result.scope.local['rel'], 'answer')

    def test_unsupported_forms_reject_before_backend(self):
        backend = FakeBackend([])
        for text in (':syseval {stderr} inspect\n', ':syseval {f} inspect\n',
                     ':syseval inspect >out\n', ':syseval inspect | :tee out\n',
                     ':syseval inspect | :assign one | :assign two\n'):
            with self.assertRaises(Unsupported):
                Evaluator(process_backend=backend, process_policy=ProcessPolicy('utf-8')).run(program(text))
        self.assertEqual(backend.requests, [])

    def test_other_process_commands_remain_barriers_before_argument_effects(self):
        for command in ('sys', 'system', 'assign'):
            result, backend = capture('BEFORE = yes\n:' + command +
                                     ' `unapproved()`\nAFTER = no\n', [])
            self.assertFalse(result.complete)
            self.assertEqual(result.halted_at.name, command)
            self.assertEqual(result.scope.local, {'_prevdir': None, 'BEFORE': 'yes'})
            self.assertEqual(backend.requests, [])

    def test_absent_backend_remains_safe_barrier(self):
        result = Evaluator().run(program(':syseval inspect | :assign rel\nAFTER = no\n'))
        self.assertEqual(result.halted_at.name, 'syseval')
        self.assertEqual(result.scope.local, {'_prevdir': None})

    def test_include_shares_current_directory_and_capture_scope(self):
        class Loader(object):
            def load(self, path):
                return Source(path, ':syseval inspect | :assign rel\n')
        backend = FakeBackend([ProcessResult(0, b'child')])
        result = Evaluator(include_loader=Loader(), process_backend=backend,
                           process_policy=ProcessPolicy('utf-8')).run(
                    program(':include sub/part.aap\nAFTER = $rel\n'))
        self.assertEqual(backend.requests[0].cwd, '/recipes')
        self.assertEqual(backend.requests[0].span.source_id, '/recipes/sub/part.aap')
        self.assertEqual(result.scope.local['AFTER'], 'child')

    def test_globals_crosses_capture_boundary_and_registers_dependencies_without_execution(self):
        source = Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1')
        ast = lower(parse(source))
        scope = Scope.top_level()
        scope.store('OSNAME', 'Linux', ast)
        scope.store('BDIR', 'build', ast)  # Explicit startup metadata, no default execution.
        backend = FakeBackend([ProcessResult(0, b'SUSE15\n'), ProcessResult(0, b'suse\n'),
                               ProcessResult(0, b'SUSE 15 6\n')])
        result = Evaluator(scope, process_backend=backend,
                           process_policy=ProcessPolicy('latin-1')).run(ast)
        self.assertEqual([r.command for r in backend.requests], [
            '/usr/local/nacharbeiten/na_bin/getdistro -v',
            '/usr/local/nacharbeiten/na_bin/getdistro',
            '/usr/local/nacharbeiten/na_bin/getdistro -vv'])
        self.assertEqual((scope.local['rel'], scope.local['rel2'], scope.local['relvv']),
                         ('SUSE15', 'suse', 'SUSE 15 6'))
        self.assertEqual(scope.local['RPMBASE'], '/export/company/SLES15SP6')
        self.assertEqual(scope.local['DISTRO'], 'suse')
        self.assertEqual(scope.local['RPMDIR'], '/export/company/SLES15SP6/bsus')
        self.assertIsNone(result.halted_at)
        self.assertTrue(result.complete)
        self.assertEqual(scope.local['MKRPM'], '/usr/bin/rpmbuild')
        self.assertEqual(len(result.graph.definitions), 32)
        self.assertEqual(len(result.graph.targets), 32)
        self.assertEqual(len(result.graph.nodes), 37)
        self.assertEqual(sum(d.body is not None for d in result.graph.definitions), 31)
        self.assertEqual(sum(len(d.source_items) for d in result.graph.definitions), 15)
        first = result.graph.definitions[0]
        self.assertEqual(first.span.start.line, 199)
        self.assertEqual(first.target_items[0].name, 'rpmcp_x86')
        self.assertEqual(first.source_items[0].name, 'fetch')
        self.assertEqual(result.graph.definitions[-1].target_items[0].name, 'IndexEntry')
        self.assertTrue(all(not d.body.origin.scanned for d in result.graph.definitions if d.body))
        self.assertEqual(len(result.declarations.actions['extract']), 4)

    def test_compatibility_guard_rejects_host_process_apis(self):
        # Popen is reviewed for the real adapter; run and text=True are not.
        for text in ('import subprocess\nsubprocess.run(["x"])',
                     'import subprocess\nsubprocess.Popen("x", text=True)',
                     'import os\nos.system("x")'):
            self.assertTrue(check_text(text))


if __name__ == '__main__':
    unittest.main()
