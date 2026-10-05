"""Port.port_patch archive-mode empty-list and marker behavior.

All effects are in-memory. Nonempty items lacking their required fixture
values, item attributes, and CVS remain gated in these scenarios.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
                           MemoryPersistence, PortRuntime, MemoryMarkers, MarkerBackend)
from aap_semantics.values import UnavailableValue


class PatchTests(unittest.TestCase):
    def make(self, variables=None, markers=None, recipe=None):
        scope = Scope.top_level()
        scope.local.update(variables or {})
        runtime = PortRuntime(markers=MemoryMarkers() if markers is None else markers)
        text = recipe or ('all:\n  BEFORE = yes\n  @port_patch(globals())\n'
                          '  AFTER = yes\n  :mkdir {force} done\n  :touch {force} done/patch\n')
        data = Evaluator(scope).run(lower(parse(Source('/recipe/main.aap', text))))
        saved = MemoryPersistence()
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, scope, data.declarations,
                             port_runtime=runtime, port_defaults=False)
        return driver, runtime, saved

    def test_empty_patch_success_and_markers_follow_helper(self):
        driver, runtime, saved = self.make({'PATCHFILES': ''})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        body = result.bodies[0]
        self.assertEqual([p.operation for p in body.port_operations], ['port_patch', 'mkdir', 'touch'])
        self.assertTrue(all(p.status == 'COMPLETED' for p in body.port_operations))
        self.assertEqual(body.scope.local['AFTER'], 'yes')
        self.assertEqual(body.context.cwd, '/recipe')
        self.assertIs(body.port_operations[0].scope, body.scope)
        self.assertEqual(body.processes, ())
        self.assertEqual(runtime.markers.files, {'/recipe/done/patch': b''})
        self.assertEqual(saved.writes, [])
        self.assertNotIn('AFTER', driver.scope.local)

    def test_missing_patch_list_is_historical_noop_not_missing_required_input(self):
        driver, runtime, saved = self.make()
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertNotIn('PATCHFILES', driver.scope.local)

    def test_unused_paths_and_commands_are_not_read(self):
        values = dict((key, UnavailableValue('must not read ' + key)) for key in
                      ('PATCHDISTDIR', 'PATCHCMD', 'PATCHDIR', 'WRKSRC', 'WRKDIR', 'CVSPATCHFILES'))
        values['PATCHFILES'] = ''
        driver, runtime, saved = self.make(values)
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertIsNone(runtime.artifacts)

    def test_helper_itself_has_no_marker_effect(self):
        driver, runtime, saved = self.make(recipe='all:\n  @port_patch(globals())\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(runtime.markers.operations, [])
        self.assertEqual(runtime.markers.files, {})
        self.assertFalse(result.bodies[0].graph_changed)
        self.assertFalse(result.bodies[0].declarations_changed)

    def test_nonempty_without_required_fixture_values_blocks_without_marker(self):
        for value in ('fix.diff', 'fix.diff{patchdir=src}', ' ', ['fix.diff'], UnavailableValue('deferred')):
            driver, runtime, saved = self.make({'PATCHFILES': value})
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED')
            self.assertEqual(result.bodies[0].port_operations[0].status, 'BLOCKED')
            self.assertNotIn('AFTER', result.bodies[0].scope.local)
            self.assertEqual(driver.completed, {})
            self.assertEqual(runtime.markers.operations, [])
            self.assertEqual(saved.writes, [])

    def test_cvs_marker_takes_precedence_and_is_gated(self):
        markers = MemoryMarkers({'/recipe/done/cvs-yes': b'', '/recipe/done/cvs-no': b''})
        driver, runtime, saved = self.make({'PATCHFILES': '', 'CVSPATCHFILES': ''}, markers)
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertNotIn('/recipe/done/patch', markers.files)
        self.assertEqual(markers.operations, [])

    def test_cvs_variable_selection_and_explicit_archive_override(self):
        driver, runtime, saved = self.make({'CVSMODULES': 'module'})
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        driver, runtime, saved = self.make({'CVSMODULES': 'module', 'CVS': 'no'})
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        driver, runtime, saved = self.make({'CVSMODULES': 'module'},
            MemoryMarkers({'/recipe/done/cvs-no': b''}))
        self.assertEqual(driver.build('all').status, 'COMPLETE')

    def test_missing_marker_observation_capability_blocks(self):
        driver, runtime, saved = self.make(markers=MarkerBackend())
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].port_operations[0].reason, 'capability_unavailable')
        self.assertEqual(driver.completed, {})
        self.assertEqual(saved.writes, [])

    def test_marker_read_error_fails_with_helper_source(self):
        class BadMarkers(MemoryMarkers):
            def marker_exists(self, path): raise OSError('observation failed')
        driver, runtime, saved = self.make(markers=BadMarkers())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.span.source_id, '/recipe/main.aap')
        self.assertEqual(result.span.start.line, 3)
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(saved.writes, [])

    def test_invalid_marker_observation_fails(self):
        class BadMarkers(MemoryMarkers):
            def marker_exists(self, path): return None
        driver, runtime, saved = self.make(markers=BadMarkers())
        self.assertEqual(driver.build('all').status, 'FAILED')
        self.assertEqual(runtime.markers.operations, [])

    def test_failed_marker_write_is_not_successful_target_completion(self):
        class BadMarkers(MemoryMarkers):
            def touch(self, path): raise OSError('touch failed')
        driver, runtime, saved = self.make(markers=BadMarkers())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.bodies[0].port_operations[0].status, 'COMPLETED')
        self.assertEqual(result.bodies[0].port_operations[-1].status, 'FAILED')
        self.assertIn('/recipe/done', runtime.markers.directories)
        self.assertNotIn('/recipe/done/patch', runtime.markers.files)
        self.assertEqual(saved.writes, [])
        self.assertEqual(driver.completed, {})

    def test_live_build_local_override_does_not_write_definition_scope(self):
        driver, runtime, saved = self.make({'PATCHFILES': 'parent.diff', 'sysresult': 77},
            recipe='all:\n  PATCHFILES =\n  @port_patch(globals())\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(driver.scope.local['PATCHFILES'], 'parent.diff')
        self.assertEqual(driver.scope.local['sysresult'], 77)
        self.assertNotIn('sysresult', result.bodies[0].scope.local)
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')

    def test_nested_block_and_failure_keep_update_and_helper_sites(self):
        class BadMarkers(MemoryMarkers):
            def marker_exists(self, path): raise OSError('cannot observe')
        recipe = ('child {virtual}:\n  @port_patch(globals())\n'
                  '  :mkdir {force} done\n  :touch {force} done/patch\n'
                  'all:\n  :update child\n  AFTER = no\n')
        for values, markers, status in (({'PATCHFILES': 'x'}, MemoryMarkers(), 'BLOCKED'),
                                       ({}, BadMarkers(), 'FAILED')):
            driver, runtime, saved = self.make(values, markers, recipe)
            result = driver.build('all')
            self.assertEqual(result.status, status)
            self.assertEqual(result.span.start.line, 2)
            update = result.bodies[0].update_failure
            self.assertEqual(update.request.span.start.line, 6)
            self.assertEqual(update.target, 'child')
            self.assertNotIn('AFTER', result.bodies[0].scope.local)
            self.assertNotIn('/recipe/done/patch', markers.files)
            self.assertEqual(saved.writes, [])

    def test_done_patch_does_not_skip_a_direct_helper_call(self):
        markers = MemoryMarkers({'/recipe/done/patch': b'existing'})
        driver, runtime, saved = self.make({'PATCHFILES': 'new.diff'}, markers)
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(markers.files['/recipe/done/patch'], b'existing')
        self.assertEqual(markers.operations, [])


if __name__ == '__main__':
    unittest.main()
