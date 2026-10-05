"""Bounded action routing and entry, not an archive-format implementation.

Evidence: Action.action_run/action_find/action_ftype, Filetype.ft_detect,
Scope.get_build_recdict. Effects require explicit trusted capabilities.
"""
import posixpath

from aap_frontend import FrontendError
from .directories import ExecutionDirectoryState
from .model import Node
from .diagnostics import SemanticError, Unsupported
from .scopes import Scope
from .values import var2string, UnavailableValue
from .command_items import items
from .lowering import lower_body
from .nested_update import UpdateStopped


class ActionWorkspace(object):
    """Explicit directory entry and file-kind/type observations; never chdir.

    prepare_directory must ensure the directory exists and can be entered,
    returning the observed absolute cwd, or raise OSError. It must not report success merely from an intended path.
    filetype supplies an observed result for detection beyond known suffixes;
    None means positively unrecognized, not an unavailable observation.
    """
    def prepare_directory(self, path):
        raise NotImplementedError('action work-directory capability unavailable')

    def file_kind(self, path):
        raise NotImplementedError('action file-kind observation unavailable')

    def filetype(self, path):
        raise NotImplementedError('extended filetype detection unavailable: ' + path)


class MemoryActionWorkspace(ActionWorkspace):
    def __init__(self, files=None, filetypes=None):
        self.files = dict(files or {})
        self.directories = set()
        self.filetypes = dict(filetypes or {})
        self.operations = []

    def prepare_directory(self, path):
        if not posixpath.isabs(path):
            raise ValueError('action directory requires an absolute path')
        directory = posixpath.normpath(path)
        ancestor = directory
        while ancestor != '/':
            if ancestor in self.files:
                raise OSError('action directory is a file: ' + ancestor)
            ancestor = posixpath.dirname(ancestor)
        self.directories.add(directory)
        self.operations.append(('directory', path))
        return directory

    def file_kind(self, path):
        return ('directory' if path in self.directories else
                'file' if path in self.files else 'missing')

    def filetype(self, path):
        if path not in self.filetypes:
            return super(MemoryActionWorkspace, self).filetype(path)
        return self.filetypes[path]


class ActionRequest(Node):
    def __init__(self, origin, definition, filename, attributes, filetype,
                 targettype, cwd, caller):
        super(ActionRequest, self).__init__(origin)
        self.definition = definition
        self.body = definition.body
        self.filename, self.attributes = filename, dict(attributes)
        self.name, self.filetype = definition.name, filetype
        self.directory, self.caller = ExecutionDirectoryState(cwd), caller
        self.graph, self.declarations = caller.graph, caller.declarations
        self.invocation_scope = caller.scope
        self.scope = Scope.build(definition.scope, caller.scope, keep_current_scope=True)
        source = var2string([filename], origin)
        # Dictlist.expand_item quote_aap retains attributes. Stable spelling
        # replaces Python 2 dictionary iteration order, with identical values.
        for key in sorted(attributes):
            source += '{' + key + '=' + str(attributes[key]) + '}'
        self.scope.local.update({'source': source, 'fname': var2string([filename], origin),
                                 'filetype': filetype, 'targettype': targettype,
                                 'action': definition.name, 'name': definition.name,
                                 'DEFER_ACTION_NAME': None, '_dirstack': [], '_prevdir': None,
                                 'recipe_name': definition.span.source_id,
                                 'recipe_lnum': definition.span.start.line})

    @property
    def cwd(self):
        return self.directory.current


class ActionResult(object):
    def __init__(self, request):
        self.request = request
        self.status = None
        self.reason = None
        self.error = None
        self.blocked_at = None
        self.span = request.body.span
        self.program = None
        self.evaluation = None
        self.nested_result = None


class ActionStopped(Exception):
    def __init__(self, result):
        super(ActionStopped, self).__init__(result.reason)
        self.result = result


class ActionBackend(object):
    """Trusted action capability, never a callable exposed to recipe Python.

    Return ActionResult for this exact request. COMPLETED certifies that the
    selected definition was performed, not that an inferred output exists.
    Tests may supply a recorded outcome; no real extraction adapter is enabled.
    Raise OSError for a failed effect, NotImplementedError for missing capability.
    """
    def execute(self, request):
        raise NotImplementedError('action execution capability unavailable')


class SemanticActionBackend(ActionBackend):
    """Re-enter the ordinary frontend/evaluator. Unsupported commands block."""
    def execute(self, request):
        from .evaluator import Evaluator
        result = ActionResult(request)
        caller = request.caller
        evaluator = None
        try:
            result.program = lower_body(request.body)
            # The historical bounded action re-entry has not enabled these
            # filesystem commands. Keep that boundary while sharing all
            # adapters that the action body already received.
            action_caps = caller.capabilities.replace(
                path_observer=caller.path_observer, tree_filesystem=None,
                move_backend=None, copy_backend=None)
            evaluator = Evaluator(
                request.scope, declarations=request.declarations, graph=request.graph,
                cwd=request.cwd, capabilities=action_caps,
                update_driver=caller.update_driver, execution_context=request)
            evaluation = evaluator.run(result.program, source_active=False)
            if evaluation.complete:
                result.status, result.reason = 'COMPLETED', 'action_body_completed'
            else:
                result.status, result.reason = 'BLOCKED', 'unsupported_action_operation'
                result.blocked_at = evaluation.halted_at
                result.span = evaluation.halted_at.span
        except (UpdateStopped, ActionStopped) as stopped:
            cause = stopped.result
            result.nested_result = cause
            result.status, result.reason = cause.status, cause.reason
            result.error, result.span, result.blocked_at = cause.error, cause.span, cause.blocked_at
        except Unsupported as error:
            result.status, result.reason = 'BLOCKED', 'unsupported_action_semantics'
            result.error, result.span = error, error.span
            result.blocked_at = evaluator.active_node if evaluator is not None else None
        except FrontendError as error:
            result.status, result.reason = 'FAILED', 'action_semantic_error'
            result.error, result.span = error, error.span
        finally:
            if evaluator is not None:
                result.evaluation = evaluator.last_result
        return result


