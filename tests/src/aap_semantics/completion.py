"""Post-body checks and pending signatures; never execute a recipe or flush."""
from .model import Node
from .target_state import FileState
from .persistence import SignatureRecord
from .values import MISSING


def observe_file(state, node):
    value = state.file_state(node)
    if (not isinstance(value, FileState) or type(value.exists) is not bool
            or type(value.directory) is not bool
            or type(value.mtime) not in (int, float) or value.mtime < 0):
        raise ValueError('invalid file-state observation')
    return value


class TargetCompletionDecision(Node):
    def __init__(self, origin, status, reason):
        super(TargetCompletionDecision, self).__init__(origin)
        self.status = status
        self.reason = reason
        self.target = origin.target
        self.before = None
        self.after = None
        self.records = ()
        self.siblings = ()
        self.graph_changed = False
        self.error = None


class PostExecutionRecheck(object):
    def check(self, body, before, observations, invocation_scope):
        decision = TargetCompletionDecision(body.context, 'BLOCKED', 'body_not_completed')
        decision.before = before
        decision.graph_changed = body.graph_changed
        if body.status != 'COMPLETED':
            return decision
        step = body.context.step
        try:
            # DoBuild.may_exec_depend only requires an originally nonzero
            # trigger timestamp to remain nonzero. Missing outputs may stay
            # missing; unchanged timestamps are explicitly allowed upstream.
            if not step.target.virtual and before.mtime > 0:
                decision.after = observe_file(observations, step.target)
                if not decision.after.exists or decision.after.mtime == 0:
                    decision.status, decision.reason = 'FAILED', 'trigger_disappeared'
                    return decision
            for target in step.signature_targets:
                observations.invalidate(target)
            if not step.signature_targets:
                decision.status, decision.reason = 'COMPLETE', 'postconditions_satisfied'
                return decision
            values = {}
            # All traversed dependency source groups, not just this body's.
            for group in step.signature_inputs:
                for item in group:
                    if item.node.virtual:
                        continue
                    method = invocation_scope.lookup('DEFAULTCHECK')
                    if method is MISSING:
                        method = 'md5'
                    if observe_file(observations, item.node).directory:
                        method = 'none'
                    if method not in ('md5', 'c_md5', 'time', 'newer', 'none'):
                        raise NotImplementedError('signature check method unavailable')
                    value = observations.current_signature(item.node, method)
                    if value is None:
                        decision.reason = 'signature_unavailable'
                        return decision
                    values[(item.node.identity, method)] = value
            buildcheck = body.context.prepared_buildcheck if step.buildcheck_required else ''
            if buildcheck is None:
                decision.reason = 'buildcheck_unavailable'
                return decision
            if buildcheck:
                values[('', 'buildcheck')] = buildcheck
            records = []
            for target in step.signature_targets:
                timestamp = observations.persistence.timestamp() if values else None
                if values and type(timestamp) is not str:
                    raise ValueError('signature timestamp must be a string')
                records.append(SignatureRecord(target, step.definition, values, timestamp))
            decision.records = tuple(records)
            observations.stage(records)  # only after ALL observations succeeded
            decision.siblings = tuple(n for n in step.signature_targets if n is not step.target)
            decision.status, decision.reason = 'COMPLETE', 'postconditions_satisfied'
        except (OSError, ValueError, NotImplementedError) as error:
            decision.reason, decision.error = 'observation_unavailable', error
        return decision
