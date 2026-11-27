---
title: MiniC Studio
emoji: 🛠️
colorFrom: blue
colorTo: purple
sdk: static
app_file: studio.html
---

# MiniC Studio

A visual compiler for MiniC, a small C-like language. Pure Python 3, no packages to install.

```
python3 studio.py              # GUI: opens in your browser at http://127.0.0.1:8000
python3 minic.py program.mc    # command line: errors, TAC, optimized TAC, assembly, output
python3 -m unittest -v test_minic
```

The GUI has an editor with error-line marks, a pipeline bar showing which phases passed, and tabs for
Output, Errors, Tokens, Parse Tree, TAC & Optimizer, Control Flow (basic-block graph), Assembly and Debugger
(step through the program one instruction at a time, with the running source line highlighted). Clicking a
source line highlights the TAC and assembly made from it. Errors suggest fixes ("did you mean 'count'?").
It has light and dark modes.

## Phases (all in `minic.py`)

1. **Lexer**: regex tokenizer. Reports bad characters, unterminated strings and comments, and invalid numbers.
2. **Parser**: recursive descent that builds the syntax tree. Recovers after errors, so it reports all of them.
3. **Semantic analysis**: finds undeclared or redeclared names, type mismatches, wrong argument counts and types, void misuse, bad returns, and a missing `main`.
4. **Three-address code**: short-circuit `&&` / `||`, labels and jumps.
5. **Optimizer** (repeats until nothing changes): constant folding, constant and copy propagation, algebraic simplification, dead-code elimination and unreachable-code elimination.
6. **Code generation**: assembly for a virtual CPU with registers R0–R2, a call stack and a value stack.
7. **Simulator**: runs the assembly. Stops on division by zero, infinite loops (2M steps) and deep recursion (1000 calls).

## The language

```c
int add(int a, int b) { return a + b; }
void hello() { print("hi"); }
int main() {                       // required
    int x = 5;  bool ok = x > 3;    // types: int (32-bit), bool
    if (ok && x != 0) { x = add(x, 1); } else { hello(); }
    while (x > 0) x = x - 2;
    for (int i = 0; i < 3; i = i + 1) print("i = ", i);   // print joins its arguments, then a newline
    return 0;
}
```

Operators: `+ - * / %` (int), `< <= > >=` (int → bool), `== !=` (same types), `&& || !` (bool), unary `-`.
Conditions must be `bool`. Uninitialized variables start at 0. Strings are allowed only inside `print`.
Not supported: global variables, arrays, pointers, input, names starting with `_`.
