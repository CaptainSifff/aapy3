#!/usr/bin/env python3
"""Standalone plain-source bundle of the bounded A-A-P CLI/runtime.

Project modules and integration adapters are embedded as readable source.
External programs, repositories, and recipe inputs remain host requirements.
"""
from __future__ import print_function

import importlib.util
import linecache
import sys

_EMBEDDED = {}
_MANIFEST = {}

_MANIFEST['aap_frontend'] = ('tests/src/aap_frontend/__init__.py', True, '18a93dee839fb91379a9d44998626dc997bb54283f243347a849415ba927fa43')
_EMBEDDED['aap_frontend'] = ('tests/src/aap_frontend/__init__.py', True, r'''"""Source-preserving A-A-P frontend; Python 3.4+, standard library only."""
from .source import Source, SourcePosition, SourceSpan, FrontendError
from .scanner import scan
from .parser import parse, parse_body

__all__ = ['Source', 'SourcePosition', 'SourceSpan', 'FrontendError',
           'scan', 'parse', 'parse_body']
''')

_MANIFEST['aap_frontend.cst'] = ('tests/src/aap_frontend/cst.py', False, '554118c79dd1c3df65601df41f53a6bdb46f6f7795794937b2ea27f01a19ea13')
_EMBEDDED['aap_frontend.cst'] = ('tests/src/aap_frontend/cst.py', False, r'''"""Neutral concrete syntax nodes; no runtime objects or handler dispatch."""


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
''')

_MANIFEST['aap_frontend.parser'] = ('tests/src/aap_frontend/parser.py', False, 'ce79e8d603e9efa913ff13797668b28431e6bfe7ff713d75a87c24bdcb3334bf')
_EMBEDDED['aap_frontend.parser'] = ('tests/src/aap_frontend/parser.py', False, r'''"""Context-directed structural readers, following Process.py's reader order.

This is deliberately not a Python parser or a command registry. Only names
whose syntax changes source boundaries have special readers.
"""
import string

from . import cst
from .scanner import scan, whitespace_end, indentation
from .source import Source, FrontendError


_VAR_CHARS = string.ascii_letters + string.digits + '_.'
_BODY_COMMANDS = {'rule': 'recipe', 'action': 'recipe', 'tree': 'recipe',
                  'route': 'route', 'filetype': 'filetype'}
_TOPLEVEL_READERS = frozenset(('rule', 'delrule', 'route', 'variant'))


def _var_name(text, start):
    end = start
    while end < len(text) and text[end] in _VAR_CHARS:
        end += 1
    if end > start and text[end - 1] == '.':
        end -= 1
    return text[start:end], whitespace_end(text, end)


class _Parser(object):
    def __init__(self, source, lines, toplevel, file_mode=False):
        self.source = source
        self.lines = tuple(lines)
        self.toplevel = toplevel
        self.index = 0
        significant = [line for line in lines if line.significant]
        self.base_indent = significant[0].indent if significant else 0
        if file_mode and self.base_indent:
            self.error(significant[0], significant[0].prefix_length,
                       'first statement in a file is indented')

    def error(self, line, index, reason):
        raise FrontendError(self.source, line.offset(index), reason)

    def next_significant(self, index):
        while index < len(self.lines) and not self.lines[index].significant:
            index += 1
        return index

    def span_lines(self, start, end, fallback):
        if start == end:
            return self.source.span(fallback, fallback)
        return self.source.span(self.lines[start].span.start.offset,
                                self.lines[end - 1].span.end.offset)

    def finish(self, node, start):
        node.span = self.span_lines(start, self.index, node.span.start.offset)
        if node.body is None:
            node.header_lines = self.lines[start:self.index]
        return node

    def document(self, span):
        children = []
        while self.index < len(self.lines):
            children.append(self.statement())
        return cst.Document(self.source, span, self.lines, children, self.toplevel)

    def backtick(self, index):
        """Active get_py_expr boundary; Python quoting intentionally ignored."""
        line = self.lines[self.index]
        start = line.offset(index)
        index += 1
        pieces = []
        while True:
            line = self.lines[self.index]
            text = line.cooked
            if index == len(text):
                following = self.next_significant(self.index + 1)
                if following == len(self.lines):
                    raise FrontendError(self.source, start, 'missing closing backtick')
                self.index = following
                index = 0
                pieces.append('\n')
                continue
            if text.startswith('``', index):
                pieces.append('`')
                index += 2
            elif text.startswith('$(`)', index):
                pieces.append('`')
                index += 4
            elif text[index] == '`':
                span = self.source.span(start, line.origins[index] + 1)
                return cst.BacktickRegion(self.source, span, ''.join(pieces)), index + 1
            else:
                pieces.append(text[index])
                index += 1

    def argument(self, index, stops='#'):
        """One getarg region; returned cursor leaves the delimiter unconsumed."""
        start = self.lines[self.index].offset(index)
        quote = ''
        braces = 0
        backticks = []
        comments = []
        while True:
            line = self.lines[self.index]
            text = line.cooked
            if index == len(text):
                break
            char = text[index]
            if text.startswith('``', index):
                backticks.append(cst.BacktickRegion(
                    self.source, line.slice_span(index, index + 2), '`', False))
                index += 2
                continue
            if char == '`':
                region, index = self.backtick(index)
                backticks.append(region)
                continue
            if (text.startswith('$(', index) and index + 3 < len(text)
                    and text[index + 3] == ')'):
                index += 4
                continue
            if quote:
                if char == quote:
                    quote = ''
            elif char in '\"\'':
                quote = char
            else:
                if char == '{':
                    braces += 1
                elif char == '}':
                    braces = max(0, braces - 1)
                if (char in stops
                        and (char != ':' or index + 1 == len(text)
                             or text[index + 1] in ' \t')
                        and (char != '=' or braces == 0)):
                    if char == '#':
                        comments.append(cst.Node(self.source,
                                        line.slice_span(index, len(text))))
                    break
            index += 1
            if char == '$' and index < len(text) and text[index] in '$#':
                index += 1
        span = self.source.span(start, line.offset(index))
        return cst.ArgumentRegion(self.source, span, backticks, comments), index

    def arguments(self, node, index, continuation=True):
        regions = []
        while True:
            region, unused = self.argument(index)
            regions.append(region)
            end = self.index + 1
            following = self.next_significant(end)
            if (not continuation or following == len(self.lines)
                    or self.lines[following].indent <= node.indent):
                self.index = end
                break
            self.index = following
            index = self.lines[following].prefix_length
        node.arguments = tuple(regions)

    def deferred(self, node, start, flavor):
        """get_commands: buffer first, split at the first minimum indent."""
        begin = self.index
        end = begin
        significant = []
        while end < len(self.lines):
            line = self.lines[end]
            if line.significant:
                if line.indent <= node.indent:
                    break
                significant.append(end)
            end += 1
        if significant:
            split = min(significant, key=lambda i: self.lines[i].indent)
            # Only header continuations undergo argument/backtick recognition.
            header = _Parser(self.source, self.lines[begin:split], self.toplevel)
            regions = list(node.arguments)
            while header.index < len(header.lines):
                line = header.lines[header.index]
                if line.significant:
                    region, unused = header.argument(line.prefix_length)
                    regions.append(region)
                header.index += 1
            node.arguments = tuple(regions)
            body_span = self.span_lines(split, end, node.span.end.offset)
            node.body = cst.DeferredBody(self.source, body_span, node,
                                         self.lines[split:end], flavor,
                                         node.indent, 'eof' if end == len(self.lines)
                                         else 'dedent')
            node.header_lines = self.lines[start:split]
            self.index = end
        else:
            # Trivia alone does not create a body.
            node.header_lines = self.lines[start:begin]
            if isinstance(node, cst.ColonCommand) and node.name == 'tree':
                self.error(node.line, node.line.prefix_length, ':tree requires a body')

    def terminator(self, line, index):
        text = line.cooked
        index = whitespace_end(text, index)
        end = index
        while end < len(text) and text[end] not in ' \t':
            end += 1
        if end == index:
            self.error(line, index, 'missing block terminator')
        tail = whitespace_end(text, end)
        if tail < len(text) and text[tail] != '#':
            self.error(line, tail, 'extra text after block terminator')
        return text[index:end]

    def block(self, node, term, flavor):
        """Literal/Python payload is never dispatched as recipe syntax."""
        node.terminator = term
        begin = self.index + 1
        end = begin
        terminator_line = None
        while end < len(self.lines):
            line = self.lines[end]
            if line.significant:
                text = line.cooked[line.prefix_length:]
                if term is None:
                    if line.indent <= node.indent:
                        break
                elif text.startswith(term):
                    tail = text[len(term):].lstrip(' \t')
                    if not tail or tail.startswith('#'):
                        terminator_line = line
                        break
            end += 1
        if term is not None and terminator_line is None:
            self.error(node.line, node.line.prefix_length,
                       'unterminated block (expected {0})'.format(term))
        node.terminator_line = terminator_line
        span = self.span_lines(begin, end, node.line.span.end.offset)
        node.body = cst.Body(self.source, span, node, self.lines[begin:end],
                             flavor, node.indent,
                             'terminator' if term is not None else
                             ('eof' if end == len(self.lines) else 'dedent'))
        # Boundary validation and a separate legacy-normalized payload view.
        # Keep raw body text untouched, including blank/comment lines.
        normalized = []
        first_indent = node.body.first_indent
        keep = node.indent - self.base_indent if flavor == 'python' else 0
        for line in node.body.lines:
            if not line.significant:
                continue
            text = line.cooked
            index = 0
            width = 0
            while width + keep < first_indent and index < len(text):
                if text[index] not in ' \t':
                    break
                width += 1 if text[index] == ' ' else 8 - width % 8
                index += 1
            if indentation(text[index:]) > first_indent - keep:
                while index < len(text) and text[index] in ' \t':
                    char = text[index]
                    width += 1 if char == ' ' else 8 - width % 8
                    index += 1
                    if char == '\t':
                        break
            if width < first_indent - keep:
                self.error(line, line.prefix_length, 'not enough indent in block')
            normalized.append(' ' * (width - first_indent + keep) + text[index:] + '\n')
        node.body.cooked_text = ''.join(normalized)
        self.index = end + (1 if terminator_line is not None else 0)

    def variant(self, node, index):
        line = node.line
        name, tail = _var_name(line.cooked, whitespace_end(line.cooked, index))
        if not name or (tail < len(line.cooked) and line.cooked[tail] != '#'):
            self.error(line, index, 'expected a single variant variable')
        node.variable = name
        self.index += 1
        begin = self.index
        children = []
        branches = []
        threshold = None
        had_star = False
        while self.index < len(self.lines):
            current = self.lines[self.index]
            if not current.significant:
                children.append(current)
                self.index += 1
                continue
            if current.indent <= node.indent:
                break
            if threshold is None:
                threshold = current.indent
            if had_star:
                self.error(current, current.prefix_length, 'variant * must be last')
            branch_start = self.index
            branch = cst.VariantBranch(self.source, current)
            prefix = current.prefix_length
            text = current.cooked
            if text[prefix] == '*':
                tail = prefix + 1
                if tail < len(text) and text[tail] not in ' \t':
                    self.error(current, tail, 'variant * must be by itself')
                branch.value = '*'
                had_star = True
            else:
                branch.value, tail = _var_name(text, prefix)
                if not branch.value:
                    self.error(current, prefix, 'expected variant value')
            branch.ignored_tail_span = current.slice_span(tail, len(text))
            self.index += 1
            body_start = self.index
            body_children = []
            while self.index < len(self.lines):
                following = self.lines[self.index]
                if following.significant and following.indent <= threshold:
                    break
                body_children.append(self.statement())
            branch.body = cst.Body(self.source,
                                  self.span_lines(body_start, self.index,
                                                  current.span.end.offset),
                                  branch, self.lines[body_start:self.index],
                                  'variant-branch', threshold, 'branch-or-dedent',
                                  True, body_children)
            self.finish(branch, branch_start)
            children.append(branch)
            branches.append(branch)
        if not branches:
            self.error(line, line.prefix_length, 'expected variant values')
        node.branches = tuple(branches)
        node.body = cst.Body(self.source,
                             self.span_lines(begin, self.index, line.span.end.offset),
                             node, self.lines[begin:self.index], 'variant',
                             node.indent, 'dedent-or-eof', True, children)

    def python_line(self, line):
        node = cst.EmbeddedPythonStatement(self.source, line)
        start = line.prefix_length + 1
        payload_start = whitespace_end(line.cooked, start)
        node.python_indent = line.indent
        if payload_start != start:
            node.python_indent = indentation(' ' * (line.indent + 1)
                                             + line.cooked[start:])
        node.payload_span = line.slice_span(payload_start, len(line.cooked))
        node.payload = line.cooked[payload_start:]
        # Backticks in @ source are *inert* for A-A-P, not getarg interpolation.
        # Mark spelling for consumers without imposing closure/Python validation.
        regions = []
        index = payload_start
        while index < len(line.cooked):
            if line.cooked[index] != '`':
                index += 1
                continue
            closing = line.cooked.find('`', index + 1)
            end = len(line.cooked) if closing < 0 else closing + 1
            regions.append(cst.BacktickRegion(self.source,
                           line.slice_span(index, end), line.cooked[index + 1:closing]
                           if closing >= 0 else line.cooked[index + 1:],
                           False, closing >= 0))
            index = end
        node.backticks = tuple(regions)
        self.index += 1
        return node

    def statement(self):
        start = self.index
        line = self.lines[start]
        if not line.significant:
            self.index += 1
            return line
        text = line.cooked
        prefix = line.prefix_length
        if text.startswith(':python', prefix):
            node = cst.PythonBlock(self.source, line, 'python')
            index = whitespace_end(text, prefix + 7)
            term = None
            if index < len(text) and text[index] != '#':
                term = self.terminator(line, index)
            self.block(node, term, 'python')
        elif text[prefix] == ':':
            end = prefix + 1
            while end < len(text) and text[end] not in ' \t':
                end += 1
            name = text[prefix + 1:end]
            special = self.toplevel or name not in _TOPLEVEL_READERS
            if name == 'variant' and special:
                node = cst.VariantStatement(self.source, line, name)
                self.variant(node, end)
            else:
                node = cst.ColonCommand(self.source, line, name)
                if name in ('rule', 'delrule') and special:
                    targets, separator = self.argument(end, ':#')
                    current = self.lines[self.index]
                    if current.cooked[separator:separator + 1] != ':':
                        self.error(current, separator, 'expected rule colon')
                    node.targets = targets
                    node.separator_span = current.slice_span(separator, separator + 1)
                    end = separator + 1
                self.arguments(node, end,
                               not (name in _BODY_COMMANDS and special))
                if name in _BODY_COMMANDS and special:
                    self.deferred(node, start, _BODY_COMMANDS[name])
        elif text[prefix] == '@':
            node = self.python_line(line)
        elif text[prefix] == '>':
            end = prefix + 1
            while end < len(text) and text[end] not in ' \t':
                end += 1
            name = text[prefix + 1:end]
            tail = whitespace_end(text, end)
            if (name not in ('always', 'build', 'nobuild')
                    or (tail < len(text) and text[tail] != '#')):
                self.error(line, prefix, 'invalid build section header')
            node = cst.SectionStatement(self.source, line)
            node.name = name
            self.index += 1
        else:
            name, index = _var_name(text, prefix)
            op_start = index
            if index < len(text) and text[index] == '$':
                index += 1
            if index < len(text) and text[index] in '+?':
                index += 1
            if name and index < len(text) and text[index] in '=<':
                if text[index] == '<':
                    if not text.startswith('<<', index):
                        self.error(line, index, 'expected << for literal assignment')
                    end = index + 2
                    node = cst.LiteralBlock(self.source, line, name, text[op_start:end])
                    self.block(node, self.terminator(line, end), 'literal')
                else:
                    node = cst.AssignmentStatement(self.source, line, name,
                                                   text[op_start:index + 1])
                    self.arguments(node, index + 1)
            else:
                node = cst.DependencyLikeStatement(self.source, line)
                targets, separator = self.argument(prefix, ':#')
                current = self.lines[self.index]
                if current.cooked[separator:separator + 1] != ':':
                    self.error(current, separator,
                               'unrecognized statement; expected dependency colon')
                if not self.toplevel:
                    self.error(line, prefix, 'dependency is not allowed in a command body')
                node.targets = targets
                node.separator_span = current.slice_span(separator, separator + 1)
                self.arguments(node, separator + 1, False)
                self.deferred(node, start, 'recipe')
        return self.finish(node, start)


def parse(source, source_id='<string>', file_mode=False, toplevel=True):
    """Parse supplied text only. file_mode enforces the first-line indent rule.

    @ fragments remain ordered siblings with indentation, allowing later mixed
    suite lowering without pretending each fragment is a complete Python AST.
    """
    if not isinstance(source, Source):
        source = Source(source_id, source)
    lines = scan(source)
    return _Parser(source, lines, toplevel, file_mode).document(
        source.span(0, len(source.text)))


def parse_body(body, toplevel=False):
    """Explicit structural re-entry, retaining coordinates and the original CST.

    Reuse already joined logical lines. This is not legacy runtime reprocessing
    of a generated string; any future generated source needs its own Source.
    """
    if not isinstance(body, cst.DeferredBody) or body.flavor != 'recipe':
        raise ValueError('only deferred recipe bodies support recipe re-entry')
    return _Parser(body.source, body.lines, toplevel).document(body.span)
''')

_MANIFEST['aap_frontend.scanner'] = ('tests/src/aap_frontend/scanner.py', False, '0d40f3532de3fef093e52f77c24ab57c23aa22947b67db9b081640ab0b6d74de')
_EMBEDDED['aap_frontend.scanner'] = ('tests/src/aap_frontend/scanner.py', False, r'''"""Physical/logical scanning derived from upstream/ParsePos.py::nextline."""
from .cst import PhysicalLine, LogicalLine, Comment, BlankLine
from .source import FrontendError


def whitespace_end(text, index=0):
    while index < len(text) and text[index] in ' \t':
        index += 1
    return index


def indentation(text):
    column = 0
    for char in text:
        if char == ' ':
            column += 1
        elif char == '\t':
            column += 8 - column % 8
        else:
            break
    return column


def scan(source):
    """Return lossless logical lines, including historically skipped trivia.

    No quote/comment state shields a final backslash. Continuation records
    cover removed backslash, EOL and any removed continuation @ prefix.
    """
    physical = []
    start = 0
    while start < len(source.text):
        newline = source.text.find('\n', start)
        end = len(source.text) if newline < 0 else newline + 1
        content_end = end if newline < 0 else newline
        if content_end > start and source.text[content_end - 1] == '\r':
            content_end -= 1
        physical.append(PhysicalLine(source, source.span(start, end),
                                     source.span(start, content_end)))
        start = end
    result = []
    index = 0
    while index < len(physical):
        first = index
        cooked = ''
        origins = []
        continuations = []
        skip = 0
        while True:
            line = physical[index]
            begin = line.content_span.start.offset + skip
            stop = line.content_span.end.offset
            cooked += source.text[begin:stop]
            origins.extend(range(begin, stop))
            index += 1
            if not cooked.endswith('\\'):
                break
            if index == len(physical):
                raise FrontendError(source, origins[-1],
                                    'last line ends in a backslash')
            skip = 0
            following = physical[index]
            next_text = source.slice(following.content_span)
            if cooked.lstrip(' \t').startswith('@'):
                prefix = whitespace_end(next_text)
                if next_text[prefix:prefix + 1] == '@':
                    skip = prefix + 1
            continuations.append(source.span(origins[-1],
                                  following.span.start.offset + skip))
            cooked = cooked[:-1]
            origins.pop()
        prefix = whitespace_end(cooked)
        cls = LogicalLine
        if prefix == len(cooked):
            cls = BlankLine
        elif cooked[prefix] == '#':
            cls = Comment
        result.append(cls(source, source.span(physical[first].span.start.offset,
                                             physical[index - 1].span.end.offset),
                          physical[first:index], cooked, origins, continuations,
                          indentation(cooked), prefix))
    return tuple(result)
''')

_MANIFEST['aap_frontend.source'] = ('tests/src/aap_frontend/source.py', False, 'a5a43454d29eec83b4205897a10a3c8dfac0c33cac8dfc472c1a2acf668607e5')
_EMBEDDED['aap_frontend.source'] = ('tests/src/aap_frontend/source.py', False, r'''"""Immutable text and physical coordinates. Offsets count Unicode characters."""
from bisect import bisect_right
from collections import namedtuple
import io


SourcePosition = namedtuple('SourcePosition', 'line column offset')
SourceSpan = namedtuple('SourceSpan', 'source_id start end')


class Source(namedtuple('_Source', 'source_id text line_starts')):
    __slots__ = ()

    def __new__(cls, source_id, text):
        if not isinstance(text, str):
            raise TypeError('source text must be decoded str')
        starts = [0]
        starts.extend(i + 1 for i, char in enumerate(text) if char == '\n')
        return super(Source, cls).__new__(cls, str(source_id), text,
                                          tuple(starts))

    @classmethod
    def from_path(cls, path, encoding):
        """Read exactly one explicit input; preserve CRLF and require encoding."""
        with io.open(str(path), 'r', encoding=encoding, newline='') as stream:
            return cls(str(path), stream.read())

    def position(self, offset):
        if not 0 <= offset <= len(self.text):
            raise ValueError('offset outside source')
        index = bisect_right(self.line_starts, offset) - 1
        return SourcePosition(index + 1, offset - self.line_starts[index] + 1,
                              offset)

    def span(self, start, end):
        if end < start:
            raise ValueError('reversed source span')
        return SourceSpan(self.source_id, self.position(start), self.position(end))

    def slice(self, span):
        if span.source_id != self.source_id:
            raise ValueError('span belongs to another source')
        if (span != self.span(span.start.offset, span.end.offset)):
            raise ValueError('inconsistent source coordinates')
        return self.text[span.start.offset:span.end.offset]


class FrontendError(ValueError):
    def __init__(self, source, offset, reason):
        self.span = source.span(offset, offset)
        self.reason = reason
        pos = self.span.start
        super(FrontendError, self).__init__('{0}:{1}:{2}: {3}'.format(
            source.source_id, pos.line, pos.column, reason))
''')

_MANIFEST['aap_semantics'] = ('tests/src/aap_semantics/__init__.py', True, 'cb6c12b57c51ae1f2a03ed953b613080a3ddc7ddf50a076ac1d96901097fef68')
_EMBEDDED['aap_semantics'] = ('tests/src/aap_semantics/__init__.py', True, r'''"""A-A-P semantic lowering and restricted metadata evaluation (Python 3.4+)."""
from .lowering import lower, lower_body
from .capabilities import RuntimeCapabilities
from .evaluator import Evaluator, EvaluationResult
from .scopes import Scope
from .work import WorkIdentity
from .recipe_mutation import (RecipeMutationBackend, RecipeMutationRequest,
                              RecipeMutationResult, MemoryRecipeMutationBackend)
from .helpers import HelperRegistry
from .diagnostics import SemanticError, Unsupported, UndefinedName
from .declarations import DeclarationState, ActionDefinition, SuffixDefinition
from .includes import SourceLoader
from .process import ProcessBackend, ProcessBackendError, ProcessRequest, ProcessResult, ProcessPolicy, ProcessUnavailable
from .system_process import SystemRequest, SystemRecord
from .graph import BuildGraph, DependencyDefinition, TargetNode
from .planner import UpdatePlanner, UpdatePlan, UpdateDecision, BodyPlan, PlanningDiagnostic
from .target_state import TargetStateBackend, MemoryTargetState, FileState, buildcheck_digest
from .body_executor import BuildExecutionContext, BodyExecutor, BodyExecutionResult
from .checksum import (ArtifactBackend, ArtifactUnavailable, MemoryArtifacts,
                       ChecksumBackend, ChecksumRequest, ChecksumResult)
from .fetch import FetchBackend, FetchRequest, FetchAttempt, FetchResult, MemoryFetchBackend
from .build_driver import BuildDriver, BuildResult
from .completion import PostExecutionRecheck, TargetCompletionDecision
from .persistence import PersistenceBackend, MemoryPersistence, SignatureRecord
from .port_defaults import PortDefaults
from .nested_update import UpdateRequest, UpdateResult
from .port_runtime import PortRuntime, PortOperation, PortMessage, MarkerBackend, MemoryMarkers
from .port_delete import DeleteBackend, DeleteRequest, DeleteResult, MemoryDeleteBackend
from .port_commands import (PortCommandRuntime, PortCommandPolicy, PortCommandRequest,
                            PortCommandRecord, PortDirectories, MemoryPortDirectories)
from .actions import (ActionRuntime, ActionRequest, ActionResult, ActionBackend,
                      SemanticActionBackend, ActionWorkspace, MemoryActionWorkspace)

from .path_observation import PathObserver, MemoryPathObserver, PathObservation, PathRequest

from .output import (PrintRuntime, PrintRequest, PrintRecord, OutputPolicy,
                     MemoryOutputSink, MemoryTextWriter, TextWriter, OutputResult)
from .cat import CatRuntime, CatRequest, CatRecord
from .tree_runtime import (TreeRuntime, TreeFilesystem, MemoryTreeFilesystem,
                           TreeRequest, TreeObservation, TreeRecord)
from .move_runtime import MoveRuntime, MoveBackend, MemoryMoveBackend, MoveRequest, MoveResult, MoveRecord
from .copy_runtime import (CopyRuntime, CopyBackend, MemoryCopyBackend,
                           CopyObservation, CopyRequest, CopyResult, CopyRecord)
from .cli import CliArgumentError, CliArguments, apply_assignments, parse_arguments, valid_variable_name

__all__ = ['lower', 'lower_body', 'RuntimeCapabilities', 'Evaluator', 'EvaluationResult', 'Scope',
           'WorkIdentity', 'RecipeMutationBackend', 'RecipeMutationRequest',
           'RecipeMutationResult', 'MemoryRecipeMutationBackend',
           'HelperRegistry', 'SemanticError', 'Unsupported', 'UndefinedName',
           'DeclarationState', 'ActionDefinition', 'SuffixDefinition', 'SourceLoader',
           'ProcessBackend', 'ProcessBackendError', 'ProcessUnavailable', 'SystemRequest', 'SystemRecord', 'ProcessRequest', 'ProcessResult', 'ProcessPolicy',
           'BuildGraph', 'DependencyDefinition', 'TargetNode',
           'UpdatePlanner', 'UpdatePlan', 'UpdateDecision', 'BodyPlan', 'PlanningDiagnostic',
           'TargetStateBackend', 'MemoryTargetState', 'FileState', 'buildcheck_digest',
           'BuildExecutionContext', 'BodyExecutor', 'BodyExecutionResult',
           'ArtifactBackend', 'ArtifactUnavailable', 'MemoryArtifacts',
           'ChecksumBackend', 'ChecksumRequest', 'ChecksumResult',
           'FetchBackend', 'FetchRequest', 'FetchAttempt', 'FetchResult',
           'MemoryFetchBackend',
           'BuildDriver', 'BuildResult', 'PostExecutionRecheck', 'TargetCompletionDecision',
           'PersistenceBackend', 'MemoryPersistence', 'SignatureRecord', 'PortDefaults',
           'UpdateRequest', 'UpdateResult', 'PortRuntime', 'PortOperation', 'PortMessage',
           'DeleteBackend', 'DeleteRequest', 'DeleteResult', 'MemoryDeleteBackend',
           'MarkerBackend', 'MemoryMarkers', 'ActionRuntime', 'ActionRequest', 'ActionResult',
           'ActionBackend', 'SemanticActionBackend', 'ActionWorkspace', 'MemoryActionWorkspace',
           'PortCommandRuntime', 'PortCommandPolicy', 'PortCommandRequest', 'PortCommandRecord',
           'PortDirectories', 'MemoryPortDirectories',
           'PathObserver', 'MemoryPathObserver', 'PathObservation', 'PathRequest',
           'PrintRuntime', 'PrintRequest', 'PrintRecord', 'OutputPolicy',
           'MemoryOutputSink', 'MemoryTextWriter', 'TextWriter', 'OutputResult',
           'CatRuntime', 'CatRequest', 'CatRecord',
           'TreeRuntime', 'TreeFilesystem', 'MemoryTreeFilesystem',
           'TreeRequest', 'TreeObservation', 'TreeRecord',
           'MoveRuntime', 'MoveBackend', 'MemoryMoveBackend', 'MoveRequest', 'MoveResult', 'MoveRecord',
           'CopyRuntime', 'CopyBackend', 'MemoryCopyBackend', 'CopyObservation',
           'CopyRequest', 'CopyResult', 'CopyRecord', 'CliArgumentError',
           'CliArguments', 'apply_assignments', 'parse_arguments',
           'valid_variable_name']
''')

