"""Reproduce the controlled Nano target frontier using authorized sources only.

The existing semantic test harness loads ports/globals.aap and ports/editors/nano/main.aap.
All processes, artifact reads, path probes, output writes, markers and saved
signatures remain injected/in-memory. No production program is launched.
"""
import io
import json
import os
import sys
import binascii


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'semantics'))

from test_port_runtime import NanoPortIntegration
from aap_semantics import (MemoryPathObserver, PathObservation,
                           MemoryPortDirectories, MemoryTextWriter,
                           MemoryArtifacts, OutputPolicy, PrintRuntime,
                           CatRuntime, MemoryMarkers, MemoryDeleteBackend)
from aap_semantics import WorkIdentity, MemoryRecipeMutationBackend
from aap_semantics import MemoryTreeFilesystem, TreeObservation
from aap_semantics import MemoryMoveBackend


BASE = '/authorized/ports/editors/nano/'
PATH_FACTS = {'pack': 'EXISTS', 'files/post_i': 'EXISTS',
              'work/post_i': 'EXISTS', 'files/unpost_i': 'MISSING',
              'files/pre_i': 'MISSING', 'work/unpost_i': 'MISSING',
              'work/pre_i': 'MISSING'}


def fixture(changes=None, post_source=True, post_script=True,
            existing_markers=None, tree_directories=None, tree_kinds=None):
    facts = dict(PATH_FACTS)
    facts.update(changes or {})
    paths = MemoryPathObserver(dict((BASE + name, PathObservation(status))
                                    for name, status in facts.items()))
    harness = NanoPortIntegration()
    markers = MemoryMarkers(dict((BASE + 'done/' + name, b'')
                                 for name in (existing_markers or ())))
    cleanup_entries = {BASE + 'work': 'directory', BASE + 'distfiles': 'directory'}
    if facts['pack'] == 'EXISTS':
        cleanup_entries[BASE + 'pack'] = 'directory'
    cleanup = MemoryDeleteBackend(
        entries=cleanup_entries,
        missing=[BASE + name for name in ('pkg-plist', 'pkg-comment',
                 'pkg-descr', 'patches', 'nano-7.1.tgz', 'AAPDIR')],
        markers=markers, marker_root=BASE + 'done', fallback=paths)
    driver, runtime, saved, process = harness.setup_nano(
        markers=markers,
        actions=harness.extraction(), enable_sys=True,
        directories=MemoryPortDirectories([BASE + 'work/nano-7.1']),
        path_observer=cleanup, deletions=cleanup)
    cleanup.stores.append(runtime.artifacts.files)
    # Work.set_defaults supplies gt before reading recipes. The controlled
    # harness now supplies this previously missing intrinsic explicitly.
    driver.scope.local['gt'] = '>'
    # Explicit Work entry identity and writable bytes; a source ID alone grants
    # no recipe mutation capability. Only the supplied Nano source is loaded.
    driver.scope.work = WorkIdentity('main.aap')
    with io.open(os.path.join(ROOT, 'tests/fixtures/ports/editors/nano/main.aap'), 'rb') as stream:
        runtime.artifacts.files[BASE + 'main.aap'] = stream.read()
    mutations = MemoryRecipeMutationBackend(
        [BASE + 'main.aap'], runtime.artifacts.files, fallback=cleanup)
    runtime.recipe_mutations = mutations
    driver.capabilities = driver.capabilities.replace(path_observer=mutations)
    # A fake sed result is not inferred from process status. The exact input
    # for the reached :move is independently supplied to the mutation store.
    runtime.artifacts.files[BASE + 'pack/.packlist'] = b'controlled old packlist\n'
    runtime.artifacts.files[BASE + 'pack/.packlist.new'] = b'controlled new packlist\n'
    runtime.artifacts.files[BASE + 'pack/perllocal.pod'] = b'=head2 L<controlled>\n=back\n'
    driver.capabilities = driver.capabilities.replace(
        move_backend=MemoryMoveBackend(runtime.artifacts.files))
    files = {}
    if post_script:
        files[BASE + 'work/post_i'] = b'#!/bin/sh\n'
    files[BASE + 'work/unpost_i'] = b'#!/bin/sh\n'
    files[BASE + 'pack/perllocal.pod'] = b'=head2 L<controlled>\n=back\n'
    if post_source:
        files[BASE + 'files/post_i'] = b'# controlled post\n\x00\xfftail'
    writer = MemoryTextWriter([BASE + 'work'], files)
    cleanup.stores.append(writer.files)
    reader = MemoryArtifacts(writer.files, shared=True)
    policy = OutputPolicy('latin-1', False)
    driver.capabilities = driver.capabilities.replace(
        output_runtime=PrintRuntime(policy, writer=writer),
        cat_runtime=CatRuntime(policy, reader, writer))
    if tree_directories is None:
        tree_directories = {BASE + 'pack': TreeObservation(
            'ENTRIES', ('.packlist', 'perllocal.pod'))}
    if tree_kinds is None:
        tree_kinds = {BASE + 'pack/.packlist': TreeObservation('FILE'),
                      BASE + 'pack/perllocal.pod': TreeObservation('FILE')}
    driver.capabilities = driver.capabilities.replace(
        tree_filesystem=MemoryTreeFilesystem(tree_directories, tree_kinds))
    return driver, writer, reader, saved, process


