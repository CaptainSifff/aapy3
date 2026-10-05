"""Port.port_exe_cmd -> logged_system, distinct from Commands.aap_shell.

Only explicit unlogged synchronous requests; no host launcher or directory
mutation. The trusted ProcessBackend interprets the exact opaque shell string.
"""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .process import ProcessResult, ProcessUnavailable, ProcessBackendError
from .values import MISSING


class PortDirectories(object):
    """Observe entry into an existing directory, never create it or host chdir.

    Return its observed absolute cwd (allowing symlink resolution). Missing or
    inaccessible directories raise OSError; unavailable observation raises
    NotImplementedError. No success may be inferred from a process result.
    """
    def enter(self, path):
        raise NotImplementedError('port directory entry observation unavailable')


class MemoryPortDirectories(PortDirectories):
    def __init__(self, directories=()):
        self.directories = set(directories)
        self.observations = []

    def enter(self, path):
        self.observations.append(path)
        # This fake has no symlinks; the runtime itself does not collapse '..'.
        observed = posixpath.normpath(path)
        if observed not in self.directories:
            raise OSError('port command directory is missing: ' + path)
        return observed


class PortCommandPolicy(object):
    """Explicit observations of msg_logname and Global.sys_cmd_log.

    None means unknown. Only False/False is implemented; never infer this
    policy from :sys mode, recipe variables or absence of a logger adapter.
    """
    def __init__(self, log_active=None, capture_active=None):
        for value in (log_active, capture_active):
            if value is not None and type(value) is not bool:
                raise ValueError('port logging state requires bool or None')
        self.log_active, self.capture_active = log_active, capture_active


class PortCommandRequest(Node):
    def __init__(self, origin, command, cwd, policy, operation='port_exe_cmd'):
        super(PortCommandRequest, self).__init__(origin)
        self.operation = operation
        self.command = command
        self.command_bytes = policy.encode(command, origin)
        # get_sys_option consumes leading whitespace even without attributes.
        self.shell_command = command.lstrip(' \t') + '\n'
        self.shell_command_bytes = policy.encode(self.shell_command, origin)
        self.cwd, self.cwd_bytes = cwd, policy.encode(cwd, origin)
        if b'\x00' in self.shell_command_bytes or b'\x00' in self.cwd_bytes:
            raise SemanticError(origin, 'encoded port command/cwd must be NUL-free')
        self.shell_mode, self.shell_required = 'posix-sh', True
        self.capture_stdout = False
        self.stdin_policy = self.stdout_policy = self.stderr_policy = 'inherit'
        self.environment, self.environment_policy = None, 'inherit-backend'
        self.echo, self.echo_text = True, command
        self.logging = False
        self.skip_in_dry_run = False  # port_exe_cmd has no skip_commands check


class PortCommandRecord(Node):
    def __init__(self, origin, command, caller_cwd, operation='port_exe_cmd'):
        super(PortCommandRecord, self).__init__(origin)
        self.command, self.caller_cwd = command, caller_cwd
        self.operation = operation
        self.selected_cwd = None
        self.request = self.result = None
        self.status = self.reason = self.error = None
        self.output = None


def _directory_value(scope, name, origin):
    value = scope.lookup(name)
    if value is MISSING or value is None:
        raise SemanticError(origin, 'missing port directory variable: ' + name)
    if type(value) is not str:
        raise Unsupported(origin, 'port directory requires a characterized string: ' + name)
    if '\x00' in value:
        raise SemanticError(origin, 'NUL in port directory variable: ' + name)
    return value