_MANIFEST['aap_semantics.actions'] = ('tests/src/aap_semantics/actions.py', False, '0e691065541f99e657352f01749407d2f0a457c61119c529e1df011460377a70')
_EMBEDDED['aap_semantics.actions'] = ('tests/src/aap_semantics/actions.py', False, r'''"""Bounded action routing and entry, not an archive-format implementation.

Evidence: Action.action_run/action_find/action_ftype, Filetype.ft_detect,
Scope.get_build_recdict. Effects require explicit trusted capabilities.
"""
import posixpath

from aap_frontend import FrontendError
from .directories import ExecutionDirectoryState
from .model import Node
from .diagnostics import SemanticError, Unsupported
from .scopes import Scope
from .values import var2string, UnavailableValue
from .command_items import items
from .lowering import lower_body
from .nested_update import UpdateStopped


class ActionWorkspace(object):
    """Explicit directory entry and file-kind/type observations; never chdir.

    prepare_directory must ensure the directory exists and can be entered,
    returning the observed absolute cwd, or raise OSError. It must not report success merely from an intended path.
    filetype supplies an observed result for detection beyond known suffixes;
    None means positively unrecognized, not an unavailable observation.
    """
    def prepare_directory(self, path):
        raise NotImplementedError('action work-directory capability unavailable')

    def file_kind(self, path):
        raise NotImplementedError('action file-kind observation unavailable')

    def filetype(self, path):
        raise NotImplementedError('extended filetype detection unavailable: ' + path)


class MemoryActionWorkspace(ActionWorkspace):
    def __init__(self, files=None, filetypes=None):
        self.files = dict(files or {})
        self.directories = set()
        self.filetypes = dict(filetypes or {})
        self.operations = []

    def prepare_directory(self, path):
        if not posixpath.isabs(path):
            raise ValueError('action directory requires an absolute path')
        directory = posixpath.normpath(path)
        ancestor = directory
        while ancestor != '/':
            if ancestor in self.files:
                raise OSError('action directory is a file: ' + ancestor)
            ancestor = posixpath.dirname(ancestor)
        self.directories.add(directory)
        self.operations.append(('directory', path))
        return directory

    def file_kind(self, path):
        return ('directory' if path in self.directories else
                'file' if path in self.files else 'missing')

    def filetype(self, path):
        if path not in self.filetypes:
            return super(MemoryActionWorkspace, self).filetype(path)
        return self.filetypes[path]


class ActionRequest(Node):
    def __init__(self, origin, definition, filename, attributes, filetype,
                 targettype, cwd, caller):
        super(ActionRequest, self).__init__(origin)
        self.definition = definition
        self.body = definition.body
        self.filename, self.attributes = filename, dict(attributes)
        self.name, self.filetype = definition.name, filetype
        self.directory, self.caller = ExecutionDirectoryState(cwd), caller
        self.graph, self.declarations = caller.graph, caller.declarations
        self.invocation_scope = caller.scope
        self.scope = Scope.build(definition.scope, caller.scope, keep_current_scope=True)
        source = var2string([filename], origin)
        # Dictlist.expand_item quote_aap retains attributes. Stable spelling
        # replaces Python 2 dictionary iteration order, with identical values.
        for key in sorted(attributes):
            source += '{' + key + '=' + str(attributes[key]) + '}'
        self.scope.local.update({'source': source, 'fname': var2string([filename], origin),
                                 'filetype': filetype, 'targettype': targettype,
                                 'action': definition.name, 'name': definition.name,
                                 'DEFER_ACTION_NAME': None, '_dirstack': [], '_prevdir': None,
                                 'recipe_name': definition.span.source_id,
                                 'recipe_lnum': definition.span.start.line})

    @property
    def cwd(self):
        return self.directory.current


class ActionResult(object):
    def __init__(self, request):
        self.request = request
        self.status = None
        self.reason = None
        self.error = None
        self.blocked_at = None
        self.span = request.body.span
        self.program = None
        self.evaluation = None
        self.nested_result = None


class ActionStopped(Exception):
    def __init__(self, result):
        super(ActionStopped, self).__init__(result.reason)
        self.result = result


class ActionBackend(object):
    """Trusted action capability, never a callable exposed to recipe Python.

    Return ActionResult for this exact request. COMPLETED certifies that the
    selected definition was performed, not that an inferred output exists.
    Tests may supply a recorded outcome; no real extraction adapter is enabled.
    Raise OSError for a failed effect, NotImplementedError for missing capability.
    """
    def execute(self, request):
        raise NotImplementedError('action execution capability unavailable')


class SemanticActionBackend(ActionBackend):
    """Re-enter the ordinary frontend/evaluator. Unsupported commands block."""
    def execute(self, request):
        from .evaluator import Evaluator
        result = ActionResult(request)
        caller = request.caller
        evaluator = None
        try:
            result.program = lower_body(request.body)
            # The historical bounded action re-entry has not enabled these
            # filesystem commands. Keep that boundary while sharing all
            # adapters that the action body already received.
            action_caps = caller.capabilities.replace(
                path_observer=caller.path_observer, tree_filesystem=None,
                move_backend=None, copy_backend=None)
            evaluator = Evaluator(
                request.scope, declarations=request.declarations, graph=request.graph,
                cwd=request.cwd, capabilities=action_caps,
                update_driver=caller.update_driver, execution_context=request)
            evaluation = evaluator.run(result.program, source_active=False)
            if evaluation.complete:
                result.status, result.reason = 'COMPLETED', 'action_body_completed'
            else:
                result.status, result.reason = 'BLOCKED', 'unsupported_action_operation'
                result.blocked_at = evaluation.halted_at
                result.span = evaluation.halted_at.span
        except (UpdateStopped, ActionStopped) as stopped:
            cause = stopped.result
            result.nested_result = cause
            result.status, result.reason = cause.status, cause.reason
            result.error, result.span, result.blocked_at = cause.error, cause.span, cause.blocked_at
        except Unsupported as error:
            result.status, result.reason = 'BLOCKED', 'unsupported_action_semantics'
            result.error, result.span = error, error.span
            result.blocked_at = evaluator.active_node if evaluator is not None else None
        except FrontendError as error:
            result.status, result.reason = 'FAILED', 'action_semantic_error'
            result.error, result.span = error, error.span
        finally:
            if evaluator is not None:
                result.evaluation = evaluator.last_result
        return result


class ActionRuntime(object):
    # Filetype.py suffix table; registration overrides (including removal) win.
    ARCHIVE_SUFFIXES = {'tar': 'tar', 'tar.gz': 'targz', 'tgz': 'targz',
                        'tar.bz2': 'tarbz2', 'zip': 'zip'}

    def __init__(self, workspace=None, backend=None):
        self.workspace = workspace if workspace is not None else ActionWorkspace()
        self.backend = backend if backend is not None else SemanticActionBackend()
        self.active = []

    def detect(self, filename, attrs, evaluator, origin):
        node = evaluator.graph.find_node(filename, evaluator.cwd)
        node_attrs = node.attributes if node is not None else {}
        for key in node_attrs:
            if key.startswith(('var_', 'add_')):
                raise Unsupported(origin, 'action node variable attributes are deferred')
        for mapping, key in ((attrs, 'filetype'), (node_attrs, 'filetype'),
                             (attrs, 'filetypehint')):
            if key in mapping:
                value = mapping[key]
                if value is not None and type(value) is not str:
                    raise Unsupported(origin, 'action filetype must be a string')
                return value
        kind = self.workspace.file_kind(filename)
        if kind not in ('file', 'missing', 'directory'):
            raise ValueError('invalid file-kind observation')
        if kind == 'directory':
            return 'directory'
        suffixes = dict(self.ARCHIVE_SUFFIXES)
        for registration in evaluator.declarations.suffix_history:
            if registration.filetype == 'remove':
                suffixes.pop(registration.suffix, None)
            else:
                suffixes[registration.suffix] = registration.filetype
        name = posixpath.basename(filename)
        index = name.find('.')
        while index > 0 and index < len(name) - 1:
            suffix = name[index + 1:]
            if suffix in suffixes:
                if suffixes[suffix] == 'ignore':
                    raise Unsupported(origin, 'ignored-suffix recursive detection is deferred')
                return suffixes[suffix]
            index = name.find('.', index + 1)
        value = self.workspace.filetype(filename)
        if value is not None and type(value) is not str:
            raise ValueError('invalid filetype observation')
        return value

    def invoke(self, name, filename, attrs, cwd, caller, origin, records):
        if self.active:
            raise Unsupported(origin, 'recursive action entry is deferred')
        if name != 'extract':
            raise Unsupported(origin, 'action invocation outside bounded extract entry: ' + name)
        if caller is None:
            raise Unsupported(origin, 'action entry requires the live evaluator context')
        definitions = caller.declarations.actions.get(name, ())
        if not definitions:
            raise SemanticError(origin, 'unknown action: ' + name)
        filetype = self.detect(filename, attrs, caller, origin) or 'default'
        candidates = [filetype]
        if '_' in filetype and not filetype.startswith('_'):
            candidates.append(filetype.split('_', 1)[0])
        candidates.append('default')
        definition = None
        # Current registration subset has only default output types. Do not
        # guess an output type for selection or add action chaining.
        for candidate in candidates:
            definition = caller.declarations.latest_action(name, candidate)
            if definition is not None:
                break
        if definition is None:
            raise SemanticError(origin, 'no commands defined for extract from ' + filetype)
        targettype = None
        target = caller.scope.local.get('target')
        if target:
            if type(target) is not str:
                raise Unsupported(origin, 'action target conversion is deferred')
            parsed = items(target, origin, label='action target')
            if parsed:
                target_name, target_attrs = parsed[0]
                try:
                    targettype = self.detect(posixpath.normpath(posixpath.join(cwd, target_name)),
                                             target_attrs, caller, origin)
                except NotImplementedError as error:
                    # All selectable definitions have default output type;
                    # unavailable detection cannot alter routing. Reading the
                    # value still blocks, rather than inventing None.
                    targettype = UnavailableValue(str(error))
        request = ActionRequest(origin, definition, filename, attrs, filetype,
                                targettype, cwd, caller)
        result = ActionResult(request)
        records.append(result)
        try:
            self.active.append(request)
            try:
                observed = self.backend.execute(request)
            finally:
                self.active.pop()
            if (not isinstance(observed, ActionResult) or observed.request is not request
                    or observed.status not in ('COMPLETED', 'BLOCKED', 'FAILED')):
                raise ValueError('invalid action backend outcome')
            result = observed
            records[-1] = result
        except NotImplementedError as error:
            result.status, result.reason = 'BLOCKED', 'action_capability_unavailable'
            result.error = Unsupported(origin, str(error))
            result.span = result.error.span
        except Unsupported as error:
            result.status, result.reason = 'BLOCKED', 'unsupported_action_semantics'
            result.error, result.span = error, error.span
        except FrontendError as error:
            result.status, result.reason = 'FAILED', 'action_semantic_error'
            result.error, result.span = error, error.span
        except (OSError, ValueError) as error:
            result.status, result.reason = 'FAILED', 'action_backend_failed'
            result.error = SemanticError(origin, str(error))
            result.span = result.error.span
        if result.status != 'COMPLETED':
            raise ActionStopped(result)
        return result
''')

_MANIFEST['aap_semantics.body_executor'] = ('tests/src/aap_semantics/body_executor.py', False, '32c76b652dfeca315203ad1f33bcd32c11f861368d1c78337dd80979a48c657c')
_EMBEDDED['aap_semantics.body_executor'] = ('tests/src/aap_semantics/body_executor.py', False, r'''"""Enter one planned dependency body through the existing evaluator.

This is semantic execution, not a build driver or signature commit protocol.
It never traverses prerequisites, writes persistent state, or marks nodes done.
"""
import posixpath

from aap_frontend import FrontendError
from .model import Node
from .planner import BodyPlan
from .scopes import Scope
from .values import UnavailableValue, var2string
from .diagnostics import Unsupported
from .lowering import lower_body
from .evaluator import Evaluator
from .nested_update import UpdateStopped
from .actions import ActionStopped
from .directories import ExecutionDirectoryState
from .capabilities import RuntimeCapabilities


def _short_name(node, cwd, origin):
    name = node.identity
    if not posixpath.isabs(name):
        return name
    if name == cwd:
        raise Unsupported(origin, 'directory identity shortening requires state observation')
    # Util.shorten_name retains absolute names when only the root is shared.
    common = 0
    for left, right in zip(name.split('/'), cwd.split('/')):
        if left != right:
            break
        common += 1
    return posixpath.relpath(name, cwd) if common > 1 else name


class BuildItem(object):
    def __init__(self, item, cwd):
        self.item = item
        self.node = item.node
        self.name = _short_name(item.node, cwd, item)
        self.attributes = dict(item.node.attributes)
        self.attributes.update(item.attributes)  # occurrence attributes win
        if any(key != 'virtual' for key in self.attributes):
            raise Unsupported(item, 'build item attributes beyond virtual are deferred')

    def text(self):
        value = var2string([self.name], self.item)
        if 'virtual' in self.attributes:
            value += '{virtual=' + str(self.attributes['virtual']) + '}'
        return value


class BuildExecutionContext(Node):
    def __init__(self, step, invocation_scope, graph, declarations,
                 process_backend=None, process_policy=None, include_loader=None,
                 prepared_buildcheck=None, checksum_backend=None, update_driver=None,
                 port_runtime=None, path_observer=None, output_runtime=None, cat_runtime=None,
                 tree_filesystem=None, move_backend=None, copy_backend=None,
                 capabilities=None):
        if not isinstance(step, BodyPlan):
            raise TypeError('body execution requires a selected BodyPlan')
        super(BuildExecutionContext, self).__init__(step)
        self.step = step
        self.target = step.target
        self.definition = step.definition
        self.body = step.body
        self.definition_scope = step.scope
        self.invocation_scope = invocation_scope
        self.scope = Scope.build(self.definition_scope, invocation_scope)
        self.directory = ExecutionDirectoryState(step.cwd)
        self.graph = graph
        self.declarations = declarations
        self.capabilities = (capabilities if capabilities is not None else
            RuntimeCapabilities(process_backend, process_policy, include_loader,
                checksum_backend, port_runtime, path_observer, output_runtime,
                cat_runtime, tree_filesystem, move_backend, copy_backend))
        caps = self.capabilities
        self.process_backend = caps.process_backend
        self.process_policy = caps.process_policy
        self.include_loader = caps.include_loader
        self.checksum_backend = caps.checksum_backend
        self.update_driver = update_driver
        self.port_runtime = caps.port_runtime
        self.path_observer = caps.path_observer
        self.output_runtime = caps.output_runtime
        self.cat_runtime = caps.cat_runtime
        self.tree_filesystem = caps.tree_filesystem
        self.move_backend = caps.move_backend
        self.copy_backend = caps.copy_backend
        # An explicit prepared observation, not a hash of the deferred source.
        self.prepared_buildcheck = prepared_buildcheck
        self.entered = False
        self.target_items = tuple(BuildItem(i, self.cwd) for i in self.definition.target_items)
        self.depend_items = tuple(BuildItem(i, self.cwd) for i in self.definition.source_items)
        self.source_items = tuple(i for i in self.depend_items if not i.node.virtual)
        local = self.scope.local
        local.update({'buildtarget': self.target.identity, 'match': '',
                      '_really_build': 1, '_dirstack': [], '_prevdir': None,
                      'recipe_name': self.span.source_id,
                      'recipe_lnum': self.span.start.line})
        for name, items in (('target', self.target_items), ('depend', self.depend_items),
                            ('source', self.source_items)):
            local[name] = ' '.join(i.text() for i in items)
            local[name + '_list'] = [i.name for i in items]
            local[name + '_dl'] = UnavailableValue('build dictlist value is deferred: ' + name + '_dl')
        local['fname'] = var2string([self.source_items[0].name], self) if self.source_items else ''

    @property
    def cwd(self):
        return self.directory.current


def _declarations(state):
    return (tuple((name, tuple(state.actions[name])) for name in sorted(state.actions)),
            tuple(state.suffix_history), tuple(sorted(state.filetypes)))


class BodyExecutionResult(object):
    def __init__(self, context):
        self.context = context
        self.target = context.target
        self.definition = context.definition
        self.body = context.body
        self.scope = context.scope
        self.program = None
        self.evaluation = None
        self.status = None  # COMPLETED, BLOCKED, FAILED
        self.reason = None
        self.blocked_at = None
        self.error = None
        self.span = context.body.span
        self.processes = ()
        self.checksums = ()
        self.updates = ()
        self.port_operations = ()
        self.directory_changes = ()
        self.tree_records = ()
        self.moves = ()
        self.copies = ()
        self.update_failure = None
        self.action_failure = None
        self.added_definitions = ()
        self.graph_changed = False
        self.declarations_changed = False
        self.post_execution_recheck_required = False
        self.target_updated = False
        self.persistence_performed = False


class BodyExecutor(object):
    def execute(self, context):
        """Enter once; reached :update delegates to the injected driver."""
        result = BodyExecutionResult(context)
        step = context.step
        guard = None
        if context.entered:
            guard = 'context_already_entered'
        elif step.mode != 'update' or step.after:
            guard = 'plan_recheck_required'
        elif getattr(step, 'graph_snapshot', None) != context.graph.snapshot():
            guard = 'graph_changed_since_plan'
        elif step.buildcheck_required and type(context.prepared_buildcheck) is not str:
            guard = 'buildcheck_unavailable'
        if guard:
            result.status, result.reason = 'BLOCKED', guard
            return result
        context.entered = True
        graph_before = context.graph.snapshot()
        count = len(context.graph.definitions)
        declarations_before = _declarations(context.declarations)
        evaluator = None
        try:
            result.program = lower_body(context.body)
            evaluator = Evaluator(context.scope, declarations=context.declarations,
                                  graph=context.graph, cwd=context.cwd,
                                  capabilities=context.capabilities,
                                  update_driver=context.update_driver,
                                  execution_context=context)
            # The defining recipe finished reading before build entry. Its
            # source identity is diagnostic provenance, not an active include.
            evaluation = evaluator.run(result.program, source_active=False)
            if evaluation.complete:
                result.status, result.reason = 'COMPLETED', 'semantic_body_completed'
            else:
                result.status, result.reason = 'BLOCKED', 'unsupported_operation'
                result.blocked_at = evaluation.halted_at
                result.span = evaluation.halted_at.span
        except ActionStopped as stopped:
            action = stopped.result
            result.status, result.reason = action.status, action.reason
            result.action_failure = action
            result.error, result.span, result.blocked_at = action.error, action.span, action.blocked_at
        except UpdateStopped as stopped:
            update = stopped.result
            result.status, result.reason = update.status, update.reason
            result.update_failure = update
            result.error, result.span = update.error, update.span
            result.blocked_at = update.blocked_at
        except Unsupported as error:
            result.status, result.reason = 'BLOCKED', 'unsupported_semantics'
            result.error, result.span = error, error.span
            result.blocked_at = evaluator.active_node if evaluator is not None else None
        except FrontendError as error:
            result.status, result.reason = 'FAILED', 'semantic_error'
            result.error, result.span = error, error.span
        finally:
            if evaluator is not None:
                result.evaluation = evaluator.last_result
                if result.evaluation is not None:
                    result.processes = tuple(result.evaluation.processes)
                    result.checksums = tuple(result.evaluation.checksums)
                    result.updates = tuple(result.evaluation.updates)
                    result.port_operations = tuple(result.evaluation.port_operations)
                    result.directory_changes = tuple(result.evaluation.directory_changes)
                    result.tree_records = tuple(result.evaluation.tree_records)
                    result.moves = tuple(result.evaluation.moves)
                    result.copies = tuple(result.evaluation.copies)
            result.added_definitions = tuple(context.graph.definitions[count:])
            result.graph_changed = context.graph.snapshot() != graph_before
            result.declarations_changed = _declarations(context.declarations) != declarations_before
            # Even a partial execution may have changed shared metadata or
            # observed processes. This is an obligation, never update success.
            result.post_execution_recheck_required = True
        return result
''')

_MANIFEST['aap_semantics.build_driver'] = ('tests/src/aap_semantics/build_driver.py', False, 'c1c7f5731601fda38b66dacebacf19e46dd6e6591b5bb94ffeb4f5d97e55e6dd')
_EMBEDDED['aap_semantics.build_driver'] = ('tests/src/aap_semantics/build_driver.py', False, r'''"""One controlled invocation: plan, enter, observe, complete, explicitly flush.

No recipe command handlers or automatic port helper execution live here.
"""
from aap_frontend import FrontendError
from .body_executor import BuildExecutionContext, BodyExecutor
from .completion import PostExecutionRecheck, observe_file
from .planner import UpdatePlanner, BodyPlan
from .persistence import InvocationObservations
from .port_defaults import PortDefaults
from .diagnostics import Unsupported
from .nested_update import UpdateRequest, UpdateResult
from .scopes import Scope
from .buildcheck import BuildSignaturePreparer
from .buildcheck import BuildSignatureFailure
from .capabilities import RuntimeCapabilities


class BuildResult(object):
    def __init__(self):
        self.status = None
        self.reason = None
        self.plan = None
        self.bodies = []
        self.completions = []
        self.completed = []
        self.blocked_at = None
        self.error = None
        self.span = None
        self.persistence_written = False
        self.pending_signatures = ()


class BuildDriver(object):
    def __init__(self, graph, state, persistence, scope, declarations,
                 cwd=None, port_defaults=True, executor=None, max_bodies=10000,
                 process_backend=None, process_policy=None, include_loader=None,
                 checksum_backend=None, port_runtime=None, path_observer=None, output_runtime=None, cat_runtime=None,
                 tree_filesystem=None,
                 move_backend=None,
                 copy_backend=None,
                 buildcheck_encoding=None, capabilities=None):
        self.capabilities = (capabilities if capabilities is not None else
            RuntimeCapabilities(process_backend, process_policy, include_loader,
                checksum_backend, port_runtime, path_observer, output_runtime,
                cat_runtime, tree_filesystem, move_backend, copy_backend))
        self.graph, self.scope, self.declarations = graph, scope, declarations
        encoding = buildcheck_encoding
        if encoding is None and self.capabilities.process_policy is not None:
            encoding = self.capabilities.process_policy.encoding
        preparer = BuildSignaturePreparer(encoding) if encoding is not None else None
        self.observations = InvocationObservations(state, persistence, preparer)
        self.persistence = persistence
        self.cwd = cwd if cwd is not None else graph.base_directory
        self.executor = executor if executor is not None else BodyExecutor()
        self.recheck = PostExecutionRecheck()
        self.completed = {}  # absolute node path -> completion reason, per run
        self.terminal = None
        self.closed = False
        self.initialized = False
        self.port_defaults = port_defaults
        self.port = None
        self.port_runtime = self.capabilities.port_runtime
        self.max_bodies = max_bodies
        self.body_count = 0
        self.active_paths = ()
        self.active_definitions = ()

    def _stop(self, result, status, reason, error=None, blocked_at=None, span=None):
        result.status, result.reason = status, reason
        result.error, result.blocked_at = error, blocked_at
        origin = blocked_at or error
        if not hasattr(origin, 'span'):
            if result.plan is not None and result.plan.bodies:
                origin = result.plan.bodies[0]
            elif result.bodies:
                origin = result.bodies[-1]
        result.span = span if span is not None else getattr(origin, 'span', None)
        result.pending_signatures = self.observations.records()
        self.terminal = result  # no unsafe retry of a partially executed body
        return result

    def _complete(self, node, reason, result):
        if node.path not in self.completed:
            # Work.get_node(add=0) creates temporary nodes for unregistered
            # explicit files. A later request must observe such a file again.
            if self.graph.find_node(node.path) is node:
                self.completed[node.path] = reason
            if node not in result.completed:
                result.completed.append(node)

    def build(self, requests=None):
        return self._build(requests, self.scope, self.cwd)

    def update_targets(self, request):
        """Synchronous command operation in this invocation; never flush here.

        Resolve each item after the previous one completes, as aap_update
        does, so includes can supply a subsequent target's definition.
        """
        if not isinstance(request, UpdateRequest):
            raise TypeError('nested update requires an UpdateRequest')
        result = UpdateResult(request)
        for target in request.targets:
            build = self._build([target], request.scope, request.cwd, request)
            result.builds.append(build)
            if build.status != 'COMPLETE':
                result.status, result.reason = build.status, build.reason
                result.target = target
                result.error, result.blocked_at = build.error, build.blocked_at
                if build.span is not None:
                    result.span = build.span
                return result
        return result

    def _build(self, requests, invocation_scope, cwd, update_request=None):
        if self.closed:
            raise ValueError('build invocation is closed')
        if self.terminal is not None:
            return self.terminal
        result = BuildResult()
        previous_preparation_scope = self.observations.preparation_scope
        self.observations.preparation_scope = invocation_scope
        resolved_requests = None
        # target_update creates one use_recdict per prerequisite group. Keep
        # these live across replans, including writes through a child's _caller.
        prerequisite_scopes = {}
        try:
            if not self.initialized:
                if self.port_defaults and self.scope.local.get('PORTNAME'):
                    markers = self.port_runtime.markers if self.port_runtime is not None else self.persistence
                    self.port = PortDefaults(self.graph, self.scope, self.cwd, markers)
                self.initialized = True
            while True:
                planner = UpdatePlanner(self.graph, self.observations, invocation_scope, cwd)
                plan = planner.plan(requests if resolved_requests is None else resolved_requests,
                                    completed=self.completed, nested=update_request is not None,
                                    active_paths=self.active_paths,
                                    active_definitions=self.active_definitions,
                                    request_origin=update_request)
                if resolved_requests is None:
                    resolved_requests = tuple(n.path for n in plan.requests)
                result.plan = plan
                # A speculative later failure must not prevent earlier work.
                # Commit only unguarded current visits before the next body.
                for entry in plan.entries:
                    if entry.bodies or entry.after or entry.status != 'current':
                        break
                    self._complete(entry.target, entry.reason, result)
                if not plan.bodies:
                    if not plan.complete:
                        diagnostic = plan.diagnostic
                        status = 'BLOCKED' if any(e.status == 'blocked' for e in plan.entries) else 'FAILED'
                        return self._stop(result, status, diagnostic.code, diagnostic)
                    for entry in plan.entries:
                        self._complete(entry.target, entry.reason, result)
                    result.status, result.reason = 'COMPLETE', 'requested_targets_complete'
                    result.pending_signatures = self.observations.records()
                    return result
                first = plan.bodies[0]
                if first.mode != 'update' or first.after:
                    return self._stop(result, 'BLOCKED', 'plan_recheck_required')
                target = first.target
                body_caller = invocation_scope
                scope_key = ()
                for parent_definition in first.prerequisite_definitions:
                    scope_key += (parent_definition,)
                    if scope_key not in prerequisite_scopes:
                        frame = Scope.build(parent_definition.scope, body_caller)
                        frame.local.update({'_dirstack': [], '_prevdir': None,
                            'recipe_name': parent_definition.span.source_id,
                            'recipe_lnum': parent_definition.span.start.line})
                        prerequisite_scopes[scope_key] = frame
                    body_caller = prerequisite_scopes[scope_key]
                # Upstream has finished this target's prerequisite loop before
                # entering any body. Own-body additions do not rerun that loop.
                # Its live build-body list *can* append ordered standard bodies.
                entered = set()
                while True:
                    definitions = [d for d in target.body_definitions if d not in entered]
                    if not definitions:
                        break
                    definition = definitions[0]
                    if not target.virtual and any(part == 'build' or part.startswith('build-')
                                                  for part in target.path.split('/')):
                        # DoBuild.locate_bdir/may_exec_depend calls checkdir
                        # before entry. No directory-creation capability yet.
                        return self._stop(result, 'BLOCKED', 'build_directory_preparation')
                    step = BodyPlan(target, definition, 'update', first.reason, 0, ())
                    step.signature_inputs = first.signature_inputs
                    step.graph_snapshot = self.graph.snapshot()
                    prepared = '' if target.virtual else self.observations.build_signature(definition, target)
                    if type(prepared) is not str:
                        return self._stop(result, 'BLOCKED', 'buildcheck_unavailable')
                    before = None if target.virtual else observe_file(self.observations, target)
                    if self.body_count >= self.max_bodies:
                        return self._stop(result, 'BLOCKED', 'body_limit')
                    context = BuildExecutionContext(step, body_caller, self.graph,
                        self.declarations, prepared_buildcheck=prepared,
                        capabilities=self.capabilities, update_driver=self)
                    self.body_count += 1
                    saved_paths, saved_definitions = self.active_paths, self.active_definitions
                    self.active_paths += first.active_paths
                    self.active_definitions = first.active_definitions
                    try:
                        body = self.executor.execute(context)
                    finally:
                        self.active_paths, self.active_definitions = saved_paths, saved_definitions
                    result.bodies.append(body)
                    if body.status != 'COMPLETED':
                        return self._stop(result, body.status, body.reason, body.error,
                                          body.blocked_at, body.span)
                    decision = self.recheck.check(body, before, self.observations, body_caller)
                    result.completions.append(decision)
                    if decision.status != 'COMPLETE':
                        return self._stop(result, decision.status, decision.reason,
                                          decision.error, span=decision.span)
                    for sibling in decision.siblings:
                        self._complete(sibling, 'shared_body_success', result)
                    entered.add(definition)
                self._complete(target, 'postconditions_satisfied', result)
                # Refresh future decisions against the live graph and state;
                # verified children remain memoized, including shared outputs.
        except Unsupported as error:
            return self._stop(result, 'BLOCKED', 'unsupported_semantics', error)
        except FrontendError as error:
            return self._stop(result, 'FAILED', 'semantic_error', error)
        except BuildSignatureFailure as error:
            return self._stop(result, 'FAILED', 'buildcheck_failed', error,
                              span=error.preparation.span)
        except (OSError, ValueError, NotImplementedError) as error:
            return self._stop(result, 'BLOCKED', 'observation_unavailable', error)
        finally:
            self.observations.preparation_scope = previous_preparation_scope

    def finish(self):
        """End invocation like Main.sign_write_all, even after a later failure.

        Earlier successfully checked bodies may have pending signatures. Failed
        or blocked bodies never add their own. Completion does not imply flush.
        """
        if self.closed:
            raise ValueError('build invocation is already closed')
        result = BuildResult()
        result.pending_signatures = self.observations.records()
        try:
            if result.pending_signatures:
                self.persistence.flush(result.pending_signatures)
                result.persistence_written = True
            result.status, result.reason = 'COMPLETE', 'invocation_finished'
        except (OSError, ValueError, NotImplementedError) as error:
            result.status, result.reason = 'BLOCKED', 'persistence_unavailable'
            result.error = error
        self.closed = True
        return result
''')

_MANIFEST['aap_semantics.buildcheck'] = ('tests/src/aap_semantics/buildcheck.py', False, '9a3695d0ca25751b6971fee5bcaa6a3cc6f1fd1aa6c82126f6c5882d434c2888')
_EMBEDDED['aap_semantics.buildcheck'] = ('tests/src/aap_semantics/buildcheck.py', False, r'''"""Pure, bounded preparation of historical expanded-command buildchecks.

DoBuild.buildcheck_update signs $xcommands after action expansion, comment
removal, special-variable masking, A-A-P expansion and whitespace folding.
This preparer admits only the characterized no-action scalar subset; every
other case has an explicit unavailable result rather than a source-text hash.
"""
from .diagnostics import SemanticError, Unsupported
from .expansion import expand_text
from .command_items import items
from .scopes import Scope
from .target_state import buildcheck_digest, normalize_buildcheck
from .values import _quote_item


def buildcheck_value(text, origin):
    """Util.get_var_val with Expand(0, quote_aap, skip_errors=1).

    No attributes: keep original whitespace/quotes. Parsed attributes: remove
    them and reserialize all items. Historical UserError from item parsing:
    retain the entire value. Our Unsupported gates are never swallowed.
    This applies both to substituted values and the final $xcommands value.
    """
    if '{' not in text:
        return text
    try:
        parsed = items(text, origin, label='buildcheck value')
    except Unsupported:
        raise
    except SemanticError:
        return text
    # str2dictlist overwrites an attribute named "name" with the item name.
    if not any(any(key != 'name' for key in attrs) for name, attrs in parsed):
        return text
    return ' '.join(_quote_item(name) for name, attrs in parsed)


class BuildSignatureFailure(ValueError):
    def __init__(self, preparation):
        super(BuildSignatureFailure, self).__init__(preparation.reason)
        self.preparation = preparation


class BuildSignaturePreparation(object):
    def __init__(self, status, definition, target, commands='', expanded='',
                 signature=None, reason=None, error=None, canonical=''):
        self.status = status
        self.definition = definition
        self.target = target
        self.commands = commands
        self.expanded = expanded
        self.signature = signature
        self.canonical = canonical
        self.reason = reason
        self.error = error
        self.cwd = definition.cwd
        self.span = definition.span

    def snapshot(self):
        return (self.status, self.definition.index, self.target.identity,
                self.span.source_id, self.span.start.line, self.cwd,
                self.commands, self.expanded, self.canonical,
                self.signature, self.reason)


class BuildSignaturePreparer(object):
    def __init__(self, encoding):
        self.encoding = encoding

    def prepare(self, definition, target, caller):
        if target.virtual:
            return BuildSignaturePreparation('PREPARED', definition, target,
                                             signature='')
        if definition.body is None:
            return BuildSignaturePreparation('UNAVAILABLE', definition, target,
                                             reason='no dependency body')
        if definition.build_attributes:
            return BuildSignaturePreparation('UNAVAILABLE', definition, target,
                                             reason='build attributes need characterization')
        # Process.get_commands reads ParsePos.nextline(), which has already
        # folded a trailing backslash and its physical newline into one
        # logical command.  ``cooked`` is the equivalent CST representation:
        # the continuation line's leading whitespace remains adjacent to the
        # preceding text.  DoBuild subsequently strips the generated #@recipe
        # marker lines before expanding.
        lines = []
        for line in definition.body.origin.lines:
            # ParsePos.nextline omits blank and comment-only lines before
            # get_commands sees them, including ones inside a body.
            if not line.significant:
                continue
            if line.indent <= definition.body.origin.threshold:
                return BuildSignaturePreparation('UNAVAILABLE', definition, target,
                                                 reason='unexpected body dedent')
            lines.append(line.cooked + '\n')
        commands = ''.join(lines)
        if ':do' in commands:
            return BuildSignaturePreparation('UNAVAILABLE', definition, target,
                                             commands=commands,
                                             reason='action_expand_do is unavailable')
        # DoBuild removes full comment lines after action expansion. Control
        # flow and embedded Python are still source lines at this stage.
        filtered = ''.join(line for line in lines if not line.lstrip(' \t').startswith('#'))
        try:
            scope = Scope.build(definition.scope, caller)
            scope.local.update({'source': '', 'target': '', 'fname': '', 'match': ''})
            scope.local['commands'] = filtered
            expanded = expand_text(filtered, scope, definition,
                                   item_attributes=True,
                                   value_transform=buildcheck_value,
                                   preserve_missing=True)
            # Default checkstring is $xcommands. Substitution applies attr=0
            # conversion again; inserted dollar text is never recursively read.
            check = buildcheck_value(expanded, definition)
            signature = buildcheck_digest(check, self.encoding)
        except Unsupported as error:
            return BuildSignaturePreparation('UNAVAILABLE', definition, target,
                                             commands=commands, reason=str(error), error=error)
        except (SemanticError, UnicodeError, LookupError) as error:
            return BuildSignaturePreparation('FAILED', definition, target,
                                             commands=commands, reason=str(error), error=error)
        return BuildSignaturePreparation('PREPARED', definition, target,
                                         commands=commands, expanded=expanded,
                                         canonical=normalize_buildcheck(check),
                                         signature=signature)
''')

_MANIFEST['aap_semantics.capabilities'] = ('tests/src/aap_semantics/capabilities.py', False, 'f4c8bff5f783afed9ca4bca27aa865a98dbb953f3395d8f77e522231dd75637e')
_EMBEDDED['aap_semantics.capabilities'] = ('tests/src/aap_semantics/capabilities.py', False, r'''"""Explicit host-facing capabilities shared by semantic execution phases."""


class RuntimeCapabilities(object):
    __slots__ = ('process_backend', 'process_policy', 'include_loader',
                 'checksum_backend', 'port_runtime', 'path_observer',
                 'output_runtime', 'cat_runtime', 'tree_filesystem',
                 'move_backend', 'copy_backend', '_frozen')

    def __init__(self, process_backend=None, process_policy=None,
                 include_loader=None, checksum_backend=None, port_runtime=None,
                 path_observer=None, output_runtime=None, cat_runtime=None,
                 tree_filesystem=None, move_backend=None, copy_backend=None):
        values = (process_backend, process_policy, include_loader,
                  checksum_backend, port_runtime, path_observer,
                  output_runtime, cat_runtime, tree_filesystem,
                  move_backend, copy_backend)
        for name, value in zip(self.__slots__, values):
            object.__setattr__(self, name, value)
        object.__setattr__(self, '_frozen', True)

    def __setattr__(self, name, value):
        if getattr(self, '_frozen', False):
            raise AttributeError('RuntimeCapabilities is immutable')
        object.__setattr__(self, name, value)

    def replace(self, **changes):
        names = self.__slots__[:-1]
        if any(name not in names for name in changes):
            raise TypeError('unknown runtime capability')
        values = dict((name, getattr(self, name)) for name in names)
        values.update(changes)
        return RuntimeCapabilities(**values)
''')

