"""Bounded port fetch request and injected acquisition capability.

The port helper constructs ordered locations. A backend performs byte I/O and
reports each attempt; checksum verification remains a later build stage.
"""
from .model import Node


class FetchRequest(Node):
    def __init__(self, origin, destination, candidates, cwd, filename, sites):
        super(FetchRequest, self).__init__(origin)
        self.destination = destination
        self.candidates = tuple(candidates)
        self.cwd = cwd
        self.filename = filename
        self.sites = sites


class FetchAttempt(object):
    def __init__(self, candidate, status, detail='', count=None, sha256=None):
        self.candidate = candidate
        self.status = status
        self.detail = detail
        self.count = count
        self.sha256 = sha256


class FetchResult(object):
    def __init__(self, status, attempts, selected=None, detail=''):
        self.status = status
        self.attempts = tuple(attempts)
        self.selected = selected
        self.detail = detail


class FetchBackend(object):
    def fetch(self, request):
        raise NotImplementedError('port acquisition capability unavailable')


class MemoryFetchBackend(FetchBackend):
    """Network-free fake sharing exact byte files with MemoryArtifacts."""
    def __init__(self, files, sources=None):
        self.files = files
        self.sources = dict(sources or {})
        self.requests = []
        self.directories = []

    def fetch(self, request):
        self.requests.append(request)
        parent = request.destination.rsplit('/', 1)[0]
        self.directories.append(parent)
        attempts = []
        for candidate in request.candidates:
            value = self.sources.get(candidate)
            if type(value) is bytes:
                self.files[request.destination] = value
                attempt = FetchAttempt(candidate, 'COMPLETED', count=len(value))
                attempts.append(attempt)
                return FetchResult('COMPLETED', attempts, candidate)
            detail = str(value) if isinstance(value, Exception) else 'source unavailable'
            attempts.append(FetchAttempt(candidate, 'FAILED', detail))
        return FetchResult('FAILED', attempts, detail='all fetch candidates failed')
