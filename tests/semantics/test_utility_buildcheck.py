"""Derived from DoBuild.buildcheck_update, Commands.expand and Util.get_var_val.

The historical rectest002/003/007 tests supply adjacent expansion/signature/
scope evidence; none are executed (their runner launches host processes).
"""
import hashlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, BuildDriver,
                           MemoryTargetState, MemoryPersistence)
from aap_semantics.buildcheck import BuildSignaturePreparer
from aap_semantics.expansion import expand_text
from aap_semantics.diagnostics import UndefinedName
from aap_semantics.values import MISSING, DeferredExpansion, UnavailableValue
from nano_target_frontier import fixture


SIGNATURES = {
    'print-rpmreq': '4b2885b598384ee34142787b681333aa',
    'print-rpmfile': '974cdd551057073c83115c0236f9b593',
    'print-deps': '43611fe096d10b292e814afcd154a84e',
    'IndexEntry': '7a926051d466c5f2946fa1202ebf08cb',
    'depclean': '3a542995c87ce8b76c07c04453ede9e9',
    'geninfo': 'f73d646471f21cba3f663e3b88930fb5',
    'chkinfo': '14c0e3342b129b0680ffd157ff4c5370',
    'chkcolon': 'd18591553e2dd584761cf68d9bac2a09',
    'prep-lpp': '3df2549a9facfdfa7ccfcbca8b68ad6e',
    'build-lpp': 'e589370f3572c1274a1e1cfb235b9789',
    'print-prereq': '1a06558457c2b45250c3e7fec3174acb',
    'doperlmod': '427c5cae293451926dea36d5044a98e1',
}