_MANIFEST['aap_semantics.cat'] = ('tests/src/aap_semantics/cat.py', False, 'b5b3022986081cdca4e46ab5032567cfbefd4d742966caf9c811c0f0bff700dd')
_EMBEDDED['aap_semantics.cat'] = ('tests/src/aap_semantics/cat.py', False, r'''"""Bounded redirected A-A-P cat: exact Linux bytes, never a shell command."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text
from .command_items import items
from .checksum import ArtifactBackend, ArtifactUnavailable
from .output import print_parts, destination, TextWriter


class CatRequest(Node):
    def __init__(self, node, scope, python, cwd, policy):
        super(CatRequest, self).__init__(node)
        self.raw = render_value(node.arguments, python)
        raw_sources, raw_destination, mode = print_parts(self.raw, node, 'cat')
        self.raw_sources, self.raw_destination = raw_sources, raw_destination
        if mode not in ('append', 'overwrite'):
            raise Unsupported(node, 'cat requires bounded append/overwrite redirection')
        self.destination, self.cwd = mode, cwd
        self.path, self.expanded_destination = destination(
            raw_destination, scope, node, cwd, details=True, label='cat')
        self.expanded_sources = expand_text(raw_sources, scope, node)
        source_items = items(self.expanded_sources, node, 'cat source')
        if not source_items:
            raise SemanticError(node, 'cat requires at least one source filename')
        self.source_items = tuple(name for name, attrs in source_items)
        paths = []
        for name, attrs in source_items:
            if attrs or name == '-' or any(c in name for c in '~*?[]{}:'):
                raise Unsupported(node, 'cat source attributes/globs/tilde/URL/pipe input are deferred')
            if '\x00' in name:
                raise SemanticError(node, 'NUL in cat source path')
            if not posixpath.isabs(name):
                if type(cwd) is not str or not posixpath.isabs(cwd):
                    raise Unsupported(node, 'relative cat source needs an absolute logical cwd')
                name = posixpath.join(cwd, name)
            paths.append(name)
        self.paths = tuple(paths)
        # Encoding applies to paths only. Contents never pass through a codec.
        self.path_bytes = policy.encode(self.path, node)
        self.source_path_bytes = tuple(policy.encode(path, node) for path in paths)
        if any(b'\x00' in path for path in (self.path_bytes,) + self.source_path_bytes):
            raise Unsupported(node, 'cat path encoding contains NUL')
        self.encoding = policy.encoding


class CatRecord(Node):
    def __init__(self, request):
        super(CatRecord, self).__init__(request)
        self.request = request
        self.status, self.phase, self.error = 'PENDING', 'open', None
        self.opened, self.closed = False, False
        self.active_source = None
        self.reads = []  # ordered (absolute path, byte count), including duplicates
        self.bytes_read, self.bytes_written = 0, 0
        self.close_error = None
        self.message = None


class CatRuntime(object):
    def __init__(self, policy, reader=None, writer=None):
        self.policy = policy
        self.reader = reader if reader is not None else ArtifactBackend()
        self.writer = writer if writer is not None else TextWriter()

    def execute(self, node, scope, python, cwd, records):
        request = CatRequest(node, scope, python, cwd, self.policy)
        record = CatRecord(request)
        records.append(record)
        session = None
        try:
            session = self.writer.open_bytes(request)
            record.opened = True
            for path in request.paths:
                record.phase, record.active_source = 'read', path
                chunks = []
                for chunk in self.reader.chunks(path):
                    if type(chunk) is not bytes:
                        raise OSError('cat reader must supply bytes')
                    chunks.append(chunk)
                # Historical readlines completes one entire file before any
                # of its data is written. This also preserves self-append.
                data = b''.join(chunks)
                record.reads.append((path, len(data)))
                record.bytes_read += len(data)
                record.phase = 'write'
                session.write(data)
                record.bytes_written += len(data)
            record.phase = 'close'
            session.close()
            record.closed = True
        except (ArtifactUnavailable, NotImplementedError) as error:
            record.status = 'BLOCKED'
            record.error = Unsupported(node, str(error))
        except Exception as error:
            record.status = 'FAILED'
            record.error = SemanticError(node, 'cat ' + record.phase + ' failed: ' + str(error))
        finally:
            if session is not None and not record.closed and record.phase != 'close':
                # Close resources on error without claiming rollback. Upstream
                # relies on object lifetime on some exceptional exits.
                try:
                    session.close()
                    record.closed = True
                except Exception as error:
                    record.close_error = str(error)
        if record.error is not None:
            raise record.error
        record.status, record.phase = 'COMPLETED', 'complete'
        # Historical msg_info presentation only; no host console/log operation.
        record.message = 'Concatenated files into "' + request.path + '"'
''')

_MANIFEST['aap_semantics.checksum'] = ('tests/src/aap_semantics/checksum.py', False, '9ad593c17ec2d0785d974dda5c1f0376dbe7fa791c3a2e4a3e820adea7cb365d')
_EMBEDDED['aap_semantics.checksum'] = ('tests/src/aap_semantics/checksum.py', False, r'''"""Read-only package MD5 verification, independent of build signatures.

Evidence: Commands.aap_checksum/get_args, Dictlist.str2dictlist/parse_attr,
Sign.check_md5/hexdigest. No local filesystem adapter is implicitly enabled.
"""
import hashlib
import posixpath

from .model import Node, PythonFragment
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text
from .command_items import attributes, items


class ArtifactUnavailable(Exception):
    """The caller has not supplied a required read capability."""


class ArtifactBackend(object):
    """Read-only capability. Paths are explicit absolute POSIX paths.

    exists must distinguish absence from an existing directory/unreadable file.
    chunks yields exact bytes from a regular file or raises OSError. A missing
    file after a successful exists probe is a read error, not a skipped check.
    No write, process, graph or signature operation belongs to this interface.
    """
    def exists(self, path):
        raise ArtifactUnavailable('artifact existence observation unavailable')

    def chunks(self, path):
        raise ArtifactUnavailable('artifact byte reads unavailable')


class MemoryArtifacts(ArtifactBackend):
    """In-memory regular files for controlled semantic tests; records reads."""
    def __init__(self, files=None, shared=False):
        # Explicit fixture adapter for a live MemoryTextWriter byte store.
        self.files = files if shared and files is not None else dict(files or {})
        self.observations = []

    def exists(self, path):
        self.observations.append(('exists', path))
        return path in self.files

    def chunks(self, path):
        self.observations.append(('read', path))
        if path not in self.files:
            raise OSError('artifact disappeared: ' + path)
        data = self.files[path]
        if type(data) is not bytes:
            raise OSError('artifact is not a regular byte file: ' + path)
        for index in range(0, len(data), 32768):
            yield data[index:index + 32768]


class ChecksumBackend(object):
    """Digest observation seam. Default implementation hashes exact bytes.

    Controlled integration tests may override md5 with a recorded observation;
    comparison remains in ChecksumRuntime and is never disabled. A real adapter
    must use the supplied read-only artifacts (no recipe-controlled callbacks).
    """
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def exists(self, request):
        return self.artifacts.exists(request.path)

    def md5(self, request):
        digest = hashlib.md5()
        for chunk in self.artifacts.chunks(request.path):
            if type(chunk) is not bytes:
                raise OSError('artifact backend must yield bytes')
            digest.update(chunk)
        return digest.hexdigest()


class ChecksumRequest(Node):
    def __init__(self, origin, filename, attributes, cwd):
        super(ChecksumRequest, self).__init__(origin)
        self.filename = filename
        self.attributes = dict(attributes)
        self.expected = {'md5': attributes['md5']} if 'md5' in attributes else {}
        self.cwd = cwd
        if '\x00' in filename:
            raise SemanticError(origin, 'NUL in checksum filename')
        if not posixpath.isabs(filename):
            if cwd is None or not posixpath.isabs(cwd):
                raise Unsupported(origin, 'relative checksum path requires an explicit absolute cwd')
            filename = posixpath.join(cwd, filename)
        # No glob, home expansion, alias/search lookup or normpath: preserving
        # a/../b and trailing slash matters for symlinks and directory checks.
        self.path = filename


class ChecksumResult(object):
    def __init__(self, request):
        self.request = request
        self.status = None  # VERIFIED, MISSING, FAILED, BLOCKED
        self.reason = None
        self.computed = {}
        self.verified_algorithms = ()
        self.error = None


class ChecksumRuntime(object):
    def __init__(self, backend):
        self.backend = backend

    def verify(self, command, scope, python, cwd, records):
        if command.body is not None:
            raise Unsupported(command, 'checksum bodies are outside the production subset')
        if any(isinstance(part, PythonFragment) for piece in command.arguments.pieces for part in piece):
            raise Unsupported(command, 'checksum backticks are outside the production subset')
        raw = render_value(command.arguments, python)
        if '|' in raw:
            raise Unsupported(command, 'checksum pipelines are outside the production subset')
        leading, index = attributes(raw, 0, command, leading_scope=scope, label="checksum")
        text = expand_text(raw[index:], scope, command, item_attributes=True)
        parsed = items(text, command, label="checksum")
        if not parsed:
            raise SemanticError(command, ':checksum requires a file argument')
        for filename, attrs in parsed:
            request = ChecksumRequest(command, filename, attrs, cwd)
            result = ChecksumResult(request)
            records.append(result)  # preserve observations even on failure
            try:
                exists = self.backend.exists(request)
                if type(exists) is not bool:
                    raise OSError('artifact existence observation must be bool')
                if not exists:
                    result.status, result.reason = 'MISSING', 'artifact_missing'
                    continue  # historical note; no digest validation or fetch
                if not attrs.get('md5'):
                    self.fail(result, 'missing_md5', 'md5 attribute missing')
                computed = self.backend.md5(request)
                if (type(computed) is not str or len(computed) != 32
                        or any(c not in '0123456789abcdef' for c in computed)):
                    raise OSError('invalid MD5 observation from checksum backend')
                result.computed['md5'] = computed
                if computed != attrs['md5']:
                    self.fail(result, 'digest_mismatch', 'md5 checksum mismatch')
                result.status, result.reason = 'VERIFIED', 'digest_match'
                result.verified_algorithms = ('md5',)
            except ArtifactUnavailable as error:
                result.status, result.reason = 'BLOCKED', 'artifact_capability_unavailable'
                result.error = Unsupported(command, str(error))
                raise result.error
            except SemanticError:
                # FrontendError derives from ValueError; do not reclassify an
                # intentional mismatch/missing-digest failure as a read error.
                raise
            except (OSError, ValueError) as error:
                self.fail(result, 'artifact_read_error', 'cannot compute md5 checksum: ' + str(error))

    @staticmethod
    def fail(result, reason, message):
        result.status, result.reason = 'FAILED', reason
        result.error = SemanticError(result.request, message + ': ' + result.request.filename)
        raise result.error
''')

_MANIFEST['aap_semantics.cli'] = ('tests/src/aap_semantics/cli.py', False, '8d5d45f6a411927101b1de22d9ace55196f4e5cf85c4a31bcea2712a88297c14')
_EMBEDDED['aap_semantics.cli'] = ('tests/src/aap_semantics/cli.py', False, r'''"""Bounded external A-A-P argv handling from DoArgs.doargs().

This module deliberately accepts an already-tokenized argv sequence.  Shell
quoting is resolved before this boundary and must not be parsed again here.
Only ordinary assignment items and target items are represented; command-line
options remain an explicit adapter-level unsupported surface.
"""
import string


class CliArgumentError(Exception):
    pass


class CliArguments(object):
    def __init__(self, assignments, targets):
        self.assignments = assignments
        self.targets = targets


def valid_variable_name(name):
    """Util.varchar(), including the historical empty-name edge case."""
    allowed = string.ascii_letters + string.digits + '._'
    return all(character in allowed for character in name)


def parse_arguments(argv):
    """Separate first-``=`` assignments from targets as DoArgs.doargs does."""
    assignments = {}
    targets = []
    for argument in argv:
        if not argument:
            raise CliArgumentError('empty arguments are unsupported')
        if argument == '-':
            raise CliArgumentError('reading from stdin is not implemented')
        if argument[0] == '-':
            raise CliArgumentError('command-line options are unsupported')
        if '=' in argument:
            name, value = argument.split('=', 1)
            assignments[name] = value
        else:
            targets.append(argument)
    return CliArguments(assignments, targets)


def apply_assignments(scope, assignments):
    """Apply valid DoArgs values to the recipe and its separate _arg scope."""
    applied = {}
    ignored = {}
    for name, value in assignments.items():
        if valid_variable_name(name):
            scope.set_command_line(name, value)
            applied[name] = value
        else:
            ignored[name] = value
    return applied, ignored
''')

_MANIFEST['aap_semantics.command_items'] = ('tests/src/aap_semantics/command_items.py', False, '9843e27f6bf0c85a5e9527374b502b5bce6769da056dbcb92de08db28286cd86')
_EMBEDDED['aap_semantics.command_items'] = ('tests/src/aap_semantics/command_items.py', False, r'''"""Bounded Dictlist item/attribute syntax shared by command consumers."""
import string

from .diagnostics import SemanticError, Unsupported
from .expansion import expand_text


def attributes(text, index, origin, leading_scope=None, label="item"):
    attributes = {}
    while True:
        start = index
        while start < len(text) and text[start] in ' \t':
            start += 1
        if start == len(text) or text[start] != '{':
            return attributes, index
        end = start + 1
        quote = None
        while end < len(text):
            char = text[end]
            if quote:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == '}':
                break
            elif char == '{':
                raise Unsupported(origin, 'nested ' + label + ' attributes are deferred')
            end += 1
        if end == len(text):
            raise SemanticError(origin, 'missing } in ' + label + ' attribute')
        parts = text[start + 1:end].strip(' \t').split('=', 1)
        name = parts[0].strip(' \t')
        if not name or any(c not in string.ascii_letters + string.digits + '_.' for c in name):
            raise SemanticError(origin, 'invalid ' + label + ' attribute name')
        # get_attrval retains quotes and trims whitespace, not digest case.
        value = 1 if len(parts) == 1 else parts[1].strip(' \t')
        if leading_scope is not None and type(value) is str:
            # Expand every occurrence before overwrite, including discarded
            # leading attributes. An earlier undefined value still errors.
            value = expand_text(value, leading_scope, origin, item_attributes=True)
        attributes[name] = value
        index = end + 1  # duplicate attribute names: last value wins


def items(text, origin, label="item"):
    items = []
    index = 0
    while index < len(text):
        if text[index] in ' \t\n':
            index += 1
            continue
        chars = []
        quote = None
        while index < len(text):
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
                else:
                    chars.append(char)
            elif char in "'\"":
                quote = char
            elif char in ' \t\n{':
                break
            else:
                chars.append(char)
            index += 1
        if quote:
            raise SemanticError(origin, 'missing quote in ' + label + ' filename')
        attrs, end = attributes(text, index, origin, label=label)
        name = ''.join(chars)
        if not name and attrs:
            raise Unsupported(origin, label + ' attributes without an item are deferred')
        if name:
            items.append((name, attrs))
        index = end
    return items
''')

_MANIFEST['aap_semantics.completion'] = ('tests/src/aap_semantics/completion.py', False, '8d19b8a9f27ba844a4eac7eeb660fc3ecdb7e294de49e02b03fa9c474078376b')
_EMBEDDED['aap_semantics.completion'] = ('tests/src/aap_semantics/completion.py', False, r'''"""Post-body checks and pending signatures; never execute a recipe or flush."""
from .model import Node
from .target_state import FileState
from .persistence import SignatureRecord
from .values import MISSING


def observe_file(state, node):
    value = state.file_state(node)
    if (not isinstance(value, FileState) or type(value.exists) is not bool
            or type(value.directory) is not bool
            or type(value.mtime) not in (int, float) or value.mtime < 0):
        raise ValueError('invalid file-state observation')
    return value


class TargetCompletionDecision(Node):
    def __init__(self, origin, status, reason):
        super(TargetCompletionDecision, self).__init__(origin)
        self.status = status
        self.reason = reason
        self.target = origin.target
        self.before = None
        self.after = None
        self.records = ()
        self.siblings = ()
        self.graph_changed = False
        self.error = None


class PostExecutionRecheck(object):
    def check(self, body, before, observations, invocation_scope):
        decision = TargetCompletionDecision(body.context, 'BLOCKED', 'body_not_completed')
        decision.before = before
        decision.graph_changed = body.graph_changed
        if body.status != 'COMPLETED':
            return decision
        step = body.context.step
        try:
            # DoBuild.may_exec_depend only requires an originally nonzero
            # trigger timestamp to remain nonzero. Missing outputs may stay
            # missing; unchanged timestamps are explicitly allowed upstream.
            if not step.target.virtual and before.mtime > 0:
                decision.after = observe_file(observations, step.target)
                if not decision.after.exists or decision.after.mtime == 0:
                    decision.status, decision.reason = 'FAILED', 'trigger_disappeared'
                    return decision
            for target in step.signature_targets:
                observations.invalidate(target)
            if not step.signature_targets:
                decision.status, decision.reason = 'COMPLETE', 'postconditions_satisfied'
                return decision
            values = {}
            # All traversed dependency source groups, not just this body's.
            for group in step.signature_inputs:
                for item in group:
                    if item.node.virtual:
                        continue
                    method = invocation_scope.lookup('DEFAULTCHECK')
                    if method is MISSING:
                        method = 'md5'
                    if observe_file(observations, item.node).directory:
                        method = 'none'
                    if method not in ('md5', 'c_md5', 'time', 'newer', 'none'):
                        raise NotImplementedError('signature check method unavailable')
                    value = observations.current_signature(item.node, method)
                    if value is None:
                        decision.reason = 'signature_unavailable'
                        return decision
                    values[(item.node.identity, method)] = value
            buildcheck = body.context.prepared_buildcheck if step.buildcheck_required else ''
            if buildcheck is None:
                decision.reason = 'buildcheck_unavailable'
                return decision
            if buildcheck:
                values[('', 'buildcheck')] = buildcheck
            records = []
            for target in step.signature_targets:
                timestamp = observations.persistence.timestamp() if values else None
                if values and type(timestamp) is not str:
                    raise ValueError('signature timestamp must be a string')
                records.append(SignatureRecord(target, step.definition, values, timestamp))
            decision.records = tuple(records)
            observations.stage(records)  # only after ALL observations succeeded
            decision.siblings = tuple(n for n in step.signature_targets if n is not step.target)
            decision.status, decision.reason = 'COMPLETE', 'postconditions_satisfied'
        except (OSError, ValueError, NotImplementedError) as error:
            decision.reason, decision.error = 'observation_unavailable', error
        return decision
''')

_MANIFEST['aap_semantics.copy_runtime'] = ('tests/src/aap_semantics/copy_runtime.py', False, 'f4b5b0810b49ab084f4fba7fe064892fc8029fefeb4cad51cc6dd696d487f291')
_EMBEDDED['aap_semantics.copy_runtime'] = ('tests/src/aap_semantics/copy_runtime.py', False, r'''"""Bounded local regular-file :copy through an injected filesystem backend.

The reached production form is one plain local regular source and one local
destination.  It deliberately has a distinct backend from :move: historical
copy uses shutil.copy() and leaves the source in place, while move first tries
rename and has a different fallback path.
"""
import posixpath

from .model import Node
from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text


class CopyObservation(object):
    def __init__(self, status='COMPLETED', kind=None, detail=None):
        self.status, self.kind, self.detail = status, kind, detail


class CopyRequest(Node):
    def __init__(self, origin, source, destination, effective_destination, cwd,
                 source_kind, destination_kind):
        super(CopyRequest, self).__init__(origin)
        self.source_argument = source
        self.destination_argument = destination
        self.effective_destination_argument = effective_destination
        self.cwd = cwd
        self.source = _resolve(source, cwd)
        self.destination = _resolve(destination, cwd)
        self.effective_destination = _resolve(effective_destination, cwd)
        self.source_kind = source_kind
        self.destination_kind = destination_kind
        self.overwrite = 'replace'


class CopyResult(object):
    def __init__(self, status='COMPLETED', detail=None):
        self.status, self.detail = status, detail


class CopyBackend(object):
    """Observation and regular-file copy mutation capability."""
    def path_kind(self, path):
        return CopyObservation('UNAVAILABLE', detail='copy observation capability unavailable')

    def copy(self, request):
        return CopyResult('UNAVAILABLE', 'copy mutation capability unavailable')


class MemoryCopyBackend(CopyBackend):
    """Controlled regular-byte files and directories for semantic tests."""
    def __init__(self, files=None, directories=None, failures=None):
        self.files = files if files is not None else {}
        self.directories = set(directories or ())
        self.failures = dict(failures or {})
        self.observations = []
        self.requests = []
        for path in self.files:
            self._parents(path)
        for path in tuple(self.directories):
            self._parents(path)

    def _parents(self, path):
        parent = posixpath.dirname(path)
        while parent and parent != '/':
            self.directories.add(parent)
            parent = posixpath.dirname(parent)
        if path.startswith('/'):
            self.directories.add('/')

    def path_kind(self, path):
        self.observations.append(path)
        if path in self.files:
            return CopyObservation(kind='regular')
        if path in self.directories:
            return CopyObservation(kind='directory')
        return CopyObservation(kind='missing')

    def copy(self, request):
        self.requests.append(request)
        failure = self.failures.get((request.source, request.effective_destination))
        if failure is not None:
            return failure
        if request.source not in self.files:
            return CopyResult('FAILED', 'copy source does not exist: ' + request.source)
        parent = posixpath.dirname(request.effective_destination)
        if parent not in self.directories:
            return CopyResult('FAILED', 'copy destination parent does not exist: ' + parent)
        self.files[request.effective_destination] = self.files[request.source]
        return CopyResult()


class CopyRecord(Node):
    def __init__(self, request):
        super(CopyRecord, self).__init__(request)
        self.request, self.result = request, None
        self.status, self.error = 'PENDING', None


def _resolve(path, cwd):
    return path if posixpath.isabs(path) else posixpath.join(cwd, path)


def _observe(backend, origin, path, role):
    try:
        outcome = backend.path_kind(path)
    except NotImplementedError as error:
        outcome = CopyObservation('UNAVAILABLE', detail=str(error))
    except Exception as error:
        outcome = CopyObservation('FAILED', detail=str(error))
    if not isinstance(outcome, CopyObservation) or outcome.status not in (
            'COMPLETED', 'UNAVAILABLE', 'FAILED'):
        raise SemanticError(origin, 'invalid copy observation result')
    if outcome.status == 'UNAVAILABLE':
        raise Unsupported(origin, outcome.detail or 'copy observation capability unavailable')
    if outcome.status == 'FAILED':
        raise SemanticError(origin, 'copy ' + role + ' observation failed: ' +
                            str(outcome.detail))
    if outcome.kind not in ('regular', 'directory', 'missing', 'other'):
        raise SemanticError(origin, 'invalid copy ' + role + ' path kind')
    return outcome.kind


class CopyRuntime(object):
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else CopyBackend()

    def execute(self, node, scope, python, cwd, records):
        raw = render_value(node.arguments, python)
        if raw.lstrip(' \t').startswith('{'):
            raise Unsupported(node, ':copy attributes/options are deferred')
        expanded = expand_text(raw, scope, node, item_attributes=True)
        parsed = items(expanded, node, label='copy')
        if len(parsed) != 2 or any(attrs for name, attrs in parsed):
            raise Unsupported(node, 'only one source and one destination are supported for :copy')
        source, destination = parsed[0][0], parsed[1][0]
        if (not source or not destination or '\x00' in source or '\x00' in destination):
            raise SemanticError(node, ':copy paths must be nonempty and NUL-free')
        if any(char in source + destination for char in '~*?[]{}') or ':' in source + destination:
            raise Unsupported(node, ':copy glob, user-directory, URL and attribute paths are deferred')
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, ':copy requires an explicit absolute cwd')
        source_path = _resolve(source, cwd)
        destination_path = _resolve(destination, cwd)
        source_kind = _observe(self.backend, node, source_path, 'source')
        if source_kind == 'missing':
            raise SemanticError(node, 'copy source does not exist: ' + source_path)
        if source_kind != 'regular':
            raise Unsupported(node, 'only regular-file sources are supported for :copy')
        destination_kind = _observe(self.backend, node, destination_path, 'destination')
        if destination_kind == 'other':
            raise Unsupported(node, 'special destination paths are deferred for :copy')
        effective = destination
        if destination_kind == 'directory':
            effective = posixpath.join(destination, posixpath.basename(source))
            effective_kind = _observe(self.backend, node, _resolve(effective, cwd),
                                      'effective destination')
            if effective_kind == 'other':
                raise Unsupported(node, 'special destination paths are deferred for :copy')
        request = CopyRequest(node, source, destination, effective, cwd,
                              source_kind, destination_kind)
        record = CopyRecord(request)
        records.append(record)
        try:
            outcome = self.backend.copy(request)
        except NotImplementedError as error:
            outcome = CopyResult('UNAVAILABLE', str(error))
        except Exception as error:
            outcome = CopyResult('FAILED', str(error))
        record.result = outcome
        if not isinstance(outcome, CopyResult) or outcome.status not in (
                'COMPLETED', 'UNAVAILABLE', 'FAILED'):
            record.status = 'FAILED'
            raise SemanticError(node, 'invalid copy mutation result')
        if outcome.status == 'UNAVAILABLE':
            record.status = 'BLOCKED'
            record.error = Unsupported(node, outcome.detail or 'copy mutation capability unavailable')
            raise record.error
        if outcome.status == 'FAILED':
            record.status = 'FAILED'
            record.error = SemanticError(node, 'copy failed: ' + str(outcome.detail))
            raise record.error
        record.status = 'COMPLETED'
''')

_MANIFEST['aap_semantics.declarations'] = ('tests/src/aap_semantics/declarations.py', False, 'd6a2ad52edf6e34ceb72a7dafca97bd5ded4299f1af2ae5276e0a33e6f2fc2e2')
_EMBEDDED['aap_semantics.declarations'] = ('tests/src/aap_semantics/declarations.py', False, r'''"""Session-owned declarative state; no detection or action execution."""
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
''')

_MANIFEST['aap_semantics.dependency_items'] = ('tests/src/aap_semantics/dependency_items.py', False, '64ed1b2a16141d5ba2680fde757ac4284ef1ba54cb547f25ed0d83312875bf34')
_EMBEDDED['aap_semantics.dependency_items'] = ('tests/src/aap_semantics/dependency_items.py', False, r'''"""Bounded dependency item language, after separate A-A-P expansion."""
from . import model as m
from .diagnostics import SemanticError, Unsupported
from .expansion import expand_text


class DependencyItem(m.Node):
    def __init__(self, origin, name, attributes):
        super(DependencyItem, self).__init__(origin)
        self.name = name
        self.attributes = dict(attributes)
        self.node = None


def raw_field(value):
    pieces = []
    for piece in value.pieces:
        if any(isinstance(part, m.PythonFragment) for part in piece):
            raise Unsupported(value, 'dependency backticks are outside the production subset')
        text = ''.join(part.value for part in piece)
        if text:
            pieces.append(text)
    # get_commands joins header continuation pieces with spaces, not $br.
    return ' '.join(pieces)


def attributes(text, index, origin):
    """Read adjacent {virtual[=value]} groups, retaining value spelling."""
    result = {}
    while True:
        start = index
        while start < len(text) and text[start] in ' \t':
            start += 1
        if start == len(text) or text[start] != '{':
            return result, index
        end = start + 1
        quote = None
        while end < len(text):
            char = text[end]
            if quote:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == '}':
                break
            elif char == '{':
                raise Unsupported(origin, 'nested dependency attributes are deferred')
            end += 1
        if end == len(text):
            raise SemanticError(origin, 'missing } in dependency attribute')
        content = text[start + 1:end].strip(' \t')
        parts = content.split('=', 1)
        name = parts[0].strip(' \t')
        if name != 'virtual':
            raise Unsupported(origin, 'unsupported dependency attribute: ' + name)
        result[name] = 1 if len(parts) == 1 else parts[1].strip(' \t')
        index = end + 1


def parse_items(text, origin):
    result = []
    index = 0
    while index < len(text):
        if text[index] in ' \t\n':
            index += 1
            continue
        chars = []
        quote = None
        while index < len(text):
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
                else:
                    chars.append(char)
            elif char in "'\"":
                quote = char
            elif char in ' \t\n{':
                break
            else:
                chars.append(char)
            index += 1
        if quote:
            raise SemanticError(origin, 'missing quote in dependency item')
        attrs, end = attributes(text, index, origin)
        name = ''.join(chars)
        if not name and attrs:
            raise Unsupported(origin, 'attributes without a dependency item are unsupported')
        if name:
            if (any(c in name for c in '*?[%') or name.startswith('~')
                    or '://' in name or '\x00' in name):
                raise Unsupported(origin, 'dependency patterns, home paths and URLs are deferred: ' + name)
            result.append(DependencyItem(origin, name, attrs))
        index = end
    return tuple(result)


def dependency_fields(dependency, scope):
    targets = raw_field(dependency.target_value)
    sources = raw_field(dependency.source_value)
    # Leading build attributes are recognized BEFORE expansion and remain raw.
    build_attributes, index = attributes(sources, 0, dependency.source_value)
    target_items = parse_items(expand_text(targets, scope, dependency.target_value,
                                           item_attributes=True), dependency.target_value)
    source_items = parse_items(expand_text(sources[index:], scope, dependency.source_value,
                                           item_attributes=True), dependency.source_value)
    return target_items, source_items, build_attributes
''')

_MANIFEST['aap_semantics.diagnostics'] = ('tests/src/aap_semantics/diagnostics.py', False, 'de37b839f3ec77464875c5bb2511ec7e58b770bba9fe8590feb76b16fe9fbf8e')
_EMBEDDED['aap_semantics.diagnostics'] = ('tests/src/aap_semantics/diagnostics.py', False, r'''"""Diagnostics shared by lowering, conversions and interpretation."""
from aap_frontend import FrontendError


class SemanticError(FrontendError):
    def __init__(self, origin, reason):
        super(SemanticError, self).__init__(origin.source,
                                            origin.span.start.offset, reason)
        self.span = origin.span


class Unsupported(SemanticError):
    pass


class UndefinedName(SemanticError):
    pass
''')

_MANIFEST['aap_semantics.directories'] = ('tests/src/aap_semantics/directories.py', False, '3d880c73b5545a7b1429cf25eedf36342a4a17b404dc21caf528bb8864ca386a')
_EMBEDDED['aap_semantics.directories'] = ('tests/src/aap_semantics/directories.py', False, r'''"""Commands.aap_cd: explicit execution-frame cwd, never host chdir."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text
from .command_items import items
from .port_commands import PortDirectories


class ExecutionDirectoryState(object):
    """One mutable cwd per execution frame; previous is a scope-local value.

    Includes share this frame. Dependency calls create a new frame. Helpers
    which historically save/restore cwd select their own explicit directory.
    """
    def __init__(self, current=None):
        self.current = current


class DirectoryChange(Node):
    def __init__(self, origin, cwd, previous):
        super(DirectoryChange, self).__init__(origin)
        self.before, self.previous_before = cwd, previous
        self.raw = self.expanded = self.path = self.requested = None
        self.components = ()
        self.after, self.previous_after = cwd, previous
        self.status = self.error = None


def change_directory(evaluator, node, records):
    scope, cwd = evaluator.scope, evaluator.cwd
    record = DirectoryChange(node, cwd, scope.local.get('_prevdir'))
    records.append(record)
    try:
        if node.body is not None:
            raise Unsupported(node, ':cd command bodies are not supported')
        record.raw = render_value(node.arguments, evaluator.python)
        record.expanded = expand_text(record.raw, scope, node)
        components = items(record.expanded, node, 'cd')
        if not components:
            raise SemanticError(node, ':cd requires at least one argument')
        if any(attrs for name, attrs in components):
            raise Unsupported(node, ':cd item attributes are deferred')
        if any(name.startswith('~') for name, attrs in components):
            raise Unsupported(node, ':cd tilde expansion requires an explicit home-directory policy')
        record.components = tuple(name for name, attrs in components)
        record.path = posixpath.join(*record.components)
        if record.path == '-':
            record.path = scope.local.get('_prevdir')
            if not record.path:
                raise SemanticError(node, 'No previous directory for :cd -')
            if type(record.path) is not str:
                raise SemanticError(node, ':cd previous directory must be a string')
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, ':cd requires an explicit absolute runtime cwd')
        # aap_chdir records getcwd BEFORE attempting chdir, including failures.
        scope.local['_prevdir'] = cwd
        record.previous_after = cwd
        if '\x00' in record.path or '\x00' in cwd:
            raise SemanticError(node, 'NUL in :cd path')
        # Do not normpath before observation: symlink/.. resolution belongs to
        # the capability. No glob, search path or automatic mkdir is involved.
        record.requested = posixpath.join(cwd, record.path)
        runtime = evaluator.port_runtime
        directories = runtime.commands.directories if runtime is not None else PortDirectories()
        observed = directories.enter(record.requested)
        if type(observed) is not str or not posixpath.isabs(observed) or '\x00' in observed:
            raise SemanticError(node, 'invalid :cd directory observation')
        evaluator.cwd = observed
        record.after, record.status = observed, 'COMPLETED'
    except NotImplementedError as error:
        record.status, record.error = 'BLOCKED', Unsupported(node, str(error))
        raise record.error
    except Unsupported as error:
        record.status, record.error = 'BLOCKED', error
        raise
    except SemanticError as error:
        record.status, record.error = 'FAILED', error
        raise
    except (OSError, ValueError) as error:
        record.status = 'FAILED'
        record.error = SemanticError(node, ':cd failed: ' + str(error))
        raise record.error
    return record
''')

