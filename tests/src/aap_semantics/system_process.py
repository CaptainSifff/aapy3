"""Bounded synchronous :sys: plain shell forms and reached f/q/l attributes.

Adjacent plain sys entries form one newline-separated unlogged shell request.
Literal source force flags split that batch; logged execution stays in the
injected process backend rather than this semantic module.
No launcher, shell substitution or arbitrary shell grammar. Reached
redirections are ``command < input [> output]`` and ``command >> output``.
Source: Commands.aap_shell, Util.logged_system/get_var_val/expand_itemstr.
"""
from collections import namedtuple
import posixpath
import string

from .model import Node, Command, PythonFragment, LiteralValue
from .diagnostics import SemanticError, Unsupported
from .process import ProcessResult, ProcessUnavailable, ProcessBackendError
from .expansion import render_value, expand_text
from .command_items import items, attributes
from .values import _quote_item


# These require shell state/control, an alternate shell, or changed environment.
# This is a syntax boundary, not a sandbox for arbitrary executable programs.
_WRAPPERS = frozenset(('cd', 'env', 'sh', 'bash', 'dash', 'zsh', 'ksh', 'eval',
    'exec', 'command', 'source', '.', 'export', 'unset', 'set', 'exit', 'return',
    'trap', 'read', 'umask', 'ulimit', 'alias', 'unalias', 'break', 'continue',
    'shift', 'wait', 'jobs', 'fg', 'bg', 'if', 'then', 'else', 'elif', 'fi',
    'do', 'done', 'case', 'esac', 'while', 'until', 'for', 'select', 'function',
    'time', 'coproc', '['))
# POSIX pathname-pattern characters stay in the shell command.  A-A-P does
# not glob :sys arguments: aap_shell expands values, then logged_system passes
# the resulting text to os.system (Commands.py / Util.py).
_SAFE = string.ascii_letters + string.digits + '/._-:=,%+@*?[]'


# AND/OR have equal precedence in POSIX shell, not Python boolean precedence.
# Nodes contain only tuples/strings; the backend receives the original spelling.
class ShellExpression(namedtuple('_ShellExpression', 'kind argv children redirections')):
    __slots__ = ()

    def __new__(cls, kind, argv, children, redirections=()):
        return super(ShellExpression, cls).__new__(cls, kind, argv, children,
                                                   tuple(redirections))


ShellRedirection = namedtuple('ShellRedirection', 'operator target')
ShellToken = namedtuple('ShellToken', 'kind value')
MAX_GROUP_DEPTH = 16


