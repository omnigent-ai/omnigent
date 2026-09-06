"""Regression tests: opencode-native MCP entries must not inherit the 60 s default.

The bug: opencode's MCP client defaults to a 60 s SDK timeout for every tool call.
Omnigent's relay budget is 300 s (_TOOL_CALL_TIMEOUT_S).  Any relayed tool call
longer than 60 s (long shell, large search, blocking sub-agent, approval wait) is
killed by opencode with "MCP error -32001: Request timed out" even though the
relay is willing to wait.

Root causes (three independent gaps):
  1. ``build_opencode_omnigent_mcp_server`` never writes a ``timeout`` key into
     the generated MCP entry, so opencode uses the SDK default of 60 s.
  2. ``build_opencode_mcp_block`` never propagates ``MCPServerConfig.timeout``
     into the generated entry, so a spec author also cannot configure around it.
  3. The bridge never emits ``notifications/progress``, which is the one mechanism
     that would let opencode's ``resetTimeoutOnProgress`` extend the deadline
     without a static number.

These tests assert each gap so the fix has a concrete fail→pass target.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace as N

import pytest

import omnigent.claude_native_bridge as claude_native_bridge
from omnigent.opencode_native_provider import (
    build_opencode_mcp_block,
    build_opencode_omnigent_mcp_server,
)

# ---------------------------------------------------------------------------
# Facet 1: build_opencode_omnigent_mcp_server must emit a timeout large enough
# to cover the relay's own _TOOL_CALL_TIMEOUT_S budget (300 s).
# ---------------------------------------------------------------------------


def test_build_omnigent_mcp_server_emits_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generated opencode MCP entry for Omnigent must include an explicit
    ``timeout`` so opencode's MCP client does not fall back to the 60 s SDK
    default and kill in-flight relayed tool calls.

    The timeout must be >= _TOOL_CALL_TIMEOUT_S (300 s) so a call that the relay
    is still legitimately running cannot be reported to the model as a timeout.
    """
    monkeypatch.setattr(
        claude_native_bridge,
        "build_mcp_config",
        lambda bridge_dir, *, python_executable=None: {
            "mcpServers": {
                "omnigent": {
                    "command": "/usr/bin/python3",
                    "args": [
                        "-m",
                        "omnigent.claude_native_bridge",
                        "serve-mcp",
                        "--bridge-dir",
                        str(bridge_dir),
                    ],
                    "env": {"PYTHONUNBUFFERED": "1"},
                }
            }
        },
    )

    block = build_opencode_omnigent_mcp_server(Path("/tmp/bridge-mcp-timeout"))
    entry = block["omnigent"]

    # The entry MUST carry an explicit timeout to override opencode's 60 s default.
    assert "timeout" in entry, (
        "build_opencode_omnigent_mcp_server did not emit a 'timeout' key in the MCP "
        "entry; opencode will fall back to its 60 s SDK default and kill any relayed "
        "tool call that runs longer than 60 s"
    )

    # opencode's mcp entry timeout is in MILLISECONDS (it feeds the MCP SDK's
    # per-request deadline, default 60000 ms). It must cover at least the
    # relay's own budget, expressed in ms.
    relay_budget_ms = claude_native_bridge._TOOL_CALL_TIMEOUT_S * 1000  # 300 s
    assert isinstance(entry["timeout"], (int, float)), (
        f"'timeout' must be a number, got {type(entry['timeout']).__name__!r}"
    )
    assert entry["timeout"] >= relay_budget_ms, (
        f"opencode MCP timeout {entry['timeout']} ms < relay budget {relay_budget_ms} ms; "
        "a call the relay is still running can be killed by the client (or, if the "
        "value was written in seconds, opencode reads it as milliseconds and kills "
        "every call almost immediately)"
    )


# ---------------------------------------------------------------------------
# Facet 2: build_opencode_mcp_block must propagate MCPServerConfig.timeout.
# ---------------------------------------------------------------------------


def test_build_mcp_block_propagates_server_timeout() -> None:
    """When an MCPServerConfig carries a ``timeout`` value, it must appear in the
    generated opencode MCP entry so spec authors can configure server-specific
    call budgets.

    Without this, there is no user-side workaround for the 60 s client default
    for declared (non-Omnigent) MCP servers either.
    """
    servers = [
        N(
            name="slow-server",
            transport="stdio",
            command="npx",
            args=["-y", "server-slow"],
            env={},
            url=None,
            headers={},
            databricks_profile=None,
            timeout=300,  # MCPServerConfig.timeout set by spec author
        ),
        N(
            name="fast-server",
            transport="stdio",
            command="npx",
            args=["-y", "server-fast"],
            env={},
            url=None,
            headers={},
            databricks_profile=None,
            timeout=None,  # no override → no timeout key expected
        ),
    ]
    block = build_opencode_mcp_block(servers)

    slow_entry = block["slow-server"]
    assert "timeout" in slow_entry, (
        "build_opencode_mcp_block did not propagate MCPServerConfig.timeout=300 "
        "into the opencode entry; spec authors cannot configure a call budget for "
        "declared MCP servers"
    )
    # MCPServerConfig.timeout is seconds; opencode's entry is milliseconds.
    assert slow_entry["timeout"] == 300_000

    fast_entry = block["fast-server"]
    assert "timeout" not in fast_entry, (
        "build_opencode_mcp_block should NOT emit a timeout key when "
        "MCPServerConfig.timeout is None"
    )


