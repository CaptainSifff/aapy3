"""Controlled aggregate traversal over the supplied example recipes.

No host tree walk, child A-A-P launch, or inferred process filesystem effect.
The declaration below is the whole observed hierarchy for this fixture.
"""
import io
import json
import os
import posixpath
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, Evaluator, MemoryMarkers,
    MemoryPersistence, MemoryPortDirectories, MemoryTargetState,
    MemoryTreeFilesystem, PortRuntime, ProcessBackend, ProcessPolicy,
    ProcessResult, Scope, TreeObservation, lower)
from aap_semantics.port_commands import (PortCommandPolicy, PortCommandRuntime,
                                         PortDirectories)


CANONICAL_ROOT = '/authorized/ports'
LISTINGS = (
    ('.', ('main.aap', 'globals.aap', 'editors', 'company')),
    ('./editors', ('nano',)),
    ('./editors/nano', ('main.aap',)),
    ('./company', ('efiloader',)),
    ('./company/efiloader', ('main.aap',)),
)
KINDS = (
    ('./main.aap', 'FILE'),
    ('./globals.aap', 'FILE'),
    ('./editors', 'DIRECTORY'),
    ('./editors/nano', 'DIRECTORY'),
    ('./editors/nano/main.aap', 'FILE'),
    ('./company', 'DIRECTORY'),
    ('./company/efiloader', 'DIRECTORY'),
    ('./company/efiloader/main.aap', 'FILE'),
)
ENTERABLE = (CANONICAL_ROOT, CANONICAL_ROOT + '/editors/nano',
             CANONICAL_ROOT + '/company/efiloader')


class RecordedTree(MemoryTreeFilesystem):
    def __init__(self, listings, kinds, events):
        super(RecordedTree, self).__init__(listings, kinds)
        self.events = events

    def list_directory(self, request):
        observation = super(RecordedTree, self).list_directory(request)
        self.events.append({'kind': 'tree_list', 'argument': request.argument,
                            'path': request.path, 'cwd': request.cwd,
                            'status': observation.status,
                            'entries': list(observation.entries)})
        return observation

    def classify(self, request):
        observation = super(RecordedTree, self).classify(request)
        self.events.append({'kind': 'tree_classify', 'argument': request.argument,
                            'path': request.path, 'cwd': request.cwd,
                            'status': observation.status})
        return observation


class RecordedDirectories(MemoryPortDirectories):
    def __init__(self, directories, events):
        super(RecordedDirectories, self).__init__(directories)
        self.events = events

    def enter(self, path):
        observed = super(RecordedDirectories, self).enter(path)
        self.events.append({'kind': 'directory_entry', 'requested': path,
                            'observed_cwd': observed})
        return observed


class OpaqueSuccess(ProcessBackend):
    """Observe shell dispatch, returning status zero without child effects."""
    def __init__(self, events):
        self.events = events
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        self.events.append({'kind': 'process_request', 'command': request.command,
                            'cwd': request.cwd, 'wait_status': 0,
                            'source_lines': [entry.span.start.line
                                             for entry in request.entries],
                            'filesystem_effects_observed': False})
        return ProcessResult(0, b'', b'')


class CanonicalPortsFixture(object):
    """One declared hierarchy; switches withdraw capabilities, not add paths."""
    def __init__(self, observe_tree=True, observe_directories=True,
                 observe_process=True):
        for relative in ('ports/main.aap', 'ports/globals.aap',
                         'ports/editors/nano/main.aap',
                         'ports/company/efiloader/main.aap'):
            if not os.path.isfile(os.path.join(ROOT, 'tests', 'fixtures', relative)):
                raise AssertionError('supplied canonical recipe missing: ' + relative)
        self.events = []
        root = CANONICAL_ROOT
        listings = (dict((posixpath.join(root, path), TreeObservation('ENTRIES', names))
                         for path, names in LISTINGS) if observe_tree else {})
        kinds = (dict((posixpath.join(root, path), TreeObservation(kind))
                      for path, kind in KINDS) if observe_tree else {})
        self.tree = RecordedTree(listings, kinds, self.events)
        self.directories = (RecordedDirectories(ENTERABLE, self.events)
                            if observe_directories else PortDirectories())
        self.process = OpaqueSuccess(self.events) if observe_process else None

    def build(self, target='packageall'):
        path = os.path.join(ROOT, 'tests', 'fixtures', 'ports', 'main.aap')
        source = Source.from_path(path, 'latin-1')
        source = Source(CANONICAL_ROOT + '/main.aap', source.text)
        scope = Scope.top_level()
        # Work.set_defaults supplies $AAP in the historical interpreter.
        # This spelling identifies an opaque controlled process, not a child run.
        scope.local['AAP'] = 'aap'
        data = Evaluator(scope).run(lower(parse(source)))
        if not data.complete:
            raise AssertionError('aggregate metadata failed')
        port = PortRuntime(markers=MemoryMarkers(), commands=PortCommandRuntime(
            self.directories, PortCommandPolicy(False, False)))
        driver = BuildDriver(data.graph, MemoryTargetState(), MemoryPersistence(),
            scope, data.declarations, port_defaults=False, tree_filesystem=self.tree,
            port_runtime=port, process_backend=self.process,
            process_policy=ProcessPolicy('latin-1', sys_mode='unlogged'))
        return driver.build(target)


