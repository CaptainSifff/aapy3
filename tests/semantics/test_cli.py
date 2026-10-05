"""Focused DoArgs-style external assignment coverage without shell parsing."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (apply_assignments, CliArgumentError, Evaluator,
                           Scope, lower, parse_arguments)
from aap_semantics.expansion import expand_text


def origin():
    return lower(parse(Source('fixture.aap', 'VALUE = value\n'))).statements[0]


class CliAssignmentTests(unittest.TestCase):
    def test_reached_script_assignment_is_separate_from_target(self):
        parsed = parse_arguments(['script=work/post_i', 'shellheader'])
        self.assertEqual(parsed.assignments, {'script': 'work/post_i'})
        self.assertEqual(parsed.targets, ['shellheader'])

    def test_assignments_seed_recipe_and_arg_scope(self):
        scope = Scope.top_level()
        applied, ignored = apply_assignments(scope, {
            'script': 'work/post_i', 'functions': 'foo bar'})
        self.assertEqual(ignored, {})
        self.assertEqual(applied, {'script': 'work/post_i', 'functions': 'foo bar'})
        node = origin()
        self.assertEqual(expand_text('$script', scope, node), 'work/post_i')
        self.assertEqual(expand_text('$_arg.script', scope, node), 'work/post_i')
        self.assertEqual(expand_text('$_arg.functions', scope, node), 'foo bar')

    def test_first_equals_empty_values_and_spaced_argv_are_preserved(self):
        parsed = parse_arguments(['x=', 'y=a=b', 'functions=foo bar', 'target'])
        self.assertEqual(parsed.assignments,
                         {'x': '', 'y': 'a=b', 'functions': 'foo bar'})
        self.assertEqual(parsed.targets, ['target'])

    def test_invalid_name_is_retained_by_parser_but_not_applied(self):
        parsed = parse_arguments(['bad-name=value', 'shellheader'])
        scope = Scope.top_level()
        applied, ignored = apply_assignments(scope, parsed.assignments)
        self.assertEqual(applied, {})
        self.assertEqual(ignored, {'bad-name': 'value'})
        self.assertNotIn('bad-name', scope.local)
        self.assertEqual(scope.namespaces['_arg'].scope.local, {})

    def test_assignments_and_targets_may_be_interspersed(self):
        parsed = parse_arguments(['first', 'x=one', 'second', 'y=two'])
        self.assertEqual(parsed.targets, ['first', 'second'])
        self.assertEqual(parsed.assignments, {'x': 'one', 'y': 'two'})

    def test_later_recipe_assignment_overwrites_recipe_not_arg(self):
        scope = Scope.top_level()
        apply_assignments(scope, {'CCC': 'cli'})
        result = Evaluator(scope).run(lower(parse(Source(
            'fixture.aap', 'CCC = recipe\nCOPIED = $_arg.CCC\n'))))
        self.assertTrue(result.complete)
        self.assertEqual(scope.local['CCC'], 'recipe')
        self.assertEqual(scope.local['COPIED'], 'cli')

    def test_target_only_and_unsupported_argv_stay_distinct(self):
        self.assertEqual(parse_arguments(['rpm']).targets, ['rpm'])
        for argv in ([''], ['-'], ['--quiet'], ['-f']):
            with self.assertRaises(CliArgumentError):
                parse_arguments(argv)


if __name__ == '__main__':
    unittest.main()
