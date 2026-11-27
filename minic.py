#!/usr/bin/env python3
"""MiniC compiler.

Pipeline: source -> lexer (tokens) -> parser (syntax tree) -> semantic analysis
-> three-address code -> optimizer -> assembly for a small virtual CPU -> simulator.

Usage: python3 minic.py program.mc
"""
import difflib
import json
import re
import sys
from collections import namedtuple


def err(phase, line, col, msg, severity='error'):
    return {'phase': phase, 'line': line, 'col': col, 'msg': msg, 'severity': severity}


def wrap32(x):
    return (x + 2**31) % 2**32 - 2**31


OPS = {
    '+': lambda a, b: a + b, '-': lambda a, b: a - b, '*': lambda a, b: a * b,
    '<': lambda a, b: int(a < b), '<=': lambda a, b: int(a <= b),
    '>': lambda a, b: int(a > b), '>=': lambda a, b: int(a >= b),
    '==': lambda a, b: int(a == b), '!=': lambda a, b: int(a != b),
}


def arith(op, a, b):
    """32-bit C integer semantics, shared by the optimizer and the simulator so both agree."""
    if op in ('/', '%'):
        if b == 0:
            raise ZeroDivisionError('division by zero')
        q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)  # C truncates toward zero
        return wrap32(q if op == '/' else a - b * q)
    return wrap32(OPS[op](a, b))


# ---------------------------------------------------------------- 1. Lexer

Tok = namedtuple('Tok', 'kind value line col')
KEYWORDS = {'int', 'bool', 'void', 'if', 'else', 'while', 'for', 'return', 'print', 'true', 'false'}
TOKEN_RE = re.compile(r'''
   (?P<space>[ \t\r\n]+)
  |(?P<comment>//[^\n]*|/\*.*?\*/)
  |(?P<badcomment>/\*.*)
  |(?P<badnum>\d+[A-Za-z_]\w*)
  |(?P<num>\d+)
  |(?P<id>[A-Za-z]\w*)
  |(?P<badid>_\w*)
  |(?P<str>"(?:[^"\\\n]|\\.)*")
  |(?P<badstr>"[^\n]*)
  |(?P<op>==|!=|<=|>=|&&|\|\||[-+*/%<>=!(){},;])
  |(?P<bad>.)
''', re.X | re.S)
LEX_ERRORS = {
    'badcomment': 'unterminated comment', 'badnum': "invalid number '{}'",
    'badid': "invalid name '{}': names must start with a letter",
    'badstr': 'unterminated string', 'bad': "unexpected character '{}'",
}


def lex(src, errors):
    toks, line, line_start = [], 1, 0
    for m in TOKEN_RE.finditer(src):
        kind, text, col = m.lastgroup, m.group(), m.start() - line_start + 1
        if kind == 'num':
            if int(text) > 2**31 - 1:
                errors.append(err('lexical', line, col, f'integer {text} is too large (max 2147483647)'))
            toks.append(Tok('NUM', int(text), line, col))
        elif kind == 'id':
            toks.append(Tok(text if text in KEYWORDS else 'ID', text, line, col))
        elif kind == 'str':
            value = re.sub(r'\\(.)', lambda e: {'n': '\n', 't': '\t'}.get(e[1], e[1]), text[1:-1])
            toks.append(Tok('STR', value, line, col))
        elif kind == 'op':
            toks.append(Tok(text, text, line, col))
        elif kind in LEX_ERRORS:
            errors.append(err('lexical', line, col, LEX_ERRORS[kind].format(text.split('\n')[0])))
        if '\n' in text:
            line += text.count('\n')
            line_start = m.start() + text.rindex('\n') + 1
    toks.append(Tok('EOF', '', line, len(src) - line_start + 1))
    return toks


# ---------------------------------------------------------------- 2. Parser (recursive descent)

class Node:
    def __init__(self, kind, at, **fields):
        self.kind, self.line, self.col = kind, at.line, at.col
        self.__dict__.update(fields)


class ParseError(Exception):
    def __init__(self, tok, msg):
        super().__init__(msg)
        self.tok, self.msg = tok, msg


def suggest(name, candidates, cutoff=0.6):
    """' (did you mean 'x'?)' for the closest candidate, or ''."""
    match = difflib.get_close_matches(name, [c for c in candidates if c != name], n=1, cutoff=cutoff)
    return f" (did you mean '{match[0]}'?)" if match else ''


def describe(tok):
    return {'EOF': 'end of file', 'STR': 'a string'}.get(tok.kind, f"'{tok.value}'")


BINARY_LEVELS = [('||',), ('&&',), ('==', '!='), ('<', '<=', '>', '>='), ('+', '-'), ('*', '/', '%')]
STMT_START = ('if', 'while', 'for', 'return', 'print', 'int', 'bool', '{')
STMT_WORDS = ('if', 'else', 'while', 'for', 'return', 'print', 'int', 'bool', 'void')


