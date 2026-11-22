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


def apply_hunks(old_text, hunks, want_touched=False):
    """Apply hunks to old_text. Returns new text and list of old line numbers
    touched (1-based, removed lines) plus insertion points."""
    old_lines = old_text.split("\n")
    old_t, new_t = set(), set()
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
        o = start  # 0-based index of the next old line
        for tag, t in h["lines"]:
            if tag == " ":
                out.append(t)
                o += 1
            elif tag == "-":
                old_t.add(o + 1)
                new_t.update((len(out), len(out) + 1))
                o += 1
            else:
                out.append(t)
                new_t.add(len(out))
                old_t.update((o, o + 1))
        pos = start + len(old_chunk)
    out.extend(old_lines[pos:])
    if want_touched:
        return "\n".join(out), old_t, new_t
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
        self.import_mods = m.get("import_mods")  # None for old maps
        self.import_classes = m.get("import_classes", [])
        self.vocab = m.get("vocab", {})
        self.str_sites = m.get("str_sites", {})
        self.func_imports = m.get("func_imports", {})
        self.lazy_importers = m.get("lazy_importers", {})
        self.dyn_import_sites = m.get("dyn_import_sites", [])
        self.guards = m.get("guards", {})
        # Importing a.b.c first runs package a.b: whoever imports a submodule
        # also imports its parent packages.
        self.parent_importers = {}
        for name, imps in self.importers.items():
            parts = name.split(".")
            for i in range(1, len(parts)):
                self.parent_importers.setdefault(".".join(parts[:i]), set()).update(imps)
        self._import_anc = None
        # Instantiated classes per ancestor: ancestor -> tests
        self._inst_cache = {}
        self.desc = {}
        for name, sidx in self.classes.items():
            for anc in self.mro.get(name, [name]):
                self.desc.setdefault(anc, []).append(sidx)
        self._users_cache = {}

    def created_at_import(self, cls_key):
        """Was an instance of cls_key (or a subclass) created while modules
        were importing?"""
        if self._import_anc is None:
            anc = set()
            for c in self.import_classes:
                anc.update(self.mro.get(c, [c]))
            self._import_anc = anc
        return cls_key in self._import_anc

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


_FP = {}


def dump(node):
    """Structural fingerprint of a syntax tree (positions ignored). Memoized
    per node object, so comparing nested statements stays cheap."""
    key = id(node)
    r = _FP.get(key)
    if r is not None and r[0] is node:
        return r[1]
    parts = [type(node).__name__]
    for name, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            parts.append(dump(value))
        elif isinstance(value, list):
            parts.append(tuple(dump(v) if isinstance(v, ast.AST) else repr(v) for v in value))
        else:
            parts.append(repr(value))
    h = hash(tuple(parts))
    _FP[key] = (node, h)
    return h


def is_docstring(stmt):
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


class _Ctx:
    """Per-file state: source lines and which lines the patch touched, on
    each side. A statement whose lines were not touched has the same text on
    both sides, so it can be matched by text without walking its tree."""

    old_lines = new_lines = ()
    old_touched = new_touched = frozenset()
    norm = None
    sides = {}  # id(node) -> "old" / "new"


CTX = _Ctx()


def _span(s):
    start = s.lineno
    for d in getattr(s, "decorator_list", ()) or ():
        start = min(start, d.lineno)
    return start, getattr(s, "end_lineno", None) or s.lineno


def untouched_text(s, side):
    """The statement's source text when the patch did not touch it, else None."""
    touched = CTX.old_touched if side == "old" else CTX.new_touched
    lines = CTX.old_lines if side == "old" else CTX.new_lines
    a, b = _span(s)
    for ln in range(a, b + 1):
        if ln in touched:
            return None
    return "\n".join(lines[a - 1 : b])


def same_text(a, b):
    """a is an old statement, b a new one."""
    ta = untouched_text(a, "old")
    return ta is not None and ta == untouched_text(b, "new")


def norm_equal(a, b):
    """True when a and b only differ by is/== swaps that cannot matter."""
    if CTX.norm is None or a is None or b is None:
        return False
    import copy

    ca = CTX.norm.visit(copy.deepcopy(a))
    cb = CTX.norm.visit(copy.deepcopy(b))
    return dump(ca) == dump(cb)


