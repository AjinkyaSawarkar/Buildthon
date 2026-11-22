"""Turn the raw trace data into one compact map file.

Usage: python make_map.py <trace_dir> <sqlglot_root> <tests.json> <out.json.gz>

Map layout (JSON, gzip):
  sets:     list of sorted lists of test numbers (shared, deduplicated)
  lines:    {file: {line: [set_id_runtime, flags]}}
            flags bit 1 = ran at import, bit 2 = ran in warmup,
                  bit 4 = ran while a test file was collected
  collect:  {test_file: [files whose import code ran while collecting it]}  (not used)
  opens:    {file: set_id}   tests that opened this file (setup opens widened to the class)
  textreads:{file: set_id}   tests that opened a source file as text
  classes:  {"module:qualname": set_id}  tests that created an instance of exactly this class
  mro:      {"module:qualname": ["module:qualname", ...]}  MRO inside sqlglot
  file_tests: {test_file: set_id}
"""

import glob
import gzip
import importlib
import json
import os
import pickle
import sys

trace_dir, root, tests_json, out = sys.argv[1:5]
sys.path.insert(0, root)

tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
file_tests = {}
class_tests = {}  # "file::Class" -> tests
for t in tests:
    file_tests.setdefault(t["file"], set()).add(t["number"])
    parts = t["id"].split("::")
    class_tests.setdefault("::".join(parts[:-1]), set()).add(t["number"])


def expand(ctx):
    """Context name -> (set of test numbers, flag)."""
    if ctx.startswith("c:"):
        n = num.get(ctx[2:])
        return ({n} if n is not None else set()), 0
    if ctx.startswith("s:"):
        nodeid = ctx[2:]
        prefix = "::".join(nodeid.split("::")[:-1])
        s = set(class_tests.get(prefix, set()))
        n = num.get(nodeid)
        if n is not None:
            s.add(n)
        return s, 0
    if ctx.startswith("collect:"):
        f = ctx[len("collect:") :]
        return set(file_tests.get(f, set())), 4
    if ctx == "import" or ctx.startswith("import:"):
        return set(), 1
    if ctx.startswith("meta:"):
        return set(), 64
    if ctx == "warmup":
        return set(), 2
    if ctx == "warmup2":
        return set(), 32
    if ctx == "between" or ctx == "collect:?":
        return set(), 8
    return set(), 0


from coverage import CoverageData  # noqa: E402

lines = {}  # file -> line -> [set, flags]
import_mods = {}  # (file, line) -> modules whose top-level code ran the line
for path in glob.glob(os.path.join(trace_dir, "cov.*")):
    data = CoverageData(basename=path)
    data.read()
    cache = {}
    for f in data.measured_files():
        rel = os.path.relpath(f, root)
        per = lines.setdefault(rel, {})
        for ln, ctxs in data.contexts_by_lineno(f).items():
            entry = per.setdefault(ln, [set(), 0])
            for c in ctxs:
                if c.startswith("import:"):
                    import_mods.setdefault((rel, ln), set()).add(c[len("import:"):])
                if c not in cache:
                    cache[c] = expand(c)
                s, fl = cache[c]
                entry[0] |= s
                entry[1] |= fl

opens, textreads, classes = {}, {}, {}
import_classes = set()
vocab = {}  # upper-case word -> tests whose SQL text (in or out) had it
for path in glob.glob(os.path.join(trace_dir, "extra.*.pkl")):
    d = pickle.load(open(path, "rb"))
    for ctx, items in d["opens"].items():
        s, _ = expand(ctx)
        for rel, mode in items:
            opens.setdefault(rel, set()).update(s)
            if rel.endswith(".py") and "b" not in mode:
                textreads.setdefault(rel, set()).update(s)
    for ctx, items in d.get("words", {}).items():
        s, _ = expand(ctx)
        if not s:
            continue
        for w in items:
            vocab.setdefault(w, set()).update(s)
    for ctx, items in d["classes"].items():
        if ctx.startswith("import") or ctx.startswith("meta:"):
            import_classes.update(items)
        s, _ = expand(ctx)
        for name in items:
            classes.setdefault(name, set()).update(s)

# MRO of every recorded class, restricted to sqlglot classes.
mro = {}
for name in classes:
    mod, qual = name.split(":", 1)
    try:
        obj = importlib.import_module(mod)
        for part in qual.split("."):
            obj = getattr(obj, part)
        mro[name] = [
            f"{k.__module__}:{k.__qualname__}" for k in obj.__mro__ if k.__module__.startswith("sqlglot")
        ]
    except Exception as e:
        print("mro failed", name, e, file=sys.stderr)
        mro[name] = [name]