class _ShellLexer(object):
    """Preserve the bounded character/quote rules; emit typed word/operators."""
    def __init__(self, text, origin):
        self.text, self.origin = text, origin

    def lex(self):
        text, origin = self.text, self.origin
        tokens, chars = [], []
        started, quote = False, None
        brace_open = False
        brace_content = []
        brace_has_comma = False
        brace_empty_alternative = True
        index = 0
        def word():
            if started:
                if brace_open:
                    raise SemanticError(origin, 'unclosed shell brace expansion in :sys')
                tokens.append(ShellToken('WORD', ''.join(chars)))
        while index < len(text):
            char = text[index]
            if char in '\x00\r\n':
                raise Unsupported(origin, 'multiline/control-byte :sys syntax is deferred')
            if quote == "'":
                if char == quote:
                    quote = None
                else:
                    chars.append(char)
            elif char == '\\':
                if index + 1 == len(text) or text[index + 1] in '\r\n\x00':
                    raise Unsupported(origin, 'shell line continuations are deferred')
                if brace_open:
                    raise Unsupported(origin, 'escaped brace-alternative content is deferred')
                following = text[index + 1]
                if quote == '"' and following not in '$`"\\':
                    chars.append('\\')
                chars.append(following)
                started = True
                index += 1
            elif quote == '"':
                if char == quote:
                    quote = None
                elif char in '$`':
                    raise Unsupported(origin, 'shell substitution is deferred')
                else:
                    chars.append(char)
            elif char in "'\"":
                if brace_open:
                    raise Unsupported(origin, 'quoted brace-alternative content is deferred')
                quote, started = char, True
            elif char in ' \t':
                if brace_open:
                    raise Unsupported(origin, 'whitespace in brace alternatives is deferred')
                word()
                chars, started = [], False
            elif char in '|&();':
                if brace_open:
                    raise Unsupported(origin, 'shell operators in brace alternatives are deferred')
                if text[index:index + 2] == '((':
                    raise Unsupported(origin, 'shell arithmetic/ambiguous double-parenthesis syntax is deferred')
                word()
                operator = char
                if char in '|&' and index + 1 < len(text) and text[index + 1] == char:
                    operator += char
                    index += 1
                if operator == '&':
                    raise Unsupported(origin, 'shell background jobs are deferred')
                tokens.append(ShellToken(operator, operator))
                chars, started = [], False
            elif char == '<':
                if brace_open:
                    raise Unsupported(origin, 'redirection in brace alternatives is deferred')
                # Keep the spelling in SystemRequest; no runtime file read.
                if started or text[index:index + 2] == '<<':
                    raise Unsupported(origin, 'shell input redirection is outside bounded :sys')
                tokens.append(ShellToken('<', '<'))
            elif char == '>':
                if brace_open:
                    raise Unsupported(origin, 'redirection in brace alternatives is deferred')
                # The parser decides if this operator is valid here.
                if text[index:index + 2] == '>>':
                    word()
                    chars, started = [], False
                    tokens.append(ShellToken('>>', '>>'))
                    index += 1
                else:
                    if started:
                        raise Unsupported(origin, 'shell output redirection is outside bounded :sys')
                    tokens.append(ShellToken('>', '>'))
            elif char == '{':
                if brace_open:
                    raise Unsupported(origin, 'nested shell brace expansion is deferred')
                brace_open = True
                brace_content = []
                brace_has_comma = False
                brace_empty_alternative = True
                chars.append(char)
                started = True
            elif char == '}':
                if not brace_open:
                    raise SemanticError(origin, 'unmatched closing shell brace in :sys')
                content = ''.join(brace_content)
                if '..' in content:
                    raise Unsupported(origin, 'shell brace ranges are deferred')
                if not brace_has_comma:
                    raise SemanticError(origin, 'shell brace expansion requires comma alternatives')
                if brace_empty_alternative:
                    raise Unsupported(origin, 'empty shell brace alternatives are deferred')
                chars.append(char)
                brace_open = False
            elif char == ',' and brace_open:
                if brace_empty_alternative:
                    raise Unsupported(origin, 'empty shell brace alternatives are deferred')
                brace_has_comma = True
                brace_empty_alternative = True
                brace_content.append(char)
                chars.append(char)
                started = True
            elif char in _SAFE or char.isalnum():
                chars.append(char)
                started = True
                if brace_open:
                    brace_content.append(char)
                    brace_empty_alternative = False
            else:
                raise Unsupported(origin, 'shell operator/expansion is outside bounded :sys: ' + char)
            index += 1
        if quote:
            raise SemanticError(origin, 'unclosed shell quote in :sys')
        word()
        return tuple(tokens)


def literal_shell(text, origin):
    """Validate the closed shell subset without evaluating short circuits.

    Groups are POSIX subshell groups. Depth is explicitly capability-bounded;
    long flat lists are parsed iteratively. Quotes keep operator words literal.
    """
    return _ShellParser(_ShellLexer(text, origin).lex(), origin).parse()


