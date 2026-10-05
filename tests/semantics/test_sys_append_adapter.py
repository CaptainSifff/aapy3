"""Real POSIX-shell ownership checks for the bounded :sys >> form."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from aap_cli import Shell
from aap_frontend import Source, parse
from aap_semantics import Evaluator, Scope, lower, ProcessPolicy, SemanticError


class AppendAdapterTests(unittest.TestCase):
    def test_real_shell_appends_external_cat_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'source'), 'wb') as stream:
                stream.write(b'B\x00\xff')
            with open(os.path.join(root, 'destination'), 'wb') as stream:
                stream.write(b'A')
            scope = Scope.top_level()
            evaluator = Evaluator(scope, cwd=root, process_backend=Shell(),
                                  process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'))
            program = lower(parse(Source(os.path.join(root, 'main.aap'),
                ':sys cat source >> destination\n')))
            self.assertTrue(evaluator.run(program).complete)
            with open(os.path.join(root, 'destination'), 'rb') as stream:
                self.assertEqual(stream.read(), b'AB\x00\xff')
            self.assertEqual(scope.local['sysresult'], 0)

    def test_real_shell_creates_missing_append_destination(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'source'), 'wb') as stream:
                stream.write(b'created')
            evaluator = Evaluator(Scope.top_level(), cwd=root, process_backend=Shell(),
                                  process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'))
            program = lower(parse(Source(os.path.join(root, 'main.aap'),
                ':sys cat source >> new-destination\n')))
            self.assertTrue(evaluator.run(program).complete)
            with open(os.path.join(root, 'new-destination'), 'rb') as stream:
                self.assertEqual(stream.read(), b'created')

    def test_redirection_open_failure_is_reported_by_shell_process(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, 'source'), 'wb') as stream:
                stream.write(b'content')
            scope = Scope.top_level()
            evaluator = Evaluator(scope, cwd=root, process_backend=Shell(),
                                  process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'))
            program = lower(parse(Source(os.path.join(root, 'main.aap'),
                ':sys cat source >> missing/destination\n')))
            with self.assertRaises(SemanticError):
                evaluator.run(program)
            self.assertNotEqual(scope.local['sysresult'], 0)
            self.assertFalse(os.path.exists(os.path.join(root, 'missing')))


if __name__ == '__main__':
    unittest.main()
