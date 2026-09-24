"""Verify auth ordering, failure cleanup, and calling-thread affinity."""

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

_REMOTE_SERVER = "https://example.databricksapps.com"


def _patch_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep URL normalization offline."""
    monkeypatch.setattr(cli, "_workspace_api_server_url", lambda s: s.rstrip("/"))


def test_daemon_spawned_after_auth_not_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start the daemon only after authentication finishes."""
    _patch_url_resolution(monkeypatch)

    auth_completed = threading.Event()
    call_log: list[tuple[str, float]] = []

    def _slow_auth(server: str, **_kw: Any) -> None:
        call_log.append(("auth_start", time.monotonic()))
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

    assert daemon_start_t >= auth_end_t, (
        "race: _ensure_host_daemon was called at "
        f"{daemon_start_t - auth_end_t:.3f}s BEFORE auth completed — "
        f"full log: {call_log}"
    )


def test_no_daemon_spawned_when_auth_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not start a daemon when authentication fails."""
    _patch_url_resolution(monkeypatch)

    daemon_called = threading.Event()

    def _failing_auth(server: str, **_kw: Any) -> None:
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

    assert not daemon_called.is_set(), (
        "orphan: _ensure_host_daemon was called even though "
        "_ensure_databricks_server_auth raised — the daemon is orphaned."
    )


def test_auth_runs_on_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run interactive authentication on the main thread."""
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
