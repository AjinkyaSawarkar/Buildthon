"""pytest plugin used only at build time to record, for every test:

* which source lines it runs (coverage.py with one context per test phase),
* which files it opens (fixtures, source files read as text),
* which sqlglot classes it creates (dialects, tokenizers, parsers, generators,
  expressions).

Contexts used for coverage:
  "import"            importing every sqlglot module before any test
  "warmup"            touching every dialect once, before any test
  "collect:<file>"    importing a test file during collection
  "s:<nodeid>"        setup phase of a test (class fixtures such as setUpClass)
  "c:<nodeid>"        call and teardown phase of a test

Output: one pickle per process in $TRACE_OUT.
"""

import os
import pickle
import sys

import coverage
import pytest

OUT = os.environ["TRACE_OUT"]
ROOT = os.path.abspath(os.environ.get("TRACE_ROOT", os.getcwd()))

_state = {"ctx": "import"}
_opens = {}  # ctx -> set of (relpath, mode)
_classes = {}  # ctx -> set of "module:qualname"
_cov = None


def _audit(event, args):
    if event != "open":
        return
    try:
        path = args[0]
        if not isinstance(path, str):
            return
        path = os.path.abspath(path)
        if not path.startswith(ROOT + os.sep):
            return
        rel = os.path.relpath(path, ROOT)
        if rel.endswith(".pyc") or "__pycache__" in rel:
            return
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else "r"
        _opens.setdefault(_state["ctx"], set()).add((rel, mode))
    except Exception:
        pass


def _set(ctx):
    _state["ctx"] = ctx
    if _cov is not None:
        _cov.switch_context(ctx)


def _wrap_init(cls):
    original = cls.__init__

    def __init__(self, *args, **kwargs):
        t = type(self)
        seen = _classes.setdefault(_state["ctx"], set())
        seen.add(f"{t.__module__}:{t.__qualname__}")
        result = original(self, *args, **kwargs)
        d = getattr(self, "dialect", None) if not hasattr(t, "arg_types") else None
        if d is not None:
            dt = type(d)
            seen.add(f"{dt.__module__}:{dt.__qualname__}")
        return result

    __init__.__wrapped__ = original
    cls.__init__ = __init__


_child = {"cov": None}


def _child_start():
    """Start coverage in a process-pool worker (spawned child)."""
    if _child["cov"] is not None:
        return _child["cov"]
    import multiprocessing.util

    cov = coverage.Coverage(
        data_file=os.path.join(OUT, "cov"),
        data_suffix=True,
        source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")],
        config_file=False,
    )
    cov.start()
    cov.switch_context("import")
    _import_everything()
    sys.addaudithook(_audit)
    _wrap_all()

    def _save():
        cov.stop()
        cov.save()
        with open(os.path.join(OUT, f"extra.{os.getpid()}.pkl"), "wb") as f:
            pickle.dump({"opens": _opens, "classes": _classes}, f)

    multiprocessing.util.Finalize(None, _save, exitpriority=100)
    _child["cov"] = cov
    return cov


class _CtxCall:
    """Picklable wrapper that runs fn in a child under the parent's context."""

    def __init__(self, ctx, fn):
        self.ctx = ctx
        self.fn = fn

    def __call__(self, *args, **kwargs):
        cov = _child_start()
        cov.switch_context(self.ctx)
        _state["ctx"] = self.ctx
        try:
            result = self.fn(*args, **kwargs)
            try:  # the pool pickles the result after we return: do it here too
                pickle.dumps(result)
            except Exception:
                pass
            return result
        finally:
            cov.switch_context("between")
            _state["ctx"] = "between"


def _patch_pools():
    import concurrent.futures.process as cfp

    orig_init = cfp.ProcessPoolExecutor.__init__
    orig_submit = cfp.ProcessPoolExecutor.submit

    def __init__(self, *args, **kwargs):
        init = kwargs.get("initializer")
        if init is not None:
            kwargs["initializer"] = _CtxCall(_state["ctx"], init)
        return orig_init(self, *args, **kwargs)

    def submit(self, fn, /, *args, **kwargs):
        return orig_submit(self, _CtxCall(_state["ctx"], fn), *args, **kwargs)

    cfp.ProcessPoolExecutor.__init__ = __init__
    cfp.ProcessPoolExecutor.submit = submit


def _import_everything():
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if "sqlglotc" in info.name or info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception as e:  # noqa
            print("import failed", info.name, e, file=sys.stderr)
    from sqlglot.dialects import DIALECTS

    for name in DIALECTS:
        getattr(sqlglot.dialects, name)


def pytest_configure(config):
    global _cov
    if getattr(config, "workerinput", None) is None and config.getoption("numprocesses", None):
        return  # xdist controller: workers do the tracing
    sys.addaudithook(_audit)
    _cov = coverage.Coverage(
        data_file=os.path.join(OUT, "cov"),
        data_suffix=True,
        source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")],
        config_file=False,
    )
    _cov.start()
    _set("import")
    _import_everything()
    _patch_pools()
    import sqlglot
    from sqlglot.dialects import DIALECTS
    from sqlglot.dialects.dialect import Dialect

    # Run the same small workload twice. Lines that run the first time but
    # not the second are once-per-process work (caches): make_map marks them.
    for ctx in ("warmup", "warmup2"):
        _set(ctx)
        _warmup(sqlglot, DIALECTS, Dialect)
    _wrap_all()
    _set("collect:?")


def _warmup(sqlglot, DIALECTS, Dialect):
    from sqlglot.optimizer import optimize
    from sqlglot.optimizer.annotate_types import annotate_types

    schema = {"b": {"a": "INT", "c": "VARCHAR", "d": "DATE"}}
    queries = [
        "SELECT a FROM b WHERE c = 1",
        "SELECT a, COUNT(*) AS n FROM b WHERE d > CAST('2020-01-01' AS DATE) GROUP BY a ORDER BY n LIMIT 5",
        "WITH x AS (SELECT a FROM b) SELECT x.a, UPPER(c) FROM x JOIN b ON x.a = b.a",
    ]
    for name in [""] + list(DIALECTS):
        try:
            d = Dialect.get_or_raise(name.lower())
        except Exception:
            continue
        for q in queries:
            try:
                e = sqlglot.parse_one(q, read=d)
                e.sql(dialect=d)
                e.sql(dialect=d, pretty=True)
                annotate_types(e.copy(), schema=schema, dialect=d)
                optimize(e.copy(), schema=schema, dialect=d).sql(dialect=d)
            except Exception:
                pass
    try:
        from sqlglot.executor import execute

        execute("SELECT a, SUM(a) AS s FROM t GROUP BY a", tables={"t": [{"a": 1}, {"a": 2}]})
    except Exception:
        pass


def _wrap_all():
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokenizer_core import TokenizerCore

    for cls in (Expr, Generator, Parser, TokenizerCore, Dialect):
        _wrap_init(cls)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if _cov is None:
        yield
        return
    path = getattr(collector, "path", None)
    if path is not None and str(path).endswith(".py"):
        _set("collect:" + os.path.relpath(str(path), ROOT))
    yield
    _set("collect:?")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    if _cov is not None:
        _set("s:" + item.nodeid)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if _cov is not None:
        _set("c:" + item.nodeid)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    if _cov is not None:
        _set("c:" + item.nodeid)
    yield
    if _cov is not None:
        _set("between")


def pytest_unconfigure(config):
    if _cov is None:
        return
    _cov.stop()
    _cov.save()
    with open(os.path.join(OUT, f"extra.{os.getpid()}.pkl"), "wb") as f:
        pickle.dump({"opens": _opens, "classes": _classes}, f)
