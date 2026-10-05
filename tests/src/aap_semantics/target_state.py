"""Read-only observations for planning, independent of the host filesystem.

Signature strings are historical observations, not hashes invented by the
planner. None means unavailable; an empty stored signature means absent.
"""
from collections import namedtuple
import hashlib


FileState = namedtuple('FileState', 'exists mtime directory')


def normalize_buildcheck(expanded_check):
    """DoBuild.buildcheck_update's final whitespace pass, before byte MD5."""
    text = expanded_check
    result = ''
    index = 0
    leading = True
    while index < len(text):
        start = index
        char = text[index]
        while (char in ' \t' and index + 1 < len(text)
               and text[index + 1] in ' \t\n'):
            index += 1
        if not leading:
            start = index
        end = index + 1
        while end < len(text) and text[end] not in ' \t':
            end += 1
        result += text[start:end]
        index = end
        leading = result[-1] == '\n'
    return result


def buildcheck_digest(expanded_check, encoding):
    """Hash an ALREADY PREPARED historical check string, not a raw CST body.

    Callers must have performed action expansion, comment removal, scoped
    expansion and special-variable masking. This helper only implements the
    final whitespace pass and MD5 in DoBuild.buildcheck_update/Sign.py.
    Encoding is explicit because upstream hashed Python 2 bytes.
    """
    return hashlib.md5(normalize_buildcheck(expanded_check).encode(encoding, 'strict')).hexdigest()


class TargetStateBackend(object):
    def file_state(self, node):
        raise NotImplementedError

    def current_signature(self, node, check):
        raise NotImplementedError

    def stored_signature(self, target, source, check):
        raise NotImplementedError

    def build_signature(self, definition, target):
        """Prepared historical buildcheck; None if expansion is unavailable."""
        raise NotImplementedError

    def implicit_dependencies(self, node):
        """False certifies none; True/None requires the deferred rule layer."""
        raise NotImplementedError

    def matching_rule(self, node):
        """False certifies no rule; True/None is outside explicit-graph planning."""
        raise NotImplementedError


class MemoryTargetState(TargetStateBackend):
    """An explicitly closed, in-memory world, never a view of ambient files.

    Files not entered are missing. Rules/autodependencies are absent unless
    overridden. Existing file signatures and buildchecks must be supplied;
    omission cannot accidentally certify that a target is current.
    """
    def __init__(self):
        self.files = {}
        self.signatures = {}
        self.stored = {}
        self.buildchecks = {}
        self.implicit = {}
        self.rules = {}

    def file_state(self, node):
        return self.files.get(node.path, FileState(False, 0, False))

    def current_signature(self, node, check):
        state = self.file_state(node)
        if check == 'time':
            return str(state.mtime if state.exists else 0)
        if check == 'none':
            return 'unknown'  # Sign.get_new_sign's historical fallback.
        if check in ('md5', 'c_md5') and not state.exists:
            return 'unknown'
        return self.signatures.get((node.identity, check))

    def stored_signature(self, target, source, check):
        identity = source.identity if source is not None else ''
        return self.stored.get((target.identity, identity, check), '')

    def build_signature(self, definition, target):
        return self.buildchecks.get((definition.index, target.identity))

    def implicit_dependencies(self, node):
        return self.implicit.get(node.identity, False)

    def matching_rule(self, node):
        return self.rules.get(node.identity, False)
