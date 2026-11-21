"""Run the selector on a folder of patches that has answers.json and report
misses and cost per commit.

Usage: python evaluate.py <dir with commits/ tests.json answers.json> [selector.py]
Only commits that already have an answer are scored.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

d = sys.argv[1]
selector = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "selector", "select_tests.py")
answers = json.load(open(os.path.join(d, "answers.json")))["commits"]
tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ids = {t["number"]: t["id"] for t in tests}

tmp = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp, "commits"))
shutil.copy(os.path.join(d, "tests.json"), tmp)
for cid in answers:
    shutil.copy(os.path.join(d, "commits", cid + ".patch"), os.path.join(tmp, "commits"))
env = {**os.environ, "BR_DATA": tmp, "BR_SRC": os.environ.get("BR_SRC", "/home/claude/sqlglot-src")}
r = subprocess.run([sys.executable, selector], capture_output=True, text=True, env=env)
why = {}
for line in r.stderr.splitlines():
    parts = line.split(" ", 1)
    if parts and parts[0] in answers:
        why[parts[0]] = parts[1][:220] if len(parts) > 1 else ""
total = 0
misses = 0
for line in r.stdout.splitlines():
    o = json.loads(line)
    w = o["workers"]
    sel = {n for x in w for n in x}
    cost = (max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) if w else 0
    fail = answers[o["commit"]]["failing"]
    missed = [ids[n] for n in fail if n not in sel]
    total += cost
    flag = ""
    if missed:
        misses += 1
        flag = f"  MISSED {len(missed)}: {missed[:4]}"
    print(f"{o['commit']:28s} fail={len(fail):4d} sel={len(sel):5d} cost={cost/1000:7.2f}{flag}")
    if missed:
        print("     why:", why.get(o["commit"]))
print(f"commits={len(answers)} cost={total/1000:.1f}s misses={misses} -> {total/1000 + 300*misses:.1f}s")
print(r.stderr.splitlines()[-1] if r.stderr else "")
