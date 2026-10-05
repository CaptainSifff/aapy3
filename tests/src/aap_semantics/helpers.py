"""Closed call registry; recipes never obtain a host callable or module."""
import posixpath
import re as _re

from .diagnostics import SemanticError, Unsupported
from .values import RegexMatchValue, check_value, var2list, var2string
from .directories import ExecutionDirectoryState
from .path_observation import PathObservationRuntime


def _string(value, origin):
    if type(value) is not str:
        raise SemanticError(origin, 'helper requires a string')
    return value


class HelperRegistry(object):
    NAMES = frozenset(('var2string', 'var2list', 'string.find', 'int',
                       'os.path.dirname', 'os.path.basename', 'os.path.abspath',
                       're.sub', 're.search', 'os.path.exists'))
    METHODS = frozenset(('replace', 'join', 'extend'))
    DEFERRED = frozenset(('os.path.isdir',
                          'file2string', 'redir_system', 'os.rename'))

    def __init__(self, cwd=None, path_observer=None):
        # Path arithmetic uses the logical frame; observations use an injected capability.
        self.directory = ExecutionDirectoryState(cwd)
        self.paths = PathObservationRuntime(path_observer)

    @property
    def cwd(self):
        return self.directory.current

    @cwd.setter
    def cwd(self, value):
        self.directory.current = value

    def call(self, name, args, origin):
        if name not in self.NAMES:
            reason = 'deferred capability: ' if name in self.DEFERRED else 'unapproved call: '
            raise Unsupported(origin, reason + name)
        for arg in args:
            check_value(arg, origin)
        if name in ('var2string', 'var2list', 'int'):
            self.arity(args, 1, 1, origin)
            if name == 'var2string':
                return var2string(args[0], origin)
            if name == 'var2list':
                return var2list(args[0], origin)
            value = args[0]
            if type(value) is str:
                value = value.strip(' \t\r\n\v\f')
                digits = value[1:] if value[:1] in ('+', '-') else value
                if not digits or any(c not in '0123456789' for c in digits):
                    raise SemanticError(origin, 'int requires ASCII decimal text')
            elif type(value) not in (int, bool):
                raise SemanticError(origin, 'int requires a string or integer')
            return int(value)
        if name == 'string.find':
            self.arity(args, 2, 4, origin)
            text, sub = _string(args[0], origin), _string(args[1], origin)
            if any(type(arg) is not int for arg in args[2:]):
                raise SemanticError(origin, 'find indices must be integers')
            return text.find(sub, *args[2:])
        if name == 're.search':
            self.arity(args, 2, 2, origin)
            pattern, text = (_string(args[0], origin), _string(args[1], origin))
            try:
                match = _re.search(pattern, text)
            except _re.error as error:
                raise SemanticError(origin, 'invalid regular expression: ' + str(error))
            return RegexMatchValue() if match is not None else None
        if name == 'os.path.exists':
            self.arity(args, 1, 1, origin)
            return self.paths.exists(args[0], self.cwd, origin)
        if name.startswith('os.path.'):
            self.arity(args, 1, 1, origin)
            path = _string(args[0], origin)
            if name == 'os.path.dirname':
                return posixpath.dirname(path)
            if name == 'os.path.basename':
                return posixpath.basename(path)
            if not posixpath.isabs(path):
                if type(self.cwd) is not str or not posixpath.isabs(self.cwd):
                    raise Unsupported(origin, 'abspath needs an explicit absolute metadata cwd')
                path = posixpath.join(self.cwd, path)
            return posixpath.normpath(path)
        self.arity(args, 3, 3, origin)
        pattern, replacement, text = [_string(arg, origin) for arg in args]
        # Only the literal-pattern, literal-replacement subset is established
        # here (globals uses re.sub('-', '_', version)). No host regex dialect.
        if (not pattern or any(c in '.^$*+?{}[]\\|()' for c in pattern)
                or '\\' in replacement):
            raise Unsupported(origin, 're.sub supports literal patterns/replacements only')
        return text.replace(pattern, replacement)

    def method(self, receiver, name, args, origin):
        check_value(receiver, origin)
        for arg in args:
            check_value(arg, origin)
        if type(receiver) is str and name == 'replace':
            self.arity(args, 2, 3, origin)
            old, new = _string(args[0], origin), _string(args[1], origin)
            if len(args) == 3:
                if type(args[2]) is not int:
                    raise SemanticError(origin, 'replace count must be an integer')
                return receiver.replace(old, new, args[2])
            return receiver.replace(old, new)
        if type(receiver) is str and name == 'join':
            self.arity(args, 1, 1, origin)
            if type(args[0]) not in (list, tuple):
                raise SemanticError(origin, 'join requires a list or tuple of strings')
            return receiver.join(_string(item, origin) for item in args[0])
        if type(receiver) is list and name == 'extend':
            self.arity(args, 1, 1, origin)
            if type(args[0]) not in (list, tuple):
                raise SemanticError(origin, 'extend requires a list or tuple')
            pending = list(args[0])
            while pending:
                value = pending.pop()
                if value is receiver:
                    raise Unsupported(origin, 'extend would create a cyclic metadata value')
                if type(value) in (list, tuple):
                    pending.extend(value)
            receiver.extend(args[0])
            return None
        raise Unsupported(origin, 'unapproved method for value type: ' + name)

    @staticmethod
    def arity(args, minimum, maximum, origin):
        if not minimum <= len(args) <= maximum:
            raise SemanticError(origin, 'wrong number of compatibility helper arguments')
