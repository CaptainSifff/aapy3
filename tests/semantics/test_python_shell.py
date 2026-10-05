"""Bounded shell :python characterization from globals.aap:617-636.

Source contract: Process.get_block_lines/Process, Commands.expand,
Util.get_var_val/bs_quote, RecPython.var2string/var2list.
"""
import hashlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, OutputPolicy, PrintRuntime,
                           MemoryTextWriter, TextWriter, SemanticError, Unsupported)
from aap_semantics.expansion import expand_text
from nano_target_frontier import BASE, fixture, run_target


def block(body):
    return lower(parse(Source('/recipe/main.aap', ':python\n' + ''.join(
        '  ' + line + '\n' for line in body.splitlines()))))


class ShellPythonTests(unittest.TestCase):
    def execute(self, body, variables=None, writer=None, cwd='/recipe', encoding='latin-1'):
        scope = Scope.top_level()
        scope.local.update(variables or {})
        writer = writer if writer is not None else MemoryTextWriter(['/recipe'])
        output = PrintRuntime(OutputPolicy(encoding, False), writer=writer)
        result = Evaluator(scope, cwd=cwd, output_runtime=output).run(block(body))
        return result, writer

    def test_backslash_modifier_items_quotes_and_missing(self):
        scope = Scope.top_level()
        scope.local['X'] = '"a b" c\\d "e\'f"'
        origin = block('fh = open("x", "w")').statements[0]
        self.assertEqual(expand_text('$\\X', scope, origin), 'a\\ b c\\\\d e\\\'f')
        scope.local['X'] = '"a\tb"'
        self.assertEqual(expand_text('$\\X', scope, origin), 'a\\\tb')
        scope.local['X'] = "'a\"b'"
        self.assertEqual(expand_text('$\\X', scope, origin), 'a\\"b')
        self.assertEqual(expand_text('$\\MISSING', scope, origin, preserve_missing=True),
                         '$\\MISSING')
        self.assertEqual(expand_text('$\\( MISSING )', scope, origin,
                                     preserve_missing=True), '$\\( MISSING )')
        with self.assertRaises(SemanticError):
            expand_text('$\\MISSING', scope, origin)
        with self.assertRaises(Unsupported):
            expand_text('$-X', scope, origin)

    def test_open_w_truncates_and_multiple_writes_close(self):
        writer = MemoryTextWriter(['/recipe'], {'/recipe/script': b'old'})
        result, writer = self.execute('script = var2string(_no.script)\n'
            'fh = open(script, "w")\nfh.write("caf\\u00e9")\n'
            'fh.write("\\nnext")\nfh.close()', {'script': 'script'}, writer)
        self.assertTrue(result.complete)
        self.assertEqual(writer.files['/recipe/script'], b'caf\xe9\nnext')
        self.assertEqual([r.operation for r in result.python_writes],
                         ['open', 'write', 'write', 'close'])
        self.assertEqual([r.status for r in result.python_writes], ['COMPLETED'] * 4)
        self.assertIs(result.scope.local['fh'].closed, True)
        self.assertEqual(result.processes, [])

    def test_append_loop_scope_and_relative_logical_cwd(self):
        writer = MemoryTextWriter(['/recipe/sub'], {'/recipe/sub/script': b'prefix'})
        result, writer = self.execute('functions = var2list(_no.functions)\n'
            'fh = open("script", "a")\nfor f in functions:\n'
            '  fh.write(f)\n  fh.write("\\n")\nfh.close()',
            {'functions': 'alpha "two words"'}, writer, '/recipe/sub')
        self.assertEqual(writer.files['/recipe/sub/script'], b'prefixalpha\ntwo words\n')
        self.assertEqual(result.scope.local['f'], 'two words')
        self.assertEqual(result.python_writes[0].request.path, '/recipe/sub/script')
        self.assertEqual(result.python_writes[0].request.destination, 'append')

    def test_append_creates_missing_file(self):
        result, writer = self.execute('fh = open("new", "a")\n'
                                      'fh.write("created")\nfh.close()')
        self.assertTrue(result.complete)
        self.assertEqual(writer.files['/recipe/new'], b'created')

    def test_python_block_shares_dictionary_with_following_at_statement(self):
        text = (':python\n  script = var2string(_no.script)\n'
                '  fh = open(script, "w")\n  fh.write("x")\n  fh.close()\n'
                '@copied = script\n')
        scope = Scope.top_level()
        scope.local['script'] = 'script'
        writer = MemoryTextWriter(['/recipe'])
        runtime = PrintRuntime(OutputPolicy('latin-1', False), writer=writer)
        result = Evaluator(scope, cwd='/recipe', output_runtime=runtime).run(
            lower(parse(Source('/recipe/main.aap', text))))
        self.assertTrue(result.complete)
        self.assertEqual(scope.local['copied'], 'script')
        self.assertEqual(writer.files['/recipe/script'], b'x')

    def test_unavailable_writer_and_write_failure_keep_partial_effects(self):
        with self.assertRaises(Unsupported):
            self.execute('fh = open("file", "w")', writer=TextWriter())

        class FailingWriter(MemoryTextWriter):
            def open_bytes(self, request):
                session = super(FailingWriter, self).open_bytes(request)
                def write(data):
                    if data == b'second':
                        raise OSError('injected write failure')
                    session.files[session.path] += data
                session.write = write
                return session
        writer = FailingWriter(['/recipe'], {'/recipe/file': b'old'})
        with self.assertRaises(SemanticError):
            self.execute('fh = open("file", "w")\nfh.write("first")\n'
                         'fh.write("second")', writer=writer)
        self.assertEqual(writer.files['/recipe/file'], b'first')

    def test_close_failure_retains_completed_writes(self):
        class FailingCloseWriter(MemoryTextWriter):
            def open_bytes(self, request):
                session = super(FailingCloseWriter, self).open_bytes(request)
                def close():
                    raise OSError('injected close failure')
                session.close = close
                return session
        writer = FailingCloseWriter(['/recipe'])
        with self.assertRaises(SemanticError):
            self.execute('fh = open("file", "w")\nfh.write("kept")\nfh.close()',
                         writer=writer)
        self.assertEqual(writer.files['/recipe/file'], b'kept')

    def test_bad_mode_and_closed_handle(self):
        writer = MemoryTextWriter(['/recipe'])
        with self.assertRaises(Unsupported):
            self.execute('fh = open("file", "r")', writer=writer)
        self.assertEqual(writer.requests, [])
        with self.assertRaises(SemanticError):
            self.execute('fh = open("file", "w")\nfh.close()\nfh.write("x")',
                         writer=writer)
        self.assertEqual(writer.files['/recipe/file'], b'')

    def test_broader_python_forms_stay_deferred(self):
        for body in ('import os', 'eval("1")', 'exec("x=1")',
                     'os.path.exists("file")', 'x = 1\nx.replace("a", "b")',
                     'print value', 'x = open("file", "w")\nx.flush()'):
            result, writer = self.execute(body)
            self.assertFalse(result.complete, body)
            self.assertEqual(writer.requests, [], body)

    def test_nano_shellheader_signature_and_exact_bytes(self):
        expected = (b'#!/usr/bin/ksh\n'
                    b'# Do not edit, generated automatically by AAP\n'
                    b'# $Id: globals.aap,v 1.172 2026/09/10 12:01:26 mheinen Exp $\n'
                    b'\n[[ -n "$DEBUG" ]] && set -x\n')
        summary = run_target('shellheader')
        self.assertEqual(summary['status'], 'COMPLETE')
        self.assertEqual(bytes.fromhex(summary['post_i_hex']), expected)
        self.assertEqual(summary['process_requests'], 3)
        self.assertEqual([r['operation'] for r in summary['python_writes']],
                         ['open', 'write', 'write', 'write', 'write', 'close'])
        self.assertEqual(summary['python_writes'][0]['path'], BASE + 'work/post_i')
        check = summary['preparations'][-1]
        self.assertEqual(check['status'], 'PREPARED')
        canonical = ('\t:python\n'
                     '\t\tscript = var2string(_no.script)\n'
                     "\t\tfh = open(script, 'w')\n"
                     "\t\tfh.write('#!/usr/bin/ksh\\n')\n"
                     "\t\tfh.write('# Do not edit, generated automatically by AAP\\n')\n"
                     "\t\tfh.write('# $Id: globals.aap,v 1.172 2026/09/10 12:01:26 mheinen Exp $\\n')\n"
                     '\t\tfh.write(\'\\n[[ -n "$DEBUG" ]] && set -x\\n\')\n'
                     '\t\tfh.close()\n')
        self.assertEqual(check['canonical'], canonical)
        self.assertEqual(check['signature'], '89d6a568dfbffbc56562069b6c1890eb')
        self.assertEqual(check['signature'], hashlib.md5(
            check['canonical'].encode('latin-1')).hexdigest())

    def test_defined_backslash_reference_changes_signature_not_script_literal(self):
        driver, writer, reader, saved, process = fixture()
        driver.scope.local['script'] = 'work/post_i'
        driver.scope.local['n'] = '"a b"'
        result = driver.build('shellheader')
        self.assertEqual(result.status, 'COMPLETE')
        check = [p for p in driver.observations.preparations
                 if p.target.name == 'shellheader'][-1]
        self.assertIn('Exp a\\ b', check.canonical)
        self.assertNotEqual(check.signature, '89d6a568dfbffbc56562069b6c1890eb')
        self.assertIn(b'Exp $\n', writer.files[BASE + 'work/post_i'])

    def test_nano_shellfooter_appends_exact_bytes(self):
        summary = run_target('shellfooter')
        self.assertEqual(summary['status'], 'COMPLETE')
        self.assertEqual(bytes.fromhex(summary['post_i_hex']),
                         b'#!/bin/sh\n\n# MAIN\nalpha\nbeta\n')
        self.assertEqual(summary['process_requests'], 3)
        self.assertEqual([r['operation'] for r in summary['python_writes']],
                         ['open', 'write', 'write', 'write', 'write', 'write', 'close'])
        self.assertEqual(summary['preparations'][-1]['signature'],
                         '0cdb1b868f7904b35855fcf098cd4925')

    def test_aix_python_blocks_remain_blocked(self):
        for name in ('geninfo', 'check-installed'):
            driver, writer, reader, saved, process = fixture()
            result = driver.build(name)
            self.assertEqual((result.status, result.reason),
                             ('BLOCKED', 'unsupported_operation'))
            self.assertEqual(writer.requests, [])


if __name__ == '__main__':
    unittest.main()
