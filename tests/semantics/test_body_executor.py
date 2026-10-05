"""Body-entry characterization: DoBuild.exec_commands, Scope.get_build_recdict.

Scope scenarios are derived from rectest008; includes from aap_include/read_recipe.
No historical driver, package tool, body shell command or write capability runs.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, UpdatePlanner, MemoryTargetState,
    FileState, BuildExecutionContext, BodyExecutor, ProcessBackend, ProcessResult,
    ProcessPolicy)
from aap_semantics.scopes import Namespace


def evaluate(text, scope=None, name='/recipe/main.aap', **kwargs):
    return Evaluator(scope, **kwargs).run(lower(parse(Source(name, text))))


def context(text, target='all', caller=None, state=None, **kwargs):
    result = evaluate(text)
    state = state or MemoryTargetState()
    plan = UpdatePlanner(result.graph, state, result.scope).plan(target)
    if not plan.bodies:
        raise AssertionError(plan.snapshot())
    return BuildExecutionContext(plan.bodies[0], caller or result.scope,
                                 result.graph, result.declarations, **kwargs)


class ExecutorTests(unittest.TestCase):
    def test_context_has_selected_definition_scope_and_source(self):
        c = context('VALUE = recipe\nall:\n  LOCAL = $VALUE\n')
        self.assertIs(c.definition, c.graph.definitions[0])
        self.assertIs(c.body, c.definition.body)
        self.assertIs(c.definition_scope, c.invocation_scope)
        self.assertIsNot(c.scope, c.definition_scope)
        self.assertEqual(c.cwd, '/recipe')
        self.assertEqual(c.scope.local['buildtarget'], 'all')
        self.assertEqual(c.scope.local['target'], 'all{virtual=1}')
        self.assertEqual(c.scope.local['target_list'], ['all'])
        self.assertEqual(c.scope.local['source_list'], [])
        self.assertEqual(c.scope.local['recipe_lnum'], 2)
        self.assertFalse(c.entered)
        self.assertFalse(c.body.origin.scanned)

    def test_target_depend_source_values_filter_virtual_sources_and_quote(self):
        state = MemoryTargetState()
        state.files['/recipe/in file'] = FileState(True, 100, False)
        c = context('all: "in file" phony {virtual}\n  :pass\nphony:\n', state=state)
        values = c.scope.local
        self.assertEqual(values['depend'], '"in file" phony{virtual=1}')
        self.assertEqual(values['depend_list'], ['in file', 'phony'])
        self.assertEqual(values['source'], '"in file"')
        self.assertEqual(values['source_list'], ['in file'])
        self.assertEqual(values['fname'], '"in file"')
        self.assertIs(c.depend_items[1].node, c.graph.find_node('phony'))

    def test_assignment_is_local_and_definition_values_are_live(self):
        c = context('VALUE = before\nall:\n  SEEN = $VALUE\n  VALUE = local\n')
        c.definition_scope.local['VALUE'] = 'after'
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertEqual(r.scope.local['SEEN'], 'after')
        self.assertEqual(r.scope.local['VALUE'], 'local')
        self.assertEqual(c.definition_scope.local['VALUE'], 'after')
        self.assertNotIn('SEEN', c.definition_scope.local)

    def test_top_caller_locals_do_not_override_definition_tree(self):
        caller = Scope.top_level()
        caller.local['VALUE'] = 'caller'
        c = context('VALUE = definition\nall:\n  SEEN = $VALUE\n', caller=caller)
        r = BodyExecutor().execute(c)
        self.assertEqual(r.scope.local['SEEN'], 'definition')
        self.assertNotIn('_caller', r.scope.namespaces)
        self.assertIs(r.scope.namespaces['_top'], caller.namespaces['_top'])

    def test_nested_build_caller_precedes_definition_tree(self):
        parent = context('VALUE = outer\nall:\n  :pass\n').scope
        parent.local['VALUE'] = 'nested caller'
        c = context('VALUE = definition\nall:\n  SEEN = $VALUE\n'
                    '  _caller.VALUE = changed\n', caller=parent)
        r = BodyExecutor().execute(c)
        self.assertEqual(r.scope.local['SEEN'], 'nested caller')
        self.assertEqual(parent.local['VALUE'], 'changed')
        self.assertEqual(c.definition_scope.local['VALUE'], 'definition')

    def test_recipe_and_user_namespace_writes_are_shared(self):
        c = context('shared.VALUE = before\nall:\n  _recipe.SEEN = yes\n'
                    '  shared.VALUE = after\n  created.VALUE = visible\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertEqual(c.definition_scope.local['SEEN'], 'yes')
        self.assertEqual(c.definition_scope.namespaces['shared'].get('VALUE'), 'after')
        self.assertIs(c.definition_scope.namespaces['created'], r.scope.namespaces['created'])

    def test_up_writes_existing_name_and_rejects_new_name(self):
        c = context('VALUE = before\nall:\n  _up.VALUE = after\n  _up.NEW = no\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'FAILED')
        self.assertEqual(c.definition_scope.local['VALUE'], 'after')
        self.assertNotIn('NEW', r.scope.local)

    def test_explicit_parent_binding_is_shared_without_inventing_parent(self):
        c = context('all:\n  _parent.VALUE = changed\n')
        missing = BodyExecutor().execute(c)
        self.assertEqual(missing.status, 'BLOCKED')
        r = evaluate('all:\n  _parent.VALUE = changed\n')
        parent = Scope.top_level()
        r.scope.namespaces['_parent'] = Namespace(parent)
        step = UpdatePlanner(r.graph, MemoryTargetState(), r.scope).plan('all').bodies[0]
        c = BuildExecutionContext(step, r.scope, r.graph, r.declarations)
        self.assertEqual(BodyExecutor().execute(c).status, 'COMPLETED')
        self.assertEqual(parent.local['VALUE'], 'changed')

    def test_bare_python_remains_local_while_no_reads_definition(self):
        c = context('VALUE = inherited\nall:\n  @value = _no.VALUE + "!"\n'
                    '  @items = target_list + source_list\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertEqual(r.scope.local['value'], 'inherited!')
        self.assertEqual(r.scope.local['items'], ['all'])
        bad = context('VALUE = inherited\nall:\n  @value = VALUE\n')
        self.assertEqual(BodyExecutor().execute(bad).status, 'FAILED')

    def test_mutable_inherited_list_alias_is_retained(self):
        c = context('@items = [1]\nall:\n  @_no.items.extend([2])\n')
        self.assertEqual(BodyExecutor().execute(c).status, 'COMPLETED')
        self.assertEqual(c.definition_scope.local['items'], [1, 2])

    def test_dictlist_values_block_instead_of_leaking_host_nodes(self):
        for code in ('X = $source_dl', '@x = target_dl'):
            c = context('all:\n  ' + code + '\n')
            r = BodyExecutor().execute(c)
            self.assertEqual(r.status, 'BLOCKED')
            self.assertIn('dictlist', str(r.error))

    def test_lazy_parse_preserves_source_span_and_original_indentation(self):
        c = context('# first\nall:\n\tX = yes\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.program.span, c.body.span)
        self.assertIs(r.program.source, c.body.source)
        self.assertEqual(r.program.statements[0].span.start.line, 3)
        self.assertEqual(r.program.source.slice(r.program.span), '\tX = yes\n')

    def test_invalid_unentered_body_harmless_but_entry_fails_before_mutations(self):
        r = evaluate('all:\n  GOOD = yes\nbad {virtual}:\n  BEFORE = no\n  @invalid +\n')
        p = UpdatePlanner(r.graph, MemoryTargetState(), r.scope)
        good = BuildExecutionContext(p.plan('all').bodies[0], r.scope, r.graph, r.declarations)
        self.assertEqual(BodyExecutor().execute(good).status, 'COMPLETED')
        bad = BuildExecutionContext(p.plan('bad').bodies[0], r.scope, r.graph, r.declarations)
        result = BodyExecutor().execute(bad)
        self.assertEqual(result.status, 'FAILED')
        self.assertNotIn('BEFORE', bad.scope.local)
        self.assertEqual(result.span.start.line, 5)

    def test_unsupported_commands_stop_without_expanding_arguments(self):
        for command in ('sys', 'system', 'mkdir', 'checksum', 'tree', 'print'):
            nested = '    :sys never\n' if command == 'tree' else ''
            c = context('all:\n  BEFORE = yes\n  :' + command + ' $MISSING\n' + nested + '  AFTER = no\n')
            r = BodyExecutor().execute(c)
            self.assertEqual(r.status, 'BLOCKED')
            self.assertEqual(r.blocked_at.name, command)
            self.assertEqual(r.scope.local['BEFORE'], 'yes')
            self.assertNotIn('AFTER', r.scope.local)
            self.assertFalse(r.target_updated)
            self.assertFalse(r.persistence_performed)
            self.assertFalse(r.evaluation.complete)

    def test_runtime_error_preserves_prior_local_state(self):
        c = context('all:\n  BEFORE = yes\n  BAD = $UNKNOWN\n  AFTER = no\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'FAILED')
        self.assertEqual(r.scope.local['BEFORE'], 'yes')
        self.assertNotIn('AFTER', r.scope.local)
        self.assertIn('/recipe/main.aap:3:', str(r.error))

    def test_direct_dependency_in_body_is_historically_forbidden(self):
        c = context('all:\n  BEFORE = no\n  child: input\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'FAILED')
        self.assertIn('dependency is not allowed', str(r.error))
        self.assertEqual(len(c.graph.definitions), 1)
        self.assertNotIn('BEFORE', r.scope.local)

    def test_include_registers_into_shared_graph_in_order_without_scheduling(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'child:\n  @invalid +\nother: child\n')
        c = context('all:\n  :include added.aap\n  AFTER = yes\n', include_loader=Loader())
        old_plan = c.step.graph_snapshot
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertTrue(r.graph_changed)
        self.assertEqual(tuple(d.index for d in r.added_definitions), (1, 2))
        self.assertEqual(tuple(n.name for n in c.graph.targets), ('all', 'child', 'other'))
        self.assertIs(r.added_definitions[0].scope, c.scope)
        self.assertEqual(r.added_definitions[0].span.source_id, '/recipe/added.aap')
        self.assertFalse(r.added_definitions[0].body.origin.scanned)
        self.assertIs(r.evaluation.graph, c.graph)
        self.assertNotEqual(old_plan, c.graph.snapshot())
        p = UpdatePlanner(c.graph, MemoryTargetState(), c.scope).plan('child')
        self.assertEqual(len(p.bodies), 1)

    def test_body_source_is_not_an_active_recipe_read(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'child:\n  :pass\n')
        c = context('all:\n  :include main.aap\n', include_loader=Loader())
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertEqual(len(r.added_definitions), 1)
        self.assertFalse(r.evaluation.includes[0].skipped_active)

    def test_actions_and_filetypes_registered_in_body_share_declaration_state(self):
        c = context('all:\n  :action extract zip\n    :sys never\n'
                    '  :filetype\n    suffix zip zip\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertTrue(r.declarations_changed)
        action = c.declarations.latest_action('extract', 'zip')
        self.assertIs(action.scope, c.scope)
        self.assertFalse(action.body.origin.scanned)
        self.assertIn('zip', c.declarations.suffixes)

    def test_block_after_dynamic_registration_preserves_mutation(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'child:\n  :sys never\n')
        c = context('all:\n  :include child.aap\n  :mkdir never\n  AFTER = no\n', include_loader=Loader())
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'BLOCKED')
        self.assertEqual(len(r.added_definitions), 1)
        self.assertTrue(r.post_execution_recheck_required)

    def test_cwd_and_process_state_use_definition_context(self):
        class Fake(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                self.requests.append(request)
                return ProcessResult(0, b'captured\n')
        fake = Fake()
        c = context('all:\n  :syseval echo $source | :assign captured\n',
                    process_backend=fake, process_policy=ProcessPolicy('ascii'))
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertEqual(fake.requests[0].cwd, '/recipe')
        self.assertEqual(r.scope.local['captured'], 'captured')
        self.assertEqual(len(r.processes), 1)
        self.assertNotIn('captured', c.invocation_scope.local)

    def test_process_records_survive_later_semantic_failure(self):
        class Fake(ProcessBackend):
            def run(self, request): return ProcessResult(0, b'yes')
        c = context('all:\n  :syseval harmless | :assign captured\n  BAD = $UNKNOWN\n',
                    process_backend=Fake(), process_policy=ProcessPolicy('ascii'))
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'FAILED')
        self.assertEqual(len(r.processes), 1)
        self.assertEqual(r.scope.local['captured'], 'yes')

    def test_definition_directory_wins_over_first_target_and_caller_directory(self):
        r = evaluate('all:\n', name='/caller/main.aap')
        definition = evaluate('all:\n  :pass\n', name='/definition/main.aap', graph=r.graph)
        step = UpdatePlanner(r.graph, MemoryTargetState(), r.scope).plan('all').bodies[0]
        c = BuildExecutionContext(step, r.scope, r.graph, definition.declarations)
        self.assertEqual(c.target.cwd, '/caller')
        self.assertEqual(c.cwd, '/definition')
        self.assertIs(c.definition_scope, definition.scope)
        self.assertEqual(BodyExecutor().execute(c).status, 'COMPLETED')

    def test_source_paths_shorten_only_when_more_than_root_is_shared(self):
        state = MemoryTargetState()
        state.files['/recipe/sub/input'] = FileState(True, 100, False)
        state.files['/else/input'] = FileState(True, 100, False)
        c = context('all: /recipe/sub/input /else/input\n  :pass\n', state=state)
        self.assertEqual(c.scope.local['source_list'], ['sub/input', '/else/input'])

    def test_nonvirtual_entry_requires_prepared_buildcheck(self):
        c = context('out:\n  VALUE = yes\n', target='out')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.reason, 'buildcheck_unavailable')
        self.assertIsNone(r.program)
        self.assertFalse(c.entered)
        c.prepared_buildcheck = 'explicit fake observation'
        self.assertEqual(BodyExecutor().execute(c).status, 'COMPLETED')

    def test_guarded_or_stale_plan_does_not_enter_body(self):
        r = evaluate('all:\n  :pass\nother {virtual}:\n  :pass\n')
        p = UpdatePlanner(r.graph, MemoryTargetState(), r.scope).plan(['all', 'other'])
        c = BuildExecutionContext(p.bodies[1], r.scope, r.graph, r.declarations)
        self.assertEqual(BodyExecutor().execute(c).reason, 'plan_recheck_required')
        evaluate('new:\n', scope=r.scope, graph=r.graph)
        c = BuildExecutionContext(p.bodies[0], r.scope, r.graph, r.declarations)
        self.assertEqual(BodyExecutor().execute(c).reason, 'graph_changed_since_plan')

    def test_completion_is_not_target_update_or_persistence_and_cannot_resume(self):
        c = context('all:\n  :pass\n')
        r = BodyExecutor().execute(c)
        self.assertEqual(r.status, 'COMPLETED')
        self.assertTrue(r.post_execution_recheck_required)
        self.assertFalse(r.target_updated)
        self.assertFalse(r.persistence_performed)
        self.assertFalse(hasattr(c.target, 'status'))
        self.assertEqual(BodyExecutor().execute(c).reason, 'context_already_entered')

    def test_planner_is_still_inert_and_body_api_is_explicit(self):
        c = context('all:\n  _recipe.EFFECT = yes\n')
        self.assertNotIn('EFFECT', c.definition_scope.local)
        self.assertFalse(c.entered)
        BodyExecutor().execute(c)
        self.assertEqual(c.definition_scope.local['EFFECT'], 'yes')

    def test_nano_integration_stops_at_checksum_without_package_effects(self):
        class Fake(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                output = (b'SUSE15\n', b'suse\n', b'SUSE 15 6\n')[len(self.requests)]
                self.requests.append(request)
                return ProcessResult(0, output)
        root = '/authorized'
        nano = root + '/ports/editors/nano/main.aap'
        globals_source = Source(root + '/ports/globals.aap', Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1').text)
        class Loader(object):
            def load(self, path):
                if path != globals_source.source_id:
                    raise AssertionError('unauthorized recipe read: ' + path)
                return globals_source
        scope = Scope.top_level()
        scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build',
                            'PKGDIR': root + '/ports/editors/nano/pack', 'DISTDIR': 'distfiles'})
        fake = Fake()
        r = evaluate(Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/editors/nano/main.aap'), 'latin-1').text,
                     scope, nano, include_loader=Loader(), process_backend=fake,
                     process_policy=ProcessPolicy('latin-1'))
        self.assertTrue(r.complete)
        self.assertEqual((len(r.graph.definitions), len(r.graph.targets), len(r.graph.nodes)), (35, 35, 39))
        p = UpdatePlanner(r.graph, MemoryTargetState(), scope)
        self.assertEqual(p.plan().requests, ())
        self.assertEqual(p.plan('fake-install').diagnostic.code, 'no_build_commands')
        plan = p.plan('do-checksum')
        self.assertEqual(tuple(e.target.name for e in plan.entries), ('do-checksum',))
        c = BuildExecutionContext(plan.bodies[0], scope, r.graph, r.declarations,
                                 process_backend=fake, process_policy=ProcessPolicy('latin-1'),
                                 prepared_buildcheck='controlled prepared signature observation')
        executed = BodyExecutor().execute(c)
        self.assertEqual(executed.status, 'BLOCKED')
        self.assertEqual(executed.blocked_at.name, 'checksum')
        self.assertEqual(executed.span.source_id, nano)
        self.assertEqual(executed.span.start.line, 51)
        self.assertEqual(c.scope.local['target'], 'do-checksum')
        self.assertEqual(c.scope.local['buildtarget'], root + '/ports/editors/nano/do-checksum')
        self.assertEqual(c.scope.local['source'], '')
        self.assertEqual(c.scope.local['depend_list'], [])
        self.assertEqual(c.cwd, root + '/ports/editors/nano')
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(executed.processes, ())
        self.assertFalse(executed.graph_changed)
        self.assertFalse(executed.target_updated)
        # A real harmless package body can complete semantically as well.
        local = p.plan('local-rpm').bodies[0]
        no_op = BodyExecutor().execute(BuildExecutionContext(local, scope, r.graph, r.declarations))
        self.assertEqual(no_op.status, 'COMPLETED')
        self.assertFalse(no_op.target_updated)

    def test_globals_body_entry_does_not_change_registration_regression(self):
        class Fake(ProcessBackend):
            def __init__(self): self.index = 0
            def run(self, request):
                output = (b'SUSE15', b'suse', b'SUSE 15 6')[self.index]
                self.index += 1
                return ProcessResult(0, output)
        scope = Scope.top_level()
        scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build'})
        fake = Fake()
        r = evaluate(Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1').text,
                     scope, '/authorized/ports/globals.aap', process_backend=fake,
                     process_policy=ProcessPolicy('latin-1'))
        self.assertEqual((len(r.graph.definitions), len(r.graph.targets), len(r.graph.nodes)), (32, 32, 37))
        snapshot = r.graph.snapshot()
        p = UpdatePlanner(r.graph, MemoryTargetState(), scope).plan('rpminst')
        c = BuildExecutionContext(p.bodies[0], scope, r.graph, r.declarations,
                                 prepared_buildcheck='explicit controlled signature')
        body = BodyExecutor().execute(c)
        self.assertEqual(body.status, 'BLOCKED')
        self.assertEqual(body.blocked_at.name, 'sys')
        self.assertEqual(snapshot, r.graph.snapshot())
        self.assertEqual(fake.index, 3)


if __name__ == '__main__':
    unittest.main()
