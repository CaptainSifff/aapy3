"""Registration only: Commands.aap_depend, Work.add_dependency, Node attributes.

Small source-derived examples; rectest/test001.py and test004.py motivate
zero/one/many prerequisites and directory identity. No legacy driver runs.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse, cst
from aap_semantics import lower, Evaluator, BuildGraph, Scope, SemanticError, Unsupported
from aap_semantics import ProcessBackend, ProcessPolicy
from aap_semantics import model as m


def program(text, name='/recipe/main.aap'):
    return lower(parse(Source(name, text)))


def run(text, graph=None, scope=None, name='/recipe/main.aap'):
    return Evaluator(scope, graph=graph).run(program(text, name))


def names(items):
    return tuple(item.name for item in items)


class GraphTests(unittest.TestCase):
    def test_one_target_zero_sources(self):
        result = run('local-rpm:\n  :sys never\n')
        graph = result.graph
        definition = graph.dependencies_for('local-rpm')[0]
        self.assertTrue(result.complete)
        self.assertEqual(names(graph.targets), ('local-rpm',))
        self.assertEqual(definition.prerequisites, ())
        self.assertFalse(graph.targets[0].virtual)
        self.assertEqual(graph.targets[0].identity, '/recipe/local-rpm')

    def test_one_target_one_and_many_sources(self):
        for text, expected in (('fake-install: build\n', ('build',)),
                               ('rpm-install: rpm rpminst\n', ('rpm', 'rpminst'))):
            result = run(text)
            definition = result.graph.definitions[0]
            self.assertEqual(names(definition.source_items), expected)
            self.assertEqual(names(definition.prerequisites), expected)
            self.assertIs(definition.targets[0], result.graph.targets[0])
            self.assertIsNone(definition.body)

    def test_multiple_targets_share_one_definition_body_and_prerequisites(self):
        result = run('one two: input\n  @invalid +\n')
        graph = result.graph
        definition = graph.definitions[0]
        self.assertEqual(len(graph.definitions), 1)
        self.assertEqual(names(definition.targets), ('one', 'two'))
        self.assertIs(graph.dependencies_for('one')[0], graph.dependencies_for('two')[0])
        self.assertEqual(names(definition.prerequisites), ('input',))
        self.assertFalse(definition.body.origin.scanned)

    def test_expansion_multi_item_variables_and_quoted_names(self):
        result = run('TARGETS = one "two words"\nSOURCES = alpha beta\n'
                     '$TARGETS: $SOURCES "gamma delta"\n')
        definition = result.graph.definitions[0]
        self.assertEqual(names(definition.target_items), ('one', 'two words'))
        self.assertEqual(names(definition.source_items), ('alpha', 'beta', 'gamma delta'))

    def test_empty_expanded_sources_and_targets(self):
        result = run('EMPTY =\nreal: $EMPTY\n$EMPTY: source\n')
        first, second = result.graph.definitions
        self.assertEqual(first.source_items, ())
        self.assertEqual(second.target_items, ())
        self.assertEqual(names(second.source_items), ('source',))
        self.assertEqual(names(result.graph.targets), ('real',))

    def test_literal_header_and_body_spans_survive_lowering(self):
        text = '# comment\nresult: source\n  :sys never\nAFTER = yes\n'
        ast = program(text)
        node = ast.statements[0]
        self.assertIsInstance(node, m.Dependency)
        self.assertIsInstance(node.origin, cst.DependencyLikeStatement)
        self.assertEqual(node.source.slice(node.target_value.span), 'result')
        result = Evaluator().run(ast)
        definition = result.graph.definitions[0]
        self.assertEqual(definition.span, node.span)
        self.assertIs(definition.body, node.body)
        self.assertEqual(definition.body.source.slice(definition.body.span), '  :sys never\n')
        self.assertEqual(definition.body.span.start.line, 3)
        self.assertEqual(result.scope.local['AFTER'], 'yes')

    def test_live_definition_scope_and_directory(self):
        scope = Scope.top_level()
        result = run('VALUE = before\nout: in\n  LOCAL = never\nVALUE = after\n', scope=scope)
        definition = result.graph.definitions[0]
        self.assertIs(definition.scope, scope)
        self.assertEqual(definition.scope.local['VALUE'], 'after')
        self.assertNotIn('LOCAL', scope.local)
        self.assertEqual(definition.cwd, '/recipe')

    def test_repeated_no_body_declarations_retain_all_lists_and_duplicates(self):
        graph = run('target: a\ntarget: b a\ntarget:\n').graph
        history = graph.definition_history('target')
        self.assertEqual(len(history), 3)
        self.assertEqual([names(d.source_items) for d in history], [('a',), ('b', 'a'), ()])
        self.assertEqual([d.index for d in history], [0, 1, 2])
        self.assertEqual(graph.dependencies_for('target'), history)

    def test_single_body_can_precede_or_follow_additional_prerequisites(self):
        graph = run('target: a\ntarget: b\n  :sys never\ntarget: c\n').graph
        self.assertEqual([names(d.source_items) for d in graph.dependencies_for('target')],
                         [('a',), ('b',), ('c',)])
        self.assertEqual(len(graph.find_node('target').body_definitions), 1)
        self.assertIs(graph.find_node('target').body_definitions[0], graph.definitions[1])

    def test_conflicting_body_rejects_without_replacement(self):
        graph = BuildGraph()
        with self.assertRaises(SemanticError) as error:
            run('target: a\n  :sys one\ntarget: b\n  :sys two\n', graph=graph)
        self.assertIn('/recipe/main.aap:3:1: multiple build bodies', str(error.exception))
        self.assertEqual(len(graph.definitions), 1)
        self.assertEqual(names(graph.definitions[0].source_items), ('a',))
        self.assertIn(':sys one', graph.definitions[0].body.origin.text)
        self.assertIsNone(graph.find_node('b'))

    def test_explicit_virtual_does_not_permit_multiple_bodies(self):
        with self.assertRaises(SemanticError):
            run('target {virtual}: a\n  :pass\ntarget: b\n  :pass\n')

    def test_standard_target_permits_ordered_multiple_bodies(self):
        graph = run('build: a\n  :sys first\nbuild: b\n  :sys second\n').graph
        node = graph.find_node('build')
        self.assertTrue(node.virtual)
        self.assertEqual(len(node.body_definitions), 2)
        self.assertEqual([names(d.source_items) for d in node.body_definitions], [('a',), ('b',)])
        self.assertIn('first', node.body_definitions[0].body.origin.text)
        self.assertIn('second', node.body_definitions[1].body.origin.text)

    def test_explicit_source_virtual_attaches_to_preceding_item_and_node(self):
        graph = run('rpm: test fake-install {virtual} prep-rpm {virtual} plain\n'
                    'fake-install:\n').graph
        items = graph.definitions[0].source_items
        self.assertEqual([i.attributes for i in items], [{}, {'virtual': 1}, {'virtual': 1}, {}])
        self.assertTrue(graph.find_node('fake-install').virtual)
        self.assertTrue(graph.find_node('prep-rpm').virtual)
        self.assertFalse(graph.find_node('plain').virtual)
        self.assertFalse(graph.find_node('rpm').virtual)
        self.assertTrue(graph.find_node('test').virtual)  # Exact historical standard name.
        self.assertEqual(graph.definitions[1].target_items[0].attributes, {})

    def test_build_attributes_are_separate_and_not_expanded_or_applied_to_nodes(self):
        graph = run('target: {virtual=$MISSING} source\n').graph
        definition = graph.definitions[0]
        self.assertEqual(definition.build_attributes, {'virtual': '$MISSING'})
        self.assertFalse(definition.targets[0].virtual)
        self.assertFalse(definition.prerequisites[0].virtual)
        self.assertEqual(definition.source_items[0].attributes, {})

    def test_virtual_truthiness_and_sticky_status(self):
        graph = run('one {virtual=}:\ntwo {virtual=0}:\n'
                    'three {virtual}:\nthree {virtual=}:\n').graph
        self.assertFalse(graph.find_node('one').virtual)
        self.assertTrue(graph.find_node('two').virtual)
        self.assertTrue(graph.find_node('three').virtual)
        self.assertEqual(graph.find_node('three').attributes['virtual'], 1)

    def test_attributes_survive_expansion_and_apply_to_last_expanded_item(self):
        result = run('NAMES = first second\nVALUE = 0\n'
                     'target: $NAMES {virtual=$VALUE}\n')
        items = result.graph.definitions[0].source_items
        self.assertEqual([i.attributes for i in items], [{}, {'virtual': '0'}])
        self.assertFalse(items[0].node.virtual)
        self.assertTrue(items[1].node.virtual)

    def test_attributes_inside_expanded_values_survive(self):
        result = run('NAMES = first {virtual} second\ntarget: $NAMES\n')
        self.assertTrue(result.graph.find_node('first').virtual)
        self.assertFalse(result.graph.find_node('second').virtual)

    def test_name_shape_is_not_virtual(self):
        graph = run('fake-install: do-build\nlocal-rpm: do-checksum\n').graph
        self.assertTrue(all(not node.virtual for node in graph.nodes))

    def test_normalized_paths_same_directory_and_cross_directory_identity(self):
        graph = BuildGraph()
        run('out: in\n./out: ./in\n', graph=graph)
        run('out: in\n', graph=graph, name='/other/main.aap')
        self.assertEqual(len(graph.dependencies_for('out', '/recipe')), 2)
        self.assertEqual(len(graph.dependencies_for('out', '/other')), 1)
        self.assertIs(graph.definitions[0].prerequisites[0], graph.definitions[1].prerequisites[0])
        self.assertIsNot(graph.definitions[0].targets[0], graph.definitions[2].targets[0])

    def test_virtual_node_name_is_shared_across_directories(self):
        graph = BuildGraph()
        run('target {virtual}:\nbuild:\n', graph=graph)
        run('target:\nbuild:\n', graph=graph, name='/other/main.aap')
        self.assertEqual(len(graph.targets), 2)
        self.assertEqual(len(graph.dependencies_for('target', '/other')), 2)

    def test_include_registration_shares_graph_but_preserves_source_identity(self):
        class Loader(object):
            def load(self, path):
                return Source(path, 'target: child\n  @invalid +\n')
        result = Evaluator(include_loader=Loader()).run(program('target: root\n:include sub/inc.aap\n'))
        definitions = result.graph.dependencies_for('target')
        self.assertEqual([d.span.source_id for d in definitions],
                         ['/recipe/main.aap', '/recipe/sub/inc.aap'])
        self.assertEqual([d.cwd for d in definitions], ['/recipe', '/recipe'])
        self.assertIs(definitions[0].scope, definitions[1].scope)

    def test_header_continuation_belongs_to_sources_body_stays_opaque(self):
        graph = run('target: first\n    second\n  :sys never\n').graph
        self.assertEqual(names(graph.definitions[0].source_items), ('first', 'second'))
        self.assertEqual(graph.definitions[0].body.origin.text, '  :sys never\n')

    def test_no_filesystem_or_process_capability_is_used(self):
        class Forbidden(ProcessBackend):
            def run(self, request):
                raise AssertionError('dependency body was executed')
        class NoReads(object):
            def load(self, path):
                raise AssertionError('dependency include was executed')
        ast = program('out: /nonexistent/source\n  :syseval forbidden\n'
                      '  :include forbidden.aap\n  @broken +\nAFTER = yes\n')
        result = Evaluator(process_backend=Forbidden(), process_policy=ProcessPolicy('utf-8'),
                           include_loader=NoReads()).run(ast)
        self.assertTrue(result.complete)
        self.assertEqual(result.scope.local, {'_prevdir': None, 'AFTER': 'yes'})
        self.assertFalse(result.graph.definitions[0].body.origin.scanned)

    def test_python_colons_and_opaque_blocks_are_not_dependency_nodes(self):
        ast = program('@if 1 != 2:\n  @for item in [1]:\n    @pass\n'
                      ':python\n  if value == 0:\n    for f in functions:\n      pass\n')
        self.assertIsInstance(ast.statements[0], m.Conditional)
        self.assertIsInstance(ast.statements[1], m.DeferredConstruct)
        result = Evaluator().run(ast)
        self.assertEqual(result.graph.definitions, [])
        self.assertIs(result.halted_at, ast.statements[1])

    def test_unselected_dependency_not_registered_or_validated(self):
        result = run('@if False:\n  target: $MISSING {unknown}\n    @bad +\nreal:\n')
        self.assertEqual(names(result.graph.targets), ('real',))

    def test_unsupported_item_features_and_attributes_stop_before_registration(self):
        for text in ('target: *.c\n', '%.o: %.c\n', 'target: ~/file\n',
                     'target: https://example.test/file\n', 'target {unknown}:\n',
                     'target: {scope=custom} source\n', 'target: $*NAMES\n',
                     'target: `unapproved()`\n', 'target: $(NAMES[0])\n'):
            graph = BuildGraph()
            with self.assertRaises(Unsupported):
                run(text, graph=graph)
            self.assertEqual(graph.definitions, [])

    def test_malformed_items_have_source_diagnostics(self):
        for text in ('target: source {virtual\n', 'target: "missing\n'):
            with self.assertRaises(SemanticError) as error:
                run(text)
            self.assertIn('/recipe/main.aap:1:', str(error.exception))

    def test_snapshot_is_stable_and_declaration_ordered(self):
        text = 'build: input\n  :sys never\nother: build\n'
        expected = ((0, ('build',), ('/recipe/input',), '/recipe/main.aap', 1, True),
                    (1, ('/recipe/other',), ('build',), '/recipe/main.aap', 3, False))
        self.assertEqual(run(text).graph.snapshot(), expected)
        self.assertEqual(run(text).graph.snapshot(), expected)

    def test_registration_continues_until_next_real_capability_barrier(self):
        result = run('target: source\n  :sys deferred\nVALUE = yes\n:tree directory\n'
                     '  :print never\nAFTER = no\n')
        self.assertEqual(len(result.graph.definitions), 1)
        self.assertEqual(result.scope.local, {'_prevdir': None, 'VALUE': 'yes'})
        self.assertEqual(result.halted_at.name, 'tree')

    def test_pattern_rule_command_remains_a_separate_barrier(self):
        result = run('ordinary: input\n:rule %.o: %.c\n  :sys never\nAFTER = no\n')
        self.assertEqual(len(result.graph.definitions), 1)
        self.assertEqual(result.halted_at.name, 'rule')
        self.assertNotIn('AFTER', result.scope.local)


if __name__ == '__main__':
    unittest.main()
