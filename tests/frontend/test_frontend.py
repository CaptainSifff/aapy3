"""Source-derived frontend characterization, never legacy runtime execution.

Anchors: upstream/rectest/test001.py (dependency), test006.py (mixed suites),
test009.py (Python indentation), test013.py (argument continuations/actions),
test018.py (sections/trivia); Process.py readers and ParsePos.py::nextline.
"""
from collections import Counter
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, SourcePosition, SourceSpan, FrontendError
from aap_frontend import parse, parse_body, scan
from aap_frontend import cst
from check_frontend_py34 import check_text, implementation_files


def nodes(document):
    """Inspect every retained CST node, including shared line/region objects."""
    pending = [document]
    seen = set()
    while pending:
        node = pending.pop()
        if not isinstance(node, cst.Node) or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        for key, value in vars(node).items():
            if key == 'owner':
                continue
            if isinstance(value, cst.Node):
                pending.append(value)
            elif isinstance(value, tuple):
                pending.extend(item for item in value if isinstance(item, cst.Node))


class SourceTests(unittest.TestCase):
    def test_immutable_positions_and_source(self):
        source = Source('unicode.aap', 'x\r\né\t= 2\n')
        self.assertEqual(source.position(3), SourcePosition(2, 1, 3))
        self.assertEqual(source.position(4), SourcePosition(2, 2, 4))
        self.assertEqual(source.position(9), SourcePosition(3, 1, 9))
        span = source.span(3, 9)
        self.assertEqual(source.slice(span), 'é\t= 2\n')
        for obj, name, value in ((source, 'text', ''), (span, 'start', None),
                                 (span.start, 'line', 10)):
            with self.assertRaises(AttributeError):
                setattr(obj, name, value)

    def test_invalid_spans(self):
        source = Source('s', 'abc')
        for offset in (-1, 4):
            with self.assertRaises(ValueError):
                source.position(offset)
        with self.assertRaises(ValueError):
            source.span(2, 1)
        with self.assertRaises(ValueError):
            source.slice(Source('other', 'abc').span(0, 1))
        with self.assertRaises(ValueError):
            source.slice(SourceSpan('s', SourcePosition(9, 9, 0), source.position(1)))

    def test_empty_and_missing_final_newline(self):
        for text in ('', '\n', '# only', 'A = 1', 'A = 1\r\n'):
            doc = parse(text)
            self.assertEqual(doc.text, text)
            self.assertEqual(''.join(n.text for n in doc.children), text)
        self.assertEqual(parse('A = 1').lines[0].physical_lines[0].ending, '')
        self.assertEqual(parse('A = 1\r\n').lines[0].physical_lines[0].ending, '\r\n')

    def test_exact_spans_and_continuation_mapping(self):
        text = '# first\r\nA = one \\\r\n  two # end\n:print yes'
        doc = parse(text, 'span.aap')
        assignment = doc.children[1]
        self.assertEqual(assignment.span.start, SourcePosition(2, 1, 9))
        self.assertEqual(assignment.text, 'A = one \\\r\n  two # end\n')
        self.assertEqual(assignment.arguments[0].text, ' one \\\r\n  two ')
        self.assertEqual(assignment.arguments[0].comments[0].text, '# end')
        line = assignment.line
        self.assertEqual(''.join(text[o] for o in line.origins), line.cooked)
        self.assertEqual(doc.children[-1].span.start.line, 4)
        self.assertEqual(doc.children[-1].span.end.offset, len(text))
        for node in nodes(doc):
            self.assertEqual(node.text, text[node.span.start.offset:node.span.end.offset])


