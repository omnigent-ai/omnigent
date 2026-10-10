"""Tests for the repro-agent driver's pure helpers (``dev/repro.py``).

Covers the launch-env tweaks ``dev/repro.py`` applies before it runs
``omnigent run`` — the parts worth pinning because a wrong value silently
changes how the launch behaves under a shared server. The subprocess / git
plumbing (``main``) is exercised manually, not here.
"""

from __future__ import annotations

from dev.repro import _shared_server_launch_env


def test_shared_server_launch_env_raises_host_wait_for_shared_server() -> None:
    """A ``--server`` launch gets the longer host-online wait."""
    env = _shared_server_launch_env({}, server="https://app.example.com")
    assert env["OMNIGENT_HOST_ONLINE_TIMEOUT_S"] == "120"


def test_shared_server_launch_env_leaves_local_run_at_default() -> None:
    """A local run (no ``--server``) keeps the client default — no override."""
    env = _shared_server_launch_env({}, server=None)
    assert "OMNIGENT_HOST_ONLINE_TIMEOUT_S" not in env


def test_shared_server_launch_env_respects_explicit_override() -> None:
    """An explicitly exported wait is never clobbered."""
    env = _shared_server_launch_env(
        {"OMNIGENT_HOST_ONLINE_TIMEOUT_S": "300"}, server="https://app.example.com"
    )
    assert env["OMNIGENT_HOST_ONLINE_TIMEOUT_S"] == "300"
