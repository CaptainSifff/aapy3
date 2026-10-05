"""Focused bounded local-file :copy observations and mutations."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (CopyBackend, CopyObservation, CopyResult, Evaluator,
                           MemoryCopyBackend, Scope, SemanticError, Unsupported,
                           lower)


class UnavailableMutation(MemoryCopyBackend):
    def copy(self, request):
        self.requests.append(request)
        return CopyResult('UNAVAILABLE', 'injected copy mutation unavailable')


class FailedObservation(CopyBackend):
    def path_kind(self, path):
        return CopyObservation('FAILED', detail='injected observation failure')


class CopyTests(unittest.TestCase):
    def make(self, text, files=None, directories=None, failures=None, values=None,
             cwd='/recipe', backend_type=MemoryCopyBackend):
        scope = Scope.top_level()
        scope.local.update(values or {})
        backend = backend_type(files, directories, failures)
        evaluator = Evaluator(scope, cwd=cwd, copy_backend=backend)
        program = lower(parse(Source('/recipe/main.aap', text)))
        return evaluator, program, backend

    def test_regular_file_to_missing_filename_keeps_source_and_exact_bytes(self):
        evaluator, program, backend = self.make(':copy source target\n',
            {'/recipe/source': b'\x00source'}, ['/recipe'])
        result = evaluator.run(program)
        self.assertTrue(result.complete)
        self.assertEqual(backend.files['/recipe/source'], b'\x00source')
        self.assertEqual(backend.files['/recipe/target'], b'\x00source')
        request = backend.requests[0]
        self.assertEqual((request.source, request.destination,
                          request.effective_destination),
                         ('/recipe/source', '/recipe/target', '/recipe/target'))
        self.assertEqual(request.overwrite, 'replace')
        self.assertEqual(result.copies[0].status, 'COMPLETED')
        self.assertEqual(result.processes, [])

    def test_existing_destination_directory_gets_source_basename(self):
        evaluator, program, backend = self.make(':copy files/grub.cfg boot/grub\n',
            {'/recipe/files/grub.cfg': b'configuration'},
            ['/recipe/boot/grub'])
        evaluator.run(program)
        request = backend.requests[0]
        self.assertEqual(request.destination, '/recipe/boot/grub')
        self.assertEqual(request.effective_destination,
                         '/recipe/boot/grub/grub.cfg')
        self.assertEqual(backend.files['/recipe/files/grub.cfg'], b'configuration')
        self.assertEqual(backend.files['/recipe/boot/grub/grub.cfg'], b'configuration')

    def test_efiloader_shape_expands_and_uses_explicit_logical_cwd(self):
        cwd = '/ports/company/efiloader'
        destination = cwd + '/work/efiloader/boot/grub'
        evaluator, program, backend = self.make(
            ':copy files/grub.cfg $WRKDIR/$WRKSRC/boot/grub\n',
            {cwd + '/files/grub.cfg': b'grub'}, [destination],
            values={'WRKDIR': 'work', 'WRKSRC': 'efiloader'}, cwd=cwd)
        evaluator.run(program)
        request = backend.requests[0]
        self.assertEqual(request.source, cwd + '/files/grub.cfg')
        self.assertEqual(request.destination, destination)
        self.assertEqual(request.effective_destination, destination + '/grub.cfg')

    def test_existing_regular_destination_is_overwritten_without_force(self):
        evaluator, program, backend = self.make(':copy source target\n',
            {'/recipe/source': b'new', '/recipe/target': b'old'}, ['/recipe'])
        evaluator.run(program)
        self.assertEqual(backend.files['/recipe/target'], b'new')

    def test_absolute_paths_are_not_rebased(self):
        evaluator, program, backend = self.make(':copy /input/a /output/b\n',
            {'/input/a': b'a'}, ['/input', '/output'], cwd='/logical/cwd')
        evaluator.run(program)
        self.assertEqual((backend.requests[0].source,
                          backend.requests[0].effective_destination),
                         ('/input/a', '/output/b'))

    def test_missing_source_fails_before_mutation(self):
        evaluator, program, backend = self.make(':copy absent target\n', {}, ['/recipe'])
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertEqual(backend.requests, [])
        self.assertEqual(backend.files, {})

    def test_directory_source_remains_gated_without_recursive_option(self):
        evaluator, program, backend = self.make(':copy tree target\n', {},
            ['/recipe/tree', '/recipe'])
        with self.assertRaises(Unsupported):
            evaluator.run(program)
        self.assertEqual(backend.requests, [])

    def test_missing_destination_parent_is_backend_failure(self):
        evaluator, program, backend = self.make(':copy source absent/target\n',
            {'/recipe/source': b'source'}, ['/recipe'])
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertEqual(backend.requests[0].effective_destination,
                         '/recipe/absent/target')
        self.assertNotIn('/recipe/absent/target', backend.files)

    def test_non_directory_destination_component_fails_without_mutation(self):
        evaluator, program, backend = self.make(':copy source holder/target\n',
            {'/recipe/source': b'source', '/recipe/holder': b'not a directory'},
            ['/recipe'])
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertNotIn('/recipe/holder/target', backend.files)

    def test_unavailable_observation_blocks_before_request(self):
        evaluator = Evaluator(Scope.top_level(), cwd='/recipe', copy_backend=CopyBackend())
        program = lower(parse(Source('/recipe/main.aap', ':copy source target\n')))
        with self.assertRaises(Unsupported):
            evaluator.run(program)
        self.assertEqual(evaluator.last_result.copies, [])

    def test_unavailable_mutation_blocks_with_a_record(self):
        evaluator, program, backend = self.make(':copy source target\n',
            {'/recipe/source': b'source'}, ['/recipe'], backend_type=UnavailableMutation)
        with self.assertRaises(Unsupported):
            evaluator.run(program)
        self.assertEqual(backend.requests[0].effective_destination, '/recipe/target')
        self.assertEqual(evaluator.last_result.copies[0].status, 'BLOCKED')

    def test_backend_and_source_errors_are_failed_and_source_aware(self):
        evaluator = Evaluator(Scope.top_level(), cwd='/recipe', copy_backend=FailedObservation())
        program = lower(parse(Source('/source/example.aap', ':copy source target\n')))
        with self.assertRaises(SemanticError) as caught:
            evaluator.run(program)
        self.assertEqual((caught.exception.span.source_id, caught.exception.span.start.line),
                         ('/source/example.aap', 1))

        failure = CopyResult('FAILED', 'injected write failure')
        evaluator, program, backend = self.make(':copy source target\n',
            {'/recipe/source': b'source'}, ['/recipe'],
            {('/recipe/source', '/recipe/target'): failure})
        with self.assertRaises(SemanticError):
            evaluator.run(program)
        self.assertEqual(evaluator.last_result.copies[0].status, 'FAILED')

    def test_unreached_broader_forms_stay_gated(self):
        for text in (':copy {force} source target\n', ':copy first second target\n',
                     ':copy *.cfg target\n', ':copy source file:target\n'):
            evaluator, program, backend = self.make(text,
                {'/recipe/source': b'source'}, ['/recipe'])
            with self.assertRaises(Unsupported):
                evaluator.run(program)
            self.assertEqual(backend.requests, [])


if __name__ == '__main__':
    unittest.main()