class ScannerTests(unittest.TestCase):
    def test_comments_blanks_and_eight_column_tabs(self):
        lines = scan(Source('s', '\n \t\n# zero\n   \t:print x\n'))
        self.assertIsInstance(lines[0], cst.BlankLine)
        self.assertIsInstance(lines[1], cst.BlankLine)
        self.assertIsInstance(lines[2], cst.Comment)
        self.assertEqual(lines[3].indent, 8)
        self.assertEqual(lines[3].prefix_length, 4)

    def test_unconditional_join_and_comment_swallow(self):
        text = '# ignored \\\n:fake\nA = "one\\\n two"\n'
        doc = parse(text)
        self.assertEqual(len(doc.children), 2)
        self.assertIsInstance(doc.children[0], cst.Comment)
        self.assertEqual(doc.children[0].cooked, '# ignored :fake')
        self.assertEqual(doc.children[1].line.cooked, 'A = "one two"')
        lines = scan(Source('s', 'A = x\\\\\ny\n'))
        self.assertEqual(lines[0].cooked, 'A = x\\y')
        self.assertEqual(len(scan(Source('s', 'A = x\\ \ny\n'))), 2)

    def test_at_continuation_prefix(self):
        lines = scan(Source('s', '@x = (1, \\\n   @ 2)\n'))
        self.assertEqual(lines[0].cooked, '@x = (1,  2)')
        self.assertEqual(len(lines[0].physical_lines), 2)
        self.assertEqual(lines[0].source.slice(lines[0].continuations[0]), '\\\n   @')

    def test_body_continuation_keeps_next_line_indentation_and_span(self):
        source = Source('joined.aap',
                        'doperlmod:\n'
                        '\t:syseval sed x |\\\n'
                        '\t\t:assign modulename\n')
        line = parse(source).children[0].body.lines[0]
        # ParsePos.nextline() removes only the backslash and physical EOL.
        self.assertEqual(line.cooked,
                         '\t:syseval sed x |\t\t:assign modulename')
        self.assertEqual(line.indent, 8)
        self.assertEqual(len(line.physical_lines), 2)
        self.assertEqual(line.source.slice(line.continuations[0]), '\\\n')
        self.assertEqual((line.span.start.line, line.span.start.column,
                          line.span.end.line, line.span.end.column),
                         (2, 1, 4, 1))

    def test_markers_do_not_corrupt_physical_coordinates(self):
        doc = parse('#@recipe=900 trailing\nA = x\n')
        self.assertIsInstance(doc.children[0], cst.Comment)
        self.assertEqual(doc.children[1].span.start.line, 2)

    def test_diagnostic(self):
        with self.assertRaises(FrontendError) as result:
            parse('A = x\\', 'bad.aap')
        self.assertEqual(str(result.exception),
                         'bad.aap:1:6: last line ends in a backslash')


class StatementTests(unittest.TestCase):
    def test_assignments_and_precedence(self):
        for operator in ('=', '+=', '?=', '$=', '$+=', '$?='):
            node = parse('scope.9 ' + operator + ' value : text\n').children[0]
            self.assertIsInstance(node, cst.AssignmentStatement)
            self.assertEqual(node.name, 'scope.9')
            self.assertEqual(node.operator, operator)
            self.assertEqual(node.arguments[0].text, ' value : text')
        self.assertIsInstance(parse('A == value: other').children[0],
                              cst.AssignmentStatement)

    def test_generic_command_and_argument_continuation(self):
        # test013.py: deeper targetattr/:produce text is arguments, not commands.
        doc = parse(':new-command value\n  :fake x\n  @fake = 3\n:print end\n')
        node = doc.children[0]
        self.assertEqual(node.name, 'new-command')
        self.assertIsNone(node.body)
        self.assertEqual(node.raw_arguments, (' value', ':fake x', '@fake = 3'))
        self.assertEqual(len(doc.children), 2)
        self.assertEqual(parse(':x:y z').children[0].name, 'x:y')
        self.assertEqual(parse(': value').children[0].name, '')

    def test_dependency_and_rule_separator(self):
        # test001.py and test018.py.
        for text in ('out: source', 'out:', 'out:: source',
                     '"out: x": source', '$(#)out: source',
                     'out{attr: x}: source'):
            self.assertIsInstance(parse(text).children[0], cst.DependencyLikeStatement)
        node = parse(':rule %.o : %.c\n  :print x\n').children[0]
        self.assertEqual(node.targets.text, ' %.o ')
        self.assertEqual(node.separator_span.start.column, 11)
        self.assertIsInstance(node.body, cst.DeferredBody)
        for text in ('out:source', 'out:#comment', 'A < value: source'):
            with self.assertRaises(FrontendError):
                parse(text)

    def test_quotes_dollars_and_backticks(self):
        text = 'A = "# intact `1 + 2`" $# $$# ignored\n'
        arg = parse(text).children[0].arguments[0]
        self.assertEqual(arg.backticks[0].cooked, '1 + 2')
        self.assertTrue(arg.backticks[0].active)
        self.assertEqual(arg.comments[0].text, '# ignored')
        node = parse('A = $(#) $(") $# ``\n').children[0]
        self.assertEqual(node.arguments[0].comments, ())
        self.assertFalse(node.arguments[0].backticks[0].active)
        # Backslash does not shield a quote from getarg's quote state.
        arg = parse('A = "x\\" # comment\n').children[0].arguments[0]
        self.assertEqual(arg.comments[0].text, '# comment')

    def test_multiline_backtick_skips_trivia_and_ignores_indent(self):
        text = 'A = `one +\n# skip\n\n  two` tail\nB = ok\n'
        doc = parse(text)
        region = doc.children[0].arguments[0].backticks[0]
        self.assertEqual(region.cooked, 'one +\n  two')
        self.assertEqual(region.text, '`one +\n# skip\n\n  two`')
        self.assertEqual(region.span.start.column, 5)
        self.assertEqual(region.span.end.line, 4)
        escaped = parse('A = `one``two$(`)three`').children[0].arguments[0].backticks[0]
        self.assertEqual(escaped.cooked, 'one`two`three')
        with self.assertRaises(FrontendError):
            parse('A = `never closed')

    def test_input_mode(self):
        self.assertEqual(parse('  A = x').children[0].indent, 2)
        with self.assertRaises(FrontendError):
            parse('# first\n  A = x', file_mode=True)
        # Generic errors remain deferred; forbidden special readers use arguments.
        node = parse(':rule not a rule\n  arbitrary text', toplevel=False).children[0]
        self.assertIsNone(node.body)
        with self.assertRaises(FrontendError):
            parse('out: source', toplevel=False)


