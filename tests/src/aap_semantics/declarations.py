"""Session-owned declarative state; no detection or action execution."""
from .model import Node
from .diagnostics import SemanticError, Unsupported


class ActionDefinition(Node):
    def __init__(self, command, name, input_type, scope):
        super(ActionDefinition, self).__init__(command)
        self.name = name
        self.input_type = input_type
        self.output_type = 'default'
        self.scope = scope                 # Live definition scope, not a snapshot.
        self.body = command.body


class SuffixDefinition(Node):
    def __init__(self, line, suffix, filetype, command, scope):
        super(SuffixDefinition, self).__init__(line)
        self.suffix = suffix
        self.filetype = filetype
        self.command = command
        self.body = command.body
        self.scope = scope


class DeclarationState(object):
    def __init__(self, known_types=()):
        self.actions = {}                  # Name -> ordered definition history.
        self.suffixes = {}                 # Suffix -> current definition.
        self.filetypes = set(known_types)
        self.suffix_history = []

    def register_action(self, command, arguments, scope):
        if len(arguments) != 2 or any(',' in word for word in arguments):
            raise Unsupported(command, 'only single-name, single-input-type actions are supported')
        if command.body is None:
            raise Unsupported(command, 'only action definitions with a body are supported')
        name, input_type = arguments
        definition = ActionDefinition(command, name, input_type, scope)
        self.actions.setdefault(name, []).append(definition)
        return definition

    def latest_action(self, name, input_type):
        """Exact registration lookup only; no detection, routing or invocation."""
        for definition in reversed(self.actions.get(name, ())):
            if definition.input_type == input_type:
                return definition
        return None

    def register_filetype(self, command, arguments, scope):
        if arguments or command.body is None:
            raise Unsupported(command, 'only inline :filetype bodies are supported')
        # This is the filetype data language, never an A-A-P recipe suite.
        for line in command.body.origin.lines:
            fields = line.cooked.split(None, 1)
            if not fields or fields[0].startswith('#'):
                continue
            if fields[0] != 'suffix':
                raise Unsupported(line, 'unsupported filetype declaration: ' + fields[0])
            rest = fields[1] if len(fields) == 2 else ''
            if rest.startswith(('"', "'")):
                end = rest.find(rest[0], 1)
                if end < 0:
                    raise SemanticError(line, 'missing quote in filetype suffix')
                suffix, rest = rest[1:end], rest[end + 1:]
            else:
                pieces = rest.split(None, 1)
                suffix = pieces[0] if pieces else ''
                rest = pieces[1] if len(pieces) == 2 else ''
            types = rest.split()
            if not types:
                raise SemanticError(line, 'missing filetype for suffix')
            # Historical reader ignores excess fields, including comment tails.
            filetype = types[0]
            definition = SuffixDefinition(line, suffix, filetype, command, scope)
            self.suffix_history.append(definition)
            if filetype == 'remove':
                self.suffixes.pop(suffix, None)
            else:
                self.suffixes[suffix] = definition
                self.filetypes.add(filetype)
