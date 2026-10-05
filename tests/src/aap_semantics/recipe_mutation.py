"""Narrow byte-stream and file-name capabilities for Port.port_makesum.

No host adapter. Open/read/write/close remain separate so failure timing and
partial temporary-file contents are observable. No transaction or rollback.
"""
from .model import Node
from .path_observation import PathObserver, PathObservation


class RecipeMutationRequest(Node):
    def __init__(self, origin, operation, path, destination=None, data=None):
        super(RecipeMutationRequest, self).__init__(origin)
        self.operation, self.path = operation, path
        self.destination, self.data = destination, data


class RecipeMutationResult(object):
    def __init__(self, status='COMPLETED', detail=None, data=None):
        self.status, self.detail, self.data = status, detail, data


class RecipeMutationBackend(object):
    def apply(self, request):
        return RecipeMutationResult('UNAVAILABLE', 'recipe mutation capability unavailable')


class MemoryRecipeMutationBackend(RecipeMutationBackend, PathObserver):
    """Explicit regular-byte-file store; recipe families are closed fixtures.

    For each supplied recipe, its numbered temps and ~ backup are known missing
    unless present in files. Other unknown paths delegate or stay unavailable.
    files may be shared with existing artifact/generated-file adapters.
    failures maps (operation, path, destination) to structured failed/unavailable
    results. Failures happen before that operation's effect; earlier writes stay.
    """
    def __init__(self, recipes=(), files=None, failures=None, fallback=None):
        self.recipes = tuple(recipes)
        self.files = files if files is not None else {}
        self.failures = dict(failures or {})
        self.fallback = fallback
        self.requests, self.observations = [], []
        self.readers, self.writers = {}, set()

    def observe(self, request):
        self.observations.append(request)
        if request.path in self.files:
            return PathObservation('EXISTS')
        for recipe in self.recipes:
            suffix = request.path[len(recipe):] if request.path.startswith(recipe) else None
            if suffix is not None and (suffix in ('', '~') or
                    (suffix and all(c in '0123456789' for c in suffix))):
                return PathObservation('MISSING')
        if self.fallback is not None:
            return self.fallback.observe(request)
        return PathObservation('UNAVAILABLE')

    def apply(self, request):
        self.requests.append(request)
        operation, path = request.operation, request.path
        failure = self.failures.get((operation, path, request.destination))
        if failure is not None:
            return failure
        try:
            if operation == 'open_read':
                if type(self.files.get(path)) is not bytes:
                    raise OSError('recipe is not a readable regular byte file')
                self.readers[path] = (self.files[path], 0)
            elif operation == 'readline':
                data, offset = self.readers[path]
                end = data.find(b'\n', offset)
                end = len(data) if end == -1 else end + 1
                self.readers[path] = (data, end)
                return RecipeMutationResult(data=data[offset:end])
            elif operation == 'close_read':
                self.readers.pop(path, None)
            elif operation == 'create_temp':
                # Like open(..., 'w'), not O_EXCL. The prior existence probe
                # does not make creation atomic.
                self.files[path] = b''
                self.writers.add(path)
            elif operation == 'write':
                if path not in self.writers or type(request.data) is not bytes:
                    raise OSError('invalid recipe byte write')
                self.files[path] += request.data
            elif operation == 'close_temp':
                self.writers.discard(path)
            elif operation == 'remove':
                if path not in self.files:
                    raise OSError('file does not exist')
                del self.files[path]
            elif operation == 'rename':
                if path not in self.files:
                    raise OSError('rename source does not exist')
                self.files[request.destination] = self.files.pop(path)
            else:
                return RecipeMutationResult('UNAVAILABLE', 'unknown recipe mutation operation')
        except (OSError, KeyError) as error:
            return RecipeMutationResult('FAILED', str(error))
        return RecipeMutationResult()
