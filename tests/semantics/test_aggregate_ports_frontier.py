"""Controlled aggregate discovery from supplied ports/main.aap.

Historical anchors: Commands.aap_tree_recurse and aap_chdir. The child AAP
process is opaque; a fake zero status establishes no package effect.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aggregate_ports_frontier import CanonicalPortsFixture, report


class AggregatePortsFrontierTests(unittest.TestCase):
    def test_ordered_nano_discovery_dispatch_and_cwd_restoration(self):
        data = report()
        self.assertEqual(data['source_boundary'], ['ports/main.aap',
            'ports/globals.aap', 'ports/editors/nano/main.aap',
            'ports/company/efiloader/main.aap'])
        self.assertEqual(data['fixture']['listings'], [
            {'path': '.', 'entries': ['main.aap', 'globals.aap', 'editors', 'company']},
            {'path': './editors', 'entries': ['nano']},
            {'path': './editors/nano', 'entries': ['main.aap']},
            {'path': './company', 'entries': ['efiloader']},
            {'path': './company/efiloader', 'entries': ['main.aap']}])
        full = data['variants']['complete_fixture']
        self.assertEqual((full['status'], full['reason']),
                         ('COMPLETE', 'requested_targets_complete'))
        self.assertIsNone(data['first_genuine_unsupported'])
        self.assertEqual(full['process_requests'], 2)
        self.assertEqual(full['body_final_cwd'], '/authorized/ports')
        self.assertIsNone(full['body_local_name'])
        self.assertEqual(full['body_local_dir'], './company/efiloader')
        events = full['events']
        self.assertEqual([event['kind'] for event in events], [
            'tree_root', 'tree_list', 'tree_classify', 'name_bound', 'dir_derived',
            'tree_classify', 'tree_classify', 'tree_list', 'tree_classify',
            'tree_list', 'tree_classify', 'name_bound', 'dir_derived',
            'directory_entry', 'process_request', 'directory_entry',
            'tree_classify', 'tree_list', 'tree_classify', 'tree_list',
            'tree_classify', 'name_bound', 'dir_derived', 'directory_entry',
            'process_request', 'directory_entry'])
        self.assertEqual([(event['value']) for event in events
                          if event['kind'] == 'name_bound'],
                         ['./main.aap', './editors/nano/main.aap',
                          './company/efiloader/main.aap'])
        self.assertEqual([(event['value']) for event in events
                          if event['kind'] == 'dir_derived'],
                         ['.', './editors/nano', './company/efiloader'])
        self.assertEqual((events[13]['cwd_before'], events[13]['requested'],
                          events[13]['cwd_after']),
                         ('/authorized/ports', '/authorized/ports/./editors/nano',
                          '/authorized/ports/editors/nano'))
        self.assertEqual((events[14]['command'], events[14]['cwd'],
                          events[14]['source_lines'], events[14]['wait_status']),
                         ('aap package\naap distclean',
                          '/authorized/ports/editors/nano', [15, 16], 0))
        self.assertFalse(events[14]['filesystem_effects_observed'])
        self.assertEqual((events[15]['cwd_before'], events[15]['cwd_after'],
                          events[15]['expanded']),
                         ('/authorized/ports/editors/nano', '/authorized/ports', '-'))
        self.assertEqual((events[23]['cwd_before'], events[23]['requested'],
                          events[23]['cwd_after']),
                         ('/authorized/ports', '/authorized/ports/./company/efiloader',
                          '/authorized/ports/company/efiloader'))
        self.assertEqual((events[24]['command'], events[24]['cwd'],
                          events[24]['source_lines'], events[24]['wait_status']),
                         ('aap package\naap distclean',
                          '/authorized/ports/company/efiloader', [15, 16], 0))
        self.assertFalse(events[24]['filesystem_effects_observed'])
        self.assertEqual((events[25]['cwd_before'], events[25]['cwd_after'],
                          events[25]['expanded']),
                         ('/authorized/ports/company/efiloader', '/authorized/ports', '-'))

    def test_missing_observations_are_not_called_new_semantics(self):
        variants = report()['variants']
        for key, line, classification in (
                ('tree_unobserved', 11, 'missing fixture fact'),
                ('directory_unobserved', 14, 'missing fixture fact'),
                ('process_unobserved', 15, 'missing fixture fact')):
            result = variants[key]
            self.assertEqual(result['status'], 'BLOCKED')
            self.assertEqual(result['location']['line'], line)
            self.assertEqual(result['classification'], classification)
            self.assertEqual(result['process_requests'], 0)
        self.assertEqual(variants['process_unobserved']['body_local_name'],
                         './editors/nano/main.aap')
        self.assertEqual(variants['process_unobserved']['body_final_cwd'],
                         '/authorized/ports/editors/nano')

    def test_same_declarative_fixture_serves_other_aggregate_targets(self):
        for target, command in (
                ('cleanall', 'aap clean'),
                ('distcleanall', 'aap distclean'),
                ('index', 'aap IndexEntry')):
            fixture = CanonicalPortsFixture()
            result = fixture.build(target)
            self.assertEqual(result.status, 'COMPLETE', target)
            self.assertEqual(len(fixture.process.requests), 2)
            self.assertEqual([request.command for request in fixture.process.requests],
                             [command, command])
            self.assertEqual([request.cwd for request in fixture.process.requests],
                             ['/authorized/ports/editors/nano',
                              '/authorized/ports/company/efiloader'])
            self.assertEqual(fixture.directories.observations,
                             ['/authorized/ports/./editors/nano', '/authorized/ports',
                              '/authorized/ports/./company/efiloader', '/authorized/ports'])


if __name__ == '__main__':
    unittest.main()
