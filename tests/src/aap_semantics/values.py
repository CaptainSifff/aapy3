"""Closed metadata value domain and characterized A-A-P conversions."""
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