class PortCommandRuntime(object):
    def __init__(self, directories=None, policy=None):
        self.directories = directories if directories is not None else PortDirectories()
        self.policy = policy if policy is not None else PortCommandPolicy()

    def execute(self, command, dirname, scope, cwd, evaluator, origin, records):
        record = PortCommandRecord(origin, command, cwd)
        records.append(record)
        if evaluator is not None and evaluator.last_result is not None:
            evaluator.last_result.processes.append(record)
        try:
            if type(command) is not str or type(cwd) is not str or not posixpath.isabs(cwd):
                raise Unsupported(origin, 'port command requires string command and explicit absolute cwd')
            if '\x00' in command or '\x00' in cwd:
                raise SemanticError(origin, 'NUL in port command/cwd')
            directory = scope.lookup(dirname)
            if directory is MISSING or directory is None or directory == '':
                directory = _directory_value(scope, 'WRKSRC', origin)
            elif type(directory) is not str:
                raise Unsupported(origin, 'port stage directory requires a characterized string')
            if '\x00' in directory:
                raise SemanticError(origin, 'NUL in port stage directory')
            work = _directory_value(scope, 'WRKDIR', origin)
            path = posixpath.join(cwd, work, directory)
            self._execute_at(record, command, path, evaluator, origin)
        except (ProcessUnavailable, NotImplementedError) as error:
            record.status, record.reason = 'BLOCKED', 'port_capability_unavailable'
            record.error = Unsupported(origin, str(error))
            raise record.error
        except Unsupported as error:
            record.status, record.reason, record.error = 'BLOCKED', 'unsupported_port_command', error
            raise
        except SemanticError as error:
            record.status, record.reason, record.error = 'FAILED', 'port_command_error', error
            raise
        except (ProcessBackendError, OSError, ValueError) as error:
            record.status, record.reason = 'FAILED', 'port_backend_error'
            record.error = SemanticError(origin, 'port command failed: ' + str(error))
            raise record.error
        return record

    def execute_at(self, command, path, cwd, evaluator, origin, records,
                   operation='port_patch'):
        """Run historical logged_system text at an explicit observed cwd."""
        record = PortCommandRecord(origin, command, cwd, operation)
        records.append(record)
        if evaluator is not None and evaluator.last_result is not None:
            evaluator.last_result.processes.append(record)
        try:
            if (type(command) is not str or type(path) is not str
                    or not posixpath.isabs(path) or type(cwd) is not str
                    or not posixpath.isabs(cwd)):
                raise Unsupported(origin, 'port patch requires string command and absolute cwd')
            if '\x00' in command or '\x00' in path or '\x00' in cwd:
                raise SemanticError(origin, 'NUL in port patch command/cwd')
            self._execute_at(record, command, path, evaluator, origin)
        except (ProcessUnavailable, NotImplementedError) as error:
            record.status, record.reason = 'BLOCKED', 'port_capability_unavailable'
            record.error = Unsupported(origin, str(error))
            raise record.error
        except Unsupported as error:
            record.status, record.reason, record.error = 'BLOCKED', 'unsupported_port_command', error
            raise
        except SemanticError as error:
            record.status, record.reason, record.error = 'FAILED', 'port_command_error', error
            raise
        except (ProcessBackendError, OSError, ValueError) as error:
            record.status, record.reason = 'FAILED', 'port_backend_error'
            record.error = SemanticError(origin, 'port command failed: ' + str(error))
            raise record.error
        return record

    def _execute_at(self, record, command, path, evaluator, origin):
        observed = self.directories.enter(path)
        if type(observed) is not str or not posixpath.isabs(observed) or '\x00' in observed:
            raise SemanticError(origin, 'invalid port cwd observation')
        record.selected_cwd = observed
        # Only port_exe_cmd has historical in-process aap dispatch.
        if (record.operation == 'port_exe_cmd'
                and (command == 'aap' or command.startswith('aap '))):
            raise Unsupported(origin, 'port aap_execute child recipe execution is deferred')
        if self.policy.log_active is not False or self.policy.capture_active is not False:
            raise Unsupported(origin, 'port logged_system requires explicit inactive log and capture state')
        if '\n' in command or '\r' in command or command.lstrip().startswith('{'):
            raise Unsupported(origin, 'port logged_system multiline/options are deferred')
        if evaluator is None or evaluator.process is None:
            raise ProcessUnavailable('port command process capability unavailable')
        process = evaluator.process
        record.request = PortCommandRequest(origin, command, observed,
                                            process.policy, record.operation)
        result = process.backend.run(record.request)
        if (not isinstance(result, ProcessResult) or type(result.wait_status) is not int
                or not 0 <= result.wait_status <= 65535):
            raise SemanticError(origin, 'port process backend must return a POSIX wait status')
        record.result = result
        if type(result.stdout) is not bytes or (result.stderr is not None and type(result.stderr) is not bytes):
            raise SemanticError(origin, 'port stream observations must be bytes')
        # Unlike :sys, do not store sysresult, expand text or parse a shell AST.
        if result.wait_status:
            if record.operation == 'port_patch':
                raise SemanticError(origin, 'Shell returned %d when patching:\n%s'
                                    % (result.wait_status, command))
            raise SemanticError(origin, 'shell returned ' + str(result.wait_status) + ' in port_exe_cmd')
        record.status, record.reason = 'COMPLETED', 'port_shell_success'
