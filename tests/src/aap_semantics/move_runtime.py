"""Bounded local regular-file :move for the reached doperlmod form.

CopyMove.remote_copy_move first tries os.rename for a local move. Semantic
code records that one-file rename request through an injected backend; it does
not call a host filesystem or infer output from a preceding shell command.
"""
import posixpath

from .model import Node
from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text


class MoveRequest(Node):
    def __init__(self, origin, source, destination, cwd):
        super(MoveRequest, self).__init__(origin)
        self.source_argument, self.destination_argument = source, destination
        self.cwd = cwd
        self.source = source if posixpath.isabs(source) else posixpath.join(cwd, source)
        self.destination = (destination if posixpath.isabs(destination)
                            else posixpath.join(cwd, destination))


class MoveResult(object):
    def __init__(self, status='COMPLETED', detail=None):
        self.status, self.detail = status, detail


class MoveBackend(object):
    def move(self, request):
        return MoveResult('UNAVAILABLE', 'move mutation capability unavailable')


class MemoryMoveBackend(MoveBackend):
    """Controlled regular-byte-file rename store.

    A successful request replaces an existing destination and removes its
    source in one recorded effect, matching the normal local os.rename path.
    Failures are reported before mutation, so earlier recipe effects remain
    visible without inventing the historical copy/delete fallback.
    """
    def __init__(self, files=None, failures=None):
        self.files = files if files is not None else {}
        self.failures = dict(failures or {})
        self.requests = []

    def move(self, request):
        self.requests.append(request)
        failure = self.failures.get((request.source, request.destination))
        if failure is not None:
            return failure
        if type(self.files.get(request.source)) is not bytes:
            return MoveResult('FAILED', 'move source does not exist: ' + request.source)
        self.files[request.destination] = self.files.pop(request.source)
        return MoveResult()


class MoveRecord(Node):
    def __init__(self, request):
        super(MoveRecord, self).__init__(request)
        self.request, self.result = request, None
        self.status, self.error = 'PENDING', None


class MoveRuntime(object):
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else MoveBackend()

    def execute(self, node, scope, python, cwd, records):
        raw = render_value(node.arguments, python)
        if raw.lstrip(' \t').startswith('{'):
            raise Unsupported(node, ':move attributes/options are deferred')
        expanded = expand_text(raw, scope, node, item_attributes=True)
        parsed = items(expanded, node, label='move')
        if len(parsed) != 2 or any(attrs for name, attrs in parsed):
            raise Unsupported(node, 'only one source and one destination are supported for :move')
        source, destination = parsed[0][0], parsed[1][0]
        if (not source or not destination or '\x00' in source or '\x00' in destination):
            raise SemanticError(node, ':move paths must be nonempty and NUL-free')
        if any(char in source + destination for char in '~*?[]{}') or ':' in source + destination:
            raise Unsupported(node, ':move glob, user-directory, URL and attribute paths are deferred')
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, ':move requires an explicit absolute cwd')
        request = MoveRequest(node, source, destination, cwd)
        record = MoveRecord(request)
        records.append(record)
        try:
            outcome = self.backend.move(request)
        except NotImplementedError as error:
            outcome = MoveResult('UNAVAILABLE', str(error))
        except Exception as error:
            outcome = MoveResult('FAILED', str(error))
        record.result = outcome
        if not isinstance(outcome, MoveResult) or outcome.status not in (
                'COMPLETED', 'UNAVAILABLE', 'FAILED'):
            record.status = 'FAILED'
            raise SemanticError(node, 'invalid move mutation result')
        if outcome.status == 'UNAVAILABLE':
            record.status = 'BLOCKED'
            record.error = Unsupported(node, outcome.detail or 'move mutation unavailable')
            raise record.error
        if outcome.status == 'FAILED':
            record.status = 'FAILED'
            record.error = SemanticError(node, 'move failed: ' + str(outcome.detail))
            raise record.error
        record.status = 'COMPLETED'
