"""The plain-source bundle does not need the development checkout."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from build_standalone import generate, self_check


class StandaloneBundleTests(unittest.TestCase):
    def test_deterministic_manifest_and_isolated_cli(self):
        first = generate()
        self.assertEqual(first, generate())
        self.assertNotIn("sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))", first)
        self.assertEqual(self_check(first), 59)


if __name__ == '__main__':
    unittest.main()
