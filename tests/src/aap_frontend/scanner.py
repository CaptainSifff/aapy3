"""Physical/logical scanning derived from upstream/ParsePos.py::nextline."""
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
