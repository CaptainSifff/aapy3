"""Structured trace and summary serializers for real integration."""
from __future__ import print_function

import binascii
import hashlib
import json
import os
import time

from aap_semantics import ChecksumBackend


class EventRecorder(object):
    def __init__(self, environment):
        self.environment = environment
        self.path = environment.get('AAP_RECURSIVE_TRACE')

    def __call__(self, kind, **values):
        if not self.path:
            return
        event = {'event': kind, 'pid': os.getpid(), 'ppid': os.getppid(),
                 'time': time.time(),
                 'invocation': self.environment.get('AAP_RECURSIVE_INVOCATION', 'primary')}
        event.update(values)
        with open(self.path, 'a') as stream:
            stream.write(json.dumps(event, sort_keys=True) + '\n')


class TracingChecksum(ChecksumBackend):
    def __init__(self, artifacts, record=None):
        super(TracingChecksum, self).__init__(artifacts)
        self._record = record if record is not None else (lambda kind, **fields: None)

    def md5(self, request):
        computed = super(TracingChecksum, self).md5(request)
        self._record('checksum_digest', path=request.path,
                     expected=request.expected.get('md5'), computed=computed,
                     matches=computed == request.expected.get('md5'))
        return computed


def inventory(recipe_dir, environment=None):
    environment = os.environ if environment is None else environment
    candidates = []
    specs = []
    configure_files = []
    counts = {'distfiles': 0, 'work': 0, 'pack': 0}
    for name in counts:
        base = os.path.join(recipe_dir, name)
        if os.path.isdir(base):
            for directory, subdirs, names in os.walk(base):
                counts[name] += len(names)
                for filename in names:
                    path = os.path.join(directory, filename)
                    if filename.endswith('.spec'):
                        specs.append(path)
                    if filename == 'configure':
                        configure_files.append(path)
    for base in ('/export/company', '/usr/src/packages/RPMS'):
        if not os.path.isdir(base):
            continue
        for directory, subdirs, names in os.walk(base):
            for name in names:
                if name.endswith('.rpm'):
                    candidates.append(os.path.join(directory, name))
    observed = []
    for argument in environment.get('AAP_RECURSIVE_OBSERVE', '').split(os.pathsep):
        if not argument:
            continue
        path = argument if os.path.isabs(argument) else os.path.join(recipe_dir, argument)
        exists = os.path.isfile(path)
        entry = {'argument': argument, 'path': path, 'exists': exists,
                 'bytes': os.path.getsize(path) if exists else None}
        if exists:
            with open(path, 'rb') as stream:
                data = stream.read()
            entry['sha256'] = hashlib.sha256(data).hexdigest()
            entry['hex'] = binascii.hexlify(data).decode('ascii')
        observed.append(entry)
    done_dir = os.path.join(recipe_dir, 'done')
    return {'file_counts': counts,
            'directory_exists': {name: os.path.isdir(os.path.join(recipe_dir, name))
                                 for name in counts},
            'configure_files': sorted(configure_files),
            'spec_files': sorted(specs),
            'observed_files': observed,
            'done': os.path.isdir(done_dir),
            'done_markers': sorted(os.listdir(done_dir)) if os.path.isdir(done_dir) else [],
            'signature_file': os.path.isfile(os.path.join(
                recipe_dir, 'AAPDIR', 'signatures.json')),
            'rpm_candidates': sorted(candidates)}


def span(value):
    if value is None:
        return None
    return {'source': value.source_id, 'line': value.start.line,
            'column': value.start.column}


def tree_record(record):
    """Serialize controlled :tree facts for recursive integration evidence.

    The interpreter has already recorded these facts in its body result.  This
    adapter merely makes that record observable at the external-process
    boundary; it does not perform another filesystem observation.
    """
    request, observation = record.request, record.observation
    return {
        'operation': request.operation,
        'argument': request.argument,
        'path': request.path,
        'cwd': request.cwd,
        'source': request.source.source_id,
        'span': span(request.span),
        'status': observation.status,
        'entries': list(observation.entries),
        'target_kind': observation.target_kind,
        'detail': observation.detail,
        'matched': record.matched,
    }