class Parser:
    def __init__(self, toks, errors):
        self.toks, self.i, self.errors = toks, 0, errors
        self.start = 0  # index of the token that starts the current statement
        self.closed = None  # (name, line) of the '}' that ended the last function

    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def at(self, *kinds):
        return self.peek().kind in kinds

    def next(self):
        tok = self.peek()
        self.i = min(self.i + 1, len(self.toks) - 1)
        return tok

    def fail(self, msg):
        raise ParseError(self.peek(), msg)

    def report(self, line, col, msg):
        self.errors.append(err('syntax', line, col, msg))

    def expect(self, kind, what=None):
        if self.at(kind):
            return self.next()
        self.fail(f"expected {what or repr(kind)} but found {describe(self.peek())}")

    def semi(self):
        if self.at(';'):
            self.next()
            return
        prev = self.toks[self.i - 1]  # report a missing ';' where it belongs, then carry on as if it were there
        self.report(prev.line, prev.col + len(str(prev.value)), f"expected ';' after {describe(prev)}" + self.keyword_hint())

    def keyword_hint(self):
        """A statement that fails to parse and starts with a misspelled keyword ('retrun x;') gets a hint."""
        first, second = self.toks[self.start], self.toks[self.start + 1]
        if first.kind == 'ID' and second.kind != '=':
            return suggest(first.value, STMT_WORDS)
        return ''

    def program(self):
        funcs = []
        while not self.at('EOF'):
            try:
                funcs.append(self.function())
            except ParseError as e:
                self.report(e.tok.line, e.tok.col, e.msg)
                self.next()
                while not self.at('EOF') and not (self.at('int', 'bool', 'void') and self.peek(1).kind == 'ID'
                                                  and self.peek(2).kind == '('):
                    self.next()
        return Node('Program', self.toks[0], funcs=funcs)

    def function(self):
        if not self.at('int', 'bool', 'void'):
            tok = self.peek()
            if self.closed and (tok.kind in STMT_START or tok.kind in ('ID', '}', 'else')):
                name, line = self.closed  # code after a function ended: usually a missing '{' or an extra '}'
                self.fail(f"{describe(tok)} is outside any function: the '}}' on line {line} already closed '{name}' "
                          f"(is a '{{' missing, or is there an extra '}}'?)")
            self.fail(f"expected a function such as 'int main() {{ ... }}' but found {describe(tok)}")
        rtype = self.next().kind
        name = self.expect('ID', 'a function name')
        if self.at('=', ';'):
            self.fail(f"global variables are not supported; declare '{name.value}' inside a function")
        self.expect('(', f"'(' after '{name.value}'")
        params = []
        if not self.at(')'):
            while True:
                if not self.at('int', 'bool'):
                    self.fail(f"expected a parameter type (int or bool) but found {describe(self.peek())}")
                ptype = self.next().kind
                pname = self.expect('ID', 'a parameter name')
                params.append(Node('Param', pname, ptype=ptype, name=pname.value))
                if not self.at(','):
                    break
                self.next()
        self.expect(')')
        body = self.block()
        self.closed = (name.value, self.toks[self.i - 1].line)
        return Node('Function', name, rtype=rtype, name=name.value, params=params, body=body)

    def block(self):
        start = self.expect('{')
        stmts = []
        while not self.at('}', 'EOF'):
            try:
                stmts.append(self.statement())
            except ParseError as e:
                self.report(e.tok.line, e.tok.col, e.msg + self.keyword_hint())
                self.sync()
        if self.at('EOF'):
            self.report(self.peek().line, self.peek().col, f"missing '}}' to close the block opened at line {start.line}")
        else:
            self.next()
        return Node('Block', start, stmts=stmts)

    def sync(self):
        """Panic-mode recovery: skip to the end of the broken statement."""
        while not self.at('EOF', '}'):
            if self.next().kind == ';' or self.at(*STMT_START):
                return

    def statement(self):
        self.start = self.i
        tok = self.peek()
        k = tok.kind
        if k in ('int', 'bool'):
            s = self.vardecl()
            self.semi()
            return s
        if k == 'void':
            self.fail("variables cannot have type 'void'")
        if k == '{':
            return self.block()
        if k == ';':
            self.next()
            return Node('Block', tok, stmts=[])
        if k in ('if', 'while'):
            self.next()
            self.expect('(', f"'(' after '{k}'")
            cond = self.expr()
            self.expect(')')
            body = self.statement()
            if k == 'while':
                return Node('While', tok, cond=cond, body=body)
            orelse = None
            if self.at('else'):
                self.next()
                orelse = self.statement()
            return Node('If', tok, cond=cond, then=body, orelse=orelse)
        if k == 'for':
            self.next()
            self.expect('(', "'(' after 'for'")
            init = None if self.at(';') else self.vardecl() if self.at('int', 'bool') else self.simple()
            self.expect(';')
            cond = None if self.at(';') else self.expr()
            self.expect(';')
            update = None if self.at(')') else self.simple()
            self.expect(')')
            return Node('For', tok, init=init, cond=cond, update=update, body=self.statement())
        if k == 'return':
            self.next()
            value = None if self.at(';') else self.expr()
            self.semi()
            return Node('Return', tok, value=value)
        if k == 'print':
            self.next()
            self.expect('(', "'(' after 'print'")
            args = []
            if not self.at(')'):
                while True:
                    if self.at('STR'):
                        s = self.next()
                        args.append(Node('Str', s, value=s.value))
                    else:
                        args.append(self.expr())
                    if not self.at(','):
                        break
                    self.next()
            self.expect(')')
            self.semi()
            return Node('Print', tok, args=args)
        if k == 'else':
            self.fail("'else' without a matching 'if'")
        s = self.simple()
        self.semi()
        return s

    def vardecl(self):
        vtype = self.next().kind
        name = self.expect('ID', 'a variable name')
        init = None
        if self.at('='):
            self.next()
            init = self.expr()
        return Node('VarDecl', name, vtype=vtype, name=name.value, init=init)

    def simple(self):
        """Assignment or expression statement (no trailing ';')."""
        if self.at('ID') and self.peek(1).kind == '=':
            name = self.next()
            self.next()
            return Node('Assign', name, name=name.value, value=self.expr())
        e = self.expr()
        return Node('ExprStmt', e, expr=e)

    def expr(self, level=0):
        if level == len(BINARY_LEVELS):
            return self.unary()
        left = self.expr(level + 1)
        while self.at(*BINARY_LEVELS[level]):
            op = self.next()
            left = Node('Binary', op, op=op.kind, left=left, right=self.expr(level + 1))
        return left

    def unary(self):
        if self.at('-', '!'):
            op = self.next()
            return Node('Unary', op, op=op.kind, operand=self.unary())
        return self.primary()

    def primary(self):
        tok = self.peek()
        if tok.kind == 'NUM':
            self.next()
            return Node('Num', tok, value=tok.value)
        if tok.kind in ('true', 'false'):
            self.next()
            return Node('Bool', tok, value=tok.kind == 'true')
        if tok.kind == 'ID':
            self.next()
            if not self.at('('):
                return Node('Var', tok, name=tok.value)
            self.next()
            args = []
            if not self.at(')'):
                args.append(self.expr())
                while self.at(','):
                    self.next()
                    args.append(self.expr())
            self.expect(')')
            return Node('Call', tok, name=tok.value, args=args)
        if tok.kind == '(':
            self.next()
            e = self.expr()
            self.expect(')')
            return e
        if tok.kind == 'STR':
            self.fail('strings can only be used inside print()')
        self.fail(f'expected an expression but found {describe(tok)}')


