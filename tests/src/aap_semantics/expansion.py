"""A-A-P expansion, separate from lexical parsing and Python interpretation."""
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
