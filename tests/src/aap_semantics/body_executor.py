"""Enter one planned dependency body through the existing evaluator.

This is semantic execution, not a build driver or signature commit protocol.
It never traverses prerequisites, writes persistent state, or marks nodes done.
"""
import posixpath

from aap_frontend import FrontendError
from .model import Node
from .planner import BodyPlan
from .scopes import Scope
from .values import UnavailableValue, var2string
from .diagnostics import Unsupported
from .lowering import lower_body
from .evaluator import Evaluator
from .nested_update import UpdateStopped
from .actions import ActionStopped
from .directories import ExecutionDirectoryState
from .capabilities import RuntimeCapabilities


def _short_name(node, cwd, origin):
    name = node.identity
    if not posixpath.isabs(name):
        return name
    if name == cwd:
        raise Unsupported(origin, 'directory identity shortening requires state observation')
    # Util.shorten_name retains absolute names when only the root is shared.
    common = 0
    for left, right in zip(name.split('/'), cwd.split('/')):
        if left != right:
            break
        common += 1
    return posixpath.relpath(name, cwd) if common > 1 else name


class BuildItem(object):
    def __init__(self, item, cwd):
        self.item = item
        self.node = item.node
        self.name = _short_name(item.node, cwd, item)
        self.attributes = dict(item.node.attributes)
        self.attributes.update(item.attributes)  # occurrence attributes win
        if any(key != 'virtual' for key in self.attributes):
            raise Unsupported(item, 'build item attributes beyond virtual are deferred')

    def text(self):
        value = var2string([self.name], self.item)
        if 'virtual' in self.attributes:
            value += '{virtual=' + str(self.attributes['virtual']) + '}'
        return value


class BuildExecutionContext(Node):
    def __init__(self, step, invocation_scope, graph, declarations,
                 process_backend=None, process_policy=None, include_loader=None,
                 prepared_buildcheck=None, checksum_backend=None, update_driver=None,
                 port_runtime=None, path_observer=None, output_runtime=None, cat_runtime=None,
                 tree_filesystem=None, move_backend=None, copy_backend=None,
                 capabilities=None):
        if not isinstance(step, BodyPlan):
            raise TypeError('body execution requires a selected BodyPlan')
        super(BuildExecutionContext, self).__init__(step)
        self.step = step
        self.target = step.target
        self.definition = step.definition
        self.body = step.body
        self.definition_scope = step.scope
        self.invocation_scope = invocation_scope
        self.scope = Scope.build(self.definition_scope, invocation_scope)
        self.directory = ExecutionDirectoryState(step.cwd)
        self.graph = graph
        self.declarations = declarations
        self.capabilities = (capabilities if capabilities is not None else
            RuntimeCapabilities(process_backend, process_policy, include_loader,
                checksum_backend, port_runtime, path_observer, output_runtime,
                cat_runtime, tree_filesystem, move_backend, copy_backend))
        caps = self.capabilities
        self.process_backend = caps.process_backend
        self.process_policy = caps.process_policy
        self.include_loader = caps.include_loader
        self.checksum_backend = caps.checksum_backend
        self.update_driver = update_driver
        self.port_runtime = caps.port_runtime
        self.path_observer = caps.path_observer
        self.output_runtime = caps.output_runtime
        self.cat_runtime = caps.cat_runtime
        self.tree_filesystem = caps.tree_filesystem
        self.move_backend = caps.move_backend
        self.copy_backend = caps.copy_backend
        # An explicit prepared observation, not a hash of the deferred source.
        self.prepared_buildcheck = prepared_buildcheck
        self.entered = False
        self.target_items = tuple(BuildItem(i, self.cwd) for i in self.definition.target_items)
        self.depend_items = tuple(BuildItem(i, self.cwd) for i in self.definition.source_items)
        self.source_items = tuple(i for i in self.depend_items if not i.node.virtual)
        local = self.scope.local
        local.update({'buildtarget': self.target.identity, 'match': '',
                      '_really_build': 1, '_dirstack': [], '_prevdir': None,
                      'recipe_name': self.span.source_id,
                      'recipe_lnum': self.span.start.line})
        for name, items in (('target', self.target_items), ('depend', self.depend_items),
                            ('source', self.source_items)):
            local[name] = ' '.join(i.text() for i in items)
            local[name + '_list'] = [i.name for i in items]
            local[name + '_dl'] = UnavailableValue('build dictlist value is deferred: ' + name + '_dl')
        local['fname'] = var2string([self.source_items[0].name], self) if self.source_items else ''

    @property
    def cwd(self):
        return self.directory.current