def stmt_key(s, side="old"):
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("def", s.name)
    if isinstance(s, ast.ClassDef):
        return ("class", s.name)
    t = untouched_text(s, side)
    if t is not None:
        return ("stmt", "text", hash(t))
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
            kk = ("**", dump(v)) if k is None else dump(k)
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


def compare_body(ob, nb, ch, in_func, scope, parent, use_text=True, outer_near=()):
    """Compare two statement lists. ``parent`` is the old enclosing statement.

    First pass matches untouched statements by their text (cheap). Blocks
    that differ are compared again by syntax tree, so a statement next to a
    deleted line is not mistaken for a changed one."""
    if use_text:
        ok = [stmt_key(s) for s in ob]
        nk = [stmt_key(s, "new") for s in nb]
    else:
        ok = [stmt_key_tree(s) for s in ob]
        nk = [stmt_key_tree(s) for s in nb]
    sm = difflib.SequenceMatcher(a=ok, b=nk, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for idx, (a, b) in enumerate(zip(ob[i1:i2], nb[j1:j2])):
                if ok[i1 + idx][0] != "stmt" and not same_text(a, b):
                    compare_stmt(a, b, ch, in_func, scope)
            continue
        olds, news = ob[i1:i2], nb[j1:j2]
        near = []
        if i1 > 0:
            near.append(ob[i1 - 1])
        if i2 < len(ob):
            near.append(ob[i2])
        if not near:
            near = list(outer_near)
        if use_text and olds and news:
            compare_body(olds, news, ch, in_func, scope, parent, use_text=False, outer_near=near)
            continue
        # Pair statements of the same type position by position.
        if tag == "replace" and len(olds) == len(news):
            pairs_ok = all(
                type(a) is type(b) and (stmt_key_tree(a)[0] == "stmt" or stmt_key_tree(a) == stmt_key_tree(b))
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
                if olds:
                    for s_ in olds:
                        ch.func_lines.update(header_lines(s_))
                elif near:
                    for s_ in near:
                        ch.func_lines.update(header_lines(s_))
                elif parent is not None:
                    ch.func_lines.update(header_lines(parent))
            else:
                changed_stmt(None, b, ch, in_func, scope)


def stmt_key_tree(s):
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("def", s.name)
    if isinstance(s, ast.ClassDef):
        return ("class", s.name)
    return ("stmt", dump(s))


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPOUND = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.While, ast.For,
    ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try,
)


def compare_stmt(o, n, ch, in_func, scope):
    if same_text(o, n):
        return
    if (not isinstance(o, _COMPOUND) or not (in_func or isinstance(o, _DEFS))) and dump(o) == dump(n):
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
        if dump(o.test) != dump(n.test) and not norm_equal(o.test, n.test):
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
    if o is not None and n is not None and norm_equal(o, n):
        return
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


class _IdentityCompare(ast.NodeTransformer):
    """Rewrite ``a is X`` as ``a == X`` (and ``is not`` as ``!=``) when X is
    None or an enum member. Both spellings give the same answer for these
    values, so a patch that only swaps one for the other changes nothing."""

    def __init__(self, modname):
        self.modname = modname
        self._mod = None
        self._tried = False

    def _module(self):
        if not self._tried:
            self._tried = True
            if self.modname.startswith("sqlglot") and _load_sqlglot():
                try:
                    import importlib

                    self._mod = importlib.import_module(self.modname)
                except BaseException:
                    self._mod = None
        return self._mod

    def _safe(self, node):
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if not isinstance(node, ast.Attribute):
            return False
        chain = []
        cur = node
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return False
        mod = self._module()
        if mod is None:
            return False
        import enum

        obj = getattr(mod, cur.id, None)
        if obj is None:
            return False
        try:
            for a in reversed(chain):
                obj = getattr(obj, a)
        except Exception:
            return False
        # Plain enums and str-valued AutoName enums; never int-like enums.
        return isinstance(obj, enum.Enum) and not isinstance(obj, (int, float))

    def visit_Compare(self, node):
        self.generic_visit(node)
        left = node.left
        ops = []
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Is, ast.IsNot)) and (self._safe(right) or self._safe(left)):
                op = ast.Eq() if isinstance(op, ast.Is) else ast.NotEq()
            ops.append(op)
            left = right
        node.ops = ops
        return node


