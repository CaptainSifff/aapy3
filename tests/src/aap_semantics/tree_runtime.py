"""Bounded Linux :tree traversal over explicit directory observations."""
import posixpath
import re

from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .expansion import expand_text, render_value
from .lowering import lower_body
from .values import MISSING, _quote_item


class TreeRequest(object):
    def __init__(self, operation, path, cwd, origin):
        self.operation, self.argument, self.cwd = operation, path, cwd
        self.path = path if posixpath.isabs(path) else posixpath.join(cwd, path)
        self.source, self.span = origin.source, origin.span


class TreeObservation(object):
    """ENTRIES preserves listdir order; SYMLINK carries its stat target kind."""
    def __init__(self, status, entries=(), target_kind=None, detail=None):
        self.status = status
        self.entries = tuple(entries)
        self.target_kind = target_kind
        self.detail = detail


class TreeFilesystem(object):
    def list_directory(self, request):
        return TreeObservation('UNAVAILABLE')

    def classify(self, request):
        return TreeObservation('UNAVAILABLE')


class MemoryTreeFilesystem(TreeFilesystem):
    """Exact path facts; no host stat, listdir or implicit directory creation."""
    def __init__(self, directories=None, kinds=None):
        self.directories = dict(directories or {})
        self.kinds = dict(kinds or {})
        self.requests = []

    def list_directory(self, request):
        self.requests.append(request)
        return self.directories.get(request.path, TreeObservation('UNAVAILABLE'))

    def classify(self, request):
        self.requests.append(request)
        return self.kinds.get(request.path, TreeObservation('UNAVAILABLE'))


class TreeRecord(object):
    def __init__(self, request, observation, matched=False):
        self.request, self.observation, self.matched = request, observation, matched
        self.warning = (('Cannot read directory "' + request.argument + '"')
                        if request.operation == 'list' and observation.status in
                        ('UNREADABLE', 'MISSING') else None)


class TreeRuntime(object):
    def __init__(self, filesystem=None):
        self.filesystem = filesystem if filesystem is not None else TreeFilesystem()

    def _observe(self, operation, path, evaluator, origin, result):
        request = TreeRequest(operation, path, evaluator.cwd, origin)
        try:
            observation = (self.filesystem.list_directory(request) if operation == 'list'
                           else self.filesystem.classify(request))
        except NotImplementedError as error:
            observation = TreeObservation('UNAVAILABLE', detail=str(error))
        except Exception as error:
            observation = TreeObservation('ERROR', detail=str(error))
        allowed = (('ENTRIES', 'UNREADABLE', 'MISSING', 'UNAVAILABLE', 'ERROR')
                   if operation == 'list' else
                   ('FILE', 'DIRECTORY', 'SYMLINK', 'MISSING', 'UNAVAILABLE', 'ERROR'))
        if not isinstance(observation, TreeObservation) or observation.status not in allowed:
            observation = TreeObservation('ERROR', detail='invalid tree observation')
        if observation.status == 'ENTRIES' and any(
                type(name) is not str or not name or '/' in name or name in ('.', '..')
                for name in observation.entries):
            observation = TreeObservation('ERROR', detail='invalid directory entry')
        if observation.status == 'SYMLINK' and observation.target_kind not in (
                'FILE', 'DIRECTORY', 'MISSING'):
            observation = TreeObservation('ERROR', detail='invalid symlink target kind')
        result.tree_records.append(TreeRecord(request, observation))
        if observation.status == 'UNAVAILABLE':
            raise Unsupported(origin, 'tree ' + operation + ' unavailable: ' + path)
        if observation.status == 'ERROR':
            raise SemanticError(origin, 'tree ' + operation + ' failed: ' + path
                                + ' (' + str(observation.detail) + ')')
        return observation

    def execute(self, node, evaluator, result):
        if node.body is None:
            raise Unsupported(node, ':tree without a body is deferred')
        raw = render_value(node.arguments, evaluator.python)
        expanded = expand_text(raw, evaluator.scope, node, item_attributes=True)
        parsed = items(expanded, node, label='tree')
        if len(parsed) != 1 or not parsed[0][0]:
            raise SemanticError(node, ':tree requires one directory name')
        root, attrs = parsed[0]
        if (set(attrs) not in (set(('filename',)), set(('filename', 'reject')))
                or type(attrs['filename']) is not str):
            raise Unsupported(node, 'only :tree filename and reject attributes are supported')
        if 'reject' in attrs and type(attrs['reject']) is not str:
            raise Unsupported(node, ':tree reject attribute must be a string')
        if not attrs['filename']:
            raise Unsupported(node, 'empty :tree filename pattern is deferred')
        if not root or '\x00' in root:
            raise SemanticError(node, 'invalid :tree directory name')
        if root.startswith('~'):
            raise Unsupported(node, ':tree user-directory expansion is deferred')
        if not posixpath.isabs(root):
            if type(evaluator.cwd) is not str or not posixpath.isabs(evaluator.cwd):
                raise Unsupported(node, ':tree requires an explicit absolute cwd')
        try:
            pattern = re.compile('^(' + attrs['filename'] + ')$')
        except Exception as error:
            raise SemanticError(node, 'invalid :tree filename pattern: ' + str(error))
        reject = None
        if attrs.get('reject'):
            try:
                reject = re.compile('^(' + attrs['reject'] + ')$')
            except Exception as error:
                raise SemanticError(node, 'invalid :tree reject pattern: ' + str(error))
        self._recurse(root, pattern, reject, node, evaluator, result)

    def _recurse(self, directory, pattern, reject, node, evaluator, result):
        listing = self._observe('list', directory, evaluator, node, result)
        # Upstream warns and returns on os.listdir failure. The record is the
        # controlled warning observation; no body executes for that directory.
        if listing.status in ('UNREADABLE', 'MISSING'):
            return
        for basename in listing.entries:
            path = posixpath.join(directory, basename)
            kind = self._observe('classify', path, evaluator, node, result)
            if kind.status == 'MISSING':
                continue
            effective = kind.target_kind if kind.status == 'SYMLINK' else kind.status
            if effective == 'DIRECTORY' and kind.status != 'SYMLINK':
                self._recurse(path, pattern, reject, node, evaluator, result)
            # This bounded filename/reject subset invokes bodies only for files.
            if (effective != 'FILE' or pattern.match(basename) is None
                    or (reject is not None and reject.match(basename) is not None)):
                continue
            result.tree_records[-1].matched = True
            # Process reparses the saved command string for each match and
            # writes name into the current build dictionary. Its restoration
            # occurs only after Process returns normally.
            previous = evaluator.scope.lookup('name')
            evaluator.scope.store('name', _quote_item(path), node)
            body = lower_body(node.body)
            evaluator.prepare(body)
            evaluator.statements(body, result)
            if previous is None or previous is MISSING:
                del evaluator.scope.local['name']
            else:
                evaluator.scope.store('name', previous, node)
