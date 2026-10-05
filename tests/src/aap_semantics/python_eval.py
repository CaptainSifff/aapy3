"""Explicit Python AST interpretation over closed metadata values."""
import ast
import io
import tokenize

from .diagnostics import SemanticError, UndefinedName, Unsupported
from .helpers import HelperRegistry
from .scopes import Namespace
from .values import MISSING, RegexMatchValue, check_value, truth


_ALLOWED = frozenset(('Module', 'Expression', 'Expr', 'Assign', 'Pass', 'If',
                     'For', 'Name', 'Attribute', 'List', 'Tuple', 'Subscript',
                     'Index', 'Compare', 'Eq', 'NotEq', 'GtE', 'In',
                     'BoolOp', 'And', 'UnaryOp', 'Not', 'BinOp', 'Add',
                     'Call', 'Load', 'Store', 'Constant', 'Str', 'Num',
                     'NameConstant'))


def call_path(node):
    if type(node).__name__ == 'Name':
        return node.id
    if type(node).__name__ == 'Attribute':
        base = call_path(node.value)
        if base:
            return base + '.' + node.attr
    return None


def constant(node):
    kind = type(node).__name__
    if kind in ('Constant', 'NameConstant'):
        return node.value
    if kind == 'Str':
        return node.s
    return node.n


