"""Pure, bounded preparation of historical expanded-command buildchecks.

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
