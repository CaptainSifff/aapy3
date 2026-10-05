"""Neutral concrete syntax nodes; no runtime objects or handler dispatch."""


class Node(object):
    def __init__(self, source, span):
        self.source = source
        self.span = span

    @property
    def text(self):
        return self.source.slice(self.span)


class PhysicalLine(Node):
    def __init__(self, source, span, content_span):
        super(PhysicalLine, self).__init__(source, span)
        self.content_span = content_span

    @property
    def ending(self):
        return self.source.text[self.content_span.end.offset:self.span.end.offset]


class LogicalLine(Node):
    def __init__(self, source, span, physical_lines, cooked, origins,
                 continuations, indent, prefix_length):
        super(LogicalLine, self).__init__(source, span)
        self.physical_lines = tuple(physical_lines)
        self.cooked = cooked
        self.origins = tuple(origins)
        self.continuations = tuple(continuations)
        self.indent = indent
        self.prefix_length = prefix_length

    def offset(self, index):
        if not 0 <= index <= len(self.cooked):
            raise ValueError('logical index outside line')
        if index < len(self.origins):
            return self.origins[index]
        return self.physical_lines[-1].content_span.end.offset

    def slice_span(self, start, end):
        # A contiguous envelope. origins maps characters across removed text.
        stop = self.origins[end - 1] + 1 if end > start else self.offset(start)
        return self.source.span(self.offset(start), stop)

    @property
    def significant(self):
        return not isinstance(self, (Comment, BlankLine))


class Comment(LogicalLine):
    pass


class BlankLine(LogicalLine):
    pass


class BacktickRegion(Node):
    def __init__(self, source, span, cooked, active=True, closed=True):
        super(BacktickRegion, self).__init__(source, span)
        self.cooked = cooked
        self.active = active
        self.closed = closed


class ArgumentRegion(Node):
    def __init__(self, source, span, backticks=(), comments=()):
        super(ArgumentRegion, self).__init__(source, span)
        self.backticks = tuple(backticks)
        self.comments = tuple(comments)


class Body(Node):
    def __init__(self, source, span, owner, lines, flavor, threshold,
                 end_reason, scanned=False, children=()):
        super(Body, self).__init__(source, span)
        self.owner = owner
        self.lines = tuple(lines)
        self.flavor = flavor
        self.threshold = threshold
        self.end_reason = end_reason
        self.scanned = scanned
        self.children = tuple(children)
        indents = [line.indent for line in lines if line.significant]
        self.first_indent = indents[0] if indents else None
        self.minimum_indent = min(indents) if indents else None


class DeferredBody(Body):
    pass


class Statement(Node):
    def __init__(self, source, line):
        super(Statement, self).__init__(source, line.span)
        self.line = line
        self.indent = line.indent
        self.header_lines = (line,)
        self.arguments = ()
        self.body = None


class AssignmentStatement(Statement):
    def __init__(self, source, line, name, operator):
        super(AssignmentStatement, self).__init__(source, line)
        self.name = name
        self.operator = operator


class LiteralBlock(AssignmentStatement):
    terminator = None
    terminator_line = None


class ColonCommand(Statement):
    def __init__(self, source, line, name):
        super(ColonCommand, self).__init__(source, line)
        self.name = name

    @property
    def raw_arguments(self):
        return tuple(argument.text for argument in self.arguments)


class DependencyLikeStatement(Statement):
    targets = None
    separator_span = None


class EmbeddedPythonStatement(Statement):
    payload_span = None
    payload = ''
    python_indent = 0
    backticks = ()


class PythonBlock(ColonCommand):
    terminator = None
    terminator_line = None


class VariantStatement(ColonCommand):
    variable = None
    branches = ()


class VariantBranch(Statement):
    value = None
    ignored_tail_span = None


class SectionStatement(Statement):
    name = None


class Document(Node):
    def __init__(self, source, span, lines, children, toplevel):
        super(Document, self).__init__(source, span)
        self.lines = tuple(lines)
        self.children = tuple(children)
        self.toplevel = toplevel
