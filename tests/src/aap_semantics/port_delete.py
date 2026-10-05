"""Injected tree deletion for the bounded Port.clean/distclean helpers.

The semantic runtime never performs host deletion. The memory adapter uses
explicit entries and missing facts; unknown paths remain unavailable.
"""
from .model import Node
from .path_observation import PathObserver, PathObservation


class DeleteRequest(Node):
    def __init__(self, origin, path, argument, cwd):
        super(DeleteRequest, self).__init__(origin)
        self.path, self.argument, self.cwd = path, argument, cwd


class DeleteResult(object):
    def __init__(self, status, detail=None):
        self.status, self.detail = status, detail


class DeleteBackend(object):
    def delete_tree(self, request):
        return DeleteResult('UNAVAILABLE', 'tree deletion capability unavailable')


class MemoryDeleteBackend(DeleteBackend, PathObserver):
    """Explicit file/dir/link facts with recursive, link-safe in-memory removal.

    A link entry is ('symlink', absolute_target). Its target is observed with
    stat-like semantics but never removed by deleting the link.
    Associated in-memory stores are changed only by successful deletion.
    """
    def __init__(self, entries=None, missing=(), markers=None, marker_root=None,
                 stores=(), failures=None, fallback=None):
        self.entries = dict(entries or {})
        self.missing = set(missing)
        self.markers = markers
        self.marker_root = marker_root
        self.stores = list(stores)
        self.failures = dict(failures or {})
        self.fallback = fallback
        self.requests = []
        self.observations = []
        self.deleted = set()

    def _within(self, path, root):
        return path == root or path.startswith(root.rstrip('/') + '/')

    def _marker_path(self, path):
        return (self.markers is not None and self.marker_root is not None
                and self._within(path, self.marker_root))

    def observe(self, request):
        self.observations.append(request)
        path = request.path
        if any(self._within(path, root) for root in self.deleted):
            return PathObservation('MISSING')
        if path in self.entries:
            entry = self.entries[path]
            if isinstance(entry, tuple) and entry[0] == 'symlink':
                target = entry[1]
                if any(self._within(target, root) for root in self.deleted):
                    return PathObservation('MISSING')
                if target in self.entries:
                    return PathObservation('EXISTS')
                if target in self.missing:
                    return PathObservation('MISSING')
                return PathObservation('UNAVAILABLE')
            return PathObservation('EXISTS')
        if self._marker_path(path):
            return PathObservation('EXISTS' if self.markers.marker_exists(path) else 'MISSING')
        if path in self.missing:
            return PathObservation('MISSING')
        if self.fallback is not None:
            return self.fallback.observe(request)
        return PathObservation('UNAVAILABLE')

    def delete_tree(self, request):
        self.requests.append(request)
        path = request.path
        failure = self.failures.get(path)
        if failure is not None:
            return failure
        if self.observe(request).status != 'EXISTS':
            return DeleteResult('FAILED', 'path disappeared before deletion')
        # A symlink is unlinked itself, even when its target is a directory.
        entry = self.entries.get(path)
        recursive = not (isinstance(entry, tuple) and entry[0] == 'symlink')
        def selected(name):
            return self._within(name, path) if recursive else name == path
        for name in list(self.entries):
            if selected(name):
                del self.entries[name]
        for store in self.stores:
            for name in list(store):
                if selected(name):
                    del store[name]
        if self.markers is not None:
            for store in (self.markers.files, self.markers.times):
                for name in list(store):
                    if selected(name):
                        del store[name]
            self.markers.directories = set(name for name in self.markers.directories
                                           if not selected(name))
        self.deleted.add(path)
        return DeleteResult('COMPLETED')
