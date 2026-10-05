"""Bounded POSIX semicolon lists in synchronous A-A-P :sys."""
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
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.results.pop(0)


def evaluate(text, cwd, backend):
    scope = Scope.top_level()
    policy = ProcessPolicy('latin-1', sys_mode='bounded-attributes',
                           log_path=os.path.join(cwd, 'AAPDIR', 'log'))
    evaluator = Evaluator(scope, cwd=cwd, process_backend=backend,
                          process_policy=policy)
    program = lower(parse(Source('semicolon.aap', text)))
    return evaluator, program


def expression(text):
    origin = lower(parse(Source('semicolon.aap', ':sys placeholder\n'))).statements[0]
    return literal_shell(text, origin)


class SysSemicolonTests(unittest.TestCase):
    def test_sequence_is_below_and_or_and_pipeline(self):
        tree = expression('a ; b && c')
        self.assertEqual(tree.kind, 'SEQUENCE')
        self.assertEqual([item.kind for item in tree.children], ['SIMPLE', 'AND'])
        self.assertEqual([item.argv for item in tree.children[1].children],
                         [('b',), ('c',)])
        tree = expression('a && b ; c')
        self.assertEqual([item.kind for item in tree.children], ['AND', 'SIMPLE'])
        tree = expression('a | b ; c')
        self.assertEqual([item.kind for item in tree.children], ['PIPELINE', 'SIMPLE'])
        tree = expression('a ; b | c')
        self.assertEqual([item.kind for item in tree.children], ['SIMPLE', 'PIPELINE'])

    def test_groups_redirection_and_trailing_separator(self):
        tree = expression('(a ; b) ; c')
        self.assertEqual(tree.kind, 'SEQUENCE')
        self.assertEqual(tree.children[0].children[0].kind, 'SEQUENCE')
        tree = expression('a ; (b ; c)')
        self.assertEqual(tree.children[1].children[0].kind, 'SEQUENCE')
        tree = expression('cat src >> dst ; echo done')
        self.assertEqual(tree.children[0].redirections[0].operator, '>>')
        self.assertEqual(tree.children[0].redirections[0].target, 'dst')
        self.assertEqual(expression('a ;').kind, 'SIMPLE')
        self.assertEqual(expression('(a ;)').kind, 'GROUP')

    def test_quoted_escaped_and_rejected_separators(self):
        for spelling in ("echo ';'", 'echo ";"', 'echo \\;'):
            self.assertEqual(expression(spelling).argv, ('echo', ';'))
        for spelling in (';', '; a', 'a ; ; b', 'a ;; b'):
            with self.assertRaises(SemanticError):
                expression(spelling)
        for spelling in ('a & b', 'a ; b &', 'a ; b > out'):
            with self.assertRaises(Unsupported):
                expression(spelling)

    def test_existing_control_wrapper_rejections_stay_in_place(self):
        for spelling in ('cd x ; command', 'export X=y ; command',
                         'sh -c command ; other', 'eval command ; other'):
            with self.assertRaises(Unsupported):
                expression(spelling)

    def test_one_statement_is_one_request_with_exact_shell_text(self):
        backend = Recording([ProcessResult(0)])
        evaluator, program = evaluate(':sys a ; b\n', '/explicit/work', backend)
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        self.assertEqual((request.command, request.shell_command),
                         ('a ; b', 'a ; b\n'))
        self.assertEqual(request.expression.kind, 'SEQUENCE')
        self.assertEqual(len(request.batch.entries), 1)
        self.assertEqual(request.cwd, '/explicit/work')
        self.assertEqual(request.environment_policy, 'inherit-backend')
        self.assertEqual((request.stdin_policy, request.stdout_policy,
                          request.stderr_policy), ('inherit', 'inherit', 'inherit'))

    def test_aap_newline_batch_remains_distinct_from_semicolon_entry(self):
        backend = Recording([ProcessResult(0)])
        evaluator, program = evaluate(':sys a ; b\n:sys c\n',
                                      '/explicit/work', backend)
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        self.assertEqual(request.command, 'a ; b\nc')
        self.assertEqual(len(request.batch.entries), 2)
        self.assertEqual([item.kind for item in request.expression.children],
                         ['SEQUENCE', 'SIMPLE'])

    def test_force_still_splits_aap_batches(self):
        backend = Recording([ProcessResult(0), ProcessResult(256), ProcessResult(0)])
        evaluator, program = evaluate(
            ':sys a ; b\n:sys {f} false ; false\n:sys c\n',
            '/explicit/work', backend)
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual([request.command for request in backend.requests],
                         ['a ; b', 'false ; false', 'c'])
        self.assertEqual([request.force for request in backend.requests],
                         [False, True, False])

    def test_real_shell_order_final_status_and_append_effect(self):
        with tempfile.TemporaryDirectory() as cwd:
            backend = PosixProcessBackend()
            evaluator, program = evaluate(':sys false ; true\n', cwd, backend)
            self.assertTrue(evaluator.run(program).complete)
            self.assertEqual(evaluator.scope.local['sysresult'], 0)
            evaluator, program = evaluate(':sys true ; false\n', cwd, backend)
            with self.assertRaises(SemanticError):
                evaluator.run(program)
            self.assertEqual(evaluator.scope.local['sysresult'], 256)
            evaluator, program = evaluate(
                ':sys printf a >> result ; printf b >> result\n', cwd, backend)
            self.assertTrue(evaluator.run(program).complete)
            with open(os.path.join(cwd, 'result'), 'rb') as stream:
                self.assertEqual(stream.read(), b'ab')
            self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_real_force_and_logged_status_remain_distinct(self):
        with tempfile.TemporaryDirectory() as cwd:
            backend = PosixProcessBackend()
            evaluator, program = evaluate(
                ':sys {f} false ; false\nAFTER = yes\n', cwd, backend)
            self.assertTrue(evaluator.run(program).complete)
            self.assertEqual((evaluator.scope.local['sysresult'],
                              evaluator.scope.local['AFTER']), (256, 'yes'))
            evaluator, program = evaluate(
                ':sys {f}{q}{l} false ; false\nAFTER = yes\n', cwd, backend)
            result = evaluator.run(program)
            self.assertTrue(result.complete)
            self.assertEqual((evaluator.scope.local['sysresult'],
                              evaluator.scope.local['AFTER']), (1, 'yes'))
            request = result.processes[0].request
            self.assertEqual((request.force, request.quiet, request.logging),
                             (True, True, True))
            self.assertFalse(request.echo)
            self.assertEqual(request.command, 'false ; false')
            self.assertEqual(request.status_policy, 'logged-recovered')
            with open(request.log_path, 'rb') as stream:
                self.assertIn(b'false ; false', stream.read())


if __name__ == '__main__':
    unittest.main()