# Every class defined in sqlglot, with its MRO; which modules `import sqlglot`
# loads (core); and which modules import which at top level.
import ast  # noqa: E402
import inspect  # noqa: E402
import pkgutil  # noqa: E402
import subprocess  # noqa: E402

import sqlglot  # noqa: E402

modules = {}
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        modules[info.name] = importlib.import_module(info.name)
    except Exception as e:
        print("import failed", info.name, e, file=sys.stderr)
modules["sqlglot"] = sqlglot

class_info = {}


def walk_classes(obj, modname, prefix):
    for name, val in list(vars(obj).items()):
        if inspect.isclass(val) and val.__module__ == modname and val.__qualname__ == prefix + name:
            key = f"{modname}:{val.__qualname__}"
            if key in class_info:
                continue
            class_info[key] = [
                f"{k.__module__}:{k.__qualname__}" for k in val.__mro__ if k.__module__.startswith("sqlglot")
            ]
            walk_classes(val, modname, val.__qualname__ + ".")


for modname, mod in modules.items():
    walk_classes(mod, modname, "")
for k, v in class_info.items():
    mro.setdefault(k, v)

core = subprocess.run(
    [sys.executable, "-c", "import sys, sqlglot; print('\\n'.join(m for m in sys.modules if m.startswith('sqlglot')))"],
    cwd=root,
    capture_output=True,
    text=True,
    env={**os.environ, "PYTHONPATH": root},
).stdout.split()


def module_of(path):
    rel = path[:-3].replace(os.sep, ".")
    return rel[: -len(".__init__")] if rel.endswith(".__init__") else rel


def resolve(modname, is_pkg, level, target):
    if level == 0:
        return target
    parts = modname.split(".")
    base = parts if is_pkg else parts[:-1]
    base = base[: len(base) - (level - 1)]
    return ".".join(base + ([target] if target else []))


importers = {}  # module -> modules importing it at top level (module or class body)
from_names = []  # (importer, package, name) for "from package import name"
lazy_importers = {}  # module -> modules that load it through a lazy package name
for dirpath, _, files in os.walk(root):
    rel_dir = os.path.relpath(dirpath, root)
    if not (rel_dir.startswith("sqlglot") or rel_dir.startswith("tests")) or "__pycache__" in rel_dir:
        continue
    if rel_dir.startswith("sqlglotc") or "fixtures" in rel_dir:
        continue
    for fn in files:
        if not fn.endswith(".py"):
            continue
        rel = os.path.normpath(os.path.join(rel_dir, fn))
        me = module_of(rel)
        is_pkg = fn == "__init__.py"
        try:
            tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        except SyntaxError:
            continue
        stack = list(tree.body)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.ClassDef):
                stack.extend(node.body)
            elif isinstance(node, (ast.If, ast.Try)):
                if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
                    stack.extend(node.orelse)  # the body only runs for type checkers
                    continue
                for field in ("body", "orelse", "finalbody", "handlers"):
                    stack.extend(getattr(node, field, []) or [])
            elif isinstance(node, ast.ExceptHandler):
                stack.extend(node.body)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    importers.setdefault(a.name, set()).add(me)
            elif isinstance(node, ast.ImportFrom):
                base = resolve(me, is_pkg, node.level, node.module or "")
                importers.setdefault(base, set()).add(me)
                for a in node.names:
                    importers.setdefault(base + "." + a.name, set()).add(me)
                    from_names.append((me, base, a.name))

# "from sqlglot.dialects import BigQuery" loads sqlglot.dialects.bigquery
# lazily: add an edge to the module the imported object comes from.
for me, base, name in from_names:
    if not base.startswith("sqlglot"):
        continue
    try:
        obj = getattr(importlib.import_module(base), name)
    except Exception:
        continue
    src_mod = getattr(obj, "__module__", None) if not isinstance(obj, type(sys)) else obj.__name__
    if isinstance(src_mod, str) and src_mod.startswith("sqlglot") and src_mod != base:
        lazy_importers.setdefault(src_mod, set()).add(me)

# Where each module- or class-level name is read. A read inside a function
# body is kind 0 (it happens when the function runs); a read in a module or
# class body, a default value, a decorator or a base class list is kind 1
# (it happens at import).
candidates = set()
py_sources = {}
for dirpath, _, files in os.walk(root):
    rel_dir = os.path.relpath(dirpath, root)
    if not (rel_dir.startswith("sqlglot") or rel_dir.startswith("tests")) or "__pycache__" in rel_dir:
        continue
    if rel_dir.startswith("sqlglotc") or "fixtures" in rel_dir:
        continue
    for fn in files:
        if fn.endswith(".py"):
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            try:
                py_sources[rel] = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
            except SyntaxError:
                pass


