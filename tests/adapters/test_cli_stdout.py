"""Real CLI stdout delivery and recursive :syseval capture regressions."""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(ROOT, 'tests', 'adapters', 'aap_cli.py')


def write(path, data):
    with open(path, 'wb') as stream:
        stream.write(data)


class CliPrintStdoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        self.recipe_dir = os.path.join(self.root, 'recipe')
        os.mkdir(self.recipe_dir)
        self.wrapper = os.path.join(self.root, 'aap')
        wrapper = ('#!/bin/sh\nexec "{0}" "{1}" "$@"\n'
                   .format(sys.executable, CLI)).encode('utf-8')
        write(self.wrapper, wrapper)
        os.chmod(self.wrapper, 0o755)
        self.env = os.environ.copy()
        self.env.update({'AAP': self.wrapper, 'AAP_REPOSITORY': ROOT})

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, recipe, target):
        write(os.path.join(self.recipe_dir, 'main.aap'), recipe.encode('latin-1'))
        process = subprocess.Popen([sys.executable, CLI, target],
            cwd=self.recipe_dir, env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def test_external_cli_print_writes_exact_bytes_to_stdout(self):
        status, stdout, stderr = self.run_cli(
            'hello:\n    :print alpha\n', 'hello')
        self.assertEqual((status, stdout, stderr), (0, b'alpha\n', b''))

    def test_external_cli_redirected_print_is_file_only(self):
        status, stdout, stderr = self.run_cli(
            'hello:\n    :print >! captured alpha\n', 'hello')
        self.assertEqual((status, stdout, stderr), (0, b'', b''))
        with open(os.path.join(self.recipe_dir, 'captured'), 'rb') as stream:
            self.assertEqual(stream.read(), b'alpha\n')

    def test_recursive_child_stdout_is_captured_and_assigned(self):
        recipe = ('collect:\n'
                  '    :syseval $AAP value | :assign result\n'
                  '    :print $result\n'
                  'value:\n'
                  '    :print one\n'
                  '    :print two\n')
        status, stdout, stderr = self.run_cli(recipe, 'collect')
        self.assertEqual((status, stdout, stderr), (0, b'one\ntwo\n', b''))
        child = subprocess.Popen([self.wrapper, 'value'], cwd=self.recipe_dir,
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child_stdout, child_stderr = child.communicate()
        self.assertEqual((child.returncode, child_stdout, child_stderr),
                         (0, b'one\ntwo\n', b''))


if __name__ == '__main__':
    unittest.main()
