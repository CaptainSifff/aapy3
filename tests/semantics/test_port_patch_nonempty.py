"""Reached archive-mode port_patch, with recording external process effects."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, Evaluator, MemoryArtifacts,
    MemoryMarkers, MemoryPersistence, MemoryPortDirectories, MemoryTargetState,
    PortCommandPolicy, PortCommandRuntime, PortRuntime, ProcessBackend,
    ProcessBackendError, ProcessPolicy, ProcessResult, Scope, lower)


class RecordingProcess(ProcessBackend):
    def __init__(self, results=None, changed=None):
        self.results = list(results or [ProcessResult(0)])
        self.requests = []
        self.changed = changed

    def run(self, request):
        self.requests.append(request)
        if self.changed is not None:
            self.changed['tree'] = b'partly patched'
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class NonemptyPatchTests(unittest.TestCase):
    def make(self, variables=None, process=None, directories=None, recipe=None,
             defaults=False, artifacts=None):
        scope = Scope.top_level(port_defaults=defaults)
        scope.local.update({'PATCHFILES': 'nachusrlocal', 'PATCHDISTDIR': 'patches',
                            'PATCHDIR': '.', 'PATCHCMD': 'patch -f -p 0 <',
                            'WRKDIR': 'work', 'WRKSRC': 'syslinux-6.04-pre1'})
        scope.local.update(variables or {})
        markers = MemoryMarkers()
        dirs = directories if directories is not None else MemoryPortDirectories(
            ['/recipe/work'])
        commands = PortCommandRuntime(dirs, PortCommandPolicy(False, False))
        runtime = PortRuntime(artifacts=artifacts, markers=markers, commands=commands)
        text = recipe or ('all:\n  @port_patch(globals())\n  AFTER = yes\n'
                          '  :mkdir {force} done\n  :touch {force} done/patch\n')
        data = Evaluator(scope).run(lower(parse(Source('/recipe/main.aap', text))))
        backend = process if process is not None else RecordingProcess()
        driver = BuildDriver(data.graph, MemoryTargetState(), MemoryPersistence(),
                             scope, data.declarations, port_runtime=runtime,
                             process_backend=backend, process_policy=ProcessPolicy('latin-1'))
        return driver, runtime, backend, markers

    def test_syslinux_exact_process_request_and_restored_cwd(self):
        driver, runtime, backend, markers = self.make()
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        request = backend.requests[0]
        self.assertEqual(request.operation, 'port_patch')
        self.assertEqual(request.cwd, '/recipe/work')
        self.assertEqual(request.command,
                         'patch -f -p 0 </recipe/patches/nachusrlocal')
        self.assertEqual(request.shell_command, request.command + '\n')
        self.assertEqual(request.span.source_id, '/recipe/main.aap')
        self.assertEqual(request.span.start.line, 2)
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertEqual(result.bodies[0].port_operations[0].cwd, '/recipe')
        self.assertEqual(result.bodies[0].port_operations[0].paths,
                         ['/recipe/patches/nachusrlocal'])
        self.assertIn('/recipe/done/patch', markers.files)

    def test_multiple_items_preserve_order(self):
        driver, runtime, backend, markers = self.make(
            {'PATCHFILES': 'first.diff second.diff'},
            process=RecordingProcess([ProcessResult(0), ProcessResult(0)]))
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual([r.command for r in backend.requests], [
            'patch -f -p 0 </recipe/patches/first.diff',
            'patch -f -p 0 </recipe/patches/second.diff'])

    def test_global_command_placeholder_and_append(self):
        for command, expected in (
                ('custom --in=%s --flag',
                 'custom --in=/recipe/patches/nachusrlocal --flag'),
                ('custom %s %s',
                 'custom /recipe/patches/nachusrlocal %s'),
                ('custom <', 'custom </recipe/patches/nachusrlocal'),
                ('', 'patch -p -f -s < /recipe/patches/nachusrlocal')):
            driver, runtime, backend, markers = self.make({'PATCHCMD': command})
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(backend.requests[0].command, expected)

    def test_patchdir_fallback_and_absolute_directory(self):
        for values, expected in (({'PATCHDIR': ''}, '/recipe/work/syslinux-6.04-pre1'),
                                 ({'PATCHDIR': '/alternate'}, '/alternate')):
            driver, runtime, backend, markers = self.make(values,
                directories=MemoryPortDirectories([expected]))
            result = driver.build('all')
            self.assertEqual(result.status, 'COMPLETE')
            self.assertEqual(backend.requests[0].cwd, expected)
            self.assertEqual(result.bodies[0].context.cwd, '/recipe')

    def test_nonzero_wait_status_keeps_partial_effect_and_no_marker(self):
        changed = {'tree': b'original'}
        backend = RecordingProcess([ProcessResult(256, b'', b'failure')], changed)
        driver, runtime, backend, markers = self.make(process=backend)
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertIn('Shell returned 256 when patching', str(result.error))
        self.assertEqual(changed['tree'], b'partly patched')
        self.assertNotIn('/recipe/done/patch', markers.files)
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertEqual(result.bodies[0].port_operations[0].processes[0].status,
                         'FAILED')

    def test_backend_unavailable_blocks_and_error_fails(self):
        for backend, status in ((ProcessBackend(), 'BLOCKED'),
                                (RecordingProcess([ProcessBackendError('launch')]),
                                 'FAILED')):
            driver, runtime, process, markers = self.make(process=backend)
            result = driver.build('all')
            self.assertEqual(result.status, status)
            self.assertNotIn('/recipe/done/patch', markers.files)
            self.assertEqual(result.bodies[0].context.cwd, '/recipe')

    def test_directory_entry_failure_restores_logical_cwd(self):
        driver, runtime, backend, markers = self.make(
            directories=MemoryPortDirectories())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(backend.requests, [])
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertNotIn('/recipe/done/patch', markers.files)

    def test_item_attributes_stay_gated(self):
        for attribute in ('distdir', 'patchdir', 'patchcmd'):
            driver, runtime, backend, markers = self.make(
                {'PATCHFILES': 'nachusrlocal {%s=value}' % attribute})
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED')
            self.assertEqual(backend.requests, [])


if __name__ == '__main__':
    unittest.main()