def tree(n, role=''):
    """Syntax tree as {label, children} for display."""
    attrs, kids = [], []
    for k, v in vars(n).items():
        if k in ('kind', 'line', 'col'):
            continue
        if isinstance(v, Node):
            kids.append(tree(v, k))
        elif isinstance(v, list):
            kids += [tree(x) for x in v]
        elif v is not None:
            attrs.append(f'{k}={v!r}' if isinstance(v, str) else f'{k}={v}')
    label = (f'{role}: ' if role else '') + ' '.join([n.kind] + attrs) + f'   [line {n.line}]'
    return {'label': label, 'children': kids}


# ---------------------------------------------------------------- 3. Semantic analysis

ARITH, RELATIONAL, EQUALITY, LOGICAL = ('+', '-', '*', '/', '%'), ('<', '<=', '>', '>='), ('==', '!='), ('&&', '||')


def returns(s):
    """True if every path through statement s ends in a return."""
    if s.kind == 'Return':
        return True
    if s.kind == 'Block':
        return any(returns(x) for x in s.stmts)
    if s.kind == 'If':
        return s.orelse is not None and returns(s.then) and returns(s.orelse)
    return False


class Analyzer:
    def __init__(self, errors):
        self.errors = errors

    def error(self, n, msg, severity='error'):
        self.errors.append(err('semantic', n.line, n.col, msg, severity))

    def program(self, prog):
        self.funcs = {}
        for f in prog.funcs:
            if f.name in self.funcs:
                self.error(f, f"function '{f.name}' is already defined")
            else:
                self.funcs[f.name] = f
        main = self.funcs.get('main')
        if not main:
            self.errors.append(err('semantic', 1, 1, "no 'main' function: add 'int main() { ... }'"))
        elif main.rtype != 'int' or main.params:
            self.error(main, "'main' must be declared as 'int main()'")
        for f in prog.funcs:
            self.function(f)

    def function(self, f):
        self.func, self.scopes, self.counts = f, [{}], {}
        for p in f.params:
            p.uid = self.declare(p, p.ptype, p.name)
        for s in f.body.stmts:  # parameters and the body's top level share one scope, as in C
            self.stmt(s)
        if f.rtype != 'void' and not returns(f.body):
            self.error(f, f"function '{f.name}' may finish without returning a value (0 is returned)", 'warning')

    def declare(self, n, typ, name):
        if name in self.scopes[-1]:
            self.error(n, f"'{name}' is already declared in this scope")
        k = self.counts.get(name, 0)
        self.counts[name] = k + 1
        uid = name if k == 0 else f'{name}.{k}'  # shadowed variables get unique names for code generation
        self.scopes[-1][name] = (typ, uid)
        return uid

    def lookup(self, n, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        self.error(n, f"undeclared variable '{name}'" + suggest(name, set().union(*self.scopes)))

    def check(self, n, want, what):
        t = self.expr(n)
        if t and t != want:
            self.error(n, f'{what}: expected {want} but got {t}')

    def stmt(self, s):
        k = s.kind
        if k == 'Block':
            self.scopes.append({})
            for x in s.stmts:
                self.stmt(x)
            self.scopes.pop()
        elif k == 'VarDecl':
            if s.init:
                self.check(s.init, s.vtype, f"cannot initialize '{s.name}'")
            s.uid = self.declare(s, s.vtype, s.name)
        elif k == 'Assign':
            sym = self.lookup(s, s.name)
            if sym:
                s.uid = sym[1]
                self.check(s.value, sym[0], f"cannot assign to '{s.name}'")
            else:
                self.expr(s.value)
        elif k in ('If', 'While'):
            self.check(s.cond, 'bool', f"'{k.lower()}' condition")
            self.stmt(s.then if k == 'If' else s.body)
            if k == 'If' and s.orelse:
                self.stmt(s.orelse)
        elif k == 'For':
            self.scopes.append({})
            if s.init:
                self.stmt(s.init)
            if s.cond:
                self.check(s.cond, 'bool', "'for' condition")
            if s.update:
                self.stmt(s.update)
            self.stmt(s.body)
            self.scopes.pop()
        elif k == 'Return':
            f = self.func
            if s.value is None:
                if f.rtype != 'void':
                    self.error(s, f"function '{f.name}' must return a value of type {f.rtype}")
            elif f.rtype == 'void':
                self.expr(s.value)
                self.error(s, f"void function '{f.name}' cannot return a value")
            else:
                self.check(s.value, f.rtype, f"return value of '{f.name}'")
        elif k == 'Print':
            for a in s.args:
                if a.kind != 'Str' and self.expr(a) == 'void':
                    self.error(a, 'cannot print a void value')
        elif k == 'ExprStmt':
            self.expr(s.expr)
            if s.expr.kind != 'Call':
                self.error(s, 'expression result is unused', 'warning')

    def expr(self, n):
        n.type = self._expr(n)
        return n.type

    def _expr(self, n):
        k = n.kind
        if k == 'Num':
            return 'int'
        if k == 'Bool':
            return 'bool'
        if k == 'Var':
            if n.name in self.funcs and not any(n.name in s for s in self.scopes):
                self.error(n, f"'{n.name}' is a function; call it with '{n.name}(...)'")
                return None
            sym = self.lookup(n, n.name)
            if sym:
                n.uid = sym[1]
                return sym[0]
            return None
        if k == 'Unary':
            t = self.expr(n.operand)
            want = 'int' if n.op == '-' else 'bool'
            if t and t != want:
                self.error(n, f"operator '{n.op}' needs {want} but got {t}")
            return want
        if k == 'Binary':
            a, b, op = self.expr(n.left), self.expr(n.right), n.op
            if op in ARITH or op in RELATIONAL:
                ok = a == b == 'int'
            elif op in LOGICAL:
                ok = a == b == 'bool'
            else:
                ok = a == b and a != 'void'
            if a and b and not ok:
                self.error(n, f"operator '{op}' cannot be applied to {a} and {b}")
            return 'int' if op in ARITH else 'bool'
        if k == 'Call':
            types = [self.expr(a) for a in n.args]
            f = self.funcs.get(n.name)
            if not f:
                is_var = any(n.name in s for s in self.scopes)
                self.error(n, f"'{n.name}' is a variable, not a function" if is_var else f"undeclared function '{n.name}'" + suggest(n.name, list(self.funcs) + ['print']))
                return None
            if len(types) != len(f.params):
                self.error(n, f"function '{f.name}' expects {len(f.params)} argument(s) but {len(types)} given")
            else:
                for i, (a, p, t) in enumerate(zip(n.args, f.params, types)):
                    if t and t != p.ptype:
                        self.error(a, f"argument {i + 1} of '{f.name}': expected {p.ptype} but got {t}")
            return f.rtype


# ---------------------------------------------------------------- 4. Three-address code
# Instructions are tuples: ('=', d, s) ('bin', d, op, a, b) ('un', d, op, a) ('label', L) ('goto', L)
# ('iffalse', c, L) ('iftrue', c, L) ('param', x) ('call', d|None, f, n) ('ret', x|None)
# ('print', x) ('printb', x) ('prints', s) ('println',) ('func', f, params) ('end', f)
# Operands are ints (constants) or names. Temporaries start with '_', which user names cannot.
# Each instruction is an Ins: a tuple that also remembers the source line it came from.

class Ins(tuple):
    line = 0


def at(line, ins):
    i = Ins(ins)
    i.line = line
    return i


class TACGen:
    def __init__(self):
        self.code, self.temps, self.labels, self.line = [], 0, 0, 0

    def emit(self, *ins):
        self.code.append(at(self.line, ins))

    def temp(self):
        self.temps += 1
        return f'_t{self.temps}'

    def label(self):
        self.labels += 1
        return f'L{self.labels}'

    def program(self, prog):
        for f in prog.funcs:
            self.line = f.line
            self.emit('func', f.name, [p.uid for p in f.params])
            self.stmt(f.body)
            if self.code[-1][0] != 'ret':
                self.emit('ret', None if f.rtype == 'void' else 0)
            self.emit('end', f.name)
        return self.code

    def assign(self, uid, e):
        x = 0 if e is None else self.expr(e)
        last = self.code[-1]
        if isinstance(x, str) and x.startswith('_t') and last[0] in ('bin', 'un', 'call') and last[1] == x:
            self.code[-1] = at(last.line, (last[0], uid) + last[2:])  # write the result straight into the variable
        else:
            self.emit('=', uid, x)

    def stmt(self, s):
        k = s.kind
        if k != 'Block':
            self.line = s.line
        if k == 'Block':
            for x in s.stmts:
                self.stmt(x)
        elif k == 'VarDecl':
            self.assign(s.uid, s.init)  # uninitialized variables start at 0
        elif k == 'Assign':
            self.assign(s.uid, s.value)
        elif k == 'If':
            c, l_else = self.expr(s.cond), self.label()
            self.emit('iffalse', c, l_else)
            self.stmt(s.then)
            if s.orelse:
                l_end = self.label()
                self.line = s.line
                self.emit('goto', l_end)
                self.emit('label', l_else)
                self.stmt(s.orelse)
                self.line = s.line
                self.emit('label', l_end)
            else:
                self.line = s.line
                self.emit('label', l_else)
        elif k in ('While', 'For'):
            if k == 'For' and s.init:
                self.stmt(s.init)
            top, end = self.label(), self.label()
            self.emit('label', top)
            if s.cond:
                self.emit('iffalse', self.expr(s.cond), end)
            self.stmt(s.body)
            if k == 'For' and s.update:
                self.stmt(s.update)
            self.line = s.line
            self.emit('goto', top)
            self.emit('label', end)
        elif k == 'Return':
            self.emit('ret', None if s.value is None else self.expr(s.value))
        elif k == 'Print':
            values = [None if a.kind == 'Str' else self.expr(a) for a in s.args]  # evaluate all args first
            for a, v in zip(s.args, values):
                if a.kind == 'Str':
                    self.emit('prints', a.value)
                else:
                    self.emit('printb' if a.type == 'bool' else 'print', v)
            self.emit('println')
        elif k == 'ExprStmt':
            self.expr(s.expr)

    def expr(self, e):
        k = e.kind
        if k == 'Num':
            return e.value
        if k == 'Bool':
            return int(e.value)
        if k == 'Var':
            return e.uid
        if k == 'Unary':
            a, t = self.expr(e.operand), self.temp()
            self.emit('un', t, e.op, a)
            return t
        if k == 'Binary' and e.op in LOGICAL:  # short-circuit: skip the right side when the left decides
            t, end = self.temp(), self.label()
            left = self.expr(e.left)
            self.emit('=', t, left)
            self.emit('iffalse' if e.op == '&&' else 'iftrue', t, end)
            right = self.expr(e.right)
            self.emit('=', t, right)
            self.emit('label', end)
            return t
        if k == 'Binary':
            a = self.expr(e.left)
            b = self.expr(e.right)
            t = self.temp()
            self.emit('bin', t, e.op, a, b)
            return t
        if k == 'Call':
            args = [self.expr(a) for a in e.args]
            for a in args:
                self.emit('param', a)
            t = self.temp() if e.type != 'void' else None
            self.emit('call', t, e.name, len(args))
            return t


TAC_FMT = {
    '=': lambda d, s: f'{d} = {s}',
    'bin': lambda d, o, a, b: f'{d} = {a} {o} {b}',
    'un': lambda d, o, a: f'{d} = {o}{a}',
    'label': lambda lbl: f'{lbl}:',
    'goto': lambda lbl: f'goto {lbl}',
    'iffalse': lambda c, lbl: f'ifFalse {c} goto {lbl}',
    'iftrue': lambda c, lbl: f'ifTrue {c} goto {lbl}',
    'param': lambda x: f'param {x}',
    'call': lambda d, f, n: f'{d} = call {f}, {n}' if d else f'call {f}, {n}',
    'ret': lambda x: 'return' if x is None else f'return {x}',
    'print': lambda x: f'print {x}',
    'printb': lambda x: f'printBool {x}',
    'prints': lambda s: f'print {json.dumps(s)}',
    'println': lambda: 'printNewline',
    'func': lambda f, ps: f"function {f}({', '.join(ps)}):",
    'end': lambda f: f'end {f}',
}


def tac_rows(code):
    """(text, source line) for every displayed line of TAC."""
    rows = []
    for ins in code:
        indent = '' if ins[0] in ('func', 'label', 'end') else '    '
        rows.append((indent + TAC_FMT[ins[0]](*ins[1:]), ins.line))
        if ins[0] == 'end':
            rows.append(('', 0))
    while rows and not rows[-1][0]:
        rows.pop()
    return rows


def fmt_tac(code):
    return '\n'.join(t for t, _ in tac_rows(code))


def count_tac(code):
    return sum(ins[0] not in ('func', 'end', 'label') for ins in code)


# ---------------------------------------------------------------- 5. Optimizer

READS = {'=': (2,), 'bin': (3, 4), 'un': (3,), 'iffalse': (1,), 'iftrue': (1,),
         'param': (1,), 'print': (1,), 'printb': (1,), 'ret': (1,)}
JUMPS = ('goto', 'iffalse', 'iftrue')


def simplify(op, a, b):
    """Value of `a op b` if known at compile time (a constant or an operand), else None."""
    if isinstance(a, int) and isinstance(b, int):
        return None if op in ('/', '%') and b == 0 else arith(op, a, b)  # keep x/0 as a runtime error
    if b == 0 and op in ('+', '-') or b == 1 and op in ('*', '/'):
        return a
    if a == 0 and op == '+' or a == 1 and op == '*':
        return b
    if op == '*' and (a == 0 or b == 0):
        return 0
    return None


def fold_propagate(code):
    """Constant folding, constant/copy propagation and algebraic simplification inside basic blocks."""
    out, env, line = [], {}, [0]

    def emit(ins):
        out.append(at(line[0], ins))  # keep the source line of the instruction being rewritten

    def val(x):
        return env.get(x, x) if isinstance(x, str) else x

    def kill(d):
        env.pop(d, None)
        for k in [k for k, v in env.items() if v == d]:
            del env[k]

    def assign(d, s):
        kill(d)
        if s != d:
            env[d] = s
            emit(('=', d, s))

    for ins in code:
        line[0] = ins.line
        op = ins[0]
        if op == '=':
            assign(ins[1], val(ins[2]))
        elif op == 'bin':
            _, d, o, a, b = ins
            a, b = val(a), val(b)
            r = simplify(o, a, b)
            if r is not None:
                assign(d, r)
            else:
                kill(d)
                emit(('bin', d, o, a, b))
        elif op == 'un':
            _, d, o, a = ins
            a = val(a)
            if isinstance(a, int):
                assign(d, wrap32(-a) if o == '-' else int(not a))
            else:
                kill(d)
                emit(('un', d, o, a))
        elif op in ('iffalse', 'iftrue'):
            c = val(ins[1])
            if not isinstance(c, int):
                emit((op, c, ins[2]))
            elif (c == 0) == (op == 'iffalse'):  # jump always taken; otherwise never taken and dropped
                emit(('goto', ins[2]))
        elif op in ('param', 'print', 'printb'):
            emit((op, val(ins[1])))
        elif op == 'ret':
            emit(('ret', val(ins[1])))
        elif op == 'call':
            if ins[1]:
                kill(ins[1])
            emit(ins)
        else:
            if op == 'label':  # another block may jump here: forget what we knew
                env.clear()
            emit(ins)
    return out


def remove_dead(code):
    """Drop assignments whose result is never read (no side effects: calls are kept)."""
    used = {ins[i] for ins in code for i in READS.get(ins[0], ())}
    out = []
    for ins in code:
        if ins[0] in ('=', 'bin', 'un') and ins[1] not in used:
            continue
        if ins[0] == 'call' and ins[1] is not None and ins[1] not in used:
            ins = at(ins.line, ('call', None) + ins[2:])
        out.append(ins)
    return out


def remove_unreachable(code):
    """Drop code after goto/return, jumps to the next line, and labels nobody jumps to."""
    out, dead = [], False
    for ins in code:
        if ins[0] in ('label', 'end'):
            dead = False
        if not dead:
            out.append(ins)
        if ins[0] in ('goto', 'ret'):
            dead = True
    out = [ins for i, ins in enumerate(out)
           if not (ins[0] in JUMPS and i + 1 < len(out) and out[i + 1] == ('label', ins[-1]))]
    targets = {ins[-1] for ins in out if ins[0] in JUMPS}
    return [ins for ins in out if ins[0] != 'label' or ins[1] in targets]


def optimize(code):
    out, func = [], []
    for ins in code:
        func.append(ins)
        if ins[0] == 'end':
            while True:  # repeat until nothing changes
                new = remove_unreachable(remove_dead(fold_propagate(func)))
                if new == func:
                    break
                func = new
            out += func
            func = []
    return out


def cfg(code):
    """Basic blocks of each function, with their successor edges and whether they can ever run."""
    funcs = []
    for ins in code:
        op = ins[0]
        if op == 'func':
            blocks, new_block = [], True
            funcs.append({'name': ins[1], 'blocks': blocks})
        elif op != 'end':
            if new_block or (op == 'label' and blocks[-1]['ins']):  # a jump ends a block; a label starts one
                blocks.append({'ins': []})
            blocks[-1]['ins'].append(ins)
            new_block = op in JUMPS or op == 'ret'
    for f in funcs:
        blocks = f['blocks']
        where = {ins[1]: i for i, b in enumerate(blocks) for ins in b['ins'] if ins[0] == 'label'}
        for i, b in enumerate(blocks):
            last = b['ins'][-1]
            b['succ'] = []
            if last[0] in JUMPS:
                b['succ'].append([where[last[-1]], {'goto': '', 'iffalse': 'false', 'iftrue': 'true'}[last[0]], 'jump'])
            if last[0] not in ('goto', 'ret') and i + 1 < len(blocks):
                b['succ'].append([i + 1, {'iffalse': 'true', 'iftrue': 'false'}.get(last[0], ''), 'fall'])
        seen, todo = {0}, [0]
        while todo:
            for nxt, _, _ in blocks[todo.pop()]['succ']:
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        for i, b in enumerate(blocks):
            b['reachable'] = i in seen
            b['code'] = [TAC_FMT[ins[0]](*ins[1:]) for ins in b['ins']]
            b['lines'] = sorted({ins.line for ins in b['ins']} - {0})
            del b['ins']
    return funcs


# ---------------------------------------------------------------- 6. Code generation (virtual CPU)
# Registers R0 (return value), R1, R2. Memory is the current function's frame of named slots.
# A value stack carries call arguments.

ASM_OP = {'+': 'ADD', '-': 'SUB', '*': 'MUL', '/': 'DIV', '%': 'MOD',
          '<': 'LT', '<=': 'LE', '>': 'GT', '>=': 'GE', '==': 'EQ', '!=': 'NE'}
ASM_ARITH = {v: k for k, v in ASM_OP.items()}


def codegen(tac):
    asm = [at(0, ('CALL', 'main', 0)), at(0, ('HALT',))]
    line = 0

    def add(*ins):
        asm.append(at(line, ins))

    def load(r, x):
        add('LOADI' if isinstance(x, int) else 'LOAD', r, x)

    for ins in tac:
        op, line = ins[0], ins.line
        if op == 'func':
            add('LABEL', ins[1])
            for p in reversed(ins[2]):
                add('POP', 'R1')
                add('STORE', 'R1', p)
        elif op == '=':
            load('R1', ins[2])
            add('STORE', 'R1', ins[1])
        elif op == 'bin':
            load('R1', ins[3])
            load('R2', ins[4])
            add(ASM_OP[ins[2]], 'R1', 'R2')
            add('STORE', 'R1', ins[1])
        elif op == 'un':
            load('R1', ins[3])
            add('NEG' if ins[2] == '-' else 'NOT', 'R1')
            add('STORE', 'R1', ins[1])
        elif op == 'label':
            add('LABEL', '.' + ins[1])
        elif op == 'goto':
            add('JMP', '.' + ins[1])
        elif op in ('iffalse', 'iftrue'):
            load('R1', ins[1])
            add('JZ' if op == 'iffalse' else 'JNZ', 'R1', '.' + ins[2])
        elif op == 'param':
            load('R1', ins[1])
            add('PUSH', 'R1')
        elif op == 'call':
            add('CALL', ins[2], ins[3])
            if ins[1]:
                add('STORE', 'R0', ins[1])
        elif op == 'ret':
            if ins[1] is not None:
                load('R0', ins[1])
            add('RET')
        elif op in ('print', 'printb'):
            load('R1', ins[1])
            add(op.upper(), 'R1')
        elif op == 'prints':
            add('PRINTS', ins[1])
        elif op == 'println':
            add('PRINTLN')
    return asm


def fmt_asm(asm):
    return '\n'.join(f'{ins[1]}:' if ins[0] == 'LABEL' else
                     (f'    {ins[0]:<7} ' + ', '.join(json.dumps(a) if ins[0] == 'PRINTS' else str(a)
                                                     for a in ins[1:])).rstrip()
                     for ins in asm)


# ---------------------------------------------------------------- 7. Simulator

def run_vm(asm, max_steps=2_000_000, max_depth=1000, trace_limit=0):
    """Execute the assembly. With trace_limit > 0, also record the machine state before each of the first
    trace_limit steps (for the step-by-step debugger)."""
    labels = {ins[1]: i for i, ins in enumerate(asm) if ins[0] == 'LABEL'}
    reg = {'R0': 0, 'R1': 0, 'R2': 0}
    stack, frames, calls, ret_addrs, out, trace = [], [{}], [], [], [], []
    pc = steps = printed = 0
    error = None
    try:
        while True:
            if len(trace) < trace_limit:
                trace.append({'pc': pc, 'reg': dict(reg), 'vars': dict(frames[-1]), 'calls': list(calls),
                              'stack': list(stack), 'out': printed})
            ins = asm[pc]
            op = ins[0]
            pc += 1
            steps += 1
            if steps > max_steps:
                raise RuntimeError(f'stopped after {max_steps:,} steps (infinite loop?)')
            if op == 'LOADI':
                reg[ins[1]] = ins[2]
            elif op == 'LOAD':
                if ins[2] not in frames[-1]:
                    raise RuntimeError(f"'{ins[2]}' read before it was assigned")
                reg[ins[1]] = frames[-1][ins[2]]
            elif op == 'STORE':
                frames[-1][ins[2]] = reg[ins[1]]
            elif op in ASM_ARITH:
                reg[ins[1]] = arith(ASM_ARITH[op], reg[ins[1]], reg[ins[2]])
            elif op == 'NEG':
                reg[ins[1]] = wrap32(-reg[ins[1]])
            elif op == 'NOT':
                reg[ins[1]] = int(not reg[ins[1]])
            elif op == 'JMP':
                pc = labels[ins[1]]
            elif op == 'JZ':
                if reg[ins[1]] == 0:
                    pc = labels[ins[2]]
            elif op == 'JNZ':
                if reg[ins[1]] != 0:
                    pc = labels[ins[2]]
            elif op == 'PUSH':
                stack.append(reg[ins[1]])
            elif op == 'POP':
                reg[ins[1]] = stack.pop()
            elif op == 'CALL':
                if len(ret_addrs) >= max_depth:
                    raise RuntimeError(f'stack overflow: more than {max_depth} nested calls')
                ret_addrs.append(pc)
                frames.append({})
                calls.append(ins[1])
                pc = labels[ins[1]]
            elif op == 'RET':
                frames.pop()
                calls.pop()
                pc = ret_addrs.pop()
            elif op in ('PRINT', 'PRINTB', 'PRINTS', 'PRINTLN'):
                text = {'PRINT': lambda: str(reg[ins[1]]), 'PRINTB': lambda: 'true' if reg[ins[1]] else 'false',
                        'PRINTS': lambda: ins[1], 'PRINTLN': lambda: '\n'}[op]()
                out.append(text)
                printed += len(text)
            elif op == 'HALT':
                break
    except (RuntimeError, ZeroDivisionError) as e:
        error = f'runtime error: {e}'
    return {'output': ''.join(out), 'exit_code': None if error else reg['R0'], 'error': error, 'steps': steps,
            'trace': trace}


# ---------------------------------------------------------------- Driver

def compile_source(src):
    """Run every phase; stops before code generation if there are errors."""
    errors = []
    toks = lex(src, errors)
    syntax_errors = []
    prog = Parser(toks, syntax_errors).program()
    if not errors:  # after lexical errors, syntax errors are mostly follow-on noise from the skipped characters
        errors += syntax_errors
    if not errors:
        Analyzer(errors).program(prog)
    errors.sort(key=lambda e: (e['line'], e['col']))
    r = {'errors': errors, 'tokens': [list(t) for t in toks[:-1]], 'tree': tree(prog),
         'tac': '', 'opt_tac': '', 'asm': '', 'run': None, 'stats': {}}
    if any(e['severity'] == 'error' for e in errors):
        return r
    tac = TACGen().program(prog)
    opt = optimize(tac)
    asm = codegen(opt)
    r.update(tac=fmt_tac(tac), opt_tac=fmt_tac(opt), asm=fmt_asm(asm), run=run_vm(asm, trace_limit=TRACE_LIMIT),
             stats={'tac': count_tac(tac), 'opt_tac': count_tac(opt), 'asm': sum(i[0] != 'LABEL' for i in asm)},
             # source line of every displayed TAC / assembly line, for linked highlighting in the GUI
             tac_lines=[line for _, line in tac_rows(tac)], opt_lines=[line for _, line in tac_rows(opt)],
             asm_lines=[i.line for i in asm], cfg={'tac': cfg(tac), 'opt': cfg(opt)})
    return r


TRACE_LIMIT = 5000


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[-1])
    r = compile_source(open(sys.argv[1], encoding='utf-8').read())
    for e in r['errors']:
        print(f"{sys.argv[1]}:{e['line']}:{e['col']}: {e['severity']} ({e['phase']}): {e['msg']}")
    if not r['run']:
        sys.exit(1)
    s = r['stats']
    for title, key in (('Three-address code', 'tac'), (f"Optimized TAC ({s['tac']} -> {s['opt_tac']} instructions)",
                                                        'opt_tac'), ('Assembly', 'asm')):
        print(f'\n===== {title} =====\n{r[key]}')
    run = r['run']
    print(f"\n===== Output =====\n{run['output']}", end='')
    print(run['error'] or f"[program exited with code {run['exit_code']} after {run['steps']} steps]")


if __name__ == '__main__':
    main()
