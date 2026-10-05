"""Characterizations from Port.py:248-380, Sign.check_md5, Process.recipe_error.

All failure injection and byte mutation are in memory. In particular the
written restoration code in Port.py is dead after recipe_error raises.
"""
import hashlib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, WorkIdentity, lower, BuildDriver,
    MemoryTargetState, MemoryPersistence, PortRuntime, MemoryArtifacts,
    RecipeMutationBackend, RecipeMutationResult, MemoryRecipeMutationBackend,
    MemoryPathObserver, PathObservation, PathRequest)
from nano_target_frontier import fixture


BASE = '/recipe/'
RECIPE = BASE + 'main.aap'
TEMP = RECIPE + '1'
BACKUP = RECIPE + '~'
ARCHIVE = BASE + 'distfiles/a.tgz'
START = b'#>>> automatically inserted by "aap makesum" <<<\n'
END = b'#>>> end <<<\n'
LINE = b'\t:checksum $DISTDIR/a.tgz {md5 = 900150983cd24fb0d6963f7d28e17f72}\n'
BLOCK = START + b'do-checksum:\n' + LINE + END
OLD = b'prefix\x00\xff\r\n' + START + b'do-checksum:\n\told\n' + END + b'tail'
NEW = b'prefix\x00\xff\r\n' + BLOCK + b'tail'