def py_changes(old_src, new_src, modname="", touched_text=None, touched_lines=None):
    _FP.clear()
    ch = Changes()
    try:
        ot = ast.parse(old_src)
    except SyntaxError:
        ch.whole = True
        return ch
    nt = ast.parse(new_src)  # SyntaxError handled by caller
    gate = touched_text is None or any(
        k in touched_text for k in (" is ", "==", "!=", " is\n")
    )
    CTX.norm = _IdentityCompare(modname) if gate else None
    CTX.old_lines = old_src.split("\n")
    CTX.new_lines = new_src.split("\n")
    if touched_lines is None:
        ot_, nt_ = set(), set()
        sm = difflib.SequenceMatcher(None, CTX.old_lines, CTX.new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            ot_.update(range(i1 + 1, i2 + 1))
            nt_.update(range(j1 + 1, j2 + 1))
            if i1 == i2:
                ot_.update((i1, i1 + 1))
            if j1 == j2:
                nt_.update((j1, j1 + 1))
        touched_lines = (ot_, nt_)
    CTX.old_touched, CTX.new_touched = frozenset(touched_lines[0]), frozenset(touched_lines[1])
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


_SKIP_ATTRS = {
    "__dict__", "__weakref__", "__module__", "__doc__", "__qualname__", "__firstlineno__",
    "__static_attributes__", "__annotations__", "__annotate__", "__annotate_func__",
    "__annotations_cache__", "__orig_bases__", "__parameters__", "__slots__", "_abc_impl",
}


def _canon(obj, depth=0, active=None):
    """A hashable summary of a value that is equal for the old and the new
    module when the value is the same, even though the objects differ."""
    import enum
    import functools
    import re
    import types

    if active is None:
        active = set()
    if obj is None or isinstance(obj, (bool, int, float, str, bytes, complex)):
        return ("v", type(obj).__name__, obj if not isinstance(obj, float) or obj == obj else "nan")
    if isinstance(obj, enum.Enum):
        return ("enum", type(obj).__module__, type(obj).__qualname__, obj.name)
    if isinstance(obj, type):
        return ("class", obj.__module__, obj.__qualname__)
    if isinstance(obj, types.ModuleType):
        return ("module", obj.__name__)
    if depth > 14:
        return ("deep", type(obj).__qualname__)
    key = id(obj)
    if key in active:
        return ("cycle",)
    active.add(key)
    try:
        d = depth + 1
        if isinstance(obj, types.FunctionType):
            cells = ()
            if obj.__closure__:
                cells = tuple(
                    _canon(c.cell_contents, d, active) if _cell_ok(c) else ("empty",) for c in obj.__closure__
                )
            return ("func", _canon_code(obj.__code__), _canon(obj.__defaults__, d, active),
                    _canon(obj.__kwdefaults__, d, active), cells)
        if isinstance(obj, (types.BuiltinFunctionType, types.MethodWrapperType, types.WrapperDescriptorType)):
            return ("builtin", getattr(obj, "__qualname__", repr(obj)))
        if isinstance(obj, types.MethodType):
            return ("method", _canon(obj.__func__, d, active), _canon(type(obj.__self__), d, active))
        if isinstance(obj, (classmethod, staticmethod)):
            return (type(obj).__name__, _canon(obj.__func__, d, active))
        if isinstance(obj, property):
            return ("property", _canon(obj.fget, d, active), _canon(obj.fset, d, active))
        if isinstance(obj, functools.partial):
            return ("partial", _canon(obj.func, d, active), _canon(obj.args, d, active), _canon(obj.keywords, d, active))
        if isinstance(obj, types.CodeType):
            return _canon_code(obj)
        if isinstance(obj, dict):
            return ("dict", frozenset((_canon(k, d, active), _canon(v, d, active)) for k, v in obj.items()))
        if isinstance(obj, (list, tuple)):
            return (type(obj).__name__, tuple(_canon(x, d, active) for x in obj))
        if isinstance(obj, (set, frozenset)):
            return ("set", frozenset(_canon(x, d, active) for x in obj))
        if isinstance(obj, re.Pattern):
            return ("re", obj.pattern, obj.flags)
        state = getattr(obj, "__dict__", None)
        if isinstance(state, dict):
            return ("obj", type(obj).__module__, type(obj).__qualname__, _canon(state, d, active))
        slots = []
        for k in type(obj).__mro__:
            for name in getattr(k, "__slots__", ()) or ():
                if hasattr(obj, name):
                    slots.append((name, _canon(getattr(obj, name), d, active)))
        if slots:
            return ("slots", type(obj).__qualname__, tuple(slots))
        return ("other", type(obj).__module__, type(obj).__qualname__, repr(obj) if len(repr(obj)) < 200 else "")
    finally:
        active.discard(key)


def _cell_ok(c):
    try:
        c.cell_contents
        return True
    except ValueError:
        return False


def _canon_code(co):
    import types

    consts = tuple(_canon_code(c) if isinstance(c, types.CodeType) else ("c", type(c).__name__, repr(c)) for c in co.co_consts)
    return ("code", co.co_code, consts, co.co_names, co.co_varnames, co.co_freevars, co.co_flags)


def _is_function_like(v):
    import types

    if isinstance(v, (classmethod, staticmethod)):
        v = v.__func__
    if isinstance(v, property):
        return True
    return isinstance(v, types.FunctionType)


def _module_classes(ns, modname, prefix=""):
    out = {}
    for name, val in list(ns.items()):
        if isinstance(val, type) and val.__module__ == modname and val.__qualname__ == prefix + name:
            out[val.__qualname__] = val
            out.update(_module_classes(vars(val), modname, val.__qualname__ + "."))
    return out


def module_diff(modname, old_ns, new_ns):
    """List what differs between the old and new module: (class qualname or
    "", attribute, changed dict keys or None). Methods are left out: their
    code is handled by line coverage. ("+", name) marks a class that exists
    on one side only."""
    out = []
    oc, nc = _module_classes(old_ns, modname), _module_classes(new_ns, modname)
    for q in set(oc) ^ set(nc):
        out.append(("+", q, None))
    scopes = [("", old_ns, new_ns)] + [(q, vars(oc[q]), vars(nc[q])) for q in oc if q in nc]
    for q, ov, nv in scopes:
        for name in set(ov) | set(nv):
            if name in _SKIP_ATTRS or (not q and name.startswith("__") and name.endswith("__")):
                continue
            a, b = ov.get(name, _MISSING), nv.get(name, _MISSING)
            if isinstance(a, type) or isinstance(b, type):
                if isinstance(a, type) and isinstance(b, type) and a.__qualname__ == b.__qualname__:
                    continue
            if _is_function_like(a) and _is_function_like(b):
                continue
            if a is _MISSING or b is _MISSING:
                if _is_function_like(b) and a is _MISSING:
                    out.append((q, name, "method"))  # may now override a base method
                    continue
                if _is_function_like(a):
                    continue  # removed method: callers ran its body (coverage)
                out.append((q, name, None))
                continue
            if _canon(a) == _canon(b):
                continue
            keys = None
            if type(a) is bool and type(b) is bool:
                keys = ("bool", a, b)
            elif isinstance(a, dict) and isinstance(b, dict):
                ks = []
                for k in set(a) | set(b):
                    if k not in a or k not in b or _canon(a[k]) != _canon(b[k]):
                        ks.append(k)
                if ks and all(isinstance(k, type) for k in ks):
                    keys = [(k.__module__, k.__qualname__) for k in ks]
                elif ks and all(isinstance(k, str) for k in ks):
                    vals = [d[k] for d in (a, b) for k in ks if k in d]
                    keys = (
                        "str",
                        sorted(ks),
                        sorted({v for v in vals if isinstance(v, str)}),
                        all(isinstance(v, str) for v in vals),
                    )
            elif isinstance(a, (set, frozenset, list, tuple)) and isinstance(b, (set, frozenset, list, tuple)):
                try:
                    sa, sb = set(a), set(b)
                except TypeError:
                    sa = sb = None
                if sa is not None and sa != sb and all(isinstance(x, str) for x in sa ^ sb):
                    keys = ("str", sorted(sa ^ sb), [], True)
            out.append((q, name, keys))
    return out


_MISSING = object()
_WORD_RE = None
_ITER_RE = None
_LINES = {}


def _source_line(path, line):
    lines = _LINES.get(path)
    if lines is None:
        try:
            lines = open(os.path.join(SRC, path), encoding="utf-8").read().split("\n")
        except OSError:
            lines = []
        _LINES[path] = lines
    return lines[line - 1] if 0 < line <= len(lines) else ""


def _init_res():
    global _WORD_RE, _ITER_RE
    import re

    _WORD_RE = re.compile(r"[A-Za-z0-9_]+|[^A-Za-z0-9_\s]")
    _ITER_RE = re.compile(r"\.(items|keys|values)\(|\bfor\b|\bin\b|\bsorted\(|\blist\(|\bset\(|\*\*")


_init_res()


def probe_module(modname, path, code, timeout=20.0):
    """Run the patched module's top-level code in a forked child, against the
    original versions of every other module, and compare what it defines
    with the original module.

    Returns (imports_ok, diff). diff is None when it could not be computed."""
    if not hasattr(os, "fork") or not _load_sqlglot():
        return True, None
    import importlib
    import pickle

    try:
        old_mod = importlib.import_module(modname)
    except BaseException:
        old_mod = None
    r, w = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(r)
        os.close(w)
        return True, None
    if pid == 0:  # child
        status = 1
        try:
            os.close(r)
            fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
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
            diff = None
            if old_mod is not None:
                try:
                    diff = module_diff(modname, vars(old_mod), mod.__dict__)
                except BaseException:
                    diff = None
            with os.fdopen(w, "wb") as f:
                pickle.dump(diff, f)
        except BaseException:
            status = 1
        finally:
            os._exit(status)
    os.close(w)
    chunks = []
    deadline = time.monotonic() + timeout
    import select as _select

    while True:
        ready, _, _ = _select.select([r], [], [], 0.05)
        if ready:
            data = os.read(r, 1 << 16)
            if data:
                chunks.append(data)
                continue
            break  # end of file: child closed the pipe
        if time.monotonic() > deadline:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            break
    os.close(r)
    try:
        _, st = os.waitpid(pid, 0)
        code_ok = os.waitstatus_to_exitcode(st) == 0
    except OSError:
        code_ok = False
    if not code_ok:
        return False, None
    try:
        return True, pickle.loads(b"".join(chunks)) if chunks else None
    except Exception:
        return True, None


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
        self._direct = {}
        self.hedge = set()

    # -- users of modules and classes ----------------------------------------

    def module_users(self, mod, depth=0, imports=False):
        """Tests that use module ``mod`` in any way we can see: they ran code
        in it, created one of its classes, read it as text, or did one of
        those things with a module that imports it (directly or through a
        chain of imports). The import graph has cycles, so the chain is
        walked as a plain reachability search."""
        key = (mod, imports)
        r = self._mod_users.get(key)
        if r is not None:
            return r
        m = self.m
        seen = {mod}
        stack = [mod]
        while stack:
            cur = stack.pop()
            edges = list(m.importers.get(cur, [])) + list(m.parent_importers.get(cur, ()))
            if imports:
                # Only for "does it still import?": modules that reach it
                # through a lazily loaded package name.
                edges += list(m.lazy_importers.get(cur, []))
            for imp in edges:
                if imp not in seen:
                    seen.add(imp)
                    stack.append(imp)
        if any(x in m.core or x == "sqlglot" for x in seen):
            r = self.all
        else:
            acc = set()
            for x in seen:
                acc |= self.direct_users(x)
            r = frozenset(acc)
        self._mod_users[key] = r
        return r

    def direct_users(self, mod):
        m = self.m
        r = self._direct.get(mod)
        if r is not None:
            return r
        path = path_of_module(mod)
        acc = set(m.file_all(path))
        prefix = mod + ":"
        for key in m.class_info:
            if key.startswith(prefix):
                acc |= m.inst(key)
        if path in m.textreads and not path.startswith("tests/"):
            acc |= m.s(m.textreads[path])
        if path.startswith("tests/"):
            acc |= self.by_file.get(path, set())
        for f, line in m.func_imports.get(mod, ()):
            acc |= m.line_info(f, line)[0]
        r = frozenset(acc)
        self._direct[mod] = r
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
        new_src, old_t, new_t = apply_hunks(old_src, f["hunks"], want_touched=True)
        if os.path.basename(path) == "conftest.py":
            return set(self.all), reasons + ["conftest"]
        try:
            code = compile(new_src, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            reasons.append("syntax error")
            return set(self.import_failure(path)), reasons
        sel = set()
        # Tests that read this file as text see any change, comments included.
        # (pytest itself reads every test file as text, so skip tests/.)
        if old_src != new_src and path in m.textreads and not path.startswith("tests/"):
            sel |= m.s(m.textreads[path])
        touched = "\n".join(t for h in f["hunks"] for tag, t in h["lines"] if tag in "+-")
        ch = py_changes(old_src, new_src, module_of(path), touched, (old_t, new_t))
        if ch.whole:
            return set(self.module_users(module_of(path))), reasons + ["whole"]
        if ch.doc:
            reasons.append("doc")
            sel |= self.doc_tests
        mod = module_of(path)
        probe_diff = None
        if ch.struct and path.startswith("sqlglot/"):
            ok_, probe_diff = probe_module(mod, path, code)
            if not ok_:
                reasons.append("import fails")
                return set(self.import_failure(path)), reasons
        for ln in ch.func_lines:
            tests, flags = m.line_info(path, ln)
            sel |= tests
            for near in (ln - 2, ln - 1, ln + 1, ln + 2):
                self.hedge |= m.line_info(path, near)[0]
            if m.import_mods is None:
                if flags & 17 or (flags & 10 and not tests):
                    reasons.append(f"importline {ln}")
                    sel |= self.module_users(mod)
                continue
            if flags & 80 or (flags & 10 and not tests):
                # Class-creation hooks, once-per-process caches, or code no
                # test ran directly: everyone who uses this module.
                reasons.append(f"importline {ln}")
                sel |= self.module_users(mod)
            elif flags & 1:
                # Ran while some module's top-level code ran (a constant built
                # at import): the users of that module.
                for im in m.import_mods.get(f"{path}:{ln}", ["?"]):
                    reasons.append(f"import-time via {im}")
                    sel |= self.module_users(mod if im == "?" else im)
        dyn = None
        if probe_diff is not None and self.dynamic_ok(ch.struct):
            roots = set()
            for scope, o, n, kind in ch.struct:
                for nm in target_names(o) | target_names(n):
                    roots.add((".".join(scope), nm))
            # Word gating only when nothing else in the file changed.
            dyn = self.diff_users(mod, probe_diff, reasons, roots if not ch.func_lines else ())
        if dyn is not None:
            sel |= dyn
        else:
            for scope, o, n, kind in ch.struct:
                sel |= self.struct_users(path, mod, scope, o, n, kind, reasons)
        return sel, reasons

    # -- module-level changes seen by running the module ---------------------

    @staticmethod
    def dynamic_ok(struct):
        """Use the run-time module diff only when every top-level change just
        binds names (no calls with side effects, no assignments into other
        objects, no conditional blocks)."""
        for scope, o, n, kind in struct:
            if kind == "classhead":
                return False
            for st in (o, n):
                if st is None:
                    continue
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                    continue
                if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                    if all(isinstance(t, ast.Name) or (isinstance(t, ast.Tuple) and all(isinstance(e, ast.Name) for e in t.elts)) for t in targets):
                        continue
                return False
        return True

    def word_gate(self, mod, diff, roots):
        """When every edited name is a table keyed by strings (keywords,
        function names, format codes), the change can only matter to a test
        whose SQL text, or own source code, contains one of those strings, or
        to code that looks the key up by a fixed string, or walks the table.
        Returns that set of tests, or None when this does not apply."""
        m = self.m
        if not m.vocab:
            return None
        root_items = [(q, name, keys) for q, name, keys in diff if (q, name) in roots]
        if not root_items:
            return None
        lexical_bases = (
            "sqlglot.tokens:Tokenizer", "sqlglot.tokenizer_core:TokenizerCore",
            "sqlglot.parser:Parser", "sqlglot.generator:Generator",
            "sqlglot.dialects.dialect:Dialect",
        )
        raw = set()
        groups = []  # a test passes the gate if it has every word of some group

        def add_group(text):
            toks = [w.upper() for w in _WORD_RE.findall(text)]
            if toks:
                groups.append(toks)

        for q, name, keys in root_items:
            if not (isinstance(keys, tuple) and keys and keys[0] == "str"):
                return None
            # Only tables of dialect, tokenizer, parser and generator classes
            # are keyed by SQL text. Other string-keyed tables (an
            # expression's arg_types, settings) are not.
            if q:
                mro = m.class_info.get(f"{mod}:{q}", [])
                if not any(b in mro for b in lexical_bases):
                    return None
            elif not mod.startswith(("sqlglot.dialects.", "sqlglot.parsers.", "sqlglot.generators.")):
                return None
            for k in keys[1]:
                raw.add(k.upper())
                add_group(k)
        # String values of changed entries (old and new), and the keys of
        # derived string-to-string tables (inverse mappings): generated SQL
        # shows these, not the edited key.
        for q, name, keys in diff:
            if not (isinstance(keys, tuple) and keys and keys[0] == "str"):
                continue
            for v in keys[2] if len(keys) > 2 else ():
                add_group(v)
            if (q, name) not in roots and len(keys) > 3 and keys[3]:
                for k in keys[1]:
                    add_group(k)
        if not groups:
            return None
        g = set()
        vocab = m.vocab
        cache = {}
        for toks in groups:
            kg = None
            for w in toks:
                ws = cache.get(w)
                if ws is None:
                    if len(w) == 1 and not w.isalnum():
                        hit = [w] if w in vocab else []
                    else:
                        hit = [v for v in vocab if w in v]
                    ws = set()
                    for v in hit:
                        ws |= m.sets[vocab[v]]
                    cache[w] = ws
                kg = set(ws) if kg is None else (kg & ws)
                if not kg:
                    break
            g |= kg or set()
        for k in raw:
            for f, line in m.str_sites.get(k, ()):
                g |= m.line_info(f, line)[0]
        for q, name, keys in root_items:
            for site in m.sites.get(name, ()):
                if site[2]:
                    continue
                text = _source_line(site[0], site[1])
                if _ITER_RE.search(text):
                    g |= m.line_info(site[0], site[1])[0]
        return g

    def diff_users(self, mod, diff, reasons, roots=()):
        m = self.m
        core = mod in m.core or mod == "sqlglot"
        r = set()
        gate = self.word_gate(mod, diff, set(roots)) if roots else None
        if gate is not None:
            reasons.append(f"word gate {len(gate)}")
        for q, name, keys in diff:
            if q == "+":
                return None
            cls_key = f"{mod}:{q}" if q else None
            if core:
                base = set(m.inst(cls_key)) if cls_key else set(self.all)
            else:
                base = set(self.module_users(mod))
                if cls_key:
                    base |= m.inst(cls_key)
            if keys == "method":
                reasons.append(f"new method {q}.{name}")
                r |= base
                continue
            flip = keys if isinstance(keys, tuple) and keys and keys[0] == "bool" else None
            readers = self.runtime_readers(name, cls_key, mod, q, flip)
            self.hedge |= base
            u = set(base)
            if readers is not None:
                u &= readers
            if gate is not None:
                u &= gate
            if keys and not isinstance(keys, tuple) and all(km.startswith("sqlglot.expressions") for km, _ in keys):
                ku = set()
                for km, kq in keys:
                    ku |= m.inst(f"{km}:{kq}")
                u &= ku
            reasons.append(f"diff {q or '<module>'}.{name}{' keys' if keys else ''}{' readers' if readers is not None else ''}")
            r |= u
        return r

    def runtime_readers(self, name, cls_key, mod, qual, flip=None):
        """Tests that ran a line reading ``name`` while it mattered. None when
        some read happens where we cannot follow it."""
        m = self.m
        ss = m.sites.get(name)
        if not ss:
            return None
        r = set()
        for site in ss:
            f, line, kind = site[0], site[1], site[2]
            owner = site[3] if len(site) > 3 else "s"
            if kind:
                # Reads at import inside the patched module itself were
                # re-run in the child, so their results are in the diff.
                if f == path_of_module(mod) or owner == "S":
                    continue
                if self.import_read_relevant(owner, cls_key, mod, qual):
                    return None
                continue
            tests, flags = m.line_info(f, line)
            if flags & 16:
                return None  # read while building a per-process cache
            if flags & 64 and not tests:
                continue  # read by a class-creation hook: its output is in the diff
            if flags & 1 and (cls_key is None or m.created_at_import(cls_key)):
                if not tests or cls_key is None:
                    return None
            if not tests and flags & 10:
                return None
            if flip is not None and m.guards:
                g = m.guards.get(f"{f}:{line}:{name}")
                if g is not None:
                    pol, body_line = g
                    was_on = bool(flip[1]) == (pol == 1)
                    now_on = bool(flip[2]) == (pol == 1)
                    if was_on and not now_on:
                        # The condition can only turn off: tests that never
                        # entered the body take the same path as before.
                        self.hedge |= tests
                        tests = m.line_info(f, body_line)[0]
            r |= tests
        return r

    def import_read_relevant(self, owner, cls_key, mod, qual):
        m = self.m
        if owner.startswith("a:"):
            base = owner[2:]
            if cls_key is None:
                return True  # module.attr at import: may be our module
            found = False
            for key, mro in m.class_info.items():
                if key.rsplit(".", 1)[-1].split(":")[-1] == base:
                    found = True
                    if cls_key in mro:
                        return True
            return not found
        if owner.startswith("n:"):
            _, omod, oqual = owner.split(":", 2)
            if cls_key is not None:
                return omod == mod and oqual == qual
            return omod == mod or omod in m.importers.get(mod, [])
        return True

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
            self.hedge |= base
            return set(base) & self.expression_users(keys)
        readers = self.name_readers(o, n, f"{mod}:{qual}" if qual else None)
        if qual:
            r = set(m.inst(f"{mod}:{qual}"))
            r |= self.module_users(mod)
        else:
            r = set(self.module_users(mod))
        if readers is not None:
            reasons.append(f"readers of {sorted(target_names(o) | target_names(n))} in {mod}:{qual}")
            self.hedge |= r
            return r & readers
        reasons.append(f"class {qual}" if qual else f"module {mod}")
        return r

    def name_readers(self, o, n, cls_key=None):
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
            for f, line, kind, *_ in ss:
                if kind:
                    return None
                tests, flags = m.line_info(f, line)
                if m.import_mods is None:
                    if flags & 17 or (not tests and flags & 10):
                        return None
                elif flags & 80 or (not tests and flags & 10):
                    return None
                elif flags & 1:
                    # Read while modules imported. That matters only if an
                    # instance of the changed class existed then.
                    if cls_key is None or m.created_at_import(cls_key):
                        return None
                r |= tests
        return r

    def import_failure(self, path):
        """Tests that fail when the module at path cannot be imported: every
        test that imports it, directly, through a chain of imports, inside a
        function, or through sqlglot's lazy loading of dialects by name."""
        mod = module_of(path)
        m = self.m
        if not path.startswith("tests/"):
            if not m.func_imports and not m.dyn_import_sites:
                return set(self.all)  # old map: be safe
            r = set(self.module_users(mod, imports=True))
            if len(r) < len(self.all):
                for f, line in m.dyn_import_sites:
                    r |= m.line_info(f, line)[0]
                # Insurance in idle worker time: tests that import any module
                # of the same package.
                self.hedge |= self.module_users(mod.rpartition(".")[0]) if "." in mod else set()
            return r
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
        self.hedge = set()
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


def pack_slack(workers, ms, extra):
    """Put extra tests into idle worker time without raising the busiest
    worker's load or the worker count, so the cost does not change. These
    are tests near the change that our rules left out: free insurance."""
    if not workers or not extra:
        return workers
    loads = [sum(ms[n] for n in w) for w in workers]
    cap = max(loads)
    for num in sorted(extra, key=lambda n: (ms[n], n)):
        best = None
        for i, load in enumerate(loads):
            if load + ms[num] <= cap and (best is None or loads[i] < loads[best]):
                best = i
        if best is None:
            continue
        workers[best].append(num)
        loads[best] += ms[num]
    return workers


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
    # Loading the map makes many small objects that live until exit; the
    # cyclic garbage collector would only spend time walking them.
    import gc

    gc.disable()
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
        sel_map_chosen = set(chosen)
        if to_data is not None and in_map_numbers:
            chosen = {to_data[n] for n in chosen if n in to_data} | (unknown if chosen else set())
        chosen = sorted(n for n in chosen if n in ms)
        workers = split([(n, ms[n]) for n in chosen])
        if workers and sel is not None and in_map_numbers:
            extra = set(sel.hedge) - set(sel_map_chosen)
            if to_data is not None:
                extra = {to_data[n] for n in extra if n in to_data}
            extra = {n for n in extra if n in ms} - set(chosen)
            workers = pack_slack(workers, ms, extra)
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