class BodyTests(unittest.TestCase):
    def test_command_and_nested_deferred_bodies(self):
        text = ':action build x\n  :tree .\n    @if flag:\n      :print yes\n'
        node = parse(text).children[0]
        self.assertIs(node.body.owner, node)
        self.assertFalse(node.body.scanned)
        self.assertEqual(node.body.minimum_indent, 2)
        tree_doc = parse_body(node.body)
        tree = tree_doc.children[0]
        self.assertEqual(tree.name, 'tree')
        self.assertEqual(tree.span.start.line, 2)
        inner = parse_body(tree.body)
        self.assertIsInstance(inner.children[0], cst.EmbeddedPythonStatement)
        self.assertEqual(inner.children[1].span.start.line, 4)
        self.assertEqual(''.join(n.text for n in inner.children), tree.body.text)
        self.assertFalse(node.body.scanned)  # Re-entry returns an independent CST.

    def test_deferred_errors_wait_for_reentry(self):
        for payload in ('not valid syntax', 'A = `unfinished', 'A << END'):
            node = parse('target:\n    ' + payload + '\n').children[0]
            self.assertIsInstance(node.body, cst.DeferredBody)
            with self.assertRaises(FrontendError):
                parse_body(node.body)

    def test_first_minimum_splits_header(self):
        # upstream/doc/ref-syntax.sgml:89-94 / Process.get_commands.
        text = 'target: source1\n        source2\n    :print $source\n        more\nnext:\n'
        doc = parse(text)
        node = doc.children[0]
        self.assertEqual(node.arguments[-1].text, 'source2')
        self.assertEqual(node.body.text, '    :print $source\n        more\n')
        self.assertEqual(node.body.span.start.line, 3)
        self.assertEqual(len(node.header_lines), 2)
        self.assertEqual(len(parse_body(node.body).children), 1)
        equal = parse('t:\n  one\n  two\n').children[0]
        self.assertEqual(equal.body.text, '  one\n  two\n')

    def test_outer_collector_does_not_honor_inner_heredoc(self):
        # Source-confirmed outer indentation limit; no recursive literal scan.
        doc = parse('t:\n  A << END\nEND:\n')
        self.assertEqual(len(doc.children), 2)
        self.assertEqual(doc.children[0].body.text, '  A << END\n')
        with self.assertRaises(FrontendError):
            parse_body(doc.children[0].body)

    def test_data_bodies_do_not_parse_as_recipe(self):
        for name, flavor in (('route', 'route'), ('filetype', 'filetype')):
            node = parse(':' + name + '\n  opaque data\n').children[0]
            self.assertEqual(node.body.flavor, flavor)
            with self.assertRaises(ValueError):
                parse_body(node.body)

    def test_sections_and_unindented_comment(self):
        # Derived from upstream/rectest/test018.py, no copy/build effects.
        node = parse(':rule %.copy : %.in\n   >always\n# comment\n'
                     '      :print always\n   >build\n      :print build\n').children[0]
        body = parse_body(node.body)
        self.assertEqual([n.name for n in body.children if isinstance(n, cst.SectionStatement)],
                         ['always', 'build'])
        self.assertIsInstance(body.children[1], cst.Comment)
        self.assertEqual(''.join(n.text for n in body.children), node.body.text)


