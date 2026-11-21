"""Find which tests really fail on a patch, by running sqlglot's whole suite.

Usage: python ground_truth.py <sqlglot_src> <tests.json> <out.json> patch1 [patch2 ...]
Writes {"commits": {id: {"failing": [...]}}} (merged with an existing out file).
"""

import json
import os
import re
import shutil
import subprocess
import sys

src, tests_json, out = sys.argv[1:4]
patches = sys.argv[4:]
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
py = os.environ.get("PY", sys.executable)

results = json.load(open(out)) if os.path.exists(out) else {"commits": {}}
for p in patches:
    cid = os.path.basename(p)[: -len(".patch")]
    if cid in results["commits"]:
        continue
    work = f"/tmp/gt_work_{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    r = subprocess.run(["git", "apply", "--unsafe-paths", os.path.abspath(p)], cwd=work, capture_output=True, text=True)
    if r.returncode:
        r = subprocess.run(["patch", "-p1", "-i", os.path.abspath(p)], cwd=work, capture_output=True, text=True)
        if r.returncode:
            print(cid, "PATCH FAILED", r.stdout, r.stderr, file=sys.stderr)
            continue
    import glob
    for g in glob.glob(f"/tmp/gt_out_{os.getpid()}*"):
        os.remove(g)
    env = {**os.environ, "SKIP_INTEGRATION": "0", "PYTHONPATH": work + ":" + os.path.dirname(os.path.abspath(__file__)),
           "PYTHONDONTWRITEBYTECODE": "1", "GT_OUT": f"/tmp/gt_out_{os.getpid()}"}
    r = subprocess.run(
        [py, "-m", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider", "-p", "gt_plugin", "--timeout", "120", "--tb=no"],
        cwd=work, capture_output=True, text=True, env=env,
    )
    failing = set()
    for g in glob.glob(f"/tmp/gt_out_{os.getpid()}*"):
        for node in open(g).read().split("\n"):
            if not node:
                continue
            if node.startswith("COLLECT:"):
                f = node[len("COLLECT:"):]
                for tf, nums in by_file.items():
                    if tf == f or tf.startswith(f.rstrip("/") + "/") or f == "":
                        failing.update(nums)
                continue
            if node in num:
                failing.add(num[node])
            else:
                print(cid, "unmatched", node, file=sys.stderr)
    tail = r.stdout.strip().splitlines()[-1:] if r.stdout.strip() else []
    print(cid, len(failing), tail, file=sys.stderr)
    results["commits"][cid] = {"failing": sorted(failing)}
    json.dump(results, open(out, "w"))
