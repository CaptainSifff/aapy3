"""Port.py:217-240 and Util.py:704-714, with injected deletion only."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
                           MemoryPersistence, PortRuntime, MemoryMarkers,
                           MemoryDeleteBackend, DeleteBackend, DeleteResult,
                           PathRequest)
from nano_target_frontier import fixture


BASE = '/recipe/'
CLEAN = [BASE + name for name in ('done', 'work', 'pkg-plist',
         'pkg-comment', 'pkg-descr')]
CLEAN.insert(2, '/absolute/pack')
EXTRA = [BASE + name for name in ('distfiles', 'patches', 'demo-1.tgz', 'AAPDIR')]


class PortCleanTests(unittest.TestCase):
    def make(self, helper='port_clean', entries=None, missing=None, failures=None,
             deletions=None, variables=None, stores=()):
        scope = Scope.top_level(port_defaults=True)
        scope.local.update({'WRKDIR': 'work', 'PKGDIR': '/absolute/pack',
                            'DISTDIR': 'distfiles', 'PORTNAME': 'demo',
                            'PORTVERSION': '1'})
        scope.local.update(variables or {})
        if missing is None:
            missing = CLEAN + EXTRA
        backend = deletions if deletions is not None else MemoryDeleteBackend(
            entries, missing, stores=stores, failures=failures)
        runtime = PortRuntime(markers=MemoryMarkers(), deletions=backend)
        source = Source('/recipe/main.aap',
                        'all {virtual}:\n  @' + helper + '(globals())\n')
        data = Evaluator(scope).run(lower(parse(source)))
        driver = BuildDriver(data.graph, MemoryTargetState(), MemoryPersistence(),
                             scope, data.declarations, port_runtime=runtime,
                             port_defaults=False, path_observer=backend)
        return driver, backend

    def test_historical_default_is_bound_before_recipe_evaluation(self):
        scope = Scope.top_level(port_defaults=True)
        self.assertEqual(scope.lookup('PATCHDISTDIR'), 'patches')
        program = lower(parse(Source('/recipe/main.aap', 'PATCHDISTDIR ?= other\n')))
        Evaluator(scope).run(program)
        self.assertEqual(scope.lookup('PATCHDISTDIR'), 'patches')

    def test_clean_checks_order_and_only_deletes_observed_existing_paths(self):
        driver, backend = self.make(entries={CLEAN[1]: 'directory',
                                             CLEAN[2]: 'directory'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        operation = result.bodies[0].port_operations[0]
        self.assertEqual(operation.paths, CLEAN)
        self.assertEqual([record.observation.status for record in operation.observations],
                         ['MISSING', 'EXISTS', 'EXISTS', 'MISSING', 'MISSING', 'MISSING'])
        self.assertEqual([request.path for request in backend.requests], CLEAN[1:3])
        self.assertEqual(backend.entries, {})
        self.assertEqual(result.bodies[0].processes, ())

    def test_distclean_extends_clean_order_and_keeps_absolute_package_path(self):
        driver, backend = self.make('port_distclean', entries={
            CLEAN[1]: 'directory', CLEAN[2]: 'directory',
            EXTRA[0]: 'directory', EXTRA[1]: 'directory',
            EXTRA[2]: 'file', EXTRA[3]: 'directory'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].port_operations[0].paths, CLEAN + EXTRA)
        self.assertEqual([request.path for request in backend.requests],
                         [CLEAN[1], CLEAN[2]] + EXTRA)
        self.assertEqual(backend.entries, {})

    def test_distclean_revision_and_explicit_patch_directory(self):
        driver, backend = self.make('port_distclean', variables={
            'PORTREVISION': '2', 'PATCHDISTDIR': '/absolute/patches'},
            missing=CLEAN + EXTRA + ['/absolute/patches', BASE + 'demo-1_2.tgz'])
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].port_operations[0].paths[-4:],
                         [EXTRA[0], '/absolute/patches', BASE + 'demo-1_2.tgz', EXTRA[3]])

    def test_missing_paths_skip_and_unknown_observation_blocks(self):
        driver, backend = self.make(entries={}, missing=CLEAN)
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(backend.requests, [])
        driver, backend = self.make(entries={CLEAN[1]: 'directory'},
                                    missing=[CLEAN[0]])
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([request.path for request in backend.requests], [CLEAN[1]])
        self.assertEqual(result.bodies[0].port_operations[0].status, 'BLOCKED')
        self.assertNotIn(CLEAN[1], backend.entries)

    def test_unavailable_delete_blocks_without_removing_existing_path(self):
        driver, backend = self.make(entries={CLEAN[1]: 'directory'},
            failures={CLEAN[1]: DeleteResult('UNAVAILABLE', 'no delete adapter')})
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertIn(CLEAN[1], backend.entries)
        self.assertEqual(result.bodies[0].port_operations[0].deletions[0][1].status,
                         'UNAVAILABLE')

    def test_absent_delete_adapter_blocks_without_host_fallback(self):
        driver, backend = self.make(entries={CLEAN[1]: 'directory'})
        driver.port_runtime.deletions = DeleteBackend()
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertIn(CLEAN[1], backend.entries)

    def test_later_delete_failure_preserves_earlier_effect(self):
        driver, backend = self.make(entries={CLEAN[1]: 'directory', CLEAN[2]: 'directory'},
            failures={CLEAN[2]: DeleteResult('FAILED', 'permission denied')})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertNotIn(CLEAN[1], backend.entries)
        self.assertIn(CLEAN[2], backend.entries)
        self.assertEqual([request.path for request in backend.requests], CLEAN[1:3])
        self.assertIn('Cannot delete "/absolute/pack"', str(result.error))

    def test_recursive_directory_and_symlink_remove_link_not_target(self):
        files = {CLEAN[1] + '/nested/file': b'old', '/outside/file': b'keep'}
        driver, backend = self.make(entries={CLEAN[1]: 'directory',
            CLEAN[1] + '/nested': 'directory', CLEAN[1] + '/nested/file': 'file',
            CLEAN[2]: ('symlink', '/outside'), '/outside': 'directory',
            '/outside/file': 'file'},
            stores=[files])
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(files, {'/outside/file': b'keep'})
        self.assertEqual(backend.entries,
                         {'/outside': 'directory', '/outside/file': 'file'})
        request = PathRequest('work/nested/file', '/recipe', result.bodies[0].context)
        self.assertEqual(backend.observe(request).status, 'MISSING')
        request = PathRequest('/outside/file', '/recipe', result.bodies[0].context)
        self.assertEqual(backend.observe(request).status, 'EXISTS')

    def test_dangling_symlink_is_skipped_by_historical_exists_gate(self):
        driver, backend = self.make(entries={CLEAN[2]: ('symlink', '/missing')},
                                     missing=CLEAN + ['/missing'])
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(backend.requests, [])
        self.assertIn(CLEAN[2], backend.entries)

    def test_nano_clean_and_distclean_use_explicit_shared_memory_state(self):
        base = '/authorized/ports/editors/nano/'
        cases = (('clean', [base + 'work', base + 'pack']),
                 ('distclean', [base + 'work', base + 'pack', base + 'distfiles']))
        for target, expected in cases:
            driver, writer, reader, saved, process = fixture()
            result = driver.build(target)
            self.assertEqual(result.status, 'COMPLETE', target)
            deletion = driver.port_runtime.deletions
            self.assertEqual([request.path for request in deletion.requests], expected)
            self.assertEqual(len(process.requests), 3)  # setup :syseval only
            self.assertNotIn(base + 'work/post_i', writer.files)
            self.assertEqual(deletion.observe(PathRequest('work', base, result.bodies[0].context)).status,
                             'MISSING')
            if target == 'distclean':
                self.assertNotIn(base + 'distfiles/nano-7.1.tar.gz',
                                 driver.port_runtime.artifacts.files)
            self.assertEqual(saved.writes, [])

    def test_nano_after_default_deletes_done_markers_first(self):
        driver, writer, reader, saved, process = fixture()
        self.assertEqual(driver.build().status, 'COMPLETE')
        self.assertTrue(driver.port_runtime.markers.files)
        before = len(process.requests)
        result = driver.build('clean')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(len(process.requests), before)
        self.assertEqual(driver.port_runtime.deletions.requests[0].path,
                         '/authorized/ports/editors/nano/done')
        self.assertEqual(driver.port_runtime.markers.files, {})


if __name__ == '__main__':
    unittest.main()
