"""Reached synchronous f/q/l sys contract from Process, Commands and Util."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, ProcessBackend, ProcessResult,
                           ProcessPolicy, SemanticError, Unsupported)


class Recording(ProcessBackend):
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.results.pop(0)


def make(text, results):
    scope = Scope.top_level()
    backend = Recording(results)
    evaluator = Evaluator(scope, cwd='/recipe', process_backend=backend,
        process_policy=ProcessPolicy('utf-8', sys_mode='bounded-attributes',
                                     log_path='/recipe/AAPDIR/log'))
    program = lower(parse(Source('/recipe/main.aap', text)))
    return evaluator, program, backend


class SysAttributeTests(unittest.TestCase):
    def test_force_nonzero_sets_status_and_continues(self):
        evaluator, program, backend = make(':sys {f} failing\nAFTER = yes\n',
                                            [ProcessResult(256)])
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual(evaluator.scope.local['sysresult'], 256)
        self.assertEqual(evaluator.scope.local['AFTER'], 'yes')
        self.assertTrue(backend.requests[0].force)
        self.assertEqual(backend.requests[0].command, 'failing')

    def test_unforced_failure_writes_status_before_error(self):
        evaluator, program, backend = make(':sys failing\nAFTER = no\n',
                                            [ProcessResult(256)])
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertEqual(evaluator.scope.local['sysresult'], 256)
        self.assertNotIn('AFTER', evaluator.scope.local)

    def test_force_splits_both_sides_of_batch(self):
        evaluator, program, backend = make(
            ':sys first\n:sys {f} second\n:sys third\n',
            [ProcessResult(0), ProcessResult(256), ProcessResult(0)])
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual([r.command for r in backend.requests],
                         ['first', 'second', 'third'])
        self.assertEqual([r.force for r in backend.requests], [False, True, False])
        evaluator, program, backend = make(':sys {f} first\n:sys second\n',
                                           [ProcessResult(0), ProcessResult(0)])
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual([r.command for r in backend.requests], ['first', 'second'])

    def test_quiet_suppresses_echo_not_child_streams(self):
        evaluator, program, backend = make(':sys {q} inspect\n',
                                            [ProcessResult(0, b'out', b'err')])
        record = evaluator.run(program).processes[0]
        self.assertFalse(record.request.echo)
        self.assertFalse(record.request.logging)
        self.assertEqual((record.result.stdout, record.result.stderr),
                         (b'out', b'err'))

    def test_logged_status_is_recovered_not_encoded(self):
        evaluator, program, backend = make(':sys {l} inspect\n',
            [ProcessResult(1, b'', b'', b'out\nerr\n', 0)])
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertEqual(evaluator.scope.local['sysresult'], 1)
        self.assertEqual(backend.requests[0].status_policy, 'logged-recovered')
        self.assertEqual(backend.requests[0].log_path, '/recipe/AAPDIR/log')

    def test_combined_force_quiet_log_success_and_failure(self):
        for status in (0, 1):
            evaluator, program, backend = make(
                ':sys {f}{q}{l} rpm --quiet --query systemd\n'
                '@if _no.sysresult == 0:\n'
                '  BRANCH = present\n'
                '@else:\n'
                '  BRANCH = absent\n',
                [ProcessResult(status, b'', b'', b'query output\n', 0)])
            self.assertTrue(evaluator.run(program).complete)
            request = backend.requests[0]
            self.assertEqual(request.command, 'rpm --quiet --query systemd')
            self.assertEqual(request.shell_command, request.command + '\n')
            self.assertEqual((request.force, request.quiet, request.logging),
                             (True, True, True))
            self.assertFalse(request.echo)
            self.assertEqual(request.cwd, '/recipe')
            self.assertEqual(evaluator.scope.local['sysresult'], status)
            self.assertEqual(evaluator.scope.local['BRANCH'],
                             'present' if status == 0 else 'absent')

    def test_aliases_and_order_are_accepted_but_values_remain_gated(self):
        evaluator, program, backend = make(':sys {log}{quiet}{force} inspect\n',
                                           [ProcessResult(0, b'', b'', b'')])
        self.assertTrue(evaluator.run(program).complete)
        self.assertEqual((backend.requests[0].force, backend.requests[0].quiet,
                          backend.requests[0].logging), (True, True, True))
        evaluator, program, backend = make(':sys {f=0}{l} inspect\n', [])
        with self.assertRaises(Unsupported):
            evaluator.run(program)
        self.assertEqual(backend.requests, [])

    def test_expansion_created_force_does_not_gain_pre_expansion_force_semantics(self):
        evaluator, program, backend = make(':sys $OPTION inspect\n', [])
        evaluator.scope.local['OPTION'] = '{f}'
        with self.assertRaises(Unsupported):
            evaluator.run(program)
        self.assertEqual(backend.requests, [])

    def test_log_path_capability_is_required_before_submission(self):
        evaluator, program, backend = make(':sys {f}{q}{l} inspect\n', [])
        evaluator.process.policy.log_path = None
        with self.assertRaises(Unsupported):
            evaluator.run(program)
        self.assertEqual(backend.requests, [])