# ---------------------------------------------------------------------------
# Facet 3: _handle_and_write_mcp_request must emit notifications/progress
# so opencode's resetTimeoutOnProgress can extend the deadline while a
# relay call is in flight (belt-and-suspenders fix).
# ---------------------------------------------------------------------------


def test_handle_mcp_request_emits_progress_notifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a relayed tool call is in flight, the bridge must emit at least one
    ``notifications/progress`` JSON-RPC notification so opencode's already-enabled
    ``resetTimeoutOnProgress`` can extend the 60 s client deadline.

    Without progress notifications, the only protection is the static ``timeout``
    key (Facet 1); with them, any SDK-based client gets keep-alive for free
    regardless of the configured ceiling.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    call_started = threading.Event()
    release_call = threading.Event()
    notifications_emitted: list[dict] = []
    responses_emitted: list[dict] = []

    # Slow relay call – holds until released so the progress ticker has a chance to fire.
    def _slow_relay(bridge_dir: Path, name: str, arguments: dict) -> dict:
        call_started.set()
        release_call.wait(timeout=5.0)
        return {"content": [{"type": "text", "text": "done"}]}

    # Capture every JSON-RPC write; split notifications from responses.
    def _capturing_write(payload: dict, lock: threading.Lock, **kwargs: object) -> None:
        if payload.get("method") == "notifications/progress":
            notifications_emitted.append(payload)
        elif "id" in payload:
            responses_emitted.append(payload)

    monkeypatch.setattr(claude_native_bridge, "_call_relay_tool", _slow_relay)
    monkeypatch.setattr(claude_native_bridge, "_write_jsonrpc", _capturing_write)
    monkeypatch.setattr(
        claude_native_bridge,
        "_read_relay_tool_names",
        lambda _path: {"sys_os_shell"},
    )

    stdout_lock = threading.Lock()
    # The stdio loop acquires the semaphore slot before spawning the handler thread;
    # mirror that here so the release() inside the handler doesn't raise ValueError.
    request_slots = threading.BoundedSemaphore(4)
    request_slots.acquire()

    # Simulate an incoming tools/call with a progressToken (as opencode sends).
    request_id = 42
    params = {
        "name": "sys_os_shell",
        "arguments": {"command": "sleep 2 && echo DONE"},
        "_meta": {"progressToken": "tok-keepalive"},
    }

    t = threading.Thread(
        target=claude_native_bridge._handle_and_write_mcp_request,
        args=(
            request_id,
            "tools/call",
            params,
            {},  # tools dict (relay tools handled via _call_relay_tool)
            bridge_dir,
            stdout_lock,
            False,  # framed
            request_slots,
        ),
        daemon=True,
    )
    t.start()

    # Let the relay call start, then give the progress ticker a moment to fire.
    assert call_started.wait(timeout=5.0), "relay call never started"
    time.sleep(0.2)
    release_call.set()
    t.join(timeout=5.0)

    assert len(responses_emitted) == 1, (
        f"expected exactly one tool response, got {responses_emitted!r}"
    )
    assert len(notifications_emitted) >= 1, (
        "No notifications/progress were emitted while a relayed tool call was "
        "in flight; opencode's resetTimeoutOnProgress cannot extend the 60 s "
        "client deadline without them"
    )
    # Each notification must carry the progressToken the client sent, and
    # ``progress`` must strictly increase (MCP requires it; a constant value
    # may be ignored by conforming clients).
    for notif in notifications_emitted:
        assert notif.get("params", {}).get("progressToken") == "tok-keepalive", (
            f"progress notification is missing or has wrong progressToken: {notif!r}"
        )
    progresses = [n["params"]["progress"] for n in notifications_emitted]
    assert progresses == sorted(set(progresses)), (
        f"progress values must strictly increase, got {progresses!r}"
    )


def test_handle_mcp_request_no_heartbeat_for_local_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung LOCAL (non-relay) tool must stay killable by the client's static
    timeout: the bridge must NOT emit keep-alive progress for it, or a wedged
    local tool would reset opencode's deadline forever.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    notifications_emitted: list[dict] = []

    def _capturing_write(payload: dict, lock: threading.Lock, **kwargs: object) -> None:
        if payload.get("method") == "notifications/progress":
            notifications_emitted.append(payload)

    monkeypatch.setattr(claude_native_bridge, "_write_jsonrpc", _capturing_write)
    # No relay advertises this tool — it is a local tool.
    monkeypatch.setattr(claude_native_bridge, "_read_relay_tool_names", lambda _path: set())

    # Slow handler: a wrongly-started heartbeat would emit its immediate first
    # tick during this window, making the assertion below deterministic.
    def _slow_handle(method: str, params: object, tools: dict, bridge_dir: Path) -> dict:
        time.sleep(0.3)
        return {"content": [{"type": "text", "text": "done"}]}

    monkeypatch.setattr(claude_native_bridge, "_handle_mcp_request", _slow_handle)

    request_slots = threading.BoundedSemaphore(4)
    request_slots.acquire()
    claude_native_bridge._handle_and_write_mcp_request(
        7,
        "tools/call",
        {
            "name": "local_tool",
            "arguments": {},
            "_meta": {"progressToken": "tok-local"},
        },
        {},
        bridge_dir,
        threading.Lock(),
        False,
        request_slots,
    )

    assert notifications_emitted == [], (
        "the bridge emitted keep-alive progress for a non-relay tool call; a hung "
        f"local tool could never be timed out by the client: {notifications_emitted!r}"
    )
