"""Archive-port preparation and bounded done-marker effects via capabilities.

Port.port_fetch and Commands.aap_mkdir/aap_touch are the evidence. This module
never downloads or opens a host file. Actions re-enter the ordinary evaluator;
port commands use a distinct injected process route, never a host launcher.
"""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .dependency_items import parse_items
from .expansion import render_value, expand_text
from .values import MISSING
from .checksum import ArtifactUnavailable
from .port_defaults import STAGES
from .command_items import items, attributes
from .actions import ActionRuntime, ActionStopped
from .port_commands import PortCommandRuntime
from .port_delete import DeleteBackend, DeleteRequest, DeleteResult
from .recipe_mutation import RecipeMutationBackend
from .port_makesum import makesum
from .fetch import FetchBackend, FetchRequest, FetchResult


class MarkerBackend(object):
    """Immediate recipe directory and marker effects, independent of signatures.

    Paths are absolute. mkdir(path) retains generated marker behavior;
    mkdir(path, mode, require_parent) creates one directory with the requested
    octal mode, optionally requiring its parent to be a directory.
    path_kind observes missing, directory, or other existing path for the
    bounded mode form. touch preserves contents and refreshes times.
    Implementations must report OSError for effects that failed, and
    NotImplementedError for unavailable capabilities. No disk adapter here.
    """
    def marker_exists(self, path):
        raise NotImplementedError('marker observations unavailable')

    def path_kind(self, path):
        raise NotImplementedError('directory kind observations unavailable')

    def mkdir(self, path, mode=None, require_parent=False):
        raise NotImplementedError('marker directory creation unavailable')

    def touch(self, path):
        raise NotImplementedError('marker touch unavailable')

    def create_exclusive(self, path):
        raise NotImplementedError('exclusive marker creation unavailable')


class MemoryMarkers(MarkerBackend):
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.directories = set(posixpath.dirname(p) for p in self.files)
        self.operations = []
        self.modes = {}
        self.times = {}
        self.clock = 0

    def marker_exists(self, path):
        return path in self.files or path in self.directories

    def path_kind(self, path):
        if path == '/':
            return 'directory'
        if path in self.directories:
            return 'directory'
        if path in self.files:
            return 'other'
        return 'missing'

    def mkdir(self, path, mode=None, require_parent=False):
        if path in self.files:
            raise OSError('marker directory is a file: ' + path)
        if mode is not None:
            if path in self.directories:
                raise OSError('directory already exists: ' + path)
            if posixpath.dirname(path) not in self.directories:
                raise OSError('directory parent missing: ' + path)
            self.directories.add(path)
            self.modes[path] = mode
            self.operations.append(('mkdir', path, mode))
            return
        if require_parent and posixpath.dirname(path) not in self.directories:
            raise OSError('directory parent missing: ' + path)
        self.directories.add(path)
        self.operations.append(('mkdir', path))

    def _create(self, path):
        if posixpath.dirname(path) not in self.directories:
            raise OSError('marker parent directory missing: ' + path)
        self.files[path] = b''

    def touch(self, path):
        if not self.marker_exists(path):
            self._create(path)
        self.clock += 1
        self.times[path] = self.clock
        self.operations.append(('touch', path))

    def create_exclusive(self, path):
        if self.marker_exists(path):
            raise OSError('exclusive marker already exists: ' + path)
        self._create(path)
        self.clock += 1
        self.times[path] = self.clock
        self.operations.append(('create_exclusive', path))


class PortOperation(Node):
    def __init__(self, origin, operation, scope, cwd):
        super(PortOperation, self).__init__(origin)
        self.operation, self.scope, self.cwd = operation, scope, cwd
        self.status = None
        self.reason = None
        self.paths = []
        self.error = None
        self.actions = []
        self.processes = []
        self.messages = []
        self.observations = []
        self.deletions = []
        self.mutations = []
        self.checksums = []
        self.fetches = []


class PortMessage(Node):
    """A historical port diagnostic, recorded without host output or logging."""
    def __init__(self, origin, kind, text):
        super(PortMessage, self).__init__(origin)
        self.kind, self.text = kind, text