_MANIFEST['aap_semantics.evaluator'] = ('tests/src/aap_semantics/evaluator.py', False, '43d6a0545eabcdcfe1a44aa8cb24139e1c2b84ac28bb33737a1e96cdf017a7c7')
_EMBEDDED['aap_semantics.evaluator'] = ('tests/src/aap_semantics/evaluator.py', False, r'''"""Metadata evaluation; unsupported runtime constructs are explicit barriers."""
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
''')

_MANIFEST['aap_semantics.expansion'] = ('tests/src/aap_semantics/expansion.py', False, '02bf4bd350728755b6d85a0c3d13262924210ca7f8f64b0cc82bbca4ecc3979c')
_EMBEDDED['aap_semantics.expansion'] = ('tests/src/aap_semantics/expansion.py', False, r'''"""A-A-P expansion, separate from lexical parsing and Python interpretation."""
import string

from . import model as m
from .diagnostics import SemanticError, UndefinedName, Unsupported
from .values import MISSING, DeferredExpansion, UnavailableValue, var2string


def expand_text(raw, scope, origin, item_attributes=False, value_transform=None,
                preserve_missing=False):
    """Scalar/string substitution and the bounded backslash quote modifier.

    item_attributes admits the characterized non-rc-style dependency path:
    attribute text survives substitution for the separate bounded item reader.
    Other callers retain the previous explicit attribute gate. value_transform
    supplies a handler-specific scalar/item spelling after scope lookup, without
    duplicating the reference reader or admitting deferred/modifier expansion.
    preserve_missing is the bounded Commands.expand(skip_errors=1) mode used
    only by buildcheck preparation: valid absent references keep their exact
    spelling. It never suppresses unavailable values or arbitrary exceptions.
    """
    result = []
    index = 0
    while index < len(raw):
        if raw[index] != '$':
            result.append(raw[index])
            index += 1
            continue
        reference_start = index
        index += 1
        if index == len(raw):
            raise SemanticError(origin, 'dangling dollar in expansion')
        char = raw[index]
        if char in '$#':
            result.append(char)
            index += 1
            continue
        if char == '(' and index + 2 < len(raw) and raw[index + 2] == ')':
            result.append(raw[index + 1])
            index += 3
            continue
        optional = char == '?'
        if optional:
            index += 1
        if index == len(raw):
            raise SemanticError(origin, 'missing expansion name')
        char = raw[index]
        backslash_quote = char == '\\'
        if backslash_quote:
            index += 1
            if index == len(raw):
                if preserve_missing:
                    result.append(raw[reference_start:index])
                    continue
                raise SemanticError(origin, 'invalid expansion name')
            char = raw[index]
        if char in '-+*/=\'"\\!':
            raise Unsupported(origin, 'expansion modifiers are not implemented')
        closing = ')' if char == '(' else '}' if char == '{' else None
        if closing:
            index += 1
            while index < len(raw) and raw[index] in ' \t':
                index += 1
        start = index
        while index < len(raw) and raw[index] in string.ascii_letters + string.digits + '_.':
            index += 1
        if index > start and raw[index - 1] == '.':
            index -= 1
        name = raw[start:index]
        if not name:
            if preserve_missing:
                raise Unsupported(origin, 'malformed-reference recovery is deferred')
            raise SemanticError(origin, 'invalid expansion name')
        if closing:
            while index < len(raw) and raw[index] in ' \t':
                index += 1
            if index < len(raw) and raw[index] == '[':
                raise Unsupported(origin, 'A-A-P item indexing is deferred')
            if index == len(raw) or raw[index] != closing:
                if preserve_missing:
                    raise Unsupported(origin, 'malformed-reference recovery is deferred')
                raise SemanticError(origin, 'unclosed expansion reference')
            index += 1
        # get_attrdict has separate consumption/expansion rules. Do not silently
        # approximate attributes following an expanded variable.
        tail = raw[index:].lstrip(' \t')
        if tail.startswith('{') and not item_attributes:
            raise Unsupported(origin, 'attributes attached to expansions are deferred')
        try:
            namespace, variable = scope.target(name, origin)
            value = namespace.get(variable)
        except Unsupported:
            # Scope.target raises Unsupported for an absent namespace. Check
            # that exact condition; do not turn other semantic gates into text.
            parts = name.split('.')
            absent_scope = len(parts) == 2 and parts[0] not in scope.namespaces
            if not optional and not (preserve_missing and absent_scope):
                raise
            value = MISSING
        if value is MISSING:
            if not optional:
                if preserve_missing:
                    result.append(raw[reference_start:index])
                    continue
                raise UndefinedName(origin, 'undefined A-A-P variable: ' + name)
            continue
        if isinstance(value, DeferredExpansion):
            raise Unsupported(origin, 'recursive/deferred dollar expansion is not implemented')
        if isinstance(value, UnavailableValue):
            raise Unsupported(origin, value.reason)
        if type(value) not in (str, int, bool):
            raise Unsupported(origin, 'A-A-P expansion requires a string or integer: ' + name)
        text = str(value)
        if backslash_quote:
            text = _backslash_quoted_value(text, origin)
        else:
            text = value_transform(text, origin) if value_transform else text
        result.append(text)
    return ''.join(result)


def _backslash_quoted_value(value, origin):
    """Util.get_var_val -> Dictlist.dictlist2str(quote_bs), attr=0 subset.

    A-A-P first splits the variable value into items. Each item is then
    escaped; whitespace *between* items remains a separator.
    """
    from .command_items import items
    try:
        parsed = items(value, origin, label='backslash quoted value')
    except Unsupported:
        raise
    except SemanticError:
        # get_var_val returns the original value on Dictlist.UserError when
        # its Expand object has skip_errors set, as buildcheck does.
        return value
    if any(attrs for name, attrs in parsed):
        raise Unsupported(origin, 'backslash quoting with item attributes is deferred')
    return ' '.join(''.join('\\' + char if char == '\\' or char in " \t'\"" else char
                            for char in name) for name, attrs in parsed)


def expression_string(value, origin):
    """RecPython.expr2str spelling, without injecting executable Python text."""
    text = var2string(value, origin)
    return (text.replace('$', '$$').replace('#', '$#').replace('>', '$(gt)')
            .replace('<', '$(lt)').replace('|', '$(bar)'))


def render_value(value, python):
    """Interpolate structural backticks, then join getarg pieces as strings.

    Dollar expansion is deliberately a separate subsequent operation.
    """
    if isinstance(value, m.LiteralValue):
        return value.value
    result = ''
    have_source = False
    literal_tail = ''
    for piece in value.pieces:
        if not any(isinstance(part, m.PythonFragment) or part.value for part in piece):
            continue
        text = ''.join(part.value if isinstance(part, m.LiteralValue) else
                       expression_string(python.expression(part), part)
                       for part in piece)
        # get_func_args decides joins while constructing expression source,
        # before backticks have produced values. An empty expression result
        # still participates; an expression-produced $br is not a join marker.
        if have_source:
            if literal_tail.endswith('$br'):
                count = 0
                index = len(literal_tail) - 4
                while index >= 0 and literal_tail[index] == '$':
                    count += 1
                    index -= 1
                result = result[:-3] + '\n' if count % 2 == 0 else result + ' '
            else:
                result += ' '
        result += text
        have_source = True
        literal_tail = piece[-1].value if isinstance(piece[-1], m.LiteralValue) else ''
    return result
''')

_MANIFEST['aap_semantics.fetch'] = ('tests/src/aap_semantics/fetch.py', False, '866395638014995bb93513aa68e17713c680cae2c54703eef5f4d4f14318d51e')
_EMBEDDED['aap_semantics.fetch'] = ('tests/src/aap_semantics/fetch.py', False, r'''"""Bounded port fetch request and injected acquisition capability.

The port helper constructs ordered locations. A backend performs byte I/O and
reports each attempt; checksum verification remains a later build stage.
"""
from .model import Node


class FetchRequest(Node):
    def __init__(self, origin, destination, candidates, cwd, filename, sites):
        super(FetchRequest, self).__init__(origin)
        self.destination = destination
        self.candidates = tuple(candidates)
        self.cwd = cwd
        self.filename = filename
        self.sites = sites


class FetchAttempt(object):
    def __init__(self, candidate, status, detail='', count=None, sha256=None):
        self.candidate = candidate
        self.status = status
        self.detail = detail
        self.count = count
        self.sha256 = sha256


class FetchResult(object):
    def __init__(self, status, attempts, selected=None, detail=''):
        self.status = status
        self.attempts = tuple(attempts)
        self.selected = selected
        self.detail = detail


class FetchBackend(object):
    def fetch(self, request):
        raise NotImplementedError('port acquisition capability unavailable')


class MemoryFetchBackend(FetchBackend):
    """Network-free fake sharing exact byte files with MemoryArtifacts."""
    def __init__(self, files, sources=None):
        self.files = files
        self.sources = dict(sources or {})
        self.requests = []
        self.directories = []

    def fetch(self, request):
        self.requests.append(request)
        parent = request.destination.rsplit('/', 1)[0]
        self.directories.append(parent)
        attempts = []
        for candidate in request.candidates:
            value = self.sources.get(candidate)
            if type(value) is bytes:
                self.files[request.destination] = value
                attempt = FetchAttempt(candidate, 'COMPLETED', count=len(value))
                attempts.append(attempt)
                return FetchResult('COMPLETED', attempts, candidate)
            detail = str(value) if isinstance(value, Exception) else 'source unavailable'
            attempts.append(FetchAttempt(candidate, 'FAILED', detail))
        return FetchResult('FAILED', attempts, detail='all fetch candidates failed')
''')

_MANIFEST['aap_semantics.graph'] = ('tests/src/aap_semantics/graph.py', False, 'ac7b22c9c79159c60c49439171760d5e3ea23df4c3a22069237489bdccf2db4a')
_EMBEDDED['aap_semantics.graph'] = ('tests/src/aap_semantics/graph.py', False, r'''"""Ordered dependency registration; no file probes, traversal or execution."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .dependency_items import dependency_fields


# Global.virtual_targets: this exact list, not filename shape or do-* spelling,
# also controls whether multiple body-bearing declarations are permitted.
STANDARD_TARGETS = frozenset(('add', 'all', 'build', 'check', 'checkin',
    'checkout', 'clean', 'cleanmore', 'cleanALL', 'commit', 'distclean',
    'extract', 'fetch', 'finally', 'install', 'patch', 'publish', 'reference',
    'remove', 'revise', 'test', 'tryout', 'unlock', 'update'))


class TargetNode(Node):
    def __init__(self, item, path, cwd, index):
        super(TargetNode, self).__init__(item)
        self.name = item.name
        self.path = path
        self.cwd = cwd
        self.index = index
        self.attributes = {'virtual': 1} if self.name in STANDARD_TARGETS else {}
        self.definitions = []

    @property
    def virtual(self):
        return bool(self.attributes.get('virtual'))

    @property
    def identity(self):
        return self.name if self.virtual else self.path

    @property
    def body_definitions(self):
        return tuple(d for d in self.definitions if d.body is not None)


class DependencyDefinition(Node):
    def __init__(self, declaration, targets, sources, attributes, scope, cwd, index):
        super(DependencyDefinition, self).__init__(declaration)
        self.target_items = targets
        self.source_items = sources
        self.build_attributes = dict(attributes)
        self.body = declaration.body
        self.scope = scope
        self.cwd = cwd
        self.index = index

    @property
    def targets(self):
        return tuple(item.node for item in self.target_items)

    @property
    def prerequisites(self):
        return tuple(item.node for item in self.source_items)


class BuildGraph(object):
    def __init__(self):
        self.definitions = []
        self.nodes = []
        self._paths = {}
        self._names = {}
        self.base_directory = None

    @property
    def targets(self):
        return tuple(node for node in self.nodes if node.definitions)

    def find_node(self, name, cwd=None):
        cwd = cwd if cwd is not None else self.base_directory
        if cwd is None and not posixpath.isabs(name):
            raise ValueError('relative graph lookup requires a directory')
        path = posixpath.normpath(name if posixpath.isabs(name) else posixpath.join(cwd, name))
        node = self._paths.get(path)
        if node is not None:
            return node
        return self.find_virtual_node(name)

    def find_virtual_node(self, name):
        """Name-only lookup used by historical default/special target selection."""
        node = self._names.get(name)
        return node if node is not None and node.virtual else None

    def dependencies_for(self, name, cwd=None):
        node = self.find_node(name, cwd)
        return tuple(node.definitions) if node is not None else ()

    def definition_history(self, name, cwd=None):
        return self.dependencies_for(name, cwd)

    def register(self, declaration, scope, cwd):
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(declaration, 'dependency registration requires an absolute recipe directory')
        targets, sources, attrs = dependency_fields(declaration, scope)
        definition = DependencyDefinition(declaration, targets, sources, attrs,
                                          scope, cwd, len(self.definitions))
        # Stage all node/attribute changes, so an invalid redefinition does not
        # leave a half-registered graph. Successful ordering matches upstream.
        paths = dict(self._paths)
        names = dict(self._names)
        pending = []
        virtual = {}
        for item in targets + sources:
            path = posixpath.normpath(posixpath.join(cwd, item.name))
            node = paths.get(path)
            if node is None:
                candidate = names.get(item.name)
                if candidate is not None and virtual.get(candidate, candidate.virtual):
                    node = candidate
            if node is None:
                node = TargetNode(item, path, cwd, len(self.nodes) + len(pending))
                pending.append(node)
                paths[path] = node
                names[item.name] = node
            item.node = node
            virtual[node] = virtual.get(node, node.virtual) or bool(item.attributes.get('virtual'))
        body_seen = set()
        if definition.body is not None:
            for item in targets:
                node = item.node
                if node.name not in STANDARD_TARGETS and (node.body_definitions or node in body_seen):
                    raise SemanticError(declaration, 'multiple build bodies for target: ' + item.name)
                body_seen.add(node)
        for item in targets + sources:
            value = item.attributes.get('virtual')
            if value:
                item.node.attributes['virtual'] = value
        self._paths, self._names = paths, names
        self.nodes.extend(pending)
        self.definitions.append(definition)
        if self.base_directory is None:
            self.base_directory = cwd
        for item in targets:
            item.node.definitions.append(definition)
        return definition

    def snapshot(self):
        """Stable, value-only declaration-order inspection, without traversal."""
        return tuple((d.index, tuple(n.identity for n in d.targets),
                      tuple(n.identity for n in d.prerequisites),
                      d.span.source_id, d.span.start.line, d.body is not None)
                     for d in self.definitions)
''')

_MANIFEST['aap_semantics.helpers'] = ('tests/src/aap_semantics/helpers.py', False, '4122f94ee1daad68f7444f06c24530a58bbe4f8b037ea68bb86f85961d28c76b')
_EMBEDDED['aap_semantics.helpers'] = ('tests/src/aap_semantics/helpers.py', False, r'''"""Closed call registry; recipes never obtain a host callable or module."""
import posixpath
import re as _re

from .diagnostics import SemanticError, Unsupported
from .values import RegexMatchValue, check_value, var2list, var2string
from .directories import ExecutionDirectoryState
from .path_observation import PathObservationRuntime


def _string(value, origin):
    if type(value) is not str:
        raise SemanticError(origin, 'helper requires a string')
    return value


class HelperRegistry(object):
    NAMES = frozenset(('var2string', 'var2list', 'string.find', 'int',
                       'os.path.dirname', 'os.path.basename', 'os.path.abspath',
                       're.sub', 're.search', 'os.path.exists'))
    METHODS = frozenset(('replace', 'join', 'extend'))
    DEFERRED = frozenset(('os.path.isdir',
                          'file2string', 'redir_system', 'os.rename'))

    def __init__(self, cwd=None, path_observer=None):
        # Path arithmetic uses the logical frame; observations use an injected capability.
        self.directory = ExecutionDirectoryState(cwd)
        self.paths = PathObservationRuntime(path_observer)

    @property
    def cwd(self):
        return self.directory.current

    @cwd.setter
    def cwd(self, value):
        self.directory.current = value

    def call(self, name, args, origin):
        if name not in self.NAMES:
            reason = 'deferred capability: ' if name in self.DEFERRED else 'unapproved call: '
            raise Unsupported(origin, reason + name)
        for arg in args:
            check_value(arg, origin)
        if name in ('var2string', 'var2list', 'int'):
            self.arity(args, 1, 1, origin)
            if name == 'var2string':
                return var2string(args[0], origin)
            if name == 'var2list':
                return var2list(args[0], origin)
            value = args[0]
            if type(value) is str:
                value = value.strip(' \t\r\n\v\f')
                digits = value[1:] if value[:1] in ('+', '-') else value
                if not digits or any(c not in '0123456789' for c in digits):
                    raise SemanticError(origin, 'int requires ASCII decimal text')
            elif type(value) not in (int, bool):
                raise SemanticError(origin, 'int requires a string or integer')
            return int(value)
        if name == 'string.find':
            self.arity(args, 2, 4, origin)
            text, sub = _string(args[0], origin), _string(args[1], origin)
            if any(type(arg) is not int for arg in args[2:]):
                raise SemanticError(origin, 'find indices must be integers')
            return text.find(sub, *args[2:])
        if name == 're.search':
            self.arity(args, 2, 2, origin)
            pattern, text = (_string(args[0], origin), _string(args[1], origin))
            try:
                match = _re.search(pattern, text)
            except _re.error as error:
                raise SemanticError(origin, 'invalid regular expression: ' + str(error))
            return RegexMatchValue() if match is not None else None
        if name == 'os.path.exists':
            self.arity(args, 1, 1, origin)
            return self.paths.exists(args[0], self.cwd, origin)
        if name.startswith('os.path.'):
            self.arity(args, 1, 1, origin)
            path = _string(args[0], origin)
            if name == 'os.path.dirname':
                return posixpath.dirname(path)
            if name == 'os.path.basename':
                return posixpath.basename(path)
            if not posixpath.isabs(path):
                if type(self.cwd) is not str or not posixpath.isabs(self.cwd):
                    raise Unsupported(origin, 'abspath needs an explicit absolute metadata cwd')
                path = posixpath.join(self.cwd, path)
            return posixpath.normpath(path)
        self.arity(args, 3, 3, origin)
        pattern, replacement, text = [_string(arg, origin) for arg in args]
        # Only the literal-pattern, literal-replacement subset is established
        # here (globals uses re.sub('-', '_', version)). No host regex dialect.
        if (not pattern or any(c in '.^$*+?{}[]\\|()' for c in pattern)
                or '\\' in replacement):
            raise Unsupported(origin, 're.sub supports literal patterns/replacements only')
        return text.replace(pattern, replacement)

    def method(self, receiver, name, args, origin):
        check_value(receiver, origin)
        for arg in args:
            check_value(arg, origin)
        if type(receiver) is str and name == 'replace':
            self.arity(args, 2, 3, origin)
            old, new = _string(args[0], origin), _string(args[1], origin)
            if len(args) == 3:
                if type(args[2]) is not int:
                    raise SemanticError(origin, 'replace count must be an integer')
                return receiver.replace(old, new, args[2])
            return receiver.replace(old, new)
        if type(receiver) is str and name == 'join':
            self.arity(args, 1, 1, origin)
            if type(args[0]) not in (list, tuple):
                raise SemanticError(origin, 'join requires a list or tuple of strings')
            return receiver.join(_string(item, origin) for item in args[0])
        if type(receiver) is list and name == 'extend':
            self.arity(args, 1, 1, origin)
            if type(args[0]) not in (list, tuple):
                raise SemanticError(origin, 'extend requires a list or tuple')
            pending = list(args[0])
            while pending:
                value = pending.pop()
                if value is receiver:
                    raise Unsupported(origin, 'extend would create a cyclic metadata value')
                if type(value) in (list, tuple):
                    pending.extend(value)
            receiver.extend(args[0])
            return None
        raise Unsupported(origin, 'unapproved method for value type: ' + name)

    @staticmethod
    def arity(args, minimum, maximum, origin):
        if not minimum <= len(args) <= maximum:
            raise SemanticError(origin, 'wrong number of compatibility helper arguments')
''')

_MANIFEST['aap_semantics.includes'] = ('tests/src/aap_semantics/includes.py', False, '331ffc71344e8a237e7017b35ce4cfd1b858e2d1f2275f6ff0ca59d6ba834352')
_EMBEDDED['aap_semantics.includes'] = ('tests/src/aap_semantics/includes.py', False, r'''"""Read-only source loading and POSIX recipe-directory resolution."""
import posixpath

from aap_frontend import Source
from .diagnostics import Unsupported
from .model import Node


class SourceLoader(object):
    """Explicit opt-in to local reads. Encoding is a caller policy, not guessed."""
    def __init__(self, encoding='utf-8'):
        self.encoding = encoding

    def load(self, path):
        return Source.from_path(path, self.encoding)


class IncludeRecord(Node):
    def __init__(self, command, path, program=None, skipped_active=False):
        super(IncludeRecord, self).__init__(command)
        self.path = path
        self.program = program
        self.skipped_active = skipped_active


def resolve_path(path, cwd, origin):
    # dictlist_expand supports wildcards/~; those need a separate capability.
    if (not path or '\x00' in path or any(c in path for c in '*?[')
            or path.startswith('~') or '://' in path):
        raise Unsupported(origin, 'include requires a literal local path after expansion')
    if not posixpath.isabs(path):
        if cwd is None or not posixpath.isabs(cwd):
            raise Unsupported(origin, 'relative include requires an absolute recipe directory')
        path = posixpath.join(cwd, path)
    return posixpath.normpath(path)
''')

_MANIFEST['aap_semantics.lowering'] = ('tests/src/aap_semantics/lowering.py', False, 'c77037c0947aafbd34c6ad45ddacb3021ac0b543110d8c53c1bef373e0557418')
_EMBEDDED['aap_semantics.lowering'] = ('tests/src/aap_semantics/lowering.py', False, r'''"""CST -> semantic structure, without value lookup or Python execution."""
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
''')

_MANIFEST['aap_semantics.model'] = ('tests/src/aap_semantics/model.py', False, '7ff3b982957d07687e872d63b3cf00a236489b9a90d83b85c58c124dc3a3d2ff')
_EMBEDDED['aap_semantics.model'] = ('tests/src/aap_semantics/model.py', False, r'''"""Semantic nodes. Source spelling belongs to their immutable-source CST refs."""


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
''')

_MANIFEST['aap_semantics.move_runtime'] = ('tests/src/aap_semantics/move_runtime.py', False, 'b37611e966fe9c001df90e9717e3cb9f17d2d663c4b361d4933d03e28b83cc74')
_EMBEDDED['aap_semantics.move_runtime'] = ('tests/src/aap_semantics/move_runtime.py', False, r'''"""Bounded local regular-file :move for the reached doperlmod form.

CopyMove.remote_copy_move first tries os.rename for a local move. Semantic
code records that one-file rename request through an injected backend; it does
not call a host filesystem or infer output from a preceding shell command.
"""
import posixpath

from .model import Node
from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text


class MoveRequest(Node):
    def __init__(self, origin, source, destination, cwd):
        super(MoveRequest, self).__init__(origin)
        self.source_argument, self.destination_argument = source, destination
        self.cwd = cwd
        self.source = source if posixpath.isabs(source) else posixpath.join(cwd, source)
        self.destination = (destination if posixpath.isabs(destination)
                            else posixpath.join(cwd, destination))


class MoveResult(object):
    def __init__(self, status='COMPLETED', detail=None):
        self.status, self.detail = status, detail


class MoveBackend(object):
    def move(self, request):
        return MoveResult('UNAVAILABLE', 'move mutation capability unavailable')


class MemoryMoveBackend(MoveBackend):
    """Controlled regular-byte-file rename store.

    A successful request replaces an existing destination and removes its
    source in one recorded effect, matching the normal local os.rename path.
    Failures are reported before mutation, so earlier recipe effects remain
    visible without inventing the historical copy/delete fallback.
    """
    def __init__(self, files=None, failures=None):
        self.files = files if files is not None else {}
        self.failures = dict(failures or {})
        self.requests = []

    def move(self, request):
        self.requests.append(request)
        failure = self.failures.get((request.source, request.destination))
        if failure is not None:
            return failure
        if type(self.files.get(request.source)) is not bytes:
            return MoveResult('FAILED', 'move source does not exist: ' + request.source)
        self.files[request.destination] = self.files.pop(request.source)
        return MoveResult()


class MoveRecord(Node):
    def __init__(self, request):
        super(MoveRecord, self).__init__(request)
        self.request, self.result = request, None
        self.status, self.error = 'PENDING', None


class MoveRuntime(object):
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else MoveBackend()

    def execute(self, node, scope, python, cwd, records):
        raw = render_value(node.arguments, python)
        if raw.lstrip(' \t').startswith('{'):
            raise Unsupported(node, ':move attributes/options are deferred')
        expanded = expand_text(raw, scope, node, item_attributes=True)
        parsed = items(expanded, node, label='move')
        if len(parsed) != 2 or any(attrs for name, attrs in parsed):
            raise Unsupported(node, 'only one source and one destination are supported for :move')
        source, destination = parsed[0][0], parsed[1][0]
        if (not source or not destination or '\x00' in source or '\x00' in destination):
            raise SemanticError(node, ':move paths must be nonempty and NUL-free')
        if any(char in source + destination for char in '~*?[]{}') or ':' in source + destination:
            raise Unsupported(node, ':move glob, user-directory, URL and attribute paths are deferred')
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(node, ':move requires an explicit absolute cwd')
        request = MoveRequest(node, source, destination, cwd)
        record = MoveRecord(request)
        records.append(record)
        try:
            outcome = self.backend.move(request)
        except NotImplementedError as error:
            outcome = MoveResult('UNAVAILABLE', str(error))
        except Exception as error:
            outcome = MoveResult('FAILED', str(error))
        record.result = outcome
        if not isinstance(outcome, MoveResult) or outcome.status not in (
                'COMPLETED', 'UNAVAILABLE', 'FAILED'):
            record.status = 'FAILED'
            raise SemanticError(node, 'invalid move mutation result')
        if outcome.status == 'UNAVAILABLE':
            record.status = 'BLOCKED'
            record.error = Unsupported(node, outcome.detail or 'move mutation unavailable')
            raise record.error
        if outcome.status == 'FAILED':
            record.status = 'FAILED'
            record.error = SemanticError(node, 'move failed: ' + str(outcome.detail))
            raise record.error
        record.status = 'COMPLETED'
''')

_MANIFEST['aap_semantics.nested_update'] = ('tests/src/aap_semantics/nested_update.py', False, '067fb81ad5b905811b91bb25364bcd72beb39c93702346fb7c931fd244791ac0')
_EMBEDDED['aap_semantics.nested_update'] = ('tests/src/aap_semantics/nested_update.py', False, r'''"""Synchronous, source-backed target updates; no execution capability of its own."""
from .model import Node
from .dependency_items import parse_items
from .expansion import render_value, expand_text
from .diagnostics import SemanticError, Unsupported


class UpdateRequest(Node):
    def __init__(self, command, scope, python, cwd, context=None):
        super(UpdateRequest, self).__init__(command)
        self.command = command
        self.scope, self.cwd, self.context = scope, cwd, context
        if command.body is not None:
            raise Unsupported(command, ':update bodies are deferred')
        self.raw = render_value(command.arguments, python)
        if self.raw.lstrip().startswith('{'):
            raise Unsupported(command, ':update options are deferred')
        self.expanded = expand_text(self.raw, scope, command)
        items = parse_items(self.expanded, command)
        if any(item.attributes for item in items):
            raise Unsupported(command, ':update item attributes are deferred')
        if not items:
            raise SemanticError(command, 'missing argument for :update')
        self.targets = tuple(item.name for item in items)


class UpdateResult(object):
    def __init__(self, request):
        self.request = request
        self.builds = []
        self.status = 'COMPLETE'
        self.reason = 'requested_targets_complete'
        self.error = None
        self.blocked_at = None
        self.span = request.span
        self.target = None


class UpdateStopped(Exception):
    """Internal structured unwind, never exposed as a recipe Python exception."""
    def __init__(self, result):
        super(UpdateStopped, self).__init__(result.reason)
        self.result = result
''')

_MANIFEST['aap_semantics.output'] = ('tests/src/aap_semantics/output.py', False, 'be546979eff51b0d2135f64d1c017f13f769ce94370f3c21abe41cac32ad3761')
_EMBEDDED['aap_semantics.output'] = ('tests/src/aap_semantics/output.py', False, r'''"""A-A-P print events and bounded local redirected output; no host I/O."""
import posixpath

from .model import Node
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text


def token(text, index):
    """Util.get_token: horizontal whitespace or a quote-preserving token."""
    start = index
    if text[index] in ' \t':
        while index < len(text) and text[index] in ' \t':
            index += 1
    else:
        quote = None
        while index < len(text):
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char in ' \t':
                break
            index += 1
    return text[start:index], index


def print_parts(raw, origin, label='print'):
    """Commands._get_redir token ordering, before dollar expansion.

    Returns unexpanded message, filename and mode. No shell interpretation.
    """
    index, message, filename, mode = 0, '', None, 'stdout'
    while index < len(raw):
        part, index = token(raw, index)
        if index == len(raw) and part[0] in ' \t':
            break
        if not message or part[0] in ' \t':
            if not message:
                nextpart, part = part, ''
            else:
                nextpart, index = token(raw, index)
            if nextpart.startswith('>'):
                if mode != 'stdout':
                    raise SemanticError(origin, 'redirection appears twice')
                prefix = nextpart[:2] if nextpart.startswith(('>!', '>>')) else '>'
                mode = {'>!': 'overwrite', '>>': 'append', '>': 'create'}[prefix]
                filename = nextpart[len(prefix):]
                if not filename:
                    if index < len(raw):
                        unused, index = token(raw, index)
                    if index == len(raw):
                        raise SemanticError(origin, 'missing filename after ' + prefix)
                    filename, index = token(raw, index)
                if not message and index < len(raw):
                    unused, index = token(raw, index)
            elif nextpart.startswith('|'):
                raise Unsupported(origin, 'A-A-P ' + label + ' pipelines are deferred')
            else:
                message += part + nextpart
        else:
            message += part
    return message, filename, mode


def destination(raw, scope, origin, cwd, details=False, label='print'):
    value = expand_text(raw, scope, origin)
    # Util.unquote, without shell escapes or splitting expansion-created spaces.
    path, quote = '', None
    for char in value:
        if quote == char:
            quote = None
        elif not quote and char in "'\"":
            quote = char
        else:
            path += char
    if quote:
        raise Unsupported(origin, 'unmatched destination quoting is deferred')
    if not path or '\x00' in path:
        raise SemanticError(origin, label + ' destination must be nonempty and NUL-free')
    if any(c in path for c in '~*?[]{}') or ':' in path:
        raise Unsupported(origin, label + ' destination glob/tilde/URL/attribute forms are deferred')
    expanded = path
    if not posixpath.isabs(path):
        if type(cwd) is not str or not posixpath.isabs(cwd):
            raise Unsupported(origin, 'redirected ' + label + ' requires an explicit absolute cwd')
        path = posixpath.join(cwd, path)
    return (path, expanded) if details else path


class OutputPolicy(object):
    def __init__(self, encoding, logging=None):
        self.encoding, self.logging = encoding, logging

    def encode(self, text, origin):
        try:
            return text.encode(self.encoding, 'strict')
        except (UnicodeError, LookupError, TypeError) as error:
            raise SemanticError(origin, 'output encoding error: ' + str(error))


class PrintRequest(Node):
    def __init__(self, origin, raw, text, mode, path, cwd, policy):
        super(PrintRequest, self).__init__(origin)
        self.raw, self.text, self.destination = raw, text, mode
        self.path, self.cwd = path, cwd
        self.output_text = text + '\n' if mode == 'stdout' or not text.endswith('\n') else text
        self.data = policy.encode(self.output_text, origin)
        self.encoding = policy.encoding
        self.path_bytes = policy.encode(path, origin) if path is not None else None
        if self.path_bytes is not None and b'\x00' in self.path_bytes:
            raise Unsupported(origin, 'output destination encoding contains NUL')
        # msg_print always prints, independently of MESSAGE quiet settings.
        self.log_type = 'print' if mode == 'stdout' else None
        self.log_text = text if mode == 'stdout' else None


class OutputResult(object):
    def __init__(self, status='COMPLETED', detail=None):
        self.status, self.detail = status, detail


class PrintRecord(Node):
    def __init__(self, request):
        super(PrintRecord, self).__init__(request)
        self.request = request
        self.status, self.error = 'PENDING', None


class MemoryOutputSink(object):
    """Capture terminal events; never use the embedding process's stdout."""
    def __init__(self):
        self.events = []

    def emit(self, request):
        self.events.append(request)
        return OutputResult()


class TextWriter(object):
    def open_bytes(self, request):
        """Open/truncate/create before cat reads. Return write(bytes)/close session.

        Missing capability raises NotImplementedError before effects. Actual I/O
        failures raise OSError; no rollback of earlier writes is promised.
        """
        raise NotImplementedError('byte output sessions unavailable')

    def write(self, request):
        raise NotImplementedError('print file writer unavailable')


class MemoryTextWriter(TextWriter):
    """Explicit writable-directory fixture, no process-inferred paths/effects.

    This fixture models no symlinks. Production adapters must honor exact path
    spelling and opening errors; no local adapter is enabled here.
    """
    def __init__(self, directories=(), files=None):
        self.directories = set(directories)
        self.files = dict(files or {})
        self.requests = []

    def open_bytes(self, request):
        self.requests.append(request)
        path = posixpath.normpath(request.path)
        if posixpath.dirname(path) not in self.directories or path in self.directories:
            raise OSError('output parent unavailable or destination is a directory: ' + path)
        if request.destination not in ('overwrite', 'append'):
            raise ValueError('unsupported byte write mode')
        if request.destination == 'overwrite' or path not in self.files:
            self.files[path] = b''
        return MemoryByteSession(self.files, path)

    def write(self, request):
        self.requests.append(request)
        path = posixpath.normpath(request.path)
        if posixpath.dirname(path) not in self.directories or path in self.directories:
            raise OSError('output parent unavailable or destination is a directory: ' + path)
        if request.destination == 'overwrite':
            self.files[path] = request.data
        elif request.destination == 'append':
            self.files[path] = self.files.get(path, b'') + request.data
        else:
            raise ValueError('unsupported print write mode')
        return OutputResult()


class MemoryByteSession(object):
    def __init__(self, files, path):
        self.files, self.path = files, path
        self.closed = False

    def write(self, data):
        if self.closed or type(data) is not bytes:
            raise OSError('invalid byte session write')
        self.files[self.path] += data

    def close(self):
        self.closed = True


class PrintRuntime(object):
    def __init__(self, policy, sink=None, writer=None):
        self.policy = policy
        self.sink = sink if sink is not None else MemoryOutputSink()
        self.writer = writer if writer is not None else TextWriter()

    def execute(self, node, scope, python, cwd, records):
        raw = render_value(node.arguments, python)
        message, filename, mode = print_parts(raw, node)
        if mode == 'create':
            raise Unsupported(node, 'non-clobber print redirection is deferred')
        if mode == 'stdout' and self.policy.logging is not False:
            raise Unsupported(node, 'ordinary print requires explicit unlogged output policy')
        path = destination(filename, scope, node, cwd) if filename is not None else None
        text = expand_text(message, scope, node)
        request = PrintRequest(node, raw, text, mode, path, cwd, self.policy)
        record = PrintRecord(request)
        records.append(record)
        try:
            outcome = self.sink.emit(request) if mode == 'stdout' else self.writer.write(request)
            if not isinstance(outcome, OutputResult) or outcome.status not in ('COMPLETED', 'BLOCKED', 'FAILED'):
                raise ValueError('invalid output capability result')
            if outcome.status == 'BLOCKED':
                raise NotImplementedError(outcome.detail or 'output capability unavailable')
            if outcome.status == 'FAILED':
                raise OSError(outcome.detail or 'output capability failed')
        except NotImplementedError as error:
            record.status = 'BLOCKED'
            record.error = Unsupported(node, str(error))
            raise record.error
        except Exception as error:
            record.status = 'FAILED'
            record.error = SemanticError(node, 'print output failed: ' + str(error))
            raise record.error
        record.status = 'COMPLETED'
''')

