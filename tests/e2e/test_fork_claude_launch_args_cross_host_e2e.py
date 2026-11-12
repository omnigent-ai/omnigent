"""A Claude Code fork launched on another host starts without its source's machine-local args.

A launcher can start a Claude Code session with an ``--mcp-config`` file under
that machine's home directory. A fork copies the session's stored launch args,
so launching the fork on a different host replayed a path that isn't there, and
Claude Code exited at startup on every launch. Two host daemons with separate
home directories stand in for the two machines; the fork is launched the way the
fork dialog does it, through ``POST /v1/hosts/{id}/runners``, and the real
``claude`` CLI either starts or exits.

Needs the ``claude`` CLI on ``PATH``; set ``OMNIGENT_E2E_CLAUDE_NATIVE=1`` to run::

    OMNIGENT_E2E_CLAUDE_NATIVE=1 \\
    .venv/bin/python -m pytest tests/e2e/test_fork_claude_launch_args_cross_host_e2e.py \\
        --llm-api-key "mock-key" -v
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host
from tests.e2e.test_host_claude_native_e2e import _claude_native_agent_id

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1" or shutil.which("claude") is None,
    reason="needs the claude CLI; set OMNIGENT_E2E_CLAUDE_NATIVE=1 to run",
)

# A startup exit tears the terminal down within seconds of launch, so a terminal
# that stays up this long actually started.
_SETTLE_S = 20.0
_STARTUP_BUDGET_S = 120.0


def _await_terminal_outcome(client: httpx.Client, session_id: str) -> tuple[str, list[str]]:
    """Wait until the session's Claude terminal stays up, or the session fails.

    :returns: ``("up" | "failed" | "timeout", trail)`` where *trail* records
        each poll's ``status/terminal`` for the failure message.
    """
    deadline = time.monotonic() + _STARTUP_BUDGET_S
    up_since: float | None = None
    trail: list[str] = []
    while time.monotonic() < deadline:
        session = client.get(f"/v1/sessions/{session_id}", timeout=10.0).json()
        status = str(session.get("status"))
        resources = client.get(f"/v1/sessions/{session_id}/resources", timeout=10.0).json()
        has_terminal = any(r.get("type") == "terminal" for r in resources.get("data", []))
        trail.append(f"{status}/{int(has_terminal)}")
        if status == "failed":
            trail.append(f"error={session.get('last_task_error')}")
            return "failed", trail
        if has_terminal and status in {"idle", "running"}:
            up_since = up_since or time.monotonic()
            if time.monotonic() - up_since >= _SETTLE_S:
                return "up", trail
        else:
            up_since = None
        time.sleep(1.0)
    return "timeout", trail


def test_fork_launched_on_another_host_starts_without_the_source_hosts_args(
    live_server: str,
    tmp_path: Path,
) -> None:
    host_env = {"PATH": os.environ["PATH"]}
    with (
        httpx.Client(base_url=live_server, timeout=30.0) as client,
        isolated_host(tmp_path / "source-host", live_server, env=host_env) as source_host,
        isolated_host(tmp_path / "other-host", live_server, env=host_env) as other_host,
    ):
        source_host.wait_online(client, timeout=60)
        other_host.wait_online(client, timeout=60)
        # Both hosts share this machine's filesystem, so the launcher's MCP
        # config is a path relative to the source's workspace: Claude resolves
        # it against its working directory, so it only exists where the source
        # runs, as a file under the laptop's home only exists on the laptop.
        source_workspace = source_host.home / "work"
        other_workspace = other_host.home / "work"
        source_workspace.mkdir()
        other_workspace.mkdir()
        config = "mcp-inject.json"
        (source_workspace / config).write_text('{"mcpServers": {}}\n', encoding="utf-8")

        created = client.post(
            "/v1/sessions",
            json={
                "agent_id": _claude_native_agent_id(client),
                "host_id": source_host.host_id,
                "workspace": str(source_workspace),
                "terminal_launch_args": ["--mcp-config", config],
            },
            timeout=60.0,
        )
        created.raise_for_status()
        source_id = str(created.json()["id"])
        outcome, trail = _await_terminal_outcome(client, source_id)
        assert outcome == "up", f"the source session never started; trail={trail}"

        forked = client.post(f"/v1/sessions/{source_id}/fork", json={}, timeout=60.0)
        forked.raise_for_status()
        fork_id = str(forked.json()["id"])
        launched = client.post(
            f"/v1/hosts/{other_host.host_id}/runners",
            json={"session_id": fork_id, "workspace": str(other_workspace)},
            timeout=120.0,
        )
        assert launched.status_code == 200, launched.text

        outcome, trail = _await_terminal_outcome(client, fork_id)
        assert outcome == "up", (
            f"the fork launched on another host could not start Claude Code; trail={trail}"
        )
        fork = client.get(f"/v1/sessions/{fork_id}", timeout=10.0).json()
        assert config not in (fork.get("terminal_launch_args") or [])
        source = client.get(f"/v1/sessions/{source_id}", timeout=10.0).json()
        assert source.get("terminal_launch_args") == ["--mcp-config", config]
