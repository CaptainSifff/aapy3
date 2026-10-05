"""Decoded persistent observations, distinct from per-run successful updates.

Sign._sign_upd_sign accumulates changes; Main flushes them even on later error.
No host sign-file serializer or recipe filesystem operation is provided here.
"""
from .target_state import TargetStateBackend
from .buildcheck import BuildSignatureFailure


class PersistenceBackend(object):
    def signature(self, target, source, check):
        """Return a saved string, '' for absence, or None for unavailable."""
        raise NotImplementedError('saved signatures unavailable')

    def marker_exists(self, path):
        raise NotImplementedError('port done-marker observations unavailable')

    def timestamp(self):
        """Explicit timestamp string for Sign.timekey; never host time implicitly."""
        raise NotImplementedError('signature timestamp unavailable')

    def flush(self, records):
        """Replace each supplied target's signatures; retain other targets.

        Called at invocation end, not body return. No port marker is implicit.
        Disk encoding/signfile placement belongs to a future adapter.
        """
        raise NotImplementedError('signature persistence unavailable')


class SignatureRecord(object):
    def __init__(self, target, definition, values, timestamp):
        self.target = target.identity
        self.target_path = target.path
        self.definition = definition
        self.cwd = definition.cwd
        self.span = definition.span
        self.values = dict(values)  # (source identity or '', check) -> string
        self.timestamp = timestamp


class MemoryPersistence(PersistenceBackend):
    def __init__(self):
        self.signatures = {}
        self.last_updates = {}
        self.markers = {}  # Existing done/<stage> bytes; contents are not read.
        self.time = '0'
        self.writes = []
        self.marker_reads = []

    def signature(self, target, source, check):
        return self.signatures.get(target, {}).get((source, check), '')

    def marker_exists(self, path):
        self.marker_reads.append(path)
        return path in self.markers

    def timestamp(self):
        return self.time

    def flush(self, records):
        for record in records:
            if record.values:
                self.signatures[record.target] = dict(record.values)
                self.last_updates[record.target] = record.timestamp
            else:
                self.signatures.pop(record.target, None)
                self.last_updates.pop(record.target, None)
            self.writes.append(record)


class InvocationObservations(TargetStateBackend):
    """Live file observations, per-run signature cache and pending save overlay."""
    def __init__(self, state, persistence, preparer=None):
        self.state = state
        self.persistence = persistence
        self.preparer = preparer
        self.preparation_scope = None
        self.preparations = []
        self.pending = {}
        self.order = []
        self.cache = {}

    def file_state(self, node):
        return self.state.file_state(node)

    def current_signature(self, node, check):
        key = (node.path, check)
        if key not in self.cache:
            # Sign.get_new_sign has no 'newer' implementation; comparison uses
            # time, but sign_updated stores the literal check name afterward.
            value = 'unknown' if check == 'newer' else self.state.current_signature(node, check)
            if value is None or type(value) is str:
                self.cache[key] = value
            else:
                raise ValueError('invalid current signature observation')
        return self.cache[key]

    def invalidate(self, node):
        for key in list(self.cache):
            if key[0] == node.path:
                del self.cache[key]

    def stored_signature(self, target, source, check):
        source_name = source.identity if source is not None else ''
        if target.identity in self.pending:
            return self.pending[target.identity].values.get((source_name, check), '')
        return self.persistence.signature(target.identity, source_name, check)

    def build_signature(self, definition, target):
        observed = self.state.build_signature(definition, target)
        if observed is not None or self.preparer is None:
            return observed
        result = self.preparer.prepare(definition, target,
                                       self.preparation_scope or definition.scope)
        self.preparations.append(result)
        if result.status == 'PREPARED':
            return result.signature
        if result.status == 'FAILED':
            raise BuildSignatureFailure(result)
        return None

    def implicit_dependencies(self, node):
        return self.state.implicit_dependencies(node)

    def matching_rule(self, node):
        return self.state.matching_rule(node)

    def stage(self, records):
        for record in records:
            if record.target not in self.pending:
                self.order.append(record.target)
            self.pending[record.target] = record

    def records(self):
        return tuple(self.pending[name] for name in self.order)
