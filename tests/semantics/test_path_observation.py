"""Process.Process/RecPython imports and CPython 2.7 genericpath.exists.

No host stat, process launcher, write capability or private recipe is used.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
    MemoryPersistence, PathObserver, MemoryPathObserver, PathObservation,
    SemanticError, Unsupported, ProcessPolicy, PortRuntime, MemoryMarkers)
from aap_semantics.port_commands import PortCommandRuntime, MemoryPortDirectories
from aap_semantics.values import UnavailableValue
from test_cd import Process, Loader


def program(text, source='/recipe/main.aap'):
    return lower(parse(Source(source, text)))


def observer(path='/recipe/item', status='EXISTS'):
    return MemoryPathObserver({path: PathObservation(status)})


def evaluate(text, paths=None, **kwargs):
    engine = Evaluator(cwd='/recipe', path_observer=paths, **kwargs)
    return engine.run(program(text))


class PathTests(unittest.TestCase):
    def make(self, body, paths=None, extra='', loader=None, checksum=None):
        data = Evaluator().run(program('all:\n' + ''.join(
            '  ' + line + '\n' for line in body.splitlines()) + extra))
        dirs = MemoryPortDirectories(['/recipe/sub', '/recipe/inner'])
        runtime = PortRuntime(markers=MemoryMarkers(), commands=PortCommandRuntime(dirs))
        process, saved, state = Process(), MemoryPersistence(), MemoryTargetState()
        for definition in data.graph.definitions:
            for node in definition.targets:
                state.buildchecks[(definition.index, node.identity)] = 'prepared'
        driver = BuildDriver(data.graph, state, saved, data.scope, data.declarations,
            port_defaults=False, port_runtime=runtime, path_observer=paths,
            process_backend=process, process_policy=ProcessPolicy('utf-8', sys_mode='unlogged'),
            include_loader=loader, checksum_backend=checksum)
        return driver, process, saved

    def test_approved_relative_exists(self):
        paths = observer()
        result = evaluate('@answer = os.path.exists("item")\n', paths)
        self.assertIs(result.scope.local['answer'], True)
        request = paths.requests[0]
        self.assertEqual((request.argument, request.path, request.cwd), ('item', '/recipe/item', '/recipe'))
        self.assertEqual(request.span.source_id, '/recipe/main.aap')
        self.assertEqual(request.span.start.line, 1)
        self.assertEqual(result.path_observations[0].observation.status, 'EXISTS')

    def test_missing(self):
        result = evaluate('@answer = os.path.exists("item")\n', observer(status='MISSING'))
        self.assertIs(result.scope.local['answer'], False)

    def test_absolute_without_cwd(self):
        paths = observer('/absolute')
        data = Evaluator(path_observer=paths).run(program('@answer = os.path.exists("/absolute")\n', 'memory'))
        self.assertIs(data.scope.local['answer'], True)
        self.assertEqual(paths.requests[0].path, '/absolute')

    def test_relative_without_cwd_blocks(self):
        paths = observer()
        with self.assertRaises(Unsupported):
            Evaluator(path_observer=paths).run(program('@os.path.exists("item")\n', 'memory'))
        self.assertEqual(paths.requests, [])

    def test_path_spelling_not_normalized_or_expanded(self):
        path = '/recipe/link/../$NAME/*/~/'
        paths = observer(path)
        evaluate('@os.path.exists("link/../$NAME/*/~/")\n', paths)
        self.assertEqual(paths.requests[0].path, path)

    def test_var2string_no_namespace(self):
        scope = Scope.top_level()
        scope.local['PKGDIR'] = '/pack'
        result = evaluate('@answer = os.path.exists(var2string(_no.PKGDIR))\n', observer('/pack'), scope=scope)
        self.assertIs(result.scope.local['answer'], True)

    def test_integer_requires_explicit_conversion(self):
        paths = observer('/recipe/123')
        result = evaluate('@answer = os.path.exists(var2string(123))\n', paths)
        self.assertIs(result.scope.local['answer'], True)
        with self.assertRaises(SemanticError):
            evaluate('@os.path.exists(123)\n', paths)
        self.assertEqual(len(paths.requests), 1)

    def test_unsupported_types(self):
        for expression in ('None', 'True', '[]', '("x",)', 'b"x"'):
            paths = observer()
            with self.assertRaises(SemanticError):
                evaluate('@os.path.exists(' + expression + ')\n', paths)
            self.assertEqual(paths.requests, [])

    def test_unknown_value_blocks_without_coercion(self):
        scope = Scope.top_level()
        scope.local['PKGDIR'] = UnavailableValue('deferred value')
        with self.assertRaises(Unsupported):
            evaluate('@os.path.exists(var2string(_no.PKGDIR))\n', observer(), scope=scope)

    def test_empty_path_is_false_without_joining_cwd(self):
        paths = observer('/recipe')
        result = evaluate('@answer = os.path.exists("")\n', paths)
        self.assertIs(result.scope.local['answer'], False)
        self.assertEqual(result.path_observations[0].request.path, '')
        self.assertEqual(paths.requests, [])

    def test_nul_is_error_before_observation(self):
        paths = observer()
        with self.assertRaises(SemanticError) as caught:
            evaluate('@os.path.exists("a\\x00b")\n', paths)
        self.assertIn('NUL', str(caught.exception))
        self.assertEqual(paths.requests, [])

    def test_unavailable_is_not_false(self):
        paths = MemoryPathObserver()
        driver, process, saved = self.make('@before = 1\n@os.path.exists("item")\nAFTER = no', paths)
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].scope.local['before'], 1)
        self.assertNotIn('AFTER', result.bodies[0].scope.local)
        self.assertEqual(result.bodies[0].evaluation.path_observations[0].observation.status, 'UNAVAILABLE')
        self.assertEqual(saved.writes, [])
        self.assertNotIn('/recipe/all', driver.completed)

    def test_backend_error_is_failed(self):
        driver, process, saved = self.make('@os.path.exists("item")', observer(status='ERROR'))
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.span.start.line, 2)
        self.assertEqual(saved.writes, [])
        self.assertEqual(process.requests, [])

    def test_exceptions_and_invalid_results(self):
        class Backend(PathObserver):
            def observe(self, request):
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                return self.outcome
        backend = Backend()
        for outcome, status in ((OSError('adapter failed'), 'FAILED'),
                                (RuntimeError('broken adapter'), 'FAILED'),
                                (NotImplementedError('offline'), 'BLOCKED'),
                                (True, 'FAILED'), (PathObservation('bad'), 'FAILED')):
            backend.outcome = outcome
            driver, process, saved = self.make('@os.path.exists("item")', backend)
            self.assertEqual(driver.build('all').status, status)

    def test_observed_stat_errors_and_links(self):
        # Adapter observations certify stat semantics, not lexists semantics.
        for detail in ('dangling symlink: ENOENT', 'permission denied: EACCES', 'ENOTDIR'):
            paths = MemoryPathObserver({'/recipe/item': PathObservation('MISSING', detail)})
            self.assertIs(evaluate('@x = os.path.exists("item")\n', paths).scope.local['x'], False)
        for detail in ('regular file', 'directory', 'symlink to existing file'):
            paths = MemoryPathObserver({'/recipe/item': PathObservation('EXISTS', detail)})
            self.assertIs(evaluate('@x = os.path.exists("item")\n', paths).scope.local['x'], True)

    def test_other_filesystem_calls_remain_unavailable(self):
        for call in ('os.path.isfile("x")', 'os.path.isdir("x")', 'os.path.lexists("x")',
                     'os.path.getmtime("x")', 'os.stat("x")', 'os.access("x", 0)',
                     'os.listdir("x")', 'os.mkdir("x")', 'open("x")'):
            paths = observer()
            with self.assertRaises(Unsupported):
                evaluate('@' + call + '\n', paths)
            self.assertEqual(paths.requests, [])

    def test_reflection_alias_and_shadowing_rejected(self):
        for code in ('@getattr(os.path, "exists")("item")\n',
                     '@f = os.path.exists\n@f("item")\n',
                     '@os = "shadow"\n@os.path.exists("item")\n',
                     '@os.path.__dict__\n', '@__import__("os")\n'):
            with self.assertRaises(SemanticError):
                evaluate(code, observer())

    def test_arity_and_keywords(self):
        for args in ('', '"a", "b"', 'path="a"'):
            with self.assertRaises(SemanticError):
                evaluate('@os.path.exists(' + args + ')\n', observer())

    def test_conditional_short_circuit_needs_no_observation(self):
        paths = MemoryPathObserver()
        result = evaluate('@if False and os.path.exists("item"):\n  BAD = no\nAFTER = yes\n', paths)
        self.assertEqual(result.scope.local['AFTER'], 'yes')
        self.assertEqual(paths.requests, [])

    def test_known_deferred_call_checked_only_when_reached(self):
        scope = Scope.top_level()
        result = evaluate('@if False:\n  @os.path.isdir("item")\n@before = 1\n', scope=scope)
        self.assertTrue(result.complete)
        with self.assertRaises(Unsupported):
            evaluate('@before = 2\n@os.path.isdir("item")\n', scope=scope)
        self.assertEqual(scope.local['before'], 2)

    def test_syntax_remains_whole_body_validated(self):
        scope = Scope.top_level()
        with self.assertRaises(SemanticError):
            evaluate('@before = 1\n@if True:\n  @x = (\n', scope=scope)
        self.assertNotIn('before', scope.local)

    def test_cd_changes_observation_cwd_not_host(self):
        host = os.getcwd()
        paths = observer('/recipe/sub/item')
        driver, process, saved = self.make(':cd sub\n@answer = os.path.exists("item")', paths)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(paths.requests[0].cwd, '/recipe/sub')
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertEqual(os.getcwd(), host)

    def test_include_shares_frame_and_keeps_source(self):
        paths = observer('/recipe/sub/item')
        loader = Loader({'/recipe/sub/part.aap': '@answer = os.path.exists("item")\n'})
        driver, process, saved = self.make(':cd sub\n:include part.aap', paths, loader=loader)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(paths.requests[0].cwd, '/recipe/sub')
        self.assertEqual(paths.requests[0].span.source_id, '/recipe/sub/part.aap')

    def test_nested_update_frame_isolation(self):
        paths = MemoryPathObserver({'/recipe/sub/item': PathObservation('EXISTS'),
                                    '/recipe/inner/item': PathObservation('MISSING')})
        driver, process, saved = self.make(':cd sub\n:update B\n@outer = os.path.exists("item")', paths,
            extra='B {virtual}:\n  :cd inner\n  @nested = os.path.exists("item")\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([r.cwd for r in paths.requests], ['/recipe/inner', '/recipe/sub'])
        body = result.bodies[0]
        self.assertIs(body.scope.local['outer'], True)
        nested = body.updates[0].builds[0].bodies[0]
        self.assertIs(nested.scope.local['nested'], False)
        self.assertIs(nested.context.path_observer, paths)
        self.assertNotIn('nested', body.scope.local)

    def test_nested_unavailable_retains_invocation_and_cause(self):
        driver, process, saved = self.make(':update B\nAFTER = no', MemoryPathObserver(),
            extra='B {virtual}:\n  @os.path.exists("item")\n')
        result = driver.build('all')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.bodies[0].updates[0].request.span.start.line, 2)
        self.assertEqual(result.span.start.line, 5)
        self.assertNotIn('AFTER', result.bodies[0].scope.local)

    def test_shell_success_does_not_imply_exists(self):
        for status, expected in (('MISSING', 'COMPLETE'), ('UNAVAILABLE', 'BLOCKED')):
            paths = observer('/recipe/X', status)
            driver, process, saved = self.make(':sys mkdir -p X\n@answer = os.path.exists("X")', paths)
            result = driver.build('all')
            self.assertEqual(result.status, expected)
            self.assertEqual(len(process.requests), 1)
            self.assertEqual(result.bodies[0].scope.local['sysresult'], 0)
            if expected == 'COMPLETE':
                self.assertIs(result.bodies[0].scope.local['answer'], False)
            else:
                self.assertNotIn('answer', result.bodies[0].scope.local)

    def test_checksum_bytes_do_not_imply_exists(self):
        from aap_semantics import ChecksumBackend, MemoryArtifacts
        artifacts = MemoryArtifacts({'/recipe/item': b''})
        backend = ChecksumBackend(artifacts)
        paths = observer(status='MISSING')
        driver, process, saved = self.make(
            ':checksum item {md5 = d41d8cd98f00b204e9800998ecf8427e}\n@answer = os.path.exists("item")',
            paths, checksum=backend)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.bodies[0].checksums[0].status, 'VERIFIED')
        self.assertIs(result.bodies[0].scope.local['answer'], False)
        self.assertEqual(process.requests, [])

    def test_action_inherits_capability_with_its_own_cwd(self):
        from test_actions import ExtractTests
        paths = observer('/recipe/work/item')
        driver, runtime, workspace, saved, definition = ExtractTests().make(
            action='@_caller.ANSWER = os.path.exists("item")\n')
        driver.capabilities = driver.capabilities.replace(path_observer=paths)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertIs(result.bodies[0].scope.local['ANSWER'], True)
        self.assertEqual(paths.requests[0].cwd, '/recipe/work')
        self.assertEqual(paths.requests[0].span.source_id, '/definitions/actions.aap')
        self.assertEqual(result.bodies[0].context.cwd, '/recipe')
        self.assertEqual(saved.writes, [])

    def test_records_are_per_run_not_cached(self):
        paths = observer()
        engine = Evaluator(cwd='/recipe', path_observer=paths)
        code = program('@answer = os.path.exists("item")\n')
        first = engine.run(code)
        paths.observations['/recipe/item'] = PathObservation('MISSING')
        second = engine.run(code)
        self.assertEqual(len(first.path_observations), 1)
        self.assertEqual(first.path_observations[0].observation.status, 'EXISTS')
        self.assertEqual(second.path_observations[0].observation.status, 'MISSING')
        self.assertIs(second.scope.local['answer'], False)


class NanoPathTests(unittest.TestCase):
    def check_branch(self, status, expected_line):
        from test_port_runtime import NanoPortIntegration
        from aap_semantics.model import Conditional, Program
        harness = NanoPortIntegration()
        path = '/authorized/ports/editors/nano/pack'
        paths = observer(path, status)
        driver, runtime, saved, process = harness.setup_nano(
            actions=harness.extraction(), enable_sys=True, path_observer=paths,
            directories=MemoryPortDirectories(['/authorized/ports/editors/nano/work/nano-7.1']))
        self.assertEqual(driver.build().status, 'COMPLETE')
        result = driver.build('rpm')
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.span.start.line, 221)
        self.assertEqual(result.blocked_at.name, 'print')
        body = result.bodies[-1]
        self.assertEqual(body.scope.local['file'], 'work/nano.spec')
        self.assertEqual(body.context.cwd, '/authorized/ports/editors/nano')
        self.assertEqual(body.scope.lookup('PKGDIR'), path)
        self.assertEqual(paths.requests, [])  # Do not skip earlier print operations.
        self.assertEqual(len(process.requests), 9)
        self.assertNotIn('/authorized/ports/editors/nano/prep-rpm', driver.completed)
        self.assertEqual(saved.writes, [])
        self.assertTrue(result.pending_signatures)
        # Separate source-backed fragment characterization, NOT continued Nano
        # execution. Keep the actual condition, branches and following :print.
        nodes = body.program.statements
        index = next(i for i, node in enumerate(nodes)
                     if isinstance(node, Conditional) and node.span.start.line == 291)
        fragment = Program(body.program, nodes[index:index + 2])
        focused = Evaluator(body.scope, cwd=body.context.cwd, path_observer=paths).run(fragment)
        self.assertFalse(focused.complete)
        self.assertEqual(focused.halted_at.name, 'print')
        self.assertEqual(focused.halted_at.span.start.line, expected_line)
        self.assertEqual(paths.requests[0].path, path)
        self.assertEqual(paths.requests[0].cwd, '/authorized/ports/editors/nano')
        self.assertEqual(paths.requests[0].span.start.line, 291)
        self.assertEqual(focused.path_observations[0].observation.status, status)
        self.assertEqual(len(process.requests), 9)

    def test_missing_pkgdir_skips_conditional_prints(self):
        self.check_branch('MISSING', 294)

    def test_existing_pkgdir_enters_first_conditional_print(self):
        self.check_branch('EXISTS', 292)


if __name__ == '__main__':
    unittest.main()