_MANIFEST['aap_semantics.path_observation'] = ('tests/src/aap_semantics/path_observation.py', False, '495f73e8b2f49060ee541d396f663ad30df8471bd1119dc55c3565184c29c5ba')
_EMBEDDED['aap_semantics.path_observation'] = ('tests/src/aap_semantics/path_observation.py', False, r'''"""Read-only stat-success observations. No host filesystem adapter is installed."""
import posixpath

from .diagnostics import SemanticError, Unsupported


class PathRequest(object):
    def __init__(self, path, cwd, origin):
        self.argument, self.cwd = path, cwd
        self.source, self.span = origin.source, origin.span
        # Keep dot segments, trailing slashes and symlink-sensitive spelling.
        self.path = path if not path or posixpath.isabs(path) else posixpath.join(cwd, path)


class PathObservation(object):
    """MISSING means stat failed, including EACCES or a dangling symlink.

    ERROR means the capability itself failed, not an observed stat failure.
    An adapter must classify these explicitly; an arbitrary exception is never
    taken as evidence that a recipe path does not exist.
    """
    def __init__(self, status, detail=None):
        self.status, self.detail = status, detail


class PathObserver(object):
    def observe(self, request):
        return PathObservation('UNAVAILABLE', 'path observation capability unavailable')


class MemoryPathObserver(PathObserver):
    """Explicit observations by exact absolute spelling; unknown is unavailable.

    Fixtures certify stat success/failure, including link-following behavior;
    this is not a filesystem or symlink simulator. No process result populates it.
    """
    def __init__(self, observations=None):
        self.observations = dict(observations or {})
        self.requests = []

    def observe(self, request):
        self.requests.append(request)
        return self.observations.get(request.path, PathObservation('UNAVAILABLE'))


class PathRecord(object):
    def __init__(self, request, observation):
        self.request, self.observation = request, observation


class PathObservationRuntime(object):
    def __init__(self, observer=None):
        self.observer = observer if observer is not None else PathObserver()
        self.records = []

    def exists(self, path, cwd, origin):
        if type(path) is not str:
            raise SemanticError(origin, 'os.path.exists requires a string path')
        if '\x00' in path:
            raise SemanticError(origin, 'os.path.exists path contains NUL')
        if path and not posixpath.isabs(path):
            if type(cwd) is not str or not posixpath.isabs(cwd):
                raise Unsupported(origin, 'path observation requires an explicit absolute cwd')
        request = PathRequest(path, cwd, origin)
        try:
            # stat("") cannot succeed. Do not accidentally join it to cwd.
            observation = (PathObservation('MISSING', 'empty path') if not path
                           else self.observer.observe(request))
        except NotImplementedError as error:
            observation = PathObservation('UNAVAILABLE', str(error))
        except Exception as error:
            observation = PathObservation('ERROR', str(error))
        if (not isinstance(observation, PathObservation)
                or observation.status not in ('EXISTS', 'MISSING', 'UNAVAILABLE', 'ERROR')):
            observation = PathObservation('ERROR', 'invalid path observation result')
        self.records.append(PathRecord(request, observation))
        if observation.status == 'UNAVAILABLE':
            raise Unsupported(origin, 'path observation unavailable: ' + request.path)
        if observation.status == 'ERROR':
            raise SemanticError(origin, 'path observation failed: ' + request.path
                                + ' (' + str(observation.detail) + ')')
        return observation.status == 'EXISTS'
''')

_MANIFEST['aap_semantics.persistence'] = ('tests/src/aap_semantics/persistence.py', False, 'e659e4462e60d5717e537d2a040e5e418326a30aeaab1adf8468c2cb83275dfc')
_EMBEDDED['aap_semantics.persistence'] = ('tests/src/aap_semantics/persistence.py', False, r'''"""Decoded persistent observations, distinct from per-run successful updates.

Sign._sign_upd_sign accumulates changes; Main flushes them even on later error.
No host sign-file serializer or recipe filesystem operation is provided here.
"""
from .target_state import TargetStateBackend
from .buildcheck import BuildSignatureFailure


class PersistenceBackend(object):
    def signature(self, target, source, check):
        """Return a saved string, '' for absence, or None for unavailable."""
        raise NotImplementedError('saved signatures unavailable')

    def marker_exists(self, path):
        raise NotImplementedError('port done-marker observations unavailable')

    def timestamp(self):
        """Explicit timestamp string for Sign.timekey; never host time implicitly."""
        raise NotImplementedError('signature timestamp unavailable')

    def flush(self, records):
        """Replace each supplied target's signatures; retain other targets.

        Called at invocation end, not body return. No port marker is implicit.
        Disk encoding/signfile placement belongs to a future adapter.
        """
        raise NotImplementedError('signature persistence unavailable')


class SignatureRecord(object):
    def __init__(self, target, definition, values, timestamp):
        self.target = target.identity
        self.target_path = target.path
        self.definition = definition
        self.cwd = definition.cwd
        self.span = definition.span
        self.values = dict(values)  # (source identity or '', check) -> string
        self.timestamp = timestamp


class MemoryPersistence(PersistenceBackend):
    def __init__(self):
        self.signatures = {}
        self.last_updates = {}
        self.markers = {}  # Existing done/<stage> bytes; contents are not read.
        self.time = '0'
        self.writes = []
        self.marker_reads = []

    def signature(self, target, source, check):
        return self.signatures.get(target, {}).get((source, check), '')

    def marker_exists(self, path):
        self.marker_reads.append(path)
        return path in self.markers

    def timestamp(self):
        return self.time

    def flush(self, records):
        for record in records:
            if record.values:
                self.signatures[record.target] = dict(record.values)
                self.last_updates[record.target] = record.timestamp
            else:
                self.signatures.pop(record.target, None)
                self.last_updates.pop(record.target, None)
            self.writes.append(record)


class InvocationObservations(TargetStateBackend):
    """Live file observations, per-run signature cache and pending save overlay."""
    def __init__(self, state, persistence, preparer=None):
        self.state = state
        self.persistence = persistence
        self.preparer = preparer
        self.preparation_scope = None
        self.preparations = []
        self.pending = {}
        self.order = []
        self.cache = {}

    def file_state(self, node):
        return self.state.file_state(node)

    def current_signature(self, node, check):
        key = (node.path, check)
        if key not in self.cache:
            # Sign.get_new_sign has no 'newer' implementation; comparison uses
            # time, but sign_updated stores the literal check name afterward.
            value = 'unknown' if check == 'newer' else self.state.current_signature(node, check)
            if value is None or type(value) is str:
                self.cache[key] = value
            else:
                raise ValueError('invalid current signature observation')
        return self.cache[key]

    def invalidate(self, node):
        for key in list(self.cache):
            if key[0] == node.path:
                del self.cache[key]

    def stored_signature(self, target, source, check):
        source_name = source.identity if source is not None else ''
        if target.identity in self.pending:
            return self.pending[target.identity].values.get((source_name, check), '')
        return self.persistence.signature(target.identity, source_name, check)

    def build_signature(self, definition, target):
        observed = self.state.build_signature(definition, target)
        if observed is not None or self.preparer is None:
            return observed
        result = self.preparer.prepare(definition, target,
                                       self.preparation_scope or definition.scope)
        self.preparations.append(result)
        if result.status == 'PREPARED':
            return result.signature
        if result.status == 'FAILED':
            raise BuildSignatureFailure(result)
        return None

    def implicit_dependencies(self, node):
        return self.state.implicit_dependencies(node)

    def matching_rule(self, node):
        return self.state.matching_rule(node)

    def stage(self, records):
        for record in records:
            if record.target not in self.pending:
                self.order.append(record.target)
            self.pending[record.target] = record

    def records(self):
        return tuple(self.pending[name] for name in self.order)
''')

_MANIFEST['aap_semantics.planner'] = ('tests/src/aap_semantics/planner.py', False, 'd1249e691447b7c76aacffbe842c10b2a3c484e1e18f0b04a462666327b9f73e')
_EMBEDDED['aap_semantics.planner'] = ('tests/src/aap_semantics/planner.py', False, r'''"""Explicit-graph update planning. No executor, rule matcher or host I/O.

Plans describe a successful-execution path through the registered graph.
After a possible body effect, file decisions are rechecks, not predictions
based on stale pre-execution observations. See notes/update-planner.md.
"""
import posixpath

from aap_frontend import Source
from .model import Node
from .graph import TargetNode, STANDARD_TARGETS
from .dependency_items import DependencyItem, parse_items
from .diagnostics import SemanticError, Unsupported
from .expansion import expand_text
from .values import MISSING, var2string
from .target_state import FileState
from .buildcheck import BuildSignatureFailure


_AUTOMATIC = frozenset(('fetch', 'publish', 'commit', 'checkout', 'checkin',
                       'unlock', 'add', 'remove', 'tag', 'revise', 'reference'))


class PlanningDiagnostic(SemanticError):
    def __init__(self, origin, code, reason):
        super(PlanningDiagnostic, self).__init__(origin, reason)
        self.code = code


class InvalidStoredSignature(ValueError):
    pass


class BodyPlan(Node):
    def __init__(self, target, definition, mode, reason, index, after):
        super(BodyPlan, self).__init__(definition)
        self.target = target
        self.definition = definition
        self.body = definition.body
        self.scope = definition.scope
        self.cwd = definition.cwd
        self.mode = mode  # update, sections, or conditional
        self.reason = reason
        self.index = index
        self.after = tuple(after)  # successful earlier body steps, graph unchanged
        self.outputs = definition.targets
        self.signature_targets = tuple(n for n in self.outputs
                                       if n.name not in STANDARD_TARGETS)
        self.signature_inputs = tuple(tuple(i for i in d.source_items if not i.node.virtual)
                                      for d in target.definitions)
        self.buildcheck_required = not target.virtual
        self.postcondition = 'trigger_survives_if_preexisting'
        self.active_paths = ()
        self.active_definitions = ()
        self.prerequisite_definitions = ()


class UpdateDecision(Node):
    def __init__(self, target, status, reason, prerequisites, bodies, after):
        super(UpdateDecision, self).__init__(target)
        self.target = target
        self.status = status
        self.reason = reason
        # Each relation retains its definition and item; duplicates survive.
        self.prerequisites = tuple(prerequisites)
        self.bodies = tuple(bodies)
        self.after = tuple(after)
        self.covered_by = None
        self.cwd = target.cwd
        self.virtual = target.virtual


class UpdatePlan(object):
    def __init__(self):
        self.requests = ()
        self.entries = []
        self.bodies = []
        self.states = {}
        self.diagnostic = None
        self.complete = True

    @property
    def requires_recheck(self):
        return any(e.status == 'recheck' for e in self.entries)

    def decision_for(self, identity):
        return next((e for e in self.entries if e.target.identity == identity), None)

    def snapshot(self):
        return tuple((e.target.identity, e.status, e.reason,
                      tuple(b.definition.index for b in e.bodies), e.after)
                     for e in self.entries)


class _Request(object):
    def __init__(self, name):
        self.source = Source('<target request>', name)
        self.span = self.source.span(0, len(name))


def _sections(body):
    # DoBuild.commands_with_sections only checks the first non-comment token.
    # It does not parse/validate the section or any statement in the body.
    for line in body.origin.text.splitlines():
        token = line.lstrip(' \t\r')
        if token and not token.startswith('#'):
            return token.startswith('>')
    return False


class UpdatePlanner(object):
    def __init__(self, graph, state, scope=None, cwd=None, build_rule_targets=()):
        self.graph = graph
        self.state = state
        self.scope = scope
        self.cwd = cwd if cwd is not None else graph.base_directory
        if self.cwd is None or not posixpath.isabs(self.cwd):
            raise ValueError('planning requires an absolute recipe directory')
        # This is supplied by a future build-rule declarator, never inferred
        # from ordinary dependency order.
        self.build_rule_targets = tuple(build_rule_targets)

    def _resolve(self, name, expand=True):
        if type(name) is not str:
            raise ValueError('requested targets must be strings')
        origin = self._request_origin or _Request(name)
        # Command-line names use the existing expansion subset, not eval.
        if expand and '$' in name:
            if self.scope is None:
                raise Unsupported(origin, 'target expansion requires a scope')
            name = expand_text(name, self.scope, origin)
        if not name or any(c in name for c in '\x00\n\r'):
            raise PlanningDiagnostic(origin, 'invalid_target', 'invalid target name')
        if any(c in name for c in '*?[%`{}') or name.startswith('~') or '://' in name:
            raise Unsupported(origin, 'unsupported requested target feature')
        node = self.graph.find_node(name, self.cwd)
        if node is not None:
            return node
        path = posixpath.normpath(posixpath.join(self.cwd, name))
        if path not in self._extra:
            item = DependencyItem(origin, name, {})
            self._extra[path] = TargetNode(item, path, self.cwd,
                                           len(self.graph.nodes) + len(self._extra))
        return self._extra[path]

    def _defaults(self):
        value = self.scope.local.get('TARGET', MISSING) if self.scope else MISSING
        if value is not MISSING and value:
            origin = _Request('$TARGET')
            items = parse_items(var2string(value, origin), origin)
            if any(i.attributes for i in items):
                raise Unsupported(origin, 'default target attributes are deferred')
            return [self._resolve(i.name, False) for i in items]
        node = self.graph.find_virtual_node('all')
        if node is not None:
            return [node]
        return [self._resolve(n, False) for n in self.build_rule_targets]

    def plan(self, requests=None, completed=(), nested=False,
             active_paths=(), active_definitions=(), request_origin=None):
        """None selects defaults; strings/lists are command-line target requests.

        No force, continue, touch, nobuild, recursive or rule options are
        implicitly enabled. Each call owns fresh traversal state.
        """
        self.result = UpdatePlan()
        self._extra = {}
        self._done = {}
        self._active = set(active_definitions)
        self._busy = frozenset(active_paths)
        self._path = []
        self._callers = []
        self._request_origin = request_origin
        self._cover = {}
        self._maybe_cover = {}
        self._sign_cache = {}
        # Only a driver which verified completion may supply these paths.
        # Standalone plans continue to own fresh speculative traversal state.
        self._completed = frozenset(completed)
        nodes = []
        if requests is None:
            nodes = self._defaults()
        else:
            if type(requests) is str:
                requests = [requests]
            for name in requests:
                node = self._resolve(name, expand=not nested)
                update = self.graph.find_virtual_node('update')
                if not nested and name == 'update' and (update is None or not update.definitions):
                    nodes.append(self._resolve('fetch'))
                    nodes.extend(self._defaults())
                else:
                    nodes.append(node)
        self.result.requests = tuple(nodes)
        for node in nodes:
            entry = self._visit(node, not nested)
            if entry.status in ('failed', 'blocked'):
                break
        else:
            # Ordinary fatal errors bypass finally in DoBuild.dobuild.
            final = self.graph.find_virtual_node('finally')
            if final is not None and not nested:
                self._visit(final, False)
        return self.result

    def _finish(self, node, status, reason, relations, bodies=(), after=None):
        if after is None:
            after = range(len(self.result.bodies))
        entry = UpdateDecision(node, status, reason, relations, bodies, after)
        self.result.entries.append(entry)
        self.result.states[node] = status
        self._done[node] = entry
        return entry

    def _problem(self, node, code, message, relations, blocked=False, origin=None):
        if self.result.diagnostic is None:
            self.result.diagnostic = PlanningDiagnostic(origin or node, code, message)
        self.result.complete = False
        return self._finish(node, 'blocked' if blocked else 'failed', code, relations)

    def _file(self, node):
        state = self.state.file_state(node)
        if (not isinstance(state, FileState) or type(state.exists) is not bool
                or type(state.directory) is not bool
                or type(state.mtime) not in (int, float) or state.mtime < 0):
            raise ValueError('invalid file-state observation')
        return state

    def _signature(self, node, check):
        key = (node, check)
        if key not in self._sign_cache:
            value = self.state.current_signature(node, check)
            if value is not None and type(value) is not str:
                raise ValueError('invalid current signature observation')
            self._sign_cache[key] = value
        return self._sign_cache[key]

    def _stored(self, target, source, check):
        value = self.state.stored_signature(target, source, check)
        if value is not None and type(value) is not str:
            raise InvalidStoredSignature('invalid stored signature observation')
        return value

    def _visit(self, node, toplevel):
        if node in self._done:
            return self._done[node]
        relations = []
        if node.path in self._completed:
            return self._finish(node, 'current', 'already_updated', relations)
        if node.path in self._busy:
            return self._problem(node, 'cycle', 'cyclic nested update: ' + node.name,
                                 relations, origin=self._request_origin)
        if node in self._cover:
            step = self._cover[node]
            entry = self._finish(node, 'covered', 'shared_body_success', relations)
            entry.covered_by = step
            return entry
        if node in self._maybe_cover:
            # A conditional shared body may mark this sibling done even when
            # no output file is created. Rechecking only its file would be
            # wrong. Defer the whole visit until the prior update result is
            # known, including whether to traverse its own prerequisite lists.
            entry = self._finish(node, 'recheck', 'shared_body_result', relations)
            entry.covered_by = self._maybe_cover[node]
            return entry
        self.result.states[node] = 'visiting'
        self._path.append(node.path)
        try:
            return self._walk(node, toplevel, relations)
        except BuildSignatureFailure as error:
            return self._problem(node, 'buildcheck_failed', str(error), relations,
                                 origin=error.preparation.definition)
        except InvalidStoredSignature as error:
            return self._problem(node, 'stored_signature_invalid', str(error),
                                 relations, origin=node)
        except (OSError, ValueError, NotImplementedError) as error:
            return self._problem(node, 'observation_unavailable',
                                 str(error) or 'target-state observation unavailable', relations, True)
        finally:
            self._path.pop()

    def _walk(self, node, toplevel, relations):
        if any(key != 'virtual' for key in node.attributes):
            return self._problem(node, 'unsupported_attributes',
                                 'target attributes require later runtime support', relations, True)
        if node.name == 'comment':
            return self._problem(node, 'automatic_target', 'comment output is deferred', relations, True)
        # Update accumulates across *all* declarations before any body runs.
        cause = None
        stamp = 0
        uncertain = False
        for definition in node.definitions:
            srcpath = definition.scope.lookup('SRCPATH')
            if srcpath is not MISSING and srcpath:
                return self._problem(node, 'source_search_path',
                                     'SRCPATH resolution is deferred', relations, True,
                                     origin=definition)
            if any(key != 'virtual' for key in definition.build_attributes):
                return self._problem(node, 'unsupported_attributes',
                                     'build attributes require later runtime support', relations, True,
                                     origin=definition)
            if definition in self._active:
                return self._problem(node, 'cycle', 'cyclic shared dependency', relations,
                                     origin=definition)
            self._active.add(definition)
            try:
                for item in definition.source_items:
                    if any(key != 'virtual' for key in item.attributes):
                        return self._problem(node, 'unsupported_attributes',
                                             'source attributes require later runtime support', relations,
                                             True, origin=item)
                    child = item.node
                    if child is node:
                        relations.append((definition, item, 'self_ignored'))
                        continue
                    if self.result.states.get(child) == 'visiting':
                        return self._problem(node, 'cycle', 'cyclic dependency: ' + child.name,
                                             relations, origin=item)
                    self._callers.append(definition)
                    try:
                        entry = self._visit(child, False)
                    finally:
                        self._callers.pop()
                    relations.append((definition, item, entry))
                    if entry.status in ('failed', 'blocked'):
                        return self._problem(node, 'prerequisite_' + entry.status,
                                             'prerequisite did not update: ' + child.name,
                                             relations, entry.status == 'blocked')
                    if not child.virtual and self.state.implicit_dependencies(child) is not False:
                        return self._problem(node, 'automatic_dependencies',
                                             'automatic dependency discovery is deferred', relations, True)
                    # Preserve Update.outdated's short circuit, including the
                    # first positive newer timestamp (not a generic max-mtime).
                    if node.virtual or cause is not None or stamp:
                        continue
                    if child.virtual:
                        cause = 'virtual_prerequisite'
                        continue
                    if self.result.bodies or entry.status in ('recheck', 'covered'):
                        uncertain = True
                        continue
                    method = definition.scope.lookup('DEFAULTCHECK')
                    if method is MISSING:
                        method = 'md5'  # upstream/default.aap:29
                    state = self._file(child)
                    if state.directory:
                        method = 'none'
                    if method not in ('md5', 'c_md5', 'time', 'newer', 'none'):
                        return self._problem(node, 'unsupported_check',
                                             'unsupported signature method', relations, True)
                    check = 'time' if method == 'newer' else method
                    new = self._signature(child, check)
                    if new is None:
                        return self._problem(node, 'signature_unavailable',
                                             'current prerequisite signature unavailable', relations, True)
                    if new in ('', '0'):
                        return self._problem(node, 'signature_error',
                                             'cannot compute prerequisite signature', relations, origin=item)
                    if method == 'newer':
                        stamp = int(float(new))
                    else:
                        old = self._stored(node, child, check)
                        if old is None:
                            return self._problem(node, 'signature_unavailable',
                                                 'stored signature observation unavailable', relations, True)
                        if old != new:
                            cause = 'signature_changed' if old else 'signature_missing'
            finally:
                self._active.remove(definition)

        definitions = node.body_definitions
        if not definitions:
            state = None
            if not node.virtual and not self.result.bodies:
                state = self._file(node)
                if state.directory:
                    return self._finish(node, 'current', 'directory_exists', relations)
            if self.state.matching_rule(node) is not False:
                return self._problem(node, 'rule_required', 'rule matching is deferred', relations, True)
            if node.name in _AUTOMATIC:
                # Commands.do_fetch_all succeeds without effects when no node
                # even has fetch/commit metadata. Port done markers expose
                # this path by generating a bodyless fetch stage. Never infer
                # success for a nonempty candidate set (may_fetch is deferred).
                if node.name == 'fetch' and not any(
                        'fetch' in item.attributes or 'commit' in item.attributes
                        for item in self.graph.nodes):
                    return self._finish(node, 'current', 'empty_fetch', relations)
                return self._problem(node, 'automatic_target',
                                     'automatic target behavior is deferred: ' + node.name, relations, True)
            if node.name == 'refresh':
                return self._finish(node, 'current', 'refresh_noop', relations)
            if node.virtual:
                if node.definitions:
                    return self._finish(node, 'current', 'virtual_aggregate', relations)
                return self._problem(node, 'no_build_commands',
                                     'virtual target has no dependency or body', relations)
            if self.result.bodies:
                return self._finish(node, 'recheck', 'post_execution_state', relations)
            if toplevel or not state.exists:
                return self._problem(node, 'no_build_commands',
                                     'do not know how to build: ' + node.name, relations)
            return self._finish(node, 'current', 'source_exists', relations)

        after = tuple(range(len(self.result.bodies)))
        bodies = []
        status, reason = 'current', 'signature_current'
        for definition in definitions:
            if node.virtual:
                status, reason = 'update', 'virtual_target'
            elif cause is not None:
                status, reason = 'update', cause
            elif uncertain or self.result.bodies:
                status, reason = 'recheck', 'post_execution_state'
            else:
                state = self._file(node)
                new = self.state.build_signature(definition, node)
                if new is not None and type(new) is not str:
                    raise ValueError('invalid buildcheck observation')
                old = self._stored(node, None, 'buildcheck')
                # A positive newer timestamp suppresses the buildcheck
                # comparison in Update.outdated; it is still needed at commit.
                if not stamp and new and old is not None and new != old:
                    status = 'update'
                    reason = 'buildcheck_changed' if old else 'buildcheck_missing'
                elif state.mtime < stamp:
                    status, reason = 'update', 'newer_prerequisite'
                elif not state.exists or state.mtime == 0:
                    status, reason = 'update', 'missing_target'
                elif not stamp and (new is None or (new and old is None)):
                    return self._problem(node, 'buildcheck_unavailable',
                                         'historical expanded body signature unavailable', relations, True)
            if status in ('update', 'recheck') or _sections(definition.body):
                mode = ('conditional' if status == 'recheck' else
                        'update' if status == 'update' else 'sections')
                step = BodyPlan(node, definition, mode, reason,
                                len(self.result.bodies), range(len(self.result.bodies)))
                step.active_paths = tuple(self._path)
                step.active_definitions = tuple(sorted(self._active, key=lambda d: d.index))
                step.prerequisite_definitions = tuple(self._callers)
                self.result.bodies.append(step)
                step.graph_snapshot = self.graph.snapshot()
                bodies.append(step)
                # Under normal successful execution, siblings are marked done
                # only when an actual update was selected, not for sections.
                if mode == 'update':
                    for sibling in step.signature_targets:
                        if sibling is not node:
                            self._cover[sibling] = step
                elif mode == 'conditional':
                    for sibling in step.signature_targets:
                        if sibling is not node:
                            self._maybe_cover[sibling] = step
        return self._finish(node, status, reason, relations, bodies, after)
''')

_MANIFEST['aap_semantics.port_commands'] = ('tests/src/aap_semantics/port_commands.py', False, '6422472ff3ee3a62e7eb36dd5c19fb652041a10de3bae04f41d0388b412a78dd')
_EMBEDDED['aap_semantics.port_commands'] = ('tests/src/aap_semantics/port_commands.py', False, r'''"""Port.port_exe_cmd -> logged_system, distinct from Commands.aap_shell.

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
''')

_MANIFEST['aap_semantics.port_defaults'] = ('tests/src/aap_semantics/port_defaults.py', False, '98c08ccd6a7fbb409c56f8ceb830d5c1571744e2bac138d6fe0fa30a232a841e')
_EMBEDDED['aap_semantics.port_defaults'] = ('tests/src/aap_semantics/port_defaults.py', False, r'''"""Archive-port default declarations from Port.add_port_defaults/add_port_dep.

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
''')

_MANIFEST['aap_semantics.port_delete'] = ('tests/src/aap_semantics/port_delete.py', False, '8010f6b9730fd503909df7c3c3aca748594859b73ab2b6c02d0de5a3641c93af')
_EMBEDDED['aap_semantics.port_delete'] = ('tests/src/aap_semantics/port_delete.py', False, r'''"""Injected tree deletion for the bounded Port.clean/distclean helpers.

The semantic runtime never performs host deletion. The memory adapter uses
explicit entries and missing facts; unknown paths remain unavailable.
"""
from .model import Node
from .path_observation import PathObserver, PathObservation


class DeleteRequest(Node):
    def __init__(self, origin, path, argument, cwd):
        super(DeleteRequest, self).__init__(origin)
        self.path, self.argument, self.cwd = path, argument, cwd


class DeleteResult(object):
    def __init__(self, status, detail=None):
        self.status, self.detail = status, detail


class DeleteBackend(object):
    def delete_tree(self, request):
        return DeleteResult('UNAVAILABLE', 'tree deletion capability unavailable')


class MemoryDeleteBackend(DeleteBackend, PathObserver):
    """Explicit file/dir/link facts with recursive, link-safe in-memory removal.

    A link entry is ('symlink', absolute_target). Its target is observed with
    stat-like semantics but never removed by deleting the link.
    Associated in-memory stores are changed only by successful deletion.
    """
    def __init__(self, entries=None, missing=(), markers=None, marker_root=None,
                 stores=(), failures=None, fallback=None):
        self.entries = dict(entries or {})
        self.missing = set(missing)
        self.markers = markers
        self.marker_root = marker_root
        self.stores = list(stores)
        self.failures = dict(failures or {})
        self.fallback = fallback
        self.requests = []
        self.observations = []
        self.deleted = set()

    def _within(self, path, root):
        return path == root or path.startswith(root.rstrip('/') + '/')

    def _marker_path(self, path):
        return (self.markers is not None and self.marker_root is not None
                and self._within(path, self.marker_root))

    def observe(self, request):
        self.observations.append(request)
        path = request.path
        if any(self._within(path, root) for root in self.deleted):
            return PathObservation('MISSING')
        if path in self.entries:
            entry = self.entries[path]
            if isinstance(entry, tuple) and entry[0] == 'symlink':
                target = entry[1]
                if any(self._within(target, root) for root in self.deleted):
                    return PathObservation('MISSING')
                if target in self.entries:
                    return PathObservation('EXISTS')
                if target in self.missing:
                    return PathObservation('MISSING')
                return PathObservation('UNAVAILABLE')
            return PathObservation('EXISTS')
        if self._marker_path(path):
            return PathObservation('EXISTS' if self.markers.marker_exists(path) else 'MISSING')
        if path in self.missing:
            return PathObservation('MISSING')
        if self.fallback is not None:
            return self.fallback.observe(request)
        return PathObservation('UNAVAILABLE')

    def delete_tree(self, request):
        self.requests.append(request)
        path = request.path
        failure = self.failures.get(path)
        if failure is not None:
            return failure
        if self.observe(request).status != 'EXISTS':
            return DeleteResult('FAILED', 'path disappeared before deletion')
        # A symlink is unlinked itself, even when its target is a directory.
        entry = self.entries.get(path)
        recursive = not (isinstance(entry, tuple) and entry[0] == 'symlink')
        def selected(name):
            return self._within(name, path) if recursive else name == path
        for name in list(self.entries):
            if selected(name):
                del self.entries[name]
        for store in self.stores:
            for name in list(store):
                if selected(name):
                    del store[name]
        if self.markers is not None:
            for store in (self.markers.files, self.markers.times):
                for name in list(store):
                    if selected(name):
                        del store[name]
            self.markers.directories = set(name for name in self.markers.directories
                                           if not selected(name))
        self.deleted.add(path)
        return DeleteResult('COMPLETED')
''')

