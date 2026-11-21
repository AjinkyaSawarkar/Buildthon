# Blast Radius: a test selector for sqlglot

This program reads code changes (patches) to sqlglot and decides which of sqlglot's tests each change needs. It then splits those tests across workers so that the slowest worker finishes as early as possible.

## How it decides

Before the contest, we ran every sqlglot test once at the pinned commit (`eb11aec`) and recorded three things for each test:

1. Which lines of code the test ran. This is called coverage.
2. Which files the test opened, such as the SQL fixture files in `tests/fixtures`.
3. Which sqlglot classes the test created, such as a Snowflake parser or an `exp.Select` expression.

All of this is stored in `selector/map.json.gz`. When a patch arrives, the selector looks at each changed file:

- **A Python file.** The selector builds the syntax tree of the file before and after the patch and compares them.
  - If only comments or blank lines changed, the trees are equal, so no test can fail and nothing is run.
  - If a line inside a function changed, the tests that ran that line are chosen.
  - If a line at the top level of a class or module changed (for example a constant or a lookup table), the selector runs the patched module's top-level code in a separate process and compares every class attribute and module name with the original. This shows exactly which names changed, including values that sqlglot computes from them when a class is created. For each changed name, it chooses the tests that use that class or module and that ran a line reading that name. When a changed table is keyed by expression classes, it keeps only the tests that created those expressions.
  - If a patch only swaps `is None` for `== None` (or `is` for `==` with an enum member), the code behaves the same, so nothing is run.
  - If a docstring changed, the doc tests are chosen.
  - If the new file has a syntax error, or its top-level code fails when we run it, every test is chosen.
- **A fixture or other data file.** The tests that opened that file are chosen.
- **Anything we do not understand.** Every test is chosen. Missing a failing test costs 300 seconds, so being careful is cheaper than guessing.

Finally, the chosen tests are split across 1 to 8 workers. For every possible worker count, the program gives each test (longest first) to the least busy worker, and it keeps the worker count with the lowest total cost.

## Files

- `Dockerfile`: builds the image that the scorer runs.
- `selector/select_tests.py`: the selector. It uses only the Python standard library.
- `selector/map.json.gz`: the recorded map described above.
- `selector/src.tgz`: the Python files of sqlglot at the pinned commit. The selector needs the original code to compare against each patch.
- `build/`: the scripts that made the map. `build/build_map.sh` remakes it (it takes about 4 minutes on 2 cores).
- `tools/`: scripts we used to check the selector on our own test patches.

## Running it on the sample

With the starter kit's `Makefile`, `score.py` and `sample/` folder placed next to this file, run:

```sh
make sample
```

On the 30 sample commits this selector scores about 116 simulated seconds with no misses. The starter that runs every test scores 1614 seconds.

## How we checked it

Besides the 30 sample commits, we made more than 160 patches of our own (random one-line code changes, deleted lines, comment-only changes, fixture edits, test file edits, syntax errors, a new file, a README change, and others). For each one we ran sqlglot's full test suite to find the tests that really fail. The selector missed no failing test on any of them.
