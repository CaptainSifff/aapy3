"""Metadata evaluation; unsupported runtime constructs are explicit barriers."""
import posixpath

from aap_frontend import Source, parse, cst
from . import model as m
from .declarations import DeclarationState
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text
from .includes import IncludeRecord, resolve_path
from .lowering import lower
from .python_eval import PythonEvaluator
from .process import ProcessRuntime
from .system_process import execute_system, sys_batch_nodes
from .checksum import ChecksumRuntime
from .nested_update import UpdateRequest, UpdateStopped
from .graph import BuildGraph
from .scopes import Scope
from .values import MISSING, DeferredExpansion, truth, var2list
from .directories import ExecutionDirectoryState, change_directory
from .python_shell import ShellBlockRuntime, approve as approve_shell_block
from .tree_runtime import TreeRuntime
from .move_runtime import MoveRuntime
from .copy_runtime import CopyRuntime
from .capabilities import RuntimeCapabilities


class EvaluationResult(object):
    def __init__(self, program, scope, declarations, graph):
        self.program = program
        self.scope = scope
        self.deferred = []
        self.halted_at = None
        self.complete = False
        self.declarations = declarations
        self.includes = []
        self.cats = []
        self.prints = []
        self.python_writes = []
        self.path_observations = []
        self.processes = []
        self.checksums = []
        self.updates = []
        self.port_operations = []
        self.directory_changes = []
        self.tree_records = []
        self.moves = []
        self.copies = []
        self.final_cwd = None
        self.graph = graph


class _Barrier(Exception):
    pass


