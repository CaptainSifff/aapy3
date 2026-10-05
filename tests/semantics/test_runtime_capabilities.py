"""Capability identity and phase boundaries across semantic re-entry."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, Evaluator, MemoryOutputSink,
                           MemoryPersistence, MemoryTargetState, OutputPolicy,
                           PrintRuntime, RuntimeCapabilities, Scope, lower)


class RuntimeCapabilitiesTests(unittest.TestCase):
    def test_explicit_absence_and_copy_without_mutation(self):
        original = RuntimeCapabilities()
        self.assertIsNone(original.output_runtime)
        output = PrintRuntime(OutputPolicy('latin-1', False), sink=MemoryOutputSink())
        changed = original.replace(output_runtime=output)
        self.assertIsNone(original.output_runtime)
        self.assertIs(changed.output_runtime, output)
        with self.assertRaises(AttributeError):
            changed.output_runtime = None
        with self.assertRaises(TypeError):
            original.replace(unknown=output)

    def test_metadata_and_body_receive_the_selected_output_capability(self):
        output = PrintRuntime(OutputPolicy('latin-1', False), sink=MemoryOutputSink())
        caps = RuntimeCapabilities(output_runtime=output)
        scope = Scope.top_level()
        source = Source('/recipe/main.aap', ':print metadata\nall:\n  :print body\n')
        metadata = Evaluator(scope, capabilities=caps).run(lower(parse(source)))
        self.assertTrue(metadata.complete)
        driver = BuildDriver(metadata.graph, MemoryTargetState(),
            MemoryPersistence(), scope, metadata.declarations,
            port_defaults=False, capabilities=caps)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertIs(result.bodies[0].context.capabilities, caps)
        self.assertEqual([event.data for event in output.sink.events],
                         [b'metadata\n', b'body\n'])

    def test_absent_metadata_output_retains_existing_block(self):
        source = Source('/recipe/main.aap', ':print metadata\n')
        result = Evaluator(Scope.top_level(),
                           capabilities=RuntimeCapabilities()).run(
                               lower(parse(source)))
        self.assertFalse(result.complete)
        self.assertEqual(result.halted_at.name, 'print')

    def test_action_reentry_retains_approved_output_adapter(self):
        from test_actions import ExtractTests
        driver, runtime, workspace, saved, definition = ExtractTests().make(
            action=':print action')
        output = PrintRuntime(OutputPolicy('latin-1', False),
                              sink=MemoryOutputSink())
        driver.capabilities = driver.capabilities.replace(output_runtime=output)
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE')
        action = result.bodies[0].port_operations[0].actions[0]
        self.assertEqual(action.evaluation.prints[0].request.data, b'action\n')
        self.assertIs(output.sink.events[0], action.evaluation.prints[0].request)


if __name__ == '__main__':
    unittest.main()