def _declarations(state):
    return (tuple((name, tuple(state.actions[name])) for name in sorted(state.actions)),
            tuple(state.suffix_history), tuple(sorted(state.filetypes)))


class BodyExecutionResult(object):
    def __init__(self, context):
        self.context = context
        self.target = context.target
        self.definition = context.definition
        self.body = context.body
        self.scope = context.scope
        self.program = None
        self.evaluation = None
        self.status = None  # COMPLETED, BLOCKED, FAILED
        self.reason = None
        self.blocked_at = None
        self.error = None
        self.span = context.body.span
        self.processes = ()
        self.checksums = ()
        self.updates = ()
        self.port_operations = ()
        self.directory_changes = ()
        self.tree_records = ()
        self.moves = ()
        self.copies = ()
        self.update_failure = None
        self.action_failure = None
        self.added_definitions = ()
        self.graph_changed = False
        self.declarations_changed = False
        self.post_execution_recheck_required = False
        self.target_updated = False
        self.persistence_performed = False


class BodyExecutor(object):
    def execute(self, context):
        """Enter once; reached :update delegates to the injected driver."""
        result = BodyExecutionResult(context)
        step = context.step
        guard = None
        if context.entered:
            guard = 'context_already_entered'
        elif step.mode != 'update' or step.after:
            guard = 'plan_recheck_required'
        elif getattr(step, 'graph_snapshot', None) != context.graph.snapshot():
            guard = 'graph_changed_since_plan'
        elif step.buildcheck_required and type(context.prepared_buildcheck) is not str:
            guard = 'buildcheck_unavailable'
        if guard:
            result.status, result.reason = 'BLOCKED', guard
            return result
        context.entered = True
        graph_before = context.graph.snapshot()
        count = len(context.graph.definitions)
        declarations_before = _declarations(context.declarations)
        evaluator = None
        try:
            result.program = lower_body(context.body)
            evaluator = Evaluator(context.scope, declarations=context.declarations,
                                  graph=context.graph, cwd=context.cwd,
                                  capabilities=context.capabilities,
                                  update_driver=context.update_driver,
                                  execution_context=context)
            # The defining recipe finished reading before build entry. Its
            # source identity is diagnostic provenance, not an active include.
            evaluation = evaluator.run(result.program, source_active=False)
            if evaluation.complete:
                result.status, result.reason = 'COMPLETED', 'semantic_body_completed'
            else:
                result.status, result.reason = 'BLOCKED', 'unsupported_operation'
                result.blocked_at = evaluation.halted_at
                result.span = evaluation.halted_at.span
        except ActionStopped as stopped:
            action = stopped.result
            result.status, result.reason = action.status, action.reason
            result.action_failure = action
            result.error, result.span, result.blocked_at = action.error, action.span, action.blocked_at
        except UpdateStopped as stopped:
            update = stopped.result
            result.status, result.reason = update.status, update.reason
            result.update_failure = update
            result.error, result.span = update.error, update.span
            result.blocked_at = update.blocked_at
        except Unsupported as error:
            result.status, result.reason = 'BLOCKED', 'unsupported_semantics'
            result.error, result.span = error, error.span
            result.blocked_at = evaluator.active_node if evaluator is not None else None
        except FrontendError as error:
            result.status, result.reason = 'FAILED', 'semantic_error'
            result.error, result.span = error, error.span
        finally:
            if evaluator is not None:
                result.evaluation = evaluator.last_result
                if result.evaluation is not None:
                    result.processes = tuple(result.evaluation.processes)
                    result.checksums = tuple(result.evaluation.checksums)
                    result.updates = tuple(result.evaluation.updates)
                    result.port_operations = tuple(result.evaluation.port_operations)
                    result.directory_changes = tuple(result.evaluation.directory_changes)
                    result.tree_records = tuple(result.evaluation.tree_records)
                    result.moves = tuple(result.evaluation.moves)
                    result.copies = tuple(result.evaluation.copies)
            result.added_definitions = tuple(context.graph.definitions[count:])
            result.graph_changed = context.graph.snapshot() != graph_before
            result.declarations_changed = _declarations(context.declarations) != declarations_before
            # Even a partial execution may have changed shared metadata or
            # observed processes. This is an obligation, never update success.
            result.post_execution_recheck_required = True
        return result
