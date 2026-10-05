"""A-A-P print events and bounded local redirected output; no host I/O."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text


def token(text, index):
    """Util.get_token: horizontal whitespace or a quote-preserving token."""
    start = index
    if text[index] in ' \t':
        while index < len(text) and text[index] in ' \t':
            index += 1
    else:
        quote = None
        while index < len(text):
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char in ' \t':
                break
            index += 1
    return text[start:index], index


def print_parts(raw, origin, label='print'):
    """Commands._get_redir token ordering, before dollar expansion.

    Returns unexpanded message, filename and mode. No shell interpretation.
    """
    index, message, filename, mode = 0, '', None, 'stdout'
    while index < len(raw):
        part, index = token(raw, index)
        if index == len(raw) and part[0] in ' \t':
            break
        if not message or part[0] in ' \t':
            if not message:
                nextpart, part = part, ''
            else:
                nextpart, index = token(raw, index)
            if nextpart.startswith('>'):
                if mode != 'stdout':
                    raise SemanticError(origin, 'redirection appears twice')
                prefix = nextpart[:2] if nextpart.startswith(('>!', '>>')) else '>'
                mode = {'>!': 'overwrite', '>>': 'append', '>': 'create'}[prefix]
                filename = nextpart[len(prefix):]
                if not filename:
                    if index < len(raw):
                        unused, index = token(raw, index)
                    if index == len(raw):
                        raise SemanticError(origin, 'missing filename after ' + prefix)
                    filename, index = token(raw, index)
                if not message and index < len(raw):
                    unused, index = token(raw, index)
            elif nextpart.startswith('|'):
                raise Unsupported(origin, 'A-A-P ' + label + ' pipelines are deferred')
            else:
                message += part + nextpart
        else:
            message += part
    return message, filename, mode


def destination(raw, scope, origin, cwd, details=False, label='print'):
    value = expand_text(raw, scope, origin)
    # Util.unquote, without shell escapes or splitting expansion-created spaces.
    path, quote = '', None
    for char in value:
        if quote == char:
            quote = None
        elif not quote and char in "'\"":
            quote = char
        else:
            path += char
    if quote:
        raise Unsupported(origin, 'unmatched destination quoting is deferred')
    if not path or '\x00' in path:
        raise SemanticError(origin, label + ' destination must be nonempty and NUL-free')
    if any(c in path for c in '~*?[]{}') or ':' in path:
        raise Unsupported(origin, label + ' destination glob/tilde/URL/attribute forms are deferred')
    expanded = path
    if not posixpath.isabs(path):
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(origin, 'redirected ' + label + ' requires an explicit absolute cwd')
        path = posixpath.join(cwd, path)
    return (path, expanded) if details else path


class OutputPolicy(object):
    def __init__(self, encoding, logging=None):
        self.encoding, self.logging = encoding, logging

    def encode(self, text, origin):
        try:
            return text.encode(self.encoding, 'strict')
        except (UnicodeError, LookupError, TypeError) as error:
            raise SemanticError(origin, 'output encoding error: ' + str(error))


class PrintRequest(Node):
    def __init__(self, origin, raw, text, mode, path, cwd, policy):
        super(PrintRequest, self).__init__(origin)
        self.raw, self.text, self.destination = raw, text, mode
        self.path, self.cwd = path, cwd
        self.output_text = text + '\n' if mode == 'stdout' or not text.endswith('\n') else text
        self.data = policy.encode(self.output_text, origin)
        self.encoding = policy.encoding
        self.path_bytes = policy.encode(path, origin) if path is not None else None
        if self.path_bytes is not None and b'\x00' in self.path_bytes:
            raise Unsupported(origin, 'output destination encoding contains NUL')
        # msg_print always prints, independently of MESSAGE quiet settings.
        self.log_type = 'print' if mode == 'stdout' else None
        self.log_text = text if mode == 'stdout' else None


class OutputResult(object):
    def __init__(self, status='COMPLETED', detail=None):
        self.status, self.detail = status, detail


class PrintRecord(Node):
    def __init__(self, request):
        super(PrintRecord, self).__init__(request)
        self.request = request
        self.status, self.error = 'PENDING', None


class MemoryOutputSink(object):
    """Capture terminal events; never use the embedding process's stdout."""
    def __init__(self):
        self.events = []

    def emit(self, request):
        self.events.append(request)
        return OutputResult()


