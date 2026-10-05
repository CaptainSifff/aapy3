"""Exercise the generated single-file launcher outside the source checkout."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_BUNDLE = os.path.join(REPOSITORY, 'github', 'aapy3.py')
ROOT_BUNDLE = os.path.join(REPOSITORY, 'aapy3.py')


def write_bytes(path, value):
    with open(path, 'wb') as stream:
        stream.write(value)


class StandaloneAapy3Tests(unittest.TestCase):
    def test_github_copy_matches_generated_root_bundle(self):
        with open(SOURCE_BUNDLE, 'rb') as github_bundle:
            with open(ROOT_BUNDLE, 'rb') as root_bundle:
                self.assertEqual(github_bundle.read(), root_bundle.read())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        self.bundle = os.path.join(self.root, 'aapy3.py')
        shutil.copyfile(SOURCE_BUNDLE, self.bundle)
        self.recipe_dir = os.path.join(self.root, 'recipe')
        os.mkdir(self.recipe_dir)
        self.wrapper = os.path.join(self.root, 'aap')
        wrapper_text = ('#!/bin/sh\nexec "{0}" -I "{1}" "$@"\n'
                        .format(sys.executable, self.bundle))
        write_bytes(self.wrapper, wrapper_text.encode('utf-8'))
        os.chmod(self.wrapper, 0o755)
        self.environment = dict(os.environ)
        self.environment['AAP'] = self.wrapper
        self.environment['AAP_REPOSITORY'] = os.path.join(self.root, 'absent')
        self.environment.pop('PYTHONPATH', None)

    def tearDown(self):
        self.temporary.cleanup()

    def run_target(self, recipe, target):
        write_bytes(os.path.join(self.recipe_dir, 'main.aap'),
                    recipe.encode('latin-1'))
        process = subprocess.Popen(
            [sys.executable, '-I', self.bundle, target],
            cwd=self.recipe_dir, env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def test_plain_print_reaches_stdout_exactly(self):
        result = self.run_target('hello:\n    :print alpha\n', 'hello')
        self.assertEqual(result, (0, b'alpha\n', b''))

    def test_redirected_print_is_file_only(self):
        result = self.run_target(
            'hello:\n    :print >! captured alpha\n', 'hello')
        self.assertEqual(result, (0, b'', b''))
        with open(os.path.join(self.recipe_dir, 'captured'), 'rb') as stream:
            self.assertEqual(stream.read(), b'alpha\n')

    def test_recursive_child_print_is_captured_by_syseval(self):
        recipe = ('collect:\n'
                  '    :syseval $AAP value | :assign result\n'
                  '    :print $result\n'
                  'value:\n'
                  '    :print one\n'
                  '    :print two\n')
        result = self.run_target(recipe, 'collect')
        self.assertEqual(result, (0, b'one\ntwo\n', b''))

    def test_sys_semicolon_sequence_runs_in_one_shell(self):
        recipe = ('make-file:\n'
                  '    :sys printf a >> result ; printf b >> result\n')
        result = self.run_target(recipe, 'make-file')
        self.assertEqual(result, (0, b'', b''))
        with open(os.path.join(self.recipe_dir, 'result'), 'rb') as stream:
            self.assertEqual(stream.read(), b'ab')


if __name__ == '__main__':
    unittest.main()