def location(span):
    if span is None:
        return None
    return {'source': span.source_id, 'line': span.start.line,
            'column': span.start.column}


def observation(result, driver, writer, saved, process):
    last = result.bodies[-1] if result.bodies else None
    preparations = []
    seen = set()
    for value in driver.observations.preparations:
        key = (value.definition.index, value.target.identity, value.status,
               value.signature, value.reason)
        if key in seen:
            continue
        seen.add(key)
        preparations.append({'definition': value.definition.index,
                             'target': value.target.name, 'status': value.status,
                             'reason': value.reason, 'signature': value.signature,
                             'canonical': value.canonical})
    summary = {'status': result.status, 'reason': result.reason,
            'location': location(result.span),
            'error': str(result.error) if result.error is not None else None,
            'blocked_command': getattr(result.blocked_at, 'name', None),
            'bodies': [value.target.name for value in result.bodies],
            'last_body': ({'target': last.target.name, 'status': last.status,
                           'reason': last.reason,
                           'location': location(last.span),
                           'blocked_command': getattr(last.blocked_at, 'name', None)}
                          if last is not None else None),
            'process_requests': len(process.requests),
            'output_requests': len(writer.requests),
            'post_i_hex': (binascii.hexlify(writer.files[BASE + 'work/post_i']).decode('ascii')
                           if BASE + 'work/post_i' in writer.files else None),
            'pending_signatures': len(result.pending_signatures),
            'persistence_writes': len(saved.writes),
            'preparations': preparations}
    cleanup = driver.port_runtime.deletions
    if cleanup.requests:
        summary['deletion_requests'] = [request.path for request in cleanup.requests]
        summary['deleted_paths'] = sorted(cleanup.deleted)
    mutations = driver.port_runtime.recipe_mutations
    if mutations.requests:
        summary['recipe_mutations'] = [
            {'operation': request.operation, 'path': request.path,
             'destination': request.destination}
            for request in mutations.requests
            if request.operation not in ('readline', 'write')]
        for key, name in (('recipe_hex', 'main.aap'), ('recipe_backup_hex', 'main.aap~')):
            data = mutations.files.get(BASE + name)
            summary[key] = binascii.hexlify(data).decode('ascii') if data is not None else None
        summary['generated_checksums'] = [
            {'path': request.path, 'md5': digest}
            for body in result.bodies for operation in body.port_operations
            for request, digest in operation.checksums]
    python_writes = [record for body in result.bodies
                     if body.evaluation is not None
                     for record in body.evaluation.python_writes]
    if python_writes:
        summary['python_writes'] = [
            {'operation': record.operation, 'path': record.request.path,
             'status': record.status,
             'data_hex': (binascii.hexlify(record.data).decode('ascii')
                          if record.data is not None else None)}
            for record in python_writes]
    tree_records = [record for body in result.bodies for record in body.tree_records]
    if tree_records:
        summary['tree_observations'] = [
            {'operation': record.request.operation,
             'path': record.request.path,
             'status': record.observation.status,
             'matched': record.matched}
            for record in tree_records]
    moves = [record for body in result.bodies for record in body.moves]
    if moves:
        summary['moves'] = [
            {'source': record.request.source, 'destination': record.request.destination,
             'status': record.status}
            for record in moves]
    return summary


