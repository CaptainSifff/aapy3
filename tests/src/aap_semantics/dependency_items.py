"""Bounded dependency item language, after separate A-A-P expansion."""
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
