"""Port.port_extract -> Action.action_run, with no host extraction effects.

Action routing scenarios derive from Action.py and rectests/test012.py; the
legacy compiler driver is not run. Production registrations are the five
extract forms in command-forms/command-forms.tsv.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
                           MemoryPersistence, PortRuntime, MemoryMarkers, ProcessBackend, ProcessPolicy, ProcessResult)
from aap_semantics.actions import (ActionRuntime, ActionBackend, ActionResult,
                                   ActionWorkspace, MemoryActionWorkspace)


class RecordedAction(ActionBackend):
    """Deliberate effect oracle; never interprets an archive or launches a tool."""
    def __init__(self, workspace, fail=None):
        self.workspace, self.fail = workspace, fail
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if request.filename not in self.workspace.files:
            raise OSError('archive missing: ' + request.filename)
        if request.filename == self.fail:
            raise OSError('controlled action failure')
        self.workspace.files[request.cwd + '/observed-output'] = b'controlled effect'
        result = ActionResult(request)
        result.status, result.reason = 'COMPLETED', 'recorded_action_completion'
        return result


class ExtractTests(unittest.TestCase):
    def make(self, action='LOCAL = $OUTER\n@_caller.RETURNED = source\n',
             variables=None, workspace=None, backend=None, declarations=None,
             body=None, include_loader=None, process_backend=None):
        definition = Scope.top_level()
        definition.local.update({'ORIGIN': 'definition', 'OUTER': 'definition'})
        text = declarations or ':action extract targz\n' + ''.join(
            '  ' + line + '\n' for line in action.splitlines())
        data = Evaluator(definition).run(lower(parse(Source('/definitions/actions.aap', text))))
        scope = Scope.top_level()
        scope.local.update({'DISTFILES': 'one.tgz', 'DISTDIR': 'dist', 'WRKDIR': 'work',
                            'OUTER': 'invoker'})
        scope.local.update(variables or {})
        if workspace is None:
            workspace = MemoryActionWorkspace({'/recipe/dist/one.tgz': b'archive'})
        runtime = PortRuntime(markers=MemoryMarkers(), actions=ActionRuntime(workspace, backend))
        body = body if body is not None else (
            'BEFORE = yes\n@port_extract(globals())\nAFTER = yes\n'
            ':mkdir {force} done\n:touch {force} done/extract\n')
        recipe = 'all:\n' + ''.join('  ' + line + '\n' for line in body.splitlines())
        data = Evaluator(scope, declarations=data.declarations, graph=data.graph).run(
            lower(parse(Source('/recipe/main.aap', recipe))))
        saved = MemoryPersistence()
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, scope, data.declarations,
                             port_runtime=runtime, port_defaults=False, include_loader=include_loader,
                             process_backend=process_backend,
                             process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'))
        return driver, runtime, workspace, saved, definition

    def operation(self, result):
        return result.bodies[0].port_operations[0]

    def test_normal_action_entry_scope_and_cwd(self):
        driver, runtime, workspace, saved, definition = self.make()
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        operation = self.operation(result)
        action = operation.actions[0]
        request = action.request
        self.assertEqual(request.cwd, '/recipe/work')
        self.assertEqual(request.filename, '/recipe/dist/one.tgz')
        self.assertEqual(request.scope.local['LOCAL'], 'invoker')
        self.assertEqual(request.scope.local['fname'], '/recipe/dist/one.tgz')
        self.assertEqual(request.scope.local['filetype'], 'targz')
        self.assertEqual(request.scope.local['action'], 'extract')
        self.assertEqual(request.scope.local['_dirstack'], [])
        self.assertEqual(request.scope.local['recipe_name'], '/definitions/actions.aap')
        self.assertEqual(result.bodies[0].scope.local['RETURNED'], '/recipe/dist/one.tgz')
        self.assertNotIn('LOCAL', result.bodies[0].scope.local)
        self.assertNotIn('RETURNED', driver.scope.local)
        self.assertIs(request.graph, driver.graph)
        self.assertIs(request.declarations, driver.declarations)
        self.assertIs(request.caller.update_driver, driver)
        self.assertEqual(driver.cwd, '/recipe')
        self.assertEqual(operation.cwd, '/recipe')
        self.assertEqual(request.caller.cwd, '/recipe')
        self.assertEqual(action.program.statements[0].span.source_id, '/definitions/actions.aap')
        self.assertIn('/recipe/done/extract', runtime.markers.files)
        self.assertEqual(saved.writes, [])

    def test_definition_namespace_and_shared_user_namespace(self):
        driver, runtime, workspace, saved, definition = self.make(
            action='SEEN = $_recipe.ORIGIN\n@_recipe.CHANGED = "yes"\n@shared.value = "action"\n')
        namespace, name = driver.scope.target('shared.value', driver.graph.definitions[0], create=True)
        namespace.set(name, 'before', driver.graph.definitions[0])
        result = driver.build('all')
        action = self.operation(result).actions[0]
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(action.request.scope.local['SEEN'], 'definition')
        self.assertEqual(definition.local['CHANGED'], 'yes')
        self.assertEqual(namespace.get(name), 'action')

    def test_extract_only_overrides_distfiles_and_extractfiles(self):
        driver, runtime, workspace, saved, definition = self.make(variables={
            'EXTRACT_ONLY': 'chosen.tgz', 'DISTFILES': 'ignored.tgz', 'EXTRACTFILES': 'ignored2.tgz'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(self.operation(result).paths, ['/recipe/dist/chosen.tgz'])
        # A harmless registered action need not inspect its input. The helper
        # does not invent a mandatory archive/output existence postcondition.

    def test_empty_extract_only_falls_back_and_keeps_order(self):
        driver, runtime, workspace, saved, definition = self.make(variables={
            'EXTRACT_ONLY': '', 'DISTFILES': 'remote/b.tgz a.tgz'})
        result = driver.build('all')
        self.assertEqual(self.operation(result).paths, ['/recipe/dist/b.tgz', '/recipe/dist/a.tgz'])
        self.assertEqual(len(self.operation(result).actions), 2)

    def test_empty_distfiles_is_noop_without_work_capability(self):
        driver, runtime, workspace, saved, definition = self.make(
            variables={'DISTFILES': ''}, workspace=ActionWorkspace())
        self.assertEqual(driver.build('all').status, 'COMPLETE')

    def test_absolute_directories_and_item_overrides(self):
        driver, runtime, workspace, saved, definition = self.make(variables={
            'DISTDIR': '/archives', 'WRKDIR': '/work',
            'DISTFILES': 'remote/a.tgz b.tgz{distdir=other}{extractdir=sub} '
                         'c.tgz{distdir=/third}{extractdir=/separate}'})
        result = driver.build('all')
        requests = [a.request for a in self.operation(result).actions]
        self.assertEqual([r.filename for r in requests],
                         ['/archives/a.tgz', '/recipe/other/b.tgz', '/third/c.tgz'])
        self.assertEqual([r.cwd for r in requests], ['/work', '/work/sub', '/separate'])
        self.assertEqual(requests[1].scope.local['source'],
                         '/recipe/other/b.tgz{distdir=other}{extractdir=sub}')
        self.assertEqual(requests[1].attributes, {'distdir': 'other', 'extractdir': 'sub'})

    def test_quoted_filename_and_abspath_normalization(self):
        driver, runtime, workspace, saved, definition = self.make(variables={
            'DISTDIR': 'dist/../archives', 'WRKDIR': 'tmp/../work', 'DISTFILES': '"a b.tgz"'})
        request = self.operation(driver.build('all')).actions[0].request
        self.assertEqual(request.filename, '/recipe/archives/a b.tgz')
        self.assertEqual(request.scope.local['source'], '"/recipe/archives/a b.tgz"')
        self.assertEqual(request.cwd, '/recipe/work')

    def test_workspace_unavailable_blocks_without_marker(self):
        driver, runtime, workspace, saved, definition = self.make(workspace=ActionWorkspace())
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(saved.writes, [])
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertNotIn('/recipe/all', driver.completed)

    def test_action_capability_unavailable_is_structured_block(self):
        driver, runtime, workspace, saved, definition = self.make(backend=ActionBackend())
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(self.operation(result).actions[0].reason, 'action_capability_unavailable')
        self.assertIs(result.bodies[0].action_failure, self.operation(result).actions[0])
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(workspace.operations, [('directory', '/recipe/work')])

    def test_real_body_blocks_at_sys_with_definition_and_invocation_locations(self):
        driver, runtime, workspace, saved, definition = self.make(action=':sys extractor $source\nAFTER = no\n')
        result = driver.build('all')
        action = self.operation(result).actions[0]
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(action.blocked_at.name, 'sys')
        self.assertEqual(result.span.source_id, '/definitions/actions.aap')
        self.assertEqual(result.span.start.line, 2)
        self.assertEqual(action.request.span.source_id, '/recipe/main.aap')
        self.assertEqual(action.request.span.start.line, 3)
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(action.request.caller.cwd, '/recipe')

    def test_missing_archive_is_action_failure_not_inferred_success(self):
        workspace = MemoryActionWorkspace()
        backend = RecordedAction(workspace)
        driver, runtime, workspace, saved, definition = self.make(workspace=workspace, backend=backend)
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(self.operation(result).actions[0].reason, 'action_backend_failed')
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(saved.writes, [])

    def test_partial_effects_survive_later_failure_but_not_success_marker(self):
        workspace = MemoryActionWorkspace({'/recipe/dist/one.tgz': b'1', '/recipe/dist/two.tgz': b'2'})
        backend = RecordedAction(workspace, fail='/recipe/dist/two.tgz')
        driver, runtime, workspace, saved, definition = self.make(workspace=workspace, backend=backend,
            variables={'DISTFILES': 'one.tgz{extractdir=one} two.tgz{extractdir=two}'})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual([a.status for a in self.operation(result).actions], ['COMPLETED', 'FAILED'])
        self.assertEqual(workspace.files['/recipe/work/one/observed-output'], b'controlled effect')
        self.assertNotIn('/recipe/work/two/observed-output', workspace.files)
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(runtime.actions.active, [])
        self.assertEqual(driver.cwd, '/recipe')

    def test_semantic_action_error_fails(self):
        driver, runtime, workspace, saved, definition = self.make(action='X = $undefined\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.span.source_id, '/definitions/actions.aap')
        self.assertEqual(runtime.markers.files, {})

    def test_work_directory_error_fails_before_action(self):
        workspace = MemoryActionWorkspace({'/recipe/work': b'not a directory'})
        driver, runtime, workspace, saved, definition = self.make(workspace=workspace)
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(self.operation(result).actions, [])
        driver, runtime, workspace, saved, definition = self.make(workspace=workspace,
            variables={'DISTFILES': 'one.tgz{extractdir=child}'})
        self.assertEqual(driver.build('all').status, 'FAILED')

    def test_cvs_default_is_gated_but_extract_only_bypasses_mode_selection(self):
        driver, runtime, workspace, saved, definition = self.make(variables={'CVSMODULES': 'module'})
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(workspace.operations, [])
        driver, runtime, workspace, saved, definition = self.make(
            variables={'CVSMODULES': 'module', 'EXTRACT_ONLY': 'one.tgz'})
        self.assertEqual(driver.build('all').status, 'COMPLETE')

    def test_unsupported_item_modifier_blocks(self):
        driver, runtime, workspace, saved, definition = self.make(
            variables={'DISTFILES': 'one.tgz{var_EXTRA=unsafe}'})
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(workspace.operations, [])

    def test_all_five_production_action_types_and_custom_suffixes(self):
        declarations = ':filetype\n  suffix tar.Z tarZ\n  suffix xz xz\n'
        for name in ('targz', 'zip', 'tarbz2', 'tarZ', 'xz'):
            declarations += ':action extract ' + name + '\n  :pass\n'
        driver, runtime, workspace, saved, definition = self.make(declarations=declarations,
            variables={'DISTFILES': 'a.tar.gz b.zip c.tar.bz2 d.tar.Z e.xz'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([a.request.definition.input_type for a in self.operation(result).actions],
                         ['targz', 'zip', 'tarbz2', 'tarZ', 'xz'])

    def test_latest_definition_and_type_root_fallback(self):
        declarations = (':action extract targz\n  :sys unused\n'
                        ':action extract targz\n  :pass\n')
        driver, runtime, workspace, saved, definition = self.make(declarations=declarations,
            variables={'DISTFILES': 'anything{filetype=targz_custom}'})
        result = driver.build('all')
        action = self.operation(result).actions[0]
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(action.request.filetype, 'targz_custom')
        self.assertEqual(action.request.definition.span.start.line, 3)

    def test_default_fallback_requires_known_detection_result(self):
        declarations = ':action extract default\n  :pass\n'
        driver, runtime, workspace, saved, definition = self.make(declarations=declarations,
            variables={'DISTFILES': 'unknown'})
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        workspace = MemoryActionWorkspace(filetypes={'/recipe/dist/unknown': None})
        driver, runtime, workspace, saved, definition = self.make(declarations=declarations,
            workspace=workspace, variables={'DISTFILES': 'unknown'})
        self.assertEqual(driver.build('all').status, 'COMPLETE')

    def test_removed_suffix_and_hidden_suffix_are_not_guessed(self):
        for filename, declarations in (
                ('a.tgz', ':filetype\n  suffix tgz remove\n:action extract targz\n  :pass\n'),
                ('.hidden.tgz', ':action extract targz\n  :pass\n')):
            driver, runtime, workspace, saved, definition = self.make(declarations=declarations,
                variables={'DISTFILES': filename})
            self.assertEqual(driver.build('all').status, 'BLOCKED')

    def test_action_updates_share_driver_and_graph(self):
        class Loader(object):
            def load(self, path):
                if path != '/recipe/work/extra.aap': raise AssertionError(path)
                return Source(path, 'new {virtual}:\n  _top.SEEN = nested\n')
        driver, runtime, workspace, saved, definition = self.make(include_loader=Loader(), action=(
            ':include extra.aap\n:update new\n@_caller.RETURNED = "yes"\n'))
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertIsNotNone(driver.graph.find_node('new', '/recipe/work'))
        self.assertEqual(driver.scope.local['SEEN'], 'nested')
        self.assertIn('/recipe/work/new', driver.completed)
        self.assertEqual(result.bodies[0].scope.local['RETURNED'], 'yes')
        self.assertEqual(saved.writes, [])

    def test_nested_update_cause_and_helper_sites_survive(self):
        class Loader(object):
            def load(self, path):
                if path != '/recipe/work/extra.aap': raise AssertionError(path)
                return Source(path, 'new {virtual}:\n  :sys unavailable\n')
        driver, runtime, workspace, saved, definition = self.make(include_loader=Loader(), action=(
            ':include extra.aap\n:update new\nAFTER = no\n'))
        result = driver.build('all')
        action = self.operation(result).actions[0]
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(action.nested_result.target, 'new')
        self.assertEqual(result.blocked_at.name, 'sys')
        self.assertEqual(result.span.source_id, '/recipe/work/extra.aap')
        self.assertEqual(action.request.span.source_id, '/recipe/main.aap')
        self.assertEqual(runtime.markers.files, {})

    def test_success_effects_immediate_signatures_pending_and_repeat_memoized(self):
        workspace = MemoryActionWorkspace({'/recipe/dist/one.tgz': b'archive'})
        backend = RecordedAction(workspace)
        driver, runtime, workspace, saved, definition = self.make(workspace=workspace, backend=backend)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertIn('/recipe/done/extract', runtime.markers.files)
        self.assertEqual(saved.writes, [])
        self.assertEqual(driver.build('all').bodies, [])
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(driver.finish().status, 'COMPLETE')

    def test_nested_failure_propagates_without_later_marker(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'new {virtual}:\n  FAIL = $undefined\n')
        driver, runtime, workspace, saved, definition = self.make(include_loader=Loader(),
            action=':include extra.aap\n:update new\nAFTER = no\n')
        result = driver.build('all')
        action = self.operation(result).actions[0]
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(action.nested_result.status, 'FAILED')
        self.assertEqual(result.span.source_id, '/recipe/work/extra.aap')
        self.assertNotIn('AFTER', action.request.scope.local)
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(saved.writes, [])

    def test_malformed_unselected_action_stays_deferred(self):
        driver, runtime, workspace, saved, definition = self.make(declarations=(
            ':action extract zip\n  @invalid = (\n'
            ':action extract targz\n  :pass\n'))
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        driver, runtime, workspace, saved, definition = self.make(action='@invalid = (\n')
        self.assertEqual(driver.build('all').status, 'FAILED')
        self.assertEqual(runtime.markers.files, {})

    def test_filetype_key_presence_and_detection_edge(self):
        driver, runtime, workspace, saved, definition = self.make(declarations=(
            ':action extract default\n  :pass\n'),
            variables={'DISTFILES': 'one.tgz{filetype=}'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(self.operation(result).actions[0].request.filetype, 'default')
        driver, runtime, workspace, saved, definition = self.make(variables={'DISTFILES': 'one..tgz'})
        self.assertEqual(driver.build('all').status, 'COMPLETE')

    def test_unavailable_output_type_blocks_only_if_read(self):
        driver, runtime, workspace, saved, definition = self.make(action='TYPE = $targettype\n')
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        workspace = MemoryActionWorkspace(filetypes={'/recipe/work/all': None})
        driver, runtime, workspace, saved, definition = self.make(workspace=workspace,
            action='@_caller.RETURNED = targettype\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertIsNone(result.bodies[0].scope.local['RETURNED'])

    def test_recursive_action_entry_is_explicitly_gated(self):
        driver, runtime, workspace, saved, definition = self.make(action='@port_extract(globals())\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(runtime.actions.active, [])
        self.assertEqual(runtime.markers.files, {})


    def test_later_action_process_failure_does_not_create_extract_marker(self):
        class Process(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                self.requests.append(request)
                return ProcessResult(0 if len(self.requests) == 1 else 256)
        process = Process()
        driver, runtime, workspace, saved, definition = self.make(process_backend=process,
            action=':sys first $_recipe.ORIGIN $OUTER\nLOCAL = established\n:sys later\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        action = self.operation(result).actions[0]
        self.assertEqual([p.status for p in action.evaluation.processes], ['COMPLETED', 'FAILED'])
        self.assertEqual(action.request.scope.local['LOCAL'], 'established')
        self.assertEqual(process.requests[0].stages, (('first', 'definition', 'invoker'),))
        self.assertEqual([p.cwd for p in process.requests], ['/recipe/work', '/recipe/work'])
        self.assertEqual(action.request.caller.cwd, '/recipe')
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(saved.writes, [])

    def test_cd_inside_extract_action_is_restored_by_port_frame(self):
        from aap_semantics.port_commands import MemoryPortDirectories
        class Process(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                self.requests.append(request)
                return ProcessResult(0)
        process = Process()
        driver, runtime, workspace, saved, definition = self.make(process_backend=process,
            action=':cd sub\n:sys observed\n',
            body='@port_extract(globals())\n:sys after\n')
        runtime.commands.directories = MemoryPortDirectories(['/recipe/work/sub'])
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        action = self.operation(result).actions[0]
        self.assertEqual([p.cwd for p in process.requests], ['/recipe/work/sub', '/recipe'])
        self.assertEqual(action.evaluation.final_cwd, '/recipe/work/sub')
        self.assertEqual(action.request.scope.local['_prevdir'], '/recipe/work')
        self.assertIsNone(result.bodies[0].scope.local['_prevdir'])
        self.assertEqual(action.request.cwd, '/recipe/work')
        self.assertEqual(driver.cwd, '/recipe')



if __name__ == '__main__':
    unittest.main()
