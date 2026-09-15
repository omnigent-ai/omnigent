"""Unit tests: _ensure_backend Databricks auth-then-daemon ordering.

Guards the sequential contract in the remote-server branch of
``_ensure_backend``: auth runs on the calling thread before the daemon starts,
so the daemon's first tunnel attempt has valid credentials and interactive
login output is never interleaved with spinner output.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import click
import pytest

import omnigent.host.connect  # noqa: F401 (evaluate Popen generic aliases)
from omnigent import cli
from omnigent.cli import _ensure_backend

_REMOTE = "https://example.databricksapps.com"


def _no_url_expand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_workspace_api_server_url", lambda s: s.rstrip("/"))


# ---------------------------------------------------------------------------
# auth-before-daemon ordering
# ---------------------------------------------------------------------------


def test_daemon_not_called_until_auth_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon start must come after auth returns, not concurrently."""
    _no_url_expand(monkeypatch)

    log: list[tuple[str, float]] = []

    def _slow_auth(server: str, **_kw: Any) -> None:
        log.append(("auth_start", time.monotonic()))
        time.sleep(0.06)
        log.append(("auth_end", time.monotonic()))

    def _record_daemon(server: str | None) -> bool:
        log.append(("daemon_start", time.monotonic()))
        return False

    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", _slow_auth)
    monkeypatch.setattr(cli, "_ensure_host_daemon", _record_daemon)

    _ensure_backend(_REMOTE)

    auth_end = next(t for name, t in log if name == "auth_end")
    daemon_start = next(t for name, t in log if name == "daemon_start")
    assert daemon_start >= auth_end, (
        f"daemon_start was {auth_end - daemon_start:.3f}s BEFORE auth_end — "
        "race condition: auth and daemon ran concurrently"
    )


# ---------------------------------------------------------------------------
# auth on main thread
# ---------------------------------------------------------------------------


def test_auth_executes_on_calling_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """_ensure_databricks_server_auth must run on the main thread (TTY-safe)."""
    _no_url_expand(monkeypatch)

    seen: list[threading.Thread] = []

    def _capture(server: str, **_kw: Any) -> None:
        seen.append(threading.current_thread())

    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", _capture)
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda s: False)

    _ensure_backend(_REMOTE)

    assert seen, "_ensure_databricks_server_auth was not called"
    assert seen[0] is threading.main_thread(), (
        f"auth ran on {seen[0].name!r} — must run on main thread for TTY safety"
    )


# ---------------------------------------------------------------------------
# auth failure prevents daemon start
# ---------------------------------------------------------------------------


def test_daemon_not_started_when_auth_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon must not start when auth raises, so no orphaned background process."""
    _no_url_expand(monkeypatch)

    daemon_started = threading.Event()

    def _bad_auth(server: str, **_kw: Any) -> None:
        raise click.ClickException(f"Not signed in to {server}")

    def _record_daemon(server: str | None) -> bool:
        daemon_started.set()
        return False

    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", _bad_auth)
    monkeypatch.setattr(cli, "_ensure_host_daemon", _record_daemon)

    with pytest.raises(click.ClickException, match="Not signed in"):
        _ensure_backend(_REMOTE)

    assert not daemon_started.is_set(), (
        "_ensure_host_daemon was called despite auth failure — orphaned daemon"
    )