class Evaluator(object):
    def __init__(self, scope=None, helpers=None, max_steps=10000,
                 declarations=None, include_loader=None, cwd=None,
                 max_include_depth=64, process_backend=None, process_policy=None,
                 graph=None, checksum_backend=None, update_driver=None,
                 execution_context=None, port_runtime=None, path_observer=None, output_runtime=None, cat_runtime=None,
                 tree_filesystem=None, move_backend=None, copy_backend=None,
                 capabilities=None):
        self.capabilities = (capabilities if capabilities is not None else
            RuntimeCapabilities(process_backend, process_policy, include_loader,
                checksum_backend, port_runtime, path_observer, output_runtime,
                cat_runtime, tree_filesystem, move_backend, copy_backend))
        caps = self.capabilities
        self.scope = scope if scope is not None else Scope.top_level()
        self.python = PythonEvaluator(self.scope, helpers, max_steps)
        if caps.path_observer is not None:
            self.python.helpers.paths.observer = caps.path_observer
        self.path_observer = self.python.helpers.paths.observer
        self.trees = {}
        self.declarations = declarations if declarations is not None else DeclarationState()
        self.include_loader = caps.include_loader
        helper_cwd = self.python.helpers.cwd
        if cwd is None:
            cwd = helper_cwd
        elif helper_cwd is not None and posixpath.normpath(cwd) != posixpath.normpath(helper_cwd):
            raise ValueError('include and helper recipe directories must agree')
        if cwd is not None and not posixpath.isabs(cwd):
            raise ValueError('metadata cwd must be absolute')
        self.directory = (execution_context.directory if execution_context is not None
                          else ExecutionDirectoryState(cwd))
        self.active_sources = set()
        self.max_include_depth = max_include_depth
        if caps.process_backend is not None and caps.process_policy is None:
            raise ValueError('process backend requires an explicit byte/text policy')
        self.process = (ProcessRuntime(caps.process_backend, caps.process_policy)
                        if caps.process_backend is not None else None)
        self.graph = graph if graph is not None else BuildGraph()
        self.last_result = None
        self.active_node = None
        self.checksum = ChecksumRuntime(caps.checksum_backend) if caps.checksum_backend is not None else None
        self.update_driver = update_driver
        self.execution_context = execution_context
        self.cat_runtime = caps.cat_runtime
        self.output_runtime = caps.output_runtime
        self.port_runtime = caps.port_runtime
        self.tree_runtime = TreeRuntime(caps.tree_filesystem) if caps.tree_filesystem is not None else None
        self.move_runtime = MoveRuntime(caps.move_backend) if caps.move_backend is not None else None
        self.copy_runtime = CopyRuntime(caps.copy_backend) if caps.copy_backend is not None else None
        self.python.port_runtime = caps.port_runtime
        self.python.evaluator_context = self

    @property
    def cwd(self):
        return self.directory.current

    @cwd.setter
    def cwd(self, value):
        self.directory.current = value

    def prepare(self, program, syntax_only=False):
        """Check syntax/interpreter policy, not availability of reached capabilities."""
        for node in program.statements:
            if isinstance(node, m.EmbeddedPython):
                self.trees[id(node.fragment)] = self.python.parse(
                    node.fragment, syntax_only=syntax_only)
            elif (isinstance(node, m.DeferredConstruct)
                  and isinstance(node.origin, cst.PythonBlock)):
                fragment = m.PythonFragment(node.origin, node.origin.body.cooked_text)
                try:
                    tree = self.python.parse(fragment, syntax_only=True)
                except SemanticError:
                    continue
                if approve_shell_block(tree):
                    self.trees[id(node)] = tree
            elif isinstance(node, m.Conditional):
                for condition, body in node.branches:
                    tree = self.python.parse(condition, syntax_only=syntax_only)
                    if len(tree.body) != 1 or type(tree.body[0]).__name__ != 'If':
                        raise SemanticError(condition, 'expected one Python if header')
                    self.trees[id(condition)] = tree.body[0]
                    self.prepare(body, syntax_only)
                if node.otherwise is not None:
                    self.prepare(node.otherwise, syntax_only)
            elif isinstance(node, m.Loop):
                tree = self.python.parse(node.header, syntax_only=syntax_only)
                if len(tree.body) != 1 or type(tree.body[0]).__name__ != 'For':
                    raise SemanticError(node.header, 'expected one Python for header')
                self.trees[id(node.header)] = tree.body[0]
                self.prepare(node.body, syntax_only)
            elif isinstance(node, m.Variant):
                # All branch syntax was compiled historically. Capabilities and
                # command arguments in an unselected branch are not evaluated.
                for value, body in node.branches:
                    self.prepare(body, syntax_only=True)
            elif isinstance(node, m.Assignment) and isinstance(node.value, m.ArgumentValue):
                for piece in node.value.pieces:
                    for part in piece:
                        if isinstance(part, m.PythonFragment):
                            self.python.parse(part, 'eval', syntax_only=syntax_only)

    def run(self, program, source_active=True):
        self.trees = {}
        self.python.steps = 0
        result = EvaluationResult(program, self.scope, self.declarations, self.graph)
        self.last_result = result
        self.python.port_records = result.port_operations
        self.python.helpers.paths.records = result.path_observations
        self.active_node = None
        self.prepare(program)
        source_id = program.span.source_id
        saved_cwd = self.cwd
        saved_helper_directory = self.python.helpers.directory
        if self.cwd is None and posixpath.isabs(source_id):
            self.cwd = posixpath.dirname(source_id)
        self.active_sources = set()
        if source_active and (posixpath.isabs(source_id) or self.cwd is not None):
            identity = source_id if posixpath.isabs(source_id) else posixpath.join(self.cwd, source_id)
            self.active_sources.add(posixpath.normpath(identity))
        self.python.helpers.directory = self.directory
        try:
            self.statements(program, result)
        except _Barrier:
            return result
        finally:
            self.active_sources.clear()
            result.final_cwd = self.cwd
            self.cwd = saved_cwd
            self.python.helpers.directory = saved_helper_directory
        result.complete = True
        return result

    def statements(self, program, result):
        index = 0
        while index < len(program.statements):
            node = program.statements[index]
            index += 1
            self.active_node = node
            self.python.step(node)
            if isinstance(node, m.Assignment):
                self.assignment(node)
            elif isinstance(node, m.EmbeddedPython):
                self.python.statements(self.trees[id(node.fragment)].body, node.fragment)
            elif isinstance(node, m.DeferredConstruct) and id(node) in self.trees:
                ShellBlockRuntime(self.python, self.output_runtime, self.cwd,
                                  result.python_writes).statements(self.trees[id(node)].body, node)
            elif isinstance(node, m.Conditional):
                selected = node.otherwise
                for condition, body in node.branches:
                    test = self.trees[id(condition)].test
                    if truth(self.python.value(test, condition), condition):
                        selected = body
                        break
                if selected is not None:
                    self.statements(selected, result)
            elif isinstance(node, m.Loop):
                header = self.trees[id(node.header)]
                for value in self.python.sequence(header.iter, node.header):
                    self.python.step(node)
                    self.python.assign(header.target, value, node.header)
                    self.statements(node.body, result)
            elif isinstance(node, m.Variant):
                self.variant(node, result)
            elif isinstance(node, m.Dependency):
                self.graph.register(node, self.scope, self.cwd)
            elif isinstance(node, m.Command) and node.name == 'cd':
                change_directory(self, node, result.directory_changes)
            elif (isinstance(node, m.Command) and node.name == 'sys'
                    and self.process is not None and self.process.policy.sys_mode is not None):
                batch, index, capture = sys_batch_nodes(program.statements, index - 1)
                for member in batch[1:]:
                    self.python.step(member)
                if capture is not None:
                    self.python.step(capture)
                    result.processes.append(self.process.capture(
                        capture, self.scope, self.python, self.cwd))
                execute_system(self.process, node, self.scope, self.python, self.cwd,
                               result.processes, batch)
            elif isinstance(node, m.Command) and node.name == 'syseval' and self.process is not None:
                result.processes.append(self.process.capture(
                    node, self.scope, self.python, self.cwd))
            elif isinstance(node, m.Command) and node.name == 'cat' and self.cat_runtime is not None:
                self.cat_runtime.execute(node, self.scope, self.python, self.cwd, result.cats)
            elif isinstance(node, m.Command) and node.name == 'print' and self.output_runtime is not None:
                self.output_runtime.execute(node, self.scope, self.python, self.cwd, result.prints)
            elif isinstance(node, m.Command) and node.name == 'checksum' and self.checksum is not None:
                self.checksum.verify(node, self.scope, self.python, self.cwd, result.checksums)
            elif isinstance(node, m.Command) and node.name == 'tree' and self.tree_runtime is not None:
                self.tree_runtime.execute(node, self, result)
            elif isinstance(node, m.Command) and node.name == 'move' and self.move_runtime is not None:
                self.move_runtime.execute(node, self.scope, self.python, self.cwd, result.moves)
            elif isinstance(node, m.Command) and node.name == 'copy' and self.copy_runtime is not None:
                self.copy_runtime.execute(node, self.scope, self.python, self.cwd, result.copies)
            elif isinstance(node, m.Command) and node.name == 'update' and self.update_driver is not None:
                request = UpdateRequest(node, self.scope, self.python, self.cwd,
                                        self.execution_context)
                update = self.update_driver.update_targets(request)
                result.updates.append(update)
                if update.status != 'COMPLETE':
                    result.halted_at = node
                    raise UpdateStopped(update)
            elif (isinstance(node, m.Command) and node.name in ('mkdir', 'touch')
                    and self.port_runtime is not None):
                self.port_runtime.marker_command(node, self.scope, self.python, self.cwd,
                                                  result.port_operations)
            elif isinstance(node, m.Command) and node.name in (
                    'filetype', 'action', 'include', 'pass'):
                self.command(node, result)
            else:
                # Even argument backticks on unsupported commands stay inert.
                result.deferred.append(node)
                result.halted_at = node
                raise _Barrier()

    def variant(self, node, result):
        first = node.branches[0][0]
        if first == '*':
            selected = node.branches[0][1]   # No default or BDIR update.
        else:
            value = self.scope.lookup(node.variable)
            if value is MISSING:
                value = first
                self.scope.store(node.variable, value, node)
            bdir = self.scope.read('BDIR', node)
            if type(value) is not str or type(bdir) is not str:
                raise Unsupported(node, 'variant value and BDIR must be strings')
            self.scope.store('BDIR', bdir + '-' + value, node)
            selected = None
            for label, body in node.branches:
                if label == value or label == '*':
                    selected = body
                    break
            if selected is None:
                raise SemanticError(node, 'invalid value for ' + node.variable + ': ' + value)
        self.prepare(selected)
        self.statements(selected, result)

    def command(self, node, result):
        raw = render_value(node.arguments, self.python)
        if node.name == 'pass':
            if raw:
                raise SemanticError(node, ':pass does not take an argument')
            return
        arguments = var2list(expand_text(raw, self.scope, node), node)
        if node.name == 'filetype':
            self.declarations.register_filetype(node, arguments, self.scope)
        elif node.name == 'action':
            self.declarations.register_action(node, arguments, self.scope)
        else:
            if len(arguments) != 1 or node.body is not None:
                raise Unsupported(node, 'only single-path includes without a body are supported')
            path = resolve_path(arguments[0], self.cwd, node)
            if path in self.active_sources:
                result.includes.append(IncludeRecord(node, path, skipped_active=True))
                return
            if self.include_loader is None:
                raise Unsupported(node, 'include requires an explicit source loader')
            if len(self.active_sources) >= self.max_include_depth:
                raise Unsupported(node, 'metadata include depth limit exceeded')
            try:
                source = self.include_loader.load(path)
            except (OSError, UnicodeError) as error:
                raise SemanticError(node, 'cannot read include ' + path + ': ' + str(error))
            if not isinstance(source, Source) or source.source_id != path:
                raise SemanticError(node, 'include loader must return a Source with the resolved identity')
            program = lower(parse(source, file_mode=True))
            result.includes.append(IncludeRecord(node, path, program))
            self.prepare(program)
            self.active_sources.add(path)
            try:
                self.statements(program, result)
            finally:
                self.active_sources.remove(path)

    def assignment(self, node):
        # Backtick evaluation precedes ?= existence checking and $= storage.
        raw = render_value(node.value, self.python)
        namespace, name = self.scope.target(node.target, node, create=True)
        previous = namespace.get(name)
        if node.mode == 'default' and previous is not MISSING:
            return
        value = raw if node.delayed else expand_text(raw, self.scope, node)
        if node.mode == 'append' and previous is not MISSING:
            if isinstance(previous, DeferredExpansion):
                raise Unsupported(node, 'appending to delayed expansion is deferred')
            if type(previous) in (int, bool):
                previous = str(previous)
            if type(previous) is not str:
                raise Unsupported(node, 'A-A-P append requires a string or integer value')
            if previous:
                value = previous + ' ' + value
        namespace.set(name, DeferredExpansion(value, node) if node.delayed else value, node)
