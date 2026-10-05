"""Commands.aap_chdir, Scope.get_build_recdict, DoBuild.exec_commands.

All directory observations are injected; no host directory mutation occurs.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
    MemoryPersistence, PortRuntime, MemoryMarkers, ProcessBackend, ProcessResult,
    ProcessPolicy)
from aap_semantics.port_commands import (PortDirectories, MemoryPortDirectories,
    PortCommandRuntime, PortCommandPolicy, PortCommandRequest)


class Process(ProcessBackend):
    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return ProcessResult(0, b'captured\n')


class Loader(object):
    def __init__(self, files):
        self.files, self.reads = files, []

    def load(self, path):
        self.reads.append(path)
        return Source(path, self.files[path])


class CdTests(unittest.TestCase):
    def make(self, body, directories=('/recipe/sub',), variables=None, extra='', loader=None):
        scope = Scope.top_level()
        scope.local.update(variables or {})
        data = Evaluator(scope).run(lower(parse(Source('/recipe/main.aap',
            'all:\n' + ''.join('  ' + line + '\n' for line in body.splitlines()) + extra))))
        dirs = MemoryPortDirectories(directories) if type(directories) in (tuple, list) else directories
        runtime = PortRuntime(markers=MemoryMarkers(), commands=PortCommandRuntime(
            dirs, PortCommandPolicy(False, False)))
        process, saved, state = Process(), MemoryPersistence(), MemoryTargetState()
        for definition in data.graph.definitions:
            for node in definition.targets:
                state.buildchecks[(definition.index, node.identity)] = 'prepared'
        driver = BuildDriver(data.graph, state, saved, scope, data.declarations,
            port_defaults=False, port_runtime=runtime, process_backend=process,
            process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'), include_loader=loader)
        return driver, dirs, process, saved

    def test_relative_and_scope_initialization(self):
        driver, dirs, process, saved = self.make(':cd sub\nAFTER = $_prevdir')
        self.assertIsNone(driver.scope.local['_prevdir'])
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        body = result.bodies[0]
        self.assertEqual(body.scope.local['AFTER'], '/recipe')
        self.assertIsNone(driver.scope.local['_prevdir'])
        self.assertEqual(body.evaluation.final_cwd, '/recipe/sub')
        self.assertEqual(body.context.cwd, '/recipe')
        self.assertEqual(dirs.observations, ['/recipe/sub'])
        self.assertEqual(process.requests, [])
        self.assertEqual(saved.writes, [])

    def test_absolute(self):
        driver, dirs, process, saved = self.make(':cd /other', ['/other'])
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(dirs.observations, ['/other'])

    def test_variable_forms_and_integer(self):
        for arg, value, expected in (('$WHERE', 'sub', '/recipe/sub'),
                                     ('${WHERE}', 'sub', '/recipe/sub'),
                                     ('$(WHERE)', 12, '/recipe/12'),
                                     ('$WHERE', 'sub child', '/recipe/sub/child')):
            driver, dirs, process, saved = self.make(':cd ' + arg, [expected], {'WHERE': value})
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(dirs.observations, [expected])

    def test_multiple_items_join_and_later_absolute_resets(self):
        for arg, expected in (('a b c', '/recipe/a/b/c'), ('a /b c', '/b/c'),
                               ('"a b" c', '/recipe/a b/c')):
            driver, dirs, process, saved = self.make(':cd ' + arg, [expected])
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(dirs.observations, [expected])

    def test_dot_dotdot_and_spelling_reach_backend(self):
        driver, dirs, process, saved = self.make(':cd sub//./../sub', ['/recipe/sub'])
        body = driver.build('all').bodies[0]
        self.assertEqual(dirs.observations, ['/recipe/sub//./../sub'])
        self.assertEqual(body.evaluation.final_cwd, '/recipe/sub')

    def test_observed_symlink_cwd_used_not_lexical_normalization(self):
        class Observed(PortDirectories):
            def enter(self, path):
                return '/physical'
        driver, dirs, process, saved = self.make(':cd link/..\n:sys echo hello', Observed())
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(process.requests[0].cwd, '/physical')

    def test_tilde_forms_gate_before_previous_state(self):
        for arg in ('~', '~/sub', '~user/sub'):
            driver, dirs, process, saved = self.make(':cd ' + arg)
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED')
            self.assertIsNone(result.bodies[0].scope.local['_prevdir'])
            self.assertEqual(dirs.observations, [])

    def test_two_cds_then_dash_toggles(self):
        driver, dirs, process, saved = self.make(':cd sub\n:cd child\n:cd -',
                                               ['/recipe/sub', '/recipe/sub/child'])
        body = driver.build('all').bodies[0]
        self.assertEqual([r.previous_after for r in body.directory_changes],
                         ['/recipe', '/recipe/sub', '/recipe/sub/child'])
        self.assertEqual(body.evaluation.final_cwd, '/recipe/sub')

    def test_dash_before_first_cd_errors_without_entry(self):
        driver, dirs, process, saved = self.make(':cd -')
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertIsNone(result.bodies[0].scope.local['_prevdir'])
        self.assertEqual(dirs.observations, [])

    def test_missing_after_success_changes_previous_but_not_cwd(self):
        driver, dirs, process, saved = self.make(':cd sub\n:cd missing\nAFTER = no')
        result = driver.build('all')
        body = result.bodies[0]
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(body.scope.local['_prevdir'], '/recipe/sub')
        self.assertEqual(body.evaluation.final_cwd, '/recipe/sub')
        self.assertEqual(body.context.cwd, '/recipe')
        self.assertNotIn('AFTER', body.scope.local)
        self.assertEqual(driver.completed, {})
        self.assertEqual(saved.writes, [])
        self.assertEqual(result.error.span.start.line, 3)

    def test_unavailable_updates_previous_and_blocks(self):
        driver, dirs, process, saved = self.make(':cd sub\nAFTER = no', None)
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].scope.local['_prevdir'], '/recipe')
        self.assertEqual(result.bodies[0].directory_changes[0].status, 'BLOCKED')
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertNotIn('AFTER', result.bodies[0].scope.local)

    def test_inaccessible_and_backend_failure(self):
        class Broken(PortDirectories):
            def enter(self, path):
                raise OSError('permission denied')
        driver, dirs, process, saved = self.make(':cd sub', Broken())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertIn('permission denied', str(result.error))
        self.assertEqual(result.error.span.source_id, '/recipe/main.aap')

    def test_invalid_backend_result_fails_without_changing_cwd(self):
        class Invalid(PortDirectories):
            def enter(self, path):
                return 'relative'
        driver, dirs, process, saved = self.make(':cd sub', Invalid())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.bodies[0].evaluation.final_cwd, '/recipe')

    def test_empty_and_empty_expansion_error_before_previous_write(self):
        for arg in ('', '$EMPTY', '""'):
            driver, dirs, process, saved = self.make(':cd ' + arg, variables={'EMPTY': ''})
            result = driver.build('all')
            self.assertEqual(result.status, 'FAILED')
            self.assertIsNone(result.bodies[0].scope.local['_prevdir'])
            self.assertEqual(dirs.observations, [])

    def test_nul_fails_after_previous_write(self):
        driver, dirs, process, saved = self.make(':cd $BAD', variables={'BAD': 'a\x00b'})
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.bodies[0].scope.local['_prevdir'], '/recipe')
        self.assertEqual(dirs.observations, [])

    def test_uncharacterized_value_blocks(self):
        driver, dirs, process, saved = self.make(':cd $BAD', variables={'BAD': ['sub']})
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(dirs.observations, [])

    def test_no_glob_expansion(self):
        driver, dirs, process, saved = self.make(':cd literal*', ['/recipe/literal*'])
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(dirs.observations, ['/recipe/literal*'])

    def test_sys_and_syseval_and_helpers_share_live_directory(self):
        driver, dirs, process, saved = self.make(':cd sub\n:sys echo hello\n'
            '@where = os.path.abspath(".")\n:syseval read | :assign output')
        host = os.getcwd()
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(os.getcwd(), host)
        self.assertEqual([r.cwd for r in process.requests], ['/recipe/sub', '/recipe/sub'])
        self.assertEqual(result.bodies[0].scope.local['where'], '/recipe/sub')
        self.assertEqual(result.bodies[0].scope.local['output'], 'captured')
        self.assertEqual(driver.cwd, '/recipe')

    def test_restore_at_all_terminal_outcomes(self):
        for statement, status in (('X = yes', 'COMPLETE'), (':unknown whatever', 'BLOCKED'),
                                   ('X = $MISSING', 'FAILED')):
            driver, dirs, process, saved = self.make(':cd sub\n' + statement)
            host = os.getcwd()
            result = driver.build('all')
            self.assertEqual(result.status, status)
            self.assertEqual(os.getcwd(), host)
            self.assertEqual(driver.cwd, '/recipe')
            self.assertEqual(result.bodies[0].context.cwd, '/recipe')
            self.assertEqual(result.bodies[0].evaluation.final_cwd, '/recipe/sub')

    def test_nested_update_definition_directory_and_restore(self):
        driver, dirs, process, saved = self.make(':cd sub\n:update B\n:sys echo after',
            ['/recipe/sub', '/recipe/other'], extra='B {virtual}:\n  :cd other\n  :sys echo nested\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        outer = result.bodies[0]
        nested = outer.updates[0].builds[0].bodies[0]
        self.assertEqual(outer.updates[0].request.cwd, '/recipe/sub')
        self.assertEqual(nested.directory_changes[0].before, '/recipe')
        self.assertIsNone(nested.directory_changes[0].previous_before)
        self.assertEqual([r.cwd for r in process.requests], ['/recipe/other', '/recipe/sub'])
        self.assertEqual(outer.scope.local['_prevdir'], '/recipe')
        self.assertEqual(nested.context.cwd, '/recipe')
        self.assertEqual(saved.writes, [])

    def test_nested_failure_retains_invocation_and_cause(self):
        driver, dirs, process, saved = self.make(':cd sub\n:update B\n:sys echo never',
            extra='B {virtual}:\n  :cd absent\n')
        result = driver.build('all')
        update = result.bodies[0].update_failure
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(update.request.span.start.line, 3)
        self.assertEqual(update.span.start.line, 6)
        self.assertEqual(update.request.cwd, '/recipe/sub')
        self.assertEqual(process.requests, [])

    def test_include_uses_current_directory_and_cd_persists_on_return(self):
        loader = Loader({'/recipe/sub/part.aap': ':cd inner\nX = $_prevdir\n'})
        driver, dirs, process, saved = self.make(':cd sub\n:include part.aap\n:sys echo after',
                                               ['/recipe/sub', '/recipe/sub/inner'], loader=loader)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(loader.reads, ['/recipe/sub/part.aap'])
        body = result.bodies[0]
        self.assertEqual(body.scope.local['X'], '/recipe/sub')
        self.assertEqual(body.directory_changes[1].span.source_id, '/recipe/sub/part.aap')
        self.assertEqual(process.requests[0].cwd, '/recipe/sub/inner')

    def test_port_helper_selects_stage_directory_and_restores_cd(self):
        driver, dirs, process, saved = self.make(':cd sub\n@port_build(globals())\n:sys echo after',
            ['/recipe/sub', '/stage/build'],
            {'BUILDCMD': 'build && check', 'BUILDDIR': '/stage/build', 'WRKDIR': 'work'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertIsInstance(process.requests[0], PortCommandRequest)
        self.assertEqual([r.cwd for r in process.requests], ['/stage/build', '/recipe/sub'])
        self.assertEqual(result.bodies[0].scope.local['_prevdir'], '/recipe')

    def test_no_directory_inferred_from_successful_sys(self):
        driver, dirs, process, saved = self.make(':sys mkdir -p sub\n:cd sub', [])
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(len(process.requests), 1)
        self.assertEqual(dirs.directories, set())

    def test_indented_arguments_are_components_not_executable_body(self):
        driver, dirs, process, saved = self.make(':cd sub\n  child', ['/recipe/sub/child'])
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].directory_changes[0].components, ('sub', 'child'))

    def test_relative_port_work_directory_uses_current_frame(self):
        driver, dirs, process, saved = self.make(':cd sub\n@port_build(globals())',
            ['/recipe/sub', '/recipe/sub/work/src'],
            {'BUILDCMD': 'build', 'WRKDIR': 'work', 'WRKSRC': 'src'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(process.requests[0].cwd, '/recipe/sub/work/src')
        self.assertEqual(result.bodies[0].evaluation.final_cwd, '/recipe/sub')

    def test_nonvirtual_nested_lookup_uses_cd_directory(self):
        driver, dirs, process, saved = self.make(':cd sub\n:update plain\n',
            extra='plain:\n  :pass\n')
        result = driver.build('all')
        # The parent's plain target is NOT found by name-only lookup; only
        # virtual names can fall back across directories (Work.find_node).
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(len(result.bodies[0].updates[0].builds[0].bodies), 0)

    def test_metadata_frame_restores_external_helper_and_records_final_cwd(self):
        from aap_semantics.helpers import HelperRegistry
        helper = HelperRegistry('/recipe')
        runtime = PortRuntime(commands=PortCommandRuntime(MemoryPortDirectories(['/recipe/sub'])))
        evaluator = Evaluator(helpers=helper, port_runtime=runtime)
        result = evaluator.run(lower(parse(Source('/recipe/main.aap',
            ':cd sub\n@where = os.path.abspath(".")\n'))))
        self.assertEqual(result.scope.local['where'], '/recipe/sub')
        self.assertEqual(result.final_cwd, '/recipe/sub')
        self.assertEqual(evaluator.cwd, '/recipe')
        self.assertEqual(helper.cwd, '/recipe')


if __name__ == '__main__':
    unittest.main()
