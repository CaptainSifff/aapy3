"""Disposable POSIX adapter tests for Util.logged_system's reached {l} route."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from aap_cli import Shell
from aap_frontend import Source, parse
from aap_semantics import Evaluator, Scope, lower, ProcessPolicy


class Request(object):
    def __init__(self, cwd, command, quiet):
        self.cwd = cwd
        self.cwd_bytes = cwd.encode('utf-8')
        self.command = command
        self.log_command_text = '{f}{q}{l} ' + command
        self.log_path = os.path.join(cwd, 'AAPDIR', 'log')
        self.quiet = quiet
        self.force = True
        self.logging = True


class LoggedAdapterTests(unittest.TestCase):
    def test_combined_form_crosses_real_process_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            scope = Scope.top_level()
            evaluator = Evaluator(scope, cwd=root, process_backend=Shell(),
                process_policy=ProcessPolicy('utf-8',
                    sys_mode='bounded-attributes',
                    log_path=os.path.join(root, 'AAPDIR', 'log')))
            program = lower(parse(Source(os.path.join(root, 'main.aap'),
                ':sys {f}{q}{l} false\nAFTER = yes\n')))
            self.assertTrue(evaluator.run(program).complete)
            self.assertEqual(scope.local['sysresult'], 1)
            self.assertEqual(scope.local['AFTER'], 'yes')
            self.assertTrue(os.path.isfile(os.path.join(root, 'AAPDIR', 'log')))

    def test_missing_shell_status_is_error_and_temps_are_removed(self):
        with tempfile.TemporaryDirectory() as root:
            request = Request(root, 'exit 1', True)
            with self.assertRaises(ValueError):
                Shell().run(request)
            self.assertEqual(next(os.walk(os.path.dirname(request.log_path)))[2],
                             ['log'])

    def test_logged_streams_status_and_temporary_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            request = Request(root, "sh -c 'printf out; printf err >&2; false'", True)
            result = Shell().run(request)
            self.assertEqual(result.wait_status, 1)
            self.assertEqual(result.shell_wait_status, 0)
            self.assertEqual(result.log_output, b'outerr')
            self.assertEqual((result.stdout, result.stderr), (b'', b''))
            with open(request.log_path, 'rb') as stream:
                logged = stream.read()
            self.assertIn(b'log:\t{f}{q}{l} ', logged)
            self.assertIn(b'log:\touterr\n', logged)
            self.assertEqual(next(os.walk(os.path.dirname(request.log_path)))[2],
                             ['log'])

    def test_logged_success_preserves_zero_and_merged_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            request = Request(root, 'printf "a\\nb\\n"; true', False)
            result = Shell().run(request)
            self.assertEqual(result.wait_status, 0)
            self.assertEqual(result.log_output, b'a\nb\n')
            with open(request.log_path, 'rb') as stream:
                logged = stream.read()
            self.assertIn(b'system:\t{f}{q}{l}', logged)
            self.assertIn(b'log:\ta\nb\n', logged)
            self.assertEqual(next(os.walk(os.path.dirname(request.log_path)))[2],
                             ['log'])
