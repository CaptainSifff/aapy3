"""Pure DoBuild.buildcheck_update characterization for the reached subset."""
import hashlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver, MemoryTargetState,
                           MemoryPersistence, FileState)
from aap_semantics.buildcheck import BuildSignaturePreparer


def prepared(text, name='out', encoding='latin-1', scope=None):
    metadata = Evaluator(scope).run(lower(parse(Source('/recipe/main.aap', text))))
    definition = metadata.graph.dependencies_for(name)[0]
    node = metadata.graph.find_node(name)
    return BuildSignaturePreparer(encoding).prepare(definition, node, metadata.scope)


class BuildcheckTests(unittest.TestCase):
    def test_reached_plain_body_exact_md5_and_deterministic_inspection(self):
        first = prepared('out:\n\t:pass\n')
        second = prepared('out:\n\t:pass\n')
        self.assertEqual(first.status, 'PREPARED')
        self.assertEqual(first.commands, '\t:pass\n')
        self.assertEqual(first.expanded, '\t:pass\n')
        self.assertEqual(first.signature, hashlib.md5(b'\t:pass\n').hexdigest())
        self.assertEqual(first.snapshot(), second.snapshot())

    def test_relevant_variable_and_definition_scope_are_live(self):
        scope = Scope.top_level()
        scope.local['X'] = 'one'
        text = 'out:\n  :buildcheck $X\n'
        one = prepared(text, scope=scope)
        scope.local['X'] = 'two'
        two = prepared(text, scope=scope)
        self.assertEqual(one.expanded, '  :buildcheck one\n')
        self.assertNotEqual(one.signature, two.signature)

    def test_nested_build_caller_scope_value_contributes(self):
        metadata = Evaluator().run(lower(parse(Source('/recipe/main.aap',
                                                 'out:\n  :buildcheck $X\n'))))
        definition = metadata.graph.dependencies_for('out')[0]
        node = metadata.graph.find_node('out')
        caller = Scope.build(metadata.scope, metadata.scope)
        caller.local['X'] = 'one'
        first = BuildSignaturePreparer('latin-1').prepare(definition, node, caller)
        caller.local['X'] = 'two'
        second = BuildSignaturePreparer('latin-1').prepare(definition, node, caller)
        self.assertEqual(first.expanded, '  :buildcheck one\n')
        self.assertNotEqual(first.signature, second.signature)

    def test_target_source_mask_and_cwd_are_not_signed_directly(self):
        first = prepared('out:\n  :buildcheck $target/$source/$fname/$match\n')
        second = prepared('elsewhere:\n  :buildcheck $target/$source/$fname/$match\n',
                          'elsewhere')
        self.assertEqual(first.expanded, '  :buildcheck ///\n')
        self.assertEqual(first.signature, second.signature)

    def test_comments_are_removed_but_command_and_indent_change_sign(self):
        one = prepared('out:\n  # first\n  :pass\n')
        two = prepared('out:\n  # second\n  :pass\n')
        between = prepared('out:\n  :pass\n\n  # ignored\n  :pass\n')
        changed = prepared('out:\n    :pass\n')
        self.assertEqual(one.signature, two.signature)
        self.assertNotEqual(one.signature, changed.signature)
        self.assertEqual(one.commands, '  :pass\n')
        self.assertEqual(one.expanded, '  :pass\n')
        self.assertEqual(between.expanded, '  :pass\n  :pass\n')

    def test_source_control_flow_is_signed_without_execution(self):
        one = prepared('out:\n  @if True:\n    :pass\n  @else:\n    :buildcheck cold\n')
        two = prepared('out:\n  @if True:\n    :pass\n  @else:\n    :buildcheck changed\n')
        self.assertIn(':buildcheck cold', one.expanded)
        self.assertNotEqual(one.signature, two.signature)

    def test_embedded_python_source_contributes_without_execution(self):
        one = prepared('out:\n  @x = 1\n  :pass\n')
        two = prepared('out:\n  @x = 2\n  :pass\n')
        self.assertEqual(one.status, 'PREPARED')
        self.assertNotEqual(one.signature, two.signature)

    def test_invalid_deferred_python_is_source_during_preparation(self):
        # Process.get_commands stores the text; the Python failure is deferred
        # until actual body entry and is not a signature-preparation failure.
        result = prepared('out:\n  @x = (\n  :pass\n')
        self.assertEqual(result.status, 'PREPARED')
        self.assertIn('@x = (', result.canonical)

    def test_non_ascii_uses_explicit_bytes_and_encoding_failure_is_distinct(self):
        one = prepared('out:\n  :buildcheck ä\n')
        self.assertEqual(one.signature, hashlib.md5('  :buildcheck ä\n'.encode('latin-1')).hexdigest())
        failure = prepared('out:\n  :buildcheck ä\n', encoding='ascii')
        self.assertEqual(failure.status, 'FAILED')
        self.assertIsNone(failure.signature)

    def test_action_expansion_unavailable_before_effects(self):
        result = prepared('out:\n  :do compile source\n')
        self.assertEqual(result.status, 'UNAVAILABLE')
        self.assertIn('action_expand_do', result.reason)

    def test_unsupported_modifier_and_deferred_value_are_unavailable(self):
        scope = Scope.top_level()
        scope.local['X'] = 'value'
        result = prepared('out:\n  :buildcheck $-X\n', scope=scope)
        self.assertEqual(result.status, 'UNAVAILABLE')
        self.assertIsNone(result.signature)

    def test_loop_source_contributes_without_iteration(self):
        one = prepared('out:\n  @for x in [1, 2]:\n    :buildcheck $X\n',
                       scope=self._scope_x('one'))
        two = prepared('out:\n  @for x in [1, 2]:\n    :buildcheck $X\n',
                       scope=self._scope_x('two'))
        self.assertIn('@for x in [1, 2]:', one.expanded)
        self.assertNotEqual(one.signature, two.signature)

    def _scope_x(self, value):
        scope = Scope.top_level()
        scope.local['X'] = value
        return scope

    def test_real_signature_comparison_and_pending_finish(self):
        text = 'out:\n  :pass\n'
        metadata = Evaluator().run(lower(parse(Source('/recipe/main.aap', text))))
        node = metadata.graph.find_node('out')
        signature = BuildSignaturePreparer('latin-1').prepare(node.definitions[0], node,
                                                              metadata.scope).signature
        for old, expected in (('', 'COMPLETE'), (signature, 'COMPLETE'),
                              ('changed', 'COMPLETE')):
            state, saved = MemoryTargetState(), MemoryPersistence()
            state.files[node.path] = FileState(True, 100, False)
            if old:
                saved.signatures[node.identity] = {('', 'buildcheck'): old}
            driver = BuildDriver(metadata.graph, state, saved, metadata.scope,
                                 metadata.declarations, buildcheck_encoding='latin-1')
            result = driver.build('out')
            self.assertEqual(result.status, expected)
            if old == signature:
                self.assertEqual(result.bodies, [])
                self.assertEqual(saved.writes, [])
            else:
                self.assertEqual(len(result.bodies), 1)
                self.assertEqual(saved.writes, [])
                self.assertEqual(result.pending_signatures[0].values[('', 'buildcheck')], signature)
                self.assertEqual(driver.finish().status, 'COMPLETE')
                self.assertEqual(saved.signature(node.identity, '', 'buildcheck'), signature)

    def test_invalid_saved_signature_and_unavailable_backend_are_distinct(self):
        metadata = Evaluator().run(lower(parse(Source('/recipe/main.aap',
                                                 'out:\n  :pass\n'))))
        node = metadata.graph.find_node('out')
        state = MemoryTargetState()
        state.files[node.path] = FileState(True, 100, False)
        saved = MemoryPersistence()
        saved.signatures[node.identity] = {('', 'buildcheck'): 42}
        driver = BuildDriver(metadata.graph, state, saved, metadata.scope,
                             metadata.declarations, buildcheck_encoding='latin-1')
        result = driver.build('out')
        self.assertEqual((result.status, result.reason), ('FAILED', 'stored_signature_invalid'))
        self.assertEqual(result.bodies, [])
        saved.signatures[node.identity] = {('', 'buildcheck'): None}
        driver = BuildDriver(metadata.graph, state, saved, metadata.scope,
                             metadata.declarations, buildcheck_encoding='latin-1')
        result = driver.build('out')
        self.assertEqual((result.status, result.reason), ('BLOCKED', 'buildcheck_unavailable'))
        self.assertEqual(result.bodies, [])

    def test_preparation_never_runs_body_effects(self):
        # The :sys text is signed before runtime dispatch; no launcher exists.
        value = prepared('out:\n  :sys forbidden\n')
        self.assertEqual(value.status, 'PREPARED')
        self.assertIn(':sys forbidden', value.canonical)

    def test_driver_keeps_unavailable_and_failed_preparation_separate(self):
        for body, encoding, status, reason in (
                ('  :do compile source\n', 'latin-1', 'BLOCKED', 'buildcheck_unavailable'),
                ('  :buildcheck ä\n', 'ascii', 'FAILED', 'buildcheck_failed')):
            metadata = Evaluator().run(lower(parse(Source('/recipe/main.aap',
                                                     'out:\n' + body))))
            state = MemoryTargetState()
            node = metadata.graph.find_node('out')
            state.files[node.path] = FileState(True, 100, False)
            saved = MemoryPersistence()
            driver = BuildDriver(metadata.graph, state, saved, metadata.scope,
                                 metadata.declarations, buildcheck_encoding=encoding)
            result = driver.build('out')
            self.assertEqual((result.status, result.reason), (status, reason))
            self.assertEqual(result.bodies, [])
            self.assertEqual(saved.writes, [])

    def test_newer_timestamp_still_updates_with_identical_body_signature(self):
        scope = Scope.top_level()
        scope.local['DEFAULTCHECK'] = 'newer'
        metadata = Evaluator(scope).run(lower(parse(Source('/recipe/main.aap',
                                                     'out: in\n  :pass\n'))))
        node = metadata.graph.find_node('out')
        source = metadata.graph.find_node('in')
        signature = BuildSignaturePreparer('latin-1').prepare(node.definitions[0],
                                                              node, scope).signature
        state, saved = MemoryTargetState(), MemoryPersistence()
        state.files[node.path] = FileState(True, 100, False)
        state.files[source.path] = FileState(True, 200, False)
        saved.signatures[node.identity] = {('', 'buildcheck'): signature}
        driver = BuildDriver(metadata.graph, state, saved, scope,
                             metadata.declarations, buildcheck_encoding='latin-1')
        result = driver.build('out')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual([body.target.name for body in result.bodies], ['out'])
        self.assertEqual(result.bodies[0].context.prepared_buildcheck, signature)


if __name__ == '__main__':
    unittest.main()
