"""Explicit-graph update planning. No executor, rule matcher or host I/O.

Plans describe a successful-execution path through the registered graph.
After a possible body effect, file decisions are rechecks, not predictions
based on stale pre-execution observations. See notes/update-planner.md.
"""
import posixpath

from aap_frontend import Source
from .model import Node
from .graph import TargetNode, STANDARD_TARGETS
from .dependency_items import DependencyItem, parse_items
from .diagnostics import SemanticError, Unsupported
from .expansion import expand_text
from .values import MISSING, var2string
from .target_state import FileState
from .buildcheck import BuildSignatureFailure


_AUTOMATIC = frozenset(('fetch', 'publish', 'commit', 'checkout', 'checkin',
                       'unlock', 'add', 'remove', 'tag', 'revise', 'reference'))


class PlanningDiagnostic(SemanticError):
    def __init__(self, origin, code, reason):
        super(PlanningDiagnostic, self).__init__(origin, reason)
        self.code = code


class InvalidStoredSignature(ValueError):
    pass


class BodyPlan(Node):
    def __init__(self, target, definition, mode, reason, index, after):
        super(BodyPlan, self).__init__(definition)
        self.target = target
        self.definition = definition
        self.body = definition.body
        self.scope = definition.scope
        self.cwd = definition.cwd
        self.mode = mode  # update, sections, or conditional
        self.reason = reason
        self.index = index
        self.after = tuple(after)  # successful earlier body steps, graph unchanged
        self.outputs = definition.targets
        self.signature_targets = tuple(n for n in self.outputs
                                       if n.name not in STANDARD_TARGETS)
        self.signature_inputs = tuple(tuple(i for i in d.source_items if not i.node.virtual)
                                      for d in target.definitions)
        self.buildcheck_required = not target.virtual
        self.postcondition = 'trigger_survives_if_preexisting'
        self.active_paths = ()
        self.active_definitions = ()
        self.prerequisite_definitions = ()


class UpdateDecision(Node):
    def __init__(self, target, status, reason, prerequisites, bodies, after):
        super(UpdateDecision, self).__init__(target)
        self.target = target
        self.status = status
        self.reason = reason
        # Each relation retains its definition and item; duplicates survive.
        self.prerequisites = tuple(prerequisites)
        self.bodies = tuple(bodies)
        self.after = tuple(after)
        self.covered_by = None
        self.cwd = target.cwd
        self.virtual = target.virtual


class UpdatePlan(object):
    def __init__(self):
        self.requests = ()
        self.entries = []
        self.bodies = []
        self.states = {}
        self.diagnostic = None
        self.complete = True

    @property
    def requires_recheck(self):
        return any(e.status == 'recheck' for e in self.entries)

    def decision_for(self, identity):
        return next((e for e in self.entries if e.target.identity == identity), None)

    def snapshot(self):
        return tuple((e.target.identity, e.status, e.reason,
                      tuple(b.definition.index for b in e.bodies), e.after)
                     for e in self.entries)


class _Request(object):
    def __init__(self, name):
        self.source = Source('<target request>', name)
        self.span = self.source.span(0, len(name))


def _sections(body):
    # DoBuild.commands_with_sections only checks the first non-comment token.
    # It does not parse/validate the section or any statement in the body.
    for line in body.origin.text.splitlines():
        token = line.lstrip(' \t\r')
        if token and not token.startswith('#'):
            return token.startswith('>')
    return False


