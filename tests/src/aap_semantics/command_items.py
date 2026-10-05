"""Bounded Dictlist item/attribute syntax shared by command consumers."""
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
