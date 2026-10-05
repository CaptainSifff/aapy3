"""Reached shell-local cd in a parenthesized synchronous :sys group."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, ProcessBackend, ProcessPolicy,
                           ProcessResult, Scope, SemanticError, Unsupported,
                           lower)
from aap_semantics.system_process import literal_shell
from process_adapter import PosixProcessBackend


class Recording(ProcessBackend):
    def __init__(self, status=0):
        self.status = status
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return ProcessResult(self.status)


def evaluate(source, cwd, backend):
    scope = Scope.top_level()
    policy = ProcessPolicy('latin-1', sys_mode='bounded-attributes',
                           log_path=os.path.join(cwd, 'AAPDIR', 'log'))
    evaluator = Evaluator(scope, cwd=cwd, process_backend=backend,
                          process_policy=policy)
    program = lower(parse(Source('shell-cd.aap', source)))
    return evaluator, program


def expression(command):
    origin = lower(parse(Source('shell-cd.aap', ':sys placeholder\n'))).statements[0]
    return literal_shell(command, origin)


class ShellGroupCdTests(unittest.TestCase):
    def test_group_only_builtin_structure(self):
        for spelling in ('(cd x)', '( cd x )', '( cd "x y" ; true )'):
            tree = expression(spelling)
            self.assertEqual(tree.kind, 'GROUP')
            child = tree.children[0]
            builtin = child.children[0] if child.kind == 'SEQUENCE' else child
            self.assertEqual(builtin.kind, 'BUILTIN')
            self.assertEqual(builtin.argv[0], 'cd')
        self.assertEqual(expression('( cd "x y" ; true )').children[0]
                         .children[0].argv, ('cd', 'x y'))

    def test_pipeline_group_sequence_structure(self):
        tree = expression('producer | ( cd x ; consumer )')
        self.assertEqual(tree.kind, 'PIPELINE')
        self.assertEqual(tree.children[0].kind, 'SIMPLE')
        self.assertEqual(tree.children[1].kind, 'GROUP')
        sequence = tree.children[1].children[0]
        self.assertEqual(sequence.kind, 'SEQUENCE')
        self.assertEqual([child.kind for child in sequence.children],
                         ['BUILTIN', 'SIMPLE'])

    def test_outside_group_and_unreached_cd_forms_stay_gated(self):
        for command in ('cd x', 'cd x ; command', '(cd)', '(cd a b)',
                        '(cd - ; command)', '(cd -- x)', '(cd -P x)',
                        '(/bin/cd x ; command)', '(env cd x ; command)',
                        '(if true ; command)', '(for x ; command)'):
            with self.subTest(command=command):
                with self.assertRaises(Unsupported):
                    expression(command)

    def test_recorded_request_keeps_aap_cwd_and_exact_shell_text(self):
        backend = Recording()
        cwd = '/explicit/apache/source'
        command = 'tar -cf - . | ( cd /explicit/apache/pack/apps/tomcat8 ; tar -xf - )'
        evaluator, program = evaluate(':sys ' + command + '\n', cwd, backend)
        result = evaluator.run(program)
        self.assertTrue(result.complete)
        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        self.assertEqual(request.cwd, cwd)
        self.assertEqual(request.command, command)
        self.assertEqual(request.shell_command, command + '\n')
        self.assertEqual(request.expression.children[1].children[0]
                         .children[0].kind, 'BUILTIN')
        self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_real_shell_cd_effect_is_local_to_group(self):
        with tempfile.TemporaryDirectory() as cwd:
            os.mkdir(os.path.join(cwd, 'dest'))
            backend = PosixProcessBackend()
            command = ':sys printf payload | ( cd dest ; cat >> result )\n'
            evaluator, program = evaluate(command, cwd, backend)
            self.assertTrue(evaluator.run(program).complete)
            self.assertEqual(evaluator.scope.local['sysresult'], 0)
            with open(os.path.join(cwd, 'dest', 'result'), 'rb') as stream:
                self.assertEqual(stream.read(), b'payload')
            self.assertFalse(os.path.exists(os.path.join(cwd, 'result')))
            evaluator, program = evaluate(':sys ( cd dest ; pwd ) ; pwd\n',
                                          cwd, backend)
            result = evaluator.run(program)
            self.assertTrue(result.complete)
            self.assertEqual(result.processes[0].result.stdout.splitlines(),
                             [os.path.join(cwd, 'dest').encode(), cwd.encode()])

    def test_failed_cd_and_final_group_or_pipeline_status(self):
        with tempfile.TemporaryDirectory() as cwd:
            os.mkdir(os.path.join(cwd, 'existing'))
            backend = PosixProcessBackend()
            cases = (('( cd existing ; true )', 0),
                     ('( cd missing ; true )', 0),
                     ('( cd missing ; false )', 256),
                     ('( false ; true )', 0),
                     ('( true ; false )', 256),
                     ('false | ( cd existing ; true )', 0),
                     ('true | ( cd missing ; false )', 256))
            for command, status in cases:
                with self.subTest(command=command):
                    evaluator, program = evaluate(':sys {f} ' + command + '\n',
                                                  cwd, backend)
                    self.assertTrue(evaluator.run(program).complete)
                    self.assertEqual(evaluator.scope.local['sysresult'], status)

    def test_force_quiet_log_stays_one_shell_request(self):
        with tempfile.TemporaryDirectory() as cwd:
            backend = PosixProcessBackend()
            evaluator, program = evaluate(
                ':sys {f}{q}{l} ( cd missing ; false )\nAFTER = yes\n',
                cwd, backend)
            result = evaluator.run(program)
            self.assertTrue(result.complete)
            self.assertEqual(len(result.processes), 1)
            self.assertEqual(evaluator.scope.local['sysresult'], 1)
            self.assertEqual(evaluator.scope.local['AFTER'], 'yes')
            request = result.processes[0].request
            self.assertEqual((request.force, request.quiet, request.logging),
                             (True, True, True))
            self.assertEqual(request.command, '( cd missing ; false )')


if __name__ == '__main__':
    unittest.main()