class PythonEvaluator(object):
    def __init__(self, scope, helpers=None, max_steps=10000):
        self.scope = scope
        self.helpers = helpers or HelperRegistry()
        self.max_steps = max_steps
        self.steps = 0
        self.port_runtime = None
        self.evaluator_context = None
        self.port_records = []

    def step(self, origin):
        self.steps += 1
        if self.steps > self.max_steps:
            raise Unsupported(origin, 'metadata evaluation step limit exceeded')

    def parse(self, fragment, mode='exec', syntax_only=False):
        try:
            code = fragment.code.strip()
            tree = ast.parse(code, fragment.span.source_id, mode)
            for token in tokenize.generate_tokens(io.StringIO(code).readline):
                if token.type == tokenize.NUMBER and '_' in token.string:
                    raise Unsupported(fragment, 'numeric separators are not Python 3.4 syntax')
        except (SyntaxError, ValueError, tokenize.TokenError) as error:
            if isinstance(error, SemanticError):
                raise
            raise SemanticError(fragment, 'invalid embedded Python: ' + str(error))
        if not syntax_only:
            self.validate(tree, fragment)
        return tree

    def validate(self, tree, origin):
        # globals() is a scope capability only in this exact embedding form.
        # Never construct or expose a host globals dictionary to recipe code.
        port_calls = set()
        globals_calls = set()
        if self.port_runtime is not None:
            for node in ast.walk(tree):
                if type(node).__name__ == 'Call' and call_path(node.func) in self.port_runtime.HELPERS:
                    if (len(node.args) != 1 or type(node.args[0]).__name__ != 'Call'
                            or call_path(node.args[0].func) != 'globals'
                            or node.args[0].args or node.args[0].keywords
                            or getattr(node.args[0], 'starargs', None) is not None
                            or getattr(node.args[0], 'kwargs', None) is not None):
                        raise Unsupported(origin, 'port helper requires exactly globals()')
                    port_calls.add(id(node))
                    globals_calls.add(id(node.args[0]))
        for node in ast.walk(tree):
            kind = type(node).__name__
            if kind not in _ALLOWED:
                raise Unsupported(origin, 'unsupported Python AST node: ' + kind)
            if kind in ('Constant', 'Str', 'Num', 'NameConstant'):
                check_value(constant(node), origin)
            if kind == 'Name' and node.id.startswith('__'):
                raise Unsupported(origin, 'private interpreter names are not permitted')
            if kind == 'Attribute' and node.attr.startswith('__'):
                raise Unsupported(origin, 'private attribute access is not permitted')
            if kind in ('Assign', 'For'):
                targets = node.targets if kind == 'Assign' else [node.target]
                for target in targets:
                    self.validate_target(target, origin)
            if kind == 'For' and node.orelse:
                raise Unsupported(origin, 'for-else is not in the characterized subset')
            if kind == 'Call':
                if (node.keywords or getattr(node, 'starargs', None) is not None
                        or getattr(node, 'kwargs', None) is not None):
                    raise Unsupported(origin, 'keyword and unpacked calls are unsupported')
                path = call_path(node.func)
                if id(node) in port_calls or id(node) in globals_calls:
                    continue
                # Known deferred capabilities are checked only on a reached
                # call. Syntax and the closed interpreter policy remain eager.
                if path in self.helpers.DEFERRED:
                    continue
                if path not in self.helpers.NAMES:
                    if (type(node.func).__name__ != 'Attribute'
                            or node.func.attr not in self.helpers.METHODS):
                        raise Unsupported(origin, 'unapproved call: ' + (path or '<expression>'))

    def validate_target(self, node, origin):
        kind = type(node).__name__
        if kind == 'Name':
            return
        if kind == 'Attribute' and type(node.value).__name__ == 'Name':
            return
        if kind in ('List', 'Tuple'):
            for element in node.elts:
                self.validate_target(element, origin)
            return
        raise Unsupported(origin, 'unsupported Python assignment target: ' + kind)

    def expression(self, fragment):
        tree = self.parse(fragment, 'eval')
        return self.value(tree.body, fragment)

    def value(self, node, origin):
        self.step(origin)
        kind = type(node).__name__
        if kind in ('Constant', 'Str', 'Num', 'NameConstant'):
            return check_value(constant(node), origin)
        if kind == 'Name':
            return self.scope.python_name(node.id, origin)
        if kind == 'Attribute':
            if call_path(node) == 'os.path.curdir':
                self.unshadowed('os.path.curdir', origin)
                return '.'
            namespace = self.value(node.value, origin)
            if not isinstance(namespace, Namespace):
                raise Unsupported(origin, 'attribute reads require an explicit scope namespace')
            value = namespace.get(node.attr)
            if value is MISSING:
                raise UndefinedName(origin, 'undefined scoped Python name: ' + node.attr)
            return value
        if kind in ('List', 'Tuple'):
            values = [self.value(element, origin) for element in node.elts]
            return check_value(values if kind == 'List' else tuple(values), origin)
        if kind == 'Subscript':
            value = self.value(node.value, origin)
            index_node = node.slice
            if type(index_node).__name__ == 'Index':
                index_node = index_node.value
            index = self.value(index_node, origin)
            if type(value) not in (str, list, tuple) or type(index) not in (int, bool):
                raise Unsupported(origin, 'indexing requires a sequence and integer')
            try:
                return value[index]
            except IndexError:
                raise SemanticError(origin, 'sequence index out of range')
        if kind == 'UnaryOp' and type(node.op).__name__ == 'Not':
            return not truth(self.value(node.operand, origin), origin)
        if kind == 'BoolOp' and type(node.op).__name__ == 'And':
            value = None
            for operand in node.values:
                value = self.value(operand, origin)
                if not truth(value, origin):
                    break
            return value
        if kind == 'Compare':
            left = self.value(node.left, origin)
            for operator, right_node in zip(node.ops, node.comparators):
                right = self.value(right_node, origin)
                check_value(left, origin)
                check_value(right, origin)
                op = type(operator).__name__
                if op == 'Eq':
                    answer = left == right
                elif op == 'NotEq':
                    answer = left != right
                elif op == 'GtE':
                    numeric_right = type(right) in (int, bool)
                    if (type(left) is RegexMatchValue and numeric_right):
                        # Historical Python 2 allowed heterogeneous ordering:
                        # numeric types precede non-numeric types. The reached
                        # recipe compares re.search(...) with an integer to
                        # test its match result.
                        answer = True
                    elif left is None and numeric_right:
                        answer = False
                    elif ((type(left) in (int, bool) and numeric_right)
                          or type(left) is str and type(right) is str):
                        answer = left >= right
                    else:
                        raise Unsupported(origin, 'ordering requires strings or integers')
                elif op == 'In':
                    if (type(right) not in (str, list, tuple)
                            or type(right) is str and type(left) is not str):
                        raise SemanticError(origin,
                                            'membership requires compatible sequence values')
                    answer = left in right
                else:
                    raise Unsupported(origin, 'unsupported comparison: ' + op)
                if not answer:
                    return False
                left = right
            return True
        if kind == 'BinOp' and type(node.op).__name__ == 'Add':
            left, right = self.value(node.left, origin), self.value(node.right, origin)
            check_value(left, origin)
            check_value(right, origin)
            if not ((type(left) in (int, bool) and type(right) in (int, bool))
                    or type(left) is type(right) and type(left) in (str, list, tuple)):
                raise SemanticError(origin, 'addition requires compatible metadata values')
            return left + right
        if kind == 'Call':
            path = call_path(node.func)
            try:
                if self.port_runtime is not None and path in self.port_runtime.HELPERS:
                    self.unshadowed(path, origin)
                    self.unshadowed('globals', origin)
                    return self.port_runtime.call(path, self.scope, self.helpers.cwd,
                                                  origin, self.port_records, self.evaluator_context)
                if path in self.helpers.DEFERRED:
                    raise Unsupported(origin, 'deferred capability: ' + path)
                if path in self.helpers.NAMES:
                    self.unshadowed(path, origin)
                    args = [self.value(arg, origin) for arg in node.args]
                    return self.helpers.call(path, args, origin)
                receiver = self.value(node.func.value, origin)
                args = [self.value(arg, origin) for arg in node.args]
                return self.helpers.method(receiver, node.func.attr, args, origin)
            except (TypeError, ValueError, OverflowError) as error:
                if isinstance(error, SemanticError):
                    raise
                raise SemanticError(origin, 'compatibility helper failed: ' + str(error))
        raise Unsupported(origin, 'unsupported Python expression: ' + kind)

    def unshadowed(self, path, origin):
        root = path.split('.')[0]
        if root in self.scope.local or root in self.scope.namespaces:
            raise Unsupported(origin,
                              'calling a shadowed compatibility helper is unsupported: ' + root)

    def assign(self, target, value, origin):
        check_value(value, origin)
        kind = type(target).__name__
        if kind == 'Name':
            self.scope.store(target.id, value, origin)
        elif kind == 'Attribute':
            namespace = self.value(target.value, origin)
            if not isinstance(namespace, Namespace):
                raise Unsupported(origin, 'attribute assignment requires a scope namespace')
            namespace.set(target.attr, value, origin)
        elif kind in ('Tuple', 'List'):
            if type(value) not in (list, tuple) or len(value) != len(target.elts):
                raise SemanticError(origin, 'unpacking requires a matching list or tuple')
            for element, item in zip(target.elts, value):
                self.assign(element, item, origin)
        else:
            raise Unsupported(origin, 'unsupported assignment target')

    def statements(self, statements, origin):
        for node in statements:
            self.step(origin)
            kind = type(node).__name__
            if kind == 'Assign':
                value = self.value(node.value, origin)
                for target in node.targets:
                    self.assign(target, value, origin)
            elif kind == 'Expr':
                self.value(node.value, origin)
            elif kind == 'Pass':
                continue
            elif kind == 'If':
                self.statements(node.body if truth(self.value(node.test, origin), origin)
                                else node.orelse, origin)
            elif kind == 'For':
                iterable = self.sequence(node.iter, origin)
                for value in iterable:
                    self.step(origin)
                    self.assign(node.target, value, origin)
                    self.statements(node.body, origin)
            else:
                raise Unsupported(origin, 'unsupported Python statement: ' + kind)

    def sequence(self, node, origin):
        value = self.value(node, origin)
        if type(value) not in (str, list, tuple):
            raise Unsupported(origin, 'for requires a metadata sequence')
        check_value(value, origin)
        return value
