"""Canonical ports entry and direct Nano include identity.

The supplied ports/main.aap is read explicitly; no private package is read.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, Evaluator, MemoryMarkers,
                           MemoryPersistence, MemoryPortDirectories,
                           MemoryTargetState, MemoryTreeFilesystem,
                           PortRuntime, ProcessBackend, ProcessPolicy,
                           ProcessResult, Scope, TreeObservation, lower)
from aap_semantics.includes import resolve_path
from aap_semantics.port_commands import (PortCommandPolicy,
                                         PortCommandRuntime)


class RecordingProcess(ProcessBackend):
    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return ProcessResult(0, b'', b'')


class PortsLayoutTests(unittest.TestCase):
    def metadata(self):
        path = os.path.join(ROOT, 'tests', 'fixtures', 'ports', 'main.aap')
        with open(path, 'rb') as stream:
            text = stream.read().decode('latin-1')
        source = Source('/authorized/ports/main.aap', text)
        scope = Scope.top_level()
        data = Evaluator(scope).run(lower(parse(source)))
        self.assertTrue(data.complete)
        return data, scope

    def build(self, tree):
        data, scope = self.metadata()
        driver = BuildDriver(data.graph, MemoryTargetState(), MemoryPersistence(),
                             scope, data.declarations, tree_filesystem=tree,
                             buildcheck_encoding='latin-1')
        return driver.build('packageall')

    def test_top_recipe_identity_and_definition_directory(self):
        data, scope = self.metadata()
        self.assertEqual(len(data.graph.definitions), 4)
        self.assertEqual([definition.span.source_id
                          for definition in data.graph.definitions],
                         ['/authorized/ports/main.aap'] * 4)
        self.assertEqual([definition.cwd for definition in data.graph.definitions],
                         ['/authorized/ports'] * 4)
        nano = Source('/authorized/ports/editors/nano/main.aap',
                      ':include ../../globals.aap\n')
        node = lower(parse(nano)).statements[0]
        self.assertEqual(resolve_path('../../globals.aap',
                                      '/authorized/ports/editors/nano', node),
                         '/authorized/ports/globals.aap')

    def test_aggregate_first_unobserved_tree_request_is_source_aware(self):
        tree = MemoryTreeFilesystem()
        result = self.build(tree)
        self.assertEqual((result.status, result.reason),
                         ('BLOCKED', 'unsupported_semantics'))
        self.assertEqual((result.span.source_id, result.span.start.line),
                         ('/authorized/ports/main.aap', 11))
        self.assertEqual((result.span.start.column, result.span.end.line,
                          result.span.end.column), (1, 20, 1))
        self.assertIn('tree list unavailable: .', str(result.error))
        self.assertEqual([(item.operation, item.path) for item in tree.requests],
                         [('list', '/authorized/ports/.')])

    def test_aggregate_tree_traverses_nano_before_directory_entry_gate(self):
        root = '/authorized/ports/.'
        tree = MemoryTreeFilesystem({
            root: TreeObservation('ENTRIES', ('main.aap', 'editors')),
            root + '/editors': TreeObservation('ENTRIES', ('nano',)),
            root + '/editors/nano': TreeObservation('ENTRIES', ('main.aap',))},
            {root + '/main.aap': TreeObservation('FILE'),
             root + '/editors': TreeObservation('DIRECTORY'),
             root + '/editors/nano': TreeObservation('DIRECTORY'),
             root + '/editors/nano/main.aap': TreeObservation('FILE')})
        result = self.build(tree)
        self.assertEqual((result.status, result.reason),
                         ('BLOCKED', 'unsupported_semantics'))
        self.assertEqual((result.span.source_id, result.span.start.line),
                         ('/authorized/ports/main.aap', 14))
        self.assertIn('port directory entry observation unavailable', str(result.error))
        self.assertEqual(tree.requests[-1].path,
                         '/authorized/ports/./editors/nano/main.aap')

    def test_packageall_tree_dispatches_supplied_www_recipe(self):
        root = '/authorized/ports'
        package_dir = root + '/www/apache-tomcat8'
        tree = MemoryTreeFilesystem({
            root + '/.': TreeObservation('ENTRIES',
                                         ('main.aap', 'globals.aap', 'www')),
            root + '/./www': TreeObservation('ENTRIES', ('apache-tomcat8',)),
            root + '/./www/apache-tomcat8': TreeObservation(
                'ENTRIES', ('main.aap',))}, {
            root + '/./main.aap': TreeObservation('FILE'),
            root + '/./globals.aap': TreeObservation('FILE'),
            root + '/./www': TreeObservation('DIRECTORY'),
            root + '/./www/apache-tomcat8': TreeObservation('DIRECTORY'),
            root + '/./www/apache-tomcat8/main.aap': TreeObservation('FILE')})
        process = RecordingProcess()
        directories = MemoryPortDirectories((root, package_dir))
        data, scope = self.metadata()
        scope.local['AAP'] = 'aap'
        port = PortRuntime(markers=MemoryMarkers(), commands=PortCommandRuntime(
            directories, PortCommandPolicy(False, False)))
        driver = BuildDriver(data.graph, MemoryTargetState(), MemoryPersistence(),
            scope, data.declarations, port_defaults=False, tree_filesystem=tree,
            port_runtime=port, process_backend=process,
            process_policy=ProcessPolicy('latin-1', sys_mode='unlogged'))

        result = driver.build('packageall')

        self.assertEqual((result.status, result.reason),
                         ('COMPLETE', 'requested_targets_complete'))
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].command,
                         'aap package\naap distclean')
        self.assertEqual(process.requests[0].cwd, package_dir)
        self.assertEqual(directories.observations,
                         [root + '/./www/apache-tomcat8', root])
        self.assertIn(root + '/./www/apache-tomcat8/main.aap',
                      [request.path for request in tree.requests
                       if request.operation == 'classify'])


if __name__ == '__main__':
    unittest.main()