class UtilityBuildcheckTests(unittest.TestCase):
    def setup_body(self, body, scope=None):
        metadata = Evaluator(scope).run(lower(parse(Source(
            '/recipe/main.aap', 'out:\n' + body))))
        node = metadata.graph.find_node('out')
        return metadata, node.definitions[0], node

    def prepare(self, body, scope=None):
        metadata, definition, node = self.setup_body(body, scope)
        return BuildSignaturePreparer('latin-1').prepare(definition, node, metadata.scope)

    def test_missing_valid_references_preserve_spelling_in_first_expansion(self):
        metadata, definition, node = self.setup_body('  :pass\n')
        text = '$missing $( missing ) ${missing} $absent.name $missing.tail.'
        self.assertEqual(expand_text(text, metadata.scope, definition,
                                    item_attributes=True, preserve_missing=True), text)
        with self.assertRaises(UndefinedName):
            expand_text('$missing', metadata.scope, definition)

    def test_missing_reference_buildcheck_is_not_empty_and_optional_stays_optional(self):
        value = self.prepare('  :print $missing/$(MISSING) [$?optional]\n')
        self.assertEqual(value.status, 'PREPARED')
        expected = '  :print $missing/$(MISSING) []\n'
        self.assertEqual(value.canonical, expected)
        self.assertEqual(value.signature, hashlib.md5(expected.encode('ascii')).hexdigest())
        self.assertNotEqual(value.signature, self.prepare('  :print / []\n').signature)

    def test_undefined_variable_remains_fatal_at_runtime_after_successful_preparation(self):
        metadata, definition, node = self.setup_body('  VALUE = $missing\n')
        driver = BuildDriver(metadata.graph, MemoryTargetState(), MemoryPersistence(),
            metadata.scope, metadata.declarations, buildcheck_encoding='latin-1')
        result = driver.build('out')
        self.assertEqual((result.status, result.reason), ('FAILED', 'semantic_error'))
        self.assertEqual(len(result.bodies), 1)
        self.assertIn('undefined A-A-P variable: missing', str(result.error))
        self.assertEqual(driver.observations.preparations[-1].status, 'PREPARED')
        self.assertEqual(result.pending_signatures, ())

    def test_invalid_dot_is_fatal_even_under_historical_skip_errors(self):
        # Commands.sep_scope runs before any tolerant lookup handler.
        value = self.prepare('  :print $_no.bad.name\n')
        self.assertEqual(value.status, 'FAILED')
        self.assertIn('invalid scoped variable name', value.reason)
        self.assertIsNone(value.signature)

    def test_unavailable_and_deferred_values_are_not_mistaken_for_absence(self):
        for value in (UnavailableValue('fixture value unavailable'),
                      DeferredExpansion('$missing', None), None, ['one']):
            scope = Scope.top_level()
            scope.local['VALUE'] = value
            result = self.prepare('  :print $VALUE\n', scope)
            self.assertEqual(result.status, 'UNAVAILABLE')
            self.assertIsNone(result.signature)

    def test_unimplemented_tolerant_syntax_is_a_gate_not_a_silent_fallback(self):
        scope = Scope.top_level()
        scope.local['VALUE'] = 'one'
        for reference in ('$-VALUE', '$(VALUE[0])', '$(VALUE', '$:'):
            result = self.prepare('  :print ' + reference + '\n', scope)
            self.assertEqual(result.status, 'UNAVAILABLE', reference)
            self.assertIsNone(result.signature)

    def test_later_assignment_is_signed_but_never_evaluated_for_lookup(self):
        scope = Scope.top_level()
        body = '  LATER = new\n  :print $LATER\n'
        first = self.prepare(body, scope)
        self.assertEqual(first.canonical, body)
        self.assertIs(scope.lookup('LATER'), MISSING)
        scope.local['LATER'] = 'before'
        second = self.prepare(body, scope)
        self.assertEqual(second.canonical, '  LATER = new\n  :print before\n')
        self.assertNotEqual(first.signature, second.signature)
        self.assertEqual(scope.local['LATER'], 'before')

    def test_continued_body_line_is_one_logical_command_for_buildcheck(self):
        body = '  :syseval sed x |\\\n    :assign modulename\n'
        value = self.prepare(body)
        expected = '  :syseval sed x |    :assign modulename\n'
        self.assertEqual(value.status, 'PREPARED')
        self.assertEqual(value.commands, expected)
        self.assertEqual(value.canonical,
                         '  :syseval sed x | :assign modulename\n')
        self.assertEqual(value.signature, 'f948e940a8a590b539720af6f0f4f09e')

    def test_noncontinued_body_signature_is_unchanged(self):
        body = '  :syseval sed x | :assign modulename\n'
        value = self.prepare(body)
        self.assertEqual(value.commands, body)
        self.assertEqual(value.canonical, body)
        self.assertEqual(value.signature, 'f948e940a8a590b539720af6f0f4f09e')

    def test_unselected_branch_is_signed_and_live_dollar_values_affect_it(self):
        scope = Scope.top_level()
        body = '  @if True:\n    :pass\n  @else:\n    :print $COLD\n'
        first = self.prepare(body, scope)
        self.assertEqual(first.canonical, body)
        scope.local['COLD'] = 'never executed'
        second = self.prepare(body, scope)
        self.assertIn(':print never executed', second.canonical)
        self.assertNotEqual(first.signature, second.signature)

    def test_correct_caller_definition_and_explicit_namespace_lookup(self):
        definition_scope = Scope.top_level()
        definition_scope.local.update({'VALUE': 'definition', 'target': 'recipe-target'})
        metadata, definition, node = self.setup_body(
            '  :print $VALUE $_recipe.VALUE $_top.VALUE $_caller.VALUE '
            '$target $_no.target $_recipe.target\n', definition_scope)
        caller_top = Scope.top_level()
        caller_top.local['VALUE'] = 'caller-top'
        caller = Scope.build(caller_top, caller_top)
        caller.local['VALUE'] = 'caller-body'
        value = BuildSignaturePreparer('latin-1').prepare(definition, node, caller)
        self.assertEqual(value.canonical,
            '  :print caller-body definition caller-top caller-body recipe-target\n')
        self.assertEqual(definition_scope.local['target'], 'recipe-target')
        self.assertNotIn('source', caller.local)

    def test_commands_binding_is_local_and_inserted_dollars_are_not_recursive(self):
        scope = Scope.top_level()
        scope.local.update({'VALUE': '$missing', 'commands': 'outer'})
        result = self.prepare('  :print $VALUE\n', scope)
        self.assertEqual(result.canonical, '  :print $missing\n')
        result = self.prepare('  :print $commands\n', scope)
        self.assertEqual(result.expanded, '  :print   :print $commands\n\n')
        self.assertEqual(scope.local['commands'], 'outer')

    def test_two_pass_attribute_removal_and_canonical_reserialization(self):
        body = '  :sys {q} echo $LATER\n'
        result = self.prepare(body)
        self.assertEqual(result.expanded, body)
        self.assertEqual(result.canonical, ':sys echo $LATER')
        # Even the preserved brace-style reference is an attribute in pass 2.
        result = self.prepare('  :print ${MISSING}\n')
        self.assertEqual(result.expanded, '  :print ${MISSING}\n')
        self.assertEqual(result.canonical, ':print $')

    def test_attr_zero_conversion_for_substituted_values_and_malformed_values(self):
        scope = Scope.top_level()
        scope.local['VALUE'] = 'one {tag=yes} "two words"'
        self.assertEqual(self.prepare('  :print $VALUE\n', scope).canonical,
                         '  :print one "two words"\n')
        # get_var_val catches Dictlist UserError and returns the original value.
        scope.local['VALUE'] = 'one {broken'
        self.assertEqual(self.prepare('  :print $VALUE\n', scope).canonical,
                         '  :print one {broken\n')
        scope.local['VALUE'] = '"literal { brace"'
        self.assertEqual(self.prepare('  :print $VALUE\n', scope).canonical,
                         '  :print "literal { brace"\n')
        scope.local['VALUE'] = 'one {tag={nested}}'
        self.assertEqual(self.prepare('  :print $VALUE\n', scope).status, 'UNAVAILABLE')

    def test_nano_exact_canonical_examples(self):
        driver, writer, reader, saved, process = fixture()
        expected = {
            'print-rpmreq': "        :print company.nano >= `re.sub('-','_', var2string(_no.PORTVERSION))`-1\n",
            'print-rpmfile': '\tADIR=noarch\n\t@if (_no.LXVER == "arch"):\n\t\tADIR=x86_64\n'
                "        :print /export/company/SLES15SP6/bsus/$(ADIR)/company.nano-"
                "`re.sub('-','_', var2string(_no.PORTVERSION))`-1.$(ADIR).rpm\n",
            'print-deps': '@deps = [] @deps.extend(var2list(_no.FETCHREQUIRES)) '
                '@deps.extend(var2list(_no.EXTRACTREQUIRES)) @deps.extend(var2list(_no.BUILDREQUIRES)) '
                '@deps.extend(var2list(_no.BR_REQUIRES)) @deps.extend(var2list(_no.REQUIRES)) '
                '@for d in deps: :cd ../../$d :sys $AAP print-deps :print $d :cd -',
            'IndexEntry': '        @if "Linux" in _no.OSNAME:\n'
                "\t        :print nano|7.1|editors|Nano's ANOther editor, an enhanced free Pico clone| | |company.nano-1\n"
                '\t@else:\n'
                "\t        :print nano|7.1|editors|Nano's ANOther editor, an enhanced free Pico clone| | |$(LPPNAME)-$LPPVERSION\n",
        }
        for name, canonical in expected.items():
            node = driver.graph.find_node(name)
            value = BuildSignaturePreparer('latin-1').prepare(node.definitions[0], node, driver.scope)
            self.assertEqual(value.canonical, canonical, name)
            self.assertEqual(value.signature, SIGNATURES[name])

    def test_every_affected_definition_has_deterministic_byte_signature_without_effects(self):
        driver, writer, reader, saved, process = fixture()
        before = dict(driver.scope.local)
        for name, signature in SIGNATURES.items():
            node = driver.graph.find_node(name)
            first = BuildSignaturePreparer('latin-1').prepare(node.definitions[0], node, driver.scope)
            second = BuildSignaturePreparer('latin-1').prepare(node.definitions[0], node, driver.scope)
            self.assertEqual(first.status, 'PREPARED', name)
            self.assertEqual(first.snapshot(), second.snapshot(), name)
            self.assertEqual(first.signature, signature, name)
            self.assertEqual(first.signature, hashlib.md5(first.canonical.encode('latin-1')).hexdigest())
        self.assertEqual(driver.scope.local, before)
        self.assertEqual(len(process.requests), 3)  # fixture setup only
        self.assertEqual(writer.requests, [])
        self.assertEqual(saved.writes, [])
        self.assertEqual(driver.port_runtime.markers.operations, [])
        self.assertEqual(driver.port_runtime.recipe_mutations.requests, [])

    def test_gt_fixture_gap_is_distinct_from_unknown_reference_preservation(self):
        driver, writer, reader, saved, process = fixture()
        node = driver.graph.find_node('print-rpmreq')
        del driver.scope.local['gt']
        value = BuildSignaturePreparer('latin-1').prepare(node.definitions[0], node, driver.scope)
        self.assertEqual(value.status, 'PREPARED')
        self.assertIn('$(gt)=', value.canonical)
        self.assertNotEqual(value.signature, SIGNATURES['print-rpmreq'])
        self.assertEqual(driver.build('print-rpmreq').status, 'FAILED')

    def test_relevant_values_change_checks_but_python_only_values_do_not(self):
        driver, writer, reader, saved, process = fixture()
        def prepare(name):
            node = driver.graph.find_node(name)
            return BuildSignaturePreparer('latin-1').prepare(node.definitions[0], node, driver.scope)
        driver.scope.local['PORTVERSION'] = 'changed-but-backtick-not-executed'
        self.assertEqual(prepare('print-rpmreq').signature, SIGNATURES['print-rpmreq'])
        driver.scope.local['RPMVERSION'] = '2'
        self.assertNotEqual(prepare('print-rpmreq').signature, SIGNATURES['print-rpmreq'])
        before = prepare('IndexEntry').signature
        driver.scope.local['LPPNAME'] = 'cold-branch-value'
        self.assertNotEqual(prepare('IndexEntry').signature, before)

    def test_five_linux_utilities_complete_with_no_additional_process_requests(self):
        for name in ('print-rpmreq', 'print-rpmfile', 'print-deps', 'depclean', 'IndexEntry'):
            driver, writer, reader, saved, process = fixture()
            result = driver.build(name)
            self.assertEqual(result.status, 'COMPLETE', name)
            self.assertEqual([b.target.name for b in result.bodies], [name])
            self.assertEqual(result.bodies[0].context.prepared_buildcheck, SIGNATURES[name])
            self.assertEqual(len(process.requests), 3)
            self.assertEqual(saved.writes, [])
            events = driver.capabilities.output_runtime.sink.events
            if name == 'print-rpmreq':
                self.assertEqual(events[0].output_text, 'company.nano >= 7.1-1\n')
            if name == 'print-rpmfile':
                self.assertEqual(events[0].output_text,
                    '/export/company/SLES15SP6/bsus/noarch/company.nano-7.1-1.noarch.rpm\n')

    def test_new_runtime_boundaries_stop_without_implementing_them(self):
        for name, status, reason in (
                ('geninfo', 'BLOCKED', 'unsupported_operation'),
                ('chkinfo', 'BLOCKED', 'unsupported_semantics'),
                ('chkcolon', 'BLOCKED', 'unsupported_semantics'),
                ('prep-lpp', 'FAILED', 'semantic_error'),
                ('print-prereq', 'FAILED', 'semantic_error'),
                ('build-lpp', 'BLOCKED', 'build_directory_preparation')):
            driver, writer, reader, saved, process = fixture()
            result = driver.build(name)
            self.assertEqual((result.status, result.reason), (status, reason), name)
            checks = driver.observations.preparations
            self.assertTrue(any(p.status == 'PREPARED' and p.signature == SIGNATURES[name] for p in checks))
            self.assertEqual(len(process.requests), 3)
            self.assertEqual(writer.requests, [])

    def test_doperlmod_prepares_folded_syseval_without_executing_it(self):
        driver, writer, reader, saved, process = fixture({'work/unpost_i': 'EXISTS'})
        node = driver.graph.find_node('doperlmod')
        value = BuildSignaturePreparer('latin-1').prepare(
            node.definitions[0], node, driver.scope)
        self.assertEqual(value.status, 'PREPARED')
        self.assertEqual(value.signature, SIGNATURES['doperlmod'])
        self.assertIn("\t\t:syseval sed -ne 's!.*L<\\([^>][^>]*\\).*!\\1!p' $name |"
                      '\t\t:assign modulename\n', value.commands)
        self.assertIn("\t\t:syseval sed -ne 's!.*L<\\([^>][^>]*\\).*!\\1!p' $name |"
                      '\t:assign modulename\n', value.canonical)
        self.assertEqual(len(process.requests), 3)
        self.assertEqual(writer.requests, [])
        self.assertEqual(saved.writes, [])
        result = driver.build('doperlmod')
        self.assertEqual((result.status, result.reason),
                         ('COMPLETE', 'requested_targets_complete'))
        self.assertEqual([(body.status, body.reason, body.span.start.line)
                          for body in result.bodies],
                         [('COMPLETED', 'semantic_body_completed', 748)])
        # The first :tree match submits its shell redirect and runs :move.
        self.assertEqual(len(process.requests), 6)

    def test_nano_rpm_signature_and_controlled_result_are_unchanged(self):
        driver, writer, reader, saved, process = fixture()
        result = driver.build('rpm')
        self.assertEqual(result.status, 'COMPLETE')
        rpm = [p for p in driver.observations.preparations if p.target.name == 'rpm']
        self.assertTrue(rpm)
        self.assertEqual(rpm[-1].canonical, '\t:pass\n')
        self.assertEqual(rpm[-1].signature, '3966681317ecb02ebc59fd8d3ae5a72f')
        self.assertEqual(len(result.pending_signatures), 18)
        self.assertEqual(saved.writes, [])


if __name__ == '__main__':
    unittest.main()
