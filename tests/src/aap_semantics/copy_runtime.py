"""Bounded local regular-file :copy through an injected filesystem backend.

The reached production form is one plain local regular source and one local
destination.  It deliberately has a distinct backend from :move: historical
copy uses shutil.copy() and leaves the source in place, while move first tries
rename and has a different fallback path.
"""
import posixpath

from .model import Node
from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text


class CopyObservation(object):
    def __init__(self, status='COMPLETED', kind=None, detail=None):
        self.status, self.kind, self.detail = status, kind, detail


class CopyRequest(Node):
    def __init__(self, origin, source, destination, effective_destination, cwd,
                 source_kind, destination_kind):
        super(CopyRequest, self).__init__(origin)
        self.source_argument = source
        self.destination_argument = destination
        self.effective_destination_argument = effective_destination
        self.cwd = cwd
        self.source = _resolve(source, cwd)
        self.destination = _resolve(destination, cwd)
        self.effective_destination = _resolve(effective_destination, cwd)
        self.source_kind = source_kind
        self.destination_kind = destination_kind
        self.overwrite = 'replace'


class CopyResult(object):
    def __init__(self, status='COMPLETED', detail=None):
        self.status, self.detail = status, detail


class CopyBackend(object):
    """Observation and regular-file copy mutation capability."""
    def path_kind(self, path):
        return CopyObservation('UNAVAILABLE', detail='copy observation capability unavailable')

    def copy(self, request):
        return CopyResult('UNAVAILABLE', 'copy mutation capability unavailable')


class MemoryCopyBackend(CopyBackend):
    """Controlled regular-byte files and directories for semantic tests."""
    def __init__(self, files=None, directories=None, failures=None):
        self.files = files if files is not None else {}
        self.directories = set(directories or ())
        self.failures = dict(failures or {})
        self.observations = []
        self.requests = []
        for path in self.files:
            self._parents(path)
        for path in tuple(self.directories):
            self._parents(path)

    def _parents(self, path):
        parent = posixpath.dirname(path)
        while parent and parent != '/':
            self.directories.add(parent)
            parent = posixpath.dirname(parent)
        if path.startswith('/'):
            self.directories.add('/')

    def path_kind(self, path):
        self.observations.append(path)
        if path in self.files:
            return CopyObservation(kind='regular')
        if path in self.directories:
            return CopyObservation(kind='directory')
        return CopyObservation(kind='missing')

    def copy(self, request):
        self.requests.append(request)
        failure = self.failures.get((request.source, request.effective_destination))
        if failure is not None:
            return failure
        if request.source not in self.files:
            return CopyResult('FAILED', 'copy source does not exist: ' + request.source)
        parent = posixpath.dirname(request.effective_destination)
        if parent not in self.directories:
            return CopyResult('FAILED', 'copy destination parent does not exist: ' + parent)
        self.files[request.effective_destination] = self.files[request.source]
        return CopyResult()


class CopyRecord(Node):
    def __init__(self, request):
        super(CopyRecord, self).__init__(request)
        self.request, self.result = request, None
        self.status, self.error = 'PENDING', None


def _resolve(path, cwd):
    return path if posixpath.isabs(path) else posixpath.join(cwd, path)


def _observe(backend, origin, path, role):
    try:
        outcome = backend.path_kind(path)
    except NotImplementedError as error:
        outcome = CopyObservation('UNAVAILABLE', detail=str(error))
    except Exception as error:
        outcome = CopyObservation('FAILED', detail=str(error))
    if not isinstance(outcome, CopyObservation) or outcome.status not in (
            'COMPLETED', 'UNAVAILABLE', 'FAILED'):
        raise SemanticError(origin, 'invalid copy observation result')
    if outcome.status == 'UNAVAILABLE':
        raise Unsupported(origin, outcome.detail or 'copy observation capability unavailable')
    if outcome.status == 'FAILED':
        raise SemanticError(origin, 'copy ' + role + ' observation failed: ' +
                            str(outcome.detail))
    if outcome.kind not in ('regular', 'directory', 'missing', 'other'):
        raise SemanticError(origin, 'invalid copy ' + role + ' path kind')
    return outcome.kind


class CopyRuntime(object):
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else CopyBackend()

    def execute(self, node, scope, python, cwd, records):
        raw = render_value(node.arguments, python)
        if raw.lstrip(' \t').startswith('{'):
            raise Unsupported(node, ':copy attributes/options are deferred')
        expanded = expand_text(raw, scope, node, item_attributes=True)
        parsed = items(expanded, node, label='copy')
        if len(parsed) != 2 or any(attrs for name, attrs in parsed):
            raise Unsupported(node, 'only one source and one destination are supported for :copy')
        source, destination = parsed[0][0], parsed[1][0]
        if (not source or not destination or '\x00' in source or '\x00' in destination):
            raise SemanticError(node, ':copy paths must be nonempty and NUL-free')
        if any(char in source + destination for char in '~*?[]{}') or ':' in source + destination:
            raise Unsupported(node, ':copy glob, user-directory, URL and attribute paths are deferred')
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, ':copy requires an explicit absolute cwd')
        source_path = _resolve(source, cwd)
        destination_path = _resolve(destination, cwd)
        source_kind = _observe(self.backend, node, source_path, 'source')
        if source_kind == 'missing':
            raise SemanticError(node, 'copy source does not exist: ' + source_path)
        if source_kind != 'regular':
            raise Unsupported(node, 'only regular-file sources are supported for :copy')
        destination_kind = _observe(self.backend, node, destination_path, 'destination')
        if destination_kind == 'other':
            raise Unsupported(node, 'special destination paths are deferred for :copy')
        effective = destination
        if destination_kind == 'directory':
            effective = posixpath.join(destination, posixpath.basename(source))
            effective_kind = _observe(self.backend, node, _resolve(effective, cwd),
                                      'effective destination')
            if effective_kind == 'other':
                raise Unsupported(node, 'special destination paths are deferred for :copy')
        request = CopyRequest(node, source, destination, effective, cwd,
                              source_kind, destination_kind)
        record = CopyRecord(request)
        records.append(record)
        try:
            outcome = self.backend.copy(request)
        except NotImplementedError as error:
            outcome = CopyResult('UNAVAILABLE', str(error))
        except Exception as error:
            outcome = CopyResult('FAILED', str(error))
        record.result = outcome
        if not isinstance(outcome, CopyResult) or outcome.status not in (
                'COMPLETED', 'UNAVAILABLE', 'FAILED'):
            record.status = 'FAILED'
            raise SemanticError(node, 'invalid copy mutation result')
        if outcome.status == 'UNAVAILABLE':
            record.status = 'BLOCKED'
            record.error = Unsupported(node, outcome.detail or 'copy mutation capability unavailable')
            raise record.error
        if outcome.status == 'FAILED':
            record.status = 'FAILED'
            record.error = SemanticError(node, 'copy failed: ' + str(outcome.detail))
            raise record.error
        record.status = 'COMPLETED'