def inventory(driver):
    return [{'name': node.name, 'identity': node.identity,
             'virtual': node.virtual,
             'definitions': [{'index': definition.index,
                              'location': location(definition.span),
                              'cwd': definition.cwd,
                              'body': definition.body is not None,
                              'sources': [item.name for item in definition.source_items]}
                             for definition in node.definitions]}
            for node in driver.graph.nodes]


def run_target(name, after_default=False, changes=None,
               post_source=True, post_script=True, postscript_value=None,
               aap_value=None, existing_markers=None):
    probe_changes = dict(changes or {})
    if name == 'doperlmod':
        probe_changes['work/unpost_i'] = 'EXISTS'
    driver, writer, reader, saved, process = fixture(probe_changes, post_source,
                                                     post_script, existing_markers)
    if after_default:
        initial = driver.build()
        if initial.status != 'COMPLETE':
            raise AssertionError('controlled default path changed: ' + initial.reason)
    if postscript_value is not None:
        driver.scope.namespaces['s_global'].scope.local['PostScript'] = postscript_value
    if aap_value is not None:
        driver.scope.local['AAP'] = aap_value
    if name in ('shellheader', 'shellfooter'):
        # Direct helper invocations need the same explicit arguments that
        # their $AAP caller normally supplies. No fake process creates bytes.
        driver.scope.local['script'] = 'work/post_i'
        if name == 'shellfooter':
            driver.scope.local['functions'] = 'alpha beta'
    result = driver.build(name)
    return observation(result, driver, writer, saved, process)


def report():
    driver, writer, reader, saved, process = fixture()
    initial = driver.build('fetch')  # registers PortDefaults; no host effect
    if initial.status != 'COMPLETE':
        raise AssertionError('controlled fetch initialization changed')
    nodes = inventory(driver)
    targets = []
    for node in nodes:
        target = dict(node)
        target['fresh'] = run_target(node['name'])
        target['after_default'] = run_target(node['name'], True)
        targets.append(target)
    branches = {
        'rpm_pack_missing': run_target('rpm', changes={'pack': 'MISSING'}),
        'rpm_no_optional_post': run_target('rpm', changes={
            'files/post_i': 'MISSING', 'work/post_i': 'MISSING'},
            post_source=False, post_script=False),
        'rpm_post_source_missing_header_without_aap': run_target(
            'rpm', changes={'work/post_i': 'MISSING'}, post_script=False),
        'rpm_post_source_missing_header_with_explicit_aap': run_target(
            'rpm', changes={'work/post_i': 'MISSING'}, post_script=False,
            aap_value='aap'),
        'rpm_postscript_without_aap': run_target('rpm', True,
                                                postscript_value='info'),
        'rpm_postscript_with_explicit_aap': run_target('rpm', True,
                                                      postscript_value='info',
                                                      aap_value='aap'),
        'rpm_existing_fetch_checksum_markers': run_target(
            'rpm', existing_markers=('fetch', 'checksum', 'cvs-no'))}
    return {'source_boundary': ['ports/globals.aap', 'ports/editors/nano/main.aap'],
            'fixture': {'os': 'Linux', 'distribution': 'controlled SUSE15',
                        'target_host_architecture': 'x86_64',
                        'nano_lxver': 'noarch', 'encoding': 'latin-1',
                        'processes': 'fake zero statuses; no external effects',
                        'filesystem': 'explicit in-memory observations only'},
            'graph': {'nodes': len(nodes), 'definitions': len(driver.graph.definitions),
                      'targets_with_definitions': len(driver.graph.targets)},
            'targets': targets, 'branches': branches}


def main():
    data = json.dumps(report(), sort_keys=True, indent=2) + '\n'
    if len(sys.argv) == 3 and sys.argv[1] == '--output':
        with io.open(sys.argv[2], 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(data)
    elif len(sys.argv) == 1:
        print(data, end='')
    else:
        raise SystemExit('usage: nano_target_frontier.py [--output FILE]')


if __name__ == '__main__':
    main()
