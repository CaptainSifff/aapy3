"""Explicit host-facing capabilities shared by semantic execution phases."""


class RuntimeCapabilities(object):
    __slots__ = ('process_backend', 'process_policy', 'include_loader',
                 'checksum_backend', 'port_runtime', 'path_observer',
                 'output_runtime', 'cat_runtime', 'tree_filesystem',
                 'move_backend', 'copy_backend', '_frozen')

    def __init__(self, process_backend=None, process_policy=None,
                 include_loader=None, checksum_backend=None, port_runtime=None,
                 path_observer=None, output_runtime=None, cat_runtime=None,
                 tree_filesystem=None, move_backend=None, copy_backend=None):
        values = (process_backend, process_policy, include_loader,
                  checksum_backend, port_runtime, path_observer,
                  output_runtime, cat_runtime, tree_filesystem,
                  move_backend, copy_backend)
        for name, value in zip(self.__slots__, values):
            object.__setattr__(self, name, value)
        object.__setattr__(self, '_frozen', True)

    def __setattr__(self, name, value):
        if getattr(self, '_frozen', False):
            raise AttributeError('RuntimeCapabilities is immutable')
        object.__setattr__(self, name, value)

    def replace(self, **changes):
        names = self.__slots__[:-1]
        if any(name not in names for name in changes):
            raise TypeError('unknown runtime capability')
        values = dict((name, getattr(self, name)) for name in names)
        values.update(changes)
        return RuntimeCapabilities(**values)
