"""Tests for the background daemon entrypoint's host-process handoff.

The foreground CLI path has its own forwarding tests; these cover the
auto-launched daemon (``omnigent.host._daemon_entry``): ``--local`` must
hand the health-verified local-server pid to ``run_host_process`` (the
adopted-orphan exclusion binds to exactly that incarnation), and
``--server`` must hand ``None`` (a remote host owns no local server).
"""

from __future__ import annotations

import sys

import pytest

from omnigent.host import _daemon_entry
from omnigent.host.local_server import LocalServerStartup


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[tuple[str, int | None]]:
    """Run ``_daemon_entry.main`` with stubs, capturing the handoff.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param argv: Daemon argv after the program name.
    :returns: Captured ``(server_url, local_server_pid)`` calls.
    """
    calls: list[tuple[str, int | None]] = []
    monkeypatch.setattr(sys, "argv", ["_daemon_entry", *argv])
    monkeypatch.setattr(
        "omnigent.process_logging.configure_process_logging", lambda name, force=False: None
    )
    monkeypatch.setattr(
        "omnigent.host.local_server.ensure_local_omnigent_server",
        lambda: LocalServerStartup(url="http://127.0.0.1:8000", spawned=True, pid=4242),
    )
    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        lambda server_url, local_server_pid=None: calls.append((server_url, local_server_pid)),
    )
    _daemon_entry.main()
    return calls


def test_local_daemon_forwards_server_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--local`` hands the spawner-verified server pid to the host."""
    calls = _run_main(monkeypatch, ["--local"])
    assert calls == [("http://127.0.0.1:8000", 4242)]


def test_remote_daemon_forwards_no_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--server`` owns no local server: nothing may be excluded."""
    calls = _run_main(monkeypatch, ["--server", "https://server.example.com"])
    assert calls == [("https://server.example.com", None)]
