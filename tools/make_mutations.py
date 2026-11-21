"""Make random small patches against sqlglot, like the sample commits, to test
the selector on changes it has never seen.

Usage: python make_mutations.py <sqlglot_src> <out_dir> <count> <seed>
"""

import difflib
import os
import random
import re
import sys

src, out_dir, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(out_dir, exist_ok=True)

py_files = []
for d, _, fs in os.walk(os.path.join(src, "sqlglot")):
    if "__pycache__" in d:
        continue
    for f in fs:
        if f.endswith(".py"):
            py_files.append(os.path.relpath(os.path.join(d, f), src))
fixtures = []
for d, _, fs in os.walk(os.path.join(src, "tests", "fixtures")):
    for f in fs:
        if f.endswith(".sql"):
            fixtures.append(os.path.relpath(os.path.join(d, f), src))
test_files = [os.path.relpath(os.path.join(d, f), src) for d, _, fs in os.walk(os.path.join(src, "tests")) for f in fs if f.endswith(".py") and "fixtures" not in d]

SWAPS = [
    (r" and ", " or "), (r" or ", " and "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
    (r" >= ", " > "), (r" == ", " != "), (r" != ", " == "), (r" is not ", " is "), (r" is None", " == None"),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r" not ", " "), (r"\+ 1\b", "+ 2"), (r"- 1\b", "- 2"),
    (r"\b0\b", "1"), (r"\b1\b", "0"), (r"\b2\b", "3"), (r'"([A-Z_]{2,})"', r'"\1X"'), (r"\.this\b", ".expression"),
    (r"\bin\b", "not in"), (r"return ", "return not "), (r"\bany\(", "all("), (r"\ball\(", "any("),
]
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# TODO: revisit", "# noqa"]


def write_patch(name, path, old, new):
    diff = difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", lineterm="")
    body = "\n".join(diff)
    header = f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n"
    with open(os.path.join(out_dir, name + ".patch"), "w") as f:
        f.write(header + body + "\n")


def mutate_py(path):
    lines = open(os.path.join(src, path), encoding="utf-8").read().split("\n")
    for _ in range(200):
        i = rng.randrange(len(lines))
        line = lines[i]
        s = line.strip()
        if not s or s.startswith("#") or s.startswith('"""') or s.startswith("import") or s.startswith("from "):
            continue
        rng.shuffle(SWAPS)
        for pat, rep in SWAPS:
            if re.search(pat, line):
                new = re.sub(pat, rep, line, count=1)
                if new != line:
                    out = lines[:i] + [new] + lines[i + 1 :]
                    return lines, out
    return None


def delete_line(path):
    lines = open(os.path.join(src, path), encoding="utf-8").read().split("\n")
    for _ in range(200):
        i = rng.randrange(len(lines))
        s = lines[i].strip()
        # Whole one-line statements or dict entries only.
        if s and not s.startswith("#") and (s.endswith(",") or (s.count("(") == s.count(")") and not s.endswith(":") and not s.endswith("\\"))):
            if s.startswith(("def ", "class ", "@", "return", "else", "elif", "try", "except", "finally", '"""', ")", "]", "}")):
                continue
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt.startswith((")", "]", "}")) and not s.endswith(","):
                continue
            return lines, lines[:i] + lines[i + 1 :]
    return None


def comment_py(path):
    lines = open(os.path.join(src, path), encoding="utf-8").read().split("\n")
    i = rng.randrange(len(lines))
    indent = re.match(r"\s*", lines[i]).group(0)
    return lines, lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:]


def mutate_fixture(path):
    lines = open(os.path.join(src, path), encoding="utf-8").read().split("\n")
    cands = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and not l.startswith("--")]
    if not cands:
        return None
    i = rng.choice(cands)
    words = lines[i].split(" ")
    j = rng.randrange(len(words))
    words.insert(j, rng.choice(["x", "1", "AND", "y.b"]))
    return lines, lines[:i] + [" ".join(words)] + lines[i + 1 :]


made = 0
while made < count:
    kind = rng.choices(["code", "comment", "fixture", "test"], weights=[6, 1, 2, 1])[0]
    if kind == "code":
        hot = [p for p in py_files if any(k in p for k in ("dialects/", "parsers/", "generators/", "typing/", "optimizer/"))]
        path = rng.choice(hot if rng.random() < 0.7 else py_files)
        r = mutate_py(path) if rng.random() < 0.8 else delete_line(path)
    elif kind == "comment":
        path = rng.choice(py_files)
        r = comment_py(path)
    elif kind == "fixture":
        path = rng.choice(fixtures)
        r = mutate_fixture(path)
    else:
        path = rng.choice(test_files)
        r = mutate_py(path)
    if r is None:
        continue
    write_patch(f"m{seed}_{made:03d}_{kind}", path, *r)
    made += 1
