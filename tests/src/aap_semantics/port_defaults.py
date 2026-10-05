"""Archive-port default declarations from Port.add_port_defaults/add_port_dep.

Generated commands remain ordinary opaque recipe bodies. In particular this
module does not implement :update, :mkdir, :touch or any port_* Python helper.
"""
import posixpath

from aap_frontend import Source, parse
from .lowering import lower
from .diagnostics import SemanticError, Unsupported
from .values import MISSING


STAGES = (('dependcheck', '', False), ('fetchdepend', 'checksum', False),
          ('fetch', 'fetch', True), ('checksum', 'checksum', True),
          ('extractdepend', 'patch', False), ('extract', 'extract', True),
          ('patch', 'patch', True), ('builddepend', 'build', False),
          ('config', 'config', True), ('build', 'build', True),
          ('testdepend', 'test', False), ('test', 'test', True),
          ('package', 'package', True), ('install', '', False))
INDEPENDENT = ('rundepend', 'installtest', 'clean', 'distclean', 'uninstall',
               'makesum', 'srcpackage')


class PortDefaults(object):
    def __init__(self, graph, scope, cwd, persistence):
        self.source = Source('<port defaults: ' + cwd + '>', '')
        self.span = self.source.span(0, 0)
        self.stages = []
        self.hooks = []
        self.definitions = []
        self.marker_observations = []
        # Validate before graph mutation. Port initialization belongs after
        # recipe reading; a new invocation should read/register a fresh graph.
        for name in [s[0] for s in STAGES] + ['rundepend']:
            node = graph.find_node(name, cwd)
            if node is not None and node.definitions:
                raise SemanticError(self, 'port stage already defined: ' + name)
        for name in ('PORTNAME', 'PORTVERSION', 'PORTCOMMENT', 'PORTDESCR'):
            value = scope.lookup(name)
            if value is MISSING or not value:
                raise SemanticError(self, 'missing or empty port variable: ' + name)
        def marker(name):
            path = posixpath.join(cwd, 'done', name)
            exists = persistence.marker_exists(path)
            if type(exists) is not bool:
                raise ValueError('port marker existence must be bool')
            self.marker_observations.append((path, exists))
            return exists
        # CVS is not the archive path characterized here. Do not fake its
        # WRKSRC selection or its separate cvs-yes/cvs-no marker semantics.
        if marker('cvs-yes') or (not marker('cvs-no') and
                scope.lookup('CVSMODULES') not in (MISSING, '', None) and
                scope.lookup('CVS') != 'no'):
            raise Unsupported(self, 'CVS port initialization is deferred')
        text = 'all: build\n'
        previous = None
        specs = list(STAGES) + [(name, '', False) for name in INDEPENDENT]
        for index, (name, check, create) in enumerate(specs):
            parent = previous if index < len(STAGES) else None
            text += name + ' {virtual}: ' + (parent or '') + '\n'
            skipped = bool(check) and marker(check)
            hooks = []
            if not skipped:
                for prefix in ('pre-', 'do-', 'post-'):
                    node = graph.find_node(prefix + name, cwd)
                    if node is not None:
                        hooks.append(node)
                        text += '  :update ' + prefix + name + '\n'
                    elif prefix == 'do-':
                        text += '  @port_' + name + '(globals())\n'
                if create:
                    text += '  :mkdir {force} done\n  :touch {force} done/' + name + '\n'
                if name == 'install':
                    text += '  :update rundepend\n  :update installtest\n'
            self.hooks.extend(hooks)
            self.stages.append((name, parent, check, create, skipped))
            previous = name
        self.source = Source(self.source.source_id, text)
        self.span = self.source.span(0, len(text))
        program = lower(parse(self.source))
        for node in self.hooks:
            node.attributes['virtual'] = 1
        for statement in program.statements:
            self.definitions.append(graph.register(statement, scope, cwd))
        if scope.lookup('WRKSRC') in (MISSING, '', None):
            name, version = scope.lookup('PORTNAME'), scope.lookup('PORTVERSION')
            if type(name) is not str or type(version) is not str:
                raise Unsupported(self, 'port WRKSRC default requires string name/version')
            scope.store('WRKSRC', name + '-' + version, self)
