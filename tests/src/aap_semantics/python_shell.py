"""The audited shellheader/shellfooter :python form, with injected byte writes.

Process.get_block_lines historically inserts the block into the same Python
dictionary used by @ statements. This interpreter keeps that scope and uses
PythonEvaluator for ordinary expressions; it never obtains host file objects.
"""
import ast
import posixpath

from . import model as m
from .diagnostics import SemanticError, Unsupported


_NODES = frozenset(('Module', 'Assign', 'Expr', 'For', 'Name', 'Attribute',
                    'Call', 'Load', 'Store', 'Constant', 'Str'))


def _kind(node):
    return type(node).__name__


def _name_call(node):
    return _kind(node) == 'Call' and _kind(node.func) == 'Name'


def _method_call(node):
    return (_kind(node) == 'Call' and _kind(node.func) == 'Attribute'
            and _kind(node.func.value) == 'Name')


def _safe_scalar(node):
    kind = _kind(node)
    if kind in ('Constant', 'Str', 'Name'):
        return True
    if kind == 'Attribute':
        return _kind(node.value) == 'Name' and node.value.id == '_no' and node.attr in (
            'script', 'functions')
    return False


def _safe_statement(node):
    kind = _kind(node)
    if kind == 'Assign':
        value = node.value
        valid_value = _safe_scalar(value)
        if _name_call(value):
            if value.func.id in ('var2string', 'var2list'):
                valid_value = len(value.args) == 1 and _safe_scalar(value.args[0])
            elif value.func.id == 'open':
                valid_value = len(value.args) == 2 and all(
                    _safe_scalar(arg) for arg in value.args)
        return (len(node.targets) == 1 and _kind(node.targets[0]) == 'Name'
                and not node.targets[0].id.startswith('__')
                and valid_value)
    if kind == 'Expr':
        value = node.value
        return (_method_call(value) and value.func.attr in ('write', 'close')
                and len(value.args) == (1 if value.func.attr == 'write' else 0)
                and all(_safe_scalar(arg) for arg in value.args))
    if kind == 'For':
        return (_kind(node.target) == 'Name' and not node.target.id.startswith('__')
                and _kind(node.iter) == 'Name' and not node.orelse
                and all(_safe_statement(part) for part in node.body))
    return False


def approve(tree):
    """Closed syntax gate; unsupported blocks remain deferred operations."""
    for node in ast.walk(tree):
        if _kind(node) not in _NODES:
            return False
        if _kind(node) == 'Name' and node.id.startswith('__'):
            return False
        if _kind(node) == 'Attribute' and node.attr.startswith('__'):
            return False
        if _kind(node) == 'Call' and (node.keywords or getattr(node, 'starargs', None)
                                     or getattr(node, 'kwargs', None)):
            return False
    return all(_safe_statement(node) for node in tree.body)


class ShellWriteRequest(m.Node):
    def __init__(self, origin, path, cwd, mode, encoding):
        super(ShellWriteRequest, self).__init__(origin)
        self.path, self.cwd, self.mode = path, cwd, mode
        self.destination = 'overwrite' if mode == 'w' else 'append'
        self.encoding = encoding


class ShellWriteRecord(m.Node):
    def __init__(self, request, operation, data=None):
        super(ShellWriteRecord, self).__init__(request)
        self.request, self.operation, self.data = request, operation, data
        self.status, self.error = 'PENDING', None


class ShellFileHandle(object):
    def __init__(self, request, session, policy, records):
        self.request, self.session = request, session
        self.policy, self.records = policy, records
        self.closed = False

    def operation(self, name, origin, text=None):
        if self.closed:
            raise SemanticError(origin, 'operation on closed shell script file')
        if name == 'write':
            if type(text) is not str:
                raise SemanticError(origin, 'shell script write requires a string')
            data = self.policy.encode(text, origin)
        else:
            data = None
        record = ShellWriteRecord(self.request, name, data)
        self.records.append(record)
        try:
            if name == 'write':
                self.session.write(data)
            else:
                self.session.close()
                self.closed = True
        except NotImplementedError as error:
            record.status, record.error = 'BLOCKED', Unsupported(origin, str(error))
            raise record.error
        except Exception as error:
            record.status, record.error = 'FAILED', SemanticError(
                origin, 'shell script ' + name + ' failed: ' + str(error))
            raise record.error
        record.status = 'COMPLETED'
        return None


class ShellBlockRuntime(object):
    def __init__(self, python, output_runtime, cwd, records):
        self.python, self.output_runtime = python, output_runtime
        self.cwd, self.records = cwd, records

    def expression(self, node, origin):
        if _name_call(node) and node.func.id == 'open':
            return self.open_file(node, origin)
        if _method_call(node):
            receiver = self.python.scope.python_name(node.func.value.id, origin)
            if type(receiver) is not ShellFileHandle:
                raise Unsupported(origin, 'shell file method requires an opened file handle')
            args = [self.expression(arg, origin) for arg in node.args]
            return receiver.operation(node.func.attr, origin, args[0] if args else None)
        return self.python.value(node, origin)

    def open_file(self, node, origin):
        if 'open' in self.python.scope.local or 'open' in self.python.scope.namespaces:
            raise Unsupported(origin, 'shadowed shell script open is unsupported')
        path, mode = [self.expression(arg, origin) for arg in node.args]
        if type(path) is not str or not path or '\x00' in path:
            raise SemanticError(origin, 'shell script path must be a nonempty string without NUL')
        if mode not in ('w', 'a'):
            raise Unsupported(origin, 'shell script open mode is unsupported: ' + str(mode))
        if self.output_runtime is None:
            raise Unsupported(origin, 'shell script writer unavailable')
        if not posixpath.isabs(path):
            if type(self.cwd) is not str or not posixpath.isabs(self.cwd):
                raise Unsupported(origin, 'shell script path requires an absolute logical cwd')
            path = posixpath.join(self.cwd, path)
        request = ShellWriteRequest(origin, path, self.cwd, mode,
                                    self.output_runtime.policy.encoding)
        record = ShellWriteRecord(request, 'open')
        self.records.append(record)
        try:
            session = self.output_runtime.writer.open_bytes(request)
            if (session is None or not callable(getattr(session, 'write', None))
                    or not callable(getattr(session, 'close', None))):
                raise ValueError('invalid shell script writer session')
        except NotImplementedError as error:
            record.status, record.error = 'BLOCKED', Unsupported(origin, str(error))
            raise record.error
        except Exception as error:
            record.status, record.error = 'FAILED', SemanticError(
                origin, 'shell script open failed: ' + str(error))
            raise record.error
        record.status = 'COMPLETED'
        return ShellFileHandle(request, session, self.output_runtime.policy, self.records)

    def statements(self, body, origin):
        for node in body:
            self.python.step(origin)
            kind = _kind(node)
            if kind == 'Assign':
                value = self.expression(node.value, origin)
                target = node.targets[0]
                if type(value) is ShellFileHandle:
                    # An opaque Python file binding lives in the same build
                    # dictionary as @ assignments. Other metadata operations
                    # still reject it through the closed value checks.
                    if target.id in self.python.scope.namespaces:
                        raise Unsupported(origin, 'overwriting a scope binding is deferred')
                    self.python.scope.local[target.id] = value
                else:
                    self.python.assign(target, value, origin)
            elif kind == 'Expr':
                self.expression(node.value, origin)
            elif kind == 'For':
                for value in self.python.sequence(node.iter, origin):
                    self.python.step(origin)
                    self.python.assign(node.target, value, origin)
                    self.statements(node.body, origin)
            else:
                raise Unsupported(origin, 'unsupported shell Python statement: ' + kind)
