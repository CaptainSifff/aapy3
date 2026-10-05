"""Read-only source loading and POSIX recipe-directory resolution."""
import posixpath

from aap_frontend import Source
from .diagnostics import Unsupported
from .model import Node


class SourceLoader(object):
    """Explicit opt-in to local reads. Encoding is a caller policy, not guessed."""
    def __init__(self, encoding='utf-8'):
        self.encoding = encoding

    def load(self, path):
        return Source.from_path(path, self.encoding)


class IncludeRecord(Node):
    def __init__(self, command, path, program=None, skipped_active=False):
        super(IncludeRecord, self).__init__(command)
        self.path = path
        self.program = program
        self.skipped_active = skipped_active


def resolve_path(path, cwd, origin):
    # dictlist_expand supports wildcards/~; those need a separate capability.
    if (not path or '\x00' in path or any(c in path for c in '*?[')
            or path.startswith('~') or '://' in path):
        raise Unsupported(origin, 'include requires a literal local path after expansion')
    if not posixpath.isabs(path):
        if cwd is None or not posixpath.isabs(cwd):
            raise Unsupported(origin, 'relative include requires an absolute recipe directory')
        path = posixpath.join(cwd, path)
    return posixpath.normpath(path)
