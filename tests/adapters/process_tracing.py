"""Read-only integration evidence around an injected process backend."""
from __future__ import print_function

import hashlib
import os

from aap_semantics import ProcessBackend


def patch_observations(environment):
    observations = []
    for path in environment.get('AAP_RECURSIVE_PATCH_OBSERVE', '').split(os.pathsep):
        if not path:
            continue
        if not os.path.isfile(path):
            observations.append({'path': path, 'exists': False})
            continue
        digest = hashlib.sha256()
        size = 0
        with open(path, 'rb') as stream:
            while True:
                block = stream.read(32768)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        observations.append({'path': path, 'exists': True, 'bytes': size,
                             'sha256': digest.hexdigest()})
    return observations


def _read_observed_file(path):
    try:
        with open(path, 'rb') as stream:
            data = stream.read()
    except IOError:
        return None, {'path': path, 'exists': False, 'bytes': None,
                      'sha256': None}
    return data, {'path': path, 'exists': True, 'bytes': len(data),
                  'sha256': hashlib.sha256(data).hexdigest()}


def _append_request_observation(request):
    """Read-only integration evidence for simple command >> target requests."""
    expression = getattr(request, 'expression', None)
    if expression is None or expression.kind != 'SIMPLE' or not expression.redirections:
        return None
    if len(expression.redirections) != 1 or expression.redirections[0].operator != '>>':
        return None
    if len(expression.argv) < 2:
        return None
    source_path = expression.argv[1]
    target_path = expression.redirections[0].target
    if not os.path.isabs(source_path):
        source_path = os.path.join(request.cwd, source_path)
    if not os.path.isabs(target_path):
        target_path = os.path.join(request.cwd, target_path)
    source_bytes, source_info = _read_observed_file(source_path)
    destination_bytes, destination_info = _read_observed_file(target_path)
    span = getattr(request, 'span', None)
    source_span = None
    if span is not None:
        source_span = {'source': getattr(span, 'source_id', None),
                       'line': getattr(getattr(span, 'start', None), 'line', None),
                       'column': getattr(getattr(span, 'start', None), 'column', None)}
    batch = getattr(request, 'batch', None)
    return {'source_bytes': source_bytes, 'destination_bytes': destination_bytes,
            'source': source_info, 'destination_before': destination_info,
            'destination_path': target_path, 'source_span': source_span,
            'batch_entry_count': len(getattr(batch, 'entries', ())) if batch else None}


def _process_file_observations(request, environment):
    """Read explicitly requested paths around a real process request."""
    observations = []
    for argument in environment.get('AAP_RECURSIVE_PROCESS_OBSERVE', '').split(os.pathsep):
        if not argument:
            continue
        path = argument if os.path.isabs(argument) else os.path.join(request.cwd, argument)
        exists = os.path.exists(path)
        item = {'argument': argument, 'path': path, 'exists': exists,
                'bytes': None, 'sha256': None}
        if exists and os.path.isfile(path):
            try:
                digest = hashlib.sha256()
                size = 0
                with open(path, 'rb') as stream:
                    while True:
                        block = stream.read(32768)
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                item['bytes'] = size
                item['sha256'] = digest.hexdigest()
            except IOError as error:
                item['observation_error'] = str(error)
        observations.append(item)
    return observations


class TracingProcessBackend(ProcessBackend):
    def __init__(self, inner, record, environment, enabled=True):
        self.inner, self.record = inner, record
        self.environment, self.enabled = environment, enabled

    def run(self, request):
        logged = getattr(request, 'logging', False)
        patch = getattr(request, 'operation', None) == 'port_patch'
        append = _append_request_observation(request) if self.enabled else None
        files_before = (_process_file_observations(request, self.environment)
                        if self.enabled else [])
        if files_before:
            self.record('process_file_observation', phase='before',
                        command=request.command, cwd=request.cwd, files=files_before)
        if append is not None:
            self.record('append_redirection_observation', phase='before',
                        command=request.command, cwd=request.cwd,
                        source_span=append['source_span'],
                        batch_entry_count=append['batch_entry_count'],
                        source=append['source'],
                        destination_before=append['destination_before'],
                        destination=append['destination_path'])
        if patch and not logged:
            self.record('patch_observation', phase='before',
                        files=patch_observations(self.environment))
        spawned = []

        def started(pid, shell_command):
            spawned.append(pid)
            if logged:
                self.record('process_start', shell_pid=pid, cwd=request.cwd,
                            command=request.command, shell_command=shell_command,
                            logging=True, quiet=request.quiet, force=request.force,
                            log_path=request.log_path)
            else:
                self.record('process_start', shell_pid=pid, cwd=request.cwd,
                            command=request.command,
                            operation=getattr(request, 'operation', None),
                            shell_command=shell_command,
                            environment_policy=getattr(request, 'environment_policy',
                                                       'inherit-backend'),
                            sentinel=self.environment.get('AAP_RECURSIVE_SENTINEL'),
                            recipe_aap=self.environment.get('AAP'))

        if hasattr(self.inner, 'run_observed'):
            result = self.inner.run_observed(request, started)
        else:
            started(None, request.shell_command)
            result = self.inner.run(request)
        pid = spawned[-1]
        if logged:
            shell_wait = result.shell_wait_status
            self.record('process_exit', shell_pid=pid, cwd=request.cwd,
                        command=request.command, logging=True,
                        quiet=request.quiet, force=request.force,
                        log_path=request.log_path,
                        returncode=shell_wait >> 8,
                        shell_wait_status=shell_wait,
                        wait_status=result.wait_status,
                        stdout=result.stdout.decode('latin-1'),
                        stderr=result.stderr.decode('latin-1'),
                        logged_output=result.log_output.decode('latin-1'))
        else:
            returncode = result.wait_status >> 8
            self.record('process_exit', shell_pid=pid, cwd=request.cwd,
                        command=request.command,
                        operation=getattr(request, 'operation', None),
                        returncode=returncode, wait_status=result.wait_status,
                        stdout=result.stdout.decode('latin-1'),
                        stderr=result.stderr.decode('latin-1'))
            if patch:
                self.record('patch_observation', phase='after',
                            files=patch_observations(self.environment))
            if append is not None:
                destination_after_bytes, destination_after = _read_observed_file(
                    append['destination_path'])
                source_bytes = append['source_bytes']
                before_bytes = append['destination_bytes']
                expected = None
                if source_bytes is not None:
                    expected = (before_bytes if before_bytes is not None else b'') + source_bytes
                self.record('append_redirection_observation', phase='after',
                            command=request.command, cwd=request.cwd,
                            source_span=append['source_span'],
                            batch_entry_count=append['batch_entry_count'],
                            source=append['source'],
                            destination_before=append['destination_before'],
                            destination_after=destination_after,
                            exact_append=(expected is not None and
                                          destination_after_bytes == expected),
                            process_status=returncode)
        if files_before:
            self.record('process_file_observation', phase='after',
                        command=request.command, cwd=request.cwd,
                        files=_process_file_observations(request, self.environment))
        return result
