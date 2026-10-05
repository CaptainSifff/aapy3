"""Tracing wraps, but does not replace, the real POSIX process contract."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from process_adapter import PosixProcessBackend
from process_tracing import TracingProcessBackend


class Request(object):
    def __init__(self, cwd, command, operation=None, logged=False):
        self.cwd, self.cwd_bytes = cwd, cwd.encode('utf-8')
        self.command = command
        self.shell_command = command + '\n'
        self.shell_command_bytes = self.shell_command.encode('utf-8')
        self.operation = operation
        self.logging = logged
        self.environment_policy = 'inherit-backend'
        self.expression = None
        if logged:
            self.log_path = os.path.join(cwd, 'AAPDIR', 'log')
            self.log_command_text = '{f}{q}{l} ' + command
            self.quiet, self.force = True, True


class RecordingPosix(PosixProcessBackend):
    def __init__(self):
        self.requests = []

    def run_observed(self, request, started):
        self.requests.append(request)
        return super(RecordingPosix, self).run_observed(request, started)


class ProcessTracingTests(unittest.TestCase):
    def run_pair(self, command, operation=None, logged=False, setup=None,
                 expression=None):
        with tempfile.TemporaryDirectory() as root:
            observed = []
            for variant in ('bare', 'traced'):
                cwd = os.path.join(root, variant)
                os.mkdir(cwd)
                if setup is not None:
                    setup(cwd)
                request = Request(cwd, command, operation, logged)
                request.expression = expression
                backend = RecordingPosix()
                events = []
                def record(kind, **fields):
                    events.append((kind, fields))
                environment = {'AAP': 'aap', 'AAP_RECURSIVE_SENTINEL': 'test'}
                if operation == 'port_patch':
                    environment['AAP_RECURSIVE_PATCH_OBSERVE'] = os.path.join(cwd, 'patch')
                runner = (backend if variant == 'bare' else
                    TracingProcessBackend(backend, record, environment))
                result = runner.run(request)
                self.assertIs(backend.requests[0], request)
                observed.append((result, events, cwd))
            bare, traced = observed[0][0], observed[1][0]
            self.assertEqual((bare.wait_status, bare.stdout, bare.stderr,
                              bare.log_output, bare.shell_wait_status),
                             (traced.wait_status, traced.stdout, traced.stderr,
                              traced.log_output, traced.shell_wait_status))
            kinds = [event[0] for event in observed[1][1]]
            self.assertIn('process_start', kinds)
            self.assertIn('process_exit', kinds)
            return traced, kinds

    def test_plain_success_and_failure_preserve_status_and_streams(self):
        for command, status in (("printf out; printf err >&2; true", 0),
                                ("printf out; printf err >&2; false", 256)):
            result, kinds = self.run_pair(command)
            self.assertEqual((result.wait_status, result.stdout, result.stderr),
                             (status, b'out', b'err'))

    def test_logged_execution_preserves_recovered_status(self):
        result, kinds = self.run_pair("sh -c 'printf out; printf err >&2; false'",
                                      logged=True)
        self.assertEqual((result.wait_status, result.shell_wait_status,
                          result.log_output), (1, 0, b'outerr'))

    def test_append_observations_leave_request_and_result_unchanged(self):
        class Redirect(object):
            operator, target = '>>', 'destination'
        class Expression(object):
            kind, argv, redirections = 'SIMPLE', ('cat', 'source'), (Redirect(),)
        def setup(cwd):
            with open(os.path.join(cwd, 'source'), 'wb') as stream:
                stream.write(b'B')
            with open(os.path.join(cwd, 'destination'), 'wb') as stream:
                stream.write(b'A')
        result, kinds = self.run_pair('cat source >> destination',
                                      setup=setup, expression=Expression())
        self.assertEqual(result.wait_status, 0)
        self.assertEqual(kinds.count('append_redirection_observation'), 2)

    def test_patch_observations_leave_request_and_result_unchanged(self):
        def setup(cwd):
            with open(os.path.join(cwd, 'patch'), 'wb') as stream:
                stream.write(b'patch bytes')
        result, kinds = self.run_pair('true', operation='port_patch', setup=setup)
        self.assertEqual(result.wait_status, 0)
        self.assertEqual(kinds.count('patch_observation'), 2)

    def test_requested_file_observations_bracket_process_effect(self):
        with tempfile.TemporaryDirectory() as cwd:
            path = os.path.join(cwd, 'marker')
            with open(path, 'wb') as stream:
                stream.write(b'before')
            request = Request(cwd, 'printf after > marker')
            backend = RecordingPosix()
            events = []
            environment = {'AAP_RECURSIVE_PROCESS_OBSERVE': path}
            def record(kind, **fields):
                events.append((kind, fields))
            result = TracingProcessBackend(backend, record, environment).run(request)
            self.assertEqual(result.wait_status, 0)
            observations = [fields for kind, fields in events
                            if kind == 'process_file_observation']
            self.assertEqual([item['phase'] for item in observations],
                             ['before', 'after'])
            self.assertEqual(observations[0]['files'][0]['bytes'], 6)
            self.assertEqual(observations[1]['files'][0]['bytes'], 5)


if __name__ == '__main__':
    unittest.main()
