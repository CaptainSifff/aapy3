"""Controlled Nano publication requests retain shell patterns as shell text."""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from nano_target_frontier import fixture


class NanoWildcardTests(unittest.TestCase):
    def test_rpmcp_targets_submit_opaque_batched_shell_text(self):
        for target, destination in (
                ('rpmcp', '/export/company/SLES15SP6/bsus/noarch'),
                ('rpmcp_x86', '/export/company/SLES15SP6/bsus')):
            driver, writer, reader, saved, process = fixture()
            original_files = dict(writer.files)
            result = driver.build(target)
            self.assertEqual(result.status, 'COMPLETE', target)
            self.assertEqual(result.bodies[-1].target.name, target)
            self.assertEqual(len(process.requests), 4)
            request = process.requests[-1]
            self.assertEqual(request.command,
                'cp distfiles/* ' + destination + '\ncreaterepo --pretty '
                '/export/company/SLES15SP6/bsus')
            self.assertEqual(request.cwd, '/authorized/ports/editors/nano')
            self.assertEqual(request.expression.kind, 'SEQUENCE')
            self.assertEqual(request.entries[0].expression.argv,
                             ('cp', 'distfiles/*', destination))
            self.assertEqual(writer.files, original_files)
            self.assertEqual(writer.requests, [])
            self.assertEqual(driver.capabilities.port_runtime.artifacts.observations,
                [('exists', '/authorized/ports/editors/nano/distfiles/nano-7.1.tar.gz')])
            self.assertEqual(saved.writes, [])


if __name__ == '__main__':
    unittest.main()