class PortRuntime(object):
    HELPERS = frozenset(('port_fetch', 'port_extract', 'port_patch', 'port_config',
                         'port_build', 'port_testdepend', 'port_test',
                         'port_checksum', 'port_installtest', 'port_srcpackage',
                         'port_clean', 'port_distclean', 'port_makesum'))
    MARKERS = frozenset(['cvs-yes', 'cvs-no'] + [name for name, check, create in STAGES if create])

    def __init__(self, artifacts=None, markers=None, actions=None, commands=None,
                 deletions=None, recipe_mutations=None, fetch_backend=None):
        self.artifacts = artifacts
        self.actions = actions if actions is not None else ActionRuntime()
        self.markers = markers if markers is not None else MarkerBackend()
        self.commands = commands if commands is not None else PortCommandRuntime()
        self.deletions = deletions if deletions is not None else DeleteBackend()
        self.recipe_mutations = (recipe_mutations if recipe_mutations is not None
                                 else RecipeMutationBackend())
        self.fetch_backend = fetch_backend if fetch_backend is not None else FetchBackend()

    def _value(self, scope, name, origin):
        value = scope.lookup(name)
        if value is MISSING or value is None:
            return ''
        if type(value) is not str:
            raise Unsupported(origin, 'port variable requires a characterized string: ' + name)
        return value

    def _path(self, cwd, name, origin):
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(origin, 'port operation requires an explicit absolute cwd')
        if '\x00' in name:
            raise SemanticError(origin, 'NUL in port path')
        # Do not collapse .. across possibly symbolic components.
        return posixpath.join(cwd, name)

    def _exists(self, backend, path, marker=False):
        value = backend.marker_exists(path) if marker else backend.exists(path)
        if type(value) is not bool:
            raise ValueError('existence capability must return bool')
        return value

    def _items(self, value, origin):
        items = parse_items(value, origin)
        if any(i.attributes for i in items):
            raise Unsupported(origin, 'port archive item attributes are deferred')
        return items

    def _fetch_candidates(self, sites, filename, origin):
        parsed = items(sites, origin, label='port site')
        if any(attrs for name, attrs in parsed):
            raise Unsupported(origin, 'port site attributes are deferred')
        candidates = []
        for site, attrs in parsed:
            if not site:
                raise SemanticError(origin, 'empty port fetch site')
            candidate = posixpath.join(site, '%file%')
            candidate = candidate.replace('%file%', filename)
            candidate = candidate.replace('%basename%', posixpath.basename(filename))
            candidates.append(candidate)
        return tuple(candidates)

    def _fetch(self, record):
        scope, cwd = record.scope, record.cwd
        no = self._path(cwd, 'done/cvs-no', record)
        yes = self._path(cwd, 'done/cvs-yes', record)
        if self._exists(self.markers, yes, True) or (
                not self._exists(self.markers, no, True)
                and self._value(scope, 'CVSMODULES', record)
                and self._value(scope, 'CVS', record) != 'no'):
            raise Unsupported(record, 'CVS port fetching is deferred')
        dist = self._value(scope, 'DISTFILES', record)
        patches = self._value(scope, 'PATCHFILES', record)
        if patches and not self._value(scope, 'PATCH_SITES', record):
            raise SemanticError(record, 'patch files defined but PATCH_SITES not defined')
        if not self._value(scope, 'EXTRACTFILES', record):
            scope.store('EXTRACTFILES', dist, record)
        for value, directory, sites in ((dist, 'DISTDIR', 'MASTER_SITES'),
                                         (patches, 'PATCHDISTDIR', 'PATCH_SITES')):
            items = self._items(value, record)
            # Upstream parses sites before observing archives. URL strings
            # need no interpretation on the already-present path. Attributes,
            # quotes and computed/deferred values outside this subset block.
            site_value = self._value(scope, sites, record)
            if any(c in site_value for c in '{}\'"'):
                raise Unsupported(record, 'port site attributes/quoting are deferred')
            dest = self._value(scope, directory, record)
            for item in items:
                path = self._path(cwd, posixpath.join(dest, posixpath.basename(item.name)), record)
                record.paths.append(path)
                if self.artifacts is None:
                    raise Unsupported(record, 'port artifact observations unavailable')
                if not self._exists(self.artifacts, path):
                    candidates = self._fetch_candidates(site_value, item.name, record)
                    if not candidates:
                        raise SemanticError(record, 'port fetch site list is empty: ' + sites)
                    request = FetchRequest(record, path, candidates, cwd, item.name,
                                           site_value)
                    result = self.fetch_backend.fetch(request)
                    record.fetches.append((request, result))
                    if not isinstance(result, FetchResult):
                        raise SemanticError(record, 'invalid port fetch result')
                    if result.status != 'COMPLETED':
                        raise SemanticError(record, 'Obtaining "' + item.name + '" failed: '
                                            + result.detail)
                    if result.selected not in request.candidates:
                        raise SemanticError(record, 'port fetch selected an unknown candidate')
                    if not self._exists(self.artifacts, path):
                        raise SemanticError(record, 'port fetch reported success without artifact: '
                                            + path)
        # Historical touch_file uses O_EXCL, unlike :touch {force}.
        self.markers.mkdir(self._path(cwd, 'done', record))
        self.markers.create_exclusive(no)

    def _extract(self, record, evaluator):
        scope, cwd = record.scope, record.cwd
        archives = self._value(scope, 'EXTRACT_ONLY', record)
        if not archives:
            yes = self._path(cwd, 'done/cvs-yes', record)
            no = self._path(cwd, 'done/cvs-no', record)
            if self._exists(self.markers, yes, True) or (
                    not self._exists(self.markers, no, True)
                    and self._value(scope, 'CVSMODULES', record)
                    and self._value(scope, 'CVS', record) != 'no'):
                raise Unsupported(record, 'CVS extraction/CVSDISTFILES acquisition is deferred')
            archives = self._value(scope, 'DISTFILES', record)
        if not archives:
            return
        parsed = items(archives, record, label='extract')
        distdir = self._value(scope, 'DISTDIR', record)
        wrkdir = self._value(scope, 'WRKDIR', record)
        # Port reads these from Work defaults. Require the caller's explicit
        # environment instead of quietly supplying a new startup policy.
        if scope.lookup('WRKDIR') is MISSING or scope.lookup('DISTDIR') is MISSING:
            raise Unsupported(record, 'extraction requires bound DISTDIR and WRKDIR')
        def absolute(name):
            # Port uses abspath here (unlike the fetch path).
            return posixpath.normpath(self._path(cwd, name, record))
        resolved = []
        for name, attrs in parsed:
            if any(key not in ('distdir', 'extractdir', 'filetype', 'filetypehint') for key in attrs):
                raise Unsupported(record, 'extract item modifiers outside bounded subset')
            if any(type(value) is not str for value in attrs.values()):
                raise Unsupported(record, 'extract item attributes require string values')
            filename = absolute(posixpath.join(attrs.get('distdir', distdir), posixpath.basename(name)))
            resolved.append((filename, attrs))
        work = absolute(wrkdir)
        for filename, attrs in resolved:
            directory = posixpath.join(work, attrs['extractdir']) if 'extractdir' in attrs else work
            if '\x00' in directory:
                raise SemanticError(record, 'NUL in extraction directory')
            record.paths.append(filename)
            directory = self.actions.workspace.prepare_directory(directory)
            if type(directory) is not str or not posixpath.isabs(directory) or '\x00' in directory:
                raise ValueError('invalid action cwd observation')
            self.actions.invoke('extract', filename, attrs, directory, evaluator,
                                record, record.actions)
        # No marker here: the generated stage's commands run after success.

    def _patch(self, record, evaluator):
        scope, cwd = record.scope, record.cwd
        yes = self._path(cwd, 'done/cvs-yes', record)
        no = self._path(cwd, 'done/cvs-no', record)
        if self._exists(self.markers, yes, True) or (
                not self._exists(self.markers, no, True)
                and self._value(scope, 'CVSMODULES', record)
                and self._value(scope, 'CVS', record) != 'no'):
            raise Unsupported(record, 'CVS patch selection is deferred')
        patchfiles = self._value(scope, 'PATCHFILES', record)
        if not patchfiles:
            # Port.port_patch returns before path/cmd lookup when empty.
            return
        patchlist = self._items(patchfiles, record)
        if scope.lookup('PATCHDISTDIR') is MISSING or scope.lookup('WRKDIR') is MISSING:
            raise Unsupported(record, 'patch requires bound PATCHDISTDIR and WRKDIR')
        patchdistdir = posixpath.normpath(self._path(cwd,
            self._value(scope, 'PATCHDISTDIR', record), record))
        workdir = posixpath.normpath(self._path(cwd,
            self._value(scope, 'WRKDIR', record), record))
        for item in patchlist:
            source = posixpath.join(patchdistdir, posixpath.basename(item.name))
            record.paths.append(source)
            command = self._value(scope, 'PATCHCMD', record)
            if not command:
                command = 'patch -p -f -s < '
            if '%s' in command:
                command = command.replace('%s', source, 1)
            else:
                command += source  # Port.py explicitly leaves shell quoting TODO.
            directory = self._value(scope, 'PATCHDIR', record)
            if not directory:
                directory = self._value(scope, 'WRKSRC', record)
            selected = posixpath.join(workdir, directory)
            self.commands.execute_at(command, selected, cwd, evaluator, record,
                                     record.processes)
        # The generated stage, not this helper, writes done/patch afterwards.

    def _config(self, record, evaluator):
        command = self._value(record.scope, 'CONFIGURECMD', record)
        if not command:
            self._diagnostic(record, 'extra', 'No CONFIGURECMD specified')
            return
        self.commands.execute(command, 'BUILDDIR', record.scope, record.cwd,
                              evaluator, record, record.processes)

    def _build(self, record, evaluator):
        self._default_command(record, evaluator, 'BUILDCMD', 'aap', 'BUILDDIR')

    def _default_command(self, record, evaluator, variable, default, dirname):
        command = record.scope.lookup(variable)
        # Port.port_build defaults on Python truth, unlike port_config's no-op.
        # Only apply that truth test to values in our characterized value model.
        if command is MISSING or command is None or (
                type(command) in (str, int, bool, list, tuple) and not command):
            command = default
        elif type(command) is not str:
            raise Unsupported(record, 'port command requires a characterized string: ' + variable)
        self.commands.execute(command, dirname, record.scope, record.cwd,
                              evaluator, record, record.processes)

    def _testdepend(self, record):
        # Port.port_testdepend -> depend_do: these guards return before any
        # dependency expression parsing, observation or acquisition.
        if self._value(record.scope, 'SKIPTEST', record) == 'yes':
            return
        if self._value(record.scope, 'AUTODEPEND', record) == 'no':
            return
        if self._value(record.scope, 'DEPEND_TEST', record):
            raise Unsupported(record, 'nonempty port test dependency handling is deferred')

    def _test(self, record, evaluator):
        if self._value(record.scope, 'SKIPTEST', record) != 'yes':
            self._default_command(record, evaluator, 'TESTCMD', 'aap test', 'TESTDIR')

    def _diagnostic(self, record, kind, text):
        record.messages.append(PortMessage(record, kind, text))

    def _clean_value(self, record, name):
        value = record.scope.lookup(name)
        if value is MISSING or value is None or type(value) is not str or not value:
            raise Unsupported(record, 'port cleanup requires a bound nonempty string: ' + name)
        return value

    def _clean(self, record, evaluator, distclean=False):
        # Port.py::port_clean constructs the complete ordered list first.
        paths = ['done', self._clean_value(record, 'WRKDIR'),
                 self._clean_value(record, 'PKGDIR'),
                 'pkg-plist', 'pkg-comment', 'pkg-descr']
        if distclean:
            revision = self._value(record.scope, 'PORTREVISION', record)
            package = (self._clean_value(record, 'PORTNAME') + '-'
                       + self._clean_value(record, 'PORTVERSION'))
            if revision:
                package += '_' + revision
            paths.extend([self._clean_value(record, 'DISTDIR'),
                          self._clean_value(record, 'PATCHDISTDIR'),
                          package + '.tgz', 'AAPDIR'])
        if evaluator is None:
            raise Unsupported(record, 'port cleanup requires path observation runtime')
        observations = evaluator.python.helpers.paths
        for argument in paths:
            path = self._path(record.cwd, argument, record)
            record.paths.append(path)
            before = len(observations.records)
            try:
                exists = observations.exists(argument, record.cwd, record)
            finally:
                if len(observations.records) > before:
                    record.observations.append(observations.records[-1])
            if not exists:
                continue
            request = DeleteRequest(record, path, argument, record.cwd)
            try:
                outcome = self.deletions.delete_tree(request)
            except NotImplementedError as error:
                raise Unsupported(record, 'port deletion unavailable: ' + str(error))
            except OSError as error:
                raise SemanticError(record, 'Cannot delete "' + argument + '": ' + str(error))
            record.deletions.append((request, outcome))
            if not isinstance(outcome, DeleteResult):
                raise SemanticError(record, 'invalid port deletion result')
            if outcome.status == 'UNAVAILABLE':
                raise Unsupported(record, 'port deletion unavailable: ' + str(outcome.detail))
            if outcome.status != 'COMPLETED':
                raise SemanticError(record, 'Cannot delete "' + argument + '": ' + str(outcome.detail))

    def call(self, name, scope, cwd, origin, records, evaluator=None):
        if name not in self.HELPERS:
            raise Unsupported(origin, 'port helper is deferred: ' + name)
        record = PortOperation(origin, name, scope, cwd)
        records.append(record)
        operations = {'port_fetch': lambda: self._fetch(record),
                      'port_extract': lambda: self._extract(record, evaluator),
                      'port_patch': lambda: self._patch(record, evaluator),
                      'port_config': lambda: self._config(record, evaluator),
                      'port_build': lambda: self._build(record, evaluator),
                      'port_testdepend': lambda: self._testdepend(record),
                      'port_test': lambda: self._test(record, evaluator),
                      'port_checksum': lambda: self._diagnostic(
                          record, 'extra',
                          'No do-checksum target defined; checking checksums skipped'),
                      'port_installtest': lambda: self._diagnostic(
                          record, 'extra', 'Default installtest: do nothing'),
                      'port_srcpackage': lambda: self._diagnostic(
                          record, 'info', 'TODO: srcpackage'),
                      'port_clean': lambda: self._clean(record, evaluator),
                      'port_distclean': lambda: self._clean(record, evaluator, True),
                      'port_makesum': lambda: makesum(self, record, evaluator)}
        operation = operations[name]
        self._perform(record, operation)
        return None

    def marker_command(self, command, scope, python, cwd, records):
        raw = render_value(command.arguments, python).strip()
        if command.body is not None:
            raise Unsupported(command, 'only force port-marker commands are supported')
        if command.name == 'mkdir' and not raw.startswith('{force}'):
            return self._mkdir(command, raw, scope, cwd, records)
        if not raw.startswith('{force}'):
            raise Unsupported(command, 'only force port-marker commands are supported')
        text = expand_text(raw[len('{force}'):], scope, command)
        items = self._items(text, command)
        if len(items) != 1:
            raise Unsupported(command, 'port-marker command requires exactly one path')
        name = items[0].name
        if ((command.name == 'mkdir' and name != 'done') or
                (command.name == 'touch' and (not name.startswith('done/') or
                                              name[5:] not in self.MARKERS))):
            raise Unsupported(command, 'filesystem command is outside the port-marker subset')
        record = PortOperation(command, command.name, scope, cwd)
        records.append(record)
        path = self._path(cwd, name, record)
        record.paths.append(path)
        operation = self.markers.mkdir if command.name == 'mkdir' else self.markers.touch
        self._perform(record, lambda: operation(path))

    def _mkdir(self, command, raw, scope, cwd, records):
        text = expand_text(raw, scope, command, item_attributes=True)
        options, start = attributes(text, 0, command, label='mkdir')
        parsed = items(text[start:], command, label='mkdir')
        if len(parsed) != 1:
            raise Unsupported(command, 'bounded mkdir requires exactly one directory')
        name, item_attrs = parsed[0]
        if set(options) == set(['r']) and not item_attrs:
            return self._recursive_mkdir(command, name, scope, cwd, records)
        if options:
            raise Unsupported(command, 'only the r mkdir option is supported')
        if (set(item_attrs) != set(['mode']) or type(item_attrs['mode']) is not str
                or not item_attrs['mode'] or
                any(char not in '01234567' for char in item_attrs['mode'])):
            raise Unsupported(command, 'only one octal mode attribute is supported for mkdir')
        if (not name or name in ('.', '..') or ':' in name
                or name.startswith('~')):
            raise Unsupported(command, 'mode mkdir requires one local directory path')
        mode = int(item_attrs['mode'], 8)  # Util.oct2int, not Python literal syntax.
        record = PortOperation(command, 'mkdir', scope, cwd)
        record.mode = mode
        records.append(record)
        path = self._path(cwd, name, record)
        record.paths.append(path)

        def create():
            kind = self.markers.path_kind(path)
            record.observations.append(('path_kind', path, kind))
            if kind == 'directory':
                raise SemanticError(record, '"' + path + '" already exists')
            if kind == 'other':
                raise SemanticError(record, '"' + path + '" exists but is not a directory')
            if kind != 'missing':
                raise ValueError('invalid directory kind observation: ' + str(kind))
            self.markers.mkdir(path, mode)

        self._perform(record, create)

    def _recursive_mkdir(self, command, name, scope, cwd, records):
        # Commands.aap_mkdir() calls os.makedirs(adir) for {r} with no mode.
        # Dot components, home expansion and remote URLs need filesystem
        # identity semantics beyond the reached production path.
        if (not name or name in ('.', '..') or name.endswith('/') or ':' in name
                or name.startswith('~') or any(piece in ('.', '..')
                                                for piece in name.split('/'))):
            raise Unsupported(command, 'recursive mkdir path form is deferred')
        record = PortOperation(command, 'mkdir', scope, cwd)
        record.recursive = True
        record.mode = None
        records.append(record)
        path = self._path(cwd, name, record)
        record.paths.append(path)

        def create():
            # aap_mkdir checks the requested directory before calling
            # os.makedirs(), giving its own final-directory diagnostics.
            final_kind = self.markers.path_kind(path)
            record.observations.append(('path_kind', path, final_kind))
            if final_kind == 'directory':
                raise SemanticError(record, '"' + path + '" already exists')
            if final_kind == 'other':
                raise SemanticError(record, '"' + path + '" exists but is not a directory')
            if final_kind != 'missing':
                raise ValueError('invalid directory kind observation: ' + str(final_kind))
            # Python 2 os.makedirs() creates missing ancestors from outermost
            # to innermost.  Existing non-directory ancestors are left for the
            # next mkdir call to fail, retaining any earlier created parents.
            for parent in self._recursive_parents(path):
                kind = self.markers.path_kind(parent)
                record.observations.append(('path_kind', parent, kind))
                if kind == 'missing':
                    self.markers.mkdir(parent, None, True)
            self.markers.mkdir(path, None, True)

        self._perform(record, create)

    def _recursive_parents(self, path):
        """Parent-first absolute components matching Python-2 os.makedirs()."""
        result = []
        current = ''
        for component in path.split('/')[:-1]:
            if not component:
                continue
            current += '/' + component
            result.append(current)
        return result

    def _perform(self, record, operation):
        try:
            operation()
            record.status, record.reason = 'COMPLETED', 'port_operation_completed'
        except (NotImplementedError, ArtifactUnavailable) as error:
            record.status, record.reason = 'BLOCKED', 'capability_unavailable'
            record.error = error
            raise Unsupported(record, str(error))
        except ActionStopped as stopped:
            cause = stopped.result
            record.status, record.reason, record.error = cause.status, cause.reason, cause.error
            raise
        except Unsupported as error:
            record.status, record.reason, record.error = 'BLOCKED', 'unsupported_semantics', error
            raise
        except SemanticError as error:
            record.status, record.reason, record.error = 'FAILED', 'semantic_error', error
            raise
        except (OSError, ValueError) as error:
            record.status, record.reason, record.error = 'FAILED', 'port_operation_failed', error
            raise SemanticError(record, 'port operation failed: ' + str(error))
