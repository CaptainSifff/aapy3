"""Synchronous, source-backed target updates; no execution capability of its own."""
from .model import Node
from .dependency_items import parse_items
from .expansion import render_value, expand_text
from .diagnostics import SemanticError, Unsupported


class UpdateRequest(Node):
    def __init__(self, command, scope, python, cwd, context=None):
        super(UpdateRequest, self).__init__(command)
        self.command = command
        self.scope, self.cwd, self.context = scope, cwd, context
        if command.body is not None:
            raise Unsupported(command, ':update bodies are deferred')
        self.raw = render_value(command.arguments, python)
        if self.raw.lstrip().startswith('{'):
            raise Unsupported(command, ':update options are deferred')
        self.expanded = expand_text(self.raw, scope, command)
        items = parse_items(self.expanded, command)
        if any(item.attributes for item in items):
            raise Unsupported(command, ':update item attributes are deferred')
        if not items:
            raise SemanticError(command, 'missing argument for :update')
        self.targets = tuple(item.name for item in items)


class UpdateResult(object):
    def __init__(self, request):
        self.request = request
        self.builds = []
        self.status = 'COMPLETE'
        self.reason = 'requested_targets_complete'
        self.error = None
        self.blocked_at = None
        self.span = request.span
        self.target = None


class UpdateStopped(Exception):
    """Internal structured unwind, never exposed as a recipe Python exception."""
    def __init__(self, result):
        super(UpdateStopped, self).__init__(result.reason)
        self.result = result