class MakesumTests(unittest.TestCase):
    def make(self, recipe=OLD, top='main.aap', variables=None, files=None,
             failures=None, backend_type=MemoryRecipeMutationBackend,
             artifacts=None):
        scope = Scope.top_level(port_defaults=True, work=WorkIdentity(top))
        scope.local.update({'DISTFILES': 'a.tgz', 'DISTDIR': 'distfiles'})
        scope.local.update(variables or {})
        store = {RECIPE: recipe, ARCHIVE: b'abc'}
        store.update(files or {})
        backend = backend_type([RECIPE], store, failures)
        artifacts = artifacts if artifacts is not None else MemoryArtifacts(store, shared=True)
        runtime = PortRuntime(artifacts=artifacts, recipe_mutations=backend)
        # Source and writable identity deliberately differ.
        source = Source('/recipe/other-source.aap',
                        'all {virtual}:\n  @port_makesum(globals())\n  AFTER = yes\n')
        data = Evaluator(scope).run(lower(parse(source)))
        saved = MemoryPersistence()
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, scope,
            data.declarations, port_runtime=runtime, port_defaults=False,
            path_observer=backend)
        return driver, backend

    def run_case(self, expected='COMPLETE', **options):
        driver, backend = self.make(**options)
        result = driver.build('all')
        self.assertEqual(result.status, expected, str(result.error))
        operation = result.bodies[0].port_operations[0]
        self.assertEqual(operation.status, 'COMPLETED' if expected == 'COMPLETE' else expected)
        self.assertEqual(result.bodies[0].processes, ())
        self.assertEqual(operation.processes, [])
        self.assertEqual(driver.persistence.writes, [])
        if expected != 'COMPLETE':
            self.assertNotIn('AFTER', result.bodies[0].scope.local)
        return result, backend, operation

    def mutations(self, backend):
        return [(r.operation, r.path, r.destination) for r in backend.requests
                if r.operation in ('remove', 'rename')]

    def fail(self, operation, path, destination=None, status='FAILED'):
        return {(operation, path, destination): RecipeMutationResult(status, 'injected ' + operation)}

    def test_no_top_recipe_fails_before_any_observation_even_with_source_id(self):
        for top in (None, ''):
            result, backend, operation = self.run_case('FAILED', top=top)
            self.assertIn('No recipe specified to makesum for', str(result.error))
            self.assertEqual(backend.requests, [])
            self.assertEqual(backend.observations, [])
            self.assertEqual(operation.span.source_id, '/recipe/other-source.aap')
            self.assertEqual(operation.span.start.line, 2)

    def test_explicit_work_identity_survives_build_scope_and_ignores_recipe_values(self):
        driver, backend = self.make(variables={'top_recipe': 'wrong.aap'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertIs(result.bodies[0].scope.get_work(), driver.scope.work)
        self.assertEqual(backend.files[RECIPE], NEW)
        self.assertNotIn(BASE + 'other-source.aap', backend.files)
        # Separate entry Work objects never inherit the previous entry's name.
        self.assertIsNone(Scope.top_level().get_work().top_recipe)

    def test_exact_replacement_digest_bytes_and_success_order(self):
        result, backend, operation = self.run_case()
        self.assertEqual(backend.files[RECIPE], NEW)
        self.assertEqual(backend.files[BACKUP], OLD)
        self.assertNotIn(TEMP, backend.files)
        self.assertEqual(operation.checksums[0][1], '900150983cd24fb0d6963f7d28e17f72')
        self.assertEqual(self.mutations(backend), [
            ('rename', RECIPE, BACKUP), ('rename', TEMP, RECIPE), ('remove', TEMP, None)])
        self.assertEqual(operation.mutations[-1][1].status, 'FAILED')  # ignored missing temp
        operations = [r.operation for r in backend.requests]
        self.assertEqual(operations[:3], ['open_read', 'create_temp', 'readline'])
        self.assertLess(operations.index('close_read'), operations.index('close_temp'))
        self.assertEqual([r.path for r in backend.observations], [ARCHIVE, TEMP, BACKUP])
        for path, status in ((RECIPE, 'EXISTS'), (BACKUP, 'EXISTS'), (TEMP, 'MISSING')):
            self.assertEqual(backend.observe(PathRequest(path, BASE, operation)).status, status)

    def test_absolute_top_recipe_identity_uses_the_explicit_recipe_path(self):
        result, backend, operation = self.run_case(top=RECIPE)
        self.assertEqual(backend.files[RECIPE], NEW)
        self.assertEqual(backend.files[BACKUP], OLD)

    def test_numbered_temp_selection_and_old_backup_replacement(self):
        result, backend, operation = self.run_case(files={
            TEMP: b'occupied 1', RECIPE + '2': b'occupied 2', BACKUP: b'old backup'})
        self.assertEqual(backend.files[TEMP], b'occupied 1')
        self.assertEqual(backend.files[RECIPE + '2'], b'occupied 2')
        self.assertEqual(backend.files[RECIPE], NEW)
        self.assertEqual(backend.files[BACKUP], OLD)
        self.assertEqual([r.path for r in backend.observations],
                         [ARCHIVE, TEMP, RECIPE + '2', RECIPE + '3', BACKUP])
        self.assertEqual(self.mutations(backend), [('remove', BACKUP, None),
            ('rename', RECIPE, BACKUP), ('rename', RECIPE + '3', RECIPE),
            ('remove', RECIPE + '3', None)])

    def test_absent_block_appends_without_inserting_a_newline(self):
        for original in (b'', b'body', b'body\n', b'body\r\n'):
            result, backend, operation = self.run_case(recipe=original)
            self.assertEqual(backend.files[RECIPE], original + BLOCK)

    def test_empty_lists_insert_pass_and_do_not_require_directory_values(self):
        result, backend, operation = self.run_case(recipe=b'', variables={
            'DISTFILES': '', 'DISTDIR': None, 'PATCHDISTDIR': None})
        self.assertEqual(backend.files[RECIPE], START + b'do-checksum:\n\t@pass\n' + END)
        self.assertEqual(operation.checksums, [])

    def test_crlf_and_unterminated_markers_are_not_exact_markers(self):
        for original in (START.replace(b'\n', b'\r\n') + b'old\r\n' + END,
                         START[:-1], b' ' + START + END):
            result, backend, operation = self.run_case(recipe=original)
            self.assertEqual(backend.files[RECIPE], original + BLOCK)

    def test_nested_start_is_discarded_but_second_separate_start_is_error(self):
        result, backend, operation = self.run_case(recipe=START + START + END)
        self.assertEqual(backend.files[RECIPE], BLOCK)
        original = START + END + START + END
        result, backend, operation = self.run_case('FAILED', recipe=original)
        self.assertIn('Duplicate makesum start marker', str(result.error))
        self.assertEqual(backend.files[RECIPE], original)
        self.assertNotIn(TEMP, backend.files)
        self.assertEqual(self.mutations(backend), [('remove', TEMP, None)])

    def test_missing_end_removes_temp_and_preserves_original(self):
        for original in (START + b'old\n', START + END[:-1], START + END.replace(b'\n', b'\r\n')):
            result, backend, operation = self.run_case('FAILED', recipe=original)
            self.assertIn('Missing makesum end marker', str(result.error))
            self.assertEqual(backend.files[RECIPE], original)
            self.assertNotIn(TEMP, backend.files)
            self.assertNotIn(BACKUP, backend.files)

    def test_basename_dedup_order_normal_then_cvs_per_group_attributes_ignored(self):
        result, backend, operation = self.run_case(recipe=b'', variables={
            'DISTFILES': 'x/a.tgz {ignored=yes} y/b.tgz x/a.tgz',
            'CVSDISTFILES': 'else/b.tgz c.tgz',
            'PATCHFILES': 'patch/a.tgz', 'CVSPATCHFILES': 'other/a.tgz b.tgz',
            'CVS': 'no'}, files={BASE + 'distfiles/b.tgz': b'def',
                BASE + 'distfiles/c.tgz': b'ghi', BASE + 'patches/a.tgz': b'jkl',
                BASE + 'patches/b.tgz': b'mno'})
        self.assertEqual([r.path for r, d in operation.checksums], [ARCHIVE,
            BASE + 'distfiles/b.tgz', BASE + 'distfiles/c.tgz',
            BASE + 'patches/a.tgz', BASE + 'patches/b.tgz'])
        expected = START + b'do-checksum:\n' + LINE
        for directory, name, data in (('DISTDIR', 'b.tgz', b'def'),
                ('DISTDIR', 'c.tgz', b'ghi'), ('PATCHDISTDIR', 'a.tgz', b'jkl'),
                ('PATCHDISTDIR', 'b.tgz', b'mno')):
            expected += ('\t:checksum $%s/%s {md5 = %s}\n' %
                         (directory, name, hashlib.md5(data).hexdigest())).encode('ascii')
        self.assertEqual(backend.files[RECIPE], expected + END)

    def test_quoted_basename_absolute_directory_and_multichunk_binary_md5(self):
        data = b'\x00\xffabc\r\n' * 20000
        result, backend, operation = self.run_case(recipe=b'', variables={
            'DISTFILES': '"dir/a file.tgz"', 'DISTDIR': '/archive'},
            files={'/archive/a file.tgz': data})
        self.assertEqual(backend.files[RECIPE], START + b'do-checksum:\n' +
            ('\t:checksum $DISTDIR/a file.tgz {md5 = %s}\n' %
             hashlib.md5(data).hexdigest()).encode('ascii') + END)

    def test_missing_archive_fails_and_unknown_observation_blocks(self):
        driver, backend = self.make()
        del backend.files[ARCHIVE]
        backend.fallback = MemoryPathObserver({ARCHIVE: PathObservation('MISSING')})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertIn('File does not exists: "distfiles/a.tgz"', str(result.error))
        self.assertEqual(backend.requests, [])
        driver, backend = self.make()
        del backend.files[ARCHIVE]
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(backend.requests, [])

    def test_archive_read_failure_and_nonfile_do_not_open_recipe(self):
        for artifacts in (MemoryArtifacts({}), MemoryArtifacts({ARCHIVE: None})):
            result, backend, operation = self.run_case('FAILED', artifacts=artifacts)
            self.assertIn('Cannot compute checksum for "distfiles/a.tgz"', str(result.error))
            self.assertEqual(backend.requests, [])

    def test_unavailable_artifact_or_mutation_capability_blocks(self):
        driver, backend = self.make()
        driver.port_runtime.artifacts = None
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(backend.requests, [])
        driver, backend = self.make()
        driver.port_runtime.recipe_mutations = RecipeMutationBackend()
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(backend.files[RECIPE], OLD)
        self.assertNotIn(TEMP, backend.files)

    def test_open_create_read_write_and_close_failure_stages(self):
        cases = [('open_read', RECIPE, 'Cannot open recipe file'),
                 ('create_temp', TEMP, 'Cannot create temp file'),
                 ('readline', RECIPE, 'Error while copying recipe file'),
                 ('write', TEMP, 'Error while copying recipe file'),
                 ('close_read', RECIPE, 'Error while copying recipe file'),
                 ('close_temp', TEMP, 'Error while copying recipe file')]
        for action, path, message in cases:
            result, backend, operation = self.run_case('FAILED', failures=self.fail(action, path))
            self.assertIn(message, str(result.error))
            self.assertEqual(backend.files[RECIPE], OLD)
            self.assertNotIn(TEMP, backend.files)
            self.assertNotIn(BACKUP, backend.files)
            if action not in ('open_read', 'create_temp'):
                self.assertEqual(backend.requests[-1].operation, 'remove')

    def test_copy_cleanup_remove_failure_masks_marker_error_and_leaves_partial_temp(self):
        original = START + b'unterminated old block\n'
        result, backend, operation = self.run_case('FAILED', recipe=original,
            failures=self.fail('remove', TEMP))
        self.assertIn('injected remove', str(result.error))
        self.assertNotIn('Missing makesum end marker', str(result.error))
        self.assertEqual(backend.files[TEMP], BLOCK)
        self.assertEqual(backend.files[RECIPE], original)
        self.assertNotIn(BACKUP, backend.files)

    def test_later_write_failure_retains_earlier_bytes_if_cleanup_fails(self):
        class LaterWriteFailure(MemoryRecipeMutationBackend):
            def apply(self, request):
                if request.operation == 'write' and request.data == b'do-checksum:\n':
                    self.failures[('write', TEMP, None)] = RecipeMutationResult('FAILED', 'disk full')
                return super(LaterWriteFailure, self).apply(request)
        result, backend, operation = self.run_case('FAILED', backend_type=LaterWriteFailure,
            failures=self.fail('remove', TEMP))
        self.assertEqual(backend.files[TEMP], b'prefix\x00\xff\r\n' + START)
        self.assertEqual(backend.files[RECIPE], OLD)

    def test_backup_removal_failure_preserves_both_original_and_old_backup(self):
        for cleanup_fails in (False, True):
            failures = self.fail('remove', BACKUP)
            if cleanup_fails:
                failures.update(self.fail('remove', TEMP))
            result, backend, operation = self.run_case('FAILED',
                files={BACKUP: b'old backup'}, failures=failures)
            self.assertIn('Cannot delete backup recipe', str(result.error))
            self.assertEqual(backend.files[RECIPE], OLD)
            self.assertEqual(backend.files[BACKUP], b'old backup')
            self.assertEqual(TEMP in backend.files, cleanup_fails)
            self.assertEqual(self.mutations(backend),
                             [('remove', BACKUP, None), ('remove', TEMP, None)])

    def test_original_rename_failure_keeps_original_but_old_backup_is_already_gone(self):
        for cleanup_fails in (False, True):
            failures = self.fail('rename', RECIPE, BACKUP)
            if cleanup_fails:
                failures.update(self.fail('remove', TEMP))
            result, backend, operation = self.run_case('FAILED',
                files={BACKUP: b'old backup'}, failures=failures)
            self.assertIn('Cannot rename recipe "', str(result.error))
            self.assertEqual(backend.files[RECIPE], OLD)
            self.assertNotIn(BACKUP, backend.files)
            self.assertEqual(TEMP in backend.files, cleanup_fails)
            self.assertEqual(self.mutations(backend), [('remove', BACKUP, None),
                ('rename', RECIPE, BACKUP), ('remove', TEMP, None)])

    def test_final_rename_failure_never_reaches_restoration_success_or_failure(self):
        for restoration_fails in (False, True):
            failures = self.fail('rename', TEMP, RECIPE)
            if restoration_fails:
                failures.update(self.fail('rename', BACKUP, RECIPE))
            result, backend, operation = self.run_case('FAILED', failures=failures)
            self.assertIn('Cannot rename recipe to "main.aap": injected rename', str(result.error))
            self.assertNotIn('It is now called', str(result.error))
            self.assertNotIn(RECIPE, backend.files)
            self.assertEqual(backend.files[BACKUP], OLD)
            self.assertEqual(backend.files[TEMP], NEW)
            self.assertEqual(self.mutations(backend),
                             [('rename', RECIPE, BACKUP), ('rename', TEMP, RECIPE)])

    def test_final_temp_cleanup_failure_is_ignored_after_success(self):
        result, backend, operation = self.run_case(failures=self.fail('remove', TEMP))
        self.assertEqual(backend.files[RECIPE], NEW)
        self.assertEqual(backend.files[BACKUP], OLD)

    def test_unavailable_mid_mutation_blocks_with_partial_state(self):
        result, backend, operation = self.run_case('BLOCKED',
            failures=self.fail('rename', TEMP, RECIPE, 'UNAVAILABLE'))
        self.assertNotIn(RECIPE, backend.files)
        self.assertEqual(backend.files[TEMP], NEW)
        self.assertEqual(backend.files[BACKUP], OLD)

    def test_nano_makesum_hashes_controlled_bytes_and_preserves_exact_recipe(self):
        base = '/authorized/ports/editors/nano/'
        with io.open(os.path.join(ROOT, 'tests/fixtures/ports/editors/nano/main.aap'), 'rb') as stream:
            original = stream.read()
        for after_default in (False, True):
            driver, writer, reader, saved, process = fixture()
            if after_default:
                self.assertEqual(driver.build().status, 'COMPLETE')
            before = len(process.requests)
            result = driver.build('makesum')
            self.assertEqual(result.status, 'COMPLETE', str(result.error))
            self.assertEqual(len(process.requests), before)
            self.assertEqual([b.target.name for b in result.bodies], ['makesum'])
            operation = result.bodies[0].port_operations[0]
            self.assertEqual(operation.span.source_id, '<port defaults: /authorized/ports/editors/nano>')
            self.assertEqual(operation.span.start.line, 59)
            self.assertEqual(driver.scope.get_work().top_recipe, 'main.aap')
            digest = hashlib.md5(b'synthetic archive').hexdigest().encode('ascii')
            self.assertNotEqual(digest, b'cc9e42c4805193f9dc3ae22b9644bb1d')
            expected = original.replace(b'cc9e42c4805193f9dc3ae22b9644bb1d', digest)
            backend = driver.port_runtime.recipe_mutations
            self.assertEqual(backend.files[base + 'main.aap'], expected)
            self.assertEqual(backend.files[base + 'main.aap~'], original)
            self.assertNotIn(base + 'main.aap1', backend.files)
            self.assertEqual(operation.checksums[0][1], digest.decode('ascii'))
            self.assertIn(('read', base + 'distfiles/nano-7.1.tar.gz'),
                          driver.port_runtime.artifacts.observations)
            self.assertEqual(self.mutations(backend), [
                ('rename', base + 'main.aap', base + 'main.aap~'),
                ('rename', base + 'main.aap1', base + 'main.aap'),
                ('remove', base + 'main.aap1', None)])
            self.assertEqual(operation.processes, [])
            self.assertEqual(saved.writes, [])


if __name__ == '__main__':
    unittest.main()
