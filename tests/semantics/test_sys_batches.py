"""Process.Process / ParsePos.nextline / aap_shell / logged_system evidence.

Only recording capabilities: no shell, package command or filesystem effects.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, ProcessBackend, ProcessResult,
    ProcessPolicy, ProcessUnavailable, ProcessBackendError, Unsupported, SemanticError,
    BuildDriver, MemoryTargetState, MemoryPersistence, PortRuntime, MemoryMarkers)
from aap_semantics.port_commands import PortCommandRuntime, MemoryPortDirectories


class Recording(ProcessBackend):
    def __init__(self, outcome=None):
        self.outcome = ProcessResult(0, b'capture\n') if outcome is None else outcome
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class SequencedRecording(ProcessBackend):
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.outcomes.pop(0)


class Writes(Scope):
    def __init__(self, enclosing=()):
        super(Writes, self).__init__(enclosing)
        self.writes = []

    def store(self, name, value, origin):
        self.writes.append((name, value))
        super(Writes, self).store(name, value, origin)


class Loader(object):
    def __init__(self, files):
        self.files = files

    def load(self, path):
        return Source(path, self.files[path])


class BatchTests(unittest.TestCase):
    def make(self, text, values=None, outcome=None, **kwargs):
        scope = Writes.top_level()
        scope.local.update(values or {})
        process = Recording(outcome)
        evaluator = Evaluator(scope, cwd='/recipe', process_backend=process,
            process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'), **kwargs)
        program = lower(parse(Source('/recipe/main.aap', text)))
        return evaluator, program, process

    def driver(self, text, loader=None, outcome=None, backend=None):
        data = Evaluator().run(lower(parse(Source('/recipe/main.aap', text))))
        process, persistence = backend or Recording(outcome), MemoryPersistence()
        runtime = PortRuntime(markers=MemoryMarkers(), commands=PortCommandRuntime(
            MemoryPortDirectories(['/recipe/sub'])))
        driver = BuildDriver(data.graph, MemoryTargetState(), persistence, data.scope,
            data.declarations, process_backend=process,
            process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'),
            port_runtime=runtime, port_defaults=False, include_loader=loader)
        return driver, process, persistence

    def test_two_plain_commands_one_request_and_resume(self):
        evaluator, program, process = self.make(':sys first\n:sys second\nAFTER = yes\n')
        result = evaluator.run(program)
        self.assertTrue(result.complete)
        self.assertEqual(len(process.requests), 1)
        request = process.requests[0]
        self.assertEqual(request.command, 'first\nsecond')
        self.assertEqual(request.shell_command, 'first\nsecond\n')
        self.assertEqual(request.shell_command_bytes, b'first\nsecond\n')
        self.assertEqual(request.echo_lines, ('first', 'second'))
        self.assertEqual((request.stdin_policy, request.stdout_policy, request.stderr_policy),
                         ('inherit', 'inherit', 'inherit'))
        self.assertEqual(request.environment_policy, 'inherit-backend')
        self.assertEqual(request.expression.kind, 'SEQUENCE')
        self.assertEqual([e.argv for e in request.expression.children], [('first',), ('second',)])
        self.assertIsNone(request.stages)
        self.assertEqual(evaluator.scope.local['AFTER'], 'yes')

    def test_three_entries_order_and_provenance(self):
        evaluator, program, process = self.make(':sys first\n:sys second\n:sys third\n')
        result = evaluator.run(program)
        request = process.requests[0]
        self.assertEqual([e.raw for e in request.entries], ['first', 'second', 'third'])
        self.assertEqual([e.expanded for e in request.entries], ['first', 'second', 'third'])
        self.assertEqual([e.span.start.line for e in request.entries], [1, 2, 3])
        self.assertEqual(request.span.start.line, 1)
        self.assertEqual(request.span.end.offset, len(program.source.text))
        self.assertIs(request.batch, result.processes[0].batch)
        self.assertEqual(request.batch.raw, 'first\nsecond\nthird\n')

    def test_status_written_once_in_local_scope(self):
        evaluator, program, process = self.make(':sys first\n:sys second\n', {'sysresult': 77})
        evaluator.run(program)
        self.assertEqual(evaluator.scope.writes, [('sysresult', 0)])

    def test_wildcard_batch_is_one_opaque_shell_request(self):
        evaluator, program, process = self.make(
            ':sys cp $DISTDIR/* $DEST\n:sys createrepo --pretty $DEST\n',
            {'DISTDIR': '/dist', 'DEST': '/rpm'})
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].shell_command,
                         'cp /dist/* /rpm\ncreaterepo --pretty /rpm\n')
        self.assertEqual(process.requests[0].cwd, '/recipe')
        self.assertEqual(evaluator.scope.writes, [('sysresult', 0)])

    def test_input_redirect_stays_in_its_batched_shell_line(self):
        evaluator, program, process = self.make(
            ':sys decode < $INPUT\n:sys report done\n', {'INPUT': 'archive/input'})
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].shell_command,
                         'decode < archive/input\nreport done\n')
        self.assertEqual(evaluator.scope.writes, [('sysresult', 0)])

    def test_entire_batch_expands_before_status_changes(self):
        evaluator, program, process = self.make(':sys inspect $sysresult\n:sys next $sysresult\n',
                                                 {'sysresult': 77})
        evaluator.run(program)
        self.assertEqual(process.requests[0].shell_command, 'inspect 77\nnext 77\n')
        self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_later_undefined_argument_prevents_all_submission(self):
        evaluator, program, process = self.make(':sys first\n:sys next $UNDEFINED\n')
        with self.assertRaises(SemanticError) as caught:
            evaluator.run(program)
        self.assertEqual(process.requests, [])
        self.assertEqual(caught.exception.span.start.line, 1)  # one aap_shell expansion
        self.assertNotIn('sysresult', evaluator.scope.local)

    def test_nonzero_final_status_updates_then_fails_at_first(self):
        evaluator, program, process = self.make(':sys first\n:sys second\nAFTER = no\n',
                                                 outcome=ProcessResult(512))
        with self.assertRaises(SemanticError) as caught:
            evaluator.run(program)
        self.assertEqual(caught.exception.span.start.line, 1)
        self.assertEqual(evaluator.scope.writes, [('sysresult', 512)])
        self.assertNotIn('AFTER', evaluator.scope.local)
        self.assertEqual(evaluator.last_result.processes[0].status, 'FAILED')

    def test_no_implicit_fail_fast_or_per_entry_status(self):
        evaluator, program, process = self.make(':sys false\n:sys true\n', outcome=ProcessResult(0))
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(process.requests[0].shell_command, 'false\ntrue\n')
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(evaluator.scope.local['sysresult'], 0)
        # This asserts the shell contract, not execution of false/true.

    def test_unavailable_backend_preserves_status_and_blocks(self):
        evaluator, program, process = self.make(':sys first\n:sys second\nAFTER = no\n',
            {'sysresult': 77}, ProcessUnavailable('unavailable'))
        with self.assertRaises(Unsupported): evaluator.run(program)
        self.assertEqual(evaluator.scope.local['sysresult'], 77)
        self.assertEqual(evaluator.last_result.processes[0].status, 'BLOCKED')
        self.assertNotIn('AFTER', evaluator.scope.local)

    def test_backend_errors_and_invalid_results_fail(self):
        for outcome in (ProcessBackendError('failed'), OSError('failed'),
                        ProcessResult(-1), ProcessResult(0, 'not bytes'), object()):
            evaluator, program, process = self.make(':sys first\n:sys second\n',
                                                      {'sysresult': 77}, outcome)
            with self.assertRaises(SemanticError): evaluator.run(program)
            self.assertEqual(evaluator.scope.local['sysresult'], 77)
            self.assertEqual(evaluator.last_result.processes[0].status, 'FAILED')

    def test_blank_lines_and_comments_do_not_flush(self):
        evaluator, program, process = self.make(':sys first # inline\n\n# comment\n\n:sys second\n')
        evaluator.run(program)
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].shell_command, 'first\nsecond\n')
        self.assertEqual([e.span.start.line for e in process.requests[0].entries], [1, 5])

    def test_assignment_flushes_before_mutation(self):
        evaluator, program, process = self.make(':sys inspect $X\nX = after\n:sys inspect $X\n', {'X': 'before'})
        evaluator.run(program)
        self.assertEqual([r.command for r in process.requests], ['inspect before', 'inspect after'])

    def test_cd_flushes_and_changed_cwd_propagates(self):
        runtime = PortRuntime(commands=PortCommandRuntime(MemoryPortDirectories(['/recipe/sub'])))
        evaluator, program, process = self.make(':sys first\n:cd sub\n:sys second\n:sys third\n', port_runtime=runtime)
        host = os.getcwd()
        evaluator.run(program)
        self.assertEqual([r.cwd for r in process.requests], ['/recipe', '/recipe/sub'])
        self.assertEqual(process.requests[1].shell_command, 'second\nthird\n')
        self.assertEqual(os.getcwd(), host)

    def test_continuations_fold_before_batching(self):
        evaluator, program, process = self.make(':sys first \\\n  --flag\n    continuation\n:sys second\n')
        evaluator.run(program)
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].entries[0].expression.argv,
                         ('first', '--flag', 'continuation'))
        self.assertEqual(process.requests[0].entries[1].span.start.line, 4)

    def test_deeper_sys_is_argument_text_not_another_batch_entry(self):
        evaluator, program, process = self.make(':sys first\n    :sys second\n:sys third\n')
        evaluator.run(program)
        self.assertEqual(len(process.requests[0].entries), 2)
        self.assertEqual(process.requests[0].entries[0].expression.argv, ('first', ':sys', 'second'))

    def test_quoted_spacing_and_operators_preserved(self):
        evaluator, program, process = self.make(':sys first  "a b" "||"\n:sys second \'#literal\'\n')
        evaluator.run(program)
        self.assertEqual(process.requests[0].shell_command, 'first  "a b" "||"\nsecond \'#literal\'\n')

    def test_pipeline_and_andor_entries_keep_existing_parser(self):
        evaluator, program, process = self.make(':sys producer | consumer\n:sys a || (b && c)\n')
        evaluator.run(program)
        self.assertEqual([e.expression.kind for e in process.requests[0].entries], ['PIPELINE', 'OR'])

    def test_no_new_shell_grammar_from_newline_batching(self):
        for text in ('a > file', 'a &', 'VAR=x a', 'a $$(host)', '`host()`'):
            evaluator, program, process = self.make(':sys first\n:sys ' + text + '\n')
            with self.assertRaises((Unsupported, SemanticError)): evaluator.run(program)
            self.assertEqual(process.requests, [])

    def test_unenabled_attributes_gate_at_historical_force_boundary(self):
        for left, right in (('{f} first', 'second'),
                            ('{f} first', '{f} second'), ('{q} first', '{q} second'),
                            ('{q} first', '{l} second'), ('first', '{f}{q}{l} second')):
            evaluator, program, process = self.make(':sys ' + left + '\n:sys ' + right + '\n',
                                                     {'sysresult': 77})
            with self.assertRaises(Unsupported): evaluator.run(program)
            self.assertEqual(len(process.requests), 1 if left == 'first' else 0)
            self.assertEqual(evaluator.scope.local['sysresult'],
                             0 if left == 'first' else 77)

    def test_expanded_options_cannot_bypass_gate(self):
        evaluator, program, process = self.make(':sys first\n:sys $OPTION second\n', {'OPTION': '{q}'})
        with self.assertRaises(Unsupported): evaluator.run(program)
        self.assertEqual(process.requests, [])

    def test_system_prefix_gate(self):
        evaluator, program, process = self.make(':sys first\n:system second\n')
        with self.assertRaises(Unsupported): evaluator.run(program)
        self.assertEqual(process.requests, [])
        evaluator, program, process = self.make(':system first\n:sys second\n')
        self.assertFalse(evaluator.run(program).complete)
        self.assertEqual(process.requests, [])

    def test_pending_sys_runs_after_separate_syseval_capture(self):
        evaluator, program, unused = self.make(':sys first $x\n:sys second\n'
            ':syseval capture | :assign x\nAFTER = yes\n', {'sysresult': 99})
        process = SequencedRecording((ProcessResult(0, b'captured\n'), ProcessResult(0)))
        evaluator.process.backend = process
        result = evaluator.run(program)
        self.assertTrue(result.complete)
        self.assertEqual(len(process.requests), 2)
        self.assertTrue(process.requests[0].capture_stdout)
        self.assertEqual(process.requests[0].command, 'capture')
        self.assertEqual(process.requests[1].shell_command, 'first captured\nsecond\n')
        self.assertFalse(process.requests[1].capture_stdout)
        self.assertEqual(evaluator.scope.local['x'], 'captured')
        self.assertEqual(evaluator.scope.local['exit'], 0)
        self.assertEqual(evaluator.scope.local['sysresult'], 0)
        self.assertEqual([r.request for r in result.processes], process.requests)
        self.assertEqual(evaluator.scope.local['AFTER'], 'yes')

    def test_dhcppxe_physical_command_sequence_in_dependency_body(self):
        source = ('PKGDIR = /recipe/pack\n'
            'fake-install:\n'
            '\t:sys chown -Rh root:root $PKGDIR\n'
            '        :syseval /usr/local/nacharbeiten/na_bin/getdistro -v | :assign rel\n'
            '        @if string.find(rel, "SUSE15") >= 0:\n'
            '        \t:pass\n')
        process = SequencedRecording((ProcessResult(0, b'SUSE15 6\n'), ProcessResult(0)))
        driver, unused, unused_state = self.driver(source, backend=process)
        result = driver.build('fake-install')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(len(process.requests), 2)
        self.assertEqual(process.requests[0].command,
                         '/usr/local/nacharbeiten/na_bin/getdistro -v')
        self.assertEqual(process.requests[1].shell_command,
                         'chown -Rh root:root /recipe/pack\n')
        self.assertEqual([r.span.start.line for r in process.requests], [4, 3])
        body = result.bodies[0]
        self.assertEqual(body.scope.local['rel'], 'SUSE15 6')
        self.assertEqual(body.scope.local['exit'], 0)
        self.assertEqual(body.scope.local['sysresult'], 0)

    def test_blank_comment_between_sys_and_syseval_do_not_flush(self):
        evaluator, program, unused = self.make(':sys before $rel\n\n# comment\n'
            ':syseval capture | :assign rel\nAFTER = yes\n')
        process = SequencedRecording((ProcessResult(0, b'value\n'), ProcessResult(0)))
        evaluator.process.backend = process
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual([r.command for r in process.requests], ['capture', 'before value'])

    def test_failed_pending_sys_follows_capture_then_stops(self):
        evaluator, program, unused = self.make(':sys before\n'
            ':syseval capture | :assign rel\nAFTER = no\n')
        process = SequencedRecording((ProcessResult(0, b'value\n'), ProcessResult(256)))
        evaluator.process.backend = process
        with self.assertRaises(SemanticError): evaluator.run(program)
        self.assertEqual([r.command for r in process.requests], ['capture', 'before'])
        self.assertEqual(evaluator.scope.local['rel'], 'value')
        self.assertEqual(evaluator.scope.local['exit'], 0)
        self.assertEqual(evaluator.scope.local['sysresult'], 256)
        self.assertNotIn('AFTER', evaluator.scope.local)

    def test_failed_syseval_does_not_stop_pending_sys(self):
        evaluator, program, unused = self.make(':sys after $rel\n'
            ':syseval capture | :assign rel\nAFTER = yes\n')
        process = SequencedRecording((ProcessResult(256, b'value\n'), ProcessResult(0)))
        evaluator.process.backend = process
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual([r.command for r in process.requests], ['capture', 'after value'])
        self.assertEqual(evaluator.scope.local['exit'], 256)
        self.assertEqual(evaluator.scope.local['sysresult'], 0)
        self.assertEqual(evaluator.scope.local['AFTER'], 'yes')

    def test_sys_after_pending_syseval_remains_gated(self):
        evaluator, program, process = self.make(':sys first\n'
            ':syseval capture | :assign x\n:sys second\n')
        with self.assertRaises(Unsupported): evaluator.run(program)
        self.assertEqual(process.requests, [])
        self.assertNotIn('x', evaluator.scope.local)

    def test_syseval_first_keeps_capture_then_plain_batch(self):
        evaluator, program, process = self.make(':syseval capture | :assign x\n:sys echo $x\n:sys last\n')
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(process.requests), 2)
        self.assertTrue(process.requests[0].capture_stdout)
        self.assertEqual(process.requests[1].shell_command, 'echo capture\nlast\n')
        self.assertNotIn(':assign', process.requests[1].shell_command)

    def test_include_is_a_boundary_not_inline_batch_text(self):
        loader = Loader({'/recipe/part.aap': ':sys middle\n:sys other\n'})
        evaluator, program, process = self.make(':sys first\n:include part.aap\n:sys last\n', include_loader=loader)
        evaluator.run(program)
        self.assertEqual([r.command for r in process.requests], ['first', 'middle\nother', 'last'])
        self.assertEqual(process.requests[1].entries[0].span.source_id, '/recipe/part.aap')

    def test_control_suite_and_dedent_flush_batches(self):
        evaluator, program, process = self.make(':sys first\n@if 1:\n  :sys second\n  :sys third\n:sys last\n')
        evaluator.run(program)
        self.assertEqual([r.command for r in process.requests], ['first', 'second\nthird', 'last'])

    def test_nested_body_entries_consumed_once_and_caller_resumes(self):
        driver, process, saved = self.driver('all:\n  :sys before\n  :update child\n  :sys after\n'
            'child {virtual}:\n  :cd sub\n  :sys one\n  :sys two\n  X = complete\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([r.command for r in process.requests], ['before', 'one\ntwo', 'after'])
        self.assertEqual([r.cwd for r in process.requests], ['/recipe', '/recipe/sub', '/recipe'])
        child = result.bodies[0].updates[0].builds[0].bodies[0]
        self.assertEqual(child.scope.local['X'], 'complete')
        self.assertEqual(child.scope.local['sysresult'], 0)
        self.assertEqual(saved.writes, [])

    def test_nested_batch_failure_preserves_invocation_and_first_cause(self):
        driver, process, saved = self.driver('all:\n  :update child\n  X = no\n'
            'child {virtual}:\n  :sys one\n  :sys two\n', outcome=ProcessResult(256))
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        update = result.bodies[0].update_failure
        self.assertEqual(update.request.span.start.line, 2)
        self.assertEqual(update.span.start.line, 5)
        self.assertEqual(saved.writes, [])
        self.assertEqual(driver.completed, {})

    def test_success_does_not_establish_directory_or_artifact_effect(self):
        driver, process, saved = self.driver('all:\n  :sys mkdir unknown\n  :sys chown owner unknown\n  :cd unknown\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(driver.completed, {})
        self.assertEqual(driver.port_runtime.markers.files, {})


if __name__ == '__main__':
    unittest.main()
