"""Conservative Python 3.4 source/API guard for the frontend milestone.

Run with Python 3.4 or newer. Newer ast feature_version modes are not trusted:
some hosts ignore the requested grammar version. This checker uses an explicit
3.4 AST vocabulary, field checks, token checks and a small stdlib API budget.
It is a static guard, not a replacement for running tests on Python 3.4.
"""
import ast
import io
import os
import sys
import tokenize


_NODES = frozenset('''Module Interactive Expression FunctionDef ClassDef Return
Delete Assign AugAssign For While If With Raise Try Assert Import ImportFrom
Global Nonlocal Expr Pass Break Continue BoolOp BinOp UnaryOp Lambda IfExp Dict
Set ListComp SetComp DictComp GeneratorExp Yield YieldFrom Compare Call Num Str
Bytes NameConstant Ellipsis Attribute Subscript Starred Name List Tuple Load
Store Del And Or Add Sub Mult Div Mod Pow LShift RShift BitOr BitXor BitAnd
FloorDiv Invert Not UAdd USub Eq NotEq Lt LtE Gt GtE Is IsNot In NotIn
comprehension ExceptHandler arguments arg keyword alias withitem Slice ExtSlice
Index Suite Param Constant'''.split())

# All modules imported by the new implementation, its tests and this guard.
# Attribute checks apply to module-qualified API usage; local object methods
# remain a reviewed contract. Additions require explicit 3.4 API review.
_APIS = {
    'ast': frozenset(('parse', 'walk', 'iter_child_nodes', 'iter_fields')),
    'binascii': frozenset(('hexlify',)),
    'bisect': frozenset(('bisect_right',)),
    'collections': frozenset(('namedtuple', 'Counter')),
    '__future__': frozenset(('print_function',)),
    'csv': frozenset(('DictReader',)),
    'hashlib': frozenset(('md5', 'sha256')),  # Available since Python 2.5; explicit byte input.
    'http.client': frozenset(('HTTPException',)),
    'http.server': frozenset(('BaseHTTPRequestHandler', 'HTTPServer')),
    'io': frozenset(('open', 'StringIO', 'BytesIO')),
    'json': frozenset(('load', 'dump', 'dumps')),
    'os': frozenset(('path', 'pathsep', 'environ', 'walk', 'getcwd',
                     'getpid', 'getppid', 'mkdir', 'makedirs', 'close',
                     'unlink', 'utime', 'open', 'rename', 'stat', 'listdir',
                     'O_CREAT', 'O_EXCL', 'O_WRONLY')),
    'os.path': frozenset(('abspath', 'dirname', 'join', 'isfile', 'exists',
                         'lexists', 'isdir', 'isabs', 'expanduser', 'realpath',
                         'islink', 'getsize')),
    'posixpath': frozenset(('dirname', 'basename', 'isabs', 'join', 'normpath', 'relpath')),
    # finditer/findall/escape/M predate 3.4; used by evidence inventories.
    're': frozenset(('compile', 'finditer', 'findall', 'escape', 'search', 'error', 'M', 'I')),
    'shutil': frozenset(('copyfile', 'copy', 'rmtree')),
    'shlex': frozenset(('quote',)),
    'subprocess': frozenset(('Popen', 'PIPE')),
    'string': frozenset(('ascii_letters', 'digits')),
    'sys': frozenset(('path', 'argv', 'version_info', 'exit', 'stderr', 'stdout')),
    'sys.path': frozenset(('insert',)),
    'tempfile': frozenset(('TemporaryDirectory', 'mkstemp')),
    'time': frozenset(('time',)),
    'threading': frozenset(('Thread',)),
    'tokenize': frozenset(('generate_tokens', 'NUMBER', 'OP', 'NAME', 'TokenError',
                          'COMMENT', 'NL', 'NEWLINE', 'ENDMARKER')),
    'unittest': frozenset(('TestCase', 'main')),
    'urllib.error': frozenset(('URLError', 'HTTPError')),
    'urllib.request': frozenset(('urlopen',)),
}
_NEW_METHODS = frozenset(('removeprefix', 'removesuffix', 'is_relative_to',
                          'read_text', 'write_text', 'read_bytes', 'write_bytes',
                          'isascii', 'bit_count'))


def _chain(node):
    if type(node).__name__ == 'Name':
        return node.id
    if type(node).__name__ == 'Attribute':
        base = _chain(node.value)
        if base:
            return base + '.' + node.attr
    return None


