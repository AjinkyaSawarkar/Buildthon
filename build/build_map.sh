#!/bin/sh
# Rebuild selector/map.json.gz and selector/src.tgz from sqlglot at the pinned commit.
# Needs: git, Python 3.13, the packages in requirements.lock (plus coverage), 2+ cores.
# Usage: sh build/build_map.sh /path/to/tests.json
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
TESTS_JSON=$1
WORK=${WORK:-/tmp/br_build}
SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
rm -rf "$WORK" && mkdir -p "$WORK/trace"
git clone -q https://github.com/tobymao/sqlglot.git "$WORK/sqlglot"
cd "$WORK/sqlglot" && git checkout -q $SHA
COVERAGE_CORE=ctrace TRACE_OUT="$WORK/trace" TRACE_ROOT="$PWD" SKIP_INTEGRATION=0 \
  PYTHONPATH="$PWD:$HERE/build" python -m pytest -q -n 2 -p no:cacheprovider -p trace_plugin
python "$HERE/build/make_map.py" "$WORK/trace" "$PWD" "$TESTS_JSON" "$HERE/selector/map.json.gz"
git ls-files | grep -vE "^tests/fixtures/" | grep -E "\.py$" | tar czf "$HERE/selector/src.tgz" -T -
