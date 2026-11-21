"""Record every failing test node id (subtest failures included) to $GT_OUT."""
import os

_failed = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _failed.add("COLLECT:" + report.nodeid)


def pytest_sessionfinish(session):
    out = os.environ.get("GT_OUT")
    if out:
        with open(out + f".{os.getpid()}", "w") as f:
            f.write("\n".join(sorted(_failed)))
