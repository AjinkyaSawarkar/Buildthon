"""Hand-made patches of kinds the sample does not show.

Usage: python make_novel.py <sqlglot_src> <out_dir>
Each case: (name, path, old_text, new_text) where old_text must occur once
(new file when old_text is None).
"""

import difflib
import os
import sys

src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)

CASES = [
    ("syntax_dialect", "sqlglot/generators/duckdb.py", "def _last_day_sql(self: DuckDBGenerator, expression: exp.LastDay) -> str:", "def _last_day_sql(self: DuckDBGenerator, expression: exp.LastDay) -> str"),
    ("syntax_core", "sqlglot/helper.py", "def seq_get(seq: Sequence[T], index: int) -> T | None:", "def seq_get(seq: Sequence[T], index: int) -> T | None"),
    ("helper_const", "sqlglot/helper.py", 'CAMEL_CASE_PATTERN = re.compile("(?<!^)(?=[A-Z])")', 'CAMEL_CASE_PATTERN = re.compile("(?<!^)(?=[A-Z0-9])")'),
    ("doctest", "sqlglot/helper.py", '        >>> split_num_words("db.table", ".", 1)', '        >>> split_num_words("db.table", ".", 2)'),
    ("validator", "tests/dialects/test_dialect.py", "            write_sql or sql, expression.sql(dialect=self.dialect, pretty=pretty, identify=identify)", "            write_sql or sql, expression.sql(dialect=self.dialect, pretty=pretty, identify=True)"),
    ("parser_func_key", "sqlglot/parser.py", '        "LOG": build_logarithm,', '        "LOG": build_logarithm,\n        "LN2": build_logarithm,'),
    ("parser_coalesce", "sqlglot/parser.py", '        **dict.fromkeys(("COALESCE", "IFNULL", "NVL"), build_coalesce),', '        **dict.fromkeys(("COALESCE", "IFNULL"), build_coalesce),'),
    ("new_module", "sqlglot/extra_utils.py", None, "def unused():\n    return 1\n"),
    ("readme", "README.md", None, None),
    ("decorator", "sqlglot/expressions/core.py", None, None),
    ("delete_method", "sqlglot/generators/duckdb.py", None, None),
    ("test_assert", "tests/dialects/test_duckdb.py", None, None),
    ("identity_fixture", "tests/fixtures/identity.sql", None, None),
    ("rename_local", "sqlglot/optimizer/eliminate_joins.py", None, None),
    ("init_change", "sqlglot/__init__.py", None, None),
]


def write(name, path, old, new):
    diff = "\n".join(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", lineterm=""))
    with open(os.path.join(out, f"n_{name}.patch"), "w") as f:
        f.write(f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n{diff}\n")


def read(path):
    return open(os.path.join(src, path), encoding="utf-8").read()


for name, path, a, b in CASES:
    if name == "new_module":
        diff = f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,2 @@\n+def unused():\n+    return 1\n"
        open(os.path.join(out, f"n_{name}.patch"), "w").write(diff)
        continue
    text = read(path)
    lines = text.split("\n")
    if name == "readme":
        new = lines[:3] + ["Some extra words."] + lines[3:]
    elif name == "decorator":
        i = next(i for i, l in enumerate(lines) if l.strip() == "@property" and i > 125)
        new = lines[:i] + lines[i + 1 :]
    elif name == "delete_method":
        i = next(i for i, l in enumerate(lines) if l.startswith("def _timediff_sql"))
        j = next(j for j in range(i + 1, len(lines)) if lines[j].startswith("def "))
        body = lines[i + 1 : j]
        new = lines[: i + 1] + ['    return self.func("TIMEDIFF", expression.this)', "", ""] + lines[j:]
    elif name == "test_assert":
        i = next(i for i, l in enumerate(lines) if "self.validate_identity(" in l and l.strip().endswith(")"))
        new = lines[:i] + [lines[i].replace("SELECT", "SELECT  ", 1) if "SELECT" in lines[i] else lines[i].replace('("', '("x ', 1)] + lines[i + 1 :]
    elif name == "identity_fixture":
        i = 40
        new = lines[:i] + [lines[i] + " +"] + lines[i + 1 :]
    elif name == "rename_local":
        new = [l.replace("alias", "alias_name") for l in lines]
    elif name == "init_change":
        i = next(i for i, l in enumerate(lines) if l.startswith("def transpile"))
        new = lines
        new = [l.replace("pretty: bool | None = None", "pretty: bool | None = False") for l in lines]
    else:
        assert text.count(a) == 1, (name, text.count(a))
        new = text.replace(a, b).split("\n")
    if new == lines:
        print("no change", name, file=sys.stderr)
        continue
    write(name, path, lines, new)
