"""Focused CopyMove local-rename observations for the Nano doperlmod form."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, MemoryMoveBackend,
                           MoveBackend, MoveResult, SemanticError, Unsupported,
                           ProcessBackend, ProcessPolicy, ProcessResult)


class RecordingProcess(ProcessBackend):
    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return ProcessResult(0)


class MoveTests(unittest.TestCase):
    def make(self, text, files=None, failures=None, values=None, cwd='/recipe'):
        scope = Scope.top_level()
        scope.local.update(values or {})
        backend = MemoryMoveBackend(files, failures)
        evaluator = Evaluator(scope, cwd=cwd, move_backend=backend)
        return evaluator, lower(parse(Source('/recipe/main.aap', text))), backend

    def test_exact_nano_form_replaces_destination_and_removes_source(self):
        source = '/recipe/pack/.packlist.new'
        destination = '/recipe/pack/.packlist'
        evaluator, program, backend = self.make(':move $(name).new $name\n',
            {source: b'new', destination: b'old'}, values={'name': destination})
        result = evaluator.run(program)
        self.assertTrue(result.complete)
        self.assertEqual(backend.files, {destination: b'new'})
        request = backend.requests[0]
        self.assertEqual((request.source, request.destination), (source, destination))
        self.assertEqual(result.moves[0].status, 'COMPLETED')
        self.assertEqual(result.processes, [])

    def test_relative_paths_use_logical_cwd_without_normalizing_spelling(self):
        evaluator, program, backend = self.make(':move stage/new stage/old\n',
            {'/logical/work/stage/new': b'bytes'}, cwd='/logical/work')
        evaluator.run(program)
        request = backend.requests[0]
        self.assertEqual((request.source_argument, request.destination_argument),
                         ('stage/new', 'stage/old'))
        self.assertEqual((request.source, request.destination),
                         ('/logical/work/stage/new', '/logical/work/stage/old'))

    def test_missing_source_and_injected_failure_leave_files_unchanged(self):
        cases = (({}, None),
                 ({'/recipe/a': b'a'}, {('/recipe/a', '/recipe/b'):
                   MoveResult('FAILED', 'injected move')}))
        for files, failures in cases:
            evaluator, program, backend = self.make(':move a b\n', files, failures)
            before = dict(backend.files)
            with self.assertRaises(SemanticError):
                evaluator.run(program)
            self.assertEqual(backend.files, before)
            self.assertEqual(backend.requests[0].source, '/recipe/a')

    def test_unavailable_backend_blocks_before_any_mutation(self):
        evaluator = Evaluator(Scope.top_level(), cwd='/recipe', move_backend=MoveBackend())
        program = lower(parse(Source('/recipe/main.aap', ':move a b\n')))
        with self.assertRaises(Unsupported):
            evaluator.run(program)

    def test_preceding_fake_process_does_not_create_move_source(self):
        process = RecordingProcess()
        backend = MemoryMoveBackend()
        evaluator = Evaluator(Scope.top_level(), cwd='/recipe', move_backend=backend,
            process_backend=process, process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'))
        program = lower(parse(Source('/recipe/main.aap', ':sys producer\n:move made final\n')))
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(backend.files, {})

    def test_wider_copy_move_forms_remain_gated(self):
        for text in (':move {f} a b\n', ':move a b c\n', ':move *.new old\n',
                     ':move file:a b\n'):
            evaluator, program, backend = self.make(text, {'/recipe/a': b'a'})
            with self.assertRaises(Unsupported):
                evaluator.run(program)
            self.assertEqual(backend.requests, [])


if __name__ == '__main__':
    unittest.main()