class TextWriter(object):
    def open_bytes(self, request):
        """Open/truncate/create before cat reads. Return write(bytes)/close session.

        Missing capability raises NotImplementedError before effects. Actual I/O
        failures raise OSError; no rollback of earlier writes is promised.
        """
        raise NotImplementedError('byte output sessions unavailable')

    def write(self, request):
        raise NotImplementedError('print file writer unavailable')


class MemoryTextWriter(TextWriter):
    """Explicit writable-directory fixture, no process-inferred paths/effects.

    This fixture models no symlinks. Production adapters must honor exact path
    spelling and opening errors; no local adapter is enabled here.
    """
    def __init__(self, directories=(), files=None):
        self.directories = set(directories)
        self.files = dict(files or {})
        self.requests = []

    def open_bytes(self, request):
        self.requests.append(request)
        path = posixpath.normpath(request.path)
        if posixpath.dirname(path) not in self.directories or path in self.directories:
            raise OSError('output parent unavailable or destination is a directory: ' + path)
        if request.destination not in ('overwrite', 'append'):
            raise ValueError('unsupported byte write mode')
        if request.destination == 'overwrite' or path not in self.files:
            self.files[path] = b''
        return MemoryByteSession(self.files, path)

    def write(self, request):
        self.requests.append(request)
        path = posixpath.normpath(request.path)
        if posixpath.dirname(path) not in self.directories or path in self.directories:
            raise OSError('output parent unavailable or destination is a directory: ' + path)
        if request.destination == 'overwrite':
            self.files[path] = request.data
        elif request.destination == 'append':
            self.files[path] = self.files.get(path, b'') + request.data
        else:
            raise ValueError('unsupported print write mode')
        return OutputResult()


class MemoryByteSession(object):
    def __init__(self, files, path):
        self.files, self.path = files, path
        self.closed = False

    def write(self, data):
        if self.closed or type(data) is not bytes:
            raise OSError('invalid byte session write')
        self.files[self.path] += data

    def close(self):
        self.closed = True


class PrintRuntime(object):
    def __init__(self, policy, sink=None, writer=None):
        self.policy = policy
        self.sink = sink if sink is not None else MemoryOutputSink()
        self.writer = writer if writer is not None else TextWriter()

    def execute(self, node, scope, python, cwd, records):
        raw = render_value(node.arguments, python)
        message, filename, mode = print_parts(raw, node)
        if mode == 'create':
            raise Unsupported(node, 'non-clobber print redirection is deferred')
        if mode == 'stdout' and self.policy.logging is not False:
            raise Unsupported(node, 'ordinary print requires explicit unlogged output policy')
        path = destination(filename, scope, node, cwd) if filename is not None else None
        text = expand_text(message, scope, node)
        request = PrintRequest(node, raw, text, mode, path, cwd, self.policy)
        record = PrintRecord(request)
        records.append(record)
        try:
            outcome = self.sink.emit(request) if mode == 'stdout' else self.writer.write(request)
            if not isinstance(outcome, OutputResult) or outcome.status not in ('COMPLETED', 'BLOCKED', 'FAILED'):
                raise ValueError('invalid output capability result')
            if outcome.status == 'BLOCKED':
                raise NotImplementedError(outcome.detail or 'output capability unavailable')
            if outcome.status == 'FAILED':
                raise OSError(outcome.detail or 'output capability failed')
        except NotImplementedError as error:
            record.status = 'BLOCKED'
            record.error = Unsupported(node, str(error))
            raise record.error
        except Exception as error:
            record.status = 'FAILED'
            record.error = SemanticError(node, 'print output failed: ' + str(error))
            raise record.error
        record.status = 'COMPLETED'
