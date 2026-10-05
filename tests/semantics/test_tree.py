"""Characterization from Commands.aap_tree/aap_tree_recurse, using only memory facts."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, Evaluator, MemoryPersistence,
                           MemoryTargetState, MemoryTreeFilesystem, OutputPolicy,
                           PrintRuntime, Scope, TreeObservation, lower)
from nano_target_frontier import BASE, fixture


def listing(*names):
    return TreeObservation('ENTRIES', names)


def kind(status, target=None, detail=None):
    return TreeObservation(status, target_kind=target, detail=detail)


class TreeTests(unittest.TestCase):
    def make(self, body='    :print $name\n', root='pack', pattern='.*',
             directories=None, kinds=None, prior=None, filesystem=True,
             attributes=None, values=None):
        scope = Scope.top_level()
        if prior is not None:
            scope.local['name'] = prior
        scope.local.update(values or {})
        attributes = (attributes if attributes is not None else
                      '{ filename = ' + pattern + ' }')
        source = Source('/recipe/main.aap',
                        'out:\n  :tree ' + root + ' ' + attributes + '\n' + body)
        metadata = Evaluator(scope).run(lower(parse(source)))
        tree = MemoryTreeFilesystem(directories, kinds) if filesystem else None
        output = PrintRuntime(OutputPolicy('latin-1', False))
        driver = BuildDriver(metadata.graph, MemoryTargetState(), MemoryPersistence(),
                             scope, metadata.declarations, tree_filesystem=tree,
                             output_runtime=output, buildcheck_encoding='latin-1')
        return driver, tree, output

    def test_single_root_match_filter_and_relative_name(self):
        driver, tree, output = self.make(pattern='.packlist',
            directories={'/recipe/pack': listing('.packlist', 'other', 'xpacklist')},
            kinds={'/recipe/pack/.packlist': kind('FILE'),
                   '/recipe/pack/other': kind('FILE'),
                   '/recipe/pack/xpacklist': kind('FILE')})
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        # Historical ^(.packlist)$: the dot is regex syntax, not a literal dot.
        self.assertEqual([event.output_text for event in output.sink.events],
                         ['pack/.packlist\n', 'pack/xpacklist\n'])
        self.assertEqual([(r.operation, r.path) for r in tree.requests],
                         [('list', '/recipe/pack'),
                          ('classify', '/recipe/pack/.packlist'),
                          ('classify', '/recipe/pack/other'),
                          ('classify', '/recipe/pack/xpacklist')])
        self.assertNotIn('name', result.bodies[0].scope.local)

    def test_second_doperlmod_filename_regex_is_case_sensitive(self):
        driver, tree, output = self.make(
            root='/recipe/pack', pattern='perllocal.pod',
            directories={'/recipe/pack': listing('perllocal.pod',
                                                 'perllocalXpod', 'PERLLOCAL.POD')},
            kinds={'/recipe/pack/perllocal.pod': kind('FILE'),
                   '/recipe/pack/perllocalXpod': kind('FILE'),
                   '/recipe/pack/PERLLOCAL.POD': kind('FILE')})
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([event.output_text for event in output.sink.events],
                         ['/recipe/pack/perllocal.pod\n',
                          '/recipe/pack/perllocalXpod\n'])

    def test_dhcppxe_filename_reject_form_is_whole_basename_regex(self):
        driver, tree, output = self.make(
            root='pxe', prior='before',
            attributes='{ filename = .* } { reject = Root|Repository|Entries }',
            directories={'/recipe/pxe': listing('Root', 'sub', 'Repository',
                                                'Entries', 'Rootish', 'boot.cfg'),
                         '/recipe/pxe/sub': listing('kernel')},
            kinds={'/recipe/pxe/Root': kind('FILE'),
                   '/recipe/pxe/sub': kind('DIRECTORY'),
                   '/recipe/pxe/sub/kernel': kind('FILE'),
                   '/recipe/pxe/Repository': kind('FILE'),
                   '/recipe/pxe/Entries': kind('FILE'),
                   '/recipe/pxe/Rootish': kind('FILE'),
                   '/recipe/pxe/boot.cfg': kind('FILE')})
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([event.output_text for event in output.sink.events],
                         ['pxe/sub/kernel\n', 'pxe/Rootish\n', 'pxe/boot.cfg\n'])
        self.assertEqual(result.bodies[0].scope.local['name'], 'before')
        self.assertEqual([(r.operation, r.path) for r in tree.requests],
                         [('list', '/recipe/pxe'),
                          ('classify', '/recipe/pxe/Root'),
                          ('classify', '/recipe/pxe/sub'),
                          ('list', '/recipe/pxe/sub'),
                          ('classify', '/recipe/pxe/sub/kernel'),
                          ('classify', '/recipe/pxe/Repository'),
                          ('classify', '/recipe/pxe/Entries'),
                          ('classify', '/recipe/pxe/Rootish'),
                          ('classify', '/recipe/pxe/boot.cfg')])

    def test_tree_filename_and_reject_values_expand_before_regex_compilation(self):
        driver, tree, output = self.make(
            root='pxe', attributes='{ filename = $FILTER } { reject = $REJECT }',
            values={'FILTER': 'boot\\..*', 'REJECT': 'boot\\.old'},
            directories={'/recipe/pxe': listing('boot.cfg', 'boot.old', 'other')},
            kinds={'/recipe/pxe/boot.cfg': kind('FILE'),
                   '/recipe/pxe/boot.old': kind('FILE'),
                   '/recipe/pxe/other': kind('FILE')})
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([event.output_text for event in output.sink.events],
                         ['pxe/boot.cfg\n'])

    def test_reject_regex_failure_is_source_aware_before_observation(self):
        driver, tree, output = self.make(
            root='pxe', attributes='{ filename = .* } { reject = [ }')
        result = driver.build('out')
        self.assertEqual((result.status, result.reason),
                         ('FAILED', 'semantic_error'))
        self.assertEqual((result.span.source_id, result.span.start.line),
                         ('/recipe/main.aap', 2))
        self.assertEqual(tree.requests, [])
        self.assertEqual(output.sink.events, [])

    def test_name_uses_historical_item_quoting(self):
        driver, tree, output = self.make(
            body='    :move x y\n',
            directories={'/recipe/pack': listing('white space')},
            kinds={'/recipe/pack/white space': kind('FILE')})
        result = driver.build('out')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].scope.local['name'],
                         '"pack/white space"')
        self.assertEqual(result.bodies[0].tree_records[0].request.span.start.line, 2)
        self.assertEqual(result.blocked_at.span.start.line, 3)

    def test_recursive_list_order_before_directory_match_and_symlinks(self):
        directories = {'/recipe/pack': listing('z.txt', 'sub', 'a.txt', 'linkdir',
                                               'linkfile', 'dangling'),
                       '/recipe/pack/sub': listing('inside.txt')}
        kinds = {'/recipe/pack/z.txt': kind('FILE'),
                 '/recipe/pack/sub': kind('DIRECTORY'),
                 '/recipe/pack/sub/inside.txt': kind('FILE'),
                 '/recipe/pack/a.txt': kind('FILE'),
                 '/recipe/pack/linkdir': kind('SYMLINK', 'DIRECTORY'),
                 '/recipe/pack/linkfile': kind('SYMLINK', 'FILE'),
                 '/recipe/pack/dangling': kind('SYMLINK', 'MISSING')}
        driver, tree, output = self.make(pattern='.*', directories=directories,
                                         kinds=kinds)
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([event.output_text for event in output.sink.events],
                         ['pack/z.txt\n', 'pack/sub/inside.txt\n',
                          'pack/a.txt\n', 'pack/linkfile\n'])
        self.assertEqual([(r.operation, r.path) for r in tree.requests],
                         [('list', '/recipe/pack'),
                          ('classify', '/recipe/pack/z.txt'),
                          ('classify', '/recipe/pack/sub'),
                          ('list', '/recipe/pack/sub'),
                          ('classify', '/recipe/pack/sub/inside.txt'),
                          ('classify', '/recipe/pack/a.txt'),
                          ('classify', '/recipe/pack/linkdir'),
                          ('classify', '/recipe/pack/linkfile'),
                          ('classify', '/recipe/pack/dangling')])
        self.assertFalse(any(r.path == '/recipe/pack/linkdir' and r.operation == 'list'
                             for r in tree.requests))

    def test_name_is_current_scope_and_restored_after_each_success(self):
        driver, tree, output = self.make(
            body='    :print $name\n  :print $name\n', prior='before',
            directories={'/recipe/pack': listing('first', 'second')},
            kinds={'/recipe/pack/first': kind('FILE'),
                   '/recipe/pack/second': kind('FILE')})
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([event.output_text for event in output.sink.events],
                         ['pack/first\n', 'pack/second\n', 'before\n'])
        self.assertEqual(result.bodies[0].scope.local['name'], 'before')
        self.assertEqual(driver.scope.local['name'], 'before')

    def test_unreadable_and_missing_directory_skip_without_body(self):
        for status in ('UNREADABLE', 'MISSING'):
            driver, tree, output = self.make(
                directories={'/recipe/pack': kind(status)})
            result = driver.build('out')
            self.assertEqual(result.status, 'COMPLETE', status)
            self.assertEqual(output.sink.events, [])
            self.assertEqual(len(result.bodies[0].tree_records), 1)
            self.assertEqual(result.bodies[0].tree_records[0].observation.status, status)
            self.assertEqual(result.bodies[0].tree_records[0].warning,
                             'Cannot read directory "pack"')
        driver, tree, output = self.make(
            directories={'/recipe/pack': listing('sub', 'ok'),
                         '/recipe/pack/sub': kind('UNREADABLE')},
            kinds={'/recipe/pack/sub': kind('DIRECTORY'),
                   '/recipe/pack/ok': kind('FILE')})
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([event.output_text for event in output.sink.events],
                         ['pack/ok\n'])

    def test_unavailable_and_error_outcomes_stop_safely(self):
        for directories, kinds, status, reason in (
                ({}, {}, 'BLOCKED', 'unsupported_semantics'),
                ({'/recipe/pack': kind('ERROR', detail='EIO')}, {},
                 'FAILED', 'semantic_error'),
                ({'/recipe/pack': listing('entry')}, {},
                 'BLOCKED', 'unsupported_semantics'),
                ({'/recipe/pack': listing('entry')},
                 {'/recipe/pack/entry': kind('ERROR', detail='EACCES')},
                 'FAILED', 'semantic_error')):
            driver, tree, output = self.make(directories=directories, kinds=kinds)
            result = driver.build('out')
            self.assertEqual((result.status, result.reason), (status, reason))
            self.assertEqual(output.sink.events, [])
            self.assertTrue(result.bodies[0].tree_records)
        driver, tree, output = self.make(filesystem=False)
        result = driver.build('out')
        self.assertEqual((result.status, result.reason),
                         ('BLOCKED', 'unsupported_operation'))

    def test_body_block_and_failure_propagate_partial_effects(self):
        for second, status, reason in (
                ('    :move x y\n', 'BLOCKED', 'unsupported_operation'),
                ('    AFTER = $missing\n', 'FAILED', 'semantic_error')):
            driver, tree, output = self.make(
                body='    :print $name\n' + second, prior='before',
                directories={'/recipe/pack': listing('one', 'two')},
                kinds={'/recipe/pack/one': kind('FILE'),
                       '/recipe/pack/two': kind('FILE')})
            result = driver.build('out')
            self.assertEqual((result.status, result.reason), (status, reason))
            self.assertEqual([event.output_text for event in output.sink.events],
                             ['pack/one\n'])
            # Upstream restores only after Process returns; a stopped body
            # leaves the current match in its build dictionary.
            self.assertEqual(result.bodies[0].scope.local['name'], 'pack/one')
            self.assertEqual(driver.scope.local['name'], 'before')
            self.assertEqual([r.path for r in tree.requests],
                             ['/recipe/pack', '/recipe/pack/one'])

    def test_attribute_boundary_and_invalid_regex(self):
        for pattern, status, reason in (
                ('[', 'FAILED', 'semantic_error'),
                ('perllocal.pod } { rename = x', 'BLOCKED', 'unsupported_semantics')):
            driver, tree, output = self.make(pattern=pattern)
            result = driver.build('out')
            self.assertEqual((result.status, result.reason), (status, reason))
            self.assertEqual(tree.requests, [])

    def test_nano_doperlmod_moves_controlled_sed_result(self):
        driver, writer, reader, saved, process = fixture({'work/unpost_i': 'EXISTS'})
        result = driver.build('doperlmod')
        self.assertEqual((result.status, result.reason),
                         ('COMPLETE', 'requested_targets_complete'))
        self.assertNotIn('name', result.bodies[0].scope.local)
        self.assertEqual([(r.request.operation, r.request.path, r.matched)
                          for r in result.bodies[0].tree_records],
                         [('list', BASE + 'pack', False),
                          ('classify', BASE + 'pack/.packlist', True),
                          ('classify', BASE + 'pack/perllocal.pod', False),
                          ('list', BASE + 'pack', False),
                          ('classify', BASE + 'pack/.packlist', False),
                          ('classify', BASE + 'pack/perllocal.pod', True)])
        self.assertEqual(len(process.requests), 6)
        self.assertEqual(process.requests[3].command,
                         "sed -e 's!/authorized/ports/editors/nano/pack!!' < "
                         '/authorized/ports/editors/nano/pack/.packlist > '
                         '/authorized/ports/editors/nano/pack/.packlist.new')
        self.assertEqual(process.requests[-1].command,
                         'rm /authorized/ports/editors/nano/pack/perllocal.pod')
        move = driver.capabilities.move_backend
        self.assertEqual([(r.source, r.destination) for r in move.requests], [
            (BASE + 'pack/.packlist.new', BASE + 'pack/.packlist')])
        self.assertEqual(move.files[BASE + 'pack/.packlist'], b'controlled new packlist\n')
        self.assertNotIn(BASE + 'pack/.packlist.new', move.files)
        self.assertEqual(reader.observations, [('read', BASE + 'pack/perllocal.pod')])

    def test_lpp_tree_form_remains_unsupported(self):
        driver, writer, reader, saved, process = fixture()
        result = driver.build('chkcolon')
        self.assertEqual((result.status, result.reason),
                         ('BLOCKED', 'unsupported_semantics'))
        self.assertEqual(result.blocked_at.name, 'tree')
        self.assertEqual(driver.capabilities.tree_filesystem.requests, [])


if __name__ == '__main__':
    unittest.main()
