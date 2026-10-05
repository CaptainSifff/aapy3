"""Ordered dependency registration; no file probes, traversal or execution."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .dependency_items import dependency_fields


# Global.virtual_targets: this exact list, not filename shape or do-* spelling,
# also controls whether multiple body-bearing declarations are permitted.
STANDARD_TARGETS = frozenset(('add', 'all', 'build', 'check', 'checkin',
    'checkout', 'clean', 'cleanmore', 'cleanALL', 'commit', 'distclean',
    'extract', 'fetch', 'finally', 'install', 'patch', 'publish', 'reference',
    'remove', 'revise', 'test', 'tryout', 'unlock', 'update'))


class TargetNode(Node):
    def __init__(self, item, path, cwd, index):
        super(TargetNode, self).__init__(item)
        self.name = item.name
        self.path = path
        self.cwd = cwd
        self.index = index
        self.attributes = {'virtual': 1} if self.name in STANDARD_TARGETS else {}
        self.definitions = []

    @property
    def virtual(self):
        return bool(self.attributes.get('virtual'))

    @property
    def identity(self):
        return self.name if self.virtual else self.path

    @property
    def body_definitions(self):
        return tuple(d for d in self.definitions if d.body is not None)


class DependencyDefinition(Node):
    def __init__(self, declaration, targets, sources, attributes, scope, cwd, index):
        super(DependencyDefinition, self).__init__(declaration)
        self.target_items = targets
        self.source_items = sources
        self.build_attributes = dict(attributes)
        self.body = declaration.body
        self.scope = scope
        self.cwd = cwd
        self.index = index

    @property
    def targets(self):
        return tuple(item.node for item in self.target_items)

    @property
    def prerequisites(self):
        return tuple(item.node for item in self.source_items)


class BuildGraph(object):
    def __init__(self):
        self.definitions = []
        self.nodes = []
        self._paths = {}
        self._names = {}
        self.base_directory = None

    @property
    def targets(self):
        return tuple(node for node in self.nodes if node.definitions)

    def find_node(self, name, cwd=None):
        cwd = cwd if cwd is not None else self.base_directory
        if cwd is None and not posixpath.isabs(name):
            raise ValueError('relative graph lookup requires a directory')
        path = posixpath.normpath(name if posixpath.isabs(name) else posixpath.join(cwd, name))
        node = self._paths.get(path)
        if node is not None:
            return node
        return self.find_virtual_node(name)

    def find_virtual_node(self, name):
        """Name-only lookup used by historical default/special target selection."""
        node = self._names.get(name)
        return node if node is not None and node.virtual else None

    def dependencies_for(self, name, cwd=None):
        node = self.find_node(name, cwd)
        return tuple(node.definitions) if node is not None else ()

    def definition_history(self, name, cwd=None):
        return self.dependencies_for(name, cwd)

    def register(self, declaration, scope, cwd):
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(declaration, 'dependency registration requires an absolute recipe directory')
        targets, sources, attrs = dependency_fields(declaration, scope)
        definition = DependencyDefinition(declaration, targets, sources, attrs,
                                          scope, cwd, len(self.definitions))
        # Stage all node/attribute changes, so an invalid redefinition does not
        # leave a half-registered graph. Successful ordering matches upstream.
        paths = dict(self._paths)
        names = dict(self._names)
        pending = []
        virtual = {}
        for item in targets + sources:
            path = posixpath.normpath(posixpath.join(cwd, item.name))
            node = paths.get(path)
            if node is None:
                candidate = names.get(item.name)
                if candidate is not None and virtual.get(candidate, candidate.virtual):
                    node = candidate
            if node is None:
                node = TargetNode(item, path, cwd, len(self.nodes) + len(pending))
                pending.append(node)
                paths[path] = node
                names[item.name] = node
            item.node = node
            virtual[node] = virtual.get(node, node.virtual) or bool(item.attributes.get('virtual'))
        body_seen = set()
        if definition.body is not None:
            for item in targets:
                node = item.node
                if node.name not in STANDARD_TARGETS and (node.body_definitions or node in body_seen):
                    raise SemanticError(declaration, 'multiple build bodies for target: ' + item.name)
                body_seen.add(node)
        for item in targets + sources:
            value = item.attributes.get('virtual')
            if value:
                item.node.attributes['virtual'] = value
        self._paths, self._names = paths, names
        self.nodes.extend(pending)
        self.definitions.append(definition)
        if self.base_directory is None:
            self.base_directory = cwd
        for item in targets:
            item.node.definitions.append(definition)
        return definition

    def snapshot(self):
        """Stable, value-only declaration-order inspection, without traversal."""
        return tuple((d.index, tuple(n.identity for n in d.targets),
                      tuple(n.identity for n in d.prerequisites),
                      d.span.source_id, d.span.start.line, d.body is not None)
                     for d in self.definitions)