_MANIFEST['aap_semantics.port_makesum'] = ('tests/src/aap_semantics/port_makesum.py', False, '83d314401356543aa6d5ab8311354f358d17a99bfeba5db45c357e59b180c1da')
_EMBEDDED['aap_semantics.port_makesum'] = ('tests/src/aap_semantics/port_makesum.py', False, r'''"""Linux Port.py:248-380 semantics, including unreachable restoration code."""
import posixpath

from .checksum import ChecksumBackend, ChecksumRequest, ArtifactBackend
from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .recipe_mutation import RecipeMutationRequest, RecipeMutationResult
from .values import MISSING


START = b'#>>> automatically inserted by "aap makesum" <<<\n'
END = b'#>>> end <<<\n'


def makesum(port, record, evaluator):
    work = record.scope.get_work()
    if work is None or not work.top_recipe:
        raise SemanticError(record, 'No recipe specified to makesum for')
    if type(work.top_recipe) is not str:
        raise Unsupported(record, 'top recipe identity must be a path string')
    recipe = port._path(record.cwd, work.top_recipe, record)
    if evaluator is None:
        raise Unsupported(record, 'makesum requires path observations')
    paths = evaluator.python.helpers.paths

    def exists(argument):
        before = len(paths.records)
        try:
            return paths.exists(argument, record.cwd, record)
        finally:
            if len(paths.records) > before:
                record.observations.append(paths.records[-1])

    # Deliberately construct the concrete byte-hashing implementation. The
    # verification backend may be a RecordedChecksum oracle; never use it here.
    digest = ChecksumBackend(port.artifacts if port.artifacts is not None else ArtifactBackend())
    lines = []
    for variable, directory in (('DISTFILES', 'DISTDIR'), ('PATCHFILES', 'PATCHDISTDIR')):
        files = []
        for name in (variable, 'CVS' + variable):
            for filename, unused in items(port._value(record.scope, name, record), record):
                basename = posixpath.basename(filename)
                if basename not in files:
                    files.append(basename)
        for filename in files:
            value = record.scope.lookup(directory)
            if value is MISSING or value is None:
                raise SemanticError(record, 'missing makesum directory: ' + directory)
            directory_value = port._value(record.scope, directory, record)
            argument = posixpath.join(directory_value, filename)
            request = ChecksumRequest(record, argument, {}, record.cwd)
            record.paths.append(request.path)
            if not exists(argument):
                raise SemanticError(record, 'File does not exists: "' + argument + '"')
            try:
                checksum = digest.md5(request)
            except OSError as error:
                raise SemanticError(record, 'Cannot compute checksum for "' + argument + '": ' + str(error))
            record.checksums.append((request, checksum))
            # ASCII filename spelling is the bounded production surface. Recipe
            # contents themselves are never decoded, even for unrelated bytes.
            try:
                line = ('\t:checksum $%s/%s {md5 = %s}\n' %
                        (directory, filename, checksum)).encode('ascii')
            except UnicodeError:
                raise Unsupported(record, 'non-ASCII makesum filename encoding is deferred')
            lines.append(line)
    _RecipeEditor(port.recipe_mutations, record, exists).rewrite(
        recipe, work.top_recipe, lines)


class _RecipeEditor(object):
    def __init__(self, backend, record, exists):
        self.backend, self.record, self.exists = backend, record, exists

    def effect(self, operation, path, destination=None, data=None):
        request = RecipeMutationRequest(self.record, operation, path, destination, data)
        try:
            result = self.backend.apply(request)
        except NotImplementedError as error:
            result = RecipeMutationResult('UNAVAILABLE', str(error))
        except OSError as error:
            result = RecipeMutationResult('FAILED', str(error))
        self.record.mutations.append((request, result))
        if not isinstance(result, RecipeMutationResult):
            raise OSError('invalid recipe mutation result')
        if result.status == 'UNAVAILABLE':
            raise Unsupported(self.record, result.detail or 'recipe mutation unavailable')
        if result.status != 'COMPLETED':
            raise OSError(result.detail or 'recipe mutation failed')
        if operation == 'readline' and type(result.data) is not bytes:
            raise OSError('recipe reader must return exact bytes')
        return result.data

    def try_delete(self, path):
        try:
            self.effect('remove', path)
        except OSError:
            pass  # Util.try_delete suppresses actual removal failures.
        # Unavailable capability is not a historical I/O failure; keep it BLOCKED.

    def error(self, message, error):
        raise SemanticError(self.record, message + str(error))

    def rewrite(self, recipe, spelling, lines):
        try:
            self.effect('open_read', recipe)
        except OSError as error:
            self.error('Cannot open recipe file "%s": ' % spelling, error)
        number = 1
        while self.exists(recipe + str(number)):
            number += 1
        temp = recipe + str(number)
        try:
            self.effect('create_temp', temp)
        except OSError as error:
            self.error('Cannot create temp file "%s": ' % (spelling + str(number)), error)

        def write(data):
            self.effect('write', temp, data=data)

        def checksum_lines():
            write(b'do-checksum:\n')
            for line in lines or [b'\t@pass\n']:
                write(line)
            write(END)

        try:
            added = False
            while True:
                line = self.effect('readline', recipe)
                if not line:
                    break
                write(line)
                if line == START:
                    if added:
                        raise SemanticError(self.record, 'Duplicate makesum start marker')
                    added = True
                    checksum_lines()
                    while True:
                        line = self.effect('readline', recipe)
                        if not line:
                            raise SemanticError(self.record, 'Missing makesum end marker')
                        if line == END:
                            break
            if not added:
                write(START)
                checksum_lines()
            self.effect('close_read', recipe)
            self.effect('close_temp', temp)
        except Unsupported:
            # Capability absence is not evidence of an OS copying failure.
            # Preserve the last observed state and report BLOCKED.
            raise
        except (OSError, SemanticError) as error:
            try:
                self.effect('close_temp', temp)
            except OSError:
                pass
            self.effect('remove', temp)  # unlike try_delete, can mask copy error
            self.error('Error while copying recipe file: ', error)

        backup = recipe + '~'
        if self.exists(backup):
            try:
                self.effect('remove', backup)
            except OSError as error:
                self.try_delete(temp)
                self.error('Cannot delete backup recipe "%s": ' % (spelling + '~'), error)
        try:
            self.effect('rename', recipe, backup)
        except OSError as error:
            self.try_delete(temp)
            self.error('Cannot rename recipe "%s" to "%s": ' %
                       (spelling, spelling + '~'), error)
        try:
            self.effect('rename', temp, recipe)
        except OSError as error:
            # Port.py:371 calls recipe_error (always raises). Its subsequent
            # backup -> original restoration is unreachable. Preserve that bug.
            self.error('Cannot rename recipe to "%s": ' % spelling, error)
        self.try_delete(temp)
''')

_MANIFEST['aap_semantics.port_runtime'] = ('tests/src/aap_semantics/port_runtime.py', False, 'f75fd2fcb062466cdaf3e36484c8f8dc497012a1d58d0e9c126c7eeb4523acd1')
_EMBEDDED['aap_semantics.port_runtime'] = ('tests/src/aap_semantics/port_runtime.py', False, r'''"""Archive-port preparation and bounded done-marker effects via capabilities.

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
''')

_MANIFEST['aap_semantics.process'] = ('tests/src/aap_semantics/process.py', False, '1f60c40be4e462e4c3e1a2d3bfedc2c89f0607f60affcbde2abf38e09df56338')
_EMBEDDED['aap_semantics.process'] = ('tests/src/aap_semantics/process.py', False, r'''"""POSIX capture semantics behind an injected backend. No host launcher."""
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
''')

_MANIFEST['aap_semantics.python_eval'] = ('tests/src/aap_semantics/python_eval.py', False, '398967349372a93609acc2486e5177f8354cbf424177303b0a38b24fb9d0a4e3')
_EMBEDDED['aap_semantics.python_eval'] = ('tests/src/aap_semantics/python_eval.py', False, r'''"""Explicit Python AST interpretation over closed metadata values."""
import ast
import io
import tokenize

from .diagnostics import SemanticError, UndefinedName, Unsupported
from .helpers import HelperRegistry
from .scopes import Namespace
from .values import MISSING, RegexMatchValue, check_value, truth


_ALLOWED = frozenset(('Module', 'Expression', 'Expr', 'Assign', 'Pass', 'If',
                     'For', 'Name', 'Attribute', 'List', 'Tuple', 'Subscript',
                     'Index', 'Compare', 'Eq', 'NotEq', 'GtE', 'In',
                     'BoolOp', 'And', 'UnaryOp', 'Not', 'BinOp', 'Add',
                     'Call', 'Load', 'Store', 'Constant', 'Str', 'Num',
                     'NameConstant'))


def call_path(node):
    if type(node).__name__ == 'Name':
        return node.id
    if type(node).__name__ == 'Attribute':
        base = call_path(node.value)
        if base:
            return base + '.' + node.attr
    return None


def constant(node):
    kind = type(node).__name__
    if kind in ('Constant', 'NameConstant'):
        return node.value
    if kind == 'Str':
        return node.s
    return node.n


class PythonEvaluator(object):
    def __init__(self, scope, helpers=None, max_steps=10000):
        self.scope = scope
        self.helpers = helpers or HelperRegistry()
        self.max_steps = max_steps
        self.steps = 0
        self.port_runtime = None
        self.evaluator_context = None
        self.port_records = []

    def step(self, origin):
        self.steps += 1
        if self.steps > self.max_steps:
            raise Unsupported(origin, 'metadata evaluation step limit exceeded')

    def parse(self, fragment, mode='exec', syntax_only=False):
        try:
            code = fragment.code.strip()
            tree = ast.parse(code, fragment.span.source_id, mode)
            for token in tokenize.generate_tokens(io.StringIO(code).readline):
                if token.type == tokenize.NUMBER and '_' in token.string:
                    raise Unsupported(fragment, 'numeric separators are not Python 3.4 syntax')
        except (SyntaxError, ValueError, tokenize.TokenError) as error:
            if isinstance(error, SemanticError):
                raise
            raise SemanticError(fragment, 'invalid embedded Python: ' + str(error))
        if not syntax_only:
            self.validate(tree, fragment)
        return tree

    def validate(self, tree, origin):
        # globals() is a scope capability only in this exact embedding form.
        # Never construct or expose a host globals dictionary to recipe code.
        port_calls = set()
        globals_calls = set()
        if self.port_runtime is not None:
            for node in ast.walk(tree):
                if type(node).__name__ == 'Call' and call_path(node.func) in self.port_runtime.HELPERS:
                    if (len(node.args) != 1 or type(node.args[0]).__name__ != 'Call'
                            or call_path(node.args[0].func) != 'globals'
                            or node.args[0].args or node.args[0].keywords
                            or getattr(node.args[0], 'starargs', None) is not None
                            or getattr(node.args[0], 'kwargs', None) is not None):
                        raise Unsupported(origin, 'port helper requires exactly globals()')
                    port_calls.add(id(node))
                    globals_calls.add(id(node.args[0]))
        for node in ast.walk(tree):
            kind = type(node).__name__
            if kind not in _ALLOWED:
                raise Unsupported(origin, 'unsupported Python AST node: ' + kind)
            if kind in ('Constant', 'Str', 'Num', 'NameConstant'):
                check_value(constant(node), origin)
            if kind == 'Name' and node.id.startswith('__'):
                raise Unsupported(origin, 'private interpreter names are not permitted')
            if kind == 'Attribute' and node.attr.startswith('__'):
                raise Unsupported(origin, 'private attribute access is not permitted')
            if kind in ('Assign', 'For'):
                targets = node.targets if kind == 'Assign' else [node.target]
                for target in targets:
                    self.validate_target(target, origin)
            if kind == 'For' and node.orelse:
                raise Unsupported(origin, 'for-else is not in the characterized subset')
            if kind == 'Call':
                if (node.keywords or getattr(node, 'starargs', None) is not None
                        or getattr(node, 'kwargs', None) is not None):
                    raise Unsupported(origin, 'keyword and unpacked calls are unsupported')
                path = call_path(node.func)
                if id(node) in port_calls or id(node) in globals_calls:
                    continue
                # Known deferred capabilities are checked only on a reached
                # call. Syntax and the closed interpreter policy remain eager.
                if path in self.helpers.DEFERRED:
                    continue
                if path not in self.helpers.NAMES:
                    if (type(node.func).__name__ != 'Attribute'
                            or node.func.attr not in self.helpers.METHODS):
                        raise Unsupported(origin, 'unapproved call: ' + (path or '<expression>'))

    def validate_target(self, node, origin):
        kind = type(node).__name__
        if kind == 'Name':
            return
        if kind == 'Attribute' and type(node.value).__name__ == 'Name':
            return
        if kind in ('List', 'Tuple'):
            for element in node.elts:
                self.validate_target(element, origin)
            return
        raise Unsupported(origin, 'unsupported Python assignment target: ' + kind)

    def expression(self, fragment):
        tree = self.parse(fragment, 'eval')
        return self.value(tree.body, fragment)

    def value(self, node, origin):
        self.step(origin)
        kind = type(node).__name__
        if kind in ('Constant', 'Str', 'Num', 'NameConstant'):
            return check_value(constant(node), origin)
        if kind == 'Name':
            return self.scope.python_name(node.id, origin)
        if kind == 'Attribute':
            if call_path(node) == 'os.path.curdir':
                self.unshadowed('os.path.curdir', origin)
                return '.'
            namespace = self.value(node.value, origin)
            if not isinstance(namespace, Namespace):
                raise Unsupported(origin, 'attribute reads require an explicit scope namespace')
            value = namespace.get(node.attr)
            if value is MISSING:
                raise UndefinedName(origin, 'undefined scoped Python name: ' + node.attr)
            return value
        if kind in ('List', 'Tuple'):
            values = [self.value(element, origin) for element in node.elts]
            return check_value(values if kind == 'List' else tuple(values), origin)
        if kind == 'Subscript':
            value = self.value(node.value, origin)
            index_node = node.slice
            if type(index_node).__name__ == 'Index':
                index_node = index_node.value
            index = self.value(index_node, origin)
            if type(value) not in (str, list, tuple) or type(index) not in (int, bool):
                raise Unsupported(origin, 'indexing requires a sequence and integer')
            try:
                return value[index]
            except IndexError:
                raise SemanticError(origin, 'sequence index out of range')
        if kind == 'UnaryOp' and type(node.op).__name__ == 'Not':
            return not truth(self.value(node.operand, origin), origin)
        if kind == 'BoolOp' and type(node.op).__name__ == 'And':
            value = None
            for operand in node.values:
                value = self.value(operand, origin)
                if not truth(value, origin):
                    break
            return value
        if kind == 'Compare':
            left = self.value(node.left, origin)
            for operator, right_node in zip(node.ops, node.comparators):
                right = self.value(right_node, origin)
                check_value(left, origin)
                check_value(right, origin)
                op = type(operator).__name__
                if op == 'Eq':
                    answer = left == right
                elif op == 'NotEq':
                    answer = left != right
                elif op == 'GtE':
                    numeric_right = type(right) in (int, bool)
                    if (type(left) is RegexMatchValue and numeric_right):
                        # Historical Python 2 allowed heterogeneous ordering:
                        # numeric types precede non-numeric types. The reached
                        # recipe compares re.search(...) with an integer to
                        # test its match result.
                        answer = True
                    elif left is None and numeric_right:
                        answer = False
                    elif ((type(left) in (int, bool) and numeric_right)
                          or type(left) is str and type(right) is str):
                        answer = left >= right
                    else:
                        raise Unsupported(origin, 'ordering requires strings or integers')
                elif op == 'In':
                    if (type(right) not in (str, list, tuple)
                            or type(right) is str and type(left) is not str):
                        raise SemanticError(origin,
                                            'membership requires compatible sequence values')
                    answer = left in right
                else:
                    raise Unsupported(origin, 'unsupported comparison: ' + op)
                if not answer:
                    return False
                left = right
            return True
        if kind == 'BinOp' and type(node.op).__name__ == 'Add':
            left, right = self.value(node.left, origin), self.value(node.right, origin)
            check_value(left, origin)
            check_value(right, origin)
            if not ((type(left) in (int, bool) and type(right) in (int, bool))
                    or type(left) is type(right) and type(left) in (str, list, tuple)):
                raise SemanticError(origin, 'addition requires compatible metadata values')
            return left + right
        if kind == 'Call':
            path = call_path(node.func)
            try:
                if self.port_runtime is not None and path in self.port_runtime.HELPERS:
                    self.unshadowed(path, origin)
                    self.unshadowed('globals', origin)
                    return self.port_runtime.call(path, self.scope, self.helpers.cwd,
                                                  origin, self.port_records, self.evaluator_context)
                if path in self.helpers.DEFERRED:
                    raise Unsupported(origin, 'deferred capability: ' + path)
                if path in self.helpers.NAMES:
                    self.unshadowed(path, origin)
                    args = [self.value(arg, origin) for arg in node.args]
                    return self.helpers.call(path, args, origin)
                receiver = self.value(node.func.value, origin)
                args = [self.value(arg, origin) for arg in node.args]
                return self.helpers.method(receiver, node.func.attr, args, origin)
            except (TypeError, ValueError, OverflowError) as error:
                if isinstance(error, SemanticError):
                    raise
                raise SemanticError(origin, 'compatibility helper failed: ' + str(error))
        raise Unsupported(origin, 'unsupported Python expression: ' + kind)

    def unshadowed(self, path, origin):
        root = path.split('.')[0]
        if root in self.scope.local or root in self.scope.namespaces:
            raise Unsupported(origin,
                              'calling a shadowed compatibility helper is unsupported: ' + root)

    def assign(self, target, value, origin):
        check_value(value, origin)
        kind = type(target).__name__
        if kind == 'Name':
            self.scope.store(target.id, value, origin)
        elif kind == 'Attribute':
            namespace = self.value(target.value, origin)
            if not isinstance(namespace, Namespace):
                raise Unsupported(origin, 'attribute assignment requires a scope namespace')
            namespace.set(target.attr, value, origin)
        elif kind in ('Tuple', 'List'):
            if type(value) not in (list, tuple) or len(value) != len(target.elts):
                raise SemanticError(origin, 'unpacking requires a matching list or tuple')
            for element, item in zip(target.elts, value):
                self.assign(element, item, origin)
        else:
            raise Unsupported(origin, 'unsupported assignment target')

    def statements(self, statements, origin):
        for node in statements:
            self.step(origin)
            kind = type(node).__name__
            if kind == 'Assign':
                value = self.value(node.value, origin)
                for target in node.targets:
                    self.assign(target, value, origin)
            elif kind == 'Expr':
                self.value(node.value, origin)
            elif kind == 'Pass':
                continue
            elif kind == 'If':
                self.statements(node.body if truth(self.value(node.test, origin), origin)
                                else node.orelse, origin)
            elif kind == 'For':
                iterable = self.sequence(node.iter, origin)
                for value in iterable:
                    self.step(origin)
                    self.assign(node.target, value, origin)
                    self.statements(node.body, origin)
            else:
                raise Unsupported(origin, 'unsupported Python statement: ' + kind)

    def sequence(self, node, origin):
        value = self.value(node, origin)
        if type(value) not in (str, list, tuple):
            raise Unsupported(origin, 'for requires a metadata sequence')
        check_value(value, origin)
        return value
''')

_MANIFEST['aap_semantics.python_shell'] = ('tests/src/aap_semantics/python_shell.py', False, '2ec5f35e08b776abaee4a41837696996e783dc470a9e9b82e0c63cebc5a65648')
_EMBEDDED['aap_semantics.python_shell'] = ('tests/src/aap_semantics/python_shell.py', False, r'''"""The audited shellheader/shellfooter :python form, with injected byte writes.

Process.get_block_lines historically inserts the block into the same Python
dictionary used by @ statements. This interpreter keeps that scope and uses
PythonEvaluator for ordinary expressions; it never obtains host file objects.
"""
import ast
import posixpath

from . import model as m
from .diagnostics import SemanticError, Unsupported


_NODES = frozenset(('Module', 'Assign', 'Expr', 'For', 'Name', 'Attribute',
                    'Call', 'Load', 'Store', 'Constant', 'Str'))


def _kind(node):
    return type(node).__name__


def _name_call(node):
    return _kind(node) == 'Call' and _kind(node.func) == 'Name'


def _method_call(node):
    return (_kind(node) == 'Call' and _kind(node.func) == 'Attribute'
            and _kind(node.func.value) == 'Name')


def _safe_scalar(node):
    kind = _kind(node)
    if kind in ('Constant', 'Str', 'Name'):
        return True
    if kind == 'Attribute':
        return _kind(node.value) == 'Name' and node.value.id == '_no' and node.attr in (
            'script', 'functions')
    return False


def _safe_statement(node):
    kind = _kind(node)
    if kind == 'Assign':
        value = node.value
        valid_value = _safe_scalar(value)
        if _name_call(value):
            if value.func.id in ('var2string', 'var2list'):
                valid_value = len(value.args) == 1 and _safe_scalar(value.args[0])
            elif value.func.id == 'open':
                valid_value = len(value.args) == 2 and all(
                    _safe_scalar(arg) for arg in value.args)
        return (len(node.targets) == 1 and _kind(node.targets[0]) == 'Name'
                and not node.targets[0].id.startswith('__')
                and valid_value)
    if kind == 'Expr':
        value = node.value
        return (_method_call(value) and value.func.attr in ('write', 'close')
                and len(value.args) == (1 if value.func.attr == 'write' else 0)
                and all(_safe_scalar(arg) for arg in value.args))
    if kind == 'For':
        return (_kind(node.target) == 'Name' and not node.target.id.startswith('__')
                and _kind(node.iter) == 'Name' and not node.orelse
                and all(_safe_statement(part) for part in node.body))
    return False


def approve(tree):
    """Closed syntax gate; unsupported blocks remain deferred operations."""
    for node in ast.walk(tree):
        if _kind(node) not in _NODES:
            return False
        if _kind(node) == 'Name' and node.id.startswith('__'):
            return False
        if _kind(node) == 'Attribute' and node.attr.startswith('__'):
            return False
        if _kind(node) == 'Call' and (node.keywords or getattr(node, 'starargs', None)
                                     or getattr(node, 'kwargs', None)):
            return False
    return all(_safe_statement(node) for node in tree.body)


class ShellWriteRequest(m.Node):
    def __init__(self, origin, path, cwd, mode, encoding):
        super(ShellWriteRequest, self).__init__(origin)
        self.path, self.cwd, self.mode = path, cwd, mode
        self.destination = 'overwrite' if mode == 'w' else 'append'
        self.encoding = encoding


class ShellWriteRecord(m.Node):
    def __init__(self, request, operation, data=None):
        super(ShellWriteRecord, self).__init__(request)
        self.request, self.operation, self.data = request, operation, data
        self.status, self.error = 'PENDING', None


class ShellFileHandle(object):
    def __init__(self, request, session, policy, records):
        self.request, self.session = request, session
        self.policy, self.records = policy, records
        self.closed = False

    def operation(self, name, origin, text=None):
        if self.closed:
            raise SemanticError(origin, 'operation on closed shell script file')
        if name == 'write':
            if type(text) is not str:
                raise SemanticError(origin, 'shell script write requires a string')
            data = self.policy.encode(text, origin)
        else:
            data = None
        record = ShellWriteRecord(self.request, name, data)
        self.records.append(record)
        try:
            if name == 'write':
                self.session.write(data)
            else:
                self.session.close()
                self.closed = True
        except NotImplementedError as error:
            record.status, record.error = 'BLOCKED', Unsupported(origin, str(error))
            raise record.error
        except Exception as error:
            record.status, record.error = 'FAILED', SemanticError(
                origin, 'shell script ' + name + ' failed: ' + str(error))
            raise record.error
        record.status = 'COMPLETED'
        return None


class ShellBlockRuntime(object):
    def __init__(self, python, output_runtime, cwd, records):
        self.python, self.output_runtime = python, output_runtime
        self.cwd, self.records = cwd, records

    def expression(self, node, origin):
        if _name_call(node) and node.func.id == 'open':
            return self.open_file(node, origin)
        if _method_call(node):
            receiver = self.python.scope.python_name(node.func.value.id, origin)
            if type(receiver) is not ShellFileHandle:
                raise Unsupported(origin, 'shell file method requires an opened file handle')
            args = [self.expression(arg, origin) for arg in node.args]
            return receiver.operation(node.func.attr, origin, args[0] if args else None)
        return self.python.value(node, origin)

    def open_file(self, node, origin):
        if 'open' in self.python.scope.local or 'open' in self.python.scope.namespaces:
            raise Unsupported(origin, 'shadowed shell script open is unsupported')
        path, mode = [self.expression(arg, origin) for arg in node.args]
        if type(path) is not str or not path or '\x00' in path:
            raise SemanticError(origin, 'shell script path must be a nonempty string without NUL')
        if mode not in ('w', 'a'):
            raise Unsupported(origin, 'shell script open mode is unsupported: ' + str(mode))
        if self.output_runtime is None:
            raise Unsupported(origin, 'shell script writer unavailable')
        if not posixpath.isabs(path):
            if type(self.cwd) is not str or not posixpath.isabs(self.cwd):
                raise Unsupported(origin, 'shell script path requires an absolute logical cwd')
            path = posixpath.join(self.cwd, path)
        request = ShellWriteRequest(origin, path, self.cwd, mode,
                                    self.output_runtime.policy.encoding)
        record = ShellWriteRecord(request, 'open')
        self.records.append(record)
        try:
            session = self.output_runtime.writer.open_bytes(request)
            if (session is None or not callable(getattr(session, 'write', None))
                    or not callable(getattr(session, 'close', None))):
                raise ValueError('invalid shell script writer session')
        except NotImplementedError as error:
            record.status, record.error = 'BLOCKED', Unsupported(origin, str(error))
            raise record.error
        except Exception as error:
            record.status, record.error = 'FAILED', SemanticError(
                origin, 'shell script open failed: ' + str(error))
            raise record.error
        record.status = 'COMPLETED'
        return ShellFileHandle(request, session, self.output_runtime.policy, self.records)

    def statements(self, body, origin):
        for node in body:
            self.python.step(origin)
            kind = _kind(node)
            if kind == 'Assign':
                value = self.expression(node.value, origin)
                target = node.targets[0]
                if type(value) is ShellFileHandle:
                    # An opaque Python file binding lives in the same build
                    # dictionary as @ assignments. Other metadata operations
                    # still reject it through the closed value checks.
                    if target.id in self.python.scope.namespaces:
                        raise Unsupported(origin, 'overwriting a scope binding is deferred')
                    self.python.scope.local[target.id] = value
                else:
                    self.python.assign(target, value, origin)
            elif kind == 'Expr':
                self.expression(node.value, origin)
            elif kind == 'For':
                for value in self.python.sequence(node.iter, origin):
                    self.python.step(origin)
                    self.python.assign(node.target, value, origin)
                    self.statements(node.body, origin)
            else:
                raise Unsupported(origin, 'unsupported shell Python statement: ' + kind)
''')

_MANIFEST['aap_semantics.recipe_mutation'] = ('tests/src/aap_semantics/recipe_mutation.py', False, '81453b69f3dd23cf4959db4dd8095f9b9cbeb70fcba7c552d0d8e30d8008490a')
_EMBEDDED['aap_semantics.recipe_mutation'] = ('tests/src/aap_semantics/recipe_mutation.py', False, r'''"""Narrow byte-stream and file-name capabilities for Port.port_makesum.

No host adapter. Open/read/write/close remain separate so failure timing and
partial temporary-file contents are observable. No transaction or rollback.
"""
from .model import Node
from .path_observation import PathObserver, PathObservation


class RecipeMutationRequest(Node):
    def __init__(self, origin, operation, path, destination=None, data=None):
        super(RecipeMutationRequest, self).__init__(origin)
        self.operation, self.path = operation, path
        self.destination, self.data = destination, data


class RecipeMutationResult(object):
    def __init__(self, status='COMPLETED', detail=None, data=None):
        self.status, self.detail, self.data = status, detail, data


class RecipeMutationBackend(object):
    def apply(self, request):
        return RecipeMutationResult('UNAVAILABLE', 'recipe mutation capability unavailable')


class MemoryRecipeMutationBackend(RecipeMutationBackend, PathObserver):
    """Explicit regular-byte-file store; recipe families are closed fixtures.

    For each supplied recipe, its numbered temps and ~ backup are known missing
    unless present in files. Other unknown paths delegate or stay unavailable.
    files may be shared with existing artifact/generated-file adapters.
    failures maps (operation, path, destination) to structured failed/unavailable
    results. Failures happen before that operation's effect; earlier writes stay.
    """
    def __init__(self, recipes=(), files=None, failures=None, fallback=None):
        self.recipes = tuple(recipes)
        self.files = files if files is not None else {}
        self.failures = dict(failures or {})
        self.fallback = fallback
        self.requests, self.observations = [], []
        self.readers, self.writers = {}, set()

    def observe(self, request):
        self.observations.append(request)
        if request.path in self.files:
            return PathObservation('EXISTS')
        for recipe in self.recipes:
            suffix = request.path[len(recipe):] if request.path.startswith(recipe) else None
            if suffix is not None and (suffix in ('', '~') or
                    (suffix and all(c in '0123456789' for c in suffix))):
                return PathObservation('MISSING')
        if self.fallback is not None:
            return self.fallback.observe(request)
        return PathObservation('UNAVAILABLE')

    def apply(self, request):
        self.requests.append(request)
        operation, path = request.operation, request.path
        failure = self.failures.get((operation, path, request.destination))
        if failure is not None:
            return failure
        try:
            if operation == 'open_read':
                if type(self.files.get(path)) is not bytes:
                    raise OSError('recipe is not a readable regular byte file')
                self.readers[path] = (self.files[path], 0)
            elif operation == 'readline':
                data, offset = self.readers[path]
                end = data.find(b'\n', offset)
                end = len(data) if end == -1 else end + 1
                self.readers[path] = (data, end)
                return RecipeMutationResult(data=data[offset:end])
            elif operation == 'close_read':
                self.readers.pop(path, None)
            elif operation == 'create_temp':
                # Like open(..., 'w'), not O_EXCL. The prior existence probe
                # does not make creation atomic.
                self.files[path] = b''
                self.writers.add(path)
            elif operation == 'write':
                if path not in self.writers or type(request.data) is not bytes:
                    raise OSError('invalid recipe byte write')
                self.files[path] += request.data
            elif operation == 'close_temp':
                self.writers.discard(path)
            elif operation == 'remove':
                if path not in self.files:
                    raise OSError('file does not exist')
                del self.files[path]
            elif operation == 'rename':
                if path not in self.files:
                    raise OSError('rename source does not exist')
                self.files[request.destination] = self.files.pop(path)
            else:
                return RecipeMutationResult('UNAVAILABLE', 'unknown recipe mutation operation')
        except (OSError, KeyError) as error:
            return RecipeMutationResult('FAILED', str(error))
        return RecipeMutationResult()
''')

