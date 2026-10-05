"""Commands.aap_update, DoBuild.target_update and rectest/test008 scenarios.

No recipe command invokes a host process, filesystem effect or persistence file.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, lower, BuildDriver, BodyExecutor,
    MemoryTargetState, MemoryPersistence, FileState, UpdateRequest,
    ProcessBackend, ProcessResult, ProcessPolicy, ChecksumBackend, MemoryArtifacts)


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


def metadata(text, **kwargs):
    return Evaluator(**kwargs).run(lower(parse(Source('/recipe/main.aap', text))))


class NestedUpdateTests(unittest.TestCase):
    def make(self, text, **kwargs):
        data = metadata(text)
        state, saved = MemoryTargetState(), MemoryPersistence()
        for definition in data.graph.definitions:
            for node in definition.targets:
                state.buildchecks[(definition.index, node.identity)] = 'prepared'
        executor = kwargs.pop('executor', ObservedExecutor())
        driver = BuildDriver(data.graph, state, saved, data.scope, data.declarations,
                             executor=executor, **kwargs)
        return driver, state, saved, executor

    def update(self, result, index=0):
        return result.bodies[index].updates[0]

    def test_success_request_and_resume(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  BEFORE = yes\n  :update B\n  AFTER = yes\n'
            'B {virtual}:\n  VALUE = nested\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A', 'B'])
        self.assertEqual(result.bodies[0].scope.local['AFTER'], 'yes')
        update = self.update(result)
        self.assertIsInstance(update.request, UpdateRequest)
        self.assertEqual(update.request.targets, ('B',))
        self.assertEqual(update.request.span.start.line, 3)
        self.assertIs(update.request.context, result.bodies[0].context)
        self.assertEqual(update.builds[0].bodies[0].status, 'COMPLETED')

    def test_already_complete_target(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\nB {virtual}:\n  :pass\n')
        driver.build('B')
        result = driver.build('A')
        self.assertEqual(self.update(result).builds[0].bodies, [])
        self.assertEqual(executor.entered, ['B', 'A'])
        self.assertEqual(self.update(result).builds[0].plan.entries[0].reason, 'already_updated')

    def test_twice_in_body(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\n  :update B\nB {virtual}:\n  :pass\n')
        result = driver.build('A')
        self.assertEqual(len(result.bodies[0].updates), 2)
        self.assertEqual(executor.entered, ['A', 'B'])
        self.assertEqual(result.bodies[0].updates[1].builds[0].bodies, [])

    def test_prerequisite_already_complete(self):
        driver, state, saved, executor = self.make(
            'A {virtual}: B\n  :update B\nB {virtual}:\n  :pass\n')
        self.assertEqual(driver.build('A').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['B', 'A'])

    def test_nested_prerequisite_order_and_diamond(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B C\nB {virtual}: X Y\n  :pass\n'
            'C {virtual}: X\n  :pass\nX {virtual}:\n  :pass\nY {virtual}:\n  :pass\n')
        self.assertEqual(driver.build('A').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A', 'X', 'Y', 'B', 'C'])

    def test_block_propagates_both_sites_and_stops_outer(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  FIRST = yes\n  :update B\n  LAST = no\n'
            'B {virtual}:\n  :mkdir forbidden\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'mkdir')
        self.assertEqual(result.blocked_at.span.start.line, 6)
        body = result.bodies[0]
        self.assertNotIn('LAST', body.scope.local)
        self.assertEqual(body.scope.local['FIRST'], 'yes')
        self.assertEqual(body.update_failure.request.span.start.line, 3)
        self.assertEqual(body.update_failure.target, 'B')
        self.assertEqual(body.update_failure.span.start.line, 6)
        self.assertEqual(driver.completed, {})
        self.assertEqual(saved.writes, [])

    def test_semantic_failure_propagates(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\n  LAST = no\nB {virtual}:\n  X = $UNDEFINED\n')
        result = driver.build('A')
        self.assertEqual((result.status, result.reason), ('FAILED', 'semantic_error'))
        self.assertEqual(result.error.span.start.line, 5)
        self.assertNotIn('LAST', result.bodies[0].scope.local)
        self.assertEqual(driver.observations.records(), ())

    def test_missing_target_and_invocation_diagnostic(self):
        driver, state, saved, executor = self.make('A {virtual}:\n  :update missing\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.error.span.source_id, '/recipe/main.aap')
        self.assertEqual(result.error.span.start.line, 2)
        self.assertEqual(executor.entered, ['A'])

    def test_existing_unregistered_file_is_valid_nested_target(self):
        driver, state, saved, executor = self.make('A {virtual}:\n  :update input\n')
        state.files['/recipe/input'] = FileState(True, 1, False)
        self.assertEqual(driver.build('A').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A'])
        self.assertNotIn('/recipe/input', driver.completed)

    def test_unregistered_file_requests_do_not_acquire_global_done_state(self):
        driver, state, saved, executor = self.make('A {virtual}:\n  :update input input\n')
        state.files['/recipe/input'] = FileState(True, 1, False)
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([b.plan.entries[0].reason for b in self.update(result).builds],
                         ['source_exists', 'source_exists'])
        self.assertNotIn('/recipe/input', driver.completed)

    def test_post_recheck_failure_propagates(self):
        state_holder = []
        def disappear(body):
            if body.target.name == 'B':
                state_holder[0].files['/recipe/B'] = FileState(False, 0, False)
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\n  AFTER = no\nB:\n  :pass\n',
            executor=ObservedExecutor(disappear))
        state_holder.append(state)
        state.files['/recipe/B'] = FileState(True, 1, False)
        result = driver.build('A')
        self.assertEqual((result.status, result.reason), ('FAILED', 'trigger_disappeared'))
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(saved.writes, [])
        self.assertEqual(driver.completed, {})
        self.assertEqual(self.update(result).span.source_id, '/recipe/main.aap')
        self.assertEqual(self.update(result).span.start.line, 4)
        self.assertEqual(result.span.start.line, 4)
        self.assertEqual(self.update(result).request.span.start.line, 2)

    def test_signature_unavailable_stops_before_nested_body(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\nB:\n  :pass\n')
        state.buildchecks.clear()
        result = driver.build('A')
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'buildcheck_unavailable'))
        self.assertEqual(executor.entered, ['A'])
        self.assertEqual(self.update(result).target, 'B')

    def test_shared_graph_and_declarations(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\nB {virtual}:\n  :action extract zip\n    :sys forbidden\n')
        result = driver.build('A')
        nested = self.update(result).builds[0].bodies[0]
        self.assertIs(nested.context.graph, driver.graph)
        self.assertIs(nested.context.declarations, driver.declarations)
        self.assertTrue(nested.declarations_changed)
        self.assertIn('extract', driver.declarations.actions)

    def test_same_process_artifact_and_policy_capabilities(self):
        class Process(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                self.requests.append(request)
                return ProcessResult(0, b'captured')
        process = Process()
        policy = ProcessPolicy('latin-1')
        artifacts = MemoryArtifacts({'/recipe/data': b''})
        checksum = ChecksumBackend(artifacts)
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\nB {virtual}:\n'
            '  :syseval controlled | :assign captured\n'
            '  :checksum data {md5=d41d8cd98f00b204e9800998ecf8427e}\n',
            process_backend=process, process_policy=policy, checksum_backend=checksum)
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        body = self.update(result).builds[0].bodies[0]
        self.assertIs(body.context.process_backend, process)
        self.assertIs(body.context.process_policy, policy)
        self.assertIs(body.context.checksum_backend, checksum)
        self.assertIs(body.context.update_driver, driver)
        self.assertEqual(body.scope.local['captured'], 'captured')
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(process.requests[0].cwd, '/recipe')
        self.assertEqual(saved.writes, [])

    def test_scope_isolation_caller_and_live_namespace(self):
        driver, state, saved, executor = self.make(
            'VALUE = recipe\nA {virtual}:\n  VALUE = caller\n  shared.x = before\n'
            '  :update B\n  AFTER = $shared.x\nB {virtual}:\n  SEEN = $VALUE\n'
            '  DEF = $_recipe.VALUE\n  VALUE = nested\n  shared.x = after\n'
            '  _caller.CALLER_WRITE = yes\n  _recipe.GLOBAL_WRITE = yes\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        outer = result.bodies[0]
        inner = self.update(result).builds[0].bodies[0]
        self.assertEqual(inner.scope.local['SEEN'], 'caller')
        self.assertEqual(inner.scope.local['DEF'], 'recipe')
        self.assertEqual(outer.scope.local['VALUE'], 'caller')
        self.assertEqual(outer.scope.local['AFTER'], 'after')
        self.assertEqual(outer.scope.local['CALLER_WRITE'], 'yes')
        self.assertEqual(driver.scope.local['GLOBAL_WRITE'], 'yes')
        self.assertIs(inner.context.invocation_scope, outer.scope)

    def test_nested_definition_cwd_and_caller_resolution(self):
        data = metadata('A {virtual}:\n  :update child/B\n  AFTER = yes\n')
        Evaluator(data.scope, graph=data.graph, declarations=data.declarations).run(
            lower(parse(Source('/recipe/child/defs.aap', 'B:\n  :pass\n'))))
        state, saved = MemoryTargetState(), MemoryPersistence()
        state.buildchecks[(1, '/recipe/child/B')] = 'prepared'
        driver = BuildDriver(data.graph, state, saved, data.scope, data.declarations)
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        update = self.update(result)
        self.assertEqual(update.request.cwd, '/recipe')
        self.assertEqual(update.builds[0].bodies[0].context.cwd, '/recipe/child')
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')

    def test_prerequisite_group_scope_shared_across_replans(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  VALUE = outer\n  :update B\n'
            'B {virtual}: C D\n  FROM_OUTER = $VALUE\n'
            'C {virtual}:\n  _caller.VALUE = group\n'
            'D {virtual}:\n  SEEN = $VALUE\n  _recipe.SEEN = $VALUE\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(driver.scope.local['SEEN'], 'group')
        nested = self.update(result).builds[0]
        self.assertIs(nested.bodies[0].context.invocation_scope,
                      nested.bodies[1].context.invocation_scope)
        # B's body uses the original :update caller, not its temporary
        # prerequisite-group dictionary (DoBuild.target_update/exec_commands).
        self.assertEqual(nested.bodies[-1].scope.local['FROM_OUTER'], 'outer')

    def test_nested_success_pending_when_outer_blocks(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\n  :sys forbidden\nB {virtual}:\n  :pass\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual([r.target for r in result.pending_signatures], ['B'])
        self.assertEqual(saved.writes, [])
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual([r.target for r in saved.writes], ['B'])

    def assert_cycle(self, text, entered):
        driver, state, saved, executor = self.make(text)
        result = driver.build('A')
        self.assertEqual((result.status, result.reason), ('FAILED', 'cycle'))
        self.assertEqual(executor.entered, entered)
        self.assertEqual(driver.observations.records(), ())
        self.assertEqual(driver.active_paths, ())
        return result

    def test_direct_self_update_is_cycle(self):
        result = self.assert_cycle('A {virtual}:\n  :update A\n', ['A'])
        self.assertEqual(result.error.span.start.line, 2)

    def test_indirect_update_cycle(self):
        self.assert_cycle('A {virtual}:\n  :update B\nB {virtual}:\n  :update A\n', ['A', 'B'])

    def test_prerequisite_update_mixed_cycle(self):
        self.assert_cycle('A {virtual}: B\n  :pass\nB {virtual}:\n  :update A\n', ['B'])

    def test_nested_prerequisite_cycle(self):
        self.assert_cycle('A {virtual}:\n  :update B\nB {virtual}: A\n  :pass\n', ['A'])

    def test_shared_active_dependency_cycle(self):
        self.assert_cycle('A Z {virtual}: B\n  :pass\nB {virtual}:\n  :update Z\n', ['B'])

    def test_three_levels_return_in_order(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\n  _recipe.A = yes\n'
            'B {virtual}:\n  :update C\n  _recipe.B = yes\nC {virtual}:\n  _recipe.C = yes\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A', 'B', 'C'])
        self.assertEqual([r.target for r in result.pending_signatures], ['C', 'B', 'A'])

    def test_multiple_targets_stop_on_first_failure(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B missing C\nB {virtual}:\n  :pass\nC {virtual}:\n  :pass\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(executor.entered, ['A', 'B'])
        self.assertEqual(len(self.update(result).builds), 2)
        self.assertEqual([r.target for r in result.pending_signatures], ['B'])

    def test_expand_once_quote_and_list_split(self):
        driver, state, saved, executor = self.make(
            'NAMES = B C\nA {virtual}:\n  :update $NAMES "space name" dollar$$name\n'
            'B {virtual}:\n  :pass\nC {virtual}:\n  :pass\n'
            '"space name" {virtual}:\n  :pass\ndollar$$name {virtual}:\n  :pass\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(self.update(result).request.targets, ('B', 'C', 'space name', 'dollar$name'))
        self.assertEqual(executor.entered, ['A', 'B', 'C', 'space name', 'dollar$name'])

    def test_empty_arguments_fail(self):
        driver, state, saved, executor = self.make('EMPTY =\nA {virtual}:\n  :update $EMPTY\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.error.span.start.line, 3)

    def test_options_and_patterns_block(self):
        for args in ('{force} B', '{searchpath=dir} B', 'B {virtual}', '*.o', '~name'):
            driver, state, saved, executor = self.make('A {virtual}:\n  :update ' + args + '\n')
            self.assertEqual(driver.build('A').status, 'BLOCKED', args)
            self.assertEqual(executor.entered, ['A'])

    def test_dynamic_new_target_resolved_after_first_argument(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'C {virtual}:\n  _recipe.SEEN = yes\n')
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B C\nB {virtual}:\n  :include new.aap\n', include_loader=Loader())
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A', 'B', 'C'])
        self.assertEqual(len(driver.graph.definitions), 3)
        self.assertTrue(self.update(result).builds[0].bodies[0].graph_changed)

    def test_nested_mutation_replans_pending_parent(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'A: extra\nextra {virtual}:\n  :pass\n')
        driver, state, saved, executor = self.make(
            'A {virtual}: B\n  :pass\nB {virtual}:\n  :update C\n'
            'C {virtual}:\n  :include more.aap\n', include_loader=Loader())
        self.assertEqual(driver.build('A').status, 'COMPLETE')
        self.assertEqual(executor.entered, ['B', 'C', 'extra', 'A'])

    def test_nested_mutation_does_not_restart_enclosing_body_prerequisites(self):
        class Loader(object):
            def load(self, path): return Source(path, 'A: missing\n')
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\n  AFTER = yes\nB {virtual}:\n  :include more.aap\n',
            include_loader=Loader())
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertNotIn('/recipe/missing', driver.completed)
        self.assertEqual(result.bodies[0].scope.local['AFTER'], 'yes')

    def test_nested_success_pending_after_outer_failure_and_no_finish(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B\n  X = $MISSING\nB:\n  :pass\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'FAILED')
        self.assertFalse(driver.closed)
        self.assertEqual(saved.writes, [])
        self.assertEqual([r.target for r in result.pending_signatures], ['/recipe/B'])
        self.assertIn('/recipe/B', driver.completed)
        self.assertNotIn('/recipe/A', driver.completed)
        self.assertEqual(driver.finish().status, 'COMPLETE')
        self.assertEqual(saved.signature('/recipe/B', '', 'buildcheck'), 'prepared')

    def test_shared_output_completion_reused(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update B C\nB C:\n  :pass\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A', 'B'])
        self.assertEqual(self.update(result).builds[1].bodies, [])

    def test_nested_multi_output_can_mark_active_sibling_done(self):
        # dep.in_use covers prerequisite traversal, not body execution.
        # A's nested B invocation of this shared body can mark A updated;
        # the subsequent :update A then returns before checking busy state.
        driver, state, saved, executor = self.make(
            'A B:\n  @if _no.buildtarget == "/recipe/A":\n'
            '    :update B\n    :update A\n')
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A', 'B'])
        self.assertEqual(result.bodies[0].updates[1].builds[0].bodies, [])

    def test_generated_update_block_retains_both_source_units(self):
        data = metadata('PORTNAME = sample\nPORTVERSION = 1\nPORTCOMMENT = sample\n'
                        'PORTDESCR = sample\ndo-dependcheck:\n  :sys forbidden\n')
        saved = MemoryPersistence()
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, data.scope, data.declarations)
        result = driver.build()
        self.assertEqual(result.status, 'BLOCKED')
        update = self.update(result)
        self.assertEqual(update.request.span.source_id, '<port defaults: /recipe>')
        self.assertEqual(update.request.span.start.line, 3)
        self.assertEqual(update.span.source_id, '/recipe/main.aap')
        self.assertEqual(update.span.start.line, 6)
        self.assertEqual(update.target, 'do-dependcheck')

    def test_nested_update_has_no_cli_alias_or_finally(self):
        driver, state, saved, executor = self.make(
            'A {virtual}:\n  :update input\nfinally:\n  :pass\n')
        state.files['/recipe/input'] = FileState(True, 1, False)
        result = driver.build('A')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(executor.entered, ['A', 'finally'])
        self.assertEqual([n.name for n in self.update(result).builds[0].plan.requests], ['input'])
        self.assertEqual(len(self.update(result).builds[0].plan.entries), 1)
        driver, state, saved, executor = self.make('A {virtual}:\n  :update update\n')
        result = driver.build('A')
        self.assertEqual([n.name for n in self.update(result).builds[0].plan.requests], ['update'])

    def test_without_driver_operation_remains_inert(self):
        result = metadata(':update B\nAFTER = no\n')
        self.assertFalse(result.complete)
        self.assertEqual(result.halted_at.name, 'update')
        self.assertNotIn('AFTER', result.scope.local)


if __name__ == '__main__':
    unittest.main()
