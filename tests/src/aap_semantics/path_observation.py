"""Read-only stat-success observations. No host filesystem adapter is installed."""
import posixpath

from .diagnostics import SemanticError, Unsupported


class PathRequest(object):
    def __init__(self, path, cwd, origin):
        self.argument, self.cwd = path, cwd
        self.source, self.span = origin.source, origin.span
        # Keep dot segments, trailing slashes and symlink-sensitive spelling.
        self.path = path if not path or posixpath.isabs(path) else posixpath.join(cwd, path)


class PathObservation(object):
    """MISSING means stat failed, including EACCES or a dangling symlink.

    ERROR means the capability itself failed, not an observed stat failure.
    An adapter must classify these explicitly; an arbitrary exception is never
    taken as evidence that a recipe path does not exist.
    """
    def __init__(self, status, detail=None):
        self.status, self.detail = status, detail


class PathObserver(object):
    def observe(self, request):
        return PathObservation('UNAVAILABLE', 'path observation capability unavailable')


class MemoryPathObserver(PathObserver):
    """Explicit observations by exact absolute spelling; unknown is unavailable.

    Fixtures certify stat success/failure, including link-following behavior;
    this is not a filesystem or symlink simulator. No process result populates it.
    """
    def __init__(self, observations=None):
        self.observations = dict(observations or {})
        self.requests = []

    def observe(self, request):
        self.requests.append(request)
        return self.observations.get(request.path, PathObservation('UNAVAILABLE'))


class PathRecord(object):
    def __init__(self, request, observation):
        self.request, self.observation = request, observation


class PathObservationRuntime(object):
    def __init__(self, observer=None):
        self.observer = observer if observer is not None else PathObserver()
        self.records = []

    def exists(self, path, cwd, origin):
        if type(path) is not str:
            raise SemanticError(origin, 'os.path.exists requires a string path')
        if '\x00' in path:
            raise SemanticError(origin, 'os.path.exists path contains NUL')
        if path and not posixpath.isabs(path):
            if type(cwd) is not str or not posixpath.isabs(cwd):
                raise Unsupported(origin, 'path observation requires an explicit absolute cwd')
        request = PathRequest(path, cwd, origin)
        try:
            # stat("") cannot succeed. Do not accidentally join it to cwd.
            observation = (PathObservation('MISSING', 'empty path') if not path
                           else self.observer.observe(request))
        except NotImplementedError as error:
            observation = PathObservation('UNAVAILABLE', str(error))
        except Exception as error:
            observation = PathObservation('ERROR', str(error))
        if (not isinstance(observation, PathObservation)
                or observation.status not in ('EXISTS', 'MISSING', 'UNAVAILABLE', 'ERROR')):
            observation = PathObservation('ERROR', 'invalid path observation result')
        self.records.append(PathRecord(request, observation))
        if observation.status == 'UNAVAILABLE':
            raise Unsupported(origin, 'path observation unavailable: ' + request.path)
        if observation.status == 'ERROR':
            raise SemanticError(origin, 'path observation failed: ' + request.path
                                + ' (' + str(observation.detail) + ')')
        return observation.status == 'EXISTS'
