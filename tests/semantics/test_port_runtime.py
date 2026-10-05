"""Port.port_fetch and Commands marker semantics with in-memory capabilities."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics.actions import ActionRuntime, ActionBackend, ActionResult, MemoryActionWorkspace
from aap_semantics.port_commands import (PortCommandRuntime, PortCommandPolicy,
                                        PortCommandRequest, MemoryPortDirectories)

from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
    MemoryPersistence, PortRuntime, PortMessage, MarkerBackend, MemoryMarkers, MemoryArtifacts,
    ProcessBackend, ProcessResult, ProcessPolicy, ProcessBackendError, ProcessUnavailable, ChecksumBackend)


class PortRuntimeTests(unittest.TestCase):
    def make(self, body='@port_fetch(globals())\n', variables=None, files=None,
             markers=None, directory='/recipe'):
        scope = Scope.top_level()
        scope.local.update({'DISTFILES': 'archive.tgz', 'DISTDIR': 'dist',
                            'PATCHFILES': '', 'MASTER_SITES': 'https://invalid.example'})
        scope.local.update(variables or {})
        artifacts = MemoryArtifacts({directory + '/dist/archive.tgz': b'fixture'}
                                    if files is None else files)
        runtime = PortRuntime(artifacts, markers)
        if markers is None:
            runtime.markers = MemoryMarkers()
        text = 'all:\n' + ''.join('  ' + line + '\n' for line in body.splitlines())
        data = Evaluator(scope).run(lower(parse(Source(directory + '/main.aap', text))))
        saved = MemoryPersistence()
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, scope, data.declarations,
                             port_runtime=runtime)
        return driver, runtime, saved

    def test_present_archive_success_and_local_globals(self):
        driver, runtime, saved = self.make('@port_fetch(globals())\nAFTER = $EXTRACTFILES\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        body = result.bodies[0]
        self.assertEqual(body.scope.local['AFTER'], 'archive.tgz')
        self.assertNotIn('EXTRACTFILES', driver.scope.local)
        self.assertIs(body.port_operations[0].scope, body.scope)
        self.assertEqual(body.port_operations[0].status, 'COMPLETED')
        self.assertEqual(runtime.artifacts.observations, [('exists', '/recipe/dist/archive.tgz')])
        self.assertEqual(runtime.markers.files, {'/recipe/done/cvs-no': b''})
        self.assertEqual(saved.writes, [])
        self.assertEqual(body.context.cwd, '/recipe')

    def test_existing_extractfiles_preserved(self):
        driver, runtime, saved = self.make(variables={'EXTRACTFILES': 'chosen.tgz'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertNotIn('EXTRACTFILES', result.bodies[0].scope.local)
        self.assertEqual(driver.scope.local['EXTRACTFILES'], 'chosen.tgz')

    def test_archive_and_patch_basename_destinations(self):
        driver, runtime, saved = self.make(variables={
            'DISTFILES': 'remote/one.tgz two.tgz', 'PATCHFILES': 'sub/fix.diff',
            'PATCHDISTDIR': '/patches', 'PATCH_SITES': 'file://patches'}, files={
                '/recipe/dist/one.tgz': b'1', '/recipe/dist/two.tgz': b'2', '/patches/fix.diff': b'3'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].port_operations[0].paths,
                         ['/recipe/dist/one.tgz', '/recipe/dist/two.tgz', '/patches/fix.diff'])

    def test_empty_distfiles_still_records_archive_mode(self):
        driver, runtime, saved = self.make(variables={'DISTFILES': ''}, files={})
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(runtime.artifacts.observations, [])
        self.assertIn('/recipe/done/cvs-no', runtime.markers.files)

    def test_diagnostic_port_helpers_emit_typed_events_without_effects(self):
        cases = (('port_checksum', 'extra',
                  'No do-checksum target defined; checking checksums skipped'),
                 ('port_installtest', 'extra', 'Default installtest: do nothing'),
                 ('port_srcpackage', 'info', 'TODO: srcpackage'))
        for name, kind, text in cases:
            driver, runtime, saved = self.make('@' + name + '(globals())\n',
                                                variables={'CHECKSUM_SENTINEL': 'kept'},
                                                files={})
            result = driver.build('all')
            self.assertEqual(result.status, 'COMPLETE', name)
            self.assertEqual(len(result.bodies[0].port_operations), 1)
            operation = result.bodies[0].port_operations[0]
            self.assertEqual(operation.operation, name)
            self.assertEqual(operation.status, 'COMPLETED')
            self.assertEqual(operation.cwd, '/recipe')
            self.assertIs(operation.scope, result.bodies[0].scope)
            self.assertEqual(len(operation.messages), 1)
            message = operation.messages[0]
            self.assertIsInstance(message, PortMessage)
            self.assertEqual((message.kind, message.text), (kind, text))
            self.assertEqual(message.span.source_id, '/recipe/main.aap')
            self.assertEqual(message.span.start.line, 2)
            self.assertEqual(operation.paths, [])
            self.assertEqual(operation.processes, [])
            self.assertEqual(operation.scope.lookup('CHECKSUM_SENTINEL'), 'kept')
            self.assertEqual(runtime.artifacts.observations, [])
            self.assertEqual(runtime.markers.operations, [])
            self.assertEqual(saved.writes, [])

    def test_diagnostic_helper_policy_does_not_approve_other_helpers_or_arguments(self):
        for body in ('@port_clean(globals())', '@port_distclean(globals())',
                     '@port_makesum(1)', '@port_installtest(1)',
                     '@port_srcpackage(1)', '@port_checksum(1)'):
            driver, runtime, saved = self.make(body + '\n', files={})
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED', body)
            self.assertEqual(runtime.artifacts.observations, [])
            self.assertEqual(runtime.markers.operations, [])

    def test_generated_default_checksum_continues_to_its_marker(self):
        scope = Scope.top_level(port_defaults=True)
        scope.local.update({'PORTNAME': 'fixture', 'PORTVERSION': '1',
                            'PORTCOMMENT': 'fixture', 'PORTDESCR': 'fixture',
                            'DISTFILES': '', 'PATCHFILES': '', 'MASTER_SITES': '',
                            'PATCH_SITES': '', 'DISTDIR': 'dist', 'PATCHDISTDIR': 'patch',
                            'WRKDIR': 'work', 'PKGDIR': 'pack'})
        data = Evaluator(scope).run(lower(parse(Source(
            '/recipe/main.aap', 'all:\n  :pass\ndo-dependcheck:\n  :pass\n'
            'do-fetchdepend:\n  :pass\n'))))
        markers = MemoryMarkers()
        artifacts = MemoryArtifacts({})
        runtime = PortRuntime(artifacts, markers)
        saved = MemoryPersistence()
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, scope,
                             data.declarations, port_runtime=runtime)
        result = driver.build('checksum')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        checksum = [body for body in result.bodies if body.target.name == 'checksum'][0]
        helper = checksum.port_operations[0]
        self.assertEqual(helper.operation, 'port_checksum')
        self.assertEqual(helper.status, 'COMPLETED')
        self.assertEqual((helper.messages[0].kind, helper.messages[0].text),
                         ('extra', 'No do-checksum target defined; checking checksums skipped'))
        self.assertEqual(helper.paths, [])
        self.assertEqual(helper.observations, [])
        self.assertEqual(helper.mutations, [])
        self.assertEqual(helper.processes, [])
        self.assertEqual(artifacts.observations, [])
        self.assertIn('/recipe/done/checksum', markers.files)
        self.assertIn(('mkdir', '/recipe/done'), markers.operations)
        self.assertIn(('touch', '/recipe/done/checksum'), markers.operations)
        self.assertEqual(saved.writes, [])

    def test_missing_archive_blocks_fetch_without_marker_or_target_success(self):
        driver, runtime, saved = self.make('@port_fetch(globals())\nAFTER = no\n', files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].port_operations[0].status, 'BLOCKED')
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(result.bodies[0].scope.local['EXTRACTFILES'], 'archive.tgz')
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(driver.completed, {})
        self.assertEqual(driver.finish().pending_signatures, ())

    def test_patch_sites_error_precedes_any_observation_or_mutation(self):
        driver, runtime, saved = self.make(variables={'PATCHFILES': 'fix', 'PATCH_SITES': ''})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.bodies[0].port_operations[0].reason, 'semantic_error')
        self.assertEqual(runtime.artifacts.observations, [])
        self.assertNotIn('EXTRACTFILES', result.bodies[0].scope.local)
        self.assertEqual(runtime.markers.files, {})

    def test_exclusive_cvs_marker_is_not_force_touch(self):
        markers = MemoryMarkers({'/recipe/done/cvs-no': b'old'})
        driver, runtime, saved = self.make(markers=markers)
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(markers.files['/recipe/done/cvs-no'], b'old')
        self.assertEqual(markers.times, {})
        self.assertEqual(result.error.span.start.line, 2)
        self.assertEqual(driver.completed, {})

    def test_cvs_precedence_and_unsupported_branch(self):
        for markers, variables in ((MemoryMarkers({'/recipe/done/cvs-yes': b''}), {}),
                                   (MemoryMarkers(), {'CVSMODULES': 'module'})):
            driver, runtime, saved = self.make(markers=markers, variables=variables)
            self.assertEqual(driver.build('all').status, 'BLOCKED')
            self.assertEqual(runtime.artifacts.observations, [])
            self.assertEqual(markers.operations, [])

    def test_cvs_no_or_disabled_cvs_selects_archives(self):
        driver, runtime, saved = self.make(variables={'CVSMODULES': 'module', 'CVS': 'no'})
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        driver, runtime, saved = self.make(variables={'CVSMODULES': 'module'},
                                          markers=MemoryMarkers({'/recipe/done/cvs-no': b''}))
        # Existing cvs-no selects archive mode, then the historic O_EXCL touch fails.
        self.assertEqual(driver.build('all').status, 'FAILED')
        self.assertEqual(len(runtime.artifacts.observations), 1)

    def test_force_marker_commands_preserve_bytes_and_refresh_time(self):
        markers = MemoryMarkers({'/recipe/done/fetch': b'existing'})
        driver, runtime, saved = self.make(':mkdir {force} done\n:touch {force} done/fetch\n'
                                          ':touch {force} done/checksum\n', markers=markers)
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(markers.files['/recipe/done/fetch'], b'existing')
        self.assertEqual(markers.files['/recipe/done/checksum'], b'')
        self.assertLess(markers.times['/recipe/done/fetch'], markers.times['/recipe/done/checksum'])
        self.assertEqual(saved.writes, [])

    def test_mode_mkdir_uses_octal_mode_and_explicit_cwd(self):
        markers = MemoryMarkers()
        markers.directories.add('/other')
        driver, runtime, saved = self.make(':mkdir work {mode = 755}\n',
                                          markers=markers, directory='/other')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        operation = result.bodies[0].port_operations[0]
        self.assertEqual(operation.operation, 'mkdir')
        self.assertEqual(operation.status, 'COMPLETED')
        self.assertEqual(operation.cwd, '/other')
        self.assertEqual(operation.paths, ['/other/work'])
        self.assertEqual(operation.mode, 0o755)
        self.assertEqual(operation.observations,
                         [('path_kind', '/other/work', 'missing')])
        self.assertEqual(markers.operations, [('mkdir', '/other/work', 0o755)])
        self.assertEqual(markers.modes['/other/work'], 0o755)
        self.assertEqual(runtime.artifacts.observations, [])
        self.assertEqual(operation.processes, [])
        self.assertEqual(saved.writes, [])

    def test_mode_mkdir_accepts_nested_local_paths_without_recursive_creation(self):
        for name, parents, expected in (
                ('work/pkg', ('/recipe/work',), '/recipe/work/pkg'),
                ('work/a/pkg', ('/recipe/work', '/recipe/work/a'),
                 '/recipe/work/a/pkg'),
                ('./work/pkg', ('/recipe/work', '/recipe/./work'),
                 '/recipe/./work/pkg'),
                ('../other/pkg', ('/recipe/..', '/recipe/../other'),
                 '/recipe/../other/pkg'),
                ('/absolute/pkg', ('/absolute',), '/absolute/pkg')):
            markers = MemoryMarkers()
            markers.directories.update(('/recipe',) + parents)
            driver, runtime, saved = self.make(':mkdir ' + name + ' {mode = 755}\n',
                                              markers=markers, files={})
            result = driver.build('all')
            self.assertEqual(result.status, 'COMPLETE', name + ': ' + str(result.error))
            operation = result.bodies[0].port_operations[0]
            self.assertEqual(operation.paths, [expected])
            self.assertEqual(operation.mode, 0o755)
            self.assertEqual(markers.operations, [('mkdir', expected, 0o755)])
            self.assertFalse(getattr(operation, 'recursive', False))

    def test_mode_mkdir_missing_parent_or_intermediate_file_fails_without_creation(self):
        for name, files, parents in (
                ('work/pkg', {}, ()),
                ('work/a/pkg', {}, ('/recipe/work',)),
                ('work/file/pkg', {'/recipe/work/file': b'old'},
                 ('/recipe/work',))):
            markers = MemoryMarkers(files)
            markers.directories.update(('/recipe',) + parents)
            driver, runtime, saved = self.make(':mkdir ' + name + ' {mode = 755}\n',
                                              markers=markers, files={})
            result = driver.build('all')
            self.assertEqual(result.status, 'FAILED', name)
            self.assertEqual(result.bodies[0].port_operations[0].reason,
                             'port_operation_failed')
            self.assertEqual(markers.operations, [])
            self.assertNotIn('/recipe/work/pkg', markers.directories)
            self.assertNotIn('/recipe/work/a', markers.directories)

    def test_mode_mkdir_nested_existing_path_and_unreached_force_stay_gated(self):
        for kind in ('directory', 'file'):
            markers = MemoryMarkers({'/recipe/work/pkg': b'old'}
                                    if kind == 'file' else None)
            markers.directories.update(('/recipe', '/recipe/work'))
            if kind == 'directory':
                markers.directories.add('/recipe/work/pkg')
            driver, runtime, saved = self.make(':mkdir work/pkg {mode = 755}\n',
                                              markers=markers, files={})
            result = driver.build('all')
            self.assertEqual(result.status, 'FAILED', kind)
            self.assertEqual(result.bodies[0].port_operations[0].reason,
                             'semantic_error')
            self.assertEqual(markers.operations, [])

            driver, runtime, saved = self.make(':mkdir {force} work/pkg {mode = 755}\n',
                                              markers=markers, files={})
            self.assertEqual(driver.build('all').status, 'BLOCKED')
            self.assertEqual(markers.operations, [])

    def test_mode_mkdir_malformed_and_remote_path_remain_gated(self):
        for command in (':mkdir work/pkg {mode = 758}',
                        ':mkdir https://example.invalid/pkg {mode = 755}',
                        ':mkdir ~/pkg {mode = 755}'):
            markers = MemoryMarkers()
            markers.directories.add('/recipe')
            driver, runtime, saved = self.make(command + '\n',
                                              markers=markers, files={})
            self.assertEqual(driver.build('all').status, 'BLOCKED', command)
            self.assertEqual(markers.operations, [])

    def test_unreached_plain_mkdir_without_mode_remains_gated(self):
        # This milestone extends the already reached mode-bearing form only.
        markers = MemoryMarkers()
        markers.directories.add('/recipe')
        driver, runtime, saved = self.make(':mkdir simple\n',
                                          markers=markers, files={})
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(markers.operations, [])

    def test_monitoring_extract_mkdir_uses_workdir_and_octal_mode(self):
        cwd = '/work/repo/ports/company/monitoring'
        markers = MemoryMarkers()
        markers.directories.add(cwd)
        driver, runtime, saved = self.make(
            ':mkdir work {mode = 755}\n'
            ':mkdir work/$WRKSRC {mode = 755}\n',
            variables={'WRKSRC': 'monitoring-145'}, markers=markers,
            directory=cwd, files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        expected = cwd + '/work/monitoring-145'
        self.assertEqual(markers.operations,
                         [('mkdir', cwd + '/work', 0o755),
                          ('mkdir', expected, 0o755)])
        self.assertEqual(result.bodies[0].port_operations[1].paths, [expected])
        self.assertEqual(markers.path_kind(expected), 'directory')

    def test_mode_mkdir_existing_directory_or_file_fails_without_chmod(self):
        for kind in ('directory', 'file'):
            markers = MemoryMarkers({'/recipe/work': b'old'} if kind == 'file' else None)
            markers.directories.add('/recipe')
            if kind == 'directory':
                markers.directories.add('/recipe/work')
                markers.modes['/recipe/work'] = 0o700
            driver, runtime, saved = self.make(':mkdir work {mode = 755}\n',
                                              markers=markers, files={})
            result = driver.build('all')
            self.assertEqual(result.status, 'FAILED', kind)
            operation = result.bodies[0].port_operations[0]
            self.assertEqual(operation.reason, 'semantic_error')
            self.assertEqual(operation.observations,
                             [('path_kind', '/recipe/work',
                               'directory' if kind == 'directory' else 'other')])
            self.assertEqual(markers.operations, [])
            self.assertEqual(markers.modes.get('/recipe/work'),
                             0o700 if kind == 'directory' else None)
            self.assertEqual(result.error.span.source_id, '/recipe/main.aap')
            self.assertEqual(result.error.span.start.line, 2)
            self.assertEqual(runtime.artifacts.observations, [])

    def test_mode_mkdir_unavailable_and_failed_capabilities(self):
        driver, runtime, saved = self.make(':mkdir work {mode = 755}\n',
                                          markers=MarkerBackend(), files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].port_operations[0].reason,
                         'capability_unavailable')
        self.assertEqual(result.error.span.start.line, 2)

        class NoMutation(MemoryMarkers):
            def mkdir(self, path, mode=None):
                raise NotImplementedError('directory mutation unavailable')
        markers = NoMutation()
        markers.directories.add('/recipe')
        driver, runtime, saved = self.make(':mkdir work {mode = 755}\n',
                                          markers=markers, files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].port_operations[0].reason,
                         'capability_unavailable')
        self.assertEqual(result.bodies[0].port_operations[0].observations,
                         [('path_kind', '/recipe/work', 'missing')])
        self.assertEqual(markers.operations, [])

        class Failed(MemoryMarkers):
            def mkdir(self, path, mode=None):
                raise OSError('controlled directory failure')
        markers = Failed()
        markers.directories.add('/recipe')
        driver, runtime, saved = self.make(':mkdir work {mode = 755}\n',
                                          markers=markers, files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.bodies[0].port_operations[0].reason,
                         'port_operation_failed')
        self.assertIn('controlled directory failure', str(result.error))
        self.assertEqual(result.error.span.source_id, '/recipe/main.aap')
        self.assertEqual(markers.operations, [])

    def test_recursive_mkdir_creates_missing_components_parent_first(self):
        class Recorded(MemoryMarkers):
            def __init__(self):
                super(Recorded, self).__init__()
                self.requests = []
            def mkdir(self, path, mode=None, require_parent=False):
                self.requests.append((path, mode, require_parent))
                return super(Recorded, self).mkdir(path, mode, require_parent)
        markers = Recorded()
        markers.directories.add('/recipe')
        driver, runtime, saved = self.make(':mkdir {r} tree/one/two\n',
                                          markers=markers, files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        operation = result.bodies[0].port_operations[0]
        self.assertTrue(operation.recursive)
        self.assertIsNone(operation.mode)
        self.assertEqual(operation.paths, ['/recipe/tree/one/two'])
        self.assertEqual(markers.requests,
                         [('/recipe/tree', None, True),
                          ('/recipe/tree/one', None, True),
                          ('/recipe/tree/one/two', None, True)])
        self.assertEqual(markers.operations,
                         [('mkdir', '/recipe/tree'), ('mkdir', '/recipe/tree/one'),
                          ('mkdir', '/recipe/tree/one/two')])
        self.assertEqual(operation.observations,
                         [('path_kind', '/recipe/tree/one/two', 'missing'),
                          ('path_kind', '/recipe', 'directory'),
                          ('path_kind', '/recipe/tree', 'missing'),
                          ('path_kind', '/recipe/tree/one', 'missing')])
        self.assertEqual(runtime.artifacts.observations, [])
        self.assertEqual(operation.processes, [])
        self.assertEqual(saved.writes, [])

    def test_recursive_mkdir_handles_existing_parent_and_absolute_path(self):
        markers = MemoryMarkers()
        markers.directories.update(['/other', '/other/tree', '/absolute'])
        driver, runtime, saved = self.make(':mkdir {r} tree/final\n',
                                          markers=markers, directory='/other', files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].port_operations[0].paths,
                         ['/other/tree/final'])
        self.assertEqual(markers.operations, [('mkdir', '/other/tree/final')])

        markers = MemoryMarkers()
        markers.directories.add('/absolute')
        driver, runtime, saved = self.make(':mkdir {r} /absolute/one/two\n',
                                          markers=markers, directory='/other', files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].port_operations[0].paths,
                         ['/absolute/one/two'])
        self.assertEqual(markers.operations,
                         [('mkdir', '/absolute/one'), ('mkdir', '/absolute/one/two')])

        markers = MemoryMarkers()
        markers.directories.add('/other')
        driver, runtime, saved = self.make(':mkdir {r} $PKGDIR/tftpboot\n',
                                          markers=markers, directory='/other', files={},
                                          variables={'PKGDIR': 'pack'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].port_operations[0].paths,
                         ['/other/pack/tftpboot'])

    def test_recursive_mkdir_existing_final_and_intermediate_file(self):
        for kind in ('directory', 'file'):
            markers = MemoryMarkers({'/recipe/tree/final': b'x'} if kind == 'file' else None)
            markers.directories.update(['/recipe', '/recipe/tree'])
            if kind == 'directory':
                markers.directories.add('/recipe/tree/final')
            driver, runtime, saved = self.make(':mkdir {r} tree/final\n',
                                              markers=markers, files={})
            result = driver.build('all')
            self.assertEqual(result.status, 'FAILED', kind)
            operation = result.bodies[0].port_operations[0]
            self.assertEqual(operation.reason, 'semantic_error')
            self.assertEqual(markers.operations, [])
            self.assertEqual(result.error.span.source_id, '/recipe/main.aap')
            self.assertEqual(result.error.span.start.line, 2)

        markers = MemoryMarkers({'/recipe/tree/file': b'x'})
        markers.directories.update(['/recipe', '/recipe/tree'])
        driver, runtime, saved = self.make(':mkdir {r} tree/file/child\n',
                                          markers=markers, files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        operation = result.bodies[0].port_operations[0]
        self.assertEqual(operation.reason, 'port_operation_failed')
        self.assertEqual(markers.operations, [])
        self.assertIn('directory parent missing', str(result.error))

    def test_recursive_mkdir_preserves_partial_creation_and_capability_results(self):
        class FailsSecond(MemoryMarkers):
            def __init__(self):
                super(FailsSecond, self).__init__()
                self.calls = 0
            def mkdir(self, path, mode=None, require_parent=False):
                self.calls += 1
                if self.calls == 2:
                    raise OSError('second recursive mkdir failed')
                return super(FailsSecond, self).mkdir(path, mode, require_parent)
        markers = FailsSecond()
        markers.directories.add('/recipe')
        driver, runtime, saved = self.make(':mkdir {r} one/two/three\n',
                                          markers=markers, files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        operation = result.bodies[0].port_operations[0]
        self.assertEqual(operation.reason, 'port_operation_failed')
        self.assertIn('/recipe/one', markers.directories)
        self.assertNotIn('/recipe/one/two', markers.directories)
        self.assertEqual(markers.operations, [('mkdir', '/recipe/one')])
        self.assertEqual(result.error.span.source_id, '/recipe/main.aap')

        driver, runtime, saved = self.make(':mkdir {r} one/two\n',
                                          markers=MarkerBackend(), files={})
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].port_operations[0].reason,
                         'capability_unavailable')

    def test_marker_parent_file_is_error(self):
        markers = MemoryMarkers({'/recipe/done': b'not a directory'})
        driver, runtime, saved = self.make(markers=markers)
        self.assertEqual(driver.build('all').status, 'FAILED')
        self.assertEqual(markers.files, {'/recipe/done': b'not a directory'})

    def test_missing_marker_capability_blocks(self):
        driver, runtime, saved = self.make(markers=MarkerBackend())
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].port_operations[0].reason, 'capability_unavailable')

    def test_backend_error_is_failure(self):
        class Broken(MemoryArtifacts):
            def exists(self, path): raise OSError('observation failed')
        driver, runtime, saved = self.make()
        runtime.artifacts = Broken()
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.error.span.source_id, '/recipe/main.aap')
        self.assertEqual(runtime.markers.operations, [])

    def test_marker_failure_leaves_legitimate_earlier_effects(self):
        class Broken(MemoryMarkers):
            def touch(self, path): raise OSError('cannot touch')
        driver, runtime, saved = self.make('@port_fetch(globals())\n:mkdir {force} done\n'
                                          ':touch {force} done/fetch\nAFTER = no\n', markers=Broken())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertIn('/recipe/done/cvs-no', runtime.markers.files)
        self.assertNotIn('/recipe/done/fetch', runtime.markers.files)
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(result.pending_signatures, ())

    def test_globals_capability_cannot_escape(self):
        for body in ('@x = globals()', '@port_fetch(1)', '@port_fetch(globals(1))',
                     '@port_fetch(globals(), globals())', '@port_extract(globals())',
                     '@globals = 1\n@port_fetch(globals())'):
            driver, runtime, saved = self.make(body)
            self.assertEqual(driver.build('all').status, 'BLOCKED', body)
            self.assertEqual(runtime.markers.operations, [])

    def test_general_filesystem_commands_stay_blocked(self):
        for body in (':mkdir {force} output', ':touch {force} done/../outside',
                     ':touch {force} arbitrary', ':mkdir done', ':touch {force} done/unknown'):
            driver, runtime, saved = self.make(body)
            self.assertEqual(driver.build('all').status, 'BLOCKED', body)
            self.assertEqual(runtime.markers.operations, [])

    def test_archive_attributes_and_deferred_values_block(self):
        for value in ('one {distdir=other}', '*.tgz'):
            driver, runtime, saved = self.make(variables={'DISTFILES': value})
            self.assertEqual(driver.build('all').status, 'BLOCKED')
            self.assertEqual(runtime.markers.files, {})

    def test_nested_helper_failure_and_caller_scope_cwd(self):
        driver, runtime, saved = self.make(':update nested\nAFTER = no\n')
        Evaluator(driver.scope, graph=driver.graph, declarations=driver.declarations).run(
            lower(parse(Source('/other/defs.aap', 'nested {virtual}:\n  @port_fetch(globals())\n'))))
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')  # /other/dist archive is missing
        nested = result.bodies[0].updates[0].builds[0].bodies[0]
        self.assertIs(nested.context.port_runtime, runtime)
        self.assertEqual(nested.context.cwd, '/other')
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertIs(nested.context.invocation_scope, result.bodies[0].scope)
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(result.bodies[0].update_failure.span.source_id, '/other/defs.aap')
        self.assertEqual(saved.writes, [])

    def test_nested_hard_failure_is_not_completion(self):
        markers = MemoryMarkers({'/recipe/done/cvs-no': b'old'})
        driver, runtime, saved = self.make(':update nested\nAFTER = no\n', markers=markers)
        Evaluator(driver.scope, graph=driver.graph, declarations=driver.declarations).run(
            lower(parse(Source('/recipe/hook.aap', 'nested {virtual}:\n  @port_fetch(globals())\n'))))
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.bodies[0].update_failure.status, 'FAILED')
        self.assertEqual(result.bodies[0].update_failure.span.source_id, '/recipe/hook.aap')
        self.assertEqual(driver.completed, {})
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(driver.finish().pending_signatures, ())

    def test_helper_without_artifact_capability_blocks(self):
        driver, runtime, saved = self.make()
        runtime.artifacts = None
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(runtime.markers.files, {})

    def test_force_touch_requires_existing_parent(self):
        driver, runtime, saved = self.make(':touch {force} done/fetch\n')
        self.assertEqual(driver.build('all').status, 'FAILED')
        self.assertEqual(runtime.markers.files, {})

    def test_marker_effects_immediate_signatures_pending_after_later_failure(self):
        driver, runtime, saved = self.make(':update stage\nX = $MISSING\n')
        Evaluator(driver.scope, graph=driver.graph, declarations=driver.declarations).run(
            lower(parse(Source('/recipe/stage.aap', 'stage {virtual}:\n'
                              '  @port_fetch(globals())\n  :mkdir {force} done\n'
                              '  :touch {force} done/fetch\n'))))
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertIn('/recipe/done/fetch', runtime.markers.files)
        self.assertIn('/recipe/stage', driver.completed)
        self.assertEqual([r.target for r in result.pending_signatures], ['stage'])
        self.assertEqual(saved.writes, [])
        driver.finish()
        self.assertEqual([r.target for r in saved.writes], ['stage'])
        self.assertEqual(saved.markers, {})  # distinct capability, no signature/marker conflation


class NanoPortIntegration(unittest.TestCase):
    def setup_nano(self, markers=None, actions=None, enable_sys=False, system_result=None,
                   directories=None, port_result=None, path_observer=None, deletions=None):
        class Process(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                if not request.capture_stdout:
                    is_port = isinstance(request, PortCommandRequest)
                    if not is_port and not enable_sys: raise AssertionError('unexpected :sys')
                    self.requests.append(request)
                    outcome = port_result if is_port else system_result
                    if isinstance(outcome, Exception): raise outcome
                    return ProcessResult(0, b'controlled output\n', b'') if outcome is None else outcome
                values = (b'SUSE15', b'suse', b'SUSE 15 6')
                # Later controlled body probes may capture an opaque command.
                # Its empty fixture result establishes no file or package fact.
                value = values[len(self.requests)] if len(self.requests) < len(values) else b''
                self.requests.append(request)
                return ProcessResult(0, value)
        shared = Source('/authorized/ports/globals.aap', Source.from_path(
            os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1').text)
        class Loader(object):
            def load(self, path):
                if path != shared.source_id: raise AssertionError('unauthorized source')
                return shared
        archive = '/authorized/ports/editors/nano/distfiles/nano-7.1.tar.gz'
        artifacts = MemoryArtifacts({archive: b'synthetic archive'})
        class RecordedChecksum(ChecksumBackend):
            def md5(self, request):
                if request.path != archive: raise AssertionError('unexpected archive')
                return 'cc9e42c4805193f9dc3ae22b9644bb1d'
        scope = Scope.top_level(port_defaults=True)
        scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build', 'DISTDIR': 'distfiles',
                            'PKGDIR': '/authorized/ports/editors/nano/pack', 'WRKDIR': 'work'})
        process, saved = Process(), MemoryPersistence()
        policy = ProcessPolicy('latin-1', sys_mode='unlogged' if enable_sys else None)
        text = Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/editors/nano/main.aap'), 'latin-1').text
        data = Evaluator(scope, include_loader=Loader(), process_backend=process,
                         process_policy=policy).run(lower(parse(Source('/authorized/ports/editors/nano/main.aap', text))))
        self.assertTrue(data.complete)
        # No Message logger/global capture exists in this controlled setup.
        # Record both observations explicitly, independent of :sys permission.
        commands = PortCommandRuntime(directories=directories, policy=PortCommandPolicy(False, False))
        runtime = PortRuntime(artifacts, markers if markers is not None else MemoryMarkers(),
                              actions, commands, deletions)
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, scope, data.declarations,
                             include_loader=Loader(), process_backend=process, process_policy=policy,
                             checksum_backend=RecordedChecksum(artifacts), port_runtime=runtime,
                             path_observer=path_observer)
        return driver, runtime, saved, process

    def test_generated_diagnostic_targets_have_exact_events_without_effects(self):
        cases = (('installtest', 51, 'extra', 'Default installtest: do nothing'),
                 ('srcpackage', 61, 'info', 'TODO: srcpackage'))
        for target, line, kind, text in cases:
            driver, runtime, saved, process = self.setup_nano()
            result = driver.build(target)
            self.assertEqual(result.status, 'COMPLETE', target)
            self.assertEqual([body.target.name for body in result.bodies], [target])
            operation = result.bodies[0].port_operations[0]
            self.assertEqual(operation.cwd, '/authorized/ports/editors/nano')
            self.assertEqual(operation.status, 'COMPLETED')
            self.assertEqual(len(operation.messages), 1)
            message = operation.messages[0]
            self.assertIsInstance(message, PortMessage)
            self.assertEqual((message.kind, message.text), (kind, text))
            self.assertEqual(message.span.source_id,
                             '<port defaults: /authorized/ports/editors/nano>')
            self.assertEqual(message.span.start.line, line)
            self.assertEqual(len(process.requests), 3)  # setup :syseval only
            self.assertEqual(runtime.artifacts.observations, [])
            self.assertEqual(runtime.markers.operations, [])
            self.assertEqual(saved.writes, [])

    def test_nano_passes_fetch_checksum_and_stops_at_extraction(self):
        driver, runtime, saved, process = self.setup_nano()
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([b.target.name for b in result.bodies],
                         ['dependcheck', 'fetchdepend', 'fetch', 'checksum', 'extractdepend', 'extract'])
        self.assertEqual(result.bodies[-1].program.statements[0].fragment.code, 'port_extract(globals())')
        self.assertEqual(result.bodies[-1].status, 'BLOCKED')
        fetch = result.bodies[2]
        self.assertEqual(fetch.status, 'COMPLETED')
        self.assertEqual(fetch.scope.local['EXTRACTFILES'], 'nano-7.1.tar.gz')
        self.assertNotIn('EXTRACTFILES', driver.scope.local)
        self.assertEqual(fetch.context.cwd, '/authorized/ports/editors/nano')
        self.assertEqual([op.operation for op in fetch.port_operations], ['port_fetch', 'mkdir', 'touch'])
        hook = result.bodies[3].updates[0].builds[0].bodies[0]
        self.assertEqual(hook.checksums[0].status, 'VERIFIED')
        self.assertEqual([operation.operation for body in result.bodies
                          for operation in body.port_operations
                          if operation.operation == 'port_checksum'], [])
        self.assertEqual(result.bodies[4].updates[0].status, 'COMPLETE')
        base = '/authorized/ports/editors/nano/'
        self.assertEqual(sorted(runtime.markers.files),
                         [base + 'done/checksum', base + 'done/cvs-no', base + 'done/fetch'])
        self.assertIn(base + 'fetch', driver.completed)
        self.assertIn(base + 'checksum', driver.completed)
        self.assertNotIn(base + 'extract', driver.completed)
        self.assertEqual(len(process.requests), 3)
        self.assertEqual((len(driver.graph.definitions), len(driver.graph.targets), len(driver.graph.nodes)), (57, 57, 58))
        self.assertEqual(saved.writes, [])
        pending = result.pending_signatures
        self.assertTrue(pending)
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual(tuple(saved.writes), pending)
        self.assertEqual(saved.markers, {})

    def test_existing_done_markers_skip_fetch_and_checksum_in_fresh_invocation(self):
        base = '/authorized/ports/editors/nano/'
        markers = MemoryMarkers({base + 'done/fetch': b'', base + 'done/checksum': b'',
                                 base + 'done/cvs-no': b''})
        driver, runtime, saved, process = self.setup_nano(markers)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([b.target.name for b in result.bodies], ['dependcheck', 'extractdepend', 'extract'])
        self.assertEqual(runtime.artifacts.observations, [])
        self.assertEqual(markers.operations, [])
        self.assertEqual(len(process.requests), 3)

    def test_explicit_fetch_repeat_uses_current_invocation_done_state(self):
        driver, runtime, saved, process = self.setup_nano()
        self.assertEqual(driver.build('fetch').status, 'COMPLETE')
        count = len(runtime.markers.operations)
        self.assertEqual(driver.build('fetch').bodies, [])
        self.assertEqual(len(runtime.markers.operations), count)

    def extraction(self, controlled=False):
        archive = '/authorized/ports/editors/nano/distfiles/nano-7.1.tar.gz'
        workspace = MemoryActionWorkspace({archive: b'synthetic archive'})
        class RecordedExtract(ActionBackend):
            def __init__(self): self.requests = []
            def execute(self, request):
                # This is an explicit outcome oracle for the registered action,
                # not a replacement tar implementation or relaxed comparison.
                if (request.name != 'extract' or request.filetype != 'targz'
                        or request.filename != archive
                        or request.definition.span.source_id != '/authorized/ports/globals.aap'
                        or ':sys gzip -dc $source | tar xf -' not in request.body.origin.text):
                    raise AssertionError('unexpected action')
                self.requests.append(request)
                workspace.files[request.cwd + '/nano-7.1/observed'] = b'controlled output'
                result = ActionResult(request)
                result.status, result.reason = 'COMPLETED', 'recorded_extract_success'
                return result
        backend = RecordedExtract() if controlled else None
        return ActionRuntime(workspace, backend)

    def test_nano_real_action_entry_stops_at_sys_without_extract_marker(self):
        actions = self.extraction()
        driver, runtime, saved, process = self.setup_nano(actions=actions)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'sys')
        self.assertEqual(result.span.source_id, '/authorized/ports/globals.aap')
        self.assertEqual(result.span.start.line, 26)
        operation = result.bodies[-1].port_operations[0]
        request = operation.actions[0].request
        self.assertTrue(request.span.source_id.startswith('<port defaults:'))
        self.assertEqual(request.cwd, '/authorized/ports/editors/nano/work')
        self.assertEqual(request.scope.local['source'], '/authorized/ports/editors/nano/distfiles/nano-7.1.tar.gz')
        self.assertEqual(request.caller.cwd, '/authorized/ports/editors/nano')
        self.assertNotIn('/authorized/ports/editors/nano/done/extract', runtime.markers.files)
        self.assertNotIn('/authorized/ports/editors/nano/extract', driver.completed)
        self.assertEqual(len(process.requests), 3)
        self.assertEqual(saved.writes, [])

    def test_nano_controlled_action_success_stops_at_build(self):
        actions = self.extraction(controlled=True)
        driver, runtime, saved, process = self.setup_nano(actions=actions, enable_sys=True)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([b.target.name for b in result.bodies],
                         ['dependcheck', 'fetchdepend', 'fetch', 'checksum', 'extractdepend', 'extract', 'patch', 'builddepend', 'config', 'build'])
        self.assertEqual(result.bodies[-1].program.statements[0].fragment.code, 'port_build(globals())')
        self.assertTrue(result.span.source_id.startswith('<port defaults:'))
        self.assertEqual(result.span.start.line, 31)
        self.assertEqual(result.bodies[-2].status, 'COMPLETED')
        self.assertIn('/authorized/ports/editors/nano/done/extract', runtime.markers.files)
        self.assertIn('/authorized/ports/editors/nano/extract', driver.completed)
        self.assertIn('/authorized/ports/editors/nano/patch', driver.completed)
        self.assertIn('/authorized/ports/editors/nano/done/patch', runtime.markers.files)
        self.assertEqual(len(actions.backend.requests), 1)
        self.assertEqual(len(process.requests), 4)
        self.assertEqual(saved.writes, [])
        pending = result.pending_signatures
        self.assertTrue(pending)
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual(tuple(saved.writes), pending)

    def test_existing_extract_marker_skips_action_in_fresh_invocation(self):
        base = '/authorized/ports/editors/nano/'
        markers = MemoryMarkers(dict((base + 'done/' + name, b'') for name in
                                     ('cvs-no', 'fetch', 'checksum', 'extract')))
        actions = self.extraction(controlled=True)
        driver, runtime, saved, process = self.setup_nano(markers, actions, enable_sys=True)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[-1].program.statements[0].fragment.code, 'port_build(globals())')
        self.assertTrue(result.span.source_id.startswith('<port defaults:'))
        self.assertEqual(result.span.start.line, 21)
        self.assertEqual(actions.backend.requests, [])
        self.assertEqual(actions.workspace.operations, [])

    def test_explicit_extract_repeat_completes_only_once(self):
        actions = self.extraction(controlled=True)
        driver, runtime, saved, process = self.setup_nano(actions=actions)
        self.assertEqual(driver.build('extract').status, 'COMPLETE')
        self.assertEqual(driver.build('extract').bodies, [])
        self.assertEqual(len(actions.backend.requests), 1)
        self.assertEqual(len(process.requests), 3)


    def test_nano_normal_action_process_returns_then_blocks_at_build(self):
        actions = self.extraction()  # SemanticActionBackend, no action oracle.
        driver, runtime, saved, process = self.setup_nano(actions=actions, enable_sys=True)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([b.target.name for b in result.bodies],
                         ['dependcheck', 'fetchdepend', 'fetch', 'checksum', 'extractdepend', 'extract', 'patch', 'builddepend', 'config', 'build'])
        self.assertEqual(result.bodies[-1].program.statements[0].fragment.code, 'port_build(globals())')
        self.assertTrue(result.span.source_id.startswith('<port defaults:'))
        self.assertEqual(result.span.start.line, 31)
        body = result.bodies[-5]
        action = body.port_operations[0].actions[0]
        self.assertEqual((body.status, action.status), ('COMPLETED', 'COMPLETED'))
        request = process.requests[-2]
        self.assertEqual(len(process.requests), 5)
        self.assertEqual(request.command,
            'gzip -dc /authorized/ports/editors/nano/distfiles/nano-7.1.tar.gz | tar xf -')
        self.assertEqual(request.stages, (('gzip', '-dc', '/authorized/ports/editors/nano/distfiles/nano-7.1.tar.gz'),
                                          ('tar', 'xf', '-')))
        self.assertEqual(request.cwd, '/authorized/ports/editors/nano/work')
        self.assertEqual(request.shell_mode, 'posix-sh')
        self.assertFalse(request.capture_stdout)
        self.assertEqual(request.span.source_id, '/authorized/ports/globals.aap')
        self.assertEqual(request.span.start.line, 26)
        self.assertEqual(action.request.caller.cwd, '/authorized/ports/editors/nano')
        self.assertEqual(action.request.scope.local['sysresult'], 0)
        self.assertNotIn('sysresult', body.scope.local)
        self.assertNotIn('sysresult', driver.scope.local)
        self.assertIs(action.request.graph, driver.graph)
        self.assertIs(action.request.caller.update_driver, driver)
        self.assertTrue(action.request.span.source_id.startswith('<port defaults:'))
        self.assertIn('/authorized/ports/editors/nano/done/extract', runtime.markers.files)
        self.assertIn('/authorized/ports/editors/nano/done/patch', runtime.markers.files)
        # A controlled process result does not claim the archive produced files.
        self.assertEqual(len(actions.workspace.files), 1)
        self.assertEqual(saved.writes, [])
        pending = result.pending_signatures
        self.assertTrue(pending)
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual(tuple(saved.writes), pending)

    def test_nano_process_failure_and_unavailability_preserve_nested_locations(self):
        for outcome, status in ((ProcessResult(256, b'partial', b'failure'), 'FAILED'),
                                (ProcessUnavailable('process not available'), 'BLOCKED'),
                                (ProcessBackendError('cannot launch'), 'FAILED')):
            actions = self.extraction()
            driver, runtime, saved, process = self.setup_nano(actions=actions, enable_sys=True,
                                                            system_result=outcome)
            result = driver.build()
            self.assertEqual(result.status, status)
            body = result.bodies[-1]
            self.assertEqual(body.target.name, 'extract')
            action = body.port_operations[0].actions[0]
            self.assertEqual(action.status, status)
            self.assertEqual(action.evaluation.processes[0].status, status)
            self.assertEqual(result.span.source_id, '/authorized/ports/globals.aap')
            self.assertEqual(result.span.start.line, 26)
            self.assertTrue(action.request.span.source_id.startswith('<port defaults:'))
            self.assertEqual(action.request.caller.cwd, '/authorized/ports/editors/nano')
            self.assertNotIn('/authorized/ports/editors/nano/done/extract', runtime.markers.files)
            self.assertNotIn('/authorized/ports/editors/nano/extract', driver.completed)
            self.assertEqual(saved.writes, [])
            if isinstance(outcome, ProcessResult):
                self.assertEqual(action.request.scope.local['sysresult'], 256)


    def test_nano_patch_entry_state_and_next_exact_barrier(self):
        actions = self.extraction()
        driver, runtime, saved, process = self.setup_nano(actions=actions, enable_sys=True)
        result = driver.build()
        patch = result.bodies[-4]
        self.assertEqual(patch.target.name, 'patch')
        helper = patch.port_operations[0]
        self.assertEqual(helper.operation, 'port_patch')
        self.assertEqual(helper.status, 'COMPLETED')
        self.assertEqual(helper.paths, [])
        self.assertEqual(helper.actions, [])
        self.assertIs(helper.scope, patch.scope)
        self.assertEqual(helper.cwd, '/authorized/ports/editors/nano')
        self.assertEqual(helper.scope.lookup('PATCHFILES'), '')
        self.assertEqual(helper.scope.lookup('PATCHCMD'), 'patch -f -p 0 <')
        self.assertEqual(helper.scope.lookup('PATCHDIR'), '.')
        self.assertEqual(helper.scope.lookup('WRKSRC'), 'nano-7.1')
        self.assertEqual(helper.scope.lookup('WRKDIR'), 'work')
        nested = result.bodies[-3].updates[0]
        self.assertTrue(nested.request.span.source_id.startswith('<port defaults:'))
        child = nested.builds[0].bodies[0]
        self.assertEqual(child.scope.local['d'], 'ncurses-devel')
        self.assertEqual(child.status, 'COMPLETED')
        self.assertEqual(child.processes[0].status, 'COMPLETED')
        self.assertEqual(child.scope.local['sysresult'], 0)
        self.assertEqual(child.scope.local['deplist'], [])  # resumed after :sys
        request = child.processes[0].request
        self.assertEqual(child.scope.lookup('CLEARCACHE'), 'zypper clean --all')
        self.assertEqual(child.scope.lookup('RPMINSTALL'), 'zypper --non-interactive --no-gpg-checks install')
        self.assertEqual(request.command, 'rpm -q ncurses-devel || ( zypper clean --all && zypper --non-interactive --no-gpg-checks install ncurses-devel )')
        self.assertEqual(request.expression.kind, 'OR')
        query, group = request.expression.children
        self.assertEqual(query.argv, ('rpm', '-q', 'ncurses-devel'))
        self.assertEqual(group.kind, 'GROUP')
        self.assertEqual(group.children[0].kind, 'AND')
        clear, install = group.children[0].children
        self.assertEqual(clear.argv, ('zypper', 'clean', '--all'))
        self.assertEqual(install.argv, ('zypper', '--non-interactive', '--no-gpg-checks', 'install', 'ncurses-devel'))
        self.assertIsNone(request.stages)
        self.assertEqual(request.span.source_id, '/authorized/ports/globals.aap')
        self.assertEqual(request.span.start.line, 585)
        self.assertEqual(request.cwd, '/authorized/ports/editors/nano')
        self.assertNotIn('sysresult', driver.scope.local)
        self.assertIn('/authorized/ports/editors/nano/builddepend', driver.completed)
        self.assertEqual(result.bodies[-1].program.statements[0].fragment.code, 'port_build(globals())')
        self.assertNotIn('/authorized/ports/editors/nano/done/build', runtime.markers.files)
        self.assertEqual(len(process.requests), 5)
        self.assertEqual(saved.writes, [])

    def test_nano_empty_config_completes_without_directory_or_process_effect(self):
        driver, runtime, saved, process = self.setup_nano(actions=self.extraction(), enable_sys=True)
        result = driver.build()
        body = result.bodies[-2]
        self.assertEqual(body.target.name, 'config')
        self.assertEqual(body.status, 'COMPLETED')
        from aap_semantics.values import MISSING
        self.assertIs(body.scope.lookup('CONFIGURECMD'), MISSING)
        self.assertIs(body.scope.lookup('BUILDDIR'), MISSING)
        self.assertEqual(body.scope.lookup('WRKSRC'), 'nano-7.1')
        self.assertEqual(body.scope.lookup('WRKDIR'), 'work')
        self.assertEqual(body.scope.lookup('PREFIX'), '/usr/local')
        self.assertEqual(body.scope.lookup('BUILDCMD'), './configure --prefix=/usr/local && make')
        self.assertEqual(body.context.cwd, '/authorized/ports/editors/nano')
        self.assertEqual([(message.kind, message.text) for message in body.port_operations[0].messages],
                         [('extra', 'No CONFIGURECMD specified')])
        self.assertFalse(runtime.commands.policy.log_active)
        self.assertFalse(runtime.commands.policy.capture_active)
        self.assertEqual(body.port_operations[0].processes, [])
        self.assertEqual(body.processes, ())
        self.assertEqual(len(process.requests), 5)
        self.assertIn('/authorized/ports/editors/nano/done/config', runtime.markers.files)
        self.assertIn('/authorized/ports/editors/nano/config', driver.completed)
        self.assertNotIn('/authorized/ports/editors/nano/build', driver.completed)
        self.assertNotIn('/authorized/ports/editors/nano/done/build', runtime.markers.files)
        self.assertNotIn('sysresult', body.scope.local)
        self.assertEqual(saved.writes, [])
        self.assertTrue(result.pending_signatures)
        self.assertEqual(result.bodies[-1].program.statements[0].fragment.code, 'port_build(globals())')

    def test_nano_build_strict_state_does_not_infer_extracted_directory(self):
        actions = self.extraction()
        driver, runtime, saved, process = self.setup_nano(actions=actions, enable_sys=True)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        body = result.bodies[-1]
        self.assertEqual(body.target.name, 'build')
        self.assertEqual(body.port_operations[0].operation, 'port_build')
        record = body.port_operations[0].processes[0]
        self.assertEqual(record.reason, 'port_capability_unavailable')
        self.assertIsNone(record.request)
        self.assertIsNone(record.selected_cwd)
        self.assertEqual(record.command, './configure --prefix=/usr/local && make')
        self.assertEqual(result.span.source_id, '<port defaults: /authorized/ports/editors/nano>')
        self.assertEqual(result.span.start.line, 31)
        self.assertEqual(len(process.requests), 5)
        self.assertFalse(any(isinstance(r, PortCommandRequest) for r in process.requests))
        self.assertNotIn('/authorized/ports/editors/nano/work/nano-7.1', actions.workspace.directories)
        self.assertNotIn('/authorized/ports/editors/nano/done/build', runtime.markers.files)
        self.assertNotIn('/authorized/ports/editors/nano/build', driver.completed)
        self.assertEqual(saved.writes, [])

    def test_nano_build_with_explicit_post_extraction_directory(self):
        directory = '/authorized/ports/editors/nano/work/nano-7.1'
        directories = MemoryPortDirectories([directory])  # explicit test observation, not extraction output
        actions = self.extraction()
        driver, runtime, saved, process = self.setup_nano(actions=actions, enable_sys=True,
                                                        directories=directories)
        result = driver.build()
        self.assertEqual(result.status, 'COMPLETE')  # upstream all: build; no implicit test/install
        self.assertEqual([b.target.name for b in result.bodies],
            ['dependcheck', 'fetchdepend', 'fetch', 'checksum', 'extractdepend', 'extract',
             'patch', 'builddepend', 'config', 'build'])
        body = result.bodies[-1]
        self.assertEqual(body.status, 'COMPLETED')
        self.assertEqual(body.port_operations[0].status, 'COMPLETED')
        self.assertEqual(len(process.requests), 6)
        request = process.requests[-1]
        self.assertIsInstance(request, PortCommandRequest)
        self.assertEqual(request.operation, 'port_exe_cmd')
        self.assertEqual(request.command, './configure --prefix=/usr/local && make')
        self.assertEqual(request.shell_command_bytes, b'./configure --prefix=/usr/local && make\n')
        self.assertEqual(request.cwd, directory)
        self.assertEqual(directories.observations, [directory])
        self.assertEqual(directories.directories, {directory})
        self.assertFalse(hasattr(request, 'expression'))
        self.assertEqual(body.context.cwd, '/authorized/ports/editors/nano')
        self.assertNotIn('sysresult', body.scope.local)
        self.assertNotIn('sysresult', driver.scope.local)
        self.assertEqual(driver.scope.lookup('BUILDCMD'), request.command)
        self.assertIn('/authorized/ports/editors/nano/done/build', runtime.markers.files)
        self.assertIn('/authorized/ports/editors/nano/build', driver.completed)
        self.assertNotIn('/authorized/ports/editors/nano/done/test', runtime.markers.files)
        self.assertEqual(len(actions.workspace.files), 1)
        self.assertNotIn(directory, actions.workspace.directories)
        self.assertEqual(saved.writes, [])
        count = len(runtime.markers.operations)
        self.assertEqual(driver.build('build').bodies, [])
        self.assertEqual(len(process.requests), 6)
        self.assertEqual(len(runtime.markers.operations), count)
        pending = result.pending_signatures
        self.assertTrue(pending)
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual(tuple(saved.writes), pending)

    def test_nano_build_known_missing_directory_is_failure(self):
        directories = MemoryPortDirectories()  # known absent, not unknown
        driver, runtime, saved, process = self.setup_nano(actions=self.extraction(), enable_sys=True,
                                                        directories=directories)
        result = driver.build()
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(directories.observations, ['/authorized/ports/editors/nano/work/nano-7.1'])
        self.assertEqual(len(process.requests), 5)
        self.assertNotIn('/authorized/ports/editors/nano/done/build', runtime.markers.files)
        self.assertEqual(saved.writes, [])

    def test_nano_build_process_failure_preserves_config_but_not_build_marker(self):
        for outcome, status in ((ProcessResult(256), 'FAILED'),
                (ProcessUnavailable('disabled'), 'BLOCKED'), (ProcessBackendError('broken'), 'FAILED'),
                (False, 'FAILED')):
            driver, runtime, saved, process = self.setup_nano(actions=self.extraction(), enable_sys=True,
                directories=MemoryPortDirectories(['/authorized/ports/editors/nano/work/nano-7.1']), port_result=outcome)
            result = driver.build()
            self.assertEqual(result.status, status)
            self.assertEqual(result.span.source_id, '<port defaults: /authorized/ports/editors/nano>')
            self.assertEqual(result.span.start.line, 31)
            self.assertEqual(result.bodies[-1].target.name, 'build')
            self.assertNotIn('sysresult', result.bodies[-1].scope.local)
            self.assertNotIn('/authorized/ports/editors/nano/build', driver.completed)
            self.assertIn('/authorized/ports/editors/nano/done/config', runtime.markers.files)
            self.assertNotIn('/authorized/ports/editors/nano/done/build', runtime.markers.files)
            self.assertEqual(len(process.requests), 6)
            self.assertEqual(saved.writes, [])

    def test_nano_cd_requires_fresh_directory_observation(self):
        class Observations(MemoryPortDirectories):
            def enter(self, path):
                if len(self.observations) == 2:  # build and test were observed
                    self.observations.append(path)
                    raise NotImplementedError('directory observation now unavailable')
                return super(Observations, self).enter(path)
        directories = Observations(['/authorized/ports/editors/nano/work/nano-7.1'])
        driver, runtime, saved, process = self.setup_nano(actions=self.extraction(), enable_sys=True,
                                                        directories=directories)
        self.assertEqual(driver.build().status, 'COMPLETE')
        result = driver.build('rpm')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'cd')
        self.assertEqual(result.span.start.line, 42)
        self.assertEqual(len(process.requests), 8)
        change = result.bodies[-1].directory_changes[0]
        self.assertEqual(change.status, 'BLOCKED')
        self.assertEqual(change.after, '/authorized/ports/editors/nano')
        self.assertEqual(change.previous_after, '/authorized/ports/editors/nano')
        self.assertNotIn('/authorized/ports/editors/nano/fake-install', driver.completed)
        self.assertEqual(saved.writes, [])

    def test_nano_rpm_batches_install_chown_then_blocks_at_prep_rpm_print(self):
        directories = MemoryPortDirectories(['/authorized/ports/editors/nano/work/nano-7.1'])
        actions = self.extraction()
        driver, runtime, saved, process = self.setup_nano(actions=actions, enable_sys=True,
                                                        directories=directories)
        self.assertEqual(driver.build().status, 'COMPLETE')
        result = driver.build('rpm')  # separate explicit request; not implicit port-stage continuation
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([b.target.name for b in result.bodies], ['testdepend', 'test', 'fake-install', 'prep-rpm'])
        self.assertEqual(result.bodies[0].status, 'COMPLETED')
        self.assertEqual(result.bodies[0].processes, ())
        from aap_semantics.values import MISSING
        for name in ('SKIPTEST', 'AUTODEPEND', 'DEPEND_TEST'):
            self.assertIs(result.bodies[0].scope.lookup(name), MISSING)
        self.assertEqual(result.bodies[1].status, 'COMPLETED')
        self.assertEqual(process.requests[-3].command, 'true')
        self.assertIsInstance(process.requests[-3], PortCommandRequest)
        self.assertEqual(process.requests[-3].cwd, '/authorized/ports/editors/nano/work/nano-7.1')
        self.assertNotIn('sysresult', result.bodies[1].scope.local)
        self.assertEqual(len(process.requests), 9)
        self.assertEqual(process.requests[-2].command,
                         'mkdir -p /authorized/ports/editors/nano/pack/usr/local/doc/nano')
        batch = process.requests[-1]
        self.assertEqual([e.raw for e in batch.entries], ['$INSTALLCMD', 'chown -Rh root:root $PKGDIR'])
        commands = ['make DESTDIR=/authorized/ports/editors/nano/pack install',
                    'chown -Rh root:root /authorized/ports/editors/nano/pack']
        self.assertEqual([e.expanded for e in batch.entries], commands)
        self.assertEqual(batch.shell_command, '\n'.join(commands) + '\n')
        self.assertEqual(batch.cwd, '/authorized/ports/editors/nano/work/nano-7.1')
        self.assertEqual([e.span.start.line for e in batch.entries], [43, 44])
        self.assertEqual(batch.expression.kind, 'SEQUENCE')
        self.assertEqual(result.blocked_at.name, 'print')
        self.assertEqual(result.span.source_id, '/authorized/ports/globals.aap')
        self.assertEqual(result.span.start.line, 221)
        # Runtime observations are not preflighted. The first :print remains
        # unsupported; the later existence condition is never reached.
        self.assertEqual(result.bodies[-1].evaluation.path_observations, [])
        self.assertEqual(result.bodies[-1].processes, ())
        self.assertEqual(result.bodies[-1].scope.local['file'], 'work/nano.spec')
        body = result.bodies[-2]
        change = body.directory_changes[0]
        self.assertEqual(change.span.start.line, 42)
        self.assertEqual(change.before, '/authorized/ports/editors/nano')
        self.assertEqual(change.raw, '$WRKDIR/$WRKSRC')
        self.assertEqual(change.expanded, 'work/nano-7.1')
        self.assertEqual(change.components, ('work/nano-7.1',))
        self.assertIsNone(change.previous_before)
        self.assertEqual(change.previous_after, change.before)
        self.assertEqual(change.after, '/authorized/ports/editors/nano/work/nano-7.1')
        self.assertEqual(change.status, 'COMPLETED')
        self.assertEqual(body.evaluation.final_cwd, change.after)
        self.assertEqual(body.scope.local['_prevdir'], change.before)
        self.assertEqual(body.status, 'COMPLETED')
        self.assertEqual(body.scope.local['sysresult'], 0)
        self.assertEqual(body.processes[-1].result.wait_status, 0)
        self.assertEqual(body.context.cwd, '/authorized/ports/editors/nano')
        self.assertIn('/authorized/ports/editors/nano/fake-install', driver.completed)
        self.assertNotIn('/authorized/ports/editors/nano/prep-rpm', driver.completed)
        self.assertIn('/authorized/ports/editors/nano/done/test', runtime.markers.files)
        self.assertEqual(len(actions.workspace.files), 1)
        self.assertEqual(directories.directories, {'/authorized/ports/editors/nano/work/nano-7.1'})
        self.assertEqual(saved.writes, [])
        self.assertTrue(result.pending_signatures)
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual(tuple(saved.writes), result.pending_signatures)

    def test_nano_failed_or_unavailable_batch_never_completes_fake_install(self):
        for outcome, status in ((ProcessResult(256), 'FAILED'),
                                (ProcessUnavailable('batch unavailable'), 'BLOCKED'),
                                (ProcessBackendError('batch error'), 'FAILED')):
            driver, runtime, saved, process = self.setup_nano(actions=self.extraction(), enable_sys=True,
                directories=MemoryPortDirectories(['/authorized/ports/editors/nano/work/nano-7.1']))
            self.assertEqual(driver.build().status, 'COMPLETE')
            original = process.run
            def controlled(request):
                if getattr(request, 'expression', None) is not None and request.expression.kind == 'SEQUENCE':
                    process.requests.append(request)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                return original(request)
            process.run = controlled
            result = driver.build('rpm')
            self.assertEqual(result.status, status)
            self.assertEqual(result.span.source_id, '/authorized/ports/editors/nano/main.aap')
            self.assertEqual(result.span.start.line, 43)
            self.assertEqual(len(process.requests), 9)
            self.assertEqual([b.target.name for b in result.bodies], ['testdepend', 'test', 'fake-install'])
            self.assertNotIn('/authorized/ports/editors/nano/fake-install', driver.completed)
            self.assertEqual(saved.writes, [])

    def test_nano_compound_failure_propagates_without_config_or_completion(self):
        for outcome, status in ((ProcessResult(256), 'FAILED'),
                                (ProcessUnavailable('compound unavailable'), 'BLOCKED'),
                                (ProcessBackendError('compound failed'), 'FAILED'),
                                (ProcessResult(-1), 'FAILED')):
            driver, runtime, saved, process = self.setup_nano(actions=self.extraction(), enable_sys=True)
            original = process.run
            def controlled(request):
                if request.expression.kind == 'OR':
                    process.requests.append(request)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                return original(request)
            process.run = controlled
            result = driver.build()
            self.assertEqual(result.status, status)
            self.assertEqual(result.span.source_id, '/authorized/ports/globals.aap')
            self.assertEqual(result.span.start.line, 585)
            outer = result.bodies[-1]
            self.assertEqual(outer.target.name, 'builddepend')
            nested = outer.update_failure
            self.assertEqual(nested.target, 'do-builddepend')
            self.assertTrue(nested.request.span.source_id.startswith('<port defaults:'))
            child = nested.builds[0].bodies[0]
            self.assertEqual(child.status, status)
            self.assertEqual(child.context.cwd, '/authorized/ports/editors/nano')
            self.assertEqual(child.scope.local['deplist'], ['ncurses-devel'])  # no statement after failure
            if isinstance(outcome, ProcessResult) and outcome.wait_status == 256:
                self.assertEqual(child.scope.local['sysresult'], 256)
            else:
                self.assertNotIn('sysresult', child.scope.local)
            self.assertNotIn('sysresult', driver.scope.local)
            self.assertNotIn('/authorized/ports/editors/nano/builddepend', driver.completed)
            self.assertIn('/authorized/ports/editors/nano/done/patch', runtime.markers.files)
            self.assertNotIn('/authorized/ports/editors/nano/done/config', runtime.markers.files)
            self.assertEqual(len(process.requests), 5)
            self.assertEqual(saved.writes, [])

    def test_explicit_patch_completion_repeat_and_fresh_marker_behavior(self):
        driver, runtime, saved, process = self.setup_nano(actions=self.extraction(), enable_sys=True)
        first = driver.build('patch')
        self.assertEqual(first.status, 'COMPLETE')
        count = len(runtime.markers.operations)
        self.assertEqual(driver.build('patch').bodies, [])
        self.assertEqual(len(runtime.markers.operations), count)
        self.assertEqual(len(process.requests), 4)
        # A new graph reads marker state at port-default generation. The
        # completed prior stage's marker persists immediately, before finish.
        second, runtime2, saved2, process2 = self.setup_nano(runtime.markers,
            self.extraction(), enable_sys=True)
        result = second.build('patch')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertFalse(any(op.operation == 'port_patch' for body in result.bodies
                             for op in body.port_operations))
        self.assertEqual(len(process2.requests), 3)
        self.assertEqual(len(runtime.markers.operations), count)
        self.assertEqual(saved.writes, [])
        self.assertEqual(saved2.writes, [])



if __name__ == '__main__':
    unittest.main()
