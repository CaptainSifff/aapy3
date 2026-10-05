"""Source-derived Port.port_config/port_exe_cmd and unlogged Util.logged_system.

Only in-memory directory observations and recording ProcessBackend results.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
    MemoryPersistence, PortRuntime, MemoryMarkers, ProcessBackend, ProcessResult,
    ProcessPolicy, ProcessUnavailable, ProcessBackendError, PortCommandRuntime,
    PortCommandPolicy, PortCommandRequest, SystemRequest, MemoryPortDirectories,
    PortDirectories)
from aap_semantics.values import UnavailableValue


class RecordedProcess(ProcessBackend):
    def __init__(self, result=None):
        self.result = ProcessResult(0, b'\xff\n\n', b' stderr\n') if result is None else result
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class PortCommandTests(unittest.TestCase):
    def make(self, values=None, directories=None, policy=None, backend=None, text=None):
        scope = Scope.top_level()
        scope.local.update({'WRKDIR': 'work', 'WRKSRC': 'src',
                            'CONFIGURECMD': './configure --prefix=/prefix && make',
                            'sysresult': 77})
        scope.local.update(values or {})
        dirs = directories if directories is not None else MemoryPortDirectories(['/recipe/work/src'])
        commands = PortCommandRuntime(dirs, policy or PortCommandPolicy(False, False))
        runtime = PortRuntime(markers=MemoryMarkers(), commands=commands)
        source = Source('/recipe/main.aap', text or (
            'all:\n  @port_config(globals())\n  AFTER = yes\n'
            '  :mkdir {force} done\n  :touch {force} done/config\n'))
        data = Evaluator(scope).run(lower(parse(source)))
        process = backend if backend is not None else RecordedProcess()
        saved = MemoryPersistence()
        driver = BuildDriver(data.graph, MemoryTargetState(), saved, scope, data.declarations,
            port_runtime=runtime, port_defaults=False, process_backend=process,
            process_policy=ProcessPolicy('utf-8'))  # no :sys permission needed
        return driver, runtime, process, saved

    def test_success_request_scope_and_marker_contract(self):
        driver, runtime, process, saved = self.make()
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        body = result.bodies[0]
        operation = body.port_operations[0]
        record = operation.processes[0]
        request = process.requests[0]
        self.assertIsInstance(request, PortCommandRequest)
        self.assertEqual(request.operation, 'port_exe_cmd')
        self.assertEqual(request.command, './configure --prefix=/prefix && make')
        self.assertEqual(request.shell_command, request.command + '\n')
        self.assertEqual(request.command_bytes, request.command.encode('utf-8'))
        self.assertEqual(request.cwd, '/recipe/work/src')
        self.assertEqual(record.caller_cwd, '/recipe')
        self.assertEqual(body.context.cwd, '/recipe')
        self.assertIs(operation.scope, body.scope)
        self.assertIs(body.context.graph, driver.graph)
        self.assertEqual(request.span.source_id, '/recipe/main.aap')
        self.assertEqual(request.span.start.line, 2)
        self.assertTrue(request.shell_required)
        self.assertEqual(request.shell_mode, 'posix-sh')
        self.assertEqual((request.stdin_policy, request.stdout_policy, request.stderr_policy), ('inherit',) * 3)
        self.assertEqual(request.environment_policy, 'inherit-backend')
        self.assertIsNone(request.environment)
        self.assertFalse(request.capture_stdout)
        self.assertFalse(request.logging)
        self.assertTrue(request.echo)
        self.assertEqual(request.echo_text, request.command)
        self.assertFalse(request.skip_in_dry_run)
        self.assertEqual(record.result.stdout, b'\xff\n\n')
        self.assertEqual(record.result.stderr, b' stderr\n')
        self.assertIs(body.processes[0], record)
        self.assertEqual(record.status, 'COMPLETED')
        self.assertEqual(body.scope.local['AFTER'], 'yes')
        self.assertNotIn('sysresult', body.scope.local)
        self.assertEqual(driver.scope.local['sysresult'], 77)
        self.assertIn('/recipe/done/config', runtime.markers.files)
        self.assertEqual(saved.writes, [])
        self.assertFalse(body.graph_changed)
        self.assertFalse(body.declarations_changed)

    def test_empty_or_missing_command_does_not_read_paths_or_logging(self):
        for value in ('', None):
            driver, runtime, process, saved = self.make({'CONFIGURECMD': value,
                'WRKDIR': UnavailableValue('unused'), 'WRKSRC': UnavailableValue('unused')},
                directories=PortDirectories(), policy=PortCommandPolicy())
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(process.requests, [])
        driver, runtime, process, saved = self.make()
        del driver.scope.local['CONFIGURECMD']
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        messages = result.bodies[0].port_operations[0].messages
        self.assertEqual([(message.kind, message.text) for message in messages],
                         [('extra', 'No CONFIGURECMD specified')])
        self.assertEqual(runtime.commands.directories.observations, [])

    def test_directory_selection_relative_absolute_and_fallback(self):
        cases = (({'BUILDDIR': 'obj'}, '/recipe/work/obj'),
                 ({'BUILDDIR': '/obj'}, '/obj'),
                 ({'WRKDIR': '/work'}, '/work/src'),
                 ({'WRKSRC': '/src'}, '/src'),
                 ({'BUILDDIR': ''}, '/recipe/work/src'),
                 ({'WRKDIR': '', 'WRKSRC': ''}, '/recipe'))
        for values, expected in cases:
            driver, runtime, process, saved = self.make(values,
                directories=MemoryPortDirectories([expected]))
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(process.requests[0].cwd, expected)

    def test_directory_backend_supplies_observed_cwd_without_lexical_collapse(self):
        class Resolved(PortDirectories):
            def enter(self, path):
                if path != '/recipe/work/link/../src': raise AssertionError(path)
                return '/resolved/src'
        driver, runtime, process, saved = self.make({'BUILDDIR': 'link/../src'}, Resolved())
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(process.requests[0].cwd, '/resolved/src')

    def test_missing_directory_is_failed_not_created(self):
        driver, runtime, process, saved = self.make(directories=MemoryPortDirectories())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(process.requests, [])
        self.assertEqual(runtime.commands.directories.directories, set())
        self.assertEqual(runtime.markers.files, {})
        self.assertEqual(saved.writes, [])

    def test_unavailable_directory_blocks(self):
        driver, runtime, process, saved = self.make(directories=PortDirectories())
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(process.requests, [])
        self.assertEqual(runtime.markers.files, {})

    def test_missing_required_directory_values_fail(self):
        for key in ('WRKDIR', 'WRKSRC'):
            driver, runtime, process, saved = self.make()
            del driver.scope.local[key]
            self.assertEqual(driver.build('all').status, 'FAILED')
            self.assertEqual(process.requests, [])

    def test_invalid_directory_and_command_values(self):
        for key, value, expected in (('WRKDIR', 1, 'BLOCKED'), ('BUILDDIR', ['x'], 'BLOCKED'),
                ('WRKDIR', '\x00', 'FAILED'), ('CONFIGURECMD', UnavailableValue('deferred'), 'BLOCKED'),
                ('CONFIGURECMD', 'bad\x00', 'FAILED')):
            driver, runtime, process, saved = self.make({key: value})
            self.assertEqual(driver.build('all').status, expected)
            self.assertEqual(process.requests, [])
            self.assertEqual(runtime.markers.files, {})

    def test_invalid_directory_observation_fails(self):
        class Invalid(PortDirectories):
            def enter(self, path): return 'relative'
        driver, runtime, process, saved = self.make(directories=Invalid())
        self.assertEqual(driver.build('all').status, 'FAILED')
        self.assertEqual(process.requests, [])

    def test_unknown_active_log_or_global_capture_blocks(self):
        for policy in (PortCommandPolicy(), PortCommandPolicy(True, False),
                       PortCommandPolicy(False, True), PortCommandPolicy(False, None)):
            driver, runtime, process, saved = self.make(policy=policy)
            self.assertEqual(driver.build('all').status, 'BLOCKED')
            self.assertEqual(process.requests, [])
            self.assertEqual(runtime.markers.files, {})

    def test_nonzero_unavailable_error_invalid_process_results(self):
        for outcome, status in ((ProcessResult(256), 'FAILED'),
                (ProcessUnavailable('disabled'), 'BLOCKED'), (OSError('broken'), 'FAILED'),
                (ProcessBackendError('broken'), 'FAILED'), (False, 'FAILED'),
                (ProcessResult(True), 'FAILED'), (ProcessResult(-1), 'FAILED'),
                (ProcessResult(0, 'text'), 'FAILED'), (ProcessResult(0, b'', 'text'), 'FAILED')):
            driver, runtime, process, saved = self.make(backend=RecordedProcess(outcome))
            result = driver.build('all')
            self.assertEqual(result.status, status)
            self.assertEqual(result.span.start.line, 2)
            body = result.bodies[0]
            self.assertEqual(body.context.cwd, '/recipe')
            self.assertNotIn('AFTER', body.scope.local)
            self.assertNotIn('sysresult', body.scope.local)
            self.assertEqual(driver.scope.local['sysresult'], 77)
            self.assertEqual(runtime.markers.files, {})
            self.assertEqual(driver.completed, {})
            self.assertEqual(saved.writes, [])
            self.assertEqual(len(process.requests), 1)

    def test_default_process_backend_unavailable(self):
        driver, runtime, process, saved = self.make(backend=ProcessBackend())
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(runtime.markers.files, {})

    def test_aap_special_path_is_gated_not_launched(self):
        for command in ('aap', 'aap test'):
            driver, runtime, process, saved = self.make({'CONFIGURECMD': command})
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED')
            self.assertEqual(result.bodies[0].processes[0].selected_cwd, '/recipe/work/src')
            self.assertEqual(process.requests, [])
            self.assertEqual(runtime.markers.files, {})
        # Exact upstream prefix test; these are ordinary shell strings.
        for command in (' aap test', 'aap\ttest', '/bin/aap test', 'aap-other'):
            driver, runtime, process, saved = self.make({'CONFIGURECMD': command})
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(process.requests[0].command, command)

    def test_options_and_multiline_remain_gated(self):
        for command in ('{q} tool', '{force} tool', '{log} tool', 'tool\nother'):
            driver, runtime, process, saved = self.make({'CONFIGURECMD': command})
            self.assertEqual(driver.build('all').status, 'BLOCKED')
            self.assertEqual(process.requests, [])

    def test_command_is_not_aap_expanded_again_or_sys_parsed(self):
        command = 'echo "$SHELLVAR" && cat input > output'
        driver, runtime, process, saved = self.make({'CONFIGURECMD': command, 'SHELLVAR': 'recipe', 'async': 1})
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(process.requests[0].command, command)
        self.assertFalse(hasattr(process.requests[0], 'expression'))
        self.assertEqual(runtime.commands.directories.directories, {'/recipe/work/src'})

    def test_identical_command_has_distinct_sys_origin_and_cwd(self):
        command = 'echo first && echo second'
        driver, runtime, process, saved = self.make({'CONFIGURECMD': command})
        port_result = driver.build('all')
        evaluator = Evaluator(Scope.top_level(), cwd='/recipe', process_backend=process,
                              process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'))
        evaluator.run(lower(parse(Source('/recipe/sys.aap', ':sys ' + command + '\n'))))
        port, sysreq = process.requests
        self.assertIsInstance(port, PortCommandRequest)
        self.assertIsInstance(sysreq, SystemRequest)
        self.assertEqual(port.command, sysreq.command)
        self.assertFalse(hasattr(port, 'expression'))
        self.assertEqual(sysreq.expression.kind, 'AND')
        self.assertEqual((port.cwd, sysreq.cwd), ('/recipe/work/src', '/recipe'))
        self.assertEqual((port.span.start.line, sysreq.span.start.line), (2, 1))
        self.assertNotIn('sysresult', port_result.bodies[0].scope.local)
        self.assertEqual(evaluator.scope.local['sysresult'], 0)

    def test_helpers_do_not_batch(self):
        driver, runtime, process, saved = self.make(text=
            'all:\n  @port_config(globals())\n  @port_config(globals())\n')
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(len(process.requests), 2)
        self.assertEqual([r.span.start.line for r in process.requests], [2, 3])

    def test_build_local_values_and_namespace_writes_preserved(self):
        driver, runtime, process, saved = self.make(text='all:\n  CONFIGURECMD = echo local\n'
            '  BUILDDIR = /chosen\n  @port_config(globals())\n  _recipe.SEEN = yes\n',
            directories=MemoryPortDirectories(['/chosen']))
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(process.requests[0].command, 'echo local')
        self.assertEqual(process.requests[0].cwd, '/chosen')
        self.assertEqual(driver.scope.local['SEEN'], 'yes')
        self.assertEqual(driver.scope.local['CONFIGURECMD'], './configure --prefix=/prefix && make')

    def test_nested_failure_retains_update_and_helper_sites(self):
        driver, runtime, process, saved = self.make(backend=RecordedProcess(ProcessResult(256)),
            text='child {virtual}:\n  @port_config(globals())\nall:\n  :update child\n  AFTER = no\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.span.start.line, 2)
        nested = result.bodies[0].update_failure
        self.assertEqual(nested.request.span.start.line, 4)
        self.assertEqual(nested.target, 'child')
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(saved.writes, [])


class PortBuildTests(unittest.TestCase):
    def make(self, values=None, helper='port_build', marker='build', **options):
        variables = {'BUILDCMD': './configure --prefix=/prefix && make', 'TESTCMD': 'true'}
        variables.update(values or {})
        text = options.pop('text', 'all:\n  @' + helper + '(globals())\n  AFTER = yes\n'
                           '  :mkdir {force} done\n  :touch {force} done/' + marker + '\n')
        return PortCommandTests.make(self, values=variables, text=text, **options)

    def test_build_success_one_opaque_request_local_status_untouched(self):
        driver, runtime, process, saved = self.make()
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        body = result.bodies[0]
        self.assertEqual(len(process.requests), 1)
        request = process.requests[0]
        self.assertIsInstance(request, PortCommandRequest)
        self.assertEqual(request.command, './configure --prefix=/prefix && make')
        self.assertEqual(request.shell_command, request.command + '\n')
        self.assertEqual(request.cwd, '/recipe/work/src')
        self.assertEqual(request.shell_mode, 'posix-sh')
        self.assertTrue(request.shell_required)
        self.assertFalse(hasattr(request, 'expression'))
        self.assertNotIn('sysresult', body.scope.local)
        self.assertEqual(driver.scope.local['sysresult'], 77)
        self.assertEqual(body.context.cwd, '/recipe')
        self.assertIs(body.port_operations[0].scope, body.scope)
        self.assertEqual(body.scope.local['AFTER'], 'yes')
        self.assertEqual(body.port_operations[0].status, 'COMPLETED')
        self.assertIn('/recipe/done/build', runtime.markers.files)
        self.assertEqual(saved.writes, [])
        self.assertEqual(request.span.source_id, '/recipe/main.aap')
        self.assertEqual(request.span.start.line, 2)
        self.assertEqual(driver.build('all').bodies, [])
        self.assertEqual(len(process.requests), 1)

    def test_build_directory_selection(self):
        for values, expected in (({}, '/recipe/work/src'), ({'BUILDDIR': ''}, '/recipe/work/src'),
                ({'BUILDDIR': 'obj'}, '/recipe/work/obj'), ({'BUILDDIR': '/obj'}, '/obj'),
                ({'WRKDIR': '/work'}, '/work/src')):
            driver, runtime, process, saved = self.make(values,
                directories=MemoryPortDirectories([expected]))
            self.assertEqual(driver.build('all').status, 'COMPLETE')
            self.assertEqual(process.requests[0].cwd, expected)

    def test_missing_and_falsy_build_command_default_to_gated_aap(self):
        for value in ('', None, 0, False, [], ()):
            driver, runtime, process, saved = self.make({'BUILDCMD': value})
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED')
            record = result.bodies[0].processes[0]
            self.assertEqual(record.command, 'aap')
            self.assertEqual(record.selected_cwd, '/recipe/work/src')
            self.assertEqual(record.reason, 'unsupported_port_command')
            self.assertEqual(process.requests, [])
            self.assertEqual(runtime.markers.files, {})
            self.assertEqual(saved.writes, [])
        driver, runtime, process, saved = self.make()
        del driver.scope.local['BUILDCMD']
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].processes[0].command, 'aap')
        self.assertEqual(process.requests, [])

    def test_explicit_aap_and_deferred_or_invalid_values_remain_gated(self):
        for value in ('aap', 'aap test', UnavailableValue('delayed expansion'), ['make'], 1):
            driver, runtime, process, saved = self.make({'BUILDCMD': value})
            self.assertEqual(driver.build('all').status, 'BLOCKED')
            self.assertEqual(process.requests, [])
            self.assertNotIn('/recipe/done/build', runtime.markers.files)

    def test_unavailable_missing_and_inaccessible_directory(self):
        class Inaccessible(PortDirectories):
            def enter(self, path): raise OSError('access denied')
        for directories, status in ((PortDirectories(), 'BLOCKED'),
                (MemoryPortDirectories(), 'FAILED'), (Inaccessible(), 'FAILED')):
            driver, runtime, process, saved = self.make(directories=directories)
            result = driver.build('all')
            self.assertEqual(result.status, status)
            self.assertEqual(result.span.start.line, 2)
            self.assertIsNone(result.bodies[0].processes[0].request)
            self.assertEqual(result.bodies[0].context.cwd, '/recipe')
            self.assertEqual(process.requests, [])
            self.assertEqual(runtime.markers.files, {})
            self.assertEqual(driver.completed, {})
            self.assertEqual(saved.writes, [])

    def test_process_failure_unavailability_and_invalid_results_do_not_complete(self):
        for outcome, status in ((ProcessUnavailable('unavailable'), 'BLOCKED'),
                (ProcessResult(256), 'FAILED'), (ProcessBackendError('broken'), 'FAILED'),
                (OSError('broken'), 'FAILED'), (False, 'FAILED'), (ProcessResult(-1), 'FAILED'),
                (ProcessResult(0, 'text'), 'FAILED')):
            driver, runtime, process, saved = self.make(backend=RecordedProcess(outcome))
            result = driver.build('all')
            self.assertEqual(result.status, status)
            self.assertEqual(result.span.start.line, 2)
            self.assertEqual(result.bodies[0].context.cwd, '/recipe')
            self.assertNotIn('sysresult', result.bodies[0].scope.local)
            self.assertNotIn('AFTER', result.bodies[0].scope.local)
            self.assertEqual(driver.scope.local['sysresult'], 77)
            self.assertEqual(driver.completed, {})
            self.assertEqual(runtime.markers.files, {})
            self.assertEqual(saved.writes, [])

    def test_build_keeps_existing_logging_gates(self):
        for policy in (PortCommandPolicy(), PortCommandPolicy(True, False), PortCommandPolicy(False, True)):
            driver, runtime, process, saved = self.make(policy=policy)
            self.assertEqual(driver.build('all').status, 'BLOCKED')
            self.assertEqual(process.requests, [])
            self.assertEqual(runtime.markers.files, {})

    def test_local_build_command_does_not_change_parent_scope(self):
        driver, runtime, process, saved = self.make(text='all:\n  BUILDCMD = echo local\n'
            '  @port_build(globals())\n  _recipe.SEEN = yes\n')
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(process.requests[0].command, 'echo local')
        self.assertEqual(driver.scope.local['BUILDCMD'], './configure --prefix=/prefix && make')
        self.assertEqual(driver.scope.local['SEEN'], 'yes')

    def test_testdepend_empty_or_disabled_is_noop_without_process_or_directory(self):
        for values in ({}, {'DEPEND_TEST': ''},
                {'SKIPTEST': 'yes', 'AUTODEPEND': UnavailableValue('unused')},
                {'AUTODEPEND': 'no', 'DEPEND_TEST': UnavailableValue('unused')}):
            driver, runtime, process, saved = self.make(values,
                text='all:\n  @port_testdepend(globals())\n  AFTER = yes\n',
                directories=PortDirectories(), policy=PortCommandPolicy())
            result = driver.build('all')
            self.assertEqual(result.status, 'COMPLETE')
            self.assertEqual(result.bodies[0].scope.local['AFTER'], 'yes')
            self.assertEqual(process.requests, [])
            self.assertEqual(runtime.markers.files, {})

    def test_testdepend_nonempty_is_a_semantic_gate(self):
        for value in ('module', ' ', UnavailableValue('deferred')):
            driver, runtime, process, saved = self.make({'DEPEND_TEST': value},
                text='all:\n  @port_testdepend(globals())\n  AFTER = no\n')
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED')
            self.assertNotIn('AFTER', result.bodies[0].scope.local)
            self.assertEqual(process.requests, [])

    def test_test_wrapper_reuses_process_and_testdir(self):
        driver, runtime, process, saved = self.make({'TESTDIR': '/tests', 'BUILDDIR': '/unused'},
            helper='port_test', marker='test', directories=MemoryPortDirectories(['/tests']))
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(process.requests[0].command, 'true')
        self.assertEqual(process.requests[0].cwd, '/tests')
        self.assertIn('/recipe/done/test', runtime.markers.files)
        for value in ('', None):
            driver, runtime, process, saved = self.make({'TESTCMD': value}, helper='port_test', marker='test')
            result = driver.build('all')
            self.assertEqual(result.status, 'BLOCKED')
            self.assertEqual(result.bodies[0].processes[0].command, 'aap test')
            self.assertEqual(process.requests, [])
            self.assertNotIn('/recipe/done/test', runtime.markers.files)

    def test_skiptest_returns_before_command_or_directory_reads(self):
        driver, runtime, process, saved = self.make({'SKIPTEST': 'yes', 'TESTCMD': UnavailableValue('unused')},
            text='all:\n  @port_test(globals())\n', directories=PortDirectories())
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(process.requests, [])
        self.assertEqual(runtime.markers.files, {})


if __name__ == '__main__':
    unittest.main()