class LiteralAndPythonTests(unittest.TestCase):
    def test_literal_protects_fake_syntax(self):
        # PORTDESCR: upstream/doc/tutor-port.sgml:45-49, Process.get_block_lines.
        text = 'PORTDESCR << EOF\n@values = (\n:sys something\nfoo: bar\n'
        text += 'N << INNER\n`unclosed\n# comment\n\nEOF# close\nA = x\n'
        doc = parse(text)
        node = doc.children[0]
        self.assertIsInstance(node, cst.LiteralBlock)
        self.assertEqual(node.terminator, 'EOF')
        self.assertEqual(node.terminator_line.text, 'EOF# close\n')
        self.assertEqual(node.body.text, text[text.index('@'):text.index('EOF#')])
        self.assertNotIn('# comment', node.body.cooked_text)
        self.assertIn('`unclosed', node.body.cooked_text)
        self.assertEqual(len(doc.children), 2)

    def test_literal_terminator_rules_and_preprocessing(self):
        node = parse('A << "END" # tail\n  x\\\n  y\n    "END" # close\n').children[0]
        self.assertEqual(node.terminator, '"END"')
        self.assertEqual(node.body.cooked_text, 'x  y\n')
        node = parse('A << END\nENDsuffix\nE\\\nND\n').children[0]
        self.assertEqual(node.body.cooked_text, 'ENDsuffix\n')
        self.assertEqual(node.terminator_line.text, 'E\\\nND\n')
        self.assertEqual(parse('A << E\nE\n').children[0].body.text, '')
        for bad in ('A <<', 'A << E extra\nE\n', 'A << E\nx\n',
                    'A << E\n  x\ny\nE\n', 'A << #END\n#END\n'):
            with self.assertRaises(FrontendError):
                parse(bad)

    def test_mixed_python_suites_remain_ordered_fragments(self):
        # test006.py @if/@try/@except; production @elif/@else/@for.
        text = '@if flag:\n  A = x\n@elif other:\n  :print y\n@else:\n'
        text += '  @for item in values:\n    :print $item\n@name = []\n'
        doc = parse(text)
        self.assertEqual([n.payload for n in doc.children if isinstance(n, cst.EmbeddedPythonStatement)],
                         ['if flag:', 'elif other:', 'else:',
                          'for item in values:', 'name = []'])
        self.assertEqual([n.indent for n in doc.children], [0, 2, 0, 2, 0, 2, 4, 0])
        self.assertTrue(all(n.body is None for n in doc.children))
        # There is no automatic Python bracket continuation.
        with self.assertRaises(FrontendError):
            parse('@x = (\n  1)\n')
        parse('@x = (\n@ 1)\n')

    def test_python_payload_indent_and_inert_backticks(self):
        node = parse('@\tname = `text` # `unterminated\n').children[0]
        self.assertEqual(node.python_indent, 8)
        self.assertEqual(node.payload, 'name = `text` # `unterminated')
        self.assertEqual(len(node.backticks), 2)
        self.assertFalse(node.backticks[0].active)
        self.assertFalse(node.backticks[1].closed)
        # Python-like arguments *do* use the A-A-P backtick reader.
        arg = parse(':eval fun(`other(1)`)\n').children[0].arguments[0]
        self.assertTrue(arg.backticks[0].active)

    def test_python_blocks_from_test009(self):
        # Exact recipe spelling derived from upstream/rectest/test009.py.
        text = 'var1 = toplevel\n:python\n print "one space indent"\n'
        text += ':python\n\tprint "one tab indent"\n   \tprint "space and tab indent"\n'
        text += 'all:\n     :python\n        print "spaces"\n\tprint "tab"\n   \tprint "spaces + tab"\n'
        doc = parse(text)
        self.assertEqual(doc.children[1].body.cooked_text, 'print "one space indent"\n')
        self.assertEqual(doc.children[2].body.cooked_text,
                         'print "one tab indent"\nprint "space and tab indent"\n')
        inner = parse_body(doc.children[3].body).children[0]
        self.assertEqual(inner.body.cooked_text,
                         'print "spaces"\nprint "tab"\nprint "spaces + tab"\n')

    def test_python_prefix_and_explicit_termination(self):
        doc = parse(':pythonEND\n@not_aap\n:also_python_data\n END # done\nA = 1\n')
        node = doc.children[0]
        self.assertIsInstance(node, cst.PythonBlock)
        self.assertEqual(node.terminator, 'END')
        self.assertEqual(node.body.text, '@not_aap\n:also_python_data\n')
        self.assertEqual(len(doc.children), 2)


