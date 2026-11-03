"""Opt-in pytest guard for dependency smoke suites that must execute every case."""

from __future__ import annotations

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.skipped:
        report.outcome = "failed"
        report.longrepr = f"Required dependency test skipped: {report.longrepr}"


def pytest_sessionfinish(session):
    if session.testscollected == 0:
        session.exitstatus = pytest.ExitCode.NO_TESTS_COLLECTED
