"""Pre-refactor structural/error characterization of bounded :sys syntax."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import lower
from aap_semantics.system_process import literal_shell


CASES = [
    'echo one', 'echo "two words"', 'echo one\\ two', 'echo a\\|b',
    'cat a | sed b', 'true && false', 'true || false',
    '(echo a && (echo b | cat))', 'cat src < input',
    'cat src >> dst', 'echo ">>"', 'echo "&&"',
    'cat a |', '(echo a', 'echo a &', 'echo a; echo b',
    'echo $USER', 'echo `date`', 'echo a > b',
    'echo a >> b >> c', 'sh -c echo', 'echo one\\\ntwo',
    '(( 1 + 2 ))', 'cat >> "target file"',
]
for left in ('echo a', 'cat "a b"', '(true)', 'echo x\\|y'):
    for operator in (' | ', ' && ', ' || ', ' >> ', ' < ', ' ; ', ' & '):
        for right in ('cat', '"target file"'):
            CASES.append(left + operator + right)

# The original 80 observations remain immutable in shell_golden.json. Nine
# inputs there were rejected solely because unquoted ';' was gated; their new
# outcomes are recorded separately alongside new sequence examples.
NEW_SEMICOLON_CASES = [
    'a ; b', 'a;b', 'a ;b', 'a; b', 'a ; b ; c',
    'a && b ; c', 'a || b ; c', 'a ; b && c', 'a ; b || c',
    'a | b ; c', 'a ; b | c', '(a ; b)', '(a ; b) ; c',
    'a ; (b ; c)', 'a ;', '(a ;)', ';', '; a', 'a ; ; b', 'a ;; b',
    "echo ';'", 'echo ";"', 'echo \\;',
    'cat src >> dst ; echo done',
]
GROUP_CD_CASES = [
    '(cd x)', '( cd x )', '( cd x ; true )',
    'producer | ( cd x ; consumer )', '( cd "x y" ; true )',
    'cd x', 'cd x ; command', '( cd )', '( cd a b )',
    '( cd - ; command )',
]


def _shape(expression):
    return [expression.kind, list(expression.argv),
            [_shape(child) for child in expression.children],
            [[redirection.operator, redirection.target]
             for redirection in expression.redirections]]


def outcome(command):
    origin = lower(parse(Source('shell.aap', ':sys placeholder\n'))).statements[0]
    try:
        return ['VALUE', _shape(literal_shell(command, origin))]
    except Exception as error:
        return ['ERROR', type(error).__name__, error.reason]


class ShellGoldenTests(unittest.TestCase):
    def test_original_80_and_explicit_semicolon_transitions(self):
        directory = os.path.dirname(__file__)
        with open(os.path.join(directory, 'shell_golden.json')) as stream:
            original = json.load(stream)
        with open(os.path.join(directory, 'shell_semicolon_golden.json')) as stream:
            new = json.load(stream)
        new_by_command = dict(new)
        self.assertEqual(len(original), 80)
        self.assertEqual([item[0] for item in original], CASES)
        self.assertEqual(len(new), len(new_by_command))
        self.assertEqual([item[0] for item in new if item[0] in NEW_SEMICOLON_CASES],
                         NEW_SEMICOLON_CASES)
        transitions = [command for command, recorded in original
                       if command in new_by_command]
        self.assertEqual(len(transitions), 9)
        for command, recorded in original:
            if command in new_by_command:
                self.assertEqual(recorded, ['ERROR', 'Unsupported',
                    'shell operator/expansion is outside bounded :sys: ;'])
                self.assertEqual(outcome(command), new_by_command[command], command)
            else:
                self.assertEqual(outcome(command), recorded, command)
        for command, recorded in new:
            self.assertEqual(outcome(command), recorded, command)

    def test_group_local_cd_transitions(self):
        directory = os.path.dirname(__file__)
        with open(os.path.join(directory, 'shell_group_cd_golden.json')) as stream:
            cases = json.load(stream)
        self.assertEqual([command for command, result in cases], GROUP_CD_CASES)
        for command, recorded in cases:
            self.assertEqual(outcome(command), recorded, command)


if __name__ == '__main__':
    unittest.main()
