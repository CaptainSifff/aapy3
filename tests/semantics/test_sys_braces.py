"""Bounded shell brace words remain intact for the target shell."""
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


def origin_node():
    return lower(parse(Source('shell-braces.aap', ':sys placeholder\n'))).statements[0]


def expression(command):
    return literal_shell(command, origin_node())


def evaluate(source, cwd, backend):
    scope = Scope.top_level()
    policy = ProcessPolicy('latin-1', sys_mode='bounded-attributes',
                           log_path=os.path.join(cwd, 'AAPDIR', 'log'))
    evaluator = Evaluator(scope, cwd=cwd, process_backend=backend,
                          process_policy=policy)
    return evaluator, lower(parse(Source('shell-braces.aap', source)))


class SysBraceTests(unittest.TestCase):
    def test_active_comma_alternatives_remain_one_word(self):
        cases = {
            'rm foo/{a,b}': ('rm', 'foo/{a,b}'),
            'rm foo/{a,b,c}': ('rm', 'foo/{a,b,c}'),
            'rm foo/{a-b,c.txt}': ('rm', 'foo/{a-b,c.txt}'),
            'rm foo/{a,b}.txt': ('rm', 'foo/{a,b}.txt'),
            'rm {a,b}{c,d}': ('rm', '{a,b}{c,d}'),
            'rm *.txt file[12] file?':
                ('rm', '*.txt', 'file[12]', 'file?'),
        }
        for command, argv in cases.items():
            with self.subTest(command=command):
                self.assertEqual(expression(command).argv, argv)

    def test_quoted_and_escaped_braces_are_literal(self):
        for command in ("echo '{a,b}'", 'echo "{a,b}"', r'echo \{a,b\}'):
            with self.subTest(command=command):
                self.assertEqual(expression(command).argv, ('echo', '{a,b}'))

    def test_malformed_active_braces_are_semantic_errors(self):
        for command in ('rm foo/{a,b', 'rm foo/a,b}', 'rm foo/{a}', 'rm foo/{}'):
            with self.subTest(command=command):
                with self.assertRaises(SemanticError):
                    expression(command)

    def test_valid_but_unbounded_brace_forms_are_unsupported(self):
        for command in ('rm foo/{a,,b}', 'rm foo/{a,}', 'rm foo/{,b}',
                        'rm foo/{a,{b,c}}', 'rm foo/{1..5}',
                        'rm foo/{a..z}', 'rm foo/{a..b,c}'):
            with self.subTest(command=command):
                with self.assertRaises(Unsupported):
                    expression(command)

    def test_apache_command_is_one_request_with_one_brace_word(self):
        backend = Recording()
        cwd = '/package/work'
        evaluator, program = evaluate(
            ':sys rm -f $PKGDIR$PREFIX/{LICENSE,NOTICE,RELEASE-NOTES,RUNNING.txt}\n',
            cwd, backend)
        evaluator.scope.local['PKGDIR'] = '/package/pack'
        evaluator.scope.local['PREFIX'] = '/apps/tomcat8'
        result = evaluator.run(program)
        self.assertTrue(result.complete)
        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        path = '/package/pack/apps/tomcat8/{LICENSE,NOTICE,RELEASE-NOTES,RUNNING.txt}'
        self.assertEqual(request.cwd, cwd)
        self.assertEqual(request.command, 'rm -f ' + path)
        self.assertEqual(request.shell_command, 'rm -f ' + path + '\n')
        self.assertEqual(request.expression.argv,
                         ('rm', '-f', path))
        self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_real_shell_removes_alternatives_and_preserves_other_file(self):
        with tempfile.TemporaryDirectory() as cwd:
            target = os.path.join(cwd, 'prefix')
            os.mkdir(target)
            names = ('LICENSE', 'NOTICE', 'RELEASE-NOTES', 'RUNNING.txt')
            for name in names + ('KEEP',):
                with open(os.path.join(target, name), 'wb') as stream:
                    stream.write(name.encode('ascii'))
            before = dict((name, os.path.isfile(os.path.join(target, name)))
                          for name in names + ('KEEP',))
            # Brace expansion is a Bash feature; Ubuntu's /bin/sh is dash.
            backend = PosixProcessBackend('/bin/bash')
            evaluator, program = evaluate(
                ':sys rm -f $PKGDIR$PREFIX/{LICENSE,NOTICE,RELEASE-NOTES,RUNNING.txt}\n',
                cwd, backend)
            evaluator.scope.local['PKGDIR'] = cwd
            evaluator.scope.local['PREFIX'] = '/prefix'
            result = evaluator.run(program)
            self.assertTrue(result.complete)
            self.assertEqual(evaluator.scope.local['sysresult'], 0)
            after = dict((name, os.path.isfile(os.path.join(target, name)))
                         for name in names + ('KEEP',))
            self.assertEqual(before, dict((name, True) for name in names + ('KEEP',)))
            self.assertEqual(after, dict((name, name == 'KEEP')
                                         for name in names + ('KEEP',)))

    def test_failed_shell_status_policy_is_unchanged(self):
        with tempfile.TemporaryDirectory() as cwd:
            evaluator, program = evaluate(':sys false\n', cwd,
                                          PosixProcessBackend())
            with self.assertRaises(SemanticError):
                evaluator.run(program)
            self.assertEqual(evaluator.scope.local['sysresult'], 256)


if __name__ == '__main__':
    unittest.main()
