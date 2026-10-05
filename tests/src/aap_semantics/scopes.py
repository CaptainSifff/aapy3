"""Explicit recipe/build lookup layers, never host Python lexical scope."""
import string

from .diagnostics import SemanticError, UndefinedName, Unsupported
from .values import MISSING, DeferredExpansion, UnavailableValue, check_value
from .work import WorkIdentity


class Namespace(object):
    def __init__(self, scope, search=False):
        self.scope = scope
        self.search = search

    def get(self, name):
        return self.scope.lookup(name) if self.search else self.scope.local.get(name, MISSING)

    def set(self, name, value, origin):
        self.scope.store(name, value, origin)


class SearchNamespace(Namespace):
    """Historical CallstackDict: ordered live lookup; writes require a name."""
    def __init__(self, layers):
        self.layers = tuple(layers)

    def get(self, name):
        for layer in self.layers:
            if name in layer.local:
                return layer.local[name]
        return MISSING

    def set(self, name, value, origin):
        for layer in self.layers:
            if name in layer.local:
                layer.store(name, value, origin)
                return
        raise UndefinedName(origin, 'variable not found in enclosing scope: ' + name)


class Scope(object):
    def __init__(self, enclosing=()):
        self.local = {}
        self.work = None  # interpreter metadata, never a recipe Python value
        # Explicit ordered lookup layers; no inference of a runtime call stack.
        self.enclosing = tuple(enclosing)
        self.recipe_tree = (self,) + self.enclosing
        self.call_stack = None  # Top-level invocations do not contribute a caller frame.
        self.namespaces = {'_no': Namespace(self, True)}

    @classmethod
    def top_level(cls, port_defaults=False, work=None):
        scope = cls()
        scope.work = work if work is not None else WorkIdentity()
        scope.local['_prevdir'] = None
        if port_defaults:
            # Work.py initializes this port default before reading the recipe.
            scope.local['PATCHDISTDIR'] = 'patches'
        namespace = Namespace(scope)
        scope.namespaces['_recipe'] = namespace
        scope.namespaces['_top'] = namespace
        # Scope.create_topscope() gives command-line settings their own
        # RecipeDict.  This intentionally remains distinct from the current
        # recipe dictionary: a later recipe assignment may replace the normal
        # value without erasing the command-line value in _arg.
        scope.namespaces['_arg'] = Namespace(cls())
        return scope

    def set_command_line(self, name, value):
        """Seed a valid DoArgs-style setting before reading the main recipe."""
        self.local[name] = value
        self.namespaces['_arg'].scope.local[name] = value

    def get_work(self):
        # Work.getwork searches the same ordered layers as _no, including
        # caller/definition layers for deferred bodies and action entry.
        for scope in (self,) + self.enclosing:
            if scope.work is not None:
                return scope.work
        return None

    @classmethod
    def build(cls, definition, caller, keep_current_scope=False):
        """Scope.get_build_recdict; actions retain the caller recipe tree."""
        stack = ((caller,) + caller.call_stack if caller.call_stack is not None else ())
        tree = ((caller.recipe_tree if keep_current_scope else ())
                + definition.recipe_tree)
        conf = caller.namespaces.get('_conf')
        extra = (conf.scope,) if conf is not None else ()
        scope = cls(stack + tree + extra)
        scope.recipe_tree = tree
        scope.call_stack = stack
        for name in ('_top', '_arg', '_default', '_start', '_conf'):
            if name in caller.namespaces:
                scope.namespaces[name] = caller.namespaces[name]
        for name in ('_recipe', '_parent'):
            if name in definition.namespaces:
                scope.namespaces[name] = definition.namespaces[name]
        for name, namespace in caller.namespaces.items():
            if not name.startswith('_'):
                scope.namespaces[name] = namespace
        if caller.call_stack is not None:
            scope.namespaces['_caller'] = Namespace(caller)
        scope.namespaces['_tree'] = SearchNamespace(tree)
        scope.namespaces['_stack'] = SearchNamespace(stack)
        scope.namespaces['_up'] = SearchNamespace(scope.enclosing)
        return scope

    def lookup(self, name):
        value = self.local.get(name, MISSING)
        if value is not MISSING:
            return value
        for scope in self.enclosing:
            if name in scope.local:
                return scope.local[name]
        return MISSING

    def python_name(self, name, origin):
        # Bare Python names do not search _up. Namespace objects are explicit.
        if name in self.local:
            value = self.local[name]
            if isinstance(value, UnavailableValue):
                raise Unsupported(origin, value.reason)
            return value
        if name in self.namespaces:
            return self.namespaces[name]
        if name in ('_recipe', '_top', '_parent', '_up', '_tree', '_stack',
                    '_caller', '_default', '_start', '_conf', '_arg'):
            raise Unsupported(origin, 'scope is not bound in this metadata context: ' + name)
        raise UndefinedName(origin, 'undefined Python name: ' + name)

    def store(self, name, value, origin):
        if name in self.namespaces:
            raise Unsupported(origin, 'overwriting a scope binding is deferred: ' + name)
        if name.startswith('__'):
            raise Unsupported(origin, 'private interpreter names are not permitted')
        if not isinstance(value, DeferredExpansion):
            check_value(value, origin)
        self.local[name] = value

    def target(self, target, origin, create=False):
        parts = target.split('.')
        if len(parts) == 1:
            return self.namespaces['_no'], target
        if len(parts) != 2 or not all(parts):
            raise SemanticError(origin, 'invalid scoped variable name: ' + target)
        name, variable = parts
        if name not in self.namespaces:
            if (not create or name[0] not in string.ascii_letters
                    or any(c not in string.ascii_letters + string.digits + '_' for c in name)):
                raise Unsupported(origin, 'scope is not bound in this metadata context: ' + name)
            for layer in (self,) + self.enclosing:
                if name in layer.local:
                    raise SemanticError(origin, 'scope name already used as a variable: ' + name)
            namespace = Namespace(Scope())
            layers = (self,) + self.enclosing
            top = self.namespaces.get('_top')
            if top is not None and top.scope not in layers:
                layers += (top.scope,)
            # create_user_scope propagates a shared user scope to explicit layers.
            for layer in layers:
                existing = layer.namespaces.get(name)
                if existing is not None:
                    raise Unsupported(origin, 'user scope must be explicitly shared: ' + name)
            for layer in layers:
                layer.namespaces[name] = namespace
        return self.namespaces[name], variable

    def read(self, target, origin):
        namespace, name = self.target(target, origin)
        value = namespace.get(name)
        if isinstance(value, UnavailableValue):
            raise Unsupported(origin, value.reason)
        if value is MISSING:
            raise UndefinedName(origin, 'undefined A-A-P variable: ' + target)
        return value
