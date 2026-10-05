"""Bounded external A-A-P argv handling from DoArgs.doargs().

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
