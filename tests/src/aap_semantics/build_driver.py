"""One controlled invocation: plan, enter, observe, complete, explicitly flush.

No recipe command handlers or automatic port helper execution live here.
"""
from aap_frontend import FrontendError
from .body_executor import BuildExecutionContext, BodyExecutor
from .completion import PostExecutionRecheck, observe_file
from .planner import UpdatePlanner, BodyPlan
from .persistence import InvocationObservations
from .port_defaults import PortDefaults
from .diagnostics import Unsupported
from .nested_update import UpdateRequest, UpdateResult
from .scopes import Scope
from .buildcheck import BuildSignaturePreparer
from .buildcheck import BuildSignatureFailure
from .capabilities import RuntimeCapabilities


class BuildResult(object):
    def __init__(self):
        self.status = None
        self.reason = None
        self.plan = None
        self.bodies = []
        self.completions = []
        self.completed = []
        self.blocked_at = None
        self.error = None
        self.span = None
        self.persistence_written = False
        self.pending_signatures = ()


class BuildDriver(object):
    def __init__(self, graph, state, persistence, scope, declarations,
                 cwd=None, port_defaults=True, executor=None, max_bodies=10000,
                 process_backend=None, process_policy=None, include_loader=None,
                 checksum_backend=None, port_runtime=None, path_observer=None, output_runtime=None, cat_runtime=None,
                 tree_filesystem=None,
                 move_backend=None,
                 copy_backend=None,
                 buildcheck_encoding=None, capabilities=None):
        self.capabilities = (capabilities if capabilities is not None else
            RuntimeCapabilities(process_backend, process_policy, include_loader,
                checksum_backend, port_runtime, path_observer, output_runtime,
                cat_runtime, tree_filesystem, move_backend, copy_backend))
        self.graph, self.scope, self.declarations = graph, scope, declarations
        encoding = buildcheck_encoding
        if encoding is None and self.capabilities.process_policy is not None:
            encoding = self.capabilities.process_policy.encoding
        preparer = BuildSignaturePreparer(encoding) if encoding is not None else None
        self.observations = InvocationObservations(state, persistence, preparer)
        self.persistence = persistence
        self.cwd = cwd if cwd is not None else graph.base_directory
        self.executor = executor if executor is not None else BodyExecutor()
        self.recheck = PostExecutionRecheck()
        self.completed = {}  # absolute node path -> completion reason, per run
        self.terminal = None
        self.closed = False
        self.initialized = False
        self.port_defaults = port_defaults
        self.port = None
        self.port_runtime = self.capabilities.port_runtime
        self.max_bodies = max_bodies
        self.body_count = 0
        self.active_paths = ()
        self.active_definitions = ()

    def _stop(self, result, status, reason, error=None, blocked_at=None, span=None):
        result.status, result.reason = status, reason
        result.error, result.blocked_at = error, blocked_at
        origin = blocked_at or error
        if not hasattr(origin, 'span'):
            if result.plan is not None and result.plan.bodies:
                origin = result.plan.bodies[0]
            elif result.bodies:
                origin = result.bodies[-1]
        result.span = span if span is not None else getattr(origin, 'span', None)
        result.pending_signatures = self.observations.records()
        self.terminal = result  # no unsafe retry of a partially executed body
        return result

    def _complete(self, node, reason, result):
        if node.path not in self.completed:
            # Work.get_node(add=0) creates temporary nodes for unregistered
            # explicit files. A later request must observe such a file again.
            if self.graph.find_node(node.path) is node:
                self.completed[node.path] = reason
            if node not in result.completed:
                result.completed.append(node)

    def build(self, requests=None):
        return self._build(requests, self.scope, self.cwd)

    def update_targets(self, request):
        """Synchronous command operation in this invocation; never flush here.

        Resolve each item after the previous one completes, as aap_update
        does, so includes can supply a subsequent target's definition.
        """
        if not isinstance(request, UpdateRequest):
            raise TypeError('nested update requires an UpdateRequest')
        result = UpdateResult(request)
        for target in request.targets:
            build = self._build([target], request.scope, request.cwd, request)
            result.builds.append(build)
            if build.status != 'COMPLETE':
                result.status, result.reason = build.status, build.reason
                result.target = target
                result.error, result.blocked_at = build.error, build.blocked_at
                if build.span is not None:
                    result.span = build.span
                return result
        return result

    def _build(self, requests, invocation_scope, cwd, update_request=None):
        if self.closed:
            raise ValueError('build invocation is closed')
        if self.terminal is not None:
            return self.terminal
        result = BuildResult()
        previous_preparation_scope = self.observations.preparation_scope
        self.observations.preparation_scope = invocation_scope
        resolved_requests = None
        # target_update creates one use_recdict per prerequisite group. Keep
        # these live across replans, including writes through a child's _caller.
        prerequisite_scopes = {}
        try:
            if not self.initialized:
                if self.port_defaults and self.scope.local.get('PORTNAME'):
                    markers = self.port_runtime.markers if self.port_runtime is not None else self.persistence
                    self.port = PortDefaults(self.graph, self.scope, self.cwd, markers)
                self.initialized = True
            while True:
                planner = UpdatePlanner(self.graph, self.observations, invocation_scope, cwd)
                plan = planner.plan(requests if resolved_requests is None else resolved_requests,
                                    completed=self.completed, nested=update_request is not None,
                                    active_paths=self.active_paths,
                                    active_definitions=self.active_definitions,
                                    request_origin=update_request)
                if resolved_requests is None:
                    resolved_requests = tuple(n.path for n in plan.requests)
                result.plan = plan
                # A speculative later failure must not prevent earlier work.
                # Commit only unguarded current visits before the next body.
                for entry in plan.entries:
                    if entry.bodies or entry.after or entry.status != 'current':
                        break
                    self._complete(entry.target, entry.reason, result)
                if not plan.bodies:
                    if not plan.complete:
                        diagnostic = plan.diagnostic
                        status = 'BLOCKED' if any(e.status == 'blocked' for e in plan.entries) else 'FAILED'
                        return self._stop(result, status, diagnostic.code, diagnostic)
                    for entry in plan.entries:
                        self._complete(entry.target, entry.reason, result)
                    result.status, result.reason = 'COMPLETE', 'requested_targets_complete'
                    result.pending_signatures = self.observations.records()
                    return result
                first = plan.bodies[0]
                if first.mode != 'update' or first.after:
                    return self._stop(result, 'BLOCKED', 'plan_recheck_required')
                target = first.target
                body_caller = invocation_scope
                scope_key = ()
                for parent_definition in first.prerequisite_definitions:
                    scope_key += (parent_definition,)
                    if scope_key not in prerequisite_scopes:
                        frame = Scope.build(parent_definition.scope, body_caller)
                        frame.local.update({'_dirstack': [], '_prevdir': None,
                            'recipe_name': parent_definition.span.source_id,
                            'recipe_lnum': parent_definition.span.start.line})
                        prerequisite_scopes[scope_key] = frame
                    body_caller = prerequisite_scopes[scope_key]
                # Upstream has finished this target's prerequisite loop before
                # entering any body. Own-body additions do not rerun that loop.
                # Its live build-body list *can* append ordered standard bodies.
                entered = set()
                while True:
                    definitions = [d for d in target.body_definitions if d not in entered]
                    if not definitions:
                        break
                    definition = definitions[0]
                    if not target.virtual and any(part == 'build' or part.startswith('build-')
                                                  for part in target.path.split('/')):
                        # DoBuild.locate_bdir/may_exec_depend calls checkdir
                        # before entry. No directory-creation capability yet.
                        return self._stop(result, 'BLOCKED', 'build_directory_preparation')
                    step = BodyPlan(target, definition, 'update', first.reason, 0, ())
                    step.signature_inputs = first.signature_inputs
                    step.graph_snapshot = self.graph.snapshot()
                    prepared = '' if target.virtual else self.observations.build_signature(definition, target)
                    if type(prepared) is not str:
                        return self._stop(result, 'BLOCKED', 'buildcheck_unavailable')
                    before = None if target.virtual else observe_file(self.observations, target)
                    if self.body_count >= self.max_bodies:
                        return self._stop(result, 'BLOCKED', 'body_limit')
                    context = BuildExecutionContext(step, body_caller, self.graph,
                        self.declarations, prepared_buildcheck=prepared,
                        capabilities=self.capabilities, update_driver=self)
                    self.body_count += 1
                    saved_paths, saved_definitions = self.active_paths, self.active_definitions
                    self.active_paths += first.active_paths
                    self.active_definitions = first.active_definitions
                    try:
                        body = self.executor.execute(context)
                    finally:
                        self.active_paths, self.active_definitions = saved_paths, saved_definitions
                    result.bodies.append(body)
                    if body.status != 'COMPLETED':
                        return self._stop(result, body.status, body.reason, body.error,
                                          body.blocked_at, body.span)
                    decision = self.recheck.check(body, before, self.observations, body_caller)
                    result.completions.append(decision)
                    if decision.status != 'COMPLETE':
                        return self._stop(result, decision.status, decision.reason,
                                          decision.error, span=decision.span)
                    for sibling in decision.siblings:
                        self._complete(sibling, 'shared_body_success', result)
                    entered.add(definition)
                self._complete(target, 'postconditions_satisfied', result)
                # Refresh future decisions against the live graph and state;
                # verified children remain memoized, including shared outputs.
        except Unsupported as error:
            return self._stop(result, 'BLOCKED', 'unsupported_semantics', error)
        except FrontendError as error:
            return self._stop(result, 'FAILED', 'semantic_error', error)
        except BuildSignatureFailure as error:
            return self._stop(result, 'FAILED', 'buildcheck_failed', error,
                              span=error.preparation.span)
        except (OSError, ValueError, NotImplementedError) as error:
            return self._stop(result, 'BLOCKED', 'observation_unavailable', error)
        finally:
            self.observations.preparation_scope = previous_preparation_scope

    def finish(self):
        """End invocation like Main.sign_write_all, even after a later failure.

        Earlier successfully checked bodies may have pending signatures. Failed
        or blocked bodies never add their own. Completion does not imply flush.
        """
        if self.closed:
            raise ValueError('build invocation is already closed')
        result = BuildResult()
        result.pending_signatures = self.observations.records()
        try:
            if result.pending_signatures:
                self.persistence.flush(result.pending_signatures)
                result.persistence_written = True
            result.status, result.reason = 'COMPLETE', 'invocation_finished'
        except (OSError, ValueError, NotImplementedError) as error:
            result.status, result.reason = 'BLOCKED', 'persistence_unavailable'
            result.error = error
        self.closed = True
        return result