class VariantTests(unittest.TestCase):
    def test_branches_and_ignored_conditions(self):
        text = ':variant MODE\n    debug [ ignored condition ]\n        A = x\n'
        text += '  release\n      :pass\n    *\n        :pass\n'
        node = parse(text).children[0]
        self.assertIsInstance(node, cst.VariantStatement)
        self.assertEqual(node.variable, 'MODE')
        self.assertEqual([b.value for b in node.branches], ['debug', 'release', '*'])
        self.assertEqual(node.branches[0].source.slice(node.branches[0].ignored_tail_span),
                         '[ ignored condition ]')
        self.assertTrue(node.body.scanned)
        self.assertEqual(node.branches[1].body.threshold, 4)

    def test_nested_variant_and_empty_branch(self):
        node = parse(':variant A\n  x\n  y\n    :variant B\n      z\n        :pass\n').children[0]
        self.assertEqual(node.branches[0].body.children, ())
        self.assertIsInstance(node.branches[1].body.children[0], cst.VariantStatement)
        for text in (':variant A\n', ':variant\n', ':variant A\n  *x\n',
                     ':variant A\n  *\n  x\n'):
            with self.assertRaises(FrontendError):
                parse(text)


class ProductionTests(unittest.TestCase):
    def test_entire_globals_and_all_deferred_recipe_bodies(self):
        # Only intentionally available production example; Latin-1 is the
        # existing audit's reversible decoding policy, not a corpus assumption.
        source = Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1')
        document = parse(source, file_mode=True)
        counts = Counter()
        pending = [document]
        while pending:
            doc = pending.pop()
            self.assertEqual(''.join(n.text for n in doc.children), doc.text)
            for node in nodes(doc):
                self.assertEqual(node.text, source.text[node.span.start.offset:node.span.end.offset])
                counts[type(node).__name__] += 1
                if isinstance(node, cst.DeferredBody) and node.flavor == 'recipe':
                    pending.append(parse_body(node))
        self.assertEqual(counts['EmbeddedPythonStatement'], 152)
        self.assertEqual(counts['PythonBlock'], 4)
        self.assertEqual(counts['VariantStatement'], 5)
        self.assertEqual(counts['VariantBranch'], 21)
        self.assertEqual(counts['DependencyLikeStatement'], 34)
        self.assertEqual(counts['Document'], 42)


class Python34Tests(unittest.TestCase):
    def test_all_new_python_files_pass_guard(self):
        import io
        errors = []
        for path in implementation_files(ROOT):
            with io.open(path, encoding='utf-8') as stream:
                errors.extend(check_text(stream.read(), path))
        self.assertEqual(errors, [])

    def test_guard_rejects_post34_syntax_and_apis(self):
        samples = (
            'x = f"value {1}"', 'x: int = 1', 'async def f():\n    pass',
            'x = (y := 1)', 'match x:\n    case 1: pass',
            'x = {**d}', 'x = [*a]', 'x = 1_000', 'x = a @ b',
            'def f(x, /): pass', 'f(*a, *b)', 'f(**a, x=1)',
            'import dataclasses', 'from typing import Any',
            'import subprocess\nsubprocess.run([])',
            'import os\nos.scandir(".")', 'x.removeprefix("a")',
            'type X = int', 'breakpoint()',
        )
        for sample in samples:
            self.assertTrue(check_text(sample), sample)

    def test_guard_accepts_representative_python34(self):
        sample = ('from collections import namedtuple\n'
                  'def f(x, *args, **kwargs):\n'
                  '    a, *b = x\n'
                  '    yield from b\n'
                  'f(1, *a, **b)\n')
        self.assertEqual(check_text(sample), [])


if __name__ == '__main__':
    unittest.main()
