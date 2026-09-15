"""Regression tests: CLI Databricks auth must complete before host daemon starts.

``_ensure_backend`` in ``omnigent/cli.py`` used to run
``_ensure_databricks_server_auth`` and ``_ensure_host_daemon`` on two worker
threads via ``ThreadPoolExecutor(max_workers=2)``.  Three distinct failure modes
are asserted here, each of which should FAIL on the buggy code and
PASS after the fix:

1. **Ordering race**: the daemon spawns while auth is still in progress, so it
   connects to the server before credentials are written to disk.
2. **Orphaned daemon**: if auth raises after the daemon has already been
   spawned, the exception short-circuits ``daemon_future.result()`` and the
   running daemon is never reaped.
3. **Non-main-thread login**: ``_ensure_databricks_server_auth`` (which may
   drive an interactive browser login) executes on a ``ThreadPoolExecutor``
   worker, which has no TTY and whose ``click.echo`` output is interleaved with
   the main thread.

Run::

    pytest tests/e2e/test_databricks_auth_daemon_ordering.py -v

"""

from __future__ import annotations

import threading
import time
from typing import Any

import click
import pytest

# Eager import so Popen generic aliases are evaluated before any patch.
import omnigent.host.connect  # noqa: F401
from omnigent import cli
from omnigent.cli import _ensure_backend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REMOTE_SERVER = "https://example.databricksapps.com"


def _patch_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out network-touching URL normalization.

    ``_ensure_backend`` calls ``_resolve_server_url(server).api_base`` which
    internally calls ``_workspace_api_server_url`` to expand bare workspace
    URLs.  Patch it to a no-op so tests stay offline.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(cli, "_workspace_api_server_url", lambda s: s.rstrip("/"))


# ---------------------------------------------------------------------------
# Bug 1: ordering race — daemon spawned before auth completes
# ---------------------------------------------------------------------------


def test_daemon_spawned_after_auth_not_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ensure_host_daemon must not start until _ensure_databricks_server_auth finishes.

    On the buggy code both functions are submitted to a ThreadPoolExecutor
    simultaneously, so the daemon connects to the server before auth writes
    valid credentials to disk.  On the fixed code auth must complete before the
    daemon is spawned.

    The test demonstrates the race by making auth block for a short interval
    and recording the wall-clock time at which the daemon call arrives.  On the
    buggy concurrent path the daemon is called while auth is still running; on
    the fixed sequential path the daemon is only called after auth finishes.
    """
    _patch_url_resolution(monkeypatch)

    auth_completed = threading.Event()
    call_log: list[tuple[str, float]] = []

    def _slow_auth(server: str, **_kw: Any) -> None:
        call_log.append(("auth_start", time.monotonic()))
        # Simulate a slow credential refresh or interactive login prompt that
        # holds the auth thread for a noticeable interval.
        time.sleep(0.08)
        call_log.append(("auth_end", time.monotonic()))
        auth_completed.set()

    def _record_daemon(server: str | None) -> bool:
        call_log.append(("daemon_start", time.monotonic()))
        return False

    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", _slow_auth)
    monkeypatch.setattr(cli, "_ensure_host_daemon", _record_daemon)

    _ensure_backend(_REMOTE_SERVER)

    auth_end_t = next(t for name, t in call_log if name == "auth_end")
    daemon_start_t = next(t for name, t in call_log if name == "daemon_start")

    # On FIXED code: daemon only starts after auth finishes.
    # On BUGGY code: daemon starts concurrently and daemon_start_t < auth_end_t.
    assert daemon_start_t >= auth_end_t, (
        "race: _ensure_host_daemon was called at "
        f"{daemon_start_t - auth_end_t:.3f}s BEFORE auth completed — "
        f"full log: {call_log}"
    )


# ---------------------------------------------------------------------------
# Bug 2: orphaned daemon — daemon is spawned even when auth later raises
# ---------------------------------------------------------------------------


def test_no_daemon_spawned_when_auth_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If auth fails the daemon must not be left orphaned.

    On the buggy code both futures are submitted at once.  ``auth_future.result()``
    re-raises the auth exception, but by that point the daemon thread has
    already executed and spawned a background process.  The exception short-
    circuits ``daemon_future.result()``, leaving the daemon unmonitored.

    The test captures whether ``_ensure_host_daemon`` was called at all when
    ``_ensure_databricks_server_auth`` raises.  On the fixed (sequential) code
    the auth failure is raised before the daemon is ever touched.
    """
    _patch_url_resolution(monkeypatch)

    daemon_called = threading.Event()

    def _failing_auth(server: str, **_kw: Any) -> None:
        # Simulate an auth failure (e.g. missing Databricks credentials).
        raise click.ClickException(
            f"Not signed in to {server}. Run `omnigent login {server}` and retry."
        )

    def _record_daemon(server: str | None) -> bool:
        daemon_called.set()
        return False

    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", _failing_auth)
    monkeypatch.setattr(cli, "_ensure_host_daemon", _record_daemon)

    with pytest.raises(click.ClickException, match="Not signed in"):
        _ensure_backend(_REMOTE_SERVER)

    # On FIXED code: auth is sequential so the daemon is never started.
    # On BUGGY code: both futures are submitted together so the daemon runs
    # even though auth fails, leaving an orphaned daemon.
    assert not daemon_called.is_set(), (
        "orphan: _ensure_host_daemon was called even though "
        "_ensure_databricks_server_auth raised — the daemon is orphaned."
    )


# ---------------------------------------------------------------------------
# Bug 3: non-main-thread login — interactive auth runs on a worker thread
# ---------------------------------------------------------------------------


def test_auth_runs_on_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ensure_databricks_server_auth must execute on the main thread.

    Interactive login (``_databricks_login``, ``click.echo``, ``sys.stdin``
    reads) is not safe from a ``ThreadPoolExecutor`` worker: the worker has no
    controlling terminal, and concurrent stdout/stderr writes corrupt the
    display.  On the fixed code auth is run directly on the calling thread;
    both calls run sequentially on the main thread.
    """
    _patch_url_resolution(monkeypatch)

    auth_thread: list[threading.Thread] = []

    def _capture_auth_thread(server: str, **_kw: Any) -> None:
        auth_thread.append(threading.current_thread())

    def _noop_daemon(server: str | None) -> bool:
        return False

    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", _capture_auth_thread)
    monkeypatch.setattr(cli, "_ensure_host_daemon", _noop_daemon)

    _ensure_backend(_REMOTE_SERVER)

    assert auth_thread, "_ensure_databricks_server_auth was never called"
    assert auth_thread[0] is threading.main_thread(), (
        "thread-safety: _ensure_databricks_server_auth ran on "
        f"worker thread {auth_thread[0].name!r} instead of the main thread — "
        "interactive login (click.echo, sys.stdin) is not safe here."
    )