_MANIFEST['aap_semantics.scopes'] = ('tests/src/aap_semantics/scopes.py', False, '96ee8eec158d85ed405cc620113ac8ec7cc6f39e6943b522b4ab3d18be2c6a35')
_EMBEDDED['aap_semantics.scopes'] = ('tests/src/aap_semantics/scopes.py', False, r'''"""Explicit recipe/build lookup layers, never host Python lexical scope."""
import string

from .diagnostics import SemanticError, UndefinedName, Unsupported
from .values import MISSING, DeferredExpansion, UnavailableValue, check_value
from .work import WorkIdentity


class Namespace(object):
    def __init__(self, scope, search=False):
        self.scope = scope
        self.search = search

    def get(self, name):
        return self.scope.lookup(name) if self.search else self.scope.local.get(name, MISSING)

    def set(self, name, value, origin):
        self.scope.store(name, value, origin)


class SearchNamespace(Namespace):
    """Historical CallstackDict: ordered live lookup; writes require a name."""
    def __init__(self, layers):
        self.layers = tuple(layers)

    def get(self, name):
        for layer in self.layers:
            if name in layer.local:
                return layer.local[name]
        return MISSING

    def set(self, name, value, origin):
        for layer in self.layers:
            if name in layer.local:
                layer.store(name, value, origin)
                return
        raise UndefinedName(origin, 'variable not found in enclosing scope: ' + name)


class Scope(object):
    def __init__(self, enclosing=()):
        self.local = {}
        self.work = None  # interpreter metadata, never a recipe Python value
        # Explicit ordered lookup layers; no inference of a runtime call stack.
        self.enclosing = tuple(enclosing)
        self.recipe_tree = (self,) + self.enclosing
        self.call_stack = None  # Top-level invocations do not contribute a caller frame.
        self.namespaces = {'_no': Namespace(self, True)}

    @classmethod
    def top_level(cls, port_defaults=False, work=None):
        scope = cls()
        scope.work = work if work is not None else WorkIdentity()
        scope.local['_prevdir'] = None
        if port_defaults:
            # Work.py initializes this port default before reading the recipe.
            scope.local['PATCHDISTDIR'] = 'patches'
        namespace = Namespace(scope)
        scope.namespaces['_recipe'] = namespace
        scope.namespaces['_top'] = namespace
        # Scope.create_topscope() gives command-line settings their own
        # RecipeDict.  This intentionally remains distinct from the current
        # recipe dictionary: a later recipe assignment may replace the normal
        # value without erasing the command-line value in _arg.
        scope.namespaces['_arg'] = Namespace(cls())
        return scope

    def set_command_line(self, name, value):
        """Seed a valid DoArgs-style setting before reading the main recipe."""
        self.local[name] = value
        self.namespaces['_arg'].scope.local[name] = value

    def get_work(self):
        # Work.getwork searches the same ordered layers as _no, including
        # caller/definition layers for deferred bodies and action entry.
        for scope in (self,) + self.enclosing:
            if scope.work is not None:
                return scope.work
        return None

    @classmethod
    def build(cls, definition, caller, keep_current_scope=False):
        """Scope.get_build_recdict; actions retain the caller recipe tree."""
        stack = ((caller,) + caller.call_stack if caller.call_stack is not None else ())
        tree = ((caller.recipe_tree if keep_current_scope else ())
                + definition.recipe_tree)
        conf = caller.namespaces.get('_conf')
        extra = (conf.scope,) if conf is not None else ()
        scope = cls(stack + tree + extra)
        scope.recipe_tree = tree
        scope.call_stack = stack
        for name in ('_top', '_arg', '_default', '_start', '_conf'):
            if name in caller.namespaces:
                scope.namespaces[name] = caller.namespaces[name]
        for name in ('_recipe', '_parent'):
            if name in definition.namespaces:
                scope.namespaces[name] = definition.namespaces[name]
        for name, namespace in caller.namespaces.items():
            if not name.startswith('_'):
                scope.namespaces[name] = namespace
        if caller.call_stack is not None:
            scope.namespaces['_caller'] = Namespace(caller)
        scope.namespaces['_tree'] = SearchNamespace(tree)
        scope.namespaces['_stack'] = SearchNamespace(stack)
        scope.namespaces['_up'] = SearchNamespace(scope.enclosing)
        return scope

    def lookup(self, name):
        value = self.local.get(name, MISSING)
        if value is not MISSING:
            return value
        for scope in self.enclosing:
            if name in scope.local:
                return scope.local[name]
        return MISSING

    def python_name(self, name, origin):
        # Bare Python names do not search _up. Namespace objects are explicit.
        if name in self.local:
            value = self.local[name]
            if isinstance(value, UnavailableValue):
                raise Unsupported(origin, value.reason)
            return value
        if name in self.namespaces:
            return self.namespaces[name]
        if name in ('_recipe', '_top', '_parent', '_up', '_tree', '_stack',
                    '_caller', '_default', '_start', '_conf', '_arg'):
            raise Unsupported(origin, 'scope is not bound in this metadata context: ' + name)
        raise UndefinedName(origin, 'undefined Python name: ' + name)

    def store(self, name, value, origin):
        if name in self.namespaces:
            raise Unsupported(origin, 'overwriting a scope binding is deferred: ' + name)
        if name.startswith('__'):
            raise Unsupported(origin, 'private interpreter names are not permitted')
        if not isinstance(value, DeferredExpansion):
            check_value(value, origin)
        self.local[name] = value

    def target(self, target, origin, create=False):
        parts = target.split('.')
        if len(parts) == 1:
            return self.namespaces['_no'], target
        if len(parts) != 2 or not all(parts):
            raise SemanticError(origin, 'invalid scoped variable name: ' + target)
        name, variable = parts
        if name not in self.namespaces:
            if (not create or name[0] not in string.ascii_letters
                    or any(c not in string.ascii_letters + string.digits + '_' for c in name)):
                raise Unsupported(origin, 'scope is not bound in this metadata context: ' + name)
            for layer in (self,) + self.enclosing:
                if name in layer.local:
                    raise SemanticError(origin, 'scope name already used as a variable: ' + name)
            namespace = Namespace(Scope())
            layers = (self,) + self.enclosing
            top = self.namespaces.get('_top')
            if top is not None and top.scope not in layers:
                layers += (top.scope,)
            # create_user_scope propagates a shared user scope to explicit layers.
            for layer in layers:
                existing = layer.namespaces.get(name)
                if existing is not None:
                    raise Unsupported(origin, 'user scope must be explicitly shared: ' + name)
            for layer in layers:
                layer.namespaces[name] = namespace
        return self.namespaces[name], variable

    def read(self, target, origin):
        namespace, name = self.target(target, origin)
        value = namespace.get(name)
        if isinstance(value, UnavailableValue):
            raise Unsupported(origin, value.reason)
        if value is MISSING:
            raise UndefinedName(origin, 'undefined A-A-P variable: ' + target)
        return value
''')

_MANIFEST['aap_semantics.system_process'] = ('tests/src/aap_semantics/system_process.py', False, 'a7b07fc3cf99d518422061eca689edfad707b2a1bdc5d4a0eec42db527cfb99c')
_EMBEDDED['aap_semantics.system_process'] = ('tests/src/aap_semantics/system_process.py', False, r'''"""Bounded synchronous :sys: plain shell forms and reached f/q/l attributes.

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
''')

_MANIFEST['aap_semantics.target_state'] = ('tests/src/aap_semantics/target_state.py', False, '0b075614cca713b51b79a046f104f5c2627ceb3c0189ce4478af1635e76ed0af')
_EMBEDDED['aap_semantics.target_state'] = ('tests/src/aap_semantics/target_state.py', False, r'''"""Read-only observations for planning, independent of the host filesystem.

Signature strings are historical observations, not hashes invented by the
planner. None means unavailable; an empty stored signature means absent.
"""
from collections import namedtuple
import hashlib


FileState = namedtuple('FileState', 'exists mtime directory')


def normalize_buildcheck(expanded_check):
    """DoBuild.buildcheck_update's final whitespace pass, before byte MD5."""
    text = expanded_check
    result = ''
    index = 0
    leading = True
    while index < len(text):
        start = index
        char = text[index]
        while (char in ' \t' and index + 1 < len(text)
               and text[index + 1] in ' \t\n'):
            index += 1
        if not leading:
            start = index
        end = index + 1
        while end < len(text) and text[end] not in ' \t':
            end += 1
        result += text[start:end]
        index = end
        leading = result[-1] == '\n'
    return result


def buildcheck_digest(expanded_check, encoding):
    """Hash an ALREADY PREPARED historical check string, not a raw CST body.

    Callers must have performed action expansion, comment removal, scoped
    expansion and special-variable masking. This helper only implements the
    final whitespace pass and MD5 in DoBuild.buildcheck_update/Sign.py.
    Encoding is explicit because upstream hashed Python 2 bytes.
    """
    return hashlib.md5(normalize_buildcheck(expanded_check).encode(encoding, 'strict')).hexdigest()


class TargetStateBackend(object):
    def file_state(self, node):
        raise NotImplementedError

    def current_signature(self, node, check):
        raise NotImplementedError

    def stored_signature(self, target, source, check):
        raise NotImplementedError

    def build_signature(self, definition, target):
        """Prepared historical buildcheck; None if expansion is unavailable."""
        raise NotImplementedError

    def implicit_dependencies(self, node):
        """False certifies none; True/None requires the deferred rule layer."""
        raise NotImplementedError

    def matching_rule(self, node):
        """False certifies no rule; True/None is outside explicit-graph planning."""
        raise NotImplementedError


class MemoryTargetState(TargetStateBackend):
    """An explicitly closed, in-memory world, never a view of ambient files.

    Files not entered are missing. Rules/autodependencies are absent unless
    overridden. Existing file signatures and buildchecks must be supplied;
    omission cannot accidentally certify that a target is current.
    """
    def __init__(self):
        self.files = {}
        self.signatures = {}
        self.stored = {}
        self.buildchecks = {}
        self.implicit = {}
        self.rules = {}

    def file_state(self, node):
        return self.files.get(node.path, FileState(False, 0, False))

    def current_signature(self, node, check):
        state = self.file_state(node)
        if check == 'time':
            return str(state.mtime if state.exists else 0)
        if check == 'none':
            return 'unknown'  # Sign.get_new_sign's historical fallback.
        if check in ('md5', 'c_md5') and not state.exists:
            return 'unknown'
        return self.signatures.get((node.identity, check))

    def stored_signature(self, target, source, check):
        identity = source.identity if source is not None else ''
        return self.stored.get((target.identity, identity, check), '')

    def build_signature(self, definition, target):
        return self.buildchecks.get((definition.index, target.identity))

    def implicit_dependencies(self, node):
        return self.implicit.get(node.identity, False)

    def matching_rule(self, node):
        return self.rules.get(node.identity, False)
''')

_MANIFEST['aap_semantics.tree_runtime'] = ('tests/src/aap_semantics/tree_runtime.py', False, '3ebfc9f7743572bcaf0e17e55a16cb833ae3ce901ef20b464b81252b30d8cb90')
_EMBEDDED['aap_semantics.tree_runtime'] = ('tests/src/aap_semantics/tree_runtime.py', False, r'''"""Bounded Linux :tree traversal over explicit directory observations."""
import posixpath
import re

from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .expansion import expand_text, render_value
from .lowering import lower_body
from .values import MISSING, _quote_item


class TreeRequest(object):
    def __init__(self, operation, path, cwd, origin):
        self.operation, self.argument, self.cwd = operation, path, cwd
        self.path = path if posixpath.isabs(path) else posixpath.join(cwd, path)
        self.source, self.span = origin.source, origin.span


class TreeObservation(object):
    """ENTRIES preserves listdir order; SYMLINK carries its stat target kind."""
    def __init__(self, status, entries=(), target_kind=None, detail=None):
        self.status = status
        self.entries = tuple(entries)
        self.target_kind = target_kind
        self.detail = detail


class TreeFilesystem(object):
    def list_directory(self, request):
        return TreeObservation('UNAVAILABLE')

    def classify(self, request):
        return TreeObservation('UNAVAILABLE')


class MemoryTreeFilesystem(TreeFilesystem):
    """Exact path facts; no host stat, listdir or implicit directory creation."""
    def __init__(self, directories=None, kinds=None):
        self.directories = dict(directories or {})
        self.kinds = dict(kinds or {})
        self.requests = []

    def list_directory(self, request):
        self.requests.append(request)
        return self.directories.get(request.path, TreeObservation('UNAVAILABLE'))

    def classify(self, request):
        self.requests.append(request)
        return self.kinds.get(request.path, TreeObservation('UNAVAILABLE'))


class TreeRecord(object):
    def __init__(self, request, observation, matched=False):
        self.request, self.observation, self.matched = request, observation, matched
        self.warning = (('Cannot read directory "' + request.argument + '"')
                        if request.operation == 'list' and observation.status in
                        ('UNREADABLE', 'MISSING') else None)


class TreeRuntime(object):
    def __init__(self, filesystem=None):
        self.filesystem = filesystem if filesystem is not None else TreeFilesystem()

    def _observe(self, operation, path, evaluator, origin, result):
        request = TreeRequest(operation, path, evaluator.cwd, origin)
        try:
            observation = (self.filesystem.list_directory(request) if operation == 'list'
                           else self.filesystem.classify(request))
        except NotImplementedError as error:
            observation = TreeObservation('UNAVAILABLE', detail=str(error))
        except Exception as error:
            observation = TreeObservation('ERROR', detail=str(error))
        allowed = (('ENTRIES', 'UNREADABLE', 'MISSING', 'UNAVAILABLE', 'ERROR')
                   if operation == 'list' else
                   ('FILE', 'DIRECTORY', 'SYMLINK', 'MISSING', 'UNAVAILABLE', 'ERROR'))
        if not isinstance(observation, TreeObservation) or observation.status not in allowed:
            observation = TreeObservation('ERROR', detail='invalid tree observation')
        if observation.status == 'ENTRIES' and any(
                type(name) is not str or not name or '/' in name or name in ('.', '..')
                for name in observation.entries):
            observation = TreeObservation('ERROR', detail='invalid directory entry')
        if observation.status == 'SYMLINK' and observation.target_kind not in (
                'FILE', 'DIRECTORY', 'MISSING'):
            observation = TreeObservation('ERROR', detail='invalid symlink target kind')
        result.tree_records.append(TreeRecord(request, observation))
        if observation.status == 'UNAVAILABLE':
            raise Unsupported(origin, 'tree ' + operation + ' unavailable: ' + path)
        if observation.status == 'ERROR':
            raise SemanticError(origin, 'tree ' + operation + ' failed: ' + path
                                + ' (' + str(observation.detail) + ')')
        return observation

    def execute(self, node, evaluator, result):
        if node.body is None:
            raise Unsupported(node, ':tree without a body is deferred')
        raw = render_value(node.arguments, evaluator.python)
        expanded = expand_text(raw, evaluator.scope, node, item_attributes=True)
        parsed = items(expanded, node, label='tree')
        if len(parsed) != 1 or not parsed[0][0]:
            raise SemanticError(node, ':tree requires one directory name')
        root, attrs = parsed[0]
        if (set(attrs) not in (set(('filename',)), set(('filename', 'reject')))
                or type(attrs['filename']) is not str):
            raise Unsupported(node, 'only :tree filename and reject attributes are supported')
        if 'reject' in attrs and type(attrs['reject']) is not str:
            raise Unsupported(node, ':tree reject attribute must be a string')
        if not attrs['filename']:
            raise Unsupported(node, 'empty :tree filename pattern is deferred')
        if not root or '\x00' in root:
            raise SemanticError(node, 'invalid :tree directory name')
        if root.startswith('~'):
            raise Unsupported(node, ':tree user-directory expansion is deferred')
        if not posixpath.isabs(root):
            if type(evaluator.cwd) is not str or not posixpath.isabs(evaluator.cwd):
                raise Unsupported(node, ':tree requires an explicit absolute cwd')
        try:
            pattern = re.compile('^(' + attrs['filename'] + ')$')
        except Exception as error:
            raise SemanticError(node, 'invalid :tree filename pattern: ' + str(error))
        reject = None
        if attrs.get('reject'):
            try:
                reject = re.compile('^(' + attrs['reject'] + ')$')
            except Exception as error:
                raise SemanticError(node, 'invalid :tree reject pattern: ' + str(error))
        self._recurse(root, pattern, reject, node, evaluator, result)

    def _recurse(self, directory, pattern, reject, node, evaluator, result):
        listing = self._observe('list', directory, evaluator, node, result)
        # Upstream warns and returns on os.listdir failure. The record is the
        # controlled warning observation; no body executes for that directory.
        if listing.status in ('UNREADABLE', 'MISSING'):
            return
        for basename in listing.entries:
            path = posixpath.join(directory, basename)
            kind = self._observe('classify', path, evaluator, node, result)
            if kind.status == 'MISSING':
                continue
            effective = kind.target_kind if kind.status == 'SYMLINK' else kind.status
            if effective == 'DIRECTORY' and kind.status != 'SYMLINK':
                self._recurse(path, pattern, reject, node, evaluator, result)
            # This bounded filename/reject subset invokes bodies only for files.
            if (effective != 'FILE' or pattern.match(basename) is None
                    or (reject is not None and reject.match(basename) is not None)):
                continue
            result.tree_records[-1].matched = True
            # Process reparses the saved command string for each match and
            # writes name into the current build dictionary. Its restoration
            # occurs only after Process returns normally.
            previous = evaluator.scope.lookup('name')
            evaluator.scope.store('name', _quote_item(path), node)
            body = lower_body(node.body)
            evaluator.prepare(body)
            evaluator.statements(body, result)
            if previous is None or previous is MISSING:
                del evaluator.scope.local['name']
            else:
                evaluator.scope.store('name', previous, node)
''')

_MANIFEST['aap_semantics.values'] = ('tests/src/aap_semantics/values.py', False, '648951eaf5fb5f695ab0d788795ea725c99182d3b6d38980a80485669bb617fa')
_EMBEDDED['aap_semantics.values'] = ('tests/src/aap_semantics/values.py', False, r'''"""Closed metadata value domain and characterized A-A-P conversions."""
from .diagnostics import SemanticError, Unsupported


class _Missing(object):
    def __bool__(self):
        raise TypeError('MISSING has no truth value')


MISSING = _Missing()


class UnavailableValue(object):
    """A reserved runtime value whose compatibility representation is gated."""
    def __init__(self, reason):
        self.reason = reason


class DeferredExpansion(object):
    def __init__(self, raw, origin):
        self.raw = raw
        self.origin = origin


class RegexMatchValue(object):
    """Opaque truthy result for the reached re.search compatibility call."""
    __slots__ = ()

    def __bool__(self):
        return True


def check_value(value, origin, active=None):
    """Reject host objects/subclasses and cycles; preserve safe list aliases."""
    if isinstance(value, UnavailableValue):
        raise Unsupported(origin, value.reason)
    if type(value) is RegexMatchValue:
        return value
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) in (list, tuple):
        active = set() if active is None else active
        if id(value) in active:
            raise Unsupported(origin, 'cyclic metadata values are unsupported')
        active.add(id(value))
        for item in value:
            check_value(item, origin, active)
        active.remove(id(value))
        return value
    raise Unsupported(origin, 'unsupported metadata value type: ' + type(value).__name__)


def truth(value, origin):
    check_value(value, origin)
    return bool(value)


def scalar_string(value, origin):
    if type(value) not in (str, int, bool, type(None)):
        raise Unsupported(origin, 'scalar string conversion requires a scalar value')
    return str(value)


def _quote_item(text, escaped=" \t", trailing=""):
    # Dictlist.listitem2str's quote switching; backslash is ordinary text.
    quote = ''
    for index, char in enumerate(text):
        if char == "'":
            quote = '"'
            break
        if char == '"':
            quote = "'"
            break
        if char in escaped and (index + 1 < len(text) or char not in trailing):
            quote = '"'
    result = quote
    for index, char in enumerate(text):
        if char in "'\"" + escaped and (index + 1 < len(text) or char not in trailing):
            if char == quote:
                result += quote
                quote = ''
            if not quote:
                quote = "'" if char == '"' else '"'
                result += quote
        result += char
    return result + quote


def var2string(value, origin):
    if isinstance(value, DeferredExpansion):
        raise Unsupported(origin, 'recursive/deferred dollar expansion is not implemented')
    check_value(value, origin)
    if value is None:
        return ''
    if type(value) is list:
        result = ''
        for item in value:
            if result:
                result += ' '
            result += _quote_item(scalar_string(item, origin))
        return result
    if type(value) is tuple:
        raise Unsupported(origin, 'tuple string conversion has not been characterized')
    return scalar_string(value, origin)


def var2list(value, origin):
    # RecPython.var2list directly calls str2list, NOT var2string first.
    check_value(value, origin)
    if not value:
        return []
    if type(value) is not str:
        raise Unsupported(origin, 'var2list requires a string (or an empty value)')
    result = []
    item = []
    quote = ''
    for char in value:
        if quote:
            if char == quote:
                quote = ''
            else:
                item.append(char)
        elif char in "'\"":
            quote = char
        elif char == '{':
            raise Unsupported(origin, 'attribute-bearing item conversion is deferred')
        elif char in ' \t\n':
            if item:
                result.append(''.join(item))
                item = []
        else:
            item.append(char)
    if quote:
        raise SemanticError(origin, 'missing quote in var2list input')
    if item:
        result.append(''.join(item))
    return result
''')

_MANIFEST['aap_semantics.work'] = ('tests/src/aap_semantics/work.py', False, 'ad7900c7f030dc9f2d2d08603b5bb1b3f912646437f71177046bcf4d4f34e218')
_EMBEDDED['aap_semantics.work'] = ('tests/src/aap_semantics/work.py', False, r'''"""Interpreter-owned Work identity, separate from source IDs and recipe values."""


class WorkIdentity(object):
    """Entry loader supplies the historical top_recipe spelling explicitly.

    Relative names use the active logical cwd, as in Port.port_makesum.
    Includes and deferred-body source IDs never set or replace this identity.
    This object grants no filesystem access by itself.
    """
    def __init__(self, top_recipe=None):
        self.top_recipe = top_recipe
''')

_MANIFEST['output_adapter'] = ('tests/adapters/output_adapter.py', False, 'c6851ee9d97b7c96267f8966172819f508bbafe62e5c527d99ab0b8e167a8e02')
_EMBEDDED['output_adapter'] = ('tests/adapters/output_adapter.py', False, r'''"""Real byte-output capability for disposable local integration adapters."""
from __future__ import print_function

import binascii
import hashlib
import os
import posixpath
import sys

from aap_semantics import MemoryOutputSink, OutputResult


class StdoutOutputSink(MemoryOutputSink):
    """Write ordinary print request bytes to this CLI process's stdout.

    The binary stream is looked up for each event so inherited pipes and file
    redirections remain the actual destination.  Events remain observable.
    """
    def __init__(self, stream_provider=None):
        super(StdoutOutputSink, self).__init__()
        self.stream_provider = stream_provider or self._binary_stdout

    def _binary_stdout(self):
        stream = sys.stdout
        binary = getattr(stream, 'buffer', None)
        if binary is None:
            raise IOError('CLI stdout has no binary stream')
        return binary

    def emit(self, request):
        self.events.append(request)
        if type(request.data) is not bytes:
            raise TypeError('ordinary print output must be encoded bytes')
        stream = self.stream_provider()
        offset = 0
        while offset < len(request.data):
            written = stream.write(request.data[offset:])
            if not isinstance(written, int) or written <= 0:
                raise IOError('short CLI stdout write')
            offset += written
        stream.flush()
        return OutputResult()


class LocalByteSession(object):
    """Opaque session retaining ordered binary writes and explicit close."""
    def __init__(self, stream, path, mode, record):
        self._stream = stream
        self._path = path
        self._mode = mode
        self._record = record
        self._closed = False
        self._operation = 0

    def write(self, data):
        if self._closed:
            raise ValueError('byte output session is closed')
        if type(data) is not bytes:
            raise TypeError('byte output session accepts bytes only')
        self._operation += 1
        try:
            written = self._stream.write(data)
            if written != len(data):
                raise IOError('short byte output write')
        except Exception as error:
            self._record('byte_output_write', path=self._path, mode=self._mode,
                         operation=self._operation, status='FAILED',
                         bytes=len(data), detail=str(error))
            raise
        self._record('byte_output_write', path=self._path, mode=self._mode,
                     operation=self._operation, status='COMPLETED',
                     bytes=len(data),
                     data_hex=binascii.hexlify(data).decode('ascii'))

    def close(self):
        if self._closed:
            raise ValueError('byte output session is already closed')
        self._operation += 1
        try:
            self._stream.close()
        except Exception as error:
            self._record('byte_output_close', path=self._path, mode=self._mode,
                         operation=self._operation, status='FAILED',
                         detail=str(error))
            raise
        self._closed = True
        self._record('byte_output_close', path=self._path, mode=self._mode,
                     operation=self._operation, status='COMPLETED',
                     after=LocalByteWriter._snapshot(self._path))


class LocalByteWriter(object):
    """Adapt a resolved shell-helper request to a local file byte session.

    This lives in the integration adapter. Semantic/runtime code only sees the
    request and the opaque write/close session returned here.
    """
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def _path(self, request):
        path = request.path
        if not posixpath.isabs(path):
            if type(request.cwd) is not str or not posixpath.isabs(request.cwd):
                raise ValueError('byte output requires an absolute logical cwd')
            path = posixpath.join(request.cwd, path)
        return path

    @staticmethod
    def _mode(request):
        """Bridge the established shell-helper and :cat request vocabularies."""
        mode = getattr(request, 'mode', None)
        if mode in ('w', 'a'):
            return mode
        destination = getattr(request, 'destination', None)
        if destination == 'overwrite':
            return 'w'
        if destination == 'append':
            return 'a'
        raise ValueError('unsupported byte output mode: ' + str(mode or destination))

    @staticmethod
    def _snapshot(path):
        """Best-effort evidence only; a tracing failure must not block output."""
        if not os.path.lexists(path):
            return {'kind': 'missing'}
        if not os.path.isfile(path):
            return {'kind': 'non-regular'}
        try:
            with open(path, 'rb') as stream:
                data = stream.read()
            return {'kind': 'regular', 'bytes': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'data_hex': binascii.hexlify(data).decode('ascii')}
        except Exception as error:
            return {'kind': 'regular', 'observation_error': str(error)}

    def open_bytes(self, request):
        path = self._path(request)
        mode = self._mode(request)
        modes = {'w': 'wb', 'a': 'ab'}
        self._record('byte_output_open', path=path, requested_path=request.path,
                     cwd=request.cwd, mode=mode, status='STARTED',
                     before=self._snapshot(path))
        try:
            stream = open(path, modes[mode])
        except Exception as error:
            self._record('byte_output_open', path=path,
                         requested_path=request.path, cwd=request.cwd,
                         mode=mode, status='FAILED', detail=str(error))
            raise
        self._record('byte_output_open', path=path, requested_path=request.path,
                     cwd=request.cwd, mode=mode, status='COMPLETED')
        return LocalByteSession(stream, path, mode, self._record)
''')

_MANIFEST['fetch_adapter'] = ('tests/adapters/fetch_adapter.py', False, '27d0c4c0763c7a5a384f47981a2d5b32cb5e7076b596e128e2cd6eaaf6f70508')
_EMBEDDED['fetch_adapter'] = ('tests/adapters/fetch_adapter.py', False, r'''"""Real byte acquisition adapter for the bounded port fetch capability."""
from __future__ import print_function

import hashlib
import http.client
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request

from aap_semantics.fetch import FetchBackend, FetchAttempt, FetchResult


class LocalFetchBackend(FetchBackend):
    def __init__(self, record=None, timeout=20):
        self.record = record if record is not None else (lambda kind, **fields: None)
        self.timeout = timeout

    def _fingerprint(self, path):
        digest = hashlib.sha256()
        count = 0
        with open(path, 'rb') as stream:
            while True:
                chunk = stream.read(32768)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
        return count, digest.hexdigest()

    def _file(self, request, candidate):
        # VersCont.separate_scheme removes file:// and Cache.local_name then
        # resolves the remainder from the process cwd without percent decoding.
        name = candidate[len('file://'):]
        source = os.path.expanduser(name)
        if not os.path.isabs(source):
            source = os.path.join(request.cwd, source)
        if not os.path.exists(source):
            return FetchAttempt(candidate, 'FAILED', 'local source missing: ' + source)
        try:
            shutil.copyfile(source, request.destination)
            count, digest = self._fingerprint(request.destination)
            return FetchAttempt(candidate, 'COMPLETED', count=count, sha256=digest)
        except OSError as error:
            # Historical local_name -> shutil.copyfile raises instead of
            # trying another candidate; a partial destination may remain.
            return FetchAttempt(candidate, 'FATAL', 'local copy failed: ' + str(error))

    def _remote(self, request, candidate):
        descriptor, temporary = tempfile.mkstemp(prefix='aap-fetch-')
        os.close(descriptor)
        try:
            try:
                with urllib.request.urlopen(candidate, timeout=self.timeout) as response:
                    resolved = response.geturl()
                    if not resolved.startswith(('http://', 'https://')):
                        raise IOError('redirected to unsupported scheme: ' + resolved)
                    declared = response.info().get('Content-Length')
                    expected = int(declared) if declared and declared.isdigit() else None
                    received = 0
                    with open(temporary, 'wb') as target:
                        while True:
                            block = response.read(32768)
                            if not block:
                                break
                            target.write(block)
                            received += len(block)
                    if expected is not None and received < expected:
                        raise IOError('incomplete remote transfer: %d of %d bytes'
                                      % (received, expected))
                    with open(temporary, 'rb') as downloaded:
                        if re.search(br'<title>\s*404\s*not\s*found',
                                     downloaded.read(1000), re.I):
                            raise IOError('remote page contains a 404 title')
            except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                return FetchAttempt(candidate, 'FAILED', 'remote download failed: ' + str(error))
            try:
                shutil.copyfile(temporary, request.destination)
                count, digest = self._fingerprint(request.destination)
                return FetchAttempt(candidate, 'COMPLETED',
                                    'resolved URL: ' + resolved,
                                    count=count, sha256=digest)
            except OSError as error:
                # As in Remote.download_file, a final copy failure ends the
                # attempt and can leave partial destination bytes.
                return FetchAttempt(candidate, 'FATAL',
                                    'destination copy failed: ' + str(error))
        finally:
            os.unlink(temporary)

    def fetch(self, request):
        self.record('fetch_request', destination=request.destination,
                    candidates=list(request.candidates), cwd=request.cwd,
                    filename=request.filename, sites=request.sites)
        parent = os.path.dirname(request.destination)
        try:
            if not os.path.isdir(parent):
                os.makedirs(parent)
        except OSError as error:
            result = FetchResult('FAILED', (),
                detail='cannot create destination directory: ' + str(error))
            self.record('fetch_result', destination=request.destination,
                        status=result.status, selected=None, detail=result.detail)
            return result
        attempts = []
        for candidate in request.candidates:
            if candidate.startswith('file://'):
                attempt = self._file(request, candidate)
            elif candidate.startswith('http://') or candidate.startswith('https://'):
                attempt = self._remote(request, candidate)
            else:
                raise NotImplementedError('port fetch scheme unavailable: ' + candidate)
            attempts.append(attempt)
            self.record('fetch_attempt', candidate=candidate, status=attempt.status,
                        detail=attempt.detail, bytes=attempt.count,
                        sha256=attempt.sha256, destination=request.destination)
            if attempt.status == 'COMPLETED':
                result = FetchResult('COMPLETED', attempts, selected=candidate)
                self.record('fetch_result', destination=request.destination,
                            status=result.status, selected=candidate)
                return result
            if attempt.status == 'FATAL':
                break
        detail = (attempts[-1].detail if attempts and attempts[-1].status == 'FATAL'
                  else 'all usable fetch candidates failed')
        result = FetchResult('FAILED', attempts, detail=detail)
        self.record('fetch_result', destination=request.destination,
                    status=result.status, selected=None, detail=result.detail)
        return result
''')

_MANIFEST['process_adapter'] = ('tests/adapters/process_adapter.py', False, '4d76b569a62419cfd7386e7a7aca97cf1f3fe8e152b9bdfdc35ba0977786ef19')
_EMBEDDED['process_adapter'] = ('tests/adapters/process_adapter.py', False, r'''"""Real POSIX process execution for the bounded A-A-P process requests."""
from __future__ import print_function

import os
import shlex
import subprocess
import tempfile

from aap_semantics import ProcessBackend, ProcessResult


class PosixProcessBackend(ProcessBackend):
    def run(self, request):
        return self.run_observed(request, None)

    def run_observed(self, request, started):
        """Optional spawn callback lets an outer observer retain event order."""
        if getattr(request, 'logging', False):
            return self._run_logged(request, started)
        process = subprocess.Popen(request.shell_command_bytes, shell=True,
            executable='/bin/sh', cwd=request.cwd_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        if started is not None:
            started(process.pid, request.shell_command)
        stdout, stderr = process.communicate()
        return ProcessResult(process.returncode << 8, stdout, stderr)

    def _run_logged(self, request, started):
        """POSIX logged_system {l}: merge output, recover $? and clean temps."""
        if not request.log_path or not os.path.isabs(request.log_path):
            raise ValueError('logged :sys requires an absolute log path')
        directory = os.path.dirname(request.log_path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        output_fd, output_path = tempfile.mkstemp(prefix='py3aap-sys-output-',
                                                  dir=directory)
        os.close(output_fd)
        status_path = None
        try:
            status_fd, status_path = tempfile.mkstemp(prefix='py3aap-sys-status-',
                                                      dir=directory)
            os.close(status_fd)
            # Util.logged_system constructs this shell wrapper when {l} is
            # present. mkstemp replaces upstream's insecure tempfile.mktemp.
            command = request.command
            wrapper = ('{ ' + command + ' 2>&1; echo $? > ' + shlex.quote(status_path) +
                       '; } 2>&1 >' + shlex.quote(output_path) + '\n')
            with open(request.log_path, 'ab') as logfile:
                kind = b'log' if request.quiet else b'system'
                logfile.write(kind + b':\t' + request.log_command_text.encode('latin-1') + b'\n')
            process = subprocess.Popen(wrapper.encode('latin-1'), shell=True,
                executable='/bin/sh', cwd=request.cwd_bytes,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if started is not None:
                started(process.pid, wrapper)
            stdout, stderr = process.communicate()
            with open(status_path, 'rb') as stream:
                status_text = stream.read().strip()
            if not status_text.isdigit():
                raise ValueError('logged :sys status file has no shell status')
            with open(output_path, 'rb') as stream:
                merged = stream.read()
            if merged:
                with open(request.log_path, 'ab') as logfile:
                    logfile.write(b'log:\t' + merged + (b'' if merged.endswith(b'\n') else b'\n'))
            status = int(status_text)
            return ProcessResult(status, stdout, stderr, merged,
                                 process.returncode << 8)
        finally:
            os.unlink(output_path)
            if status_path is not None:
                os.unlink(status_path)
''')

