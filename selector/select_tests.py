"""Blast radius selector.

For every patch in /data/commits it prints one JSON line naming the tests to run
and how to split them over workers.

How a test set is chosen, per changed file:

* Python file in sqlglot/ or tests/: compare the old and new syntax trees.
  - A changed statement inside a function: the tests that ran that statement
    (from the coverage map).
  - A changed statement at module or class level (constants, dict tables,
    class attributes, imports): the tests that use that class or module
    (they created an instance of it, ran code in it, or use a module that
    imports it). Dict tables keyed by expression classes are narrowed to the
    tests that created those expressions.
  - Only comments or formatting changed: nothing.
  - Docstring changed: the doc tests.
  - The new file does not parse: every test that could import it.
* Fixture or data file: the tests that opened it.
* Anything we do not understand: every test.
"""

import ast
import difflib
import gzip
import json
import os
import sys
import time

DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.json.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


# ----------------------------------------------------------------------------
# Patch parsing and applying


class PatchError(Exception):
    pass


def parse_patch(text):
    """Return a list of dicts: old, new (paths or None), hunks, binary."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False, "header": line}
            parts = line[len("diff --git ") :].split(" b/")
            if len(parts) == 2 and parts[0].startswith("a/"):
                cur["old"] = parts[0][2:]
                cur["new"] = parts[1]
            files.append(cur)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "binary": False, "header": ""}
                files.append(cur)
            o = line[4:].split("\t")[0].strip()
            n = lines[i + 1][4:].split("\t")[0].strip()
            cur["old"] = None if o == "/dev/null" else (o[2:] if o.startswith("a/") else o)
            cur["new"] = None if n == "/dev/null" else (n[2:] if n.startswith("b/") else n)
            i += 2
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur["old_missing"] = True
            elif line.startswith("deleted file mode"):
                cur["new_missing"] = True
            elif line.startswith("rename from "):
                cur["old"] = line[len("rename from ") :]
            elif line.startswith("rename to "):
                cur["new"] = line[len("rename to ") :]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur["binary"] = True
            elif line.startswith("@@"):
                try:
                    head = line.split("@@")[1].strip()
                    a, b = head.split(" ")[:2]
                    a = a[1:].split(",")
                    b = b[1:].split(",")
                    hunk = {
                        "os": int(a[0]),
                        "ol": int(a[1]) if len(a) > 1 else 1,
                        "ns": int(b[0]),
                        "nl": int(b[1]) if len(b) > 1 else 1,
                        "lines": [],
                    }
                except Exception as e:
                    raise PatchError(f"bad hunk header {line!r}") from e
                cur["hunks"].append(hunk)
                i += 1
                ol, nl = hunk["ol"], hunk["nl"]
                while i < len(lines) and (ol > 0 or nl > 0):
                    hl = lines[i]
                    if hl.startswith("\\"):
                        i += 1
                        continue
                    tag = hl[:1] if hl else " "
                    if tag not in " +-":
                        break
                    hunk["lines"].append((tag, hl[1:]))
                    if tag in " -":
                        ol -= 1
                    if tag in " +":
                        nl -= 1
                    i += 1
                while i < len(lines) and lines[i].startswith("\\"):
                    hunk.setdefault("no_newline", True)
                    i += 1
                continue
        i += 1
    for f in files:
        if f.get("old_missing"):
            f["old"] = None
        if f.get("new_missing"):
            f["new"] = None
    return files


def apply_hunks(old_text, hunks):
    """Apply hunks to old_text. Returns new text and list of old line numbers
    touched (1-based, removed lines) plus insertion points."""
    old_lines = old_text.split("\n")
    out = []
    pos = 0  # index into old_lines
    offset = 0
    for h in hunks:
        old_chunk = [t for tag, t in h["lines"] if tag in " -"]
        start = h["os"] - 1 if h["ol"] > 0 else h["os"]
        start += offset
        if old_lines[start : start + len(old_chunk)] != old_chunk:
            found = None
            for delta in range(1, 200):
                for s in (start - delta, start + delta):
                    if 0 <= s and old_lines[s : s + len(old_chunk)] == old_chunk:
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                raise PatchError("hunk does not apply")
            offset += found - start
            start = found
        if start < pos:
            raise PatchError("overlapping hunks")
        out.extend(old_lines[pos:start])
        for tag, t in h["lines"]:
            if tag in " +":
                out.append(t)
        pos = start + len(old_chunk)
    out.extend(old_lines[pos:])
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Map


PICKLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.pkl")


def load_raw_map(path):
    if os.path.exists(PICKLE):
        import pickle

        with open(PICKLE, "rb") as f:
            return pickle.load(f)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def prepare():
    """Build step: store the map as a pickle, which loads faster than JSON."""
    import pickle

    with gzip.open(MAP, "rt", encoding="utf-8") as f:
        m = json.load(f)
    with open(PICKLE, "wb") as f:
        pickle.dump(m, f, protocol=pickle.HIGHEST_PROTOCOL)


class Map:
    def __init__(self, path):
        m = load_raw_map(path)
        self.sets = [frozenset(s) for s in m["sets"]]
        self.lines = m["lines"]
        self.opens = m["opens"]
        self.textreads = m["textreads"]
        self.classes = m["classes"]
        self.mro = m["mro"]
        self.file_tests = m["file_tests"]
        self.class_info = m["class_info"]
        self.core = set(m["core"])
        self.importers = m["importers"]
        self.sites = m.get("sites", {})
        self.test_ids = m.get("test_ids")
        # Instantiated classes per ancestor: ancestor -> tests
        self._inst_cache = {}
        self.desc = {}
        for name, sidx in self.classes.items():
            for anc in self.mro.get(name, [name]):
                self.desc.setdefault(anc, []).append(sidx)
        self._users_cache = {}

    def s(self, idx):
        return self.sets[idx]

    def line_info(self, path, ln):
        e = self.lines.get(path, {}).get(str(ln))
        if e is None:
            return frozenset(), 0
        return self.sets[e[0]], e[1]

    def inst(self, cls_key):
        """Tests that created an instance of cls_key or a subclass."""
        r = self._inst_cache.get(cls_key)
        if r is None:
            r = set()
            for sidx in self.desc.get(cls_key, []):
                r |= self.sets[sidx]
            r = frozenset(r)
            self._inst_cache[cls_key] = r
        return r

    def file_all(self, path):
        """Tests that ran any line of the file at test time."""
        r = set()
        for e in self.lines.get(path, {}).values():
            r |= self.sets[e[0]]
        return r


def module_of(path):
    rel = path[:-3].replace("/", ".")
    return rel[: -len(".__init__")] if rel.endswith(".__init__") else rel


def path_of_module(mod):
    p = mod.replace(".", "/")
    if os.path.isdir(os.path.join(SRC, p)):
        return p + "/__init__.py"
    return p + ".py"


# ----------------------------------------------------------------------------
# Syntax tree comparison


def dump(node):
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def is_docstring(stmt):
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def stmt_key(s):
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("def", s.name)
    if isinstance(s, ast.ClassDef):
        return ("class", s.name)
    return ("stmt", dump(s))


def target_names(stmt):
    names = set()
    if stmt is None:
        return names
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
        return names
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
        return names
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
    return names


class Changes:
    def __init__(self):
        self.func_lines = set()  # old line numbers of changed statements inside functions
        self.struct = []  # (scope qualname, old stmt or None, new stmt or None)
        self.doc = False
        self.whole = False  # treat file as fully changed


def exp_key_diff(o, n):
    """If o and n are the same dict assignment that differs only in entries
    keyed by exp.<Name>, return the set of those names, else None."""
    if not (isinstance(o, (ast.Assign, ast.AnnAssign)) and type(o) is type(n)):
        return None
    ot = o.targets if isinstance(o, ast.Assign) else [o.target]
    nt = n.targets if isinstance(n, ast.Assign) else [n.target]
    if [dump(x) for x in ot] != [dump(x) for x in nt]:
        return None
    ov, nv = o.value, n.value
    if not (isinstance(ov, ast.Dict) and isinstance(nv, ast.Dict)):
        return None

    def entries(d):
        res = {}
        for k, v in zip(d.keys, d.values):
            kk = "**" + dump(v) if k is None else dump(k)
            res[kk] = (k, dump(v))
        return res

    oe, ne = entries(ov), entries(nv)
    changed = []
    for kk in set(oe) | set(ne):
        if oe.get(kk, (None, None))[1] != ne.get(kk, (None, None))[1]:
            changed.append((oe.get(kk) or ne.get(kk))[0])
    names = set()
    for k in changed:
        if (
            isinstance(k, ast.Attribute)
            and isinstance(k.value, ast.Name)
            and k.value.id == "exp"
        ):
            names.add(k.attr)
        else:
            return None
    return names


def stmt_lines(s):
    return range(s.lineno, (getattr(s, "end_lineno", None) or s.lineno) + 1)


def header_lines(s):
    """Lines of a compound statement's header (before its body)."""
    body = getattr(s, "body", None)
    if body:
        end = body[0].lineno - 1
        return range(s.lineno, max(s.lineno, end) + 1)
    return stmt_lines(s)


