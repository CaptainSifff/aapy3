"""Metadata-only characterization; no legacy recipe or external effect runs.

Sources: Scope.py, Commands.aap_assign/expand, RecPython.var2string/var2list,
Dictlist.listitem2str/str2dictlist; rectest/test006.py, test007.py, test008.py.
Production justification: the supplied ports/globals.aap and Python-block audit.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse, cst
from aap_semantics import lower, lower_body, Evaluator, Scope, HelperRegistry
from aap_semantics import SemanticError, Unsupported, UndefinedName
from aap_semantics import model as m
from aap_semantics.expansion import expand_text, render_value
from aap_semantics.values import MISSING, DeferredExpansion, var2string, var2list
from aap_semantics.python_eval import PythonEvaluator, constant
from check_frontend_py34 import check_text


def program(text):
    return lower(parse(text, 'fixture.aap'))


def run(text, scope=None, helpers=None, max_steps=10000):
    return Evaluator(scope, helpers, max_steps).run(program(text))


def scope_with_rel(value):
    scope = Scope.top_level()
    scope.store('rel', value, program(''))
    return scope


def fragment(text):
    source = Source('expression.aap', text)
    return m.PythonFragment(cst.Node(source, source.span(0, len(text))), text)


class LoweringTests(unittest.TestCase):
    def test_separate_model_and_span_preservation(self):
        doc = parse('# trivia\nVALUE += text\n:unknown `unapproved()`\n', 'sample.aap')
        lowered = lower(doc)
        assignment, command = lowered.statements
        self.assertIsInstance(assignment, m.Assignment)
        self.assertEqual(assignment.mode, 'append')
        self.assertIs(assignment.origin, doc.children[1])
        self.assertEqual(assignment.span, doc.children[1].span)
        self.assertEqual(command.name, 'unknown')
        self.assertEqual(command.source.slice(command.span), ':unknown `unapproved()`\n')
        self.assertEqual(''.join(n.text for n in doc.children), doc.text)
        self.assertNotIsInstance(assignment, cst.AssignmentStatement)

    def test_assignment_operator_normalization(self):
        for op, mode, delayed in (('=', 'replace', False), ('+=', 'append', False),
                                  ('?=', 'default', False), ('$=', 'replace', True),
                                  ('$+=', 'append', True), ('$?=', 'default', True)):
            node = program('AA ' + op + ' x\n').statements[0]
            self.assertEqual((node.mode, node.delayed), (mode, delayed))

    def test_literal_value(self):
        ast = program('DESC << END\n@fake\n:sys fake\nEND\n')
        node = ast.statements[0]
        self.assertIsInstance(node.value, m.LiteralValue)
        self.assertEqual(node.value.value, '@fake\n:sys fake\n')
        self.assertEqual(Evaluator().run(ast).scope.local['DESC'], '@fake\n:sys fake\n')

    def test_dependency_body_remains_lazy(self):
        ast = program('all:\n  @not valid Python !!\n')
        node = ast.statements[0]
        self.assertIsInstance(node, m.Dependency)
        self.assertIsInstance(node.body, m.DeferredBody)
        result = Evaluator(cwd='/recipe').run(ast)
        self.assertTrue(result.complete)
        self.assertIs(result.graph.definitions[0].body, node.body)
        inner = lower_body(node.body)
        with self.assertRaises(SemanticError):
            Evaluator().run(inner)

    def test_mixed_suites_and_locations(self):
        ast = program('@if first:\n  AA = one\n@elif second:\n  @for x in xs:\n'
                      '    :print $x\n@else:\n  AA = other\nAFTER = yes\n')
        conditional = ast.statements[0]
        self.assertIsInstance(conditional, m.Conditional)
        self.assertEqual(len(conditional.branches), 2)
        loop = conditional.branches[1][1].statements[0]
        self.assertIsInstance(loop, m.Loop)
        self.assertEqual(loop.body.statements[0].span.start.line, 5)
        self.assertEqual(conditional.span.end.line, 8)
        self.assertEqual(ast.statements[1].target, 'AFTER')

    def test_invalid_suite_boundaries(self):
        for text in ('@else:\n  AA = x\n', '@if x:\nAA = y\n',
                     '@if x:\n  AA = x\n@else invalid:\n  AA = y\n'):
            with self.assertRaises(SemanticError):
                program(text)

    def test_python_blocks_are_deferred(self):
        ast = program(':python\n print "legacy"\n')
        result = Evaluator().run(ast)
        self.assertIsInstance(result.halted_at, m.DeferredConstruct)
        self.assertFalse(result.complete)


class ScopeAndAssignmentTests(unittest.TestCase):
    def test_simple_assignment_overwrite_append_default(self):
        # test007: command-line seed is not absolute; ?= keeps it, = overwrites.
        scope = Scope.top_level()
        origin = program('')
        scope.store('BBB', 'bbb', origin)
        scope.store('CCC', 'ccc', origin)
        result = run('BBB ?= xxx\nCCC = yyy\nAA = one\nAA += two\nAA +=\n', scope)
        self.assertTrue(result.complete)
        self.assertEqual(scope.local['BBB'], 'bbb')
        self.assertEqual(scope.local['CCC'], 'yyy')
        self.assertEqual(scope.local['AA'], 'one two ')

    def test_no_lookup_and_local_shadow_writes(self):
        # Scope.NoScopeDict: read local then ordered _up; write local.
        root = Scope.top_level()
        origin = program('')
        root.store('VALUE', 'outer', origin)
        child = Scope((root,))
        result = run('VALUE ?= ignored\nCOPY = $VALUE\nVALUE += local\n', child)
        self.assertTrue(result.complete)
        self.assertEqual(child.local['COPY'], 'outer')
        self.assertEqual(child.local['VALUE'], 'outer local')
        self.assertEqual(root.local['VALUE'], 'outer')

    def test_bare_python_name_does_not_search_enclosing_scopes(self):
        root = Scope.top_level()
        root.store('VALUE', 'outer', program(''))
        child = Scope((root,))
        run('@copy = _no.VALUE\n', child)
        self.assertEqual(child.local['copy'], 'outer')
        with self.assertRaises(UndefinedName):
            run('@copy = VALUE\n', child)

    def test_no_attribute_writes_and_shared_list_mutation(self):
        root = Scope.top_level()
        root.store('items', ['outer'], program(''))
        child = Scope((root,))
        run('@_no.items.extend(["shared"])\n@_no.items = ["local"]\n', child)
        self.assertEqual(root.local['items'], ['outer', 'shared'])
        self.assertEqual(child.local['items'], ['local'])

    def test_root_aliases_and_user_scopes(self):
        # create_topscope and rectest/test008.py's scoped assignments.
        result = run('_recipe.AA = recipe\n_top.BB = top\n'
                     's_global.PreScript ?= start\n@copy = s_global.PreScript\n'
                     '@s_global.PreScript = "updated"\n')
        self.assertEqual(result.scope.local['AA'], 'recipe')
        self.assertEqual(result.scope.local['BB'], 'top')
        self.assertEqual(result.scope.local['copy'], 'start')
        self.assertEqual(result.scope.read('s_global.PreScript', program('')), 'updated')

    def test_unbound_special_scopes_and_conflicting_user_scope(self):
        for text in ('_parent.AA = x\n', '@x = _parent.AA\n', 'a.b.c = x\n',
                     'existing = value\nexisting.AA = nope\n'):
            with self.assertRaises(SemanticError):
                run(text)
        with self.assertRaises(Unsupported):
            run('@x = _recipe.AA\n', Scope())

    def test_undefined_names_are_not_empty_strings(self):
        for text in ('AA = $ABSENT\n', '@x = ABSENT\n', '@x = _no.ABSENT\n'):
            with self.assertRaises(UndefinedName) as result:
                run(text)
            self.assertIn('fixture.aap:1:1:', str(result.exception))
        self.assertIs(Scope().lookup('absent'), MISSING)

    def test_delayed_assignment_stores_marker_without_expanding(self):
        result = run('AA $= $NOT_DEFINED\n')
        self.assertIsInstance(result.scope.local['AA'], DeferredExpansion)
        self.assertEqual(result.scope.local['AA'].raw, '$NOT_DEFINED')
        with self.assertRaises(Unsupported):
            run('BB = $AA\n', result.scope)

    def test_default_skips_dollars_but_not_backticks(self):
        result = run('@items = []\nAA = set\n'
                     'AA ?= `items.extend(["observed"])`$ABSENT\n')
        self.assertEqual(result.scope.local['items'], ['observed'])
        self.assertEqual(result.scope.local['AA'], 'set')


class ConversionAndExpansionTests(unittest.TestCase):
    def test_var2string(self):
        origin = program('')
        for value, expected in ((None, ''), (7, '7'), (True, 'True'),
                                ('raw', 'raw'), (['a', 'two words'], 'a "two words"'),
                                (['', 'a'], 'a'), (['a', ''], 'a ')):
            self.assertEqual(var2string(value, origin), expected)
        # Quotes switch without using shell-style backslash escaping.
        value = ['a\'b"c', 'x\ty', 'back\\slash']
        self.assertEqual(var2list(var2string(value, origin), origin), value)

    def test_var2list(self):
        origin = program('')
        self.assertEqual(var2list('one "two words" \'three four\'\nlast', origin),
                         ['one', 'two words', 'three four', 'last'])
        self.assertEqual(var2list('"" \'\' a\\b', origin), ['a\\b'])
        self.assertEqual(var2list(None, origin), [])
        self.assertEqual(var2list([], origin), [])
        for value in (['a'], 3, 'a {attr=value}'):
            with self.assertRaises(Unsupported):
                var2list(value, origin)
        with self.assertRaises(SemanticError):
            var2list('"unclosed', origin)

    def test_separate_raw_expansion_and_conversion(self):
        ast = program('AA = $VALUE\n')
        scope = Scope.top_level()
        scope.store('VALUE', 'one "two words"', ast)
        raw = render_value(ast.statements[0].value, PythonEvaluator(scope))
        self.assertEqual(raw, '$VALUE')
        expanded = expand_text(raw, scope, ast)
        self.assertEqual(expanded, 'one "two words"')
        self.assertEqual(var2list(expanded, ast), ['one', 'two words'])

    def test_expansion_escapes_scopes_and_literal_dollars(self):
        result = run('NAME = value\nAA = "$NAME" ${NAME} $(NAME) $NAME.\n'
                     'BB = $$HOME $# $(x) $?ABSENT\n@number = 3\nCC = $number\n')
        self.assertEqual(result.scope.local['AA'], '"value" value value value.')
        self.assertEqual(result.scope.local['BB'], '$HOME # x ')
        self.assertEqual(result.scope.local['CC'], '3')

    def test_expansion_does_not_recurse_on_plain_string_values(self):
        result = run('@raw = "$MISSING"\nAA = $raw\n')
        self.assertEqual(result.scope.local['AA'], '$MISSING')

    def test_unimplemented_expansion_features_fail_explicitly(self):
        for text in ('AA = $*NAME\n', 'AA = $(NAME[0])\n',
                     'AA = $NAME {attr=yes}\n', '@xs = ["x"]\nAA = $xs\n'):
            with self.assertRaises(Unsupported):
                run(text)

    def test_backticks_are_structural_expressions(self):
        result = run('@version = "1-2"\nAA = `re.sub("-", "_", version)`\n'
                     'BB = `"$not_a_variable#"`\nCC = before `` after\n')
        self.assertEqual(result.scope.local['AA'], '1_2')
        self.assertEqual(result.scope.local['BB'], '$not_a_variable#')
        self.assertEqual(result.scope.local['CC'], 'before ` after')
        with self.assertRaises(SemanticError):
            run('@name = `1`\n')  # A-A-P never interpolates @ payloads.

    def test_logical_continuations_and_br_folding(self):
        result = run('AA = one \\\n two\nBB = first$br\n    second\n'
                     'CC = first$$br\n    second\n')
        self.assertEqual(result.scope.local['AA'], 'one  two')
        self.assertEqual(result.scope.local['BB'], 'first\nsecond')
        self.assertEqual(result.scope.local['CC'], 'first$br second')

    def test_argument_joins_are_decided_before_backtick_values(self):
        # Process.get_func_args joins generated argument-source fragments.
        result = run('AA = `None`\n    tail\nBB = `"value$br"`\n    tail\n')
        self.assertEqual(result.scope.local['AA'], ' tail')
        self.assertEqual(result.scope.local['BB'], 'value$br tail')


class PythonTests(unittest.TestCase):
    def test_literals_tuple_unpacking_and_indexing(self):
        # Tuple/index support is a small subsystem extension, not a claim that
        # it occurs in the measured production @ vocabulary.
        result = run('@name = "s"\n@number = 3\n@yes = True\n@nothing = None\n'
                     '@a, b = ("x", ["y"])\n@item = b[0]\n@letter = name[0]\n')
        self.assertEqual(result.scope.local['item'], 'y')
        self.assertEqual(result.scope.local['letter'], 's')
        self.assertIsNone(result.scope.local['nothing'])

    def test_comparisons_membership_and_boolean_operands(self):
        py = PythonEvaluator(Scope.top_level())
        for text, expected in (('3 >= 2', True), ('3 == 3 != 4', True),
                               ('"x" in ["x", "y"]', True),
                               ('"nux" in "Linux"', True),
                               ('not []', True), ('"left" and "right"', 'right'),
                               ('False and missing', False)):
            self.assertEqual(py.expression(fragment(text)), expected)

    def test_addition_is_bounded(self):
        result = run('@a = 1 + 2\n@b = "a" + "b"\n@c = [1] + [2]\n')
        self.assertEqual(result.scope.local['a'], 3)
        self.assertEqual(result.scope.local['b'], 'ab')
        self.assertEqual(result.scope.local['c'], [1, 2])
        with self.assertRaises(Unsupported):
            run('@x = 2 ** 100\n')

    def test_if_elif_else_and_for_mixed_bodies(self):
        result = run('@values = ["a", "b"]\nRESULT =\n'
                     '@for item in values:\n  @if item == "a":\n    RESULT += first\n'
                     '  @elif item == "b":\n    RESULT += second\n'
                     '  @else:\n    RESULT += other\n@pass\n')
        self.assertEqual(result.scope.local['RESULT'], 'first second')
        self.assertEqual(result.scope.local['item'], 'b')
        self.assertTrue(result.complete)
        self.assertEqual(run('@if False:\n  AA = no\n@else:\n  AA = yes\n').scope.local['AA'], 'yes')

    def test_allowed_calls_and_methods(self):
        result = run('@xs = var2list(\'a "b c"\')\n'
                     '@xs.extend(["d"])\n@text = var2string(xs)\n'
                     '@found = string.find(text, "b")\n@number = int("12")\n'
                     '@new = "pkg.i686".replace(".i686", "(x86-32)")\n'
                     '@path = "/".join(["one", "two"])\n')
        self.assertEqual(result.scope.local['xs'], ['a', 'b c', 'd'])
        self.assertEqual(result.scope.local['found'], 3)
        self.assertEqual(result.scope.local['number'], 12)
        self.assertEqual(result.scope.local['new'], 'pkg(x86-32)')
        self.assertEqual(result.scope.local['path'], 'one/two')

    def test_re_search_returns_opaque_match_or_none(self):
        result = run('@matched = re.search("^SUSE12$", "SUSE12")\n'
                     '@unmatched = re.search("^SUSE12$", "SUSE16")\n')
        matched = result.scope.local['matched']
        self.assertTrue(bool(matched))
        self.assertIsNotNone(matched)
        self.assertIsNone(result.scope.local['unmatched'])

    def test_re_search_uses_scoped_values_and_normal_if_truth(self):
        for value, expected in (('SUSE12', 'yes'), ('SUSE16', 'no')):
            result = run('@pattern = "^SUSE12$"\n'
                         '@if re.search(pattern, rel):\n  RESULT = yes\n'
                         '@else:\n  RESULT = no\n',
                         scope_with_rel(value))
            self.assertEqual(result.scope.local['RESULT'], expected)

    def test_re_search_result_uses_reached_python2_numeric_ordering(self):
        # The supplied recipe tests the Python 2 match-or-None result with >= 0.
        for value, expected in (('SUSE12', 'yes'), ('SUSE16', 'no')):
            result = run('@if re.search("^SUSE12$", rel) >= 0:\n'
                         '  RESULT = yes\n@else:\n  RESULT = no\n',
                         scope_with_rel(value))
            self.assertEqual(result.scope.local['RESULT'], expected)

    def test_re_search_anchor_boundaries_and_final_newline(self):
        for value, expected in (('SUSE12', True), ('SUSE16', False),
                                ('XSUSE12', False), ('SUSE12X', False),
                                ('', False), ('SUSE12\n', True)):
            result = run('@match = re.search("^SUSE12$", rel)\n',
                         scope_with_rel(value))
            self.assertEqual(result.scope.local['match'] is not None, expected)

    def test_re_search_errors_are_source_aware_and_types_stay_bounded(self):
        with self.assertRaises(SemanticError) as invalid:
            run('@match = re.search("[", "SUSE12")\n')
        self.assertEqual(invalid.exception.span.start.line, 1)
        self.assertIn('invalid regular expression', str(invalid.exception))
        for expression in ('re.search("^SUSE12$", 12)',
                           're.search("^SUSE12$", b"SUSE12")'):
            with self.assertRaises(SemanticError):
                run('@match = ' + expression + '\n')

    def test_re_search_signature_and_attributes_remain_closed(self):
        for expression in ('re.search("a", "a", 0)',
                           're.search("a", "a", flags=0)',
                           're.match("a", "a")', 're.foo("a", "a")',
                           're.__dict__', 're.search.__class__',
                           're.search("a", "a").group()'):
            with self.assertRaises(SemanticError):
                run('@value = ' + expression + '\n')
        with self.assertRaises(UndefinedName):
            run('@value = re\n')
        shadowed = Scope.top_level()
        shadowed.store('re', 'shadow', program(''))
        with self.assertRaises(Unsupported):
            run('@value = re.search("a", "a")\n', shadowed)

    def test_path_helpers_are_pure_and_cwd_is_explicit(self):
        result = run('@p = os.path.abspath(os.path.curdir)\n'
                     '@name = os.path.basename(p)\n@parent = os.path.dirname(p)\n',
                     helpers=HelperRegistry('/metadata/package'))
        self.assertEqual(result.scope.local['name'], 'package')
        self.assertEqual(result.scope.local['parent'], '/metadata')
        with self.assertRaises(Unsupported):
            run('@p = os.path.abspath(".")\n')

    def test_list_alias_mutation_and_cycle_rejection(self):
        result = run('@xs = ["a"]\n@alias = xs\n@alias.extend(["b"])\n')
        self.assertIs(result.scope.local['xs'], result.scope.local['alias'])
        self.assertEqual(result.scope.local['xs'], ['a', 'b'])
        with self.assertRaises(Unsupported):
            run('@xs = []\n@xs.extend([xs])\n')

    def test_rejects_nodes_before_any_mutation(self):
        for payload in ('{1: 2}', '[x for x in []]', 'lambda: 1',
                        'f"text {1}"', '1.5', '1_000'):
            scope = Scope.top_level()
            with self.assertRaises(Unsupported) as result:
                run('BEFORE = value\n@bad = ' + payload + '\n', scope)
            self.assertEqual(result.exception.span.start.line, 2)
            self.assertEqual(scope.local, {'_prevdir': None})

    def test_rejects_unapproved_calls_and_introspection(self):
        for payload in ('open("output", "w")', '__import__("os")',
                        'eval("1")', 'exec("pass")', 'getattr(_no, "x")',
                        '"x".__class__', '"x".upper()', 'int("1_0")',
                        're.sub(".*", "x", "text")'):
            with self.assertRaises(SemanticError):
                run('@bad = ' + payload + '\n')

    def test_unimplemented_capabilities_are_explicit(self):
        for name in ('file2string', 'redir_system', 'os.rename',
                     'os.path.isdir'):
            with self.assertRaises(Unsupported) as result:
                run('@bad = ' + name + '("ignored")\n')
            self.assertIn('deferred capability', str(result.exception))

    def test_host_objects_and_subclasses_are_not_values(self):
        class HostObject(object):
            def __str__(self):
                raise AssertionError('host method must not run')
        class HostString(str):
            pass
        for value in (HostObject(), HostString('x'), {'key': 'value'}):
            with self.assertRaises(Unsupported):
                Scope().store('value', value, program(''))

    def test_step_limit_stops_self_growing_iteration(self):
        with self.assertRaises(Unsupported) as result:
            run('@xs = [1]\n@for x in xs:\n  @xs.extend([1])\n', max_steps=50)
        self.assertIn('step limit', str(result.exception))

    def test_legacy_ast_constant_adapters(self):
        # Python 3.4 exposes Num/Str/NameConstant instead of Constant.
        for kind, field, value in (('Num', 'n', 3), ('Str', 's', 'text'),
                                    ('NameConstant', 'value', None)):
            node = type(kind, (object,), {field: value})()
            self.assertEqual(constant(node), value)


class InertAndProductionTests(unittest.TestCase):
    def test_command_is_inert_and_stops_at_effect_boundary(self):
        # Backticks in an inert command must not be evaluated or validated.
        ast = program('BEFORE = yes\n:sys `unapproved()`\nAFTER = no\n')
        result = Evaluator().run(ast)
        self.assertEqual(result.scope.local, {'_prevdir': None, 'BEFORE': 'yes'})
        self.assertIs(result.halted_at, ast.statements[1])
        self.assertEqual(result.deferred, [ast.statements[1]])
        self.assertIs(result.program, ast)
        self.assertFalse(result.complete)

    def test_unreached_command_does_not_block_metadata(self):
        result = run('@if False:\n  :include absent.aap\nAFTER = yes\n')
        self.assertTrue(result.complete)
        self.assertEqual(result.scope.local['AFTER'], 'yes')

    def test_command_inside_loop_preserves_current_state_and_whole_program(self):
        result = run('@for x in ["first", "second"]:\n  BEFORE = $x\n'
                     '  :mkdir $x\n  AFTER = no\n')
        self.assertEqual(result.scope.local['BEFORE'], 'first')
        self.assertNotIn('AFTER', result.scope.local)
        self.assertEqual(result.halted_at.name, 'mkdir')

    def test_lower_entire_globals_and_explicitly_enter_recipe_bodies(self):
        ast = lower(parse(Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1')))
        pending = [ast]
        counts = {}
        while pending:
            node = pending.pop()
            kind = type(node).__name__
            counts[kind] = counts.get(kind, 0) + 1
            self.assertTrue(node.source.slice(node.span) or isinstance(node, m.Program))
            if isinstance(node, m.Program):
                pending.extend(node.statements)
            elif isinstance(node, m.Conditional):
                pending.extend(body for condition, body in node.branches)
                if node.otherwise is not None:
                    pending.append(node.otherwise)
            elif isinstance(node, m.Loop):
                pending.append(node.body)
            elif isinstance(node, m.Variant):
                pending.extend(body for value, body in node.branches)
            elif isinstance(node, (m.Command, m.Dependency)):
                if node.body is not None and node.body.flavor == 'recipe':
                    pending.append(lower_body(node.body))
        self.assertEqual(counts['Command'], 199)
        self.assertEqual(counts['Assignment'], 140)
        self.assertEqual(counts['Conditional'], 41)
        self.assertEqual(counts['Loop'], 21)
        self.assertEqual(counts['EmbeddedPython'], 76)
        self.assertEqual(counts['DeferredConstruct'], 4)
        self.assertEqual(counts['Variant'], 5)
        self.assertEqual(counts['Dependency'], 34)

    def test_globals_harmless_prefix_evaluates(self):
        ast = lower(parse(Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1')))
        scope = Scope.top_level()
        scope.store('OSNAME', 'Linux', ast)
        result = Evaluator(scope).run(ast)
        self.assertEqual(scope.local['ARCHIV'], '/export/company/distfiles')
        self.assertEqual(scope.local['PATCH_SITES'], 'file://files')
        self.assertEqual(scope.local['PATCHCMD'], 'patch -f -p 0 <')
        self.assertEqual(result.halted_at.name, 'syseval')
        self.assertEqual(result.halted_at.span.start.line, 45)
        self.assertEqual(scope.local['PREFIX'], '/usr/local')
        self.assertEqual(scope.local['RPMBASE'], '/export/company')
        self.assertNotIn('YUMCLEAN', scope.local)
        self.assertEqual(set(result.declarations.suffixes), set(('tar.Z', 'xz')))
        self.assertEqual([d.input_type for d in result.declarations.actions['extract']],
                         ['tarZ', 'xz', 'targz', 'tarbz2'])
        self.assertFalse(result.complete)

    def test_compatibility_guard_covers_new_modules_and_forbids_dynamic_execution(self):
        self.assertEqual(check_text('import posixpath\nx = posixpath.basename("/x")'), [])
        for text in ('eval("1")', 'exec("pass")', 'compile("1", "x", "eval")',
                     'from ast import Constant', 'import ast\nx = ast.unparse(tree)'):
            self.assertTrue(check_text(text), text)


if __name__ == '__main__':
    unittest.main()
