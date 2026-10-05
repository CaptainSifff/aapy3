"""CST -> semantic structure, without value lookup or Python execution."""
import io
import tokenize

from aap_frontend import cst, parse_body
from . import model as m
from .diagnostics import SemanticError


def argument_value(node, regions=None):
    pieces = []
    for region in node.arguments if regions is None else regions:
        parts = []
        buffer = []
        ticks = iter(region.backticks)
        tick = next(ticks, None)
        for line in node.header_lines:
            for char, offset in zip(line.cooked, line.origins):
                if not region.span.start.offset <= offset < region.span.end.offset:
                    continue
                while tick is not None and offset >= tick.span.end.offset:
                    tick = next(ticks, None)
                if tick is not None and offset >= tick.span.start.offset:
                    if offset == tick.span.start.offset:
                        if buffer:
                            parts.append(m.LiteralValue(region, ''.join(buffer)))
                            buffer = []
                        parts.append(m.PythonFragment(tick, tick.cooked)
                                     if tick.active else m.LiteralValue(tick, tick.cooked))
                    continue
                buffer.append(char)
        if buffer:
            parts.append(m.LiteralValue(region, ''.join(buffer)))
        if parts and isinstance(parts[0], m.LiteralValue):
            parts[0].value = parts[0].value.lstrip(' \t')
        if parts and isinstance(parts[-1], m.LiteralValue):
            parts[-1].value = parts[-1].value.rstrip(' \t')
        pieces.append(parts)
    return m.ArgumentValue(node, pieces)


def _header(node):
    if not isinstance(node, cst.EmbeddedPythonStatement):
        return None
    try:
        tokens = [token for token in tokenize.generate_tokens(
                  io.StringIO(node.payload).readline)
                  if token.type not in (tokenize.COMMENT, tokenize.NL,
                                        tokenize.NEWLINE, tokenize.ENDMARKER)]
    except (tokenize.TokenError, IndentationError):
        return None
    if (tokens and tokens[-1].string == ':'
            and tokens[0].string in ('if', 'elif', 'else', 'for')):
        if tokens[0].string == 'else' and len(tokens) != 2:
            raise SemanticError(node, 'invalid @else header')
        return tokens[0].string
    return None


def _indent(node):
    if isinstance(node, cst.EmbeddedPythonStatement):
        return node.python_indent
    return node.indent


class _Lowerer(object):
    def __init__(self, origin, children):
        self.origin = origin
        self.nodes = tuple(n for n in children
                           if not isinstance(n, (cst.Comment, cst.BlankLine)))
        self.index = 0

    def suite(self, indent):
        result = []
        while self.index < len(self.nodes):
            node = self.nodes[self.index]
            level = _indent(node)
            if level < indent:
                break
            if level > indent:
                raise SemanticError(node, 'unexpected indentation in mixed suite')
            kind = _header(node)
            if kind in ('elif', 'else'):
                raise SemanticError(node, 'orphan @' + kind)
            if kind == 'if':
                branches = []
                otherwise = None
                first = node
                while True:
                    code = node.payload
                    if kind == 'elif':
                        code = 'if' + code[4:]
                    condition = m.PythonFragment(node, code + '\n    pass\n')
                    self.index += 1
                    body = self.body(node, indent)
                    branches.append((condition, body))
                    if self.index == len(self.nodes):
                        break
                    node = self.nodes[self.index]
                    if _indent(node) != indent:
                        break
                    kind = _header(node)
                    if kind == 'else':
                        self.index += 1
                        otherwise = self.body(node, indent)
                        break
                    if kind != 'elif':
                        break
                conditional = m.Conditional(first, branches, otherwise)
                last = otherwise or branches[-1][1]
                conditional.span = first.source.span(first.span.start.offset,
                                                       last.span.end.offset)
                result.append(conditional)
            elif kind == 'for':
                self.index += 1
                body = self.body(node, indent)
                loop = m.Loop(node, m.PythonFragment(
                    node, node.payload + '\n    pass\n'), body)
                loop.span = node.source.span(node.span.start.offset, body.span.end.offset)
                result.append(loop)
            else:
                self.index += 1
                result.append(_statement(node))
        program = m.Program(self.origin, result)
        if result:
            program.span = self.origin.source.span(result[0].span.start.offset,
                                                    result[-1].span.end.offset)
        return program

    def body(self, header, indent):
        if self.index == len(self.nodes) or _indent(self.nodes[self.index]) <= indent:
            raise SemanticError(header, 'expected an indented mixed suite')
        return self.suite(_indent(self.nodes[self.index]))


def _statement(node):
    if isinstance(node, cst.LiteralBlock):
        return m.Assignment(node, m.LiteralValue(node.body, node.body.cooked_text))
    if isinstance(node, cst.AssignmentStatement):
        return m.Assignment(node, argument_value(node))
    if isinstance(node, cst.PythonBlock):
        return m.DeferredConstruct(node, ':python execution')
    if isinstance(node, cst.VariantStatement):
        return m.Variant(node, [(branch.value, lower_children(branch.body,
                           branch.body.children)) for branch in node.branches])
    if isinstance(node, cst.ColonCommand):
        body = m.DeferredBody(node.body) if node.body is not None else None
        return m.Command(node, argument_value(node), body)
    if isinstance(node, cst.DependencyLikeStatement):
        body = m.DeferredBody(node.body) if node.body is not None else None
        targets = argument_value(node, (node.targets,))
        targets.span = node.targets.span
        sources = argument_value(node)
        if node.arguments:
            sources.span = node.source.span(node.arguments[0].span.start.offset,
                                            node.arguments[-1].span.end.offset)
        return m.Dependency(node, body, targets, sources)
    if isinstance(node, cst.EmbeddedPythonStatement):
        return m.EmbeddedPython(node, m.PythonFragment(node, node.payload))
    if isinstance(node, cst.SectionStatement):
        return m.DeferredConstruct(node, 'build section context')
    raise SemanticError(node, 'unhandled CST node ' + type(node).__name__)


def lower_children(origin, children):
    reader = _Lowerer(origin, children)
    if not reader.nodes:
        return m.Program(origin, ())
    program = reader.suite(_indent(reader.nodes[0]))
    if reader.index != len(reader.nodes):
        raise SemanticError(reader.nodes[reader.index], 'inconsistent mixed-suite dedent')
    return program


def lower(document):
    result = lower_children(document, document.children)
    result.span = document.span
    return result


def lower_body(body):
    """Explicit re-entry only; enclosing lowering never validates this text."""
    if not isinstance(body, m.DeferredBody) or body.flavor != 'recipe':
        raise SemanticError(body, 'only deferred recipe bodies can be lowered')
    return lower(parse_body(body.origin))
