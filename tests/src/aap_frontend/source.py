"""Immutable text and physical coordinates. Offsets count Unicode characters."""
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
