"""Commands.aap_cd: explicit execution-frame cwd, never host chdir."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text
from .command_items import items
from .port_commands import PortDirectories


class ExecutionDirectoryState(object):
    """One mutable cwd per execution frame; previous is a scope-local value.

    Includes share this frame. Dependency calls create a new frame. Helpers
    which historically save/restore cwd select their own explicit directory.
    """
    def __init__(self, current=None):
        self.current = current


class DirectoryChange(Node):
    def __init__(self, origin, cwd, previous):
        super(DirectoryChange, self).__init__(origin)
        self.before, self.previous_before = cwd, previous
        self.raw = self.expanded = self.path = self.requested = None
        self.components = ()
        self.after, self.previous_after = cwd, previous
        self.status = self.error = None


def change_directory(evaluator, node, records):
    scope, cwd = evaluator.scope, evaluator.cwd
    record = DirectoryChange(node, cwd, scope.local.get('_prevdir'))
    records.append(record)
    try:
        if node.body is not None:
            raise Unsupported(node, ':cd command bodies are not supported')
        record.raw = render_value(node.arguments, evaluator.python)
        record.expanded = expand_text(record.raw, scope, node)
        components = items(record.expanded, node, 'cd')
        if not components:
            raise SemanticError(node, ':cd requires at least one argument')
        if any(attrs for name, attrs in components):
            raise Unsupported(node, ':cd item attributes are deferred')
        if any(name.startswith('~') for name, attrs in components):
            raise Unsupported(node, ':cd tilde expansion requires an explicit home-directory policy')
        record.components = tuple(name for name, attrs in components)
        record.path = posixpath.join(*record.components)
        if record.path == '-':
            record.path = scope.local.get('_prevdir')
            if not record.path:
                raise SemanticError(node, 'No previous directory for :cd -')
            if type(record.path) is not str:
                raise SemanticError(node, ':cd previous directory must be a string')
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, ':cd requires an explicit absolute runtime cwd')
        # aap_chdir records getcwd BEFORE attempting chdir, including failures.
        scope.local['_prevdir'] = cwd
        record.previous_after = cwd
        if '\x00' in record.path or '\x00' in cwd:
            raise SemanticError(node, 'NUL in :cd path')
        # Do not normpath before observation: symlink/.. resolution belongs to
        # the capability. No glob, search path or automatic mkdir is involved.
        record.requested = posixpath.join(cwd, record.path)
        runtime = evaluator.port_runtime
        directories = runtime.commands.directories if runtime is not None else PortDirectories()
        observed = directories.enter(record.requested)
        if type(observed) is not str or not posixpath.isabs(observed) or '\x00' in observed:
            raise SemanticError(node, 'invalid :cd directory observation')
        evaluator.cwd = observed
        record.after, record.status = observed, 'COMPLETED'
    except NotImplementedError as error:
        record.status, record.error = 'BLOCKED', Unsupported(node, str(error))
        raise record.error
    except Unsupported as error:
        record.status, record.error = 'BLOCKED', error
        raise
    except SemanticError as error:
        record.status, record.error = 'FAILED', error
        raise
    except (OSError, ValueError) as error:
        record.status = 'FAILED'
        record.error = SemanticError(node, ':cd failed: ' + str(error))
        raise record.error
    return record
