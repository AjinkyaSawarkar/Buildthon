"""Random changes to class-level and module-level statements (constants,
lookup tables), the kind the dynamic module diff handles.

Usage: python make_struct_mutations.py <sqlglot_src> <out_dir> <count> <seed>
"""
import ast, difflib, os, random, re, sys

src, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)
files = []
for d, _, fs in os.walk(os.path.join(src, "sqlglot")):
    if "__pycache__" in d:
        continue
    for f in fs:
        if f.endswith(".py"):
            files.append(os.path.relpath(os.path.join(d, f), src))
hot = [f for f in files if any(k in f for k in ("dialects/", "parsers/", "generators/", "typing/", "tokens.py", "parser.py", "generator.py", "expressions/"))]


def toplevel_lines(tree):
    """Line numbers of statements at module or class level (not in functions)."""
    res = []
    def walk(body):
        for st in body:
            if isinstance(st, ast.ClassDef):
                walk(st.body)
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                res.extend(range(st.lineno, st.end_lineno + 1))
    walk(tree.body)
    return res


SWAPS = [(r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\b(\d+)\b", None), (r'"([A-Za-z_ ]{2,})"', None)]
made = 0
while made < count:
    path = rng.choice(hot)
    text = open(os.path.join(src, path), encoding="utf-8").read()
    lines = text.split("\n")
    cand = toplevel_lines(ast.parse(text))
    if not cand:
        continue
    i = rng.choice(cand) - 1
    line = lines[i]
    if not line.strip() or line.strip().startswith("#"):
        continue
    new = None
    if rng.random() < 0.25 and line.strip().endswith(",") and ":" in line:
        nl = lines[:i] + lines[i + 1:]  # delete a dict entry
    else:
        rng.shuffle(SWAPS)
        for pat, rep in SWAPS:
            m = re.search(pat, line)
            if not m:
                continue
            if rep is None and pat.startswith(r"\b(\d"):
                new = line[: m.start()] + str(int(m.group(1)) + 1) + line[m.end():]
            elif rep is None:
                new = line[: m.start()] + '"' + m.group(1) + 'X"' + line[m.end():]
            else:
                new = re.sub(pat, rep, line, count=1)
            break
        if new is None or new == line:
            continue
        nl = lines[:i] + [new] + lines[i + 1:]
    try:
        compile("\n".join(nl), path, "exec")
    except SyntaxError:
        continue
    diff = "\n".join(difflib.unified_diff(lines, nl, f"a/{path}", f"b/{path}", lineterm=""))
    open(os.path.join(out, f"s{seed}_{made:03d}.patch"), "w").write(f"diff --git a/{path} b/{path}\n{diff}\n")
    made += 1
