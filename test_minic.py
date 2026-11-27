"""Tests for the MiniC compiler. Run: python3 -m unittest -v test_minic"""
import unittest
from pathlib import Path

import minic

EXAMPLES = Path(__file__).parent / 'examples'

TRICKY = '''
int side(int x) { print("side ", x); return x; }
int main() {
    int a = 5;
    int b = a;
    a = 7;
    print(b);
    int i = 0; int s = 0;
    while (i < 5) { s = s + i; i = i + 1; }
    print(s);
    bool t = side(0) == 1 && side(2) == 2;
    print(t);
    bool u = side(1) == 1 || side(3) == 3;
    print(u);
    print(-7 / 2, " ", -7 % 2, " ", 7 / -2);
    int x = 1;
    { int x = 2; print(x); }
    print(x);
    int k = 3;
    int z = k * 0; print(z);
    for (int j = 0; j < 3; j = j + 1) { int x = j * 2; print(x); }
    if (!(a > 3) || false) print("no"); else print("yes");
    return a - 7;
}
'''


def run(src):
    r = minic.compile_source(src)
    assert r['run'], r['errors']
    return r['run']


def messages(src):
    return [e['msg'] for e in minic.compile_source(src)['errors']]


class Programs(unittest.TestCase):
    def test_tricky_semantics(self):
        r = run(TRICKY)
        self.assertEqual(r['output'], '5\n10\nside 0\nfalse\nside 1\ntrue\n-3 -1 -3\n2\n1\n0\n0\n2\n4\nyes\n')
        self.assertEqual(r['exit_code'], 0)

    def test_examples(self):
        self.assertIn('fact(7) = 5040   loop: 5040\n', run((EXAMPLES / '1_factorial.mc').read_text(encoding='utf-8'))['output'])
        out = run((EXAMPLES / '2_primes.mc').read_text(encoding='utf-8'))['output']
        self.assertIn('primes up to 30: 10\nis 97 prime? true   is 91 prime? false\ngcd(84, 36) = 12\n', out)
        self.assertEqual(run((EXAMPLES / '3_optimizer_demo.mc').read_text(encoding='utf-8'))['output'],
                         'c = 22\ny = 7, a > y is true\n')

    def test_optimizer_shrinks_code(self):
        s = minic.compile_source((EXAMPLES / '3_optimizer_demo.mc').read_text(encoding='utf-8'))['stats']
        self.assertLess(s['opt_tac'], s['tac'] / 2)

    def test_optimized_code_behaves_like_unoptimized(self):
        sources = [TRICKY] + [p.read_text(encoding='utf-8') for p in EXAMPLES.glob('*.mc')]
        for src in sources:
            errors = []
            prog = minic.Parser(minic.lex(src, errors), errors).program()
            minic.Analyzer(errors).program(prog)
            if any(e['severity'] == 'error' for e in errors):
                continue
            tac = minic.TACGen().program(prog)
            plain, opt = minic.run_vm(minic.codegen(tac)), minic.run_vm(minic.codegen(minic.optimize(tac)))
            self.assertEqual((plain['output'], plain['exit_code']), (opt['output'], opt['exit_code']))


