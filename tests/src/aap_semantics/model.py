"""Semantic nodes. Source spelling belongs to their immutable-source CST refs."""


class Node(object):
    def __init__(self, origin):
        self.origin = origin
        self.source = origin.source
        self.span = origin.span


class Program(Node):
    def __init__(self, origin, statements):
        super(Program, self).__init__(origin)
        self.statements = tuple(statements)


class LiteralValue(Node):
    def __init__(self, origin, value):
        super(LiteralValue, self).__init__(origin)
        self.value = value


class PythonFragment(Node):
    def __init__(self, origin, code):
        super(PythonFragment, self).__init__(origin)
        self.code = code


class ArgumentValue(Node):
    def __init__(self, origin, pieces):
        super(ArgumentValue, self).__init__(origin)
        # Each piece corresponds to one getarg call; joins happen separately.
        self.pieces = tuple(tuple(piece) for piece in pieces)


class Assignment(Node):
    def __init__(self, origin, value):
        super(Assignment, self).__init__(origin)
        self.target = origin.name
        self.mode = ('append' if '+' in origin.operator else
                     'default' if '?' in origin.operator else 'replace')
        self.delayed = '$' in origin.operator
        self.value = value


class DeferredBody(Node):
    def __init__(self, origin, flavor=None):
        super(DeferredBody, self).__init__(origin)
        self.flavor = flavor or origin.flavor


class Command(Node):
    def __init__(self, origin, arguments, body):
        super(Command, self).__init__(origin)
        self.name = origin.name
        self.arguments = arguments
        self.body = body


class Dependency(Node):
    def __init__(self, origin, body, target_value, source_value):
        super(Dependency, self).__init__(origin)
        self.targets = origin.targets
        self.arguments = origin.arguments
        self.body = body
        self.target_value = target_value
        self.source_value = source_value


class EmbeddedPython(Node):
    def __init__(self, origin, fragment):
        super(EmbeddedPython, self).__init__(origin)
        self.fragment = fragment


class Conditional(Node):
    def __init__(self, origin, branches, otherwise):
        super(Conditional, self).__init__(origin)
        # Branches are (PythonFragment condition, Program suite) pairs.
        self.branches = tuple(branches)
        self.otherwise = otherwise


class Loop(Node):
    def __init__(self, origin, header, body):
        super(Loop, self).__init__(origin)
        self.header = header
        self.body = body


class DeferredConstruct(Node):
    def __init__(self, origin, reason):
        super(DeferredConstruct, self).__init__(origin)
        self.reason = reason


class Variant(Node):
    def __init__(self, origin, branches):
        super(Variant, self).__init__(origin)
        self.variable = origin.variable
        self.branches = tuple(branches)
