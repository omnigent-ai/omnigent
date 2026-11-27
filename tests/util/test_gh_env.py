"""Tests for the ``gh`` subprocess environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.util import gh_env


def _socket_at(monkeypatch: pytest.MonkeyPatch, *sockets: Path) -> None:
    """Make only *sockets* look like unix sockets."""
    monkeypatch.setattr(gh_env.Path, "is_socket", lambda self: self in sockets)


def test_keeps_an_address_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _socket_at(monkeypatch)
    env = gh_env.gh_subprocess_env({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/7/bus"})
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/7/bus"


def test_names_the_runtime_bus_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The socket under XDG_RUNTIME_DIR, else under /run/user/<uid>, as gh finds it."""
    xdg = tmp_path / "xdg"
    run_user = tmp_path / "run-user"
    monkeypatch.setattr(gh_env, "_RUN_USER_DIR", run_user)
    monkeypatch.setattr(gh_env.os, "getuid", lambda: 7)
    _socket_at(monkeypatch, xdg / "bus")
    env = gh_env.gh_subprocess_env({"XDG_RUNTIME_DIR": str(xdg)})
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={xdg / 'bus'}"

    # XDG_RUNTIME_DIR points elsewhere, but the uid's runtime bus exists.
    _socket_at(monkeypatch, run_user / "7" / "bus")
    env = gh_env.gh_subprocess_env({"XDG_RUNTIME_DIR": str(xdg)})
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={run_user / '7' / 'bus'}"


def test_reads_a_dbus_session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_user = tmp_path / "run-user"
    (run_user / "7").mkdir(parents=True)
    (run_user / "7" / "dbus-session").write_text(
        "DBUS_SESSION_BUS_ADDRESS='unix:abstract=/tmp/dbus-example,guid=abc'\n"
        "DBUS_SESSION_BUS_PID=4242\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gh_env, "_RUN_USER_DIR", run_user)
    monkeypatch.setattr(gh_env.os, "getuid", lambda: 7)
    _socket_at(monkeypatch)
    env = gh_env.gh_subprocess_env({})
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:abstract=/tmp/dbus-example,guid=abc"


def test_disables_the_bus_when_none_can_be_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh_env, "_RUN_USER_DIR", tmp_path / "missing")
    _socket_at(monkeypatch)
    env = gh_env.gh_subprocess_env({"XDG_RUNTIME_DIR": str(tmp_path / "xdg"), "PATH": "/usr/bin"})
    assert env["DBUS_SESSION_BUS_ADDRESS"] == gh_env.GH_NO_SESSION_BUS
    assert env["PATH"] == "/usr/bin"
