"""POSIX capture semantics behind an injected backend. No host launcher."""
import posixpath
import string

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text


class ProcessBackendError(Exception):
    """A backend could not launch or complete a request (not a shell failure)."""


class ProcessUnavailable(Exception):
    """A required process capability was not supplied."""


class ProcessBackend(object):
    def run(self, request):
        """Return ProcessResult; execute in request.cwd without host chdir.

        A concrete adapter must execute request.shell_command_bytes through a
        POSIX shell and honor the request's stream policies. Captures,
        bounded :sys and port_exe_cmd requests are distinct modes. Return the
        shell's encoded wait status for plain requests. A logged :sys request
        returns the decimal status recovered from its temporary shell status
        file. Recipe scope is never exported. No host launcher is implicit.
        """
        raise ProcessUnavailable('no process backend implementation')


class ProcessResult(object):
    def __init__(self, wait_status, stdout=b'', stderr=None, log_output=None,
                 shell_wait_status=None):
        # Plain requests retain encoded POSIX wait status. A logged :sys may
        # instead return the decimal status recovered from its shell wrapper.
        self.wait_status = wait_status
        self.stdout = stdout
        # Optional observation supplied by a backend/test; never capture input.
        self.stderr = stderr
        self.log_output = log_output
        self.shell_wait_status = shell_wait_status


class ProcessPolicy(object):
    def __init__(self, encoding, sys_mode=None, log_path=None):
        """Explicit byte codec; strict errors, never implicit locale decoding."""
        if sys_mode not in (None, 'unlogged', 'bounded-attributes'):
            raise ValueError('unsupported synchronous :sys mode')
        self.sys_mode = sys_mode
        self.encoding = encoding
        self.log_path = log_path

    def encode(self, value, origin):
        try:
            return value.encode(self.encoding, 'strict')
        except (UnicodeError, LookupError) as error:
            raise SemanticError(origin, 'process encoding error: ' + str(error))

    def capture(self, value, origin):
        if type(value) is str:
            value = self.encode(value, origin)
        if type(value) is not bytes:
            raise SemanticError(origin, 'process stdout must be bytes or text')
        # Python 2 byte-pattern \s, without LOCALE/UNICODE flags. Do this before
        # decoding: Unicode str.strip() would also eat e.g. Latin-1 NBSP.
        value = value.strip(b' \t\n\r\v\f')
        try:
            return value.decode(self.encoding, 'strict')
        except (UnicodeError, LookupError) as error:
            raise SemanticError(origin, 'process decoding error: ' + str(error))


class ProcessRequest(Node):
    def __init__(self, origin, command, cwd, policy):
        super(ProcessRequest, self).__init__(origin)
        self.command = command
        self.command_bytes = policy.encode(command, origin)
        # Preserve upstream grouping, including observable shell syntax errors
        # when expansion introduces an unterminated quote or trailing comment.
        self.shell_command = '(' + command + ')'
        self.shell_command_bytes = policy.encode(self.shell_command, origin)
        self.cwd = cwd
        self.cwd_bytes = policy.encode(cwd, origin)
        if b'\x00' in self.shell_command_bytes or b'\x00' in self.cwd_bytes:
            raise SemanticError(origin, 'encoded process command and cwd must be NUL-free')
        self.shell_mode = 'posix-sh'
        self.capture_stdout = True
        self.stderr_policy = 'inherit'
        self.stdin_policy = 'inherit'
        self.environment = None             # Backend environment, not scope.
        self.echo = False
        self.skip_in_dry_run = False         # aap_syseval never skip_commands().


class CapturePipeline(Node):
    """Handler argument syntax, separated before dollar expansion."""
    def __init__(self, origin, shell_source, target=None):
        super(CapturePipeline, self).__init__(origin)
        self.shell_source = shell_source
        self.target = target                # Raw name; never dollar-expanded.


class ProcessRecord(Node):
    def __init__(self, origin, pipeline, request, result, output):
        super(ProcessRecord, self).__init__(origin)
        self.pipeline = pipeline
        self.request = request
        self.result = result
        self.output = output
        # Bare syseval historically msg_print's one newline, even for empty
        # output. Retain the event for a presentation layer; don't print here.
        self.print_text = output + '\n' if pipeline.target is None else None


def capture_pipeline(raw, origin):
    """Bounded Commands._get_redir / Util.get_token argument reader.

    Quotes shield token boundaries, backslashes do not. Operators inside a
    token or introduced by later expansion remain ordinary shell text.
    """
    if raw.lstrip(' \t').startswith('{'):
        raise Unsupported(origin, ':syseval attributes are outside the production subset')
    index = 0
    while index < len(raw):
        if raw[index] in ' \t':
            index += 1
            continue
        start = index
        quote = None
        while index < len(raw):
            char = raw[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in '\"\'':
                quote = char
            elif char in ' \t':
                break
            index += 1
        token = raw[start:index]
        if token.startswith('>'):
            raise Unsupported(origin, 'A-A-P process output redirection is deferred')
        if token.startswith('|'):
            tail = raw[start + 1:].lstrip(' \t')
            if not tail.startswith(':'):
                raise SemanticError(origin, "missing ':' after A-A-P '|' token")
            fields = tail.split(None, 1)
            if fields[0] != ':assign':
                raise Unsupported(origin, 'only the :assign capture stage is supported')
            target = fields[1] if len(fields) == 2 else ''
            if '|' in target:
                raise Unsupported(origin, 'multiple A-A-P capture stages are deferred')
            return CapturePipeline(origin, raw[:start].rstrip(' \t'), target)
    return CapturePipeline(origin, raw.rstrip(' \t'))


class ProcessRuntime(object):
    def __init__(self, backend, policy):
        self.backend = backend
        self.policy = policy

    def capture(self, node, scope, python, cwd):
        # Structural backticks render first, redirection/pipeline recognition
        # follows, and only the shell portion then undergoes dollar expansion.
        pipeline = capture_pipeline(render_value(node.arguments, python), node)
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, 'process capture requires an absolute recipe directory')
        command = expand_text(pipeline.shell_source, scope, node)
        if '\x00' in command or '\x00' in cwd:
            raise SemanticError(node, 'NUL is not permitted in process command or cwd')
        request = ProcessRequest(node, command, cwd, self.policy)
        try:
            result = self.backend.run(request)
        except (ProcessUnavailable, NotImplementedError) as error:
            raise Unsupported(node, str(error))
        except (ProcessBackendError, OSError) as error:
            raise SemanticError(node, 'process backend failed: ' + str(error))
        if not isinstance(result, ProcessResult) or type(result.wait_status) is not int:
            raise SemanticError(node, 'process backend must return an integer POSIX wait status')
        # Nonzero status still delivers output. This never updates sysresult.
        scope.store('exit', result.wait_status, node)
        output = self.policy.capture(result.stdout, node)
        if pipeline.target is not None:
            target = pipeline.target
            if (not target or any(c not in string.ascii_letters + string.digits + '_.'
                                  for c in target)):
                raise SemanticError(node, 'invalid :assign target: ' + target)
            namespace, name = scope.target(target, node, create=True)
            namespace.set(name, output, node)
        return ProcessRecord(node, pipeline, request, result, output)
