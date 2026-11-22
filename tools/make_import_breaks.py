"""Patches that make a sqlglot module fail to import: syntax errors and bad
names in top-level code.

Usage: python make_import_breaks.py <sqlglot_src> <out_dir> <count> <seed>
"""
import difflib, os, random, re, sys

src, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)
files = [os.path.relpath(os.path.join(d, f), src) for d, _, fs in os.walk(os.path.join(src, "sqlglot"))
         if "__pycache__" not in d for f in fs if f.endswith(".py")]
made = 0
while made < count:
    path = rng.choice(files)
    lines = open(os.path.join(src, path), encoding="utf-8").read().split("\n")
    cand = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith(("#", '"', "'")) and len(l.strip()) > 8]
    if not cand:
        continue
    i = rng.choice(cand)
    line = lines[i]
    kind = rng.random()
    if kind < 0.4 and ":" in line and line.rstrip().endswith(":"):
        new = line.rstrip()[:-1]  # drop a block colon
    elif kind < 0.7 and re.search(r"exp\.[A-Z]\w+", line) and not line.startswith(" " * 8):
        new = re.sub(r"exp\.([A-Z]\w+)", r"exp.\1Zz", line, count=1)  # bad name at import
    else:
        new = line + " )"  # stray parenthesis
    nl = lines[:i] + [new] + lines[i + 1:]
    text = "\n".join(nl)
    try:
        compile(text, path, "exec")
        if "Zz" not in new:
            continue  # still valid and not a bad-name case
    except SyntaxError:
        pass
    diff = "\n".join(difflib.unified_diff(lines, nl, f"a/{path}", f"b/{path}", lineterm=""))
    open(os.path.join(out, f"b{seed}_{made:03d}.patch"), "w").write(f"diff --git a/{path} b/{path}\n{diff}\n")
    made += 1
