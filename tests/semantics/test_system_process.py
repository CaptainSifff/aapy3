"""Source-derived Commands.aap_shell / Util.logged_system unlogged branch.

All requests go to recording backends. No shell is launched. Rectests012/013
exercise compiler actions historically; their build drivers are not executed.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, ProcessBackend, ProcessPolicy,
    ProcessResult, ProcessBackendError, ProcessUnavailable, SemanticError, Unsupported)
from aap_semantics.system_process import (literal_shell, MAX_GROUP_DEPTH,
    ShellRedirection)


class RecordingProcess(ProcessBackend):
    def __init__(self, outcome=None):
        self.outcome = ProcessResult(0) if outcome is None else outcome
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class SystemTests(unittest.TestCase):
    def setup_case(self, text, values=None, outcome=None, encoding='utf-8'):
        scope = Scope.top_level()
        scope.local.update(values or {})
        backend = RecordingProcess(outcome)
        evaluator = Evaluator(scope, cwd='/explicit/work', process_backend=backend,
                              process_policy=ProcessPolicy(encoding, sys_mode='unlogged'))
        program = lower(parse(Source('/definition/action.aap', text)))
        return evaluator, program, backend

    def shell(self, command):
        origin = lower(parse(Source('shell.aap', ':sys placeholder\n'))).statements[0]
        return literal_shell(command, origin)

    def test_simple_command_request_contract(self):
        evaluator, program, backend = self.setup_case(':sys inspect --flag input\nAFTER = yes\n')
        result = evaluator.run(program)
        self.assertTrue(result.complete)
        request = backend.requests[0]
        self.assertEqual(request.stages, (('inspect', '--flag', 'input'),))
        self.assertEqual(request.command, 'inspect --flag input')
        self.assertEqual(request.shell_command_bytes, b'inspect --flag input\n')
        self.assertTrue(request.shell_required)
        self.assertEqual(request.shell_mode, 'posix-sh')
        self.assertFalse(request.capture_stdout)
        self.assertEqual((request.stdin_policy, request.stdout_policy, request.stderr_policy),
                         ('inherit', 'inherit', 'inherit'))
        self.assertEqual(request.environment_policy, 'inherit-backend')
        self.assertIsNone(request.environment)
        self.assertEqual(request.cwd, '/explicit/work')
        self.assertEqual(request.cwd_bytes, b'/explicit/work')
        self.assertEqual(request.span.source_id, '/definition/action.aap')
        self.assertEqual(request.span.start.line, 1)
        self.assertTrue(request.echo)
        self.assertEqual(request.echo_text, request.shell_command)
        self.assertEqual(evaluator.scope.local['sysresult'], 0)
        self.assertEqual(evaluator.scope.local['AFTER'], 'yes')

    def test_item_quoted_python_launcher_with_space_in_both_paths(self):
        launcher = ('"/work/launcher space/python3" '
                    '"/work/entry space/aap_wrapper.py"')
        evaluator, program, backend = self.setup_case(':sys $AAP rpm\n',
                                                      {'AAP': launcher})
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(backend.requests[0].command, launcher + ' rpm')
        self.assertEqual(backend.requests[0].stages,
                         (('/work/launcher space/python3',
                           '/work/entry space/aap_wrapper.py', 'rpm'),))
        self.assertEqual(backend.requests[0].cwd, '/explicit/work')

    def test_unclosed_item_quoted_launcher_fails_before_process_request(self):
        evaluator, program, backend = self.setup_case(
            ':sys $AAP rpm\n', {'AAP': '"/work/launcher space/python3'})
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertEqual(backend.requests, [])

    def test_pipeline_is_shell_structure_not_aap_pipeline(self):
        evaluator, program, backend = self.setup_case(':sys decode -dc $source | unpack xf -\n',
                                                       {'source': '/archives/file.tgz'})
        evaluator.run(program)
        self.assertEqual(backend.requests[0].stages,
                         (('decode', '-dc', '/archives/file.tgz'), ('unpack', 'xf', '-')))
        self.assertEqual(backend.requests[0].command, 'decode -dc /archives/file.tgz | unpack xf -')

    def test_nonzero_wait_status_stored_before_failure(self):
        evaluator, program, backend = self.setup_case(':sys failing\nAFTER = no\n',
            {'exit': 99}, ProcessResult(256, b'partial\n', b'error\n'))
        with self.assertRaises(SemanticError) as caught:
            evaluator.run(program)
        self.assertEqual(evaluator.scope.local['sysresult'], 256)
        self.assertEqual(evaluator.scope.local['exit'], 99)
        self.assertNotIn('AFTER', evaluator.scope.local)
        self.assertEqual(caught.exception.span.source_id, '/definition/action.aap')
        record = evaluator.last_result.processes[0]
        self.assertEqual(record.status, 'FAILED')
        self.assertEqual(record.result.stdout, b'partial\n')
        self.assertEqual(record.result.stderr, b'error\n')

    def test_pipeline_uses_backend_shell_status_not_first_member(self):
        # POSIX final-member status, no invented pipefail. This is a contract
        # observation, not a claim that a shell was executed in this test.
        evaluator, program, backend = self.setup_case(':sys producer | consumer\n', outcome=ProcessResult(0))
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_unavailable_capability_blocks(self):
        for outcome in (ProcessUnavailable('no launcher'), NotImplementedError('not authorized')):
            evaluator, program, backend = self.setup_case(':sys inspect\n', outcome=outcome)
            with self.assertRaises(Unsupported): evaluator.run(program)
            self.assertEqual(evaluator.last_result.processes[0].status, 'BLOCKED')
            self.assertNotIn('sysresult', evaluator.scope.local)

    def test_backend_error_fails_and_preserves_previous_status(self):
        for outcome in (ProcessBackendError('launch failed'), OSError('launch failed'), ValueError('invalid backend')):
            evaluator, program, backend = self.setup_case(':sys inspect\n', {'sysresult': 7}, outcome)
            with self.assertRaises(SemanticError): evaluator.run(program)
            self.assertEqual(evaluator.last_result.processes[0].status, 'FAILED')
            self.assertEqual(evaluator.scope.local['sysresult'], 7)

    def test_stream_observations_are_exact_bytes_not_capture_values(self):
        evaluator, program, backend = self.setup_case(':sys inspect\n',
            outcome=ProcessResult(0, b' \xff\x00\n\n', b' stderr\n'))
        record = evaluator.run(program).processes[0]
        self.assertEqual(record.result.stdout, b' \xff\x00\n\n')
        self.assertEqual(record.result.stderr, b' stderr\n')
        self.assertIsNone(record.output)
        self.assertNotIn('exit', evaluator.scope.local)

    def test_aap_item_quoting_and_attribute_removal(self):
        evaluator, program, backend = self.setup_case(':sys inspect $source\n',
            {'source': '"/archive/a b.tgz"{distdir=remote}{extractdir=work} other.tgz'})
        evaluator.run(program)
        self.assertEqual(backend.requests[0].command, 'inspect "/archive/a b.tgz" other.tgz')
        self.assertEqual(backend.requests[0].stages,
                         (('inspect', '/archive/a b.tgz', 'other.tgz'),))
        self.assertIn('{distdir=', evaluator.scope.local['source'])

    def test_literal_quotes_and_escapes_preserve_argv_boundaries(self):
        evaluator, program, backend = self.setup_case(':sys inspect "a b" \'c|d\' e\\ f ""\n')
        evaluator.run(program)
        self.assertEqual(backend.requests[0].stages, (('inspect', 'a b', 'c|d', 'e f', ''),))

    def test_expansion_inside_quotes_is_historical_not_python_formatting(self):
        evaluator, program, backend = self.setup_case(':sys inspect "$X"\n', {'X': 'one two'})
        evaluator.run(program)
        self.assertEqual(backend.requests[0].stages, (('inspect', 'one two'),))

    def test_shell_metacharacter_from_value_keeps_historical_quote_behavior(self):
        evaluator, program, backend = self.setup_case(':sys inspect $X\n', {'X': 'a|b'})
        evaluator.run(program)
        self.assertEqual(backend.requests[0].command, 'inspect "a|b"')
        self.assertEqual(backend.requests[0].stages, (('inspect', 'a|b'),))
        # Historical trailing | is unquoted; do not silently shell-escape it.
        evaluator, program, backend = self.setup_case(':sys producer $X consumer\n', {'X': '|'})
        evaluator.run(program)
        self.assertEqual(backend.requests[0].stages, (('producer',), ('consumer',)))

    def test_wildcard_arguments_pass_through_to_shell(self):
        # Commands.aap_shell expands first; Util.logged_system passes the
        # resulting unlogged command to os.system without A-A-P globbing.
        evaluator, program, backend = self.setup_case(
            ':sys cp *.rpm /packages\n', outcome=ProcessResult(0))
        self.assertTrue(evaluator.run(program).complete)
        request = backend.requests[0]
        self.assertEqual(request.command, 'cp *.rpm /packages')
        self.assertEqual(request.shell_command_bytes, b'cp *.rpm /packages\n')
        self.assertEqual(request.stages, (('cp', '*.rpm', '/packages'),))
        self.assertEqual(request.cwd, '/explicit/work')
        self.assertEqual(evaluator.scope.local['sysresult'], 0)
        self.assertEqual(len(backend.requests), 1)

    def test_wildcard_from_aap_variable_is_not_globbed_or_escaped(self):
        evaluator, program, backend = self.setup_case(
            ':sys cp $DISTDIR/* $DEST\n',
            {'DISTDIR': '/dist', 'DEST': '/packages'})
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(backend.requests[0].command, 'cp /dist/* /packages')
        self.assertEqual(backend.requests[0].stages,
                         (('cp', '/dist/*', '/packages'),))

    def test_quoted_escaped_and_other_posix_patterns_preserve_spelling(self):
        commands = ('inspect "*.rpm"', "inspect '*.rpm'", 'inspect \\*.rpm',
                    'inspect file?.rpm', 'inspect file[abc].rpm')
        for command in commands:
            evaluator, program, backend = self.setup_case(':sys ' + command + '\n')
            self.assertTrue(evaluator.run(program).complete, command)
            self.assertEqual(backend.requests[0].command, command)
            self.assertEqual(backend.requests[0].shell_command, command + '\n')
            self.assertEqual(len(backend.requests), 1)

    def test_wildcard_in_supported_pipeline_keeps_original_command(self):
        evaluator, program, backend = self.setup_case(':sys list *.rpm | inspect file?.rpm\n')
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(backend.requests[0].command,
                         'list *.rpm | inspect file?.rpm')
        self.assertEqual(backend.requests[0].stages,
                         (('list', '*.rpm'), ('inspect', 'file?.rpm')))

    def test_no_namespace_export(self):
        evaluator, program, backend = self.setup_case(':sys inspect\n', {'PATH': '/recipe', 'SECRET': 'local'})
        evaluator.run(program)
        self.assertIsNone(backend.requests[0].environment)

    def test_reject_unsupported_forms_before_effects(self):
        forms = ('inspect > output', 'inspect 2>&1',
                 'inspect &', 'X=value inspect', 'cd other', 'env X=y inspect',
                 'sh -c "inspect"', '[ -f file ]', 'inspect ~', 'inspect $$HOME',
                 'inspect $$(nested)', 'inspect | :assign x', '{q} inspect',
                 'inspect $+source', 'inspect `1`')
        for form in forms:
            evaluator, program, backend = self.setup_case(':sys ' + form + '\n', {'source': 'x'})
            with self.assertRaises((Unsupported, SemanticError)):
                evaluator.run(program)
            self.assertEqual(backend.requests, [], form)

    def test_input_redirection_is_shell_passthrough_without_file_observation(self):
        forms = ('inspect < input', 'inspect <input',
                 'inspect < "input name"', 'inspect < input\\ name')
        for command in forms:
            evaluator, program, backend = self.setup_case(':sys ' + command + '\n')
            self.assertTrue(evaluator.run(program).complete, command)
            request = backend.requests[0]
            self.assertEqual(request.command, command)
            self.assertEqual(request.shell_command, command + '\n')
            self.assertEqual(request.stages, (('inspect',),))
            self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_input_redirect_expansion_and_required_doperlmod_output_tail(self):
        evaluator, program, backend = self.setup_case(
            ':sys sed -e x < $name > $(name).new\n', {'name': 'pack/list'})
        self.assertTrue(evaluator.run(program).complete)
        request = backend.requests[0]
        self.assertEqual(request.command, 'sed -e x < pack/list > pack/list.new')
        self.assertEqual(request.stages, (('sed', '-e', 'x'),))
        self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_input_redirection_composes_with_existing_shell_forms(self):
        evaluator, program, backend = self.setup_case(
            ':sys decode < source | consume && report\n')
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(backend.requests[0].command, 'decode < source | consume && report')
        self.assertEqual(backend.requests[0].expression.kind, 'AND')

    def test_append_redirection_is_structured_shell_passthrough(self):
        cases = ('cat src >> dst', 'cat src>>dst',
                 'cat src >> "dst file"', 'cat "src file" >> dst')
        for command in cases:
            expression = self.shell(command)
            self.assertEqual(expression.kind, 'SIMPLE')
            self.assertEqual(expression.redirections[0].operator, '>>')
            self.assertEqual(expression.redirections[0].target,
                'dst file' if '"dst file"' in command else 'dst')
        self.assertEqual(self.shell('echo ">>"').argv, ('echo', '>>'))
        self.assertEqual(self.shell(r'echo \>\>').argv, ('echo', '>>'))

    def test_append_redirection_request_keeps_expanded_shell_text_and_cwd(self):
        evaluator, program, backend = self.setup_case(
            ':sys cat $script >> $file\n',
            {'script': 'work/unpost_i', 'file': 'work/sec.spec'})
        self.assertTrue(evaluator.run(program).complete)
        request = backend.requests[0]
        self.assertEqual(request.command, 'cat work/unpost_i >> work/sec.spec')
        self.assertEqual(request.shell_command, 'cat work/unpost_i >> work/sec.spec\n')
        self.assertEqual(request.shell_command_bytes,
                         b'cat work/unpost_i >> work/sec.spec\n')
        self.assertTrue(request.shell_required)
        self.assertEqual(request.shell_mode, 'posix-sh')
        self.assertEqual(request.cwd, '/explicit/work')
        self.assertIsNone(request.environment)
        self.assertEqual(request.environment_policy, 'inherit-backend')
        self.assertEqual((request.stdin_policy, request.stdout_policy,
                          request.stderr_policy), ('inherit', 'inherit', 'inherit'))
        self.assertEqual(request.span.source_id, '/definition/action.aap')
        self.assertEqual(request.span.start.line, 1)
        self.assertEqual(request.expression.redirections,
                         (ShellRedirection('>>', 'work/sec.spec'),))

    def test_append_target_with_space_uses_existing_aap_shell_quoting(self):
        evaluator, program, backend = self.setup_case(
            ':sys cat $script >> $file\n',
            {'script': 'work/unpost_i', 'file': '"work/sec spec"'})
        self.assertTrue(evaluator.run(program).complete)
        request = backend.requests[0]
        self.assertEqual(request.command,
                         'cat work/unpost_i >> "work/sec spec"')
        self.assertEqual(request.expression.redirections[0].target,
                         'work/sec spec')

    def test_append_redirection_does_not_split_adjacent_sys_batch(self):
        evaluator, program, backend = self.setup_case(
            ':sys first\n:sys cat src >> dst\n:sys third\n')
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.requests[0].command,
                         'first\ncat src >> dst\nthird')
        self.assertEqual(backend.requests[0].expression.kind, 'SEQUENCE')

    def test_append_redirection_malformed_or_wider_forms_stay_gated(self):
        for command in ('cat src >>', '>> dst', 'cat src >> dst >> other',
                        'cat src >> dst > other', 'cat src << input'):
            with self.assertRaises((Unsupported, SemanticError)):
                self.shell(command)

    def test_wider_redirection_forms_remain_gated_before_effects(self):
        forms = ('inspect > output', 'inspect << input',
                 'inspect <<< input', 'inspect 2< input', 'inspect < input 2>&1',
                 'inspect < input > output > again')
        for form in forms:
            evaluator, program, backend = self.setup_case(':sys ' + form + '\n')
            with self.assertRaises((Unsupported, SemanticError)):
                evaluator.run(program)
            self.assertEqual(backend.requests, [], form)

    def test_adjacent_system_alias_remains_gated_before_first_process(self):
        evaluator, program, backend = self.setup_case(':sys first\n:system next\n')
        with self.assertRaises(Unsupported): evaluator.run(program)
        self.assertEqual(backend.requests, [])

    def test_separated_commands_overwrite_sysresult(self):
        evaluator, program, backend = self.setup_case(':sys one\nBETWEEN = yes\n:sys two\n')
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_indented_arguments_join_as_one_command(self):
        evaluator, program, backend = self.setup_case(':sys inspect\n    --flag\n')
        evaluator.run(program)
        self.assertEqual(backend.requests[0].stages, (('inspect', '--flag'),))

    def test_undefined_and_deferred_values_stop_before_backend(self):
        evaluator, program, backend = self.setup_case(':sys inspect $missing\n')
        with self.assertRaises(SemanticError): evaluator.run(program)
        self.assertEqual(backend.requests, [])

    def test_explicit_process_mode_required(self):
        evaluator, program, backend = self.setup_case(':sys inspect\n')
        evaluator.process.policy = ProcessPolicy('utf-8')
        self.assertFalse(evaluator.run(program).complete)
        self.assertEqual(backend.requests, [])
        with self.assertRaises(ValueError): ProcessPolicy('utf-8', sys_mode='logged')

    def test_async_and_bad_process_observations_are_rejected(self):
        evaluator, program, backend = self.setup_case(':sys inspect\n', {'async': 1})
        with self.assertRaises(Unsupported): evaluator.run(program)
        self.assertEqual(backend.requests, [])
        for outcome in (ProcessResult(-1), ProcessResult(True), ProcessResult(0, 'text')):
            evaluator, program, backend = self.setup_case(':sys inspect\n', outcome=outcome)
            with self.assertRaises(SemanticError): evaluator.run(program)

    def test_encoding_is_explicit_and_strict(self):
        evaluator, program, backend = self.setup_case(':sys inspect é\n', encoding='latin-1')
        evaluator.run(program)
        self.assertEqual(backend.requests[0].command_bytes, b'inspect \xe9')
        evaluator, program, backend = self.setup_case(':sys inspect é\n', encoding='ascii')
        with self.assertRaises(SemanticError): evaluator.run(program)
        self.assertEqual(backend.requests, [])


def simple(name):
    return ('SIMPLE', (name,), (), ())


class ShellListTests(unittest.TestCase):
    setup_case = SystemTests.setup_case

    def setUp(self):
        self.origin = lower(parse(Source('shell.aap', ':sys placeholder\n'))).statements[0]

    def shell(self, command):
        return literal_shell(command, self.origin)

    def test_or(self):
        self.assertEqual(self.shell('a || b'), ('OR', (), (simple('a'), simple('b')), ()))

    def test_and(self):
        self.assertEqual(self.shell('a && b'), ('AND', (), (simple('a'), simple('b')), ()))

    def test_grouped_and_in_or(self):
        self.assertEqual(self.shell('a || (b && c)'),
            ('OR', (), (simple('a'), ('GROUP', (), (('AND', (), (simple('b'), simple('c')), ()),), ())), ()))

    def test_equal_precedence_left_associative_not_python_boolean_precedence(self):
        self.assertEqual(self.shell('a || b && c'),
            ('AND', (), (('OR', (), (simple('a'), simple('b')), ()), simple('c')), ()))
        self.assertEqual(self.shell('a && b || c'),
            ('OR', (), (('AND', (), (simple('a'), simple('b')), ()), simple('c')), ()))

    def test_pipeline_binds_tighter_and_groups_stay_intact(self):
        self.assertEqual(self.shell('a|b && (c||d)|e'), ('AND', (), (
            ('PIPELINE', (), (simple('a'), simple('b')), ()),
            ('PIPELINE', (), (('GROUP', (), (('OR', (), (simple('c'), simple('d')), ()),), ()), simple('e')), ())), ()))

    def test_nested_groups_and_explicit_depth_gate(self):
        self.assertEqual(self.shell('(a || (b && c))').children[0].kind, 'OR')
        self.shell('( ' * MAX_GROUP_DEPTH + 'a' + ' )' * MAX_GROUP_DEPTH)
        with self.assertRaises(Unsupported):
            self.shell('( ' * (MAX_GROUP_DEPTH + 1) + 'a' + ' )' * (MAX_GROUP_DEPTH + 1))
        with self.assertRaises(Unsupported): self.shell('((a))')

    def test_quoted_and_escaped_operators_are_argv(self):
        self.assertEqual(self.shell('a "&&" \'||\' "(" \')\' x"||"y \\&\\&'),
                         ('SIMPLE', ('a', '&&', '||', '(', ')', 'x||y', '&&'), (), ()))

    def test_malformed_structure_fails_before_any_backend(self):
        for command in ('a ||', 'a &&', 'a |', '|| a', '&& a', '()', '( )',
                        '(a', 'a)', 'a || ()', 'a || (b &&)', '(a) b', 'a (b)',
                        'a ||| b', 'a && || b', '(a)(b)', 'a | | b'):
            evaluator, program, backend = self.setup_case(':sys ' + command + '\n')
            with self.assertRaises(SemanticError): evaluator.run(program)
            self.assertEqual(backend.requests, [], command)

    def test_deferred_shell_features_remain_gated(self):
        for command in ('a & b', 'a > file', 'VAR=x a',
                        'a || VAR=x b', 'a || cd x', '$(host-command)', '`host-command`',
                        'a $HOME', 'a ~', 'a # comment', '[ -f file ]', '{ a; }',
                        'a &&& b', 'a\nb', 'a\x00b', 'a || sh -c b'):
            with self.assertRaises((Unsupported, SemanticError)): self.shell(command)

    def test_input_redirection_stays_shell_text_in_list_expression(self):
        expression = self.shell('a < input | b && c')
        self.assertEqual(expression.kind, 'AND')
        self.assertEqual(expression.children[0].kind, 'PIPELINE')

    def test_aap_expansion_precedes_shell_structure(self):
        text = ('CLEAR = echo clear\nY = echo install\nd = package\n'
                ':sys rpm -q $d || ( $(CLEAR) && $Y $d )\nAFTER = yes\n')
        evaluator, program, backend = self.setup_case(text)
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        self.assertEqual(request.command, 'rpm -q package || ( echo clear && echo install package )')
        self.assertEqual(request.expression.children[1].children[0].children[0].argv, ('echo', 'clear'))
        self.assertIsNone(request.stages)  # no misleading flattened argv list
        self.assertEqual(request.shell_command_bytes, request.command.encode('utf-8') + b'\n')
        self.assertEqual(evaluator.scope.local['sysresult'], 0)
        self.assertEqual(evaluator.scope.local['AFTER'], 'yes')

    def test_single_character_parenthesized_escape_is_historical_literal(self):
        # Commands.expand 1554: $(x) reduces to x, unlike $(CLEARCACHE).
        evaluator, program, backend = self.setup_case(
            'X = echo clear\nY = echo install\nd = package\n'
            ':sys rpm -q $d || ( $(X) && $Y $d )\n')
        evaluator.run(program)
        self.assertEqual(backend.requests[0].command, 'rpm -q package || ( X && echo install package )')

    def test_expansion_cannot_enable_shell_substitution(self):
        for value in ('$(host-command)', '`host-command`', '$HOME'):
            evaluator, program, backend = self.setup_case(':sys a || b $X\n', {'X': value})
            with self.assertRaises(Unsupported): evaluator.run(program)
            self.assertEqual(backend.requests, [])
        for form in ('a || b $$(host-command)', 'a || b `1`'):
            evaluator, program, backend = self.setup_case(':sys ' + form + '\n')
            with self.assertRaises(Unsupported): evaluator.run(program)
            self.assertEqual(backend.requests, [])

    def test_compound_request_preserves_contract_and_exact_streams(self):
        command = 'a|| ( b && c|d )'
        evaluator, program, backend = self.setup_case(':sys ' + command + '\n',
            {'PATH': '/recipe-only'}, ProcessResult(0, b'\xff\n\n', b' stderr\n'))
        record = evaluator.run(program).processes[0]
        request = backend.requests[0]
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(request.command, command)
        self.assertTrue(request.shell_required)
        self.assertEqual(request.shell_mode, 'posix-sh')
        self.assertEqual(request.cwd, '/explicit/work')
        self.assertEqual(evaluator.cwd, '/explicit/work')
        self.assertIsNone(request.environment)
        self.assertEqual((request.stdin_policy, request.stdout_policy, request.stderr_policy), ('inherit',) * 3)
        self.assertFalse(request.capture_stdout)
        self.assertFalse(request.logging)
        self.assertEqual(record.result.stdout, b'\xff\n\n')
        self.assertEqual(record.result.stderr, b' stderr\n')
        self.assertIsNone(record.output)

    def test_compound_final_wait_status_is_stored_before_failure(self):
        evaluator, program, backend = self.setup_case(':sys a || (b && c)\nAFTER = no\n',
            outcome=ProcessResult(256))
        with self.assertRaises(SemanticError) as error: evaluator.run(program)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(evaluator.scope.local['sysresult'], 256)
        self.assertNotIn('AFTER', evaluator.scope.local)
        self.assertEqual(error.exception.span.start.line, 1)
        self.assertEqual(error.exception.span.source_id, '/definition/action.aap')

    def test_compound_unavailable_error_invalid_results(self):
        for outcome, status in ((ProcessUnavailable('no capability'), 'BLOCKED'),
                                (ProcessBackendError('broken'), 'FAILED'),
                                (OSError('broken'), 'FAILED'), (ProcessResult(-1), 'FAILED'),
                                (ProcessResult(0, 'text'), 'FAILED'), (False, 'FAILED')):
            evaluator, program, backend = self.setup_case(':sys a || b\nAFTER = no\n',
                                                         {'sysresult': 77}, outcome)
            with self.assertRaises(SemanticError): evaluator.run(program)
            self.assertEqual(evaluator.last_result.processes[0].status, status)
            self.assertEqual(evaluator.scope.local['sysresult'], 77)
            self.assertNotIn('AFTER', evaluator.scope.local)
            self.assertEqual(len(backend.requests), 1)


if __name__ == '__main__':
    unittest.main()
