r"""A claude-native session must still start when its persisted
``terminal_launch_args`` would make Claude Code exit at startup.

The runner replays a session's stored ``terminal_launch_args`` on every
(re)launch. Two stored shapes make ``claude`` exit before its TUI starts, so the
session's terminal is torn down and the session can never come up again:

* ``--print``/``-p`` with no stdin prompt — ``claude`` errors that input must be
  provided through stdin or a prompt argument.
* ``--mcp-config <path>`` whose file was deleted after it was stored — ``claude``
  errors that the MCP config file is not found.

Both are real-process failures: they only appear when the actual ``claude`` CLI
the runner launches rejects the replayed args, so this exercises the live
runner + terminal boundary rather than a composed-argv seam.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
)

# claude-native auto-launch + first-run pre-accept + terminal advert.
_STARTUP_BUDGET_S = 120
# A startup exit tears the terminal down within a few seconds, so a terminal
# that stays live this long has actually come up rather than merely flickered
# through the pre-exit moment.
_SETTLE_S = 20


def _session(base_url: str, session_id: str) -> dict:
    return httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()


def _terminal_resources(base_url: str, session_id: str) -> list[dict]:
    data = (
        httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
        .json()
        .get("data", [])
    )
    return [r for r in data if r.get("type") == "terminal"]


def _await_startup(base_url: str, session_id: str) -> tuple[str, str, list[str]]:
    """Settle the session's terminal launch.

    :returns: ``(outcome, status, trail)`` where outcome is ``"up"`` when a
        terminal stays live and the session is usable, ``"failed"`` when the
        runner tore the terminal down, or ``"timeout"``.
    """
    deadline = time.time() + _STARTUP_BUDGET_S
    saw_terminal = False
    up_since: float | None = None
    trail: list[str] = []
    status = "unknown"
    while time.time() < deadline:
        status = str(_session(base_url, session_id).get("status"))
        has_terminal = bool(_terminal_resources(base_url, session_id))
        trail.append(f"{status}/{int(has_terminal)}")
        if status == "failed":
            return "failed", status, trail
        if has_terminal:
            saw_terminal = True
        elif saw_terminal:
            return "failed", status, trail  # terminal came up then was torn down
        if has_terminal and status in {"idle", "running", "working"}:
            up_since = up_since or time.time()
            if time.time() - up_since >= _SETTLE_S:
                return "up", status, trail
        else:
            up_since = None
        time.sleep(1.0)
    return ("up" if up_since is not None else "timeout"), status, trail


def test_stored_print_arg_does_not_prevent_terminal_startup(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_claude_session(
        live_server, runner_id, terminal_launch_args=["--print"]
    )
    try:
        outcome, status, trail = _await_startup(live_server, session_id)
        assert outcome == "up", (
            "claude-native session with a stored --print arg could not start its "
            f"terminal; last status={status}, trail={trail}"
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)


def test_stored_missing_mcp_config_does_not_prevent_terminal_startup(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    # The file exists when the args are stored, then is deleted before the
    # launch replays them.
    config = tmp_path / "servers.json"
    config.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    created = create_native_session(
        httpx,
        live_server,
        harness="claude",
        metadata={"terminal_launch_args": ["--mcp-config", str(config)]},
    )
    session_id = str(created["session_id"])
    config.unlink()
    bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
    try:
        outcome, status, trail = _await_startup(live_server, session_id)
        assert outcome == "up", (
            "claude-native session whose stored --mcp-config file was deleted "
            f"could not start its terminal; last status={status}, trail={trail}"
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)
