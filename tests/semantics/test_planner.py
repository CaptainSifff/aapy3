"""Read-only scenarios from DoBuild/Sign and rectest003/004/018.

No historical drivers, recipe commands, stat calls or external processes run.
Signature tokens below are explicit oracle observations, not inferred hashes.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, UpdatePlanner,
    MemoryTargetState, FileState, buildcheck_digest, PlanningDiagnostic,
    ProcessBackend, ProcessResult, ProcessPolicy, Unsupported)


def recipe(text):
    return Evaluator().run(lower(parse(Source('/recipe/main.aap', text))))


def planner(text, state=None):
    result = recipe(text)
    return UpdatePlanner(result.graph, state or MemoryTargetState(), result.scope)


def identities(plan):
    return tuple(e.target.identity for e in plan.entries)


def current_state(graph):
    state = MemoryTargetState()
    for node in graph.nodes:
        state.files[node.path] = FileState(True, 100, False)
        state.signatures[(node.identity, 'md5')] = 'unchanged input'
        for definition in node.definitions:
            for child in definition.prerequisites:
                state.stored[(node.identity, child.identity, 'md5')] = 'unchanged input'
            if definition.body is not None:
                state.buildchecks[(definition.index, node.identity)] = 'unchanged commands'
                state.stored[(node.identity, '', 'buildcheck')] = 'unchanged commands'
    return state


class PlannerTests(unittest.TestCase):
    def test_explicit_lookup_normalizes_paths_without_graph_mutation(self):
        p = planner('out:\n  :sys never\n')
        snapshot = p.graph.snapshot()
        result = p.plan('./out')
        self.assertIs(result.requests[0], p.graph.find_node('out'))
        self.assertEqual(identities(result), ('/recipe/out',))
        self.assertEqual(p.graph.snapshot(), snapshot)

    def test_missing_requested_node_is_local_and_fails(self):
        p = planner('out:\n')
        count = len(p.graph.nodes)
        result = p.plan('unknown')
        self.assertFalse(result.complete)
        self.assertEqual(result.diagnostic.code, 'no_build_commands')
        self.assertIn('<target request>:1:1:', str(result.diagnostic))
        self.assertEqual(len(p.graph.nodes), count)

    def test_unregistered_existing_file_is_not_a_buildable_toplevel_target(self):
        state = MemoryTargetState()
        state.files['/recipe/known'] = FileState(True, 100, False)
        p = planner('out:\n', state)
        self.assertEqual(p.plan('known').entries[-1].status, 'failed')

    def test_default_is_not_first_dependency(self):
        result = planner('first:\n  :sys never\n').plan()
        self.assertEqual(result.requests, ())
        self.assertEqual(result.entries, [])
        self.assertTrue(result.complete)

    def test_default_all_then_target_variable_precedence(self):
        p = planner('all:\n  :sys never\nother:\n  :sys never\n')
        self.assertEqual(p.plan().requests[0].name, 'all')
        p.scope.local['TARGET'] = 'other all'
        self.assertEqual(tuple(n.name for n in p.plan().requests), ('other', 'all'))

    def test_build_rule_targets_are_explicit_default_input(self):
        r = recipe('a:\n  :pass\nb:\n  :pass\n')
        p = UpdatePlanner(r.graph, MemoryTargetState(), r.scope,
                          build_rule_targets=('b', 'a'))
        self.assertEqual(tuple(n.name for n in p.plan().requests), ('b', 'a'))

    def test_defaults_and_finally_use_virtual_name_not_path_alias(self):
        p = planner('./all:\n  :pass\n./finally:\n  :pass\n./update:\n  :pass\n')
        self.assertEqual(p.plan().entries, [])
        self.assertEqual(tuple(n.name for n in p.plan('update').requests), ('fetch',))
        self.assertEqual(p.plan('all').requests[0].name, './all')

    def test_requested_variable_uses_existing_expander(self):
        p = planner('NAME = out\nout:\n  :pass\n')
        self.assertEqual(p.plan('$NAME').requests[0].name, 'out')
        with self.assertRaises(Unsupported):
            p.plan('*.o')
        with self.assertRaises(PlanningDiagnostic):
            p.plan('')

    def test_update_alias_empty_fetch_succeeds_but_candidates_block(self):
        p = planner('all:\n  :pass\n')
        result = p.plan('update')
        self.assertEqual(tuple(n.name for n in result.requests), ('fetch', 'all'))
        self.assertEqual(identities(result), ('fetch', 'all'))
        self.assertEqual(result.entries[0].reason, 'empty_fetch')
        self.assertTrue(result.complete)
        p.graph.find_node('all').attributes['fetch'] = 'unavailable'
        result = p.plan('fetch')
        self.assertEqual(result.diagnostic.code, 'automatic_target')
        self.assertEqual(result.bodies, [])

    def test_explicit_update_definition_overrides_alias(self):
        result = planner('update:\n  :pass\n').plan('update')
        self.assertEqual(identities(result), ('update',))
        self.assertEqual(result.entries[0].reason, 'virtual_target')

    def test_missing_target_zero_sources_selects_body(self):
        p = planner('out:\n  @invalid +\n')
        result = p.plan('out')
        entry = result.entries[0]
        self.assertEqual((entry.status, entry.reason), ('update', 'missing_target'))
        step = result.bodies[0]
        self.assertIs(step.definition, p.graph.definitions[0])
        self.assertIs(step.scope, p.scope)
        self.assertEqual(step.cwd, '/recipe')
        self.assertEqual(step.body.span.start.line, 2)
        self.assertEqual(step.body.source.slice(step.body.span), '  @invalid +\n')
        self.assertFalse(step.body.origin.scanned)

    def test_source_is_checked_before_target(self):
        p = planner('out: input\n  :sys never\n')
        p.state = current_state(p.graph)
        result = p.plan('out')
        self.assertEqual(identities(result), ('/recipe/input', '/recipe/out'))
        self.assertEqual(tuple(e.reason for e in result.entries),
                         ('source_exists', 'signature_current'))
        self.assertEqual(result.bodies, [])

    def test_order_across_repeated_definitions_and_duplicate_references(self):
        p = planner('out: a b a\nout: c b\n  :sys never\n')
        p.state = current_state(p.graph)
        result = p.plan('out')
        self.assertEqual(identities(result), ('/recipe/a', '/recipe/b', '/recipe/c', '/recipe/out'))
        relations = result.entries[-1].prerequisites
        self.assertEqual(tuple(item.name for d, item, e in relations), ('a', 'b', 'a', 'c', 'b'))
        self.assertIs(relations[0][2], relations[2][2])
        self.assertEqual(tuple(d.index for d, item, e in relations), (0, 0, 0, 1, 1))

    def test_standard_virtual_ignores_file_and_signature_observations(self):
        class NoFiles(MemoryTargetState):
            def file_state(self, node):
                raise AssertionError('virtual planning read a file')
            def build_signature(self, definition, target):
                raise AssertionError('virtual planning read buildcheck')
        result = planner('build:\n  :sys never\n', NoFiles()).plan('build')
        self.assertEqual(result.entries[-1].reason, 'virtual_target')

    def test_explicit_sticky_virtual_always_updates(self):
        p = planner('phony {virtual}:\nphony {virtual=}:\n  :sys never\n')
        result = p.plan('phony')
        self.assertEqual(result.entries[0].status, 'update')
        self.assertTrue(result.entries[0].virtual)

    def test_current_file_and_body_signature_changes(self):
        p = planner('out:\n  :sys never\n')
        p.state = current_state(p.graph)
        self.assertEqual(p.plan('out').entries[-1].status, 'current')
        p.state.buildchecks[(0, '/recipe/out')] = 'new commands'
        self.assertEqual(p.plan('out').entries[-1].reason, 'buildcheck_changed')
        del p.state.stored[('/recipe/out', '', 'buildcheck')]
        self.assertEqual(p.plan('out').entries[-1].reason, 'buildcheck_missing')

    def test_unknown_buildcheck_blocks_instead_of_hashing_raw_body(self):
        p = planner('out:\n  :do arbitrary $UNKNOWN\n')
        p.state.files['/recipe/out'] = FileState(True, 100, False)
        result = p.plan('out')
        self.assertEqual(result.diagnostic.code, 'buildcheck_unavailable')
        self.assertEqual(result.bodies, [])
        self.assertFalse(p.graph.definitions[0].body.origin.scanned)

    def test_default_md5_does_not_use_mtime_order(self):
        p = planner('out: input\n  :pass\n')
        p.state = current_state(p.graph)
        p.state.files['/recipe/input'] = FileState(True, 999, False)
        self.assertEqual(p.plan('out').entries[-1].status, 'current')
        p.state.signatures[('/recipe/input', 'md5')] = 'different bytes'
        p.state.files['/recipe/input'] = FileState(True, 1, False)
        self.assertEqual(p.plan('out').entries[-1].reason, 'signature_changed')

    def test_missing_old_signature_forces_update(self):
        p = planner('out: input\n  :pass\n')
        p.state = current_state(p.graph)
        del p.state.stored[('/recipe/out', '/recipe/input', 'md5')]
        self.assertEqual(p.plan('out').entries[-1].reason, 'signature_missing')

    def test_newer_check_and_historical_first_timestamp_short_circuit(self):
        p = planner('DEFAULTCHECK = newer\nout: a b\n  :pass\n')
        p.state = current_state(p.graph)
        p.state.files['/recipe/a'] = FileState(True, 50, False)
        p.state.files['/recipe/b'] = FileState(True, 150, False)
        # Update.outdated considers a positive time conclusive and skips b.
        self.assertEqual(p.plan('out').entries[-1].status, 'current')
        p.state.files['/recipe/a'] = FileState(True, 150, False)
        self.assertEqual(p.plan('out').entries[-1].reason, 'newer_prerequisite')

    def test_time_check_compares_stored_time_not_target_time(self):
        p = planner('DEFAULTCHECK = time\nout: input\n  :pass\n')
        p.state = current_state(p.graph)
        p.state.stored[('/recipe/out', '/recipe/input', 'time')] = '100'
        self.assertEqual(p.plan('out').entries[-1].status, 'current')
        p.state.stored[('/recipe/out', '/recipe/input', 'time')] = '101'
        self.assertEqual(p.plan('out').entries[-1].reason, 'signature_changed')

    def test_signature_error_and_unavailable_are_distinct(self):
        p = planner('out: input\n  :pass\n')
        p.state = current_state(p.graph)
        p.state.signatures[('/recipe/input', 'md5')] = ''
        self.assertEqual(p.plan('out').diagnostic.code, 'signature_error')
        p.state.signatures[('/recipe/input', 'md5')] = None
        self.assertEqual(p.plan('out').diagnostic.code, 'signature_unavailable')

    def test_signature_cache_is_per_invocation(self):
        class Count(MemoryTargetState):
            def __init__(self):
                super(Count, self).__init__()
                self.calls = []
            def current_signature(self, node, check):
                self.calls.append((node.identity, check))
                return 'same'
        state = Count()
        state.files['/recipe/input'] = FileState(True, 100, False)
        state.stored[('/recipe/out', '/recipe/input', 'md5')] = 'same'
        p = planner('out: input input\n  :pass\n', state)
        p.plan('out')
        self.assertEqual(state.calls, [('/recipe/input', 'md5')])
        p.plan('out')
        self.assertEqual(len(state.calls), 2)

    def test_explicit_disabled_buildcheck_does_not_need_stored_signature(self):
        p = planner('out:\n  :pass\n')
        p.state.files['/recipe/out'] = FileState(True, 100, False)
        p.state.buildchecks[(0, '/recipe/out')] = ''
        p.state.stored[('/recipe/out', '', 'buildcheck')] = None
        self.assertEqual(p.plan('out').entries[-1].status, 'current')

    def test_bodyless_virtual_aggregate_including_empty_declaration(self):
        p = planner('all: leaf\nleaf {virtual}:\n')
        result = p.plan('all')
        self.assertTrue(result.complete)
        self.assertEqual(tuple(e.reason for e in result.entries),
                         ('virtual_aggregate', 'virtual_aggregate'))
        self.assertEqual(result.bodies, [])

    def test_bodyless_virtual_without_declaration_fails(self):
        result = planner('out: test\n  :pass\n').plan('out')
        self.assertEqual(identities(result), ('test', '/recipe/out'))
        self.assertEqual(result.diagnostic.code, 'no_build_commands')
        self.assertEqual(result.entries[-1].reason, 'prerequisite_failed')

    def test_bodyless_existing_source_succeeds_but_direct_request_fails(self):
        p = planner('out: input\n  :pass\ninput:\n')
        p.state = current_state(p.graph)
        self.assertTrue(p.plan('out').complete)
        self.assertEqual(p.plan('input').entries[-1].status, 'failed')

    def test_existing_directory_bypasses_rule_matching(self):
        p = planner('directory:\n')
        p.state.files['/recipe/directory'] = FileState(True, 100, True)
        p.state.rules['/recipe/directory'] = True
        self.assertEqual(p.plan('directory').entries[-1].reason, 'directory_exists')

    def test_missing_prerequisite_stops_later_siblings_and_requested_targets(self):
        result = planner('out: missing later\n  :pass\nlater:\n  :pass\n').plan(['out', 'later'])
        self.assertEqual(identities(result), ('/recipe/missing', '/recipe/out'))
        self.assertEqual(result.bodies, [])
        self.assertIn('/recipe/main.aap:1:', str(result.diagnostic))

    def test_direct_self_dependency_is_ignored(self):
        result = planner('build: build\n  :pass\n').plan('build')
        self.assertTrue(result.complete)
        self.assertEqual(result.entries[0].prerequisites[0][2], 'self_ignored')
        self.assertEqual(len(result.bodies), 1)

    def test_longer_cycle_detected_before_body_selection(self):
        result = planner('a: b\n  :pass\nb: a\n  :pass\n').plan('a')
        self.assertFalse(result.complete)
        self.assertEqual(result.diagnostic.code, 'cycle')
        self.assertEqual(result.bodies, [])
        self.assertEqual(identities(result), ('/recipe/b', '/recipe/a'))

    def test_shared_definition_reentrancy_is_cycle(self):
        result = planner('a b: b\n  :pass\n').plan('a')
        self.assertEqual(result.diagnostic.code, 'cycle')
        self.assertEqual(result.bodies, [])

    def test_diamond_memoizes_one_virtual_update_per_invocation(self):
        p = planner('all: left right\n  :pass\nleft {virtual}: leaf\n  :pass\n'
                    'right {virtual}: leaf\n  :pass\nleaf {virtual}:\n  :pass\n')
        one = p.plan(['all', 'leaf', 'all'])
        two = p.plan('all')
        self.assertEqual(identities(one), ('leaf', 'left', 'right', 'all'))
        self.assertEqual(tuple(b.target.name for b in one.bodies), ('leaf', 'left', 'right', 'all'))
        self.assertEqual(one.snapshot(), two.snapshot())
        self.assertIsNot(one.states, two.states)
        self.assertTrue(all(not hasattr(n, 'status') for n in p.graph.nodes))

    def test_file_parent_rechecks_after_prerequisite_body(self):
        p = planner('out: input\n  :pass\ninput:\n  :pass\n')
        p.state = current_state(p.graph)
        p.state.buildchecks[(1, '/recipe/input')] = 'changed'
        result = p.plan('out')
        self.assertEqual(tuple(e.status for e in result.entries), ('update', 'recheck'))
        self.assertEqual(tuple(b.mode for b in result.bodies), ('update', 'conditional'))
        self.assertEqual(result.entries[-1].after, (0,))
        self.assertTrue(result.requires_recheck)

    def test_later_unrelated_file_also_rechecks_after_arbitrary_body_effects(self):
        p = planner('build:\n  :sys never\nout:\n  :pass\n')
        p.state = current_state(p.graph)
        result = p.plan(['build', 'out'])
        self.assertEqual(result.entries[-1].status, 'recheck')
        self.assertEqual(result.bodies[-1].after, (0,))

    def test_virtual_prerequisite_forces_file_body_on_success(self):
        result = planner('out: build\n  :pass\nbuild:\n  :pass\n').plan('out')
        self.assertEqual(result.entries[-1].reason, 'virtual_prerequisite')
        self.assertEqual(tuple(b.mode for b in result.bodies), ('update', 'update'))

    def test_multitarget_update_covers_sibling_even_if_sibling_missing(self):
        p = planner('a b: input\n  @invalid +\n')
        p.state = current_state(p.graph)
        p.state.buildchecks[(0, '/recipe/a')] = 'changed'
        del p.state.files['/recipe/b']
        result = p.plan(['a', 'b'])
        self.assertEqual(tuple(e.status for e in result.entries), ('current', 'update', 'covered'))
        self.assertEqual(len(result.bodies), 1)
        self.assertIs(result.entries[-1].covered_by, result.bodies[0])
        self.assertEqual(tuple(n.name for n in result.bodies[0].outputs), ('a', 'b'))

    def test_current_multitarget_does_not_check_sibling_existence_or_cover_it(self):
        p = planner('a b:\n  :pass\n')
        p.state = current_state(p.graph)
        del p.state.files['/recipe/b']
        first = p.plan('a')
        self.assertEqual(first.bodies, [])
        both = p.plan(['a', 'b'])
        self.assertEqual(tuple(e.status for e in both.entries), ('current', 'update'))
        self.assertEqual(both.bodies[0].target.name, 'b')

    def test_conditional_shared_body_requires_result_before_sibling_traversal(self):
        p = planner('build:\n  :pass\na b:\n  :pass\nb: later\n')
        result = p.plan(['build', 'a', 'b'])
        self.assertEqual(identities(result), ('build', '/recipe/a', '/recipe/b'))
        self.assertEqual(result.entries[-1].reason, 'shared_body_result')
        self.assertEqual(result.entries[-1].bodies, ())
        self.assertIs(result.entries[-1].covered_by, result.bodies[-1])
        self.assertEqual(tuple(b.mode for b in result.bodies), ('update', 'conditional'))

    def test_shared_standard_virtual_outputs_are_not_sibling_memoized(self):
        result = planner('build test:\n  :pass\n').plan(['build', 'test'])
        self.assertEqual(tuple(b.target.name for b in result.bodies), ('build', 'test'))
        self.assertEqual(result.bodies[0].signature_targets, ())

    def test_standard_multiple_bodies_follow_all_prerequisite_lists(self):
        p = planner('build: a\n  :sys first\nbuild: b\n  :sys second\n')
        p.state = current_state(p.graph)
        result = p.plan('build')
        self.assertEqual(identities(result), ('/recipe/a', '/recipe/b', 'build'))
        self.assertEqual(tuple(b.definition.index for b in result.bodies), (0, 1))
        self.assertEqual(tuple(b.mode for b in result.bodies), ('update', 'update'))

    def test_current_sections_still_require_execution_without_update(self):
        p = planner('out:\n  # comment\n  >nobuild\n    :sys never\n')
        p.state = current_state(p.graph)
        result = p.plan('out')
        self.assertEqual(result.entries[0].status, 'current')
        self.assertEqual(result.bodies[0].mode, 'sections')

    def test_finally_is_appended_normally_and_not_after_fatal_error(self):
        p = planner('all:\n  :pass\nfinally:\n  :pass\n')
        self.assertEqual(identities(p.plan()), ('all', 'finally'))
        self.assertEqual(identities(p.plan('missing')), ('/recipe/missing',))

    def test_rules_autodependencies_and_attributes_fail_closed(self):
        p = planner('out: input\n  :pass\n')
        p.state = current_state(p.graph)
        p.state.rules['/recipe/input'] = True
        self.assertEqual(p.plan('out').diagnostic.code, 'rule_required')
        p.state.rules.clear()
        p.state.implicit['/recipe/input'] = None
        self.assertEqual(p.plan('out').diagnostic.code, 'automatic_dependencies')
        p.graph.find_node('out').attributes['remember'] = 1
        self.assertEqual(p.plan('out').diagnostic.code, 'unsupported_attributes')

    def test_source_search_path_is_an_explicit_expansion_boundary(self):
        p = planner('SRCPATH = elsewhere\nout: input\n  :pass\n')
        result = p.plan('out')
        self.assertEqual(result.diagnostic.code, 'source_search_path')
        self.assertEqual(result.bodies, [])

    def test_backend_error_is_source_aware_and_blocks(self):
        class Broken(MemoryTargetState):
            def file_state(self, node):
                raise OSError('unavailable state')
        result = planner('out:\n  :pass\n', Broken()).plan('out')
        self.assertEqual(result.diagnostic.code, 'observation_unavailable')
        self.assertIn('/recipe/main.aap:1:1:', str(result.diagnostic))
        self.assertFalse(result.complete)

    def test_prepared_buildcheck_digest_preserves_historical_whitespace(self):
        self.assertEqual(buildcheck_digest('hello', 'ascii'), '5d41402abc4b2a76b9719d911017c592')
        self.assertEqual(buildcheck_digest('  a   b \n', 'ascii'),
                         buildcheck_digest('  a b\n', 'ascii'))
        self.assertNotEqual(buildcheck_digest(' a', 'ascii'), buildcheck_digest('a', 'ascii'))
        with self.assertRaises(UnicodeEncodeError):
            buildcheck_digest('\xe4', 'ascii')

    def test_no_body_parsing_or_persistent_signature_writes(self):
        p = planner('out:\n  :include /never\n  :syseval never\n  @broken +\n')
        before = dict(p.scope.local)
        result = p.plan('out')
        self.assertEqual(len(result.bodies), 1)
        self.assertEqual(p.scope.local, before)
        self.assertEqual(p.state.stored, {})
        self.assertFalse(p.graph.definitions[0].body.origin.scanned)

    def test_globals_planning_uses_only_fake_process_and_target_state(self):
        class Captures(ProcessBackend):
            def __init__(self):
                self.requests = []
                self.output = [b'SUSE15\n', b'suse\n', b'SUSE 15 6\n']
            def run(self, request):
                self.requests.append(request)
                return ProcessResult(0, self.output[len(self.requests) - 1])
        scope = Scope.top_level()
        scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build'})
        backend = Captures()
        ast = lower(parse(Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1')))
        evaluated = Evaluator(scope, process_backend=backend,
                              process_policy=ProcessPolicy('latin-1')).run(ast)
        self.assertTrue(evaluated.complete)
        self.assertEqual(len(evaluated.graph.definitions), 32)
        p = UpdatePlanner(evaluated.graph, MemoryTargetState(), scope)
        self.assertEqual(p.plan().requests, ())  # globals alone has no default.
        for name, reason in (('prep-rpm', 'virtual_target'), ('rpminst', 'missing_target'),
                             ('IndexEntry', 'missing_target')):
            result = p.plan(name)
            self.assertTrue(result.complete)
            self.assertEqual(tuple(e.target.name for e in result.entries), (name,))
            self.assertEqual(result.entries[0].reason, reason)
            self.assertEqual(len(result.bodies), 1)
        result = p.plan('rpmcp_x86')
        self.assertTrue(result.complete)
        self.assertEqual(tuple(e.target.name for e in result.entries), ('fetch', 'rpmcp_x86'))
        self.assertEqual(result.entries[0].reason, 'empty_fetch')
        self.assertEqual(len(result.bodies), 1)
        for name, child, code in (('rpm', 'test', 'no_build_commands'),
                                  ('do-package', 'fake-install', 'no_build_commands')):
            result = p.plan(name)
            self.assertFalse(result.complete)
            self.assertEqual(tuple(e.target.name for e in result.entries), (child, name))
            self.assertEqual(result.diagnostic.code, code)
            self.assertEqual(result.bodies, [])
        self.assertEqual(len(backend.requests), 3)
        self.assertTrue(all(not d.body.origin.scanned for d in evaluated.graph.definitions if d.body))


if __name__ == '__main__':
    unittest.main()