class _ShellParser(object):
    def __init__(self, tokens, origin):
        self.tokens, self.origin, self.index = tokens, origin, 0

    def peek(self):
        return self.tokens[self.index][0] if self.index < len(self.tokens) else None

    def parse(self):
        result = self.sequence(0)
        if self.peek() is not None:
            raise SemanticError(self.origin, 'unexpected token in :sys shell command')
        return result

    def sequence(self, depth):
        commands = [self.and_or(depth)]
        while self.peek() == ';':
            self.index += 1
            # POSIX permits a final separator, including before a group's
            # closing parenthesis. It does not add an empty command child.
            if self.peek() in (None, ')'):
                break
            commands.append(self.and_or(depth))
        if len(commands) == 1:
            return commands[0]
        return ShellExpression('SEQUENCE', (), tuple(commands))

    def and_or(self, depth):
        result = self.pipeline(depth)
        while self.peek() in ('&&', '||'):
            operator = self.peek()
            self.index += 1
            right = self.pipeline(depth)
            result = ShellExpression('AND' if operator == '&&' else 'OR', (), (result, right))
        return result

    def pipeline(self, depth):
        commands = [self.command(depth)]
        while self.peek() == '|':
            self.index += 1
            commands.append(self.command(depth))
        if len(commands) == 1:
            return commands[0]
        return ShellExpression('PIPELINE', (), tuple(commands))

    def command(self, depth):
        if self.peek() == '(':
            if depth >= MAX_GROUP_DEPTH:
                raise Unsupported(self.origin, ':sys subshell grouping depth is outside bounded subset')
            self.index += 1
            child = self.sequence(depth + 1)
            if self.peek() != ')':
                raise SemanticError(self.origin, 'missing closing parenthesis in :sys')
            self.index += 1
            return ShellExpression('GROUP', (), (child,))
        words = []
        while self.peek() == 'WORD':
            words.append(self.tokens[self.index][1])
            self.index += 1
        if not words or not words[0]:
            raise SemanticError(self.origin, 'missing command in :sys shell expression')
        executable = words[0]
        # This is a shell builtin, not A-A-P :cd.  Only the reached subshell
        # form is admitted; the backend still receives the original shell text
        # and request cwd.  The shell resolves the directory and owns failure.
        if executable == 'cd' and depth > 0:
            if len(words) != 2 or not words[1] or words[1].startswith('-'):
                raise Unsupported(self.origin, 'shell cd form is outside bounded subshell subset')
            return ShellExpression('BUILTIN', tuple(words), ())
        # A quoted executable path remains one WORD even when its filename
        # contains spaces.  Work.py builds the default $AAP as separately
        # item-quoted Python and Main.py paths; reject control wrappers, but
        # let the shell execute a quoted filename as historical aap_shell did.
        if ('=' in executable or executable.startswith(':')
                or posixpath.basename(executable) in _WRAPPERS):
            raise Unsupported(self.origin, 'shell environment/control wrapper is deferred')
        if self.peek() == '<':
            self.index += 1
            if self.peek() != 'WORD':
                raise Unsupported(self.origin, 'shell input redirection requires one literal input word')
            self.index += 1
            # globals.aap:doperlmod writes a sed result after reading the
            # matched file.  Preserve that shell form without admitting an
            # independent output-redirection capability.
            if self.peek() == '>':
                self.index += 1
                if self.peek() != 'WORD':
                    raise Unsupported(self.origin, 'bounded input redirection output requires one literal word')
                self.index += 1
        elif self.peek() == '>>':
            self.index += 1
            if self.peek() != 'WORD':
                raise Unsupported(self.origin, 'append redirection requires one literal target word')
            target = self.tokens[self.index][1]
            self.index += 1
            return ShellExpression('SIMPLE', tuple(words), (),
                                   (ShellRedirection('>>', target),))
        elif self.peek() == '>':
            raise Unsupported(self.origin, 'shell output redirection is outside bounded :sys')
        return ShellExpression('SIMPLE', tuple(words), ())


def pipeline_stages(expression):
    """Legacy argv inspection only when the expression is a plain pipeline.

    Never flatten an and/or list or group into independently launchable stages.
    """
    if expression.kind == 'SIMPLE':
        return (expression.argv,)
    if expression.kind == 'PIPELINE' and all(c.kind == 'SIMPLE' for c in expression.children):
        return tuple(c.argv for c in expression.children)
    return None


def shell_value(value, origin):
    # get_var_val with Expand(0, quote_shell): parse A-A-P items, discard
    # attributes, quote each item with the historical POSIX character set.
    # No safer modern shell quoting is silently substituted for its quirks.
    parsed = items(value, origin, label='shell value')
    allowed = ('distdir', 'extractdir', 'filetype', 'filetypehint', 'virtual')
    if any(key not in allowed for name, attrs in parsed for key in attrs):
        raise Unsupported(origin, 'shell value attributes outside bounded subset')
    return ' '.join(_quote_item(name, ' \t&;|$<>', '&;|') for name, attrs in parsed)


class SysBatchEntry(Node):
    def __init__(self, node):
        super(SysBatchEntry, self).__init__(node)
        self.raw = self.expanded = self.expression = None
        self.source_options = None


def source_force(node):
    """Process.py probes leading force before collecting the next :sys."""
    first = node.arguments.pieces[0] if node.arguments.pieces else ()
    raw = ''.join(part.value for part in first if isinstance(part, LiteralValue))
    options, ignored = attributes(raw, 0, node, label='sys')
    return bool(options.get('f') or options.get('force'))