class UpdatePlanner(object):
    def __init__(self, graph, state, scope=None, cwd=None, build_rule_targets=()):
        self.graph = graph
        self.state = state
        self.scope = scope
        self.cwd = cwd if cwd is not None else graph.base_directory
        if self.cwd is None or not posixpath.isabs(self.cwd):
            raise ValueError('planning requires an absolute recipe directory')
        # This is supplied by a future build-rule declarator, never inferred
        # from ordinary dependency order.
        self.build_rule_targets = tuple(build_rule_targets)

    def _resolve(self, name, expand=True):
        if type(name) is not str:
            raise ValueError('requested targets must be strings')
        origin = self._request_origin or _Request(name)
        # Command-line names use the existing expansion subset, not eval.
        if expand and '$' in name:
            if self.scope is None:
                raise Unsupported(origin, 'target expansion requires a scope')
            name = expand_text(name, self.scope, origin)
        if not name or any(c in name for c in '\x00\n\r'):
            raise PlanningDiagnostic(origin, 'invalid_target', 'invalid target name')
        if any(c in name for c in '*?[%`{}') or name.startswith('~') or '://' in name:
            raise Unsupported(origin, 'unsupported requested target feature')
        node = self.graph.find_node(name, self.cwd)
        if node is not None:
            return node
        path = posixpath.normpath(posixpath.join(self.cwd, name))
        if path not in self._extra:
            item = DependencyItem(origin, name, {})
            self._extra[path] = TargetNode(item, path, self.cwd,
                                           len(self.graph.nodes) + len(self._extra))
        return self._extra[path]

    def _defaults(self):
        value = self.scope.local.get('TARGET', MISSING) if self.scope else MISSING
        if value is not MISSING and value:
            origin = _Request('$TARGET')
            items = parse_items(var2string(value, origin), origin)
            if any(i.attributes for i in items):
                raise Unsupported(origin, 'default target attributes are deferred')
            return [self._resolve(i.name, False) for i in items]
        node = self.graph.find_virtual_node('all')
        if node is not None:
            return [node]
        return [self._resolve(n, False) for n in self.build_rule_targets]

    def plan(self, requests=None, completed=(), nested=False,
             active_paths=(), active_definitions=(), request_origin=None):
        """None selects defaults; strings/lists are command-line target requests.

        No force, continue, touch, nobuild, recursive or rule options are
        implicitly enabled. Each call owns fresh traversal state.
        """
        self.result = UpdatePlan()
        self._extra = {}
        self._done = {}
        self._active = set(active_definitions)
        self._busy = frozenset(active_paths)
        self._path = []
        self._callers = []
        self._request_origin = request_origin
        self._cover = {}
        self._maybe_cover = {}
        self._sign_cache = {}
        # Only a driver which verified completion may supply these paths.
        # Standalone plans continue to own fresh speculative traversal state.
        self._completed = frozenset(completed)
        nodes = []
        if requests is None:
            nodes = self._defaults()
        else:
            if type(requests) is str:
                requests = [requests]
            for name in requests:
                node = self._resolve(name, expand=not nested)
                update = self.graph.find_virtual_node('update')
                if not nested and name == 'update' and (update is None or not update.definitions):
                    nodes.append(self._resolve('fetch'))
                    nodes.extend(self._defaults())
                else:
                    nodes.append(node)
        self.result.requests = tuple(nodes)
        for node in nodes:
            entry = self._visit(node, not nested)
            if entry.status in ('failed', 'blocked'):
                break
        else:
            # Ordinary fatal errors bypass finally in DoBuild.dobuild.
            final = self.graph.find_virtual_node('finally')
            if final is not None and not nested:
                self._visit(final, False)
        return self.result

    def _finish(self, node, status, reason, relations, bodies=(), after=None):
        if after is None:
            after = range(len(self.result.bodies))
        entry = UpdateDecision(node, status, reason, relations, bodies, after)
        self.result.entries.append(entry)
        self.result.states[node] = status
        self._done[node] = entry
        return entry

    def _problem(self, node, code, message, relations, blocked=False, origin=None):
        if self.result.diagnostic is None:
            self.result.diagnostic = PlanningDiagnostic(origin or node, code, message)
        self.result.complete = False
        return self._finish(node, 'blocked' if blocked else 'failed', code, relations)

    def _file(self, node):
        state = self.state.file_state(node)
        if (not isinstance(state, FileState) or type(state.exists) is not bool
                or type(state.directory) is not bool
                or type(state.mtime) not in (int, float) or state.mtime < 0):
            raise ValueError('invalid file-state observation')
        return state

    def _signature(self, node, check):
        key = (node, check)
        if key not in self._sign_cache:
            value = self.state.current_signature(node, check)
            if value is not None and type(value) is not str:
                raise ValueError('invalid current signature observation')
            self._sign_cache[key] = value
        return self._sign_cache[key]

    def _stored(self, target, source, check):
        value = self.state.stored_signature(target, source, check)
        if value is not None and type(value) is not str:
            raise InvalidStoredSignature('invalid stored signature observation')
        return value

    def _visit(self, node, toplevel):
        if node in self._done:
            return self._done[node]
        relations = []
        if node.path in self._completed:
            return self._finish(node, 'current', 'already_updated', relations)
        if node.path in self._busy:
            return self._problem(node, 'cycle', 'cyclic nested update: ' + node.name,
                                 relations, origin=self._request_origin)
        if node in self._cover:
            step = self._cover[node]
            entry = self._finish(node, 'covered', 'shared_body_success', relations)
            entry.covered_by = step
            return entry
        if node in self._maybe_cover:
            # A conditional shared body may mark this sibling done even when
            # no output file is created. Rechecking only its file would be
            # wrong. Defer the whole visit until the prior update result is
            # known, including whether to traverse its own prerequisite lists.
            entry = self._finish(node, 'recheck', 'shared_body_result', relations)
            entry.covered_by = self._maybe_cover[node]
            return entry
        self.result.states[node] = 'visiting'
        self._path.append(node.path)
        try:
            return self._walk(node, toplevel, relations)
        except BuildSignatureFailure as error:
            return self._problem(node, 'buildcheck_failed', str(error), relations,
                                 origin=error.preparation.definition)
        except InvalidStoredSignature as error:
            return self._problem(node, 'stored_signature_invalid', str(error),
                                 relations, origin=node)
        except (OSError, ValueError, NotImplementedError) as error:
            return self._problem(node, 'observation_unavailable',
                                 str(error) or 'target-state observation unavailable', relations, True)
        finally:
            self._path.pop()

    def _walk(self, node, toplevel, relations):
        if any(key != 'virtual' for key in node.attributes):
            return self._problem(node, 'unsupported_attributes',
                                 'target attributes require later runtime support', relations, True)
        if node.name == 'comment':
            return self._problem(node, 'automatic_target', 'comment output is deferred', relations, True)
        # Update accumulates across *all* declarations before any body runs.
        cause = None
        stamp = 0
        uncertain = False
        for definition in node.definitions:
            srcpath = definition.scope.lookup('SRCPATH')
            if srcpath is not MISSING and srcpath:
                return self._problem(node, 'source_search_path',
                                     'SRCPATH resolution is deferred', relations, True,
                                     origin=definition)
            if any(key != 'virtual' for key in definition.build_attributes):
                return self._problem(node, 'unsupported_attributes',
                                     'build attributes require later runtime support', relations, True,
                                     origin=definition)
            if definition in self._active:
                return self._problem(node, 'cycle', 'cyclic shared dependency', relations,
                                     origin=definition)
            self._active.add(definition)
            try:
                for item in definition.source_items:
                    if any(key != 'virtual' for key in item.attributes):
                        return self._problem(node, 'unsupported_attributes',
                                             'source attributes require later runtime support', relations,
                                             True, origin=item)
                    child = item.node
                    if child is node:
                        relations.append((definition, item, 'self_ignored'))
                        continue
                    if self.result.states.get(child) == 'visiting':
                        return self._problem(node, 'cycle', 'cyclic dependency: ' + child.name,
                                             relations, origin=item)
                    self._callers.append(definition)
                    try:
                        entry = self._visit(child, False)
                    finally:
                        self._callers.pop()
                    relations.append((definition, item, entry))
                    if entry.status in ('failed', 'blocked'):
                        return self._problem(node, 'prerequisite_' + entry.status,
                                             'prerequisite did not update: ' + child.name,
                                             relations, entry.status == 'blocked')
                    if not child.virtual and self.state.implicit_dependencies(child) is not False:
                        return self._problem(node, 'automatic_dependencies',
                                             'automatic dependency discovery is deferred', relations, True)
                    # Preserve Update.outdated's short circuit, including the
                    # first positive newer timestamp (not a generic max-mtime).
                    if node.virtual or cause is not None or stamp:
                        continue
                    if child.virtual:
                        cause = 'virtual_prerequisite'
                        continue
                    if self.result.bodies or entry.status in ('recheck', 'covered'):
                        uncertain = True
                        continue
                    method = definition.scope.lookup('DEFAULTCHECK')
                    if method is MISSING:
                        method = 'md5'  # upstream/default.aap:29
                    state = self._file(child)
                    if state.directory:
                        method = 'none'
                    if method not in ('md5', 'c_md5', 'time', 'newer', 'none'):
                        return self._problem(node, 'unsupported_check',
                                             'unsupported signature method', relations, True)
                    check = 'time' if method == 'newer' else method
                    new = self._signature(child, check)
                    if new is None:
                        return self._problem(node, 'signature_unavailable',
                                             'current prerequisite signature unavailable', relations, True)
                    if new in ('', '0'):
                        return self._problem(node, 'signature_error',
                                             'cannot compute prerequisite signature', relations, origin=item)
                    if method == 'newer':
                        stamp = int(float(new))
                    else:
                        old = self._stored(node, child, check)
                        if old is None:
                            return self._problem(node, 'signature_unavailable',
                                                 'stored signature observation unavailable', relations, True)
                        if old != new:
                            cause = 'signature_changed' if old else 'signature_missing'
            finally:
                self._active.remove(definition)

        definitions = node.body_definitions
        if not definitions:
            state = None
            if not node.virtual and not self.result.bodies:
                state = self._file(node)
                if state.directory:
                    return self._finish(node, 'current', 'directory_exists', relations)
            if self.state.matching_rule(node) is not False:
                return self._problem(node, 'rule_required', 'rule matching is deferred', relations, True)
            if node.name in _AUTOMATIC:
                # Commands.do_fetch_all succeeds without effects when no node
                # even has fetch/commit metadata. Port done markers expose
                # this path by generating a bodyless fetch stage. Never infer
                # success for a nonempty candidate set (may_fetch is deferred).
                if node.name == 'fetch' and not any(
                        'fetch' in item.attributes or 'commit' in item.attributes
                        for item in self.graph.nodes):
                    return self._finish(node, 'current', 'empty_fetch', relations)
                return self._problem(node, 'automatic_target',
                                     'automatic target behavior is deferred: ' + node.name, relations, True)
            if node.name == 'refresh':
                return self._finish(node, 'current', 'refresh_noop', relations)
            if node.virtual:
                if node.definitions:
                    return self._finish(node, 'current', 'virtual_aggregate', relations)
                return self._problem(node, 'no_build_commands',
                                     'virtual target has no dependency or body', relations)
            if self.result.bodies:
                return self._finish(node, 'recheck', 'post_execution_state', relations)
            if toplevel or not state.exists:
                return self._problem(node, 'no_build_commands',
                                     'do not know how to build: ' + node.name, relations)
            return self._finish(node, 'current', 'source_exists', relations)

        after = tuple(range(len(self.result.bodies)))
        bodies = []
        status, reason = 'current', 'signature_current'
        for definition in definitions:
            if node.virtual:
                status, reason = 'update', 'virtual_target'
            elif cause is not None:
                status, reason = 'update', cause
            elif uncertain or self.result.bodies:
                status, reason = 'recheck', 'post_execution_state'
            else:
                state = self._file(node)
                new = self.state.build_signature(definition, node)
                if new is not None and type(new) is not str:
                    raise ValueError('invalid buildcheck observation')
                old = self._stored(node, None, 'buildcheck')
                # A positive newer timestamp suppresses the buildcheck
                # comparison in Update.outdated; it is still needed at commit.
                if not stamp and new and old is not None and new != old:
                    status = 'update'
                    reason = 'buildcheck_changed' if old else 'buildcheck_missing'
                elif state.mtime < stamp:
                    status, reason = 'update', 'newer_prerequisite'
                elif not state.exists or state.mtime == 0:
                    status, reason = 'update', 'missing_target'
                elif not stamp and (new is None or (new and old is None)):
                    return self._problem(node, 'buildcheck_unavailable',
                                         'historical expanded body signature unavailable', relations, True)
            if status in ('update', 'recheck') or _sections(definition.body):
                mode = ('conditional' if status == 'recheck' else
                        'update' if status == 'update' else 'sections')
                step = BodyPlan(node, definition, mode, reason,
                                len(self.result.bodies), range(len(self.result.bodies)))
                step.active_paths = tuple(self._path)
                step.active_definitions = tuple(sorted(self._active, key=lambda d: d.index))
                step.prerequisite_definitions = tuple(self._callers)
                self.result.bodies.append(step)
                step.graph_snapshot = self.graph.snapshot()
                bodies.append(step)
                # Under normal successful execution, siblings are marked done
                # only when an actual update was selected, not for sections.
                if mode == 'update':
                    for sibling in step.signature_targets:
                        if sibling is not node:
                            self._cover[sibling] = step
                elif mode == 'conditional':
                    for sibling in step.signature_targets:
                        if sibling is not node:
                            self._maybe_cover[sibling] = step
        return self._finish(node, status, reason, relations, bodies, after)