def location(span):
    return None if span is None else {'source': span.source_id,
        'line': span.start.line, 'column': span.start.column,
        'end_line': span.end.line, 'end_column': span.end.column}


def ordered_trace(fixture, result):
    body = result.bodies[-1] if result.bodies else None
    matches = {} if body is None else dict((record.request.path, record.request.argument)
        for record in body.tree_records if record.matched)
    changes = [] if body is None else list(body.directory_changes)
    events = [{'kind': 'tree_root', 'argument': '.', 'cwd': CANONICAL_ROOT,
               'path': CANONICAL_ROOT + '/.'}]
    change_index = 0
    for event in fixture.events:
        event = dict(event)
        if event['kind'] == 'directory_entry':
            change = changes[change_index]
            change_index += 1
            event.update({'cwd_before': change.before, 'cwd_after': change.after,
                          'expanded': change.expanded,
                          'previous_before': change.previous_before,
                          'previous_after': change.previous_after})
        events.append(event)
        if event['kind'] == 'tree_classify' and event['path'] in matches:
            name = matches[event['path']]
            events.append({'kind': 'name_bound', 'value': name,
                           'cwd': event['cwd'], 'source_line': 11})
            events.append({'kind': 'dir_derived', 'value': posixpath.dirname(name),
                           'cwd': event['cwd'], 'source_line': 12})
    return events


def report():
    variants = (
        ('tree_unobserved', CanonicalPortsFixture(False, True, True),
         'missing fixture fact'),
        ('directory_unobserved', CanonicalPortsFixture(True, False, True),
         'missing fixture fact'),
        ('process_unobserved', CanonicalPortsFixture(True, True, False),
         'missing fixture fact'),
        ('complete_fixture', CanonicalPortsFixture(), None),
    )
    observations = {}
    for name, fixture, classification in variants:
        result = fixture.build()
        body = result.bodies[-1] if result.bodies else None
        observations[name] = {'status': result.status, 'reason': result.reason,
            'location': location(result.span),
            'blocked_command': getattr(result.blocked_at, 'name', None),
            'error': str(result.error) if result.error is not None else None,
            'classification': classification,
            'events': ordered_trace(fixture, result),
            'body_final_cwd': (body.evaluation.final_cwd if body is not None
                               and body.evaluation is not None else None),
            'body_local_dir': body.scope.local.get('dir') if body is not None else None,
            'body_local_name': body.scope.local.get('name') if body is not None else None,
            'process_requests': len(fixture.process.requests)
                                if fixture.process is not None else 0}
    if observations['complete_fixture']['status'] != 'COMPLETE':
        raise AssertionError('aggregate controlled fixture no longer completes')
    if observations['complete_fixture']['process_requests'] != 2:
        raise AssertionError('aggregate traversal did not dispatch both supplied packages')
    for name, line in (('tree_unobserved', 11),
                       ('directory_unobserved', 14),
                       ('process_unobserved', 15)):
        observed = observations[name]
        if (observed['status'] != 'BLOCKED' or observed['location'] is None
                or observed['location']['line'] != line):
            raise AssertionError('aggregate fixture boundary changed: ' + name)
    return {'source_boundary': ['ports/main.aap', 'ports/globals.aap',
                                'ports/editors/nano/main.aap',
                                'ports/company/efiloader/main.aap'],
            'fixture': {'root': CANONICAL_ROOT,
                        'listings': [{'path': path, 'entries': list(entries)}
                                     for path, entries in LISTINGS],
                        'classifications': [{'path': path, 'kind': kind}
                                            for path, kind in KINDS],
                        'enterable_directories': list(ENTERABLE),
                        'AAP': 'aap',
                        'process_effects': 'fake zero status; no child execution'},
            'target': 'packageall', 'variants': observations,
            'first_genuine_unsupported': None,
            'dispatch_boundary': 'opaque external AAP batch submitted; child effects unobserved'}


def main():
    data = json.dumps(report(), sort_keys=True, indent=2) + '\n'
    if len(sys.argv) == 3 and sys.argv[1] == '--output':
        with io.open(sys.argv[2], 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(data)
    elif len(sys.argv) == 1:
        print(data, end='')
    else:
        raise SystemExit('usage: aggregate_ports_frontier.py [--output FILE]')


if __name__ == '__main__':
    main()