class SysBatch(Node):
    """One aap_shell invocation; entries remain identifiable even on failure."""
    def __init__(self, nodes):
        super(SysBatch, self).__init__(nodes[0])
        self.entries = tuple(SysBatchEntry(node) for node in nodes)
        self.span = self.source.span(nodes[0].span.start.offset, nodes[-1].span.end.offset)
        self.raw = self.expanded = None
        self.option_policy = 'plain-unlogged-synchronous'


def sys_batch_nodes(statements, start):
    """Collect a candidate run inside one lowered source suite.

    Blank/comment lines have already been omitted, as by ParsePos.nextline.
    Deeper lines are argument continuations in the CST, not new statements.
    One trailing syseval is emitted before the pending shell buffer by the
    historical reader's four-character prefix check.  It is not shell text.
    Other mixed-prefix sequences remain gated until individually characterized.
    A force flag on either side flushes the pending shell batch, as in
    Process.py::Process. Other attributed mixed batches remain gated later.
    """
    first = statements[start]
    end = start + 1
    capture = None
    while end < len(statements):
        node = statements[end]
        if (not isinstance(node, Command) or not node.name.startswith('sys')
                or node.source is not first.source or node.origin.indent != first.origin.indent):
            break
        if source_force(first) or source_force(node):
            break
        if node.name == 'syseval' and capture is None:
            capture = node
            end += 1
            continue
        if node.name != 'sys':
            raise Unsupported(first, ':sys mixed-prefix batching with :' + node.name + ' is deferred')
        if capture is not None:
            raise Unsupported(first, ':sys commands after pending :syseval are deferred')
        end += 1
    if capture is not None:
        return statements[start:end - 1], end, capture
    return statements[start:end], end, None


class SystemRequest(Node):
    def __init__(self, node, command, expression, cwd, policy, batch=None,
                 options=None):
        super(SystemRequest, self).__init__(node)
        self.command = command
        self.batch = batch
        self.entries = batch.entries if batch is not None else ()
        self.expression = expression
        self.stages = pipeline_stages(expression)
        self.shell_mode = 'posix-sh'
        self.shell_required = True  # os.system even for a single argv stage
        self.command_bytes = policy.encode(command, node)
        self.shell_command = command + '\n'  # unlogged logged_system branch
        self.shell_command_bytes = policy.encode(self.shell_command, node)
        self.cwd, self.cwd_bytes = cwd, policy.encode(cwd, node)
        if b'\x00' in self.shell_command_bytes or b'\x00' in self.cwd_bytes:
            raise SemanticError(node, 'encoded command/cwd must be NUL-free')
        self.capture_stdout = False
        self.stdout_policy = 'inherit'
        self.stderr_policy = 'inherit'
        self.stdin_policy = 'inherit'
        self.environment = None  # inherited from backend invocation, not A-A-P
        self.environment_policy = 'inherit-backend'
        options = options or {}
        self.force = bool(options.get('force'))
        self.quiet = bool(options.get('quiet'))
        self.echo = not self.quiet
        self.echo_text = command + '\n'
        self.echo_lines = tuple(entry.expanded for entry in self.entries) if batch is not None else (command,)
        self.logging = bool(options.get('log'))
        self.log_path = policy.log_path if self.logging else None
        self.log_command_text = batch.expanded.rstrip('\n') if self.logging else None
        self.status_policy = 'logged-recovered' if self.logging else 'encoded-wait'
        if self.logging:
            self.stdout_policy = 'log-merged'
            self.stderr_policy = 'log-merged'
        self.skip_in_dry_run = True


class SystemRecord(Node):
    def __init__(self, node):
        super(SystemRecord, self).__init__(node)
        self.request = None
        self.result = None
        self.status = None
        self.reason = None
        self.error = None
        self.output = None  # No A-A-P capture/assignment result for :sys.
        self.batch = None