class ActionRuntime(object):
    # Filetype.py suffix table; registration overrides (including removal) win.
    ARCHIVE_SUFFIXES = {'tar': 'tar', 'tar.gz': 'targz', 'tgz': 'targz',
                        'tar.bz2': 'tarbz2', 'zip': 'zip'}

    def __init__(self, workspace=None, backend=None):
        self.workspace = workspace if workspace is not None else ActionWorkspace()
        self.backend = backend if backend is not None else SemanticActionBackend()
        self.active = []

    def detect(self, filename, attrs, evaluator, origin):
        node = evaluator.graph.find_node(filename, evaluator.cwd)
        node_attrs = node.attributes if node is not None else {}
        for key in node_attrs:
            if key.startswith(('var_', 'add_')):
                raise Unsupported(origin, 'action node variable attributes are deferred')
        for mapping, key in ((attrs, 'filetype'), (node_attrs, 'filetype'),
                             (attrs, 'filetypehint')):
            if key in mapping:
                value = mapping[key]
                if value is not None and type(value) is not str:
                    raise Unsupported(origin, 'action filetype must be a string')
                return value
        kind = self.workspace.file_kind(filename)
        if kind not in ('file', 'missing', 'directory'):
            raise ValueError('invalid file-kind observation')
        if kind == 'directory':
            return 'directory'
        suffixes = dict(self.ARCHIVE_SUFFIXES)
        for registration in evaluator.declarations.suffix_history:
            if registration.filetype == 'remove':
                suffixes.pop(registration.suffix, None)
            else:
                suffixes[registration.suffix] = registration.filetype
        name = posixpath.basename(filename)
        index = name.find('.')
        while index > 0 and index < len(name) - 1:
            suffix = name[index + 1:]
            if suffix in suffixes:
                if suffixes[suffix] == 'ignore':
                    raise Unsupported(origin, 'ignored-suffix recursive detection is deferred')
                return suffixes[suffix]
            index = name.find('.', index + 1)
        value = self.workspace.filetype(filename)
        if value is not None and type(value) is not str:
            raise ValueError('invalid filetype observation')
        return value

    def invoke(self, name, filename, attrs, cwd, caller, origin, records):
        if self.active:
            raise Unsupported(origin, 'recursive action entry is deferred')
        if name != 'extract':
            raise Unsupported(origin, 'action invocation outside bounded extract entry: ' + name)
        if caller is None:
            raise Unsupported(origin, 'action entry requires the live evaluator context')
        definitions = caller.declarations.actions.get(name, ())
        if not definitions:
            raise SemanticError(origin, 'unknown action: ' + name)
        filetype = self.detect(filename, attrs, caller, origin) or 'default'
        candidates = [filetype]
        if '_' in filetype and not filetype.startswith('_'):
            candidates.append(filetype.split('_', 1)[0])
        candidates.append('default')
        definition = None
        # Current registration subset has only default output types. Do not
        # guess an output type for selection or add action chaining.
        for candidate in candidates:
            definition = caller.declarations.latest_action(name, candidate)
            if definition is not None:
                break
        if definition is None:
            raise SemanticError(origin, 'no commands defined for extract from ' + filetype)
        targettype = None
        target = caller.scope.local.get('target')
        if target:
            if type(target) is not str:
                raise Unsupported(origin, 'action target conversion is deferred')
            parsed = items(target, origin, label='action target')
            if parsed:
                target_name, target_attrs = parsed[0]
                try:
                    targettype = self.detect(posixpath.normpath(posixpath.join(cwd, target_name)),
                                             target_attrs, caller, origin)
                except NotImplementedError as error:
                    # All selectable definitions have default output type;
                    # unavailable detection cannot alter routing. Reading the
                    # value still blocks, rather than inventing None.
                    targettype = UnavailableValue(str(error))
        request = ActionRequest(origin, definition, filename, attrs, filetype,
                                targettype, cwd, caller)
        result = ActionResult(request)
        records.append(result)
        try:
            self.active.append(request)
            try:
                observed = self.backend.execute(request)
            finally:
                self.active.pop()
            if (not isinstance(observed, ActionResult) or observed.request is not request
                    or observed.status not in ('COMPLETED', 'BLOCKED', 'FAILED')):
                raise ValueError('invalid action backend outcome')
            result = observed
            records[-1] = result
        except NotImplementedError as error:
            result.status, result.reason = 'BLOCKED', 'action_capability_unavailable'
            result.error = Unsupported(origin, str(error))
            result.span = result.error.span
        except Unsupported as error:
            result.status, result.reason = 'BLOCKED', 'unsupported_action_semantics'
            result.error, result.span = error, error.span
        except FrontendError as error:
            result.status, result.reason = 'FAILED', 'action_semantic_error'
            result.error, result.span = error, error.span
        except (OSError, ValueError) as error:
            result.status, result.reason = 'FAILED', 'action_backend_failed'
            result.error = SemanticError(origin, str(error))
            result.span = result.error.span
        if result.status != 'COMPLETED':
            raise ActionStopped(result)
        return result
