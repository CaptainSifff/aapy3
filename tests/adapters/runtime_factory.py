"""Compose real host capabilities for one external A-A-P invocation."""
from __future__ import print_function

import os

from aap_semantics import (ActionRuntime, CatRuntime, OutputPolicy,
    PortCommandPolicy, PortCommandRuntime, PortRuntime, PrintRuntime,
    ProcessPolicy, RuntimeCapabilities, Scope, SourceLoader, WorkIdentity)
from fetch_adapter import LocalFetchBackend
from host_filesystem import (Copy, Delete, Directories, Files, HostState,
                             Markers, Move, Paths, Trees, Workspace, Writer)
from host_persistence import DiskPersistence
from integration_evidence import TracingChecksum
from output_adapter import StdoutOutputSink
from process_adapter import PosixProcessBackend
from process_tracing import TracingProcessBackend


class RuntimeAssembly(object):
    __slots__ = ('scope', 'capabilities', 'metadata_capabilities',
                 'state', 'persistence')

    def __init__(self, scope, capabilities, metadata_capabilities,
                 state, persistence):
        self.scope, self.capabilities = scope, capabilities
        self.metadata_capabilities = metadata_capabilities
        self.state, self.persistence = state, persistence


def initial_scope(cwd, recipe_aap):
    scope = Scope.top_level(port_defaults=True, work=WorkIdentity('main.aap'))
    scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build',
                        'DISTDIR': 'distfiles', 'PATCHDISTDIR': 'patches',
                        'PKGDIR': os.path.join(cwd, 'pack'), 'WRKDIR': 'work',
                        'AAP': recipe_aap, 'gt': '>'})
    return scope


def create_runtime(cwd, environment, record, scope):
    policy = ProcessPolicy('latin-1', sys_mode='bounded-attributes',
                           log_path=os.path.join(cwd, 'AAPDIR', 'log'))
    output_policy = OutputPolicy('latin-1', logging=False)
    shell = TracingProcessBackend(PosixProcessBackend(), record, environment,
                                  enabled=bool(environment.get('AAP_RECURSIVE_TRACE')))
    files, directories = Files(record), Directories()
    paths, markers = Paths(), Markers(record)
    commands = PortCommandRuntime(directories, PortCommandPolicy(False, False))
    runtime = PortRuntime(files, markers, ActionRuntime(Workspace()), commands,
                          deletions=Delete(), fetch_backend=LocalFetchBackend(record))
    loader = SourceLoader('latin-1')
    writer = Writer(record)
    output = PrintRuntime(output_policy, sink=StdoutOutputSink(), writer=writer)
    cat = CatRuntime(output_policy, files, writer)
    capabilities = RuntimeCapabilities(
        process_backend=shell, process_policy=policy, include_loader=loader,
        checksum_backend=TracingChecksum(files, record), port_runtime=runtime,
        path_observer=paths, output_runtime=output, cat_runtime=cat,
        tree_filesystem=Trees(), move_backend=Move(), copy_backend=Copy(record))
    # Top-level metadata has no ordinary print, checksum, or port helper
    # capability in the current CLI baseline. Preserve those phase gates.
    metadata_capabilities = capabilities.replace(
        checksum_backend=None, port_runtime=None, output_runtime=None)
    return RuntimeAssembly(scope, capabilities, metadata_capabilities,
                           HostState(), DiskPersistence(cwd, record))