def collect_candidates(body):
    for st in body:
        if isinstance(st, ast.ClassDef):
            collect_candidates(st.body)
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            tg = st.targets if isinstance(st, ast.Assign) else [st.target]
            for t in tg:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        candidates.add(n.id)
        elif isinstance(st, (ast.If, ast.Try)):
            collect_candidates(st.body)
            collect_candidates(getattr(st, "orelse", []))


for rel, tree in py_sources.items():
    if rel.startswith("sqlglot"):
        collect_candidates(tree.body)

sites = {}


def add_site(name, rel, line, kind, owner=""):
    if name in candidates:
        sites.setdefault(name, set()).add((rel, line, kind, owner if kind else ""))


def _base_name(node):
    """Last name in the expression an attribute hangs off: ``a.b.C.X`` -> C."""
    v = node.value
    if isinstance(v, ast.Attribute):
        return v.attr
    if isinstance(v, ast.Name):
        return v.id
    return "?"


def visit(node, rel, in_func, qual=""):
    """qual: class path of the class body we are in (module level: "")."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in node.decorator_list:
            visit(d, rel, in_func, qual)
        for d in node.args.defaults + [x for x in node.args.kw_defaults if x is not None]:
            visit(d, rel, in_func, qual)
        for st in node.body:
            visit(st, rel, True, qual)
        return
    if isinstance(node, ast.Lambda):
        for d in node.args.defaults + [x for x in node.args.kw_defaults if x is not None]:
            visit(d, rel, in_func, qual)
        visit(node.body, rel, True, qual)
        return
    if isinstance(node, ast.ClassDef):
        for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            visit(d, rel, in_func, qual)
        inner = (qual + "." if qual else "") + node.name
        for st in node.body:
            visit(st, rel, in_func, inner if not in_func else qual)
        return
    if isinstance(node, ast.AnnAssign):
        visit(node.target, rel, in_func, qual)
        if node.value is not None:
            visit(node.value, rel, in_func, qual)
        return
    kind = 0 if in_func else 1
    if isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Store):
            add_site(node.id, rel, node.lineno, kind, f"n:{module_of(rel)}:{qual}")
    elif isinstance(node, ast.Attribute):
        if not isinstance(node.ctx, ast.Store):
            add_site(node.attr, rel, node.lineno, kind, "a:" + _base_name(node))
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        # "S": a plain string; "s": a string handed to getattr() and friends.
        add_site(node.value, rel, node.lineno, kind, "S")
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
        "getattr", "setattr", "hasattr", "delattr"
    ):
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                add_site(a.value, rel, a.lineno, kind, "s")
    for child in ast.iter_child_nodes(node):
        visit(child, rel, in_func, qual)


for rel, tree in py_sources.items():
    for st in tree.body:
        visit(st, rel, False)

import re as _re  # noqa: E402

_WORDS = _re.compile(r"[A-Za-z0-9_]+|[^A-Za-z0-9_\s]")
by_id = {t["id"]: t["number"] for t in tests}
for t in tests:
    parts = t["id"].split("::")
    tree = py_sources.get(parts[0])
    if tree is None or len(parts) < 2:
        continue
    node = tree
    for name in parts[1:]:
        found = None
        for st in getattr(node, "body", []):
            if isinstance(st, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == name:
                found = st
                break
        node = found
        if node is None:
            break
    if node is None:
        continue
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and len(sub.value) < 100_000:
            for w in _WORDS.findall(sub.value):
                vocab.setdefault(w.upper(), set()).add(t["number"])

# Flags read as the condition of an ``if``: "file:line:name" -> [polarity,
# first line of the if body]. polarity 1: ``if X`` or ``if X and ...``;
# polarity -1: ``if not X`` or ``if not X and ...``. When a flag flip turns
# such a condition off, only tests that entered the body can change.
guards = {}


def _flag_name(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


_PURE_CALLS = {"isinstance", "len", "type", "bool"}


def _pure(node):
    """No side effects: names, attributes, constants, comparisons, boolean
    logic and a few builtins."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.NamedExpr) or isinstance(sub, (ast.Await, ast.Yield, ast.YieldFrom)):
            return False
        if isinstance(sub, ast.Call):
            if not (isinstance(sub.func, ast.Name) and sub.func.id in _PURE_CALLS):
                return False
    return True


