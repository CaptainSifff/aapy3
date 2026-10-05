"""Context-directed structural readers, following Process.py's reader order.

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