def directory_change(record):
    """Serialize existing :cd execution-frame records without changing them."""
    return {
        'raw': record.raw,
        'expanded': record.expanded,
        'before': record.before,
        'requested': record.requested,
        'after': record.after,
        'status': record.status,
        'error': str(record.error) if record.error else None,
        'source': record.source.source_id,
        'span': span(record.span),
    }


def body_tree_records(result):
    records = []
    for body in result.bodies:
        records.extend(tree_record(record) for record in body.tree_records)
    return records


def body_directory_changes(result):
    records = []
    for body in result.bodies:
        records.extend(directory_change(record) for record in body.directory_changes)
    return records


def cat_record(record):
    request = record.request
    return {'raw': request.raw, 'raw_destination': request.raw_destination,
            'raw_sources': request.raw_sources,
            'expanded_destination': request.expanded_destination,
            'expanded_sources': request.expanded_sources,
            'destination': request.destination, 'path': request.path,
            'source_items': list(request.source_items), 'paths': list(request.paths),
            'cwd': request.cwd, 'status': record.status, 'phase': record.phase,
            'active_source': record.active_source, 'reads': list(record.reads),
            'bytes_read': record.bytes_read, 'bytes_written': record.bytes_written,
            'opened': record.opened, 'closed': record.closed,
            'error': str(record.error) if record.error else None,
            'close_error': record.close_error, 'message': record.message,
            'source': request.source.source_id, 'span': span(request.span)}


def body_cat_records(result):
    records = []
    for body in result.bodies:
        records.extend(cat_record(record) for record in body.evaluation.cats)
    return records


def body_print_records(result):
    records = []
    for body in result.bodies:
        evaluation = getattr(body, 'evaluation', None)
        if evaluation is None:
            continue
        for item in evaluation.prints:
            request = item.request
            if request.destination != 'stdout':
                continue
            records.append({'target': body.target.name,
                            'text': request.text,
                            'data_hex': binascii.hexlify(request.data).decode('ascii'),
                            'bytes': len(request.data),
                            'destination': request.destination,
                            'status': item.status,
                            'source': request.source.source_id,
                            'span': span(item.span)})
    return records


def body_process_captures(result):
    records = []
    for body in result.bodies:
        evaluation = getattr(body, 'evaluation', None)
        if evaluation is None:
            continue
        for item in evaluation.processes:
            if not hasattr(item, 'pipeline') or item.pipeline.target is None:
                continue
            records.append({'target': body.target.name,
                            'command': item.request.command,
                            'cwd': item.request.cwd,
                            'assignment': item.pipeline.target,
                            'captured_value': item.output,
                            'backend_stdout_hex': binascii.hexlify(
                                item.result.stdout).decode('ascii'),
                            'wait_status': item.result.wait_status,
                            'source': item.source.source_id,
                            'span': span(item.span)})
    return records


def body_decisions(result):
    records = []
    for body in result.bodies:
        step = body.context.step
        records.append({'target': body.target.name, 'status': body.status,
                        'trigger': step.reason, 'virtual': body.target.virtual,
                        'prepared_buildcheck': body.context.prepared_buildcheck})
    return records


def completion_decisions(result):
    return [{'target': item.target.name, 'status': item.status,
             'reason': item.reason,
             'signature_records': len(item.records)}
            for item in result.completions]


def python_write_records(result):
    records = []
    for body in result.bodies:
        evaluation = getattr(body, 'evaluation', None)
        if evaluation is None:
            continue
        for item in evaluation.python_writes:
            request = item.request
            records.append({'operation': item.operation,
                            'status': item.status,
                            'path': request.path,
                            'cwd': request.cwd,
                            'mode': request.mode,
                            'bytes': len(item.data) if item.data is not None else None,
                            'data_hex': (binascii.hexlify(item.data).decode('ascii')
                                         if item.data is not None else None),
                            'source': request.source.source_id,
                            'span': span(item.span)})
    return records