_MANIFEST['process_tracing'] = ('tests/adapters/process_tracing.py', False, '447ecac3e1c2f981afe1526a71530f5fb15174c68ce8794e7cab143e42be5c14')
_EMBEDDED['process_tracing'] = ('tests/adapters/process_tracing.py', False, r'''"""Read-only integration evidence around an injected process backend."""
from __future__ import print_function

import hashlib
import os

from aap_semantics import ProcessBackend


def patch_observations(environment):
    observations = []
    for path in environment.get('AAP_RECURSIVE_PATCH_OBSERVE', '').split(os.pathsep):
        if not path:
            continue
        if not os.path.isfile(path):
            observations.append({'path': path, 'exists': False})
            continue
        digest = hashlib.sha256()
        size = 0
        with open(path, 'rb') as stream:
            while True:
                block = stream.read(32768)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        observations.append({'path': path, 'exists': True, 'bytes': size,
                             'sha256': digest.hexdigest()})
    return observations


def _read_observed_file(path):
    try:
        with open(path, 'rb') as stream:
            data = stream.read()
    except IOError:
        return None, {'path': path, 'exists': False, 'bytes': None,
                      'sha256': None}
    return data, {'path': path, 'exists': True, 'bytes': len(data),
                  'sha256': hashlib.sha256(data).hexdigest()}


def _append_request_observation(request):
    """Read-only integration evidence for simple command >> target requests."""
    expression = getattr(request, 'expression', None)
    if expression is None or expression.kind != 'SIMPLE' or not expression.redirections:
        return None
    if len(expression.redirections) != 1 or expression.redirections[0].operator != '>>':
        return None
    if len(expression.argv) < 2:
        return None
    source_path = expression.argv[1]
    target_path = expression.redirections[0].target
    if not os.path.isabs(source_path):
        source_path = os.path.join(request.cwd, source_path)
    if not os.path.isabs(target_path):
        target_path = os.path.join(request.cwd, target_path)
    source_bytes, source_info = _read_observed_file(source_path)
    destination_bytes, destination_info = _read_observed_file(target_path)
    span = getattr(request, 'span', None)
    source_span = None
    if span is not None:
        source_span = {'source': getattr(span, 'source_id', None),
                       'line': getattr(getattr(span, 'start', None), 'line', None),
                       'column': getattr(getattr(span, 'start', None), 'column', None)}
    batch = getattr(request, 'batch', None)
    return {'source_bytes': source_bytes, 'destination_bytes': destination_bytes,
            'source': source_info, 'destination_before': destination_info,
            'destination_path': target_path, 'source_span': source_span,
            'batch_entry_count': len(getattr(batch, 'entries', ())) if batch else None}


def _process_file_observations(request, environment):
    """Read explicitly requested paths around a real process request."""
    observations = []
    for argument in environment.get('AAP_RECURSIVE_PROCESS_OBSERVE', '').split(os.pathsep):
        if not argument:
            continue
        path = argument if os.path.isabs(argument) else os.path.join(request.cwd, argument)
        exists = os.path.exists(path)
        item = {'argument': argument, 'path': path, 'exists': exists,
                'bytes': None, 'sha256': None}
        if exists and os.path.isfile(path):
            try:
                digest = hashlib.sha256()
                size = 0
                with open(path, 'rb') as stream:
                    while True:
                        block = stream.read(32768)
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                item['bytes'] = size
                item['sha256'] = digest.hexdigest()
            except IOError as error:
                item['observation_error'] = str(error)
        observations.append(item)
    return observations


class TracingProcessBackend(ProcessBackend):
    def __init__(self, inner, record, environment, enabled=True):
        self.inner, self.record = inner, record
        self.environment, self.enabled = environment, enabled

    def run(self, request):
        logged = getattr(request, 'logging', False)
        patch = getattr(request, 'operation', None) == 'port_patch'
        append = _append_request_observation(request) if self.enabled else None
        files_before = (_process_file_observations(request, self.environment)
                        if self.enabled else [])
        if files_before:
            self.record('process_file_observation', phase='before',
                        command=request.command, cwd=request.cwd, files=files_before)
        if append is not None:
            self.record('append_redirection_observation', phase='before',
                        command=request.command, cwd=request.cwd,
                        source_span=append['source_span'],
                        batch_entry_count=append['batch_entry_count'],
                        source=append['source'],
                        destination_before=append['destination_before'],
                        destination=append['destination_path'])
        if patch and not logged:
            self.record('patch_observation', phase='before',
                        files=patch_observations(self.environment))
        spawned = []

        def started(pid, shell_command):
            spawned.append(pid)
            if logged:
                self.record('process_start', shell_pid=pid, cwd=request.cwd,
                            command=request.command, shell_command=shell_command,
                            logging=True, quiet=request.quiet, force=request.force,
                            log_path=request.log_path)
            else:
                self.record('process_start', shell_pid=pid, cwd=request.cwd,
                            command=request.command,
                            operation=getattr(request, 'operation', None),
                            shell_command=shell_command,
                            environment_policy=getattr(request, 'environment_policy',
                                                       'inherit-backend'),
                            sentinel=self.environment.get('AAP_RECURSIVE_SENTINEL'),
                            recipe_aap=self.environment.get('AAP'))

        if hasattr(self.inner, 'run_observed'):
            result = self.inner.run_observed(request, started)
        else:
            started(None, request.shell_command)
            result = self.inner.run(request)
        pid = spawned[-1]
        if logged:
            shell_wait = result.shell_wait_status
            self.record('process_exit', shell_pid=pid, cwd=request.cwd,
                        command=request.command, logging=True,
                        quiet=request.quiet, force=request.force,
                        log_path=request.log_path,
                        returncode=shell_wait >> 8,
                        shell_wait_status=shell_wait,
                        wait_status=result.wait_status,
                        stdout=result.stdout.decode('latin-1'),
                        stderr=result.stderr.decode('latin-1'),
                        logged_output=result.log_output.decode('latin-1'))
        else:
            returncode = result.wait_status >> 8
            self.record('process_exit', shell_pid=pid, cwd=request.cwd,
                        command=request.command,
                        operation=getattr(request, 'operation', None),
                        returncode=returncode, wait_status=result.wait_status,
                        stdout=result.stdout.decode('latin-1'),
                        stderr=result.stderr.decode('latin-1'))
            if patch:
                self.record('patch_observation', phase='after',
                            files=patch_observations(self.environment))
            if append is not None:
                destination_after_bytes, destination_after = _read_observed_file(
                    append['destination_path'])
                source_bytes = append['source_bytes']
                before_bytes = append['destination_bytes']
                expected = None
                if source_bytes is not None:
                    expected = (before_bytes if before_bytes is not None else b'') + source_bytes
                self.record('append_redirection_observation', phase='after',
                            command=request.command, cwd=request.cwd,
                            source_span=append['source_span'],
                            batch_entry_count=append['batch_entry_count'],
                            source=append['source'],
                            destination_before=append['destination_before'],
                            destination_after=destination_after,
                            exact_append=(expected is not None and
                                          destination_after_bytes == expected),
                            process_status=returncode)
        if files_before:
            self.record('process_file_observation', phase='after',
                        command=request.command, cwd=request.cwd,
                        files=_process_file_observations(request, self.environment))
        return result
''')

_MANIFEST['host_filesystem'] = ('tests/adapters/host_filesystem.py', False, '17c8576a952717e6ba55cebe3c8d600cb37777dd07df122795a2f51ce1773ff4')
_EMBEDDED['host_filesystem'] = ('tests/adapters/host_filesystem.py', False, r'''"""Real host filesystem capabilities for the integration CLI."""
from __future__ import print_function

import hashlib
import os
import shutil

from aap_semantics import (ActionWorkspace, ArtifactBackend, CopyBackend, CopyObservation, CopyResult, DeleteBackend, DeleteResult, FileState, MarkerBackend, MoveBackend, MoveResult, OutputResult, PathObservation, PathObserver, PortDirectories, TargetStateBackend, TreeFilesystem, TreeObservation)
from output_adapter import LocalByteWriter


class Files(ArtifactBackend):
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def exists(self, path):
        return os.path.isfile(path)

    def chunks(self, path):
        self._record('artifact_read', path=path, status='STARTED')
        if not os.path.exists(path):
            error = OSError('artifact is missing: ' + path)
            self._record('artifact_read', path=path, path_kind='missing',
                         status='FAILED', detail=str(error))
            raise error
        if not os.path.isfile(path):
            error = OSError('artifact is not a regular file: ' + path)
            self._record('artifact_read', path=path, path_kind='non-regular',
                         status='FAILED', detail=str(error))
            raise error
        digest, count = hashlib.sha256(), 0
        try:
            with open(path, 'rb') as stream:
                while True:
                    data = stream.read(32768)
                    if not data:
                        break
                    digest.update(data)
                    count += len(data)
                    yield data
        except Exception as error:
            self._record('artifact_read', path=path, path_kind='regular',
                         status='FAILED', bytes=count, detail=str(error))
            raise
        self._record('artifact_read', path=path, path_kind='regular',
                     status='COMPLETED', bytes=count,
                     sha256=digest.hexdigest())


class Directories(PortDirectories):
    def enter(self, path):
        if not os.path.isdir(path):
            raise OSError('directory missing or inaccessible: ' + path)
        return os.path.realpath(path)


class Workspace(ActionWorkspace):
    def prepare_directory(self, path):
        if not os.path.isdir(path):
            os.makedirs(path)
        return os.path.realpath(path)

    def file_kind(self, path):
        if os.path.isdir(path):
            return 'directory'
        return 'file' if os.path.isfile(path) else 'missing'

    def filetype(self, path):
        return None


class Markers(MarkerBackend):
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def marker_exists(self, path):
        exists = os.path.exists(path)
        self._record('marker_lookup', path=path, exists=exists)
        return exists

    def path_kind(self, path):
        if not os.path.exists(path):
            return 'missing'
        return 'directory' if os.path.isdir(path) else 'other'

    def mkdir(self, path, mode=None, require_parent=False):
        if require_parent:
            if mode is None:
                os.mkdir(path)
            else:
                os.mkdir(path, mode)
            return
        if mode is not None:
            os.mkdir(path, mode)
            return
        if not os.path.isdir(path):
            os.makedirs(path)

    def touch(self, path):
        with open(path, 'ab'):
            pass
        os.utime(path, None)

    def create_exclusive(self, path):
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        os.close(descriptor)


class Paths(PathObserver):
    def observe(self, request):
        return PathObservation('EXISTS' if os.path.exists(request.path) else 'MISSING')


class Writer(LocalByteWriter):
    def __init__(self, record=None):
        super(Writer, self).__init__(record if record is not None else
                                      (lambda kind, **fields: None))

    def write(self, request):
        parent = os.path.dirname(request.path)
        if not os.path.isdir(parent):
            raise OSError('output parent unavailable: ' + parent)
        mode = 'wb' if request.destination == 'overwrite' else 'ab'
        with open(request.path, mode) as stream:
            stream.write(request.data)
        return OutputResult()


class Trees(TreeFilesystem):
    def list_directory(self, request):
        try:
            return TreeObservation('ENTRIES', os.listdir(request.path))
        except FileNotFoundError:
            return TreeObservation('MISSING')
        except OSError as error:
            return TreeObservation('UNREADABLE', detail=str(error))

    def classify(self, request):
        try:
            if os.path.islink(request.path):
                if os.path.isdir(request.path):
                    target = 'DIRECTORY'
                elif os.path.isfile(request.path):
                    target = 'FILE'
                else:
                    target = 'MISSING'
                return TreeObservation('SYMLINK', target_kind=target)
            if os.path.isdir(request.path):
                return TreeObservation('DIRECTORY')
            if os.path.isfile(request.path):
                return TreeObservation('FILE')
            return TreeObservation('MISSING')
        except OSError as error:
            return TreeObservation('ERROR', detail=str(error))


class Delete(DeleteBackend):
    def delete_tree(self, request):
        try:
            if os.path.islink(request.path) or os.path.isfile(request.path):
                os.unlink(request.path)
            elif os.path.isdir(request.path):
                shutil.rmtree(request.path)
            else:
                return DeleteResult('FAILED', 'path disappeared before deletion')
            return DeleteResult('COMPLETED')
        except OSError as error:
            return DeleteResult('FAILED', str(error))


class Move(MoveBackend):
    def move(self, request):
        try:
            os.rename(request.source, request.destination)
            return MoveResult()
        except OSError as error:
            return MoveResult('FAILED', str(error))


class Copy(CopyBackend):
    """Real local regular-file adapter for the bounded :copy runtime."""
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def path_kind(self, path):
        if os.path.isfile(path):
            outcome = CopyObservation(kind='regular')
        elif os.path.isdir(path):
            outcome = CopyObservation(kind='directory')
        elif os.path.lexists(path):
            outcome = CopyObservation(kind='other')
        else:
            outcome = CopyObservation(kind='missing')
        self._record('copy_observation', path=path, path_kind=outcome.kind)
        return outcome

    def _fingerprint(self, path):
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            while True:
                data = stream.read(32768)
                if not data:
                    break
                digest.update(data)
        return {'bytes': os.path.getsize(path), 'sha256': digest.hexdigest()}

    def copy(self, request):
        self._record('copy_request', source=request.source,
                     destination_argument=request.destination_argument,
                     effective_destination=request.effective_destination,
                     cwd=request.cwd, source_kind=request.source_kind,
                     destination_kind=request.destination_kind,
                     overwrite=request.overwrite)
        try:
            shutil.copy(request.source, request.effective_destination)
            self._record('copy_complete', source=request.source,
                         effective_destination=request.effective_destination,
                         source_present=os.path.isfile(request.source),
                         destination_present=os.path.isfile(request.effective_destination),
                         source_fingerprint=self._fingerprint(request.source),
                         destination_fingerprint=self._fingerprint(request.effective_destination))
            return CopyResult()
        except (IOError, OSError) as error:
            self._record('copy_failed', source=request.source,
                         effective_destination=request.effective_destination,
                         detail=str(error))
            return CopyResult('FAILED', str(error))


class HostState(TargetStateBackend):
    def file_state(self, node):
        path = node.path
        try:
            value = os.stat(path)
            return FileState(True, value.st_mtime, os.path.isdir(path))
        except OSError:
            return FileState(False, 0, False)

    def current_signature(self, node, check):
        state = self.file_state(node)
        if check == 'time':
            return str(state.mtime if state.exists else 0)
        if check == 'none':
            return 'unknown'
        if check in ('md5', 'c_md5'):
            if not state.exists or state.directory:
                return 'unknown'
            digest = hashlib.md5()
            with open(node.path, 'rb') as stream:
                while True:
                    data = stream.read(32768)
                    if not data:
                        break
                    digest.update(data)
            return digest.hexdigest()
        return None

    def stored_signature(self, target, source, check):
        return ''

    def build_signature(self, definition, target):
        return None

    def implicit_dependencies(self, node):
        return False

    def matching_rule(self, node):
        return False

''')

_MANIFEST['host_persistence'] = ('tests/adapters/host_persistence.py', False, 'adfaabd4c3ed0641a3cf0842ceacdb453deae2011523b6f3d1e25c1127d8e529')
_EMBEDDED['host_persistence'] = ('tests/adapters/host_persistence.py', False, r'''"""Real JSON signature persistence for the integration CLI."""
from __future__ import print_function

import json
import os
import time

from aap_semantics import PersistenceBackend


class DiskPersistence(PersistenceBackend):
    def __init__(self, recipe_dir, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)
        self.path = os.path.join(recipe_dir, 'AAPDIR', 'signatures.json')
        self.targets = {}
        existed = os.path.isfile(self.path)
        size = os.path.getsize(self.path) if existed else 0
        if existed:
            with open(self.path) as stream:
                data = json.load(stream)
            self.targets = data.get('targets', {})
        self._record('persistence_load', path=self.path,
                     existed=existed, bytes=size,
                     target_count=len(self.targets))

    def signature(self, target, source, check):
        for item in self.targets.get(target, {}).get('values', ()):
            if item['source'] == source and item['check'] == check:
                self._record('signature_lookup', target=target, source=source,
                             check=check, found=True, value=item['value'])
                return item['value']
        self._record('signature_lookup', target=target, source=source,
                     check=check, found=False, value='')
        return ''

    def marker_exists(self, path):
        return os.path.exists(path)

    def timestamp(self):
        return str(time.time())

    def flush(self, records):
        for record in records:
            if record.values:
                values = []
                for key, value in sorted(record.values.items()):
                    values.append({'source': key[0], 'check': key[1],
                                   'value': value})
                self.targets[record.target] = {'values': values,
                                               'timestamp': record.timestamp}
            else:
                self.targets.pop(record.target, None)
        parent = os.path.dirname(self.path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        temporary = self.path + '.new'
        with open(temporary, 'w') as stream:
            json.dump({'targets': self.targets}, stream, indent=2,
                      sort_keys=True)
            stream.write('\n')
        os.rename(temporary, self.path)
        self._record('persistence_flush', path=self.path,
                     bytes=os.path.getsize(self.path),
                     record_count=len(records), target_count=len(self.targets))

''')

_MANIFEST['integration_evidence'] = ('tests/adapters/integration_evidence.py', False, '19dc0409ba150e724ee101f2bc83fdf40f291e2c096d8f929b92498129e1935d')
_EMBEDDED['integration_evidence'] = ('tests/adapters/integration_evidence.py', False, r'''"""Structured trace and summary serializers for real integration."""
from __future__ import print_function

import binascii
import hashlib
import json
import os
import time

from aap_semantics import ChecksumBackend


class EventRecorder(object):
    def __init__(self, environment):
        self.environment = environment
        self.path = environment.get('AAP_RECURSIVE_TRACE')

    def __call__(self, kind, **values):
        if not self.path:
            return
        event = {'event': kind, 'pid': os.getpid(), 'ppid': os.getppid(),
                 'time': time.time(),
                 'invocation': self.environment.get('AAP_RECURSIVE_INVOCATION', 'primary')}
        event.update(values)
        with open(self.path, 'a') as stream:
            stream.write(json.dumps(event, sort_keys=True) + '\n')


class TracingChecksum(ChecksumBackend):
    def __init__(self, artifacts, record=None):
        super(TracingChecksum, self).__init__(artifacts)
        self._record = record if record is not None else (lambda kind, **fields: None)

    def md5(self, request):
        computed = super(TracingChecksum, self).md5(request)
        self._record('checksum_digest', path=request.path,
                     expected=request.expected.get('md5'), computed=computed,
                     matches=computed == request.expected.get('md5'))
        return computed


def inventory(recipe_dir, environment=None):
    environment = os.environ if environment is None else environment
    candidates = []
    specs = []
    configure_files = []
    counts = {'distfiles': 0, 'work': 0, 'pack': 0}
    for name in counts:
        base = os.path.join(recipe_dir, name)
        if os.path.isdir(base):
            for directory, subdirs, names in os.walk(base):
                counts[name] += len(names)
                for filename in names:
                    path = os.path.join(directory, filename)
                    if filename.endswith('.spec'):
                        specs.append(path)
                    if filename == 'configure':
                        configure_files.append(path)
    for base in ('/export/company', '/usr/src/packages/RPMS'):
        if not os.path.isdir(base):
            continue
        for directory, subdirs, names in os.walk(base):
            for name in names:
                if name.endswith('.rpm'):
                    candidates.append(os.path.join(directory, name))
    observed = []
    for argument in environment.get('AAP_RECURSIVE_OBSERVE', '').split(os.pathsep):
        if not argument:
            continue
        path = argument if os.path.isabs(argument) else os.path.join(recipe_dir, argument)
        exists = os.path.isfile(path)
        entry = {'argument': argument, 'path': path, 'exists': exists,
                 'bytes': os.path.getsize(path) if exists else None}
        if exists:
            with open(path, 'rb') as stream:
                data = stream.read()
            entry['sha256'] = hashlib.sha256(data).hexdigest()
            entry['hex'] = binascii.hexlify(data).decode('ascii')
        observed.append(entry)
    done_dir = os.path.join(recipe_dir, 'done')
    return {'file_counts': counts,
            'directory_exists': {name: os.path.isdir(os.path.join(recipe_dir, name))
                                 for name in counts},
            'configure_files': sorted(configure_files),
            'spec_files': sorted(specs),
            'observed_files': observed,
            'done': os.path.isdir(done_dir),
            'done_markers': sorted(os.listdir(done_dir)) if os.path.isdir(done_dir) else [],
            'signature_file': os.path.isfile(os.path.join(
                recipe_dir, 'AAPDIR', 'signatures.json')),
            'rpm_candidates': sorted(candidates)}


def span(value):
    if value is None:
        return None
    return {'source': value.source_id, 'line': value.start.line,
            'column': value.start.column}


def tree_record(record):
    """Serialize controlled :tree facts for recursive integration evidence.

    The interpreter has already recorded these facts in its body result.  This
    adapter merely makes that record observable at the external-process
    boundary; it does not perform another filesystem observation.
    """
    request, observation = record.request, record.observation
    return {
        'operation': request.operation,
        'argument': request.argument,
        'path': request.path,
        'cwd': request.cwd,
        'source': request.source.source_id,
        'span': span(request.span),
        'status': observation.status,
        'entries': list(observation.entries),
        'target_kind': observation.target_kind,
        'detail': observation.detail,
        'matched': record.matched,
    }


def directory_change(record):
    """Serialize existing :cd execution-frame records without changing them."""
    return {
        'raw': record.raw,
        'expanded': record.expanded,
        'before': record.before,
        'requested': record.requested,
        'after': record.after,
        'status': record.status,
        'error': str(record.error) if record.error else None,
        'source': record.source.source_id,
        'span': span(record.span),
    }


def body_tree_records(result):
    records = []
    for body in result.bodies:
        records.extend(tree_record(record) for record in body.tree_records)
    return records


def body_directory_changes(result):
    records = []
    for body in result.bodies:
        records.extend(directory_change(record) for record in body.directory_changes)
    return records


def cat_record(record):
    request = record.request
    return {'raw': request.raw, 'raw_destination': request.raw_destination,
            'raw_sources': request.raw_sources,
            'expanded_destination': request.expanded_destination,
            'expanded_sources': request.expanded_sources,
            'destination': request.destination, 'path': request.path,
            'source_items': list(request.source_items), 'paths': list(request.paths),
            'cwd': request.cwd, 'status': record.status, 'phase': record.phase,
            'active_source': record.active_source, 'reads': list(record.reads),
            'bytes_read': record.bytes_read, 'bytes_written': record.bytes_written,
            'opened': record.opened, 'closed': record.closed,
            'error': str(record.error) if record.error else None,
            'close_error': record.close_error, 'message': record.message,
            'source': request.source.source_id, 'span': span(request.span)}


def body_cat_records(result):
    records = []
    for body in result.bodies:
        records.extend(cat_record(record) for record in body.evaluation.cats)
    return records


def body_print_records(result):
    records = []
    for body in result.bodies:
        evaluation = getattr(body, 'evaluation', None)
        if evaluation is None:
            continue
        for item in evaluation.prints:
            request = item.request
            if request.destination != 'stdout':
                continue
            records.append({'target': body.target.name,
                            'text': request.text,
                            'data_hex': binascii.hexlify(request.data).decode('ascii'),
                            'bytes': len(request.data),
                            'destination': request.destination,
                            'status': item.status,
                            'source': request.source.source_id,
                            'span': span(item.span)})
    return records


def body_process_captures(result):
    records = []
    for body in result.bodies:
        evaluation = getattr(body, 'evaluation', None)
        if evaluation is None:
            continue
        for item in evaluation.processes:
            if not hasattr(item, 'pipeline') or item.pipeline.target is None:
                continue
            records.append({'target': body.target.name,
                            'command': item.request.command,
                            'cwd': item.request.cwd,
                            'assignment': item.pipeline.target,
                            'captured_value': item.output,
                            'backend_stdout_hex': binascii.hexlify(
                                item.result.stdout).decode('ascii'),
                            'wait_status': item.result.wait_status,
                            'source': item.source.source_id,
                            'span': span(item.span)})
    return records


def body_decisions(result):
    records = []
    for body in result.bodies:
        step = body.context.step
        records.append({'target': body.target.name, 'status': body.status,
                        'trigger': step.reason, 'virtual': body.target.virtual,
                        'prepared_buildcheck': body.context.prepared_buildcheck})
    return records


def completion_decisions(result):
    return [{'target': item.target.name, 'status': item.status,
             'reason': item.reason,
             'signature_records': len(item.records)}
            for item in result.completions]


def python_write_records(result):
    records = []
    for body in result.bodies:
        evaluation = getattr(body, 'evaluation', None)
        if evaluation is None:
            continue
        for item in evaluation.python_writes:
            request = item.request
            records.append({'operation': item.operation,
                            'status': item.status,
                            'path': request.path,
                            'cwd': request.cwd,
                            'mode': request.mode,
                            'bytes': len(item.data) if item.data is not None else None,
                            'data_hex': (binascii.hexlify(item.data).decode('ascii')
                                         if item.data is not None else None),
                            'source': request.source.source_id,
                            'span': span(item.span)})
    return records
''')

_MANIFEST['runtime_factory'] = ('tests/adapters/runtime_factory.py', False, '07d6ab80ccb4748a2b450ac1a5a0e459cac2d2373614f244ab4357403a9d7bb2')
_EMBEDDED['runtime_factory'] = ('tests/adapters/runtime_factory.py', False, r'''"""Compose real host capabilities for one external A-A-P invocation."""
from __future__ import print_function

import os

from aap_semantics import (ActionRuntime, CatRuntime, OutputPolicy,
    PortCommandPolicy, PortCommandRuntime, PortRuntime, PrintRuntime,
    ProcessPolicy, RuntimeCapabilities, Scope, SourceLoader, WorkIdentity)
from fetch_adapter import LocalFetchBackend
from host_filesystem import (Copy, Delete, Directories, Files, HostState,
                             Markers, Move, Paths, Trees, Workspace, Writer)
from host_persistence import DiskPersistence
from integration_evidence import TracingChecksum
from output_adapter import StdoutOutputSink
from process_adapter import PosixProcessBackend
from process_tracing import TracingProcessBackend


class RuntimeAssembly(object):
    __slots__ = ('scope', 'capabilities', 'metadata_capabilities',
                 'state', 'persistence')

    def __init__(self, scope, capabilities, metadata_capabilities,
                 state, persistence):
        self.scope, self.capabilities = scope, capabilities
        self.metadata_capabilities = metadata_capabilities
        self.state, self.persistence = state, persistence


def initial_scope(cwd, recipe_aap):
    scope = Scope.top_level(port_defaults=True, work=WorkIdentity('main.aap'))
    scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build',
                        'DISTDIR': 'distfiles', 'PATCHDISTDIR': 'patches',
                        'PKGDIR': os.path.join(cwd, 'pack'), 'WRKDIR': 'work',
                        'AAP': recipe_aap, 'gt': '>'})
    return scope


def create_runtime(cwd, environment, record, scope):
    policy = ProcessPolicy('latin-1', sys_mode='bounded-attributes',
                           log_path=os.path.join(cwd, 'AAPDIR', 'log'))
    output_policy = OutputPolicy('latin-1', logging=False)
    shell = TracingProcessBackend(PosixProcessBackend(), record, environment,
                                  enabled=bool(environment.get('AAP_RECURSIVE_TRACE')))
    files, directories = Files(record), Directories()
    paths, markers = Paths(), Markers(record)
    commands = PortCommandRuntime(directories, PortCommandPolicy(False, False))
    runtime = PortRuntime(files, markers, ActionRuntime(Workspace()), commands,
                          deletions=Delete(), fetch_backend=LocalFetchBackend(record))
    loader = SourceLoader('latin-1')
    writer = Writer(record)
    output = PrintRuntime(output_policy, sink=StdoutOutputSink(), writer=writer)
    cat = CatRuntime(output_policy, files, writer)
    capabilities = RuntimeCapabilities(
        process_backend=shell, process_policy=policy, include_loader=loader,
        checksum_backend=TracingChecksum(files, record), port_runtime=runtime,
        path_observer=paths, output_runtime=output, cat_runtime=cat,
        tree_filesystem=Trees(), move_backend=Move(), copy_backend=Copy(record))
    # Top-level metadata has no ordinary print, checksum, or port helper
    # capability in the current CLI baseline. Preserve those phase gates.
    metadata_capabilities = capabilities.replace(
        checksum_backend=None, port_runtime=None, output_runtime=None)
    return RuntimeAssembly(scope, capabilities, metadata_capabilities,
                           HostState(), DiskPersistence(cwd, record))
''')

_MANIFEST['__main__'] = ('tests/adapters/aap_cli.py', False, '9d7ff5bcea4f23647594848142d0ca3516e57d2330ddc4671f5ead7763664b1e')
_EMBEDDED['__main__'] = ('tests/adapters/aap_cli.py', False, r'''#!/usr/bin/env python3
"""Disposable generic CLI adapter for real recursive A-A-P integration.

The adapter deliberately discovers ``main.aap`` from the process cwd.  It is
not a recursive interpreter feature: every ``aap`` command is launched by the
ordinary process backend and starts this program in a new OS process.
"""
from __future__ import print_function

import os
import sys


from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, CliArgumentError, Evaluator,
                           apply_assignments, lower, parse_arguments)
from host_filesystem import Files  # public integration fixture import
from integration_evidence import (EventRecorder, body_cat_records,
    body_decisions, body_directory_changes, body_print_records,
    body_process_captures, body_tree_records, completion_decisions,
    inventory, python_write_records, span)
from process_adapter import PosixProcessBackend as Shell  # fixture import
from runtime_factory import create_runtime, initial_scope


def main(argv):
    environment = os.environ
    record = EventRecorder(environment)
    cwd = os.path.realpath(os.getcwd())
    recipe = os.path.join(cwd, 'main.aap')
    recipe_aap = environment.get('AAP')
    if not os.path.isfile(recipe):
        record('cli_error', cwd=cwd, argv=argv,
                     category='recipe discovery', detail='main.aap is absent')
        print('aap: main.aap is absent in ' + cwd, file=sys.stderr)
        return 2
    try:
        parsed = parse_arguments(argv)
    except CliArgumentError as error:
        record('cli_error', cwd=cwd, argv=argv, category='CLI/entrypoint',
                     detail=str(error))
        print('aap: ' + str(error), file=sys.stderr)
        return 2
    targets = parsed.targets or None
    if not recipe_aap:
        record('cli_error', cwd=cwd, argv=argv, category='launcher',
                     detail='AAP launcher value is absent')
        print('aap: AAP launcher value is absent', file=sys.stderr)
        return 2

    record('cli_start', cwd=cwd, argv=argv, recipe=recipe,
                 top_recipe='main.aap', source_identity=recipe,
                 sentinel=environment.get('AAP_RECURSIVE_SENTINEL'),
                 path=environment.get('PATH'), recipe_aap=recipe_aap,
                 python_executable=sys.executable, entrypoint=sys.argv[0],
                 environment={key: environment.get(key) for key in
                              ('AAP', 'AAP_REPOSITORY', 'AAP_RECURSIVE_SENTINEL',
                               'AAP_RECURSIVE_INVOCATION', 'PATH')})
    scope = initial_scope(cwd, recipe_aap)
    applied_assignments, ignored_assignments = apply_assignments(
        scope, parsed.assignments)
    record('cli_settings', cwd=cwd, argv=argv,
                 assignments=parsed.assignments,
                 applied_assignments=applied_assignments,
                 ignored_assignments=ignored_assignments,
                 targets=parsed.targets)
    runtime = create_runtime(cwd, environment, record, scope)
    source = Source.from_path(recipe, 'latin-1')
    metadata = Evaluator(scope, cwd=cwd,
        capabilities=runtime.metadata_capabilities).run(lower(parse(source)))
    if not metadata.complete:
        record('cli_metadata_exit', cwd=cwd, argv=argv, status='FAILED',
                     halted_at=span(metadata.halted_at.span if metadata.halted_at else None))
        return 1

    driver = BuildDriver(metadata.graph, runtime.state, runtime.persistence,
        scope, metadata.declarations, cwd=cwd,
        capabilities=runtime.capabilities)
    result = driver.build(targets)
    finish = driver.finish()
    exit_code = 0 if result.status == 'COMPLETE' and finish.status == 'COMPLETE' else 1
    record('cli_exit', cwd=cwd, argv=argv, recipe=recipe,
                 top_recipe='main.aap', status=result.status,
                 reason=result.reason, error=str(result.error) if result.error else None,
                 span=span(result.span), bodies=[body.target.name for body in result.bodies],
                 pending_signatures=len(result.pending_signatures),
                 pending_targets=[item.target for item in result.pending_signatures],
                 body_decisions=body_decisions(result),
                 completion_decisions=completion_decisions(result),
                 marker_observations=(list(driver.port.marker_observations)
                                      if driver.port is not None else []),
                 finish_status=finish.status,
                 persistence_written=finish.persistence_written,
                 inventory=inventory(cwd, environment), exit_code=exit_code,
                 recipe_aap=recipe_aap,
                 sentinel=environment.get('AAP_RECURSIVE_SENTINEL'),
                 tree_records=body_tree_records(result),
                 directory_changes=body_directory_changes(result),
                 cats=body_cat_records(result),
                 print_records=body_print_records(result),
                 process_captures=body_process_captures(result),
                 python_writes=python_write_records(result))
    if exit_code:
        print('aap: {0}: {1}: {2}'.format(result.status, result.reason,
              str(result.error) if result.error else ''), file=sys.stderr)
    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
''')

class _BundledSourceImporter(object):
    """Load embedded project modules through Python import machinery."""
    def find_spec(self, fullname, path=None, target=None):
        item = _EMBEDDED.get(fullname)
        if item is None or fullname == '__main__':
            return None
        origin = '<aapy3>/' + item[0]
        return importlib.util.spec_from_loader(fullname, self, origin=origin,
                                               is_package=item[1])

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path, is_package, source = _EMBEDDED[module.__name__]
        module.__file__ = '<aapy3>/' + path
        linecache.cache[module.__file__] = (len(source), None,
                                           source.splitlines(True), module.__file__)
        if is_package:
            module.__path__ = [module.__file__.rsplit('/', 1)[0]]
        exec(compile(source, module.__file__, 'exec'), module.__dict__)


sys.meta_path.insert(0, _BundledSourceImporter())

if __name__ == '__main__':
    _path, _is_package, _source = _EMBEDDED['__main__']
    _origin = '<aapy3>/' + _path
    linecache.cache[_origin] = (len(_source), None, _source.splitlines(True), _origin)
    exec(compile(_source, _origin, 'exec'), globals())
