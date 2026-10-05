"""Source-derived DoBuild.may_exec_depend/target_update, Sign and Port cases.

The changed/unchanged input scenario comes from rectest/test003.py. All
artifacts, post-body changes and persistence are in-memory observations.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, BodyExecutor,
    MemoryTargetState, MemoryPersistence, FileState, PortDefaults,
    ProcessBackend, ProcessResult, ProcessPolicy, ChecksumBackend, MemoryArtifacts)


def recipe(text, scope=None, **kwargs):
    return Evaluator(scope, **kwargs).run(lower(parse(Source('/recipe/main.aap', text))))


class ObservedExecutor(object):
    def __init__(self, after=None):
        self.entered = []
        self.after = after

    def execute(self, context):
        self.entered.append(context.target.name)
        result = BodyExecutor().execute(context)
        if self.after is not None:
            self.after(result)
        return result


class DriverTests(unittest.TestCase):
    def make(self, text, state=None, persistence=None, **kwargs):
        metadata = recipe(text)
        state = state if state is not None else MemoryTargetState()
        persistence = persistence if persistence is not None else MemoryPersistence()
        for definition in metadata.graph.definitions:
            for node in definition.targets:
                state.buildchecks[(definition.index, node.identity)] = 'prepared before execution'
        driver = BuildDriver(metadata.graph, state, persistence, metadata.scope,
                             metadata.declarations, **kwargs)
        return driver, state, persistence

    def test_body_completion_then_recheck_then_pending_then_flush(self):
        driver, state, saved = self.make('out:\n  RESULT = yes\n')
        state.files['/recipe/out'] = FileState(True, 10, False)
        saved.time = '123.5'
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].status, 'COMPLETED')
        self.assertFalse(result.bodies[0].target_updated)
        self.assertEqual(result.completions[0].after.mtime, 10)
        self.assertEqual(result.completions[0].status, 'COMPLETE')
        self.assertIn('/recipe/out', driver.completed)
        self.assertEqual(saved.writes, [])
        self.assertEqual(result.pending_signatures[0].timestamp, '123.5')
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual(saved.signature('/recipe/out', '', 'buildcheck'), 'prepared before execution')
        self.assertEqual(saved.last_updates['/recipe/out'], '123.5')
        self.assertEqual(len(saved.writes), 1)

    def test_missing_before_and_after_can_complete(self):
        driver, state, saved = self.make('out:\n  :pass\n')
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertFalse(result.completions[0].before.exists)
        self.assertIsNone(result.completions[0].after)
        self.assertFalse(state.file_state(driver.graph.find_node('out')).exists)

    def test_preexisting_output_disappears_fails_no_success_persistence(self):
        state = MemoryTargetState()
        state.files['/recipe/out'] = FileState(True, 10, False)
        def remove_observation(result):
            state.files['/recipe/out'] = FileState(False, 0, False)
        driver, state, saved = self.make('out:\n  :pass\n', state,
                                        executor=ObservedExecutor(remove_observation))
        result = driver.build('out')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.reason, 'trigger_disappeared')
        self.assertNotIn('/recipe/out', driver.completed)
        self.assertEqual(driver.finish().pending_signatures, ())
        self.assertEqual(saved.writes, [])

    def test_virtual_skips_filesystem_recheck_and_build_signature(self):
        class NoFiles(MemoryTargetState):
            def file_state(self, node): raise AssertionError('virtual file probe')
            def build_signature(self, definition, target): raise AssertionError('virtual buildcheck')
        driver, state, saved = self.make('all:\n  :pass\n', NoFiles())
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.pending_signatures, ())
        self.assertIsNone(result.completions[0].before)

    def test_explicit_virtual_clears_old_signatures_without_buildcheck(self):
        driver, state, saved = self.make('phony {virtual}:\n  :pass\n')
        saved.signatures['phony'] = {('', 'buildcheck'): 'obsolete'}
        result = driver.build('phony')
        self.assertEqual(result.pending_signatures[0].values, {})
        driver.finish()
        self.assertNotIn('phony', saved.signatures)

    def test_buildcheck_is_prepared_before_body_and_not_recomputed_after(self):
        driver, state, saved = self.make('out:\n  :pass\n')
        def change_preparation(result):
            state.buildchecks[(0, '/recipe/out')] = 'after execution'
        driver.executor = ObservedExecutor(change_preparation)
        result = driver.build('out')
        self.assertEqual(result.pending_signatures[0].values[('', 'buildcheck')], 'prepared before execution')

    def test_updated_child_signature_observed_before_parent_decision(self):
        driver, state, saved = self.make('out: child\n  :pass\nchild:\n  :pass\n')
        def effects(result):
            node = result.target
            state.files[node.path] = FileState(True, 1, False)
            state.signatures[(node.identity, 'md5')] = 'post-body ' + node.name
        driver.executor = ObservedExecutor(effects)
        result = driver.build('out')
        self.assertEqual(driver.executor.entered, ['child', 'out'])
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.pending_signatures[-1].values[('/recipe/child', 'md5')], 'post-body child')

    def test_source_signature_cache_survives_body_without_source_update(self):
        driver, state, saved = self.make('out: input\n  :pass\n')
        state.files['/recipe/input'] = FileState(True, 1, False)
        state.signatures[('/recipe/input', 'md5')] = 'observed before'
        driver.executor = ObservedExecutor(lambda result: state.signatures.update({('/recipe/input', 'md5'): 'changed by observation'}))
        result = driver.build('out')
        # get_new_sign(force=1) still consults its cache; force is not rehash.
        self.assertEqual(result.pending_signatures[0].values[('/recipe/input', 'md5')], 'observed before')

    def test_blocked_body_stops_and_cannot_be_replayed(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('out:\n  :sys never\n  AFTER = no\n', executor=executor)
        result = driver.build('out')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'sys')
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertIs(driver.build('out'), result)
        self.assertEqual(executor.entered, ['out'])
        driver.finish()
        self.assertEqual(saved.writes, [])

    def test_failed_body_does_not_persist(self):
        driver, state, saved = self.make('out:\n  VALUE = $MISSING\n')
        result = driver.build('out')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(driver.completed, {})
        driver.finish()
        self.assertEqual(saved.writes, [])

    def test_later_failure_flushes_only_earlier_success_at_invocation_end(self):
        driver, state, saved = self.make('first:\n  :pass\nsecond:\n  :sys stop\n')
        result = driver.build(['first', 'second'])
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([r.target for r in result.pending_signatures], ['/recipe/first'])
        self.assertEqual(saved.writes, [])
        driver.finish()
        self.assertEqual([r.target for r in saved.writes], ['/recipe/first'])

    def test_signature_unavailable_before_body_blocks(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('out:\n  :pass\n', executor=executor)
        state.buildchecks.clear()
        result = driver.build('out')
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'buildcheck_unavailable'))
        self.assertEqual(executor.entered, [])
        driver.finish()
        self.assertEqual(saved.writes, [])

    def test_signature_unavailable_after_body_blocks_without_partial_records(self):
        # First virtual input makes update conclusive; second input's digest
        # is only required when recording successful source signatures.
        driver, state, saved = self.make('out: phony input\n  :pass\nphony {virtual}:\n')
        state.files['/recipe/input'] = FileState(True, 1, False)
        result = driver.build('out')
        self.assertEqual(result.bodies[0].status, 'COMPLETED')
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'signature_unavailable'))
        self.assertNotIn('/recipe/out', driver.completed)
        driver.finish()
        self.assertEqual(saved.writes, [])

    def test_same_invocation_does_not_repeat_virtual_or_missing_file(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('all: out\n  :pass\nout:\n  :pass\n', executor=executor)
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['out', 'all'])

    def test_independent_invocation_reexecutes_virtual_target(self):
        saved = MemoryPersistence()
        for unused in range(2):
            executor = ObservedExecutor()
            driver, state, saved = self.make('all:\n  :pass\n', persistence=saved, executor=executor)
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(executor.entered, ['all'])
            driver.finish()

    def test_rectest003_saved_signature_suppresses_then_changed_input_rebuilds(self):
        saved = MemoryPersistence()
        for value, count in (('original', 1), ('original', 0), ('changed', 1)):
            executor = ObservedExecutor()
            driver, state, saved = self.make('out: input\n  :pass\n', persistence=saved, executor=executor)
            state.files.update({'/recipe/out': FileState(True, 20, False), '/recipe/input': FileState(True, 10, False)})
            state.signatures[('/recipe/input', 'md5')] = value
            self.assertEqual(driver.build('out').status, 'COMPLETE')
            self.assertEqual(len(executor.entered), count)
            driver.finish()

    def test_ordered_diamond_prerequisites_only_execute_shared_once(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('all: left right\n  :pass\n'
            'left {virtual}: shared\n  :pass\nright {virtual}: shared\n  :pass\n'
            'shared {virtual}:\n  :pass\n', executor=executor)
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['shared', 'left', 'right', 'all'])

    def test_multi_target_success_covers_missing_sibling_and_its_other_dependencies(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('a b:\n  :pass\nb: unavailable\n', executor=executor)
        result = driver.build(['a', 'b'])
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['a'])
        self.assertEqual([r.target for r in result.pending_signatures], ['/recipe/a', '/recipe/b'])
        self.assertIn('/recipe/b', driver.completed)

    def test_standard_sibling_is_not_covered(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('all clean:\n  :pass\n', executor=executor)
        self.assertEqual(driver.build(['all', 'clean']).status, 'COMPLETE')
        self.assertEqual(executor.entered, ['all', 'clean'])

    def test_standard_multiple_bodies_after_all_prerequisites(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('all: left\n  :pass\nall: right\n  :pass\n'
            'left {virtual}:\n  :pass\nright {virtual}:\n  :pass\n', executor=executor)
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['left', 'right', 'all', 'all'])

    def test_cycle_fails_without_execution(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('all: x\nx {virtual}: all\n  :pass\n', executor=executor)
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.reason, 'cycle')
        self.assertEqual(executor.entered, [])

    def test_dynamic_addition_is_visible_to_pending_parent(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'all: extra\nextra {virtual}:\n  :pass\n')
        executor = ObservedExecutor()
        driver, state, saved = self.make('all: child\n  :pass\nchild {virtual}:\n  :include added.aap\n',
                                        include_loader=Loader(), executor=executor)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['child', 'extra', 'all'])
        self.assertTrue(result.completions[0].graph_changed)

    def test_own_body_new_prerequisite_waits_until_later_invocation(self):
        class Loader(object):
            def load(self, path): return Source(path, 'all: missing\n')
        driver, state, saved = self.make('all:\n  :include added.aap\n', include_loader=Loader())
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(driver.build('all').bodies, [])
        self.assertNotIn('/recipe/missing', driver.completed)

    def test_body_can_append_ordered_standard_body_without_retraversing_sources(self):
        class Loader(object):
            def load(self, path): return Source(path, 'all: missing\n  _recipe.SEEN = yes\n')
        executor = ObservedExecutor()
        driver, state, saved = self.make('all:\n  :include added.aap\n', include_loader=Loader(), executor=executor)
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['all', 'all'])
        self.assertEqual(driver.scope.local['SEEN'], 'yes')

    def test_unrelated_dynamic_definition_is_not_automatically_scheduled(self):
        class Loader(object):
            def load(self, path): return Source(path, 'unused:\n  :sys never\n')
        driver, state, saved = self.make('all:\n  :include added.aap\n', include_loader=Loader())
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertNotIn('/recipe/unused', driver.completed)

    def test_selected_default_is_not_reselected_after_body_changes_TARGET(self):
        driver, state, saved = self.make('TARGET = all\nall:\n  _recipe.TARGET = other\nother:\n  :sys stop\n')
        self.assertEqual(driver.build().status, 'COMPLETE')
        self.assertEqual(driver.build().blocked_at.name, 'sys')

    def test_persistence_failure_is_separate_from_verified_completion(self):
        class Unwritable(MemoryPersistence):
            def flush(self, records): raise OSError('read-only state store')
        driver, state, saved = self.make('out:\n  :pass\n', persistence=Unwritable())
        self.assertEqual(driver.build('out').status, 'COMPLETE')
        result = driver.finish()
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'persistence_unavailable'))
        self.assertIn('/recipe/out', driver.completed)
        self.assertEqual(saved.writes, [])

    def test_pending_state_is_not_shared_until_finish(self):
        driver, state, saved = self.make('out:\n  :pass\n')
        driver.build('out')
        self.assertEqual(saved.signature('/recipe/out', '', 'buildcheck'), '')
        self.assertEqual(driver.observations.stored_signature(driver.graph.find_node('out'), None, 'buildcheck'), 'prepared before execution')

    def test_automatic_build_directory_creation_remains_a_barrier(self):
        executor = ObservedExecutor()
        driver, state, saved = self.make('build-test/out:\n  :pass\n', executor=executor)
        result = driver.build('build-test/out')
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'build_directory_preparation'))
        self.assertEqual(executor.entered, [])

    def test_persistence_timestamp_unavailable_does_not_mark_success(self):
        driver, state, saved = self.make('out:\n  :pass\n')
        saved.time = None
        result = driver.build('out')
        self.assertEqual(result.bodies[0].status, 'COMPLETED')
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'observation_unavailable'))
        self.assertNotIn('/recipe/out', driver.completed)
        driver.finish()
        self.assertEqual(saved.writes, [])

    def test_newer_check_retains_historical_persistence_key(self):
        driver, state, saved = self.make('DEFAULTCHECK = newer\nout: input\n  :pass\n')
        state.files['/recipe/input'] = FileState(True, 20, False)
        state.files['/recipe/out'] = FileState(True, 10, False)
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.pending_signatures[0].values[('/recipe/input', 'newer')], 'unknown')

    def test_post_body_automatic_dependencies_block_parent_without_replaying_child(self):
        driver, state, saved = self.make('all: child\n  :pass\nchild:\n  :pass\n')
        driver.executor = ObservedExecutor(lambda result: state.implicit.update({result.target.identity: True}))
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.reason, 'automatic_dependencies')
        self.assertEqual(driver.executor.entered, ['child'])
        self.assertIn('/recipe/child', driver.completed)
        self.assertNotIn('/recipe/all', driver.completed)


class PortDriverTests(unittest.TestCase):
    def metadata(self, text='do-checksum:\n  :pass\n'):
        scope = Scope.top_level()
        scope.local.update({'PORTNAME': 'fixture', 'PORTVERSION': '1',
                            'PORTCOMMENT': 'fixture', 'PORTDESCR': 'fixture'})
        return recipe(text, scope)

    def driver(self, metadata, saved=None, **kwargs):
        return BuildDriver(metadata.graph, MemoryTargetState(), saved or MemoryPersistence(),
                           metadata.scope, metadata.declarations, **kwargs)

    def test_port_hook_virtualized_but_explicit_request_does_not_run_stages(self):
        metadata = self.metadata()
        saved = MemoryPersistence()
        driver = self.driver(metadata, saved)
        result = driver.build('do-checksum')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertTrue(metadata.graph.find_node('do-checksum').virtual)
        self.assertEqual([b.target.name for b in result.bodies], ['do-checksum'])
        self.assertEqual(result.pending_signatures[0].values, {})
        driver.finish()
        self.assertEqual(saved.markers, {})

    def test_done_marker_skips_stage_body_at_initialization_not_prerequisites(self):
        metadata = self.metadata()
        saved = MemoryPersistence()
        saved.markers['/recipe/done/checksum'] = b'any content'
        defaults = PortDefaults(metadata.graph, metadata.scope, '/recipe', saved)
        node = metadata.graph.find_node('checksum')
        self.assertEqual(node.body_definitions, ())
        self.assertEqual([n.name for n in node.definitions[0].prerequisites], ['fetch'])
        self.assertFalse(metadata.graph.find_node('do-checksum').virtual)
        # Marker state is captured at initialization, not a per-visit switch.
        saved.markers.clear()
        self.assertEqual(node.body_definitions, ())
        self.assertIn(('checksum', 'fetch', 'checksum', True, True), defaults.stages)

    def test_default_graph_encodes_actual_order_and_crosses_nested_update(self):
        metadata = self.metadata('do-dependcheck:\n  :pass\n')
        driver = self.driver(metadata)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.plan.requests[0].name, 'all')
        self.assertEqual(result.bodies[0].target.name, 'dependcheck')
        self.assertEqual(result.bodies[0].updates[0].status, 'COMPLETE')
        self.assertEqual(result.bodies[-1].target.name, 'fetchdepend')
        self.assertIn('port_fetchdepend', str(result.error))
        self.assertEqual([name for name, parent, check, create, skipped in driver.port.stages[:10]],
                         ['dependcheck', 'fetchdepend', 'fetch', 'checksum', 'extractdepend',
                          'extract', 'patch', 'builddepend', 'config', 'build'])
        self.assertIn('/recipe/do-dependcheck', driver.completed)

    def test_default_helper_stays_deferred_when_hook_absent(self):
        driver = self.driver(self.metadata())
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        self.assertIn('port_dependcheck', str(result.error))

    def test_generated_markers_are_after_hooks_and_not_implicit_target_done(self):
        metadata = self.metadata()
        driver = self.driver(metadata)
        driver.build('do-checksum')
        text = metadata.graph.find_node('checksum').body_definitions[0].body.origin.text
        self.assertLess(text.index(':update do-checksum'), text.index(':mkdir'))
        self.assertIn(':touch {force} done/checksum', text)
        self.assertEqual(driver.persistence.markers, {})

    def test_marker_observation_unavailable_blocks_without_execution(self):
        class Unavailable(MemoryPersistence):
            def marker_exists(self, path): raise NotImplementedError('no marker view')
        driver = self.driver(self.metadata(), Unavailable())
        result = driver.build('do-checksum')
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'observation_unavailable'))
        self.assertEqual(result.bodies, [])

    def test_existing_stage_marker_does_not_complete_explicit_hook_request(self):
        saved = MemoryPersistence()
        saved.markers['/recipe/done/checksum'] = b''
        driver = self.driver(self.metadata(), saved)
        result = driver.build('do-checksum')
        # Skipped stage construction did not virtualize its hook; independent
        # direct execution still needs the nonvirtual prepared-buildcheck gate.
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'buildcheck_unavailable'))
        self.assertNotIn('/recipe/do-checksum', driver.completed)

    def test_nano_explicit_completion_repeat_and_default_boundary(self):
        class FakeProcess(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                output = (b'SUSE15', b'suse', b'SUSE 15 6')[len(self.requests)]
                self.requests.append(request)
                return ProcessResult(0, output)
        source = Source('/authorized/ports/globals.aap', Source.from_path(
            os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1').text)
        class Loader(object):
            def load(self, path):
                if path != source.source_id: raise AssertionError('unauthorized recipe')
                return source
        archive = '/authorized/ports/editors/nano/distfiles/nano-7.1.tar.gz'
        class RecordedDigest(ChecksumBackend):
            def __init__(self):
                super(RecordedDigest, self).__init__(MemoryArtifacts({archive: b'synthetic'}))
                self.requests = []
            def md5(self, request):
                if request.path != archive: raise AssertionError('unexpected artifact')
                self.requests.append(request)
                return 'cc9e42c4805193f9dc3ae22b9644bb1d'
        scope = Scope.top_level()
        scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build', 'DISTDIR': 'distfiles',
                            'PKGDIR': '/authorized/ports/editors/nano/pack'})
        process = FakeProcess()
        text = Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/editors/nano/main.aap'), 'latin-1').text
        metadata = Evaluator(scope, include_loader=Loader(), process_backend=process,
                             process_policy=ProcessPolicy('latin-1')).run(lower(parse(
                                 Source('/authorized/ports/editors/nano/main.aap', text))))
        self.assertTrue(metadata.complete)
        self.assertEqual((len(metadata.graph.definitions), len(metadata.graph.targets), len(metadata.graph.nodes)), (35, 35, 39))
        saved, checksum = MemoryPersistence(), RecordedDigest()
        driver = BuildDriver(metadata.graph, MemoryTargetState(), saved, scope, metadata.declarations,
                             process_backend=process, process_policy=ProcessPolicy('latin-1'),
                             checksum_backend=checksum)
        result = driver.build('do-checksum')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].status, 'COMPLETED')
        self.assertEqual(result.bodies[0].checksums[0].status, 'VERIFIED')
        self.assertIsNone(result.completions[0].before)
        self.assertIsNone(result.completions[0].after)
        self.assertFalse(result.bodies[0].context.step.buildcheck_required)
        self.assertEqual(result.pending_signatures[0].values, {})
        self.assertEqual(result.pending_signatures[0].target, 'do-checksum')
        self.assertEqual(driver.build('do-checksum').bodies, [])
        self.assertEqual(len(checksum.requests), 1)
        self.assertEqual(saved.markers, {})
        package = driver.build()
        self.assertEqual(package.status, 'BLOCKED')
        self.assertEqual(package.plan.requests[0].name, 'all')
        self.assertEqual([b.target.name for b in package.bodies],
                         ['dependcheck', 'fetchdepend', 'fetch'])
        for body, hook, line in ((package.bodies[0], 'do-dependcheck', 512),
                                 (package.bodies[1], 'do-fetchdepend', 521)):
            self.assertEqual(body.status, 'COMPLETED')
            update = body.updates[0]
            self.assertEqual(update.status, 'COMPLETE')
            self.assertEqual(update.request.targets, (hook,))
            self.assertEqual(update.request.span.source_id,
                             '<port defaults: /authorized/ports/editors/nano>')
            nested = update.builds[0].bodies[0]
            self.assertEqual(nested.status, 'COMPLETED')
            self.assertEqual(nested.definition.span.start.line, line)
            self.assertEqual(nested.definition.span.source_id, '/authorized/ports/globals.aap')
            self.assertEqual(nested.context.scope.local['target_list'], [hook])
            self.assertEqual(nested.context.scope.local['source_list'], [])
            self.assertEqual(nested.context.cwd, '/authorized/ports/editors/nano')
            self.assertEqual(nested.processes, ())
            self.assertEqual(nested.definition.prerequisites, ())
        self.assertEqual(package.bodies[0].updates[0].request.span.start.line, 3)
        self.assertEqual(package.bodies[0].updates[0].builds[0].bodies[0].scope.local['broken'], '')
        self.assertEqual(package.bodies[1].updates[0].builds[0].bodies[0].scope.local['deplist'], [])
        self.assertEqual(package.bodies[-1].status, 'BLOCKED')
        self.assertEqual(package.bodies[-1].program.statements[0].fragment.code,
                         'port_fetch(globals())')
        self.assertEqual(package.error.span.source_id,
                         '<port defaults: /authorized/ports/editors/nano>')
        self.assertNotIn('/authorized/ports/editors/nano/fetch', driver.completed)
        self.assertEqual(len(process.requests), 3)
        self.assertEqual(len(checksum.requests), 1)
        self.assertEqual((len(metadata.graph.definitions), len(metadata.graph.targets), len(metadata.graph.nodes)), (57, 57, 58))
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual([r.target for r in saved.writes],
                         ['do-checksum', 'do-dependcheck', 'dependcheck',
                          'do-fetchdepend', 'fetchdepend'])
        self.assertEqual(saved.writes[0].values, {})
        self.assertEqual(saved.markers, {})


if __name__ == '__main__':
    unittest.main()
