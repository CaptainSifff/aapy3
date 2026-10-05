"""Bounded redirected A-A-P cat: exact Linux bytes, never a shell command."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text
from .command_items import items
from .checksum import ArtifactBackend, ArtifactUnavailable
from .output import print_parts, destination, TextWriter


class CatRequest(Node):
    def __init__(self, node, scope, python, cwd, policy):
        super(CatRequest, self).__init__(node)
        self.raw = render_value(node.arguments, python)
        raw_sources, raw_destination, mode = print_parts(self.raw, node, 'cat')
        self.raw_sources, self.raw_destination = raw_sources, raw_destination
        if mode not in ('append', 'overwrite'):
            raise Unsupported(node, 'cat requires bounded append/overwrite redirection')
        self.destination, self.cwd = mode, cwd
        self.path, self.expanded_destination = destination(
            raw_destination, scope, node, cwd, details=True, label='cat')
        self.expanded_sources = expand_text(raw_sources, scope, node)
        source_items = items(self.expanded_sources, node, 'cat source')
        if not source_items:
            raise SemanticError(node, 'cat requires at least one source filename')
        self.source_items = tuple(name for name, attrs in source_items)
        paths = []
        for name, attrs in source_items:
            if attrs or name == '-' or any(c in name for c in '~*?[]{}:'):
                raise Unsupported(node, 'cat source attributes/globs/tilde/URL/pipe input are deferred')
            if '\x00' in name:
                raise SemanticError(node, 'NUL in cat source path')
            if not posixpath.isabs(name):
                if type(cwd) is not str or not posixpath.isabs(cwd):
                    raise Unsupported(node, 'relative cat source needs an absolute logical cwd')
                name = posixpath.join(cwd, name)
            paths.append(name)
        self.paths = tuple(paths)
        # Encoding applies to paths only. Contents never pass through a codec.
        self.path_bytes = policy.encode(self.path, node)
        self.source_path_bytes = tuple(policy.encode(path, node) for path in paths)
        if any(b'\x00' in path for path in (self.path_bytes,) + self.source_path_bytes):
            raise Unsupported(node, 'cat path encoding contains NUL')
        self.encoding = policy.encoding


class CatRecord(Node):
    def __init__(self, request):
        super(CatRecord, self).__init__(request)
        self.request = request
        self.status, self.phase, self.error = 'PENDING', 'open', None
        self.opened, self.closed = False, False
        self.active_source = None
        self.reads = []  # ordered (absolute path, byte count), including duplicates
        self.bytes_read, self.bytes_written = 0, 0
        self.close_error = None
        self.message = None


class CatRuntime(object):
    def __init__(self, policy, reader=None, writer=None):
        self.policy = policy
        self.reader = reader if reader is not None else ArtifactBackend()
        self.writer = writer if writer is not None else TextWriter()

    def execute(self, node, scope, python, cwd, records):
        request = CatRequest(node, scope, python, cwd, self.policy)
        record = CatRecord(request)
        records.append(record)
        session = None
        try:
            session = self.writer.open_bytes(request)
            record.opened = True
            for path in request.paths:
                record.phase, record.active_source = 'read', path
                chunks = []
                for chunk in self.reader.chunks(path):
                    if type(chunk) is not bytes:
                        raise OSError('cat reader must supply bytes')
                    chunks.append(chunk)
                # Historical readlines completes one entire file before any
                # of its data is written. This also preserves self-append.
                data = b''.join(chunks)
                record.reads.append((path, len(data)))
                record.bytes_read += len(data)
                record.phase = 'write'
                session.write(data)
                record.bytes_written += len(data)
            record.phase = 'close'
            session.close()
            record.closed = True
        except (ArtifactUnavailable, NotImplementedError) as error:
            record.status = 'BLOCKED'
            record.error = Unsupported(node, str(error))
        except Exception as error:
            record.status = 'FAILED'
            record.error = SemanticError(node, 'cat ' + record.phase + ' failed: ' + str(error))
        finally:
            if session is not None and not record.closed and record.phase != 'close':
                # Close resources on error without claiming rollback. Upstream
                # relies on object lifetime on some exceptional exits.
                try:
                    session.close()
                    record.closed = True
                except Exception as error:
                    record.close_error = str(error)
        if record.error is not None:
            raise record.error
        record.status, record.phase = 'COMPLETED', 'complete'
        # Historical msg_info presentation only; no host console/log operation.
        record.message = 'Concatenated files into "' + request.path + '"'