def execute_system(runtime, node, scope, python, cwd, records, nodes=None):
    record = SystemRecord(node)
    records.append(record)
    batch = SysBatch(tuple(nodes) if nodes is not None else (node,))
    record.batch = batch
    try:
        if runtime.policy.sys_mode not in ('unlogged', 'bounded-attributes'):
            raise Unsupported(node, ':sys requires explicit synchronous process policy')
        if scope.local.get('async'):
            raise Unsupported(node, 'asynchronous :sys is deferred')
        for entry in batch.entries:
            command_node = entry.origin
            if any(isinstance(part, PythonFragment) for piece in command_node.arguments.pieces for part in piece):
                raise Unsupported(command_node, ':sys backticks are outside the bounded subset')
            entry.raw = render_value(command_node.arguments, python)
            entry.source_options, unused = attributes(entry.raw.lstrip(' \t'), 0,
                                                       command_node, label='sys')
            if entry.raw.lstrip().startswith('{') and runtime.policy.sys_mode != 'bounded-attributes':
                raise Unsupported(command_node, ':sys attributes/logging/force modes are deferred')
            if '\n' in entry.raw or '\r' in entry.raw:
                raise Unsupported(command_node, ':sys entry newlines are deferred')
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, ':sys requires explicit absolute cwd')
        # aap_shell expands the WHOLE collected string once, before launching
        # anything and before storing sysresult. No command result can influence
        # later entries' expansion in the same batch.
        batch.raw = ''.join(entry.raw + '\n' for entry in batch.entries)
        batch.expanded = expand_text(batch.raw, scope, node, value_transform=shell_value)
        lines = batch.expanded.split('\n')
        if len(lines) != len(batch.entries) + 1 or lines[-1] != '':
            raise Unsupported(node, ':sys expansion-created entry newlines are deferred')
        commands, expressions = [], []
        batch_options = {}
        for entry, expanded in zip(batch.entries, lines[:-1]):
            entry.expanded = expanded
            # get_sys_option consumes leading whitespace on EVERY shell line.
            command = expanded.lstrip(' \t')
            options, start = attributes(command, 0, entry, label='sys')
            if options:
                if (not entry.source_options or len(batch.entries) != 1 or
                        any(key not in ('f', 'force', 'q', 'quiet', 'l', 'log')
                            or value != 1 for key, value in options.items())):
                    raise Unsupported(entry, ':sys attributed batch or option value is deferred')
                batch_options = {'force': bool(options.get('f') or options.get('force')),
                                 'quiet': bool(options.get('q') or options.get('quiet')),
                                 'log': bool(options.get('l') or options.get('log'))}
                if (batch_options['log'] and
                        (type(runtime.policy.log_path) is not str or
                         not posixpath.isabs(runtime.policy.log_path))):
                    raise Unsupported(entry, ':sys logging path capability is unavailable')
                command = command[start:].lstrip(' \t')
            elif entry.raw.lstrip().startswith('{') or command.startswith('{'):
                raise Unsupported(entry, ':sys expanded options are deferred')
            entry.expression = literal_shell(command, entry)
            commands.append(command)
            expressions.append(entry.expression)
        expression = (expressions[0] if len(expressions) == 1 else
                      ShellExpression('SEQUENCE', (), tuple(expressions)))
        record.request = SystemRequest(batch, '\n'.join(commands), expression, cwd,
                                       runtime.policy, batch, batch_options)
        result = runtime.backend.run(record.request)
        if (not isinstance(result, ProcessResult) or type(result.wait_status) is not int
                or not 0 <= result.wait_status <= 65535):
            raise SemanticError(node, 'process backend must return a POSIX wait status')
        record.result = result
        # stdout/stderr are optional observations of inherited streams. Keep
        # exact bytes; no trimming, decoding, merging or :assign is performed.
        if type(result.stdout) is not bytes or (result.stderr is not None and type(result.stderr) is not bytes):
            raise SemanticError(node, ':sys stream observations must be bytes')
        if record.request.logging and type(result.log_output) is not bytes:
            raise SemanticError(node, 'logged :sys requires byte log observation')
        scope.store('sysresult', result.wait_status, node)
        if result.wait_status and not record.request.force:
            raise SemanticError(node, 'shell command returned ' + str(result.wait_status))
        record.status, record.reason = ('COMPLETED',
            'forced_shell_failure' if result.wait_status else 'shell_success')
    except (ProcessUnavailable, NotImplementedError) as error:
        record.status, record.reason = 'BLOCKED', 'process_capability_unavailable'
        record.error = Unsupported(node, str(error))
        raise record.error
    except Unsupported as error:
        record.status, record.reason, record.error = 'BLOCKED', 'unsupported_process_form', error
        raise
    except SemanticError as error:
        record.status, record.reason, record.error = 'FAILED', 'process_semantic_error', error
        raise
    except (ProcessBackendError, OSError, ValueError) as error:
        record.status, record.reason = 'FAILED', 'process_backend_failed'
        record.error = SemanticError(node, 'process backend failed: ' + str(error))
        raise record.error