class Errors(unittest.TestCase):
    def test_lexical(self):
        m = messages('int main() { int x = 5 @ 3; int _y = 1; print("oops); return 0; }')
        self.assertIn("unexpected character '@'", m)
        self.assertIn("invalid name '_y': names must start with a letter", m)
        self.assertIn('unterminated string', m)
        self.assertEqual(len(m), 3)  # follow-on syntax errors are not reported

    def test_syntax_errors_all_reported(self):
        m = messages((EXAMPLES / '4_syntax_errors.mc').read_text(encoding='utf-8'))
        self.assertEqual(m, ["expected ';' after '10'", "expected ')' but found ';'",
                             "expected '(' after 'if' but found 'x'", "expected an expression but found ';'"])

    def test_semantic_errors_all_reported(self):
        m = messages((EXAMPLES / '5_semantic_errors.mc').read_text(encoding='utf-8'))
        self.assertEqual(len(m), 9)
        for expected in ["undeclared variable 'y'", "'x' is already declared in this scope",
                         "function 'square' expects 1 argument(s) but 2 given",
                         "argument 1 of 'square': expected int but got bool",
                         "cannot initialize 'g': expected int but got void", "undeclared function 'cube'",
                         "'if' condition: expected bool but got int",
                         "return value of 'main': expected int but got bool"]:
            self.assertIn(expected, m)

    def test_missing_main_and_brace(self):
        self.assertIn("no 'main' function: add 'int main() { ... }'", messages('int f() { return 1; }'))
        self.assertIn("missing '}' to close the block opened at line 1", messages('int main() { return 0;'))

    def test_code_after_function_closed_early(self):
        src = 'int main() {\n    for (int i = 0; i < 3; i = i + 1)\n        print(i);\n    }\n    return 0;\n}'
        self.assertEqual(messages(src), ["'return' is outside any function: the '}' on line 4 already closed 'main' "
                                         "(is a '{' missing, or is there an extra '}'?)"])

    def test_missing_return_is_a_warning(self):
        r = minic.compile_source('int f(int n) { if (n > 0) return 1; } int main() { print(f(0)); return 0; }')
        self.assertEqual([e['severity'] for e in r['errors']], ['warning'])
        self.assertEqual(r['run']['output'], '0\n')


class Runtime(unittest.TestCase):
    def test_division_by_zero(self):
        r = run('int main() { int z = 0; print(10 / z); return 0; }')
        self.assertEqual(r['error'], 'runtime error: division by zero')

    def test_infinite_loop_stops(self):
        self.assertIn('infinite loop', run('int main() { while (true) { } return 0; }')['error'])

    def test_runaway_recursion_stops(self):
        self.assertIn('stack overflow', run('int f(int n) { return f(n + 1); } int main() { return f(0); }')['error'])


class Tools(unittest.TestCase):
    def test_did_you_mean_hints(self):
        self.assertEqual(messages('int main() { int count = 1; print(cout); return 0; }'),
                         ["undeclared variable 'cout' (did you mean 'count'?)"])
        self.assertEqual(messages('int main() { prnt(1); return 0; }'), ["undeclared function 'prnt' (did you mean 'print'?)"])
        self.assertEqual(messages('int main() { retrun 0; }'), ["expected ';' after 'retrun' (did you mean 'return'?)"])

    def test_source_lines_follow_code_to_assembly(self):
        r = minic.compile_source((EXAMPLES / '1_factorial.mc').read_text(encoding='utf-8'))
        tac = dict(zip(r['opt_tac'].split('\n'), r['opt_lines']))
        self.assertEqual(tac['    _t4 = n * _t3'], 4)         # return n * fact(n - 1);
        self.assertEqual(tac['    result = result * i'], 10)
        asm = r['asm'].split('\n')
        self.assertEqual(len(asm), len(r['asm_lines']))
        self.assertEqual(r['asm_lines'][asm.index('    MUL     R1, R2')], 4)

    def test_debugger_trace_matches_run(self):
        r = minic.compile_source((EXAMPLES / '1_factorial.mc').read_text(encoding='utf-8'))
        run, trace = r['run'], r['run']['trace']
        self.assertEqual(len(trace), run['steps'])            # every step recorded (below the limit)
        self.assertEqual(r['asm'].split('\n')[trace[-1]['pc']].split(), ['HALT'])
        self.assertEqual(trace[-1]['out'], len(run['output']))
        self.assertEqual(trace[-1]['reg']['R0'], run['exit_code'])
        self.assertIn(['main', 'fact', 'fact'], [t['calls'] for t in trace])  # recursion is visible

    def test_control_flow_graph(self):
        r = minic.compile_source('int main() { int i = 0; while (i < 3) { i = i + 1; } return i; print(i); }')
        blocks = r['cfg']['tac'][0]['blocks']
        self.assertIn([1, '', 'jump'], [s for b in blocks for s in b['succ']])  # the loop's back edge
        self.assertEqual([b['reachable'] for b in blocks][-1], False)          # print after return
        self.assertEqual(len(r['cfg']['opt'][0]['blocks']), len(blocks) - 1)   # optimizer removed it


if __name__ == '__main__':
    unittest.main()
