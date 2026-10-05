"""Generate the readable, single-file A-A-P launcher from canonical sources."""
from __future__ import print_function

import io
import hashlib
import os
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT = os.path.join(ROOT, 'aapy3.py')
PACKAGES = ('aap_frontend', 'aap_semantics')
ADAPTERS = (('output_adapter', 'output_adapter.py'),
            ('fetch_adapter', 'fetch_adapter.py'),
            ('process_adapter', 'process_adapter.py'),
            ('process_tracing', 'process_tracing.py'),
            ('host_filesystem', 'host_filesystem.py'),
            ('host_persistence', 'host_persistence.py'),
            ('integration_evidence', 'integration_evidence.py'),
            ('runtime_factory', 'runtime_factory.py'),
            ('__main__', 'aap_cli.py'))

CLI_BOOTSTRAP = ("ROOT = os.environ.get('AAP_REPOSITORY', '/work/repo')\n"
    "sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))\n"
    "sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))\n\n")


def sources():
    for package in PACKAGES:
        directory = os.path.join(ROOT, 'tests', 'src', package)
        for filename in sorted(name for name in os.listdir(directory)
                               if name.endswith('.py')):
            name = package if filename == '__init__.py' else (
                package + '.' + filename[:-3])
            yield name, 'tests/src/' + package + '/' + filename, filename == '__init__.py'
    for name, filename in ADAPTERS:
        yield name, 'tests/adapters/' + filename, False


def generate():
    parts = ['''#!/usr/bin/env python3
"""Standalone plain-source bundle of the bounded A-A-P CLI/runtime.

Project modules and integration adapters are embedded as readable source.
External programs, repositories, and recipe inputs remain host requirements.
"""
from __future__ import print_function

import importlib.util
import linecache
import sys

_EMBEDDED = {}
_MANIFEST = {}

''']
    for name, path, package in sources():
        with io.open(os.path.join(ROOT, path), 'r', encoding='utf-8') as stream:
            source = stream.read()
        if name == '__main__':
            if source.count(CLI_BOOTSTRAP) != 1:
                raise ValueError('development CLI bootstrap changed')
            source = source.replace(CLI_BOOTSTRAP, '')
        if "'''" in source:
            raise ValueError('triple single quote cannot be embedded: ' + path)
        if not source.endswith('\n'):
            raise ValueError('source lacks final newline: ' + path)
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        parts.append('_MANIFEST[{0!r}] = ({1!r}, {2}, {3!r})\n'.format(
            name, path, package, digest))
        parts.append('_EMBEDDED[{0!r}] = ({1!r}, {2}, r\'\'\''.format(
            name, path, package))
        parts.append(source)
        parts.append("''')\n\n")
    parts.append('''class _BundledSourceImporter(object):
    """Load embedded project modules through Python import machinery."""
    def find_spec(self, fullname, path=None, target=None):
        item = _EMBEDDED.get(fullname)
        if item is None or fullname == '__main__':
            return None
        origin = '<aapy3>/' + item[0]
        return importlib.util.spec_from_loader(fullname, self, origin=origin,
                                               is_package=item[1])

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path, is_package, source = _EMBEDDED[module.__name__]
        module.__file__ = '<aapy3>/' + path
        linecache.cache[module.__file__] = (len(source), None,
                                           source.splitlines(True), module.__file__)
        if is_package:
            module.__path__ = [module.__file__.rsplit('/', 1)[0]]
        exec(compile(source, module.__file__, 'exec'), module.__dict__)


sys.meta_path.insert(0, _BundledSourceImporter())

if __name__ == '__main__':
    _path, _is_package, _source = _EMBEDDED['__main__']
    _origin = '<aapy3>/' + _path
    linecache.cache[_origin] = (len(_source), None, _source.splitlines(True), _origin)
    exec(compile(_source, _origin, 'exec'), globals())
''')
    return ''.join(parts)


def self_check(bundle):
    """Check embedded compilation/digests and run away from the source tree."""
    namespace = {'__name__': 'bundle_check'}
    previous_importers = list(sys.meta_path)
    try:
        exec(compile(bundle, '<bundle-check>', 'exec'), namespace)
    finally:
        sys.meta_path[:] = previous_importers
    embedded, manifest = namespace['_EMBEDDED'], namespace['_MANIFEST']
    if set(embedded) != set(manifest):
        raise ValueError('embedded module and manifest names differ')
    for name in sorted(embedded):
        path, package, source = embedded[name]
        entry = manifest[name]
        if entry != (path, package, hashlib.sha256(source.encode('utf-8')).hexdigest()):
            raise ValueError('manifest mismatch: ' + name)
        compile(source, '<aapy3>/' + path, 'exec')
    with tempfile.TemporaryDirectory() as directory:
        launcher = os.path.join(directory, 'aapy3.py')
        with io.open(launcher, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(bundle)
        recipe = os.path.join(directory, 'main.aap')
        with io.open(recipe, 'w', encoding='latin-1') as stream:
            stream.write('value:\n    :print bundled-output\n')
        environment = dict(os.environ)
        environment['AAP'] = sys.executable + ' ' + launcher
        environment['AAP_REPOSITORY'] = os.path.join(directory, 'absent-source')
        environment.pop('PYTHONPATH', None)
        process = subprocess.Popen([sys.executable, '-I', launcher, 'value'],
            cwd=directory, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if process.returncode != 0 or stdout != b'bundled-output\n':
            raise ValueError('isolated bundle smoke failed: {0!r} {1!r} {2}'.format(
                stdout, stderr, process.returncode))
    return len(embedded)


def main(argv):
    expected = generate()
    if argv == ['--check']:
        with io.open(OUTPUT, 'r', encoding='utf-8') as stream:
            actual = stream.read()
        if actual != expected:
            raise SystemExit('aapy3.py differs from canonical sources')
        print('standalone bundle matches canonical sources')
        return
    if argv == ['--self-check']:
        print('standalone bundle: {0} modules verified; isolated CLI passed'.format(
              self_check(expected)))
        return
    if argv:
        raise SystemExit('usage: build_standalone.py [--check|--self-check]')
    with io.open(OUTPUT, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(expected)


if __name__ == '__main__':
    main(sys.argv[1:])