def check_text(text, filename='<string>'):
    """Return diagnostics; fail closed for syntax beyond the reviewed subset."""
    errors = []
    try:
        tree = ast.parse(text, filename)
    except SyntaxError as error:
        return ['{0}:{1}: {2}'.format(filename, error.lineno, error.msg)]

    def reject(node, reason):
        errors.append('{0}:{1}: {2}'.format(filename,
                      getattr(node, 'lineno', 1), reason))

    imports = {}
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
        kind = type(node).__name__
        if kind not in _NODES:
            reject(node, 'AST node unavailable in Python 3.4: ' + kind)
        if kind == 'Import':
            for item in node.names:
                if item.name not in _APIS:
                    reject(node, 'module outside reviewed Python 3.4 API budget: ' + item.name)
                imports[item.asname or item.name] = item.name
        elif kind == 'ImportFrom' and node.level == 0:
            if node.module in _APIS:
                for item in node.names:
                    if item.name not in _APIS[node.module]:
                        reject(node, 'unreviewed imported API: ' + node.module + '.' + item.name)
            elif not (node.module == 'build_standalone'
                      or node.module == 'aap_frontend'
                      or node.module.startswith('aap_frontend.')
                      or node.module == 'aap_semantics'
                      or node.module.startswith('aap_semantics.')
                      or node.module in ('check_frontend_py34', 'checksum_census',
                                         'nano_target_frontier', 'aggregate_ports_frontier', 'test_cd',
                                         'test_actions', 'test_port_runtime',
                                         'output_adapter', 'fetch_adapter', 'aap_cli',
                                         'process_adapter', 'process_tracing',
                                         'host_filesystem', 'host_persistence',
                                         'integration_evidence', 'runtime_factory')):
                reject(node, 'unreviewed module: ' + node.module)
        if getattr(node, 'posonlyargs', ()):
            reject(node, 'positional-only parameters require Python 3.8')
        if getattr(node, 'type_params', ()):
            reject(node, 'type parameters require Python 3.12')
        if getattr(node, 'is_async', 0):
            reject(node, 'async comprehensions require Python 3.6')
        if kind == 'Dict' and any(key is None for key in node.keys):
            reject(node, 'dictionary unpacking requires Python 3.5')
        if kind == 'Constant' and not isinstance(node.value,
                (str, bytes, int, float, complex, bool, type(None), type(Ellipsis))):
            reject(node, 'unreviewed constant type')
        if kind == 'Call':
            if (_chain(node.func) == 'subprocess.Popen'
                    and any(kw.arg == 'text' for kw in node.keywords)):
                reject(node, 'text keyword requires Python 3.7')
            starred = [i for i, arg in enumerate(node.args)
                       if type(arg).__name__ == 'Starred']
            if starred and (len(starred) > 1 or starred[0] != len(node.args) - 1):
                reject(node, 'extended call unpacking requires Python 3.5')
            unpacked = [i for i, kw in enumerate(node.keywords) if kw.arg is None]
            if unpacked and (len(unpacked) > 1 or unpacked[0] != len(node.keywords) - 1):
                reject(node, 'extended keyword unpacking requires Python 3.5')
            if (type(node.func).__name__ == 'Name'
                    and node.func.id in ('breakpoint', 'aiter', 'anext')):
                reject(node, 'builtin unavailable in Python 3.4')
            if (type(node.func).__name__ == 'Name'
                    and node.func.id in ('eval', 'exec', 'compile', '__import__')):
                reject(node, 'dynamic execution is forbidden in the port')
        if kind == 'With':
            # Conservatively disallow grouped with-items, added in Python 3.9.
            # Multiple unparenthesized items have always been supported.
            line = text.splitlines()[node.lineno - 1].lstrip()
            if line.startswith('with ('):
                reject(node, 'parenthesized with-items outside reviewed 3.4 syntax')

    for node in ast.walk(tree):
        kind = type(node).__name__
        if kind == 'Starred' and type(node.ctx).__name__ == 'Load':
            if type(parents.get(id(node))).__name__ != 'Call':
                reject(node, 'iterable display unpacking requires Python 3.5')
        if kind == 'Attribute':
            if node.attr in _NEW_METHODS:
                reject(node, 'method unavailable in Python 3.4: ' + node.attr)
            chain = _chain(node)
            if chain:
                parts = chain.split('.')
                if parts[0] in imports:
                    full = imports[parts[0]] + '.' + '.'.join(parts[1:])
                    module, member = full.rsplit('.', 1)
                    if member not in _APIS.get(module, ()):
                        reject(node, 'unreviewed Python 3.4 API: ' + full)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError) as error:
        return errors + [filename + ': ' + str(error)]
    for token in tokens:
        if token.type == tokenize.NUMBER and '_' in token.string:
            errors.append('{0}:{1}: numeric separators require Python 3.6'.format(
                filename, token.start[0]))
    return errors


def implementation_files(root):
    for directory in ('tests/src/aap_frontend', 'tests/frontend',
                      'tests/src/aap_semantics', 'tests/semantics'):
        for current, dirs, files in os.walk(os.path.join(root, directory)):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            for filename in sorted(files):
                if filename.endswith('.py'):
                    yield os.path.join(current, filename)
    yield os.path.join(root, 'tests/tools/check_frontend_py34.py')
    yield os.path.join(root, 'tests/tools/checksum_census.py')
    yield os.path.join(root, 'tests/tools/nano_target_frontier.py')
    yield os.path.join(root, 'tests/tools/aggregate_ports_frontier.py')
    yield os.path.join(root, 'tests/adapters/output_adapter.py')
    yield os.path.join(root, 'tests/adapters/fetch_adapter.py')
    yield os.path.join(root, 'tests/adapters/process_adapter.py')
    yield os.path.join(root, 'tests/adapters/process_tracing.py')
    yield os.path.join(root, 'tests/adapters/host_filesystem.py')
    yield os.path.join(root, 'tests/adapters/host_persistence.py')
    yield os.path.join(root, 'tests/adapters/integration_evidence.py')
    yield os.path.join(root, 'tests/adapters/runtime_factory.py')


def main():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    files = list(implementation_files(root))
    errors = []
    for path in files:
        with io.open(path, encoding='utf-8') as stream:
            errors.extend(check_text(stream.read(), path))
    for error in errors:
        print(error)
    if errors:
        return 1
    print('Python 3.4 static syntax/API guard: {0} files passed'.format(len(files)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