def compare_body(ob, nb, ch, in_func, scope, parent):
    """Compare two statement lists. ``parent`` is the old enclosing statement."""
    ok = [stmt_key(s) for s in ob]
    nk = [stmt_key(s) for s in nb]
    sm = difflib.SequenceMatcher(a=ok, b=nk, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for idx, (a, b) in enumerate(zip(ob[i1:i2], nb[j1:j2])):
                if ok[i1 + idx][0] != "stmt":
                    compare_stmt(a, b, ch, in_func, scope)
            continue
        olds, news = ob[i1:i2], nb[j1:j2]
        # Pair statements of the same type position by position.
        if tag == "replace" and len(olds) == len(news):
            pairs_ok = all(
                type(a) is type(b) and (stmt_key(a)[0] == "stmt" or stmt_key(a) == stmt_key(b))
                for a, b in zip(olds, news)
            )
            if pairs_ok:
                for a, b in zip(olds, news):
                    compare_stmt(a, b, ch, in_func, scope)
                continue
        for a in olds:
            changed_stmt(a, None, ch, in_func, scope)
        for b in news:
            if in_func:
                # An inserted statement runs when its neighbours run.
                near = []
                if i1 > 0:
                    near.append(ob[i1 - 1])
                if i1 < len(ob):
                    near.append(ob[i1])
                if near:
                    for s in near:
                        ch.func_lines.update(header_lines(s))
                elif parent is not None:
                    ch.func_lines.update(header_lines(parent))
            else:
                changed_stmt(None, b, ch, in_func, scope)


def compare_stmt(o, n, ch, in_func, scope):
    if dump(o) == dump(n):
        return
    if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
        ohead = [dump(x) for x in o.decorator_list] + [dump(o.args), dump(o.returns) if o.returns else ""]
        nhead = [dump(x) for x in n.decorator_list] + [dump(n.args), dump(n.returns) if n.returns else ""]
        if ohead != nhead or type(o) is not type(n):
            if in_func:
                ch.func_lines.update(stmt_lines(o))
            else:
                # Callers are the tests that ran its body (in the old code).
                # The def line itself runs at import, so it says nothing.
                ch.func_lines.update(range(o.body[0].lineno, o.end_lineno + 1))
                ch.struct.append((scope, o, n, "header"))
            return
        ob, nb = o.body, n.body
        if ob and nb and is_docstring(ob[0]) and is_docstring(nb[0]):
            if dump(ob[0]) != dump(nb[0]):
                ch.doc = True
            ob, nb = ob[1:], nb[1:]
        elif (ob and is_docstring(ob[0])) != (nb and is_docstring(nb[0])):
            ch.doc = True
            if ob and is_docstring(ob[0]):
                ob = ob[1:]
            if nb and is_docstring(nb[0]):
                nb = nb[1:]
        compare_body(ob, nb, ch, True, scope + [o.name], o)
        return
    if isinstance(o, ast.ClassDef):
        ohead = [dump(x) for x in o.decorator_list + o.bases + o.keywords]
        nhead = [dump(x) for x in n.decorator_list + n.bases + n.keywords]
        if ohead != nhead:
            if in_func:
                ch.func_lines.update(stmt_lines(o))
            else:
                ch.struct.append((scope + [o.name], o, n, "classhead"))
            return
        ob, nb = o.body, n.body
        if ob and nb and is_docstring(ob[0]) and is_docstring(nb[0]):
            if dump(ob[0]) != dump(nb[0]):
                ch.doc = True
            ob, nb = ob[1:], nb[1:]
        compare_body(ob, nb, ch, in_func, scope + [o.name], o)
        return
    if not in_func:
        changed_stmt(o, n, ch, in_func, scope)
        return
    # Inside a function: descend into compound statements.
    if isinstance(o, (ast.If, ast.While)):
        if dump(o.test) != dump(n.test):
            ch.func_lines.update(range(o.lineno, o.test.end_lineno + 1))
        compare_body(o.body, n.body, ch, True, scope, o)
        compare_body(o.orelse, n.orelse, ch, True, scope, o)
        return
    if isinstance(o, (ast.For, ast.AsyncFor)):
        if dump(o.target) != dump(n.target) or dump(o.iter) != dump(n.iter):
            ch.func_lines.update(range(o.lineno, o.iter.end_lineno + 1))
        compare_body(o.body, n.body, ch, True, scope, o)
        compare_body(o.orelse, n.orelse, ch, True, scope, o)
        return
    if isinstance(o, (ast.With, ast.AsyncWith)):
        if [dump(x) for x in o.items] != [dump(x) for x in n.items]:
            ch.func_lines.update(header_lines(o))
        compare_body(o.body, n.body, ch, True, scope, o)
        return
    if isinstance(o, ast.Try) and len(o.handlers) == len(n.handlers):
        compare_body(o.body, n.body, ch, True, scope, o)
        for ho, hn in zip(o.handlers, n.handlers):
            if (dump(ho.type) if ho.type else "") != (dump(hn.type) if hn.type else "") or ho.name != hn.name:
                ch.func_lines.update(header_lines(ho))
            compare_body(ho.body, hn.body, ch, True, scope, ho)
        compare_body(o.orelse, n.orelse, ch, True, scope, o)
        compare_body(o.finalbody, n.finalbody, ch, True, scope, o)
        return
    changed_stmt(o, n, ch, in_func, scope)


def changed_stmt(o, n, ch, in_func, scope):
    if in_func:
        if o is not None:
            ch.func_lines.update(stmt_lines(o))
        return
    if (o is not None and is_docstring(o)) or (n is not None and is_docstring(n)):
        if (o is None or is_docstring(o)) and (n is None or is_docstring(n)):
            ch.doc = True
            return
    # A function removed at module or class level: its callers ran its body.
    # (If something at import time still names it, the import check fails.)
    if o is not None and n is None and isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
        ch.func_lines.update(range(o.body[0].lineno, o.end_lineno + 1))
        ch.struct.append((scope, o, n, "deleted def"))
        return
    if o is not None and isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
        ch.func_lines.update(range(o.body[0].lineno, o.end_lineno + 1))
    ch.struct.append((scope, o, n, "stmt"))


def py_changes(old_src, new_src):
    ch = Changes()
    try:
        ot = ast.parse(old_src)
    except SyntaxError:
        ch.whole = True
        return ch
    nt = ast.parse(new_src)  # SyntaxError handled by caller
    ob, nb = ot.body, nt.body
    if ob and nb and is_docstring(ob[0]) and is_docstring(nb[0]):
        if dump(ob[0]) != dump(nb[0]):
            ch.doc = True
        ob, nb = ob[1:], nb[1:]
    compare_body(ob, nb, ch, False, [], None)
    return ch


# ----------------------------------------------------------------------------
# Running a changed module's top-level code


_sqlglot_loaded = {"ok": None}


def _load_sqlglot():
    if _sqlglot_loaded["ok"] is None:
        try:
            if SRC not in sys.path:
                sys.path.insert(0, SRC)
            import logging

            logging.disable(logging.CRITICAL)
            import sqlglot  # noqa: F401

            _sqlglot_loaded["ok"] = True
        except BaseException as e:  # noqa
            log("cannot import sqlglot:", e)
            _sqlglot_loaded["ok"] = False
    return _sqlglot_loaded["ok"]


def import_ok(modname, path, code, timeout=20.0):
    """Run the patched module's top-level code in a forked child, against the
    original versions of every other module. False when it raises or hangs."""
    if not hasattr(os, "fork") or not _load_sqlglot():
        return True
    try:
        pid = os.fork()
    except OSError:
        return True
    if pid == 0:  # child
        status = 1
        try:
            fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            import importlib
            import types

            pkg = modname.rpartition(".")[0]
            if path.endswith("__init__.py"):
                pkg = modname
            if pkg:
                importlib.import_module(pkg)
            mod = types.ModuleType(modname)
            mod.__file__ = os.path.join(SRC, path)
            mod.__package__ = pkg
            if path.endswith("__init__.py"):
                mod.__path__ = [os.path.dirname(mod.__file__)]
            sys.modules[modname] = mod
            exec(code, mod.__dict__)
            status = 0
        except BaseException:
            status = 1
        finally:
            os._exit(status)
    deadline = time.monotonic() + timeout
    while True:
        done, st = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(st) == 0
        if time.monotonic() > deadline:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass
            return False
        time.sleep(0.005)


# ----------------------------------------------------------------------------
# Test selection


class Selector:
    def __init__(self, m, tests):
        self.m = m
        self.tests = tests
        self.all = frozenset(t["number"] for t in tests)
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self.doc_tests = frozenset(
            t["number"] for t in tests if t["file"] in ("tests/test_docs.py",)
        )
        self._mod_users = {}

    # -- users of modules and classes ----------------------------------------

    def module_users(self, mod, depth=0):
        """Tests that use module ``mod`` in any way we can see."""
        if mod in self._mod_users:
            return self._mod_users[mod]
        self._mod_users[mod] = frozenset()  # cycle guard
        m = self.m
        if mod in m.core or mod == "sqlglot":
            r = self.all
        else:
            path = path_of_module(mod)
            r = set(m.file_all(path))
            for key in m.class_info:
                if key.split(":", 1)[0] == mod:
                    r |= m.inst(key)
            r |= m.s(m.textreads[path]) if path in m.textreads else set()
            if path.startswith("tests/"):
                r |= self.by_file.get(path, set())
            for imp in m.importers.get(mod, []):
                if imp != mod:
                    r |= self.module_users(imp, depth + 1)
            r = frozenset(r)
        self._mod_users[mod] = r
        return r

    def class_users(self, mod, qual):
        m = self.m
        key = f"{mod}:{qual}"
        r = set(m.inst(key))
        # Tests that ran code inside the class (methods, classmethods).
        return r

    def is_expression_class(self, mod, qual):
        mro = self.m.class_info.get(f"{mod}:{qual}", [])
        return "sqlglot.expressions.core:Expr" in mro

    def expression_users(self, names):
        """Tests that created an instance of exp.<name> (any module) or a subclass."""
        r = set()
        for key in self.m.class_info:
            mod, qual = key.split(":", 1)
            if qual in names and mod.startswith("sqlglot.expressions"):
                r |= self.m.inst(key)
        return r

    # -- per file -------------------------------------------------------------

    def select_file(self, f):
        m = self.m
        old, new = f["old"], f["new"]
        path = old or new
        reasons = []
        if f["binary"]:
            return self.data_file(path, reasons) | (self.data_file(new, reasons) if new else set()), reasons
        if old and new and old != new:
            # Rename: both paths matter.
            r = set()
            for p in (old, new):
                r |= self.data_file(p, reasons) if not p.endswith(".py") else self.all
            return r, reasons + ["rename"]
        if not path.endswith(".py"):
            return self.data_file(path, reasons), reasons
        top = path.split("/")[0]
        if top not in ("sqlglot", "tests"):
            if path in m.opens:
                reasons.append("opened")
                return set(m.s(m.opens[path])), reasons
            reasons.append("unknown py")
            return set(self.all), reasons
        if old is None:
            # New python file: only matters if something imports it.
            if path.startswith("tests/") and os.path.basename(path).startswith("test_"):
                reasons.append("new test file")
                return set(), reasons
            if os.path.basename(path) in ("conftest.py",):
                return set(self.all), reasons + ["conftest"]
            reasons.append("new module")
            return set(), reasons
        try:
            old_src = open(os.path.join(SRC, old), encoding="utf-8").read()
        except OSError:
            return set(self.all), reasons + ["old missing"]
        if new is None:
            reasons.append("deleted module")
            return set(self.import_failure(path)), reasons
        new_src = apply_hunks(old_src, f["hunks"])
        if os.path.basename(path) == "conftest.py":
            return set(self.all), reasons + ["conftest"]
        try:
            code = compile(new_src, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            reasons.append("syntax error")
            return set(self.import_failure(path)), reasons
        sel = set()
        # Tests that read this file as text see any change, comments included.
        if old_src != new_src and path in m.textreads:
            sel |= m.s(m.textreads[path])
        ch = py_changes(old_src, new_src)
        if ch.whole:
            return set(self.module_users(module_of(path))), reasons + ["whole"]
        if ch.doc:
            reasons.append("doc")
            sel |= self.doc_tests
        mod = module_of(path)
        if ch.struct and path.startswith("sqlglot/") and not import_ok(mod, path, code):
            reasons.append("import fails")
            return set(self.import_failure(path)), reasons
        for ln in ch.func_lines:
            tests, flags = m.line_info(path, ln)
            sel |= tests
            if flags & 17 or (flags & 10 and not tests):
                # Ran only at import or warmup (caches, factories): every user.
                reasons.append(f"importline {ln}")
                sel |= self.module_users(mod)
        for scope, o, n, kind in ch.struct:
            sel |= self.struct_users(path, mod, scope, o, n, kind, reasons)
        return sel, reasons

    def struct_users(self, path, mod, scope, o, n, kind, reasons):
        m = self.m
        if path.startswith("tests/"):
            r = set(self.by_file.get(path, set()))
            for imp in m.importers.get(mod, []):
                r |= self.by_file.get(path_of_module(imp), set()) if imp.startswith("tests") else set()
                r |= self.module_users(imp) if imp.startswith("tests") else set()
            reasons.append("test struct")
            return r
        if kind == "deleted def":
            return set()
        if kind == "header" and o is not None and isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Decorators and defaults run at definition; callers ran the body.
            # Body coverage is already added. Decorator changes may change what
            # the name means at import time: treat as users of the scope.
            if [dump(x) for x in o.decorator_list] == [dump(x) for x in n.decorator_list]:
                return set()
        qual = ".".join(scope)
        keys = exp_key_diff(o, n) if (o is not None and n is not None) else None
        if scope:
            # Find the innermost class we know about.
            parts = list(scope)
            while parts and f"{mod}:{'.'.join(parts)}" not in m.class_info:
                parts.pop()
            qual = ".".join(parts)
        if qual and self.is_expression_class(mod, qual):
            reasons.append(f"expr class {qual}")
            r = set(m.inst(f"{mod}:{qual}"))
            # Methods of the class ran in these tests too.
            return r
        if keys:
            reasons.append(f"exp keys {sorted(keys)} in {mod}:{qual}")
            base = self.module_users(mod) if not qual else (set(m.inst(f"{mod}:{qual}")) | self.module_users(mod))
            return set(base) & self.expression_users(keys)
        readers = self.name_readers(o, n)
        if qual:
            r = set(m.inst(f"{mod}:{qual}"))
            r |= self.module_users(mod)
        else:
            r = set(self.module_users(mod))
        if readers is not None:
            reasons.append(f"readers of {sorted(target_names(o) | target_names(n))} in {mod}:{qual}")
            return r & readers
        reasons.append(f"class {qual}" if qual else f"module {mod}")
        return r

    def name_readers(self, o, n):
        """Tests that read a changed module/class-level name, when every read
        happens inside a function that tests run. None when we cannot tell."""
        ok_types = (ast.Assign, ast.AnnAssign, ast.AugAssign)
        for st in (o, n):
            if st is not None and not isinstance(st, ok_types):
                return None
        names = target_names(o) | target_names(n)
        if not names:
            return None
        m = self.m
        r = set()
        for name in names:
            ss = m.sites.get(name)
            if not ss:
                return None
            for f, line, kind in ss:
                if kind:
                    return None
                tests, flags = m.line_info(f, line)
                if flags & 17 or (not tests and flags & 10):
                    return None
                r |= tests
        return r

    def import_failure(self, path):
        mod = module_of(path)
        if not path.startswith("tests/"):
            # Any sqlglot module may be imported by tests that never use it
            # (doc tests import every module), so a broken one breaks a lot.
            return set(self.all)
        if path.startswith("tests/"):
            r = set(self.by_file.get(path, set()))
            for imp in self.m.importers.get(mod, []):
                if imp.startswith("tests"):
                    r |= self.by_file.get(path_of_module(imp), set())
            return r
        return self.module_users(mod)

    def data_file(self, path, reasons):
        m = self.m
        if path is None:
            return set()
        name = os.path.basename(path)
        if name in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "setup.py", "conftest.py"):
            if path.count("/") == 0:
                reasons.append("project config")
                return set(self.all)
        if path in m.opens:
            reasons.append("opened")
            return set(m.s(m.opens[path]))
        # Never opened: maybe found by listing its folder. Tests that opened
        # a sibling file.
        folder = os.path.dirname(path)
        r = set()
        if folder.startswith("tests"):
            for p, sidx in m.opens.items():
                if os.path.dirname(p) == folder:
                    r |= m.s(sidx)
        reasons.append("unopened data")
        return r

    def select(self, patch_text):
        files = parse_patch(patch_text)
        sel = set()
        reasons = []
        if not files and patch_text.strip():
            return set(self.all), ["unparsed"]
        for f in files:
            try:
                s, r = self.select_file(f)
            except Exception as e:  # anything unexpected: be safe
                s, r = set(self.all), [f"error {type(e).__name__}: {e}"]
            sel |= s
            reasons.append(((f["old"] or f["new"]), r[:6]))
        return sel, reasons


# ----------------------------------------------------------------------------
# Splitting over workers


def split(tests_ms):
    """tests_ms: list of (number, ms). Returns list of workers minimizing
    max load + 2 s per worker."""
    if not tests_ms:
        return []
    order = sorted(tests_ms, key=lambda x: (-x[1], x[0]))
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        workers = [[] for _ in range(k)]
        loads = [0] * k
        for num, ms in order:
            i = loads.index(min(loads))
            workers[i].append(num)
            loads[i] += ms
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


def main():
    t0 = time.process_time()
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    to_data = None  # map test number -> /data test number
    unknown = set()  # /data tests the map has never seen: always run them
    try:
        m = Map(MAP)
        data_ids = [t["id"] for t in tests]
        if m.test_ids is not None and m.test_ids != data_ids:
            pos = {tid: t["number"] for tid, t in zip(data_ids, tests)}
            to_data = {i: pos[tid] for i, tid in enumerate(m.test_ids) if tid in pos}
            unknown = set(pos.values()) - set(to_data.values())
            map_tests = [
                {"number": i, "id": tid, "file": tid.split("::")[0], "seconds": 0.0}
                for i, tid in enumerate(m.test_ids)
            ]
            log("test list differs from the map:", len(unknown), "unknown tests")
        else:
            map_tests = tests
        sel = Selector(m, map_tests)
    except Exception as e:
        log("map failed, running everything:", e)
        sel = None
    commits = sorted(os.listdir(os.path.join(DATA, "commits")))
    out = []
    for fn in commits:
        if not fn.endswith(".patch"):
            continue
        cid = fn[: -len(".patch")]
        in_map_numbers = True
        try:
            text = open(os.path.join(DATA, "commits", fn), encoding="utf-8", errors="replace").read()
            if sel is None:
                chosen, why, in_map_numbers = set(ms), ["no map"], False
            else:
                chosen, why = sel.select(text)
        except Exception as e:
            chosen, why, in_map_numbers = set(ms), [f"error {e}"], False
        if to_data is not None and in_map_numbers:
            chosen = {to_data[n] for n in chosen if n in to_data} | (unknown if chosen else set())
        chosen = sorted(n for n in chosen if n in ms)
        workers = split([(n, ms[n]) for n in chosen])
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        log(cid, len(chosen), "tests", why)
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    try:
        import resource  # not on Windows

        ch = resource.getrusage(resource.RUSAGE_CHILDREN)
        log(f"cpu {time.process_time() - t0:.2f}s, children {ch.ru_utime + ch.ru_stime:.2f}s")
    except Exception:
        log(f"cpu {time.process_time() - t0:.2f}s")


if __name__ == "__main__":
    if "--prepare" in sys.argv:
        prepare()
    else:
        main()
