"""Pytest plugin recording os_env helper subprocesses that outlive a test's fixtures.

Load with ``-p tests.runner._os_env_fixture_teardown_probe``. For every test
that used one of :data:`WATCHED_FIXTURES`, once all of its fixtures have been
torn down it appends the ``omnigent.inner.os_env helper`` children that were
not alive before the test to the JSON-lines file named by
``OMNIGENT_FIXTURE_PROBE_LOG``.
"""

from __future__ import annotations

import json
import os
import time

import psutil
import pytest

WATCHED_FIXTURES = frozenset({"registry", "glob_client", "make_os_env"})

_before_by_nodeid: dict[str, set[int]] = {}


def live_helper_pids() -> set[int]:
    """Return the pids of this process's live ``os_env`` helper descendants."""
    pids: set[int] = set()
    for child in psutil.Process().children(recursive=True):
        try:
            # A killed but not yet reaped helper is a zombie, not a leak.
            if child.status() == psutil.STATUS_ZOMBIE:
                continue
            cmdline = child.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "omnigent.inner.os_env" in cmdline and "helper" in cmdline:
            pids.add(child.pid)
    return pids


def wait_for_helpers_to_exit(started: set[int], timeout: float = 2.0) -> set[int]:
    """Return the pids in *started* still alive after *timeout* seconds.

    ``close()`` may escalate to ``kill()`` without waiting, so a helper can be
    observed for a moment after teardown; only one that stays alive is a leak.
    """
    deadline = time.monotonic() + timeout
    remaining = live_helper_pids() & started
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = live_helper_pids() & started
    return remaining


def _watched(item: pytest.Item) -> list[str]:
    if not os.environ.get("OMNIGENT_FIXTURE_PROBE_LOG"):
        return []
    return sorted(WATCHED_FIXTURES & set(getattr(item, "fixturenames", ())))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if _watched(item):
        _before_by_nodeid[item.nodeid] = live_helper_pids()


# ``trylast`` runs after pytest's own teardown hook has finalized the test's
# function-scoped fixtures, so every watched fixture has already been torn down.
@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    before = _before_by_nodeid.pop(item.nodeid, None)
    fixtures = _watched(item)
    if before is None or not fixtures:
        return
    record = {
        "test": item.nodeid,
        "fixtures": fixtures,
        "helpers_alive_after_teardown": sorted(
            wait_for_helpers_to_exit(live_helper_pids() - before)
        ),
    }
    with open(os.environ["OMNIGENT_FIXTURE_PROBE_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