def _match_call(node):
    """self._match...(...): consumes tokens only when it succeeds."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("_match")
        and all(_pure(a) for a in node.args)
    )


def _guard_visit(node, rel):
    if isinstance(node, (ast.If, ast.While)) and node.body:
        test = node.test
        parts = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else [test]
        for i, p in enumerate(parts):
            pol = 1
            if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
                p, pol = p.operand, -1
            name = _flag_name(p)
            if not (name and name in candidates):
                continue
            # When the flag turns the condition off, the operands after it
            # stop running. That is safe only if they have no side effects,
            # or if the only one left is a _match call (which, when it
            # succeeds, enters the body anyway).
            rest = parts[i + 1:]
            if all(_pure(x) for x in rest) or (len(rest) == 1 and _match_call(rest[0])):
                guards[f"{rel}:{p.lineno}:{name}"] = [pol, node.body[0].lineno]
    for ch in ast.iter_child_nodes(node):
        _guard_visit(ch, rel)


for rel, tree in py_sources.items():
    _guard_visit(tree, rel)

# Imports that run inside functions (``import sqlglot.transforms`` in a
# method): a test that runs that line needs the module.
func_imports = {}
dyn_import_sites = set()  # lines in sqlglot that import a module by name


def _fi_visit(node, rel, in_func):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        for ch in ast.iter_child_nodes(node):
            _fi_visit(ch, rel, True)
        return
    if in_func and isinstance(node, ast.Call):
        f = node.func
        fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if fname in ("import_module", "__import__") and rel.startswith("sqlglot"):
            dyn_import_sites.add((rel, node.lineno))
    if in_func and isinstance(node, (ast.Import, ast.ImportFrom)):
        me = module_of(rel)
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        else:
            base = resolve(me, rel.endswith("__init__.py"), node.level, node.module or "")
            targets = [base] + [base + "." + a.name for a in node.names]
            for a in node.names:  # lazily loaded names: the module they come from
                try:
                    obj = getattr(importlib.import_module(base), a.name)
                    src_mod = obj.__name__ if isinstance(obj, type(sys)) else getattr(obj, "__module__", None)
                    if isinstance(src_mod, str):
                        targets.append(src_mod)
                except Exception:
                    pass
        for t in targets:
            parts = t.split(".")
            for i in range(1, len(parts) + 1):
                func_imports.setdefault(".".join(parts[:i]), set()).add((rel, node.lineno))
    for ch in ast.iter_child_nodes(node):
        _fi_visit(ch, rel, in_func)


for rel, tree in py_sources.items():
    _fi_visit(tree, rel, False)

# Upper-cased string constants inside sqlglot functions: code that looks a
# table up by a fixed key.
str_sites = {}


def _str_visit(node, rel, in_func):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        for ch in ast.iter_child_nodes(node):
            _str_visit(ch, rel, True)
        return
    if in_func and isinstance(node, ast.Constant) and isinstance(node.value, str) and 0 < len(node.value) <= 60:
        str_sites.setdefault(node.value.upper(), set()).add((rel, node.lineno))
    for ch in ast.iter_child_nodes(node):
        _str_visit(ch, rel, in_func)


for rel, tree in py_sources.items():
    if rel.startswith("sqlglot"):
        _str_visit(tree, rel, False)

sets, index = [], {}


def sid(s):
    key = tuple(sorted(s))
    if key not in index:
        index[key] = len(sets)
        sets.append(list(key))
    return index[key]


# Once-per-process lines: ran in the first warmup but not in the second.
for per in lines.values():
    for e in per.values():
        if e[1] & 2 and not e[1] & 32:
            e[1] |= 16

result = {
    "sets": sets,
    "lines": {f: {str(ln): [sid(e[0]), e[1]] for ln, e in per.items()} for f, per in lines.items()},
    "opens": {f: sid(s) for f, s in opens.items()},
    "textreads": {f: sid(s) for f, s in textreads.items()},
    "classes": {c: sid(s) for c, s in classes.items()},
    "mro": mro,
    "file_tests": {f: sid(s) for f, s in file_tests.items()},
    "class_info": class_info,
    "test_ids": [t["id"] for t in tests],
    "core": sorted(core),
    "importers": {k: sorted(v) for k, v in importers.items()},
    "lazy_importers": {k: sorted(v) for k, v in lazy_importers.items()},
    "sites": {k: sorted(v) for k, v in sites.items()},
    "import_mods": {f"{f}:{ln}": sorted(v) for (f, ln), v in import_mods.items()},
    "import_classes": sorted(import_classes),
    "vocab": {w: sid(s) for w, s in vocab.items()},
    "func_imports": {k: sorted(v) for k, v in func_imports.items() if k.startswith("sqlglot")},
    "dyn_import_sites": sorted(dyn_import_sites),
    "guards": guards,
    "str_sites": {k: sorted(v) for k, v in str_sites.items()},
}
with gzip.open(out, "wt", encoding="utf-8") as f:
    json.dump(result, f, separators=(",", ":"))
print("files", len(lines), "sets", len(sets), "classes", len(classes), file=sys.stderr)
