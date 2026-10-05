"""Production declarative forms; source-derived tests, no legacy driver runs.

Evidence: Commands.aap_filetype/aap_action/aap_include/aap_pass,
Filetype.ft_add_rules/_add_suffix, Action.action_add/find_action,
Process.Process variant stack, DoRead.read_recipe/doread, rectest/test006.py.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse, FrontendError
from aap_semantics import (lower, Evaluator, Scope, DeclarationState,
                           SourceLoader, SemanticError, Unsupported, UndefinedName,
                           HelperRegistry)


def program(text, name='/recipes/main.aap'):
    return lower(parse(Source(name, text)))


class MemoryLoader(object):
    def __init__(self, files):
        self.files = files
        self.reads = []

    def load(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise IOError('fixture not found')
        return Source(path, self.files[path])


class DeclarationTests(unittest.TestCase):
    def test_filetype_data_registration_scope_and_spans(self):
        ast = program(':filetype\n  # data comment\n  suffix tar.Z tarZ\n'
                      '  suffix xz xz\nAFTER = yes\n')
        result = Evaluator().run(ast)
        state = result.declarations
        self.assertTrue(result.complete)
        self.assertEqual(state.filetypes, set(('tarZ', 'xz')))
        rule = state.suffixes['tar.Z']
        self.assertEqual(rule.filetype, 'tarZ')
        self.assertEqual(rule.source.slice(rule.span), '  suffix tar.Z tarZ\n')
        self.assertEqual(rule.span.start.line, 3)
        self.assertIs(rule.scope, result.scope)
        self.assertIs(rule.body, ast.statements[0].body)
        self.assertFalse(rule.body.origin.scanned)

    def test_filetype_quote_tail_replacement_and_removal(self):
        result = Evaluator().run(program(':filetype\n  suffix "foo bar" one extra\n'
                    '  suffix x first\n  suffix x second # ignored\n'
                    '  suffix "foo bar" remove\n'))
        state = result.declarations
        self.assertEqual(list(state.suffixes), ['x'])
        self.assertEqual(state.suffixes['x'].filetype, 'second')
        self.assertIn('first', state.filetypes)  # Historical removal keeps declarations.
        self.assertEqual(len(state.suffix_history), 4)

    def test_filetype_body_never_expands_or_executes(self):
        result = Evaluator().run(program(':filetype\n  suffix $name literal\n'))
        self.assertIn('$name', result.declarations.suffixes)
        for body in ('  @bad()\n', '  :sys forbidden\n', '  python\n    bad()\n'):
            with self.assertRaises(Unsupported):
                Evaluator().run(program(':filetype\n' + body))
        for body in ('  suffix x\n', '  suffix "x type\n'):
            with self.assertRaises(SemanticError):
                Evaluator().run(program(':filetype\n' + body))

    def test_all_five_production_actions_remain_deferred(self):
        for filetype in ('targz', 'zip', 'tarbz2', 'tarZ', 'xz'):
            ast = program(':action extract ' + filetype + '\n'
                          '  @not even valid Python !\n  :sys forbidden\nAFTER = yes\n')
            result = Evaluator().run(ast)
            definition = result.declarations.latest_action('extract', filetype)
            self.assertTrue(result.complete)
            self.assertEqual(definition.output_type, 'default')
            self.assertIs(definition.scope, result.scope)
            self.assertIs(definition.body, ast.statements[0].body)
            self.assertEqual(definition.span, ast.statements[0].span)
            self.assertFalse(definition.body.origin.scanned)
            self.assertIn('@not even valid', definition.body.origin.text)
            self.assertEqual(result.scope.local, {'_prevdir': None, 'AFTER': 'yes'})

    def test_action_redefinition_retains_history_and_live_definition_scope(self):
        state = DeclarationState()
        first = Evaluator(declarations=state).run(program(
            ':action extract xz\n  :sys first\nLATER = visible\n'))
        old = state.latest_action('extract', 'xz')
        self.assertEqual(old.scope.local['LATER'], 'visible')
        second = Evaluator(declarations=state).run(program(
            ':action extract zip\n  :sys other\n:action extract xz\n  :sys replacement\n'))
        self.assertEqual(len(state.actions['extract']), 3)
        self.assertIs(state.actions['extract'][0], old)
        self.assertIs(old.scope, first.scope)
        self.assertIs(state.latest_action('extract', 'xz').scope, second.scope)
        self.assertIsNone(state.latest_action('missing', 'xz'))

    def test_unobserved_action_and_filetype_forms_are_explicit(self):
        for text in (':action build out in\n  :sys never\n',
                     ':action extract xz,zip\n  :sys never\n',
                     ':action extract xz\n', ':filetype external.afd\n'):
            with self.assertRaises(Unsupported):
                Evaluator().run(program(text))

    def test_command_header_expansion_occurs_only_when_reached(self):
        result = Evaluator().run(program('KIND = zip\n:action extract $KIND\n'
                  '  :sys deferred\n@if False:\n  :action $MISSING zip\n    :sys never\n'))
        self.assertIsNotNone(result.declarations.latest_action('extract', 'zip'))

    def test_unsupported_commands_stop_before_later_mutation_or_header_evaluation(self):
        for name in ('tree', 'checksum', 'print', 'sys', 'syseval'):
            body = '\n  :sys never' if name == 'tree' else ''
            ast = program(':action extract zip\n  :sys never\n:' + name +
                          ' `unapproved()`' + body + '\nAFTER = stale\n')
            result = Evaluator().run(ast)
            self.assertEqual(result.halted_at.name, name)
            self.assertNotIn('AFTER', result.scope.local)
            self.assertEqual(result.deferred, [result.halted_at])


class VariantTests(unittest.TestCase):
    TEXT = (':variant MODE\n  first ignored condition\n    VALUE = first\n'
            '  second\n    VALUE = second\n')

    def run_variant(self, prefix='', scope=None, suffix=''):
        return Evaluator(scope).run(program(prefix + self.TEXT + suffix))

    def test_default_and_explicit_selection_update_bdir(self):
        for prefix, selected in (('BDIR = build\n', 'first'),
                                ('BDIR = build\nMODE = second\n', 'second')):
            result = self.run_variant(prefix)
            self.assertTrue(result.complete)
            self.assertEqual(result.scope.local['MODE'], selected)
            self.assertEqual(result.scope.local['VALUE'], selected)
            self.assertEqual(result.scope.local['BDIR'], 'build-' + selected)

    def test_empty_is_not_missing_and_invalid_value_errors(self):
        for value in ('', 'unknown'):
            with self.assertRaises(SemanticError) as error:
                self.run_variant('BDIR = build\nMODE = ' + value + '\n')
            self.assertIn('invalid value for MODE', str(error.exception))

    def test_fallback_and_star_only(self):
        result = self.run_variant('BDIR = build\nMODE = unknown\n',
                                 suffix='  *\n    VALUE = fallback\n')
        self.assertEqual(result.scope.local['VALUE'], 'fallback')
        self.assertEqual(result.scope.local['BDIR'], 'build-unknown')
        result = Evaluator().run(program(':variant MODE\n  *\n    VALUE = only\n'))
        self.assertEqual(result.scope.local, {'_prevdir': None, 'VALUE': 'only'})

    def test_inherited_reads_and_local_writes(self):
        outer = Scope.top_level()
        outer.store('MODE', 'second', program(''))
        outer.store('BDIR', 'build', program(''))
        inner = Scope((outer,))
        result = self.run_variant(scope=inner)
        self.assertNotIn('MODE', inner.local)
        self.assertEqual(inner.local['BDIR'], 'build-second')
        self.assertEqual(outer.local['BDIR'], 'build')
        self.assertIs(result.scope, inner)

    def test_nested_variants_and_pass(self):
        result = Evaluator().run(program('BDIR = build\n:variant AA\n  first\n'
                    '    :variant BB\n      nested\n        :pass\n'))
        self.assertEqual(result.scope.local['BDIR'], 'build-first-nested')
        with self.assertRaises(SemanticError):
            Evaluator().run(program(':pass argument\n'))

    def test_unselected_capabilities_and_command_arguments_are_not_evaluated(self):
        result = Evaluator().run(program('BDIR = build\n:variant MODE\n  yes\n'
                    '    VALUE = ok\n  no\n    @unapproved()\n'
                    '    :include $MISSING\n    :filetype nonexistent.afd\n'))
        self.assertTrue(result.complete)
        self.assertEqual(result.scope.local['VALUE'], 'ok')

    def test_unselected_python_syntax_checked_but_action_body_is_not(self):
        scope = Scope.top_level()
        with self.assertRaises(SemanticError):
            Evaluator(scope).run(program('BDIR = build\n:variant MODE\n  yes\n'
                      '    :pass\n  no\n    @bad +\n'))
        self.assertEqual(scope.local, {'_prevdir': None})  # Whole-source syntax preparation.
        result = Evaluator().run(program('BDIR = build\n:variant MODE\n  yes\n'
                    '    :pass\n  no\n    :action extract zip\n      @bad +\n'))
        self.assertTrue(result.complete)
        self.assertEqual(result.declarations.actions, {})

    def test_selected_branch_capabilities_reject_and_missing_bdir_is_explicit(self):
        with self.assertRaises(Unsupported):
            Evaluator().run(program('BDIR = build\n:variant MODE\n  yes\n    @unapproved()\n'))
        with self.assertRaises(UndefinedName):
            self.run_variant()


class IncludeTests(unittest.TestCase):
    def test_explicit_context_overrides_source_directory_and_is_shared_with_helpers(self):
        loader = MemoryLoader({'/context/part.aap': '@where = os.path.abspath(".")\n'})
        helpers = HelperRegistry()
        evaluator = Evaluator(helpers=helpers, include_loader=loader, cwd='/context')
        result = evaluator.run(program(':include part.aap\n'))
        self.assertEqual(result.scope.local['where'], '/context')
        self.assertEqual(loader.reads, ['/context/part.aap'])
        self.assertIsNone(helpers.cwd)
        with self.assertRaises(ValueError):
            Evaluator(helpers=HelperRegistry('/different'), cwd='/context')

    def test_relative_nested_include_uses_unchanged_recipe_directory(self):
        loader = MemoryLoader({'/recipes/sub/part.aap':
                        'VALUE += included\n:include peer.aap\n',
                               '/recipes/peer.aap': 'VALUE += peer\n'})
        result = Evaluator(include_loader=loader).run(program(
                    'VALUE = root\n:include sub/part.aap\nVALUE += end\n'))
        self.assertEqual(result.scope.local['VALUE'], 'root included peer end')
        self.assertEqual(loader.reads, ['/recipes/sub/part.aap', '/recipes/peer.aap'])
        self.assertTrue(result.complete)

    def test_include_expansion_scope_identity_and_declaration_sharing(self):
        loader = MemoryLoader({'/shared.aap': '_recipe.VALUE = changed\n'
                               'user_scope.ITEM = shared\n'
                               ':action extract zip\n  :sys never\n'})
        result = Evaluator(include_loader=loader).run(program(
                    'PATH = ../shared.aap\nVALUE = old\n:include $PATH\n'
                    'AFTER = $user_scope.ITEM\n'))
        self.assertEqual(result.scope.local['VALUE'], 'changed')
        self.assertEqual(result.scope.local['AFTER'], 'shared')
        definition = result.declarations.latest_action('extract', 'zip')
        self.assertIs(definition.scope, result.scope)
        self.assertEqual(definition.span.source_id, '/shared.aap')
        self.assertEqual(definition.span.start.line, 3)
        self.assertEqual(len(result.includes), 1)
        self.assertEqual(result.includes[0].program.source.text, loader.files['/shared.aap'])

    def test_repeated_includes_run_again_and_active_recursion_is_skipped(self):
        loader = MemoryLoader({'/recipes/part.aap': 'VALUE += part\n'
                               ':include ./main.aap\n'})
        result = Evaluator(include_loader=loader).run(program(
                    'VALUE = start\n:include part.aap\n:include part.aap\n'))
        self.assertEqual(result.scope.local['VALUE'], 'start part part')
        self.assertEqual(loader.reads, ['/recipes/part.aap', '/recipes/part.aap'])
        self.assertEqual([r.skipped_active for r in result.includes],
                         [False, True, False, True])

    def test_missing_include_and_included_diagnostics(self):
        with self.assertRaises(SemanticError) as error:
            Evaluator(include_loader=MemoryLoader({})).run(program('\n:include missing.aap\n'))
        self.assertIn('/recipes/main.aap:2:1: cannot read include', str(error.exception))
        for text in ('\nAA = $MISSING\n', '\n@if syntax +\n'):
            with self.assertRaises(FrontendError) as error:
                Evaluator(include_loader=MemoryLoader({'/recipes/part.aap': text})).run(
                    program(':include part.aap\n'))
            self.assertIn('/recipes/part.aap:2:1:', str(error.exception))

    def test_include_barrier_stops_caller_and_retains_included_program(self):
        loader = MemoryLoader({'/recipes/part.aap': 'BEFORE = yes\n:print never\nSTALE = no\n'})
        result = Evaluator(include_loader=loader).run(program(':include part.aap\nAFTER = no\n'))
        self.assertFalse(result.complete)
        self.assertEqual(result.scope.local, {'_prevdir': None, 'BEFORE': 'yes'})
        self.assertEqual(result.halted_at.span.source_id, '/recipes/part.aap')
        self.assertIs(result.halted_at, result.includes[0].program.statements[1])

    def test_include_context_and_dynamic_forms_reject_without_reads(self):
        loader = MemoryLoader({})
        for path in ('*.aap', '~/recipe.aap', 'https://example.test/main.aap',
                     '{once} part.aap', 'one.aap two.aap'):
            with self.assertRaises(Unsupported):
                Evaluator(include_loader=loader).run(program(':include ' + path + '\n'))
        with self.assertRaises(Unsupported):
            Evaluator(include_loader=loader).run(program(':include part.aap\n', '<string>'))
        self.assertEqual(loader.reads, [])
        with self.assertRaises(Unsupported):
            Evaluator().run(program(':include part.aap\n'))

    def test_include_depth_guard_and_cleanup(self):
        loader = MemoryLoader({'/recipes/one.aap': ':include two.aap\n'})
        evaluator = Evaluator(include_loader=loader, max_include_depth=2)
        with self.assertRaises(Unsupported):
            evaluator.run(program(':include one.aap\n'))
        self.assertEqual(evaluator.active_sources, set())
        self.assertIsNone(evaluator.cwd)

    def test_read_only_file_loader_normal_pipeline(self):
        filename = os.path.join(ROOT, 'tests', 'semantics', 'fixtures', 'include-root.aap')
        result = Evaluator(include_loader=SourceLoader('utf-8')).run(
                    lower(parse(Source.from_path(filename, 'utf-8'), file_mode=True)))
        self.assertTrue(result.complete)
        self.assertEqual(result.scope.local['VALUE'], 'root child final')
        self.assertTrue(result.includes[0].path.endswith('/include-child.aap'))


if __name__ == '__main__':
    unittest.main()
