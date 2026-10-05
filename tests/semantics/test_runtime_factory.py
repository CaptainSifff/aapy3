"""The real CLI assembles explicit, phase-appropriate host capabilities."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from host_filesystem import Files, HostState
from host_persistence import DiskPersistence
from output_adapter import StdoutOutputSink
from process_adapter import PosixProcessBackend
from process_tracing import TracingProcessBackend
from runtime_factory import create_runtime, initial_scope


class RuntimeFactoryTests(unittest.TestCase):
    def test_explicit_host_wiring_and_metadata_phase_gates(self):
        with tempfile.TemporaryDirectory() as cwd:
            events = []
            def record(kind, **fields):
                events.append((kind, fields))
            environment = {'AAP': 'aap', 'AAP_RECURSIVE_TRACE': ''}
            scope = initial_scope(cwd, environment['AAP'])
            runtime = create_runtime(cwd, environment, record, scope)
            caps = runtime.capabilities
            self.assertIs(runtime.scope, scope)
            self.assertEqual(scope.local['PKGDIR'], os.path.join(cwd, 'pack'))
            self.assertIsInstance(caps.process_backend, TracingProcessBackend)
            self.assertIsInstance(caps.process_backend.inner, PosixProcessBackend)
            self.assertIsInstance(caps.port_runtime.artifacts, Files)
            self.assertIs(caps.checksum_backend.artifacts,
                          caps.port_runtime.artifacts)
            self.assertIsInstance(caps.output_runtime.sink, StdoutOutputSink)
            self.assertIsNotNone(caps.port_runtime.fetch_backend)
            self.assertIsInstance(runtime.state, HostState)
            self.assertIsInstance(runtime.persistence, DiskPersistence)
            self.assertIs(runtime.metadata_capabilities.process_backend,
                          caps.process_backend)
            self.assertIsNone(runtime.metadata_capabilities.output_runtime)
            self.assertIsNone(runtime.metadata_capabilities.port_runtime)
            self.assertEqual(events[0][0], 'persistence_load')


if __name__ == '__main__':
    unittest.main()
