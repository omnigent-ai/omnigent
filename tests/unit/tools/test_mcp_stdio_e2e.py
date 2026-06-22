"""End-to-end test for the stdio MCP transport on the runner.

Spawns a real FastMCP subprocess (``tests/tools/fixtures/echo_stdio_mcp_server.py``)
through :class:`omnigent.runner.mcp_manager.RunnerMcpManager`, discovers
its tool via real MCP stdio, and invokes the tool over the live subprocess.

Post designs/RUNNER_MCP.md the runner owns MCP lifecycle, so this is
the runner-side version of what used to be a ToolManager test.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.spec.types import AgentSpec, MCPServerConfig

_ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_stdio_mcp_server.py")
_ENV_PROBE_SERVER = str(Path(__file__).parent / "fixtures" / "env_probe_stdio_mcp_server.py")


@pytest.fixture()
def echo_mcp_spec() -> AgentSpec:
    """Spec declaring the echo-test FastMCP server as a stdio MCP."""
    mcp = MCPServerConfig(
        name="echo-test",
        transport="stdio",
        command=sys.executable,
        args=[_ECHO_SERVER],
    )
    return AgentSpec(spec_version=1, mcp_servers=[mcp])


@pytest.mark.asyncio
async def test_stdio_mcp_discovers_tool_via_real_subprocess(
    echo_mcp_spec: AgentSpec,
) -> None:
    """``schemas_for`` spawns the subprocess and surfaces the echo tool schema.

    Verifies the full stdio path: ``MCPServerConfig`` field shape,
    ``McpServerConnection._open_stdio_transport`` (subprocess spawn +
    stdio_client), and MCP ``tools/list`` discovery.
    """
    manager = RunnerMcpManager()
    try:
        result = await manager.schemas_for(echo_mcp_spec)
        # Tool names are namespaced as {server}__{tool}; the echo-test
        # server exposes "echo", so the full name is "echo-test__echo".
        assert "echo-test__echo" in result.tool_names, (
            f"Expected 'echo-test__echo' in tool_names; got {result.tool_names!r} "
            f"(failures={result.failures!r})"
        )
        echo_schema = next(s for s in result.schemas if s["name"] == "echo-test__echo")
        properties = echo_schema["parameters"].get("properties", {})
        # ``text`` parameter from the FastMCP decorator must round-trip.
        assert "text" in properties, (
            f"Expected 'text' param in echo schema; got {properties.keys()}"
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stdio_mcp_call_tool_round_trips_through_subprocess(
    echo_mcp_spec: AgentSpec,
) -> None:
    """``call_tool`` round-trips through the live subprocess.

    The server prefixes input with ``"echo: "`` so a bare passthrough
    of the request payload wouldn't match — the response must really
    flow through the MCP server body and back.
    """
    manager = RunnerMcpManager()
    try:
        await manager.schemas_for(echo_mcp_spec)
        result = await manager.call_tool(echo_mcp_spec, "echo-test__echo", {"text": "ping"})
        # Prefix match proves the subprocess executed the tool body.
        assert result == "echo: ping", (
            f"Expected 'echo: ping'; got {result!r}. If empty or "
            f"missing the prefix, the stdio MCP handshake is broken."
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stdio_mcp_shutdown_does_not_log_cancel_scope_error(
    echo_mcp_spec: AgentSpec,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``shutdown()`` closes stdio MCP servers cleanly (no anyio cancel-scope error).

    Regression: ``McpServerConnection`` runs a long-lived lifecycle
    task that owns the ``AsyncExitStack`` so ``connect()`` and
    ``close()`` run on the same task. Without that, anyio raises
    "Attempted to exit cancel scope in a different task than it was
    entered in" during shutdown.
    """
    caplog.set_level(logging.ERROR, logger="omnigent.runner.mcp_manager")
    manager = RunnerMcpManager()
    result = await manager.schemas_for(echo_mcp_spec)
    # Sanity: connect actually succeeded, so there is something to close.
    assert "echo-test__echo" in result.tool_names
    await manager.shutdown()
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], (
        f"shutdown logged {len(error_records)} error(s); expected clean "
        f"close. Messages: {[r.getMessage() for r in error_records]!r}"
    )


@pytest.mark.asyncio
async def test_stdio_mcp_subprocess_never_sees_runner_binding_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner-auth secret is stripped from the spawned MCP server env.

    A stdio MCP server command is spec-author-provided code.
    When ``config.env`` is non-empty the runner overlays it on
    ``os.environ`` to spawn the subprocess — the exact branch that
    leaked the runner tunnel binding token. This drives the real
    subprocess: the probe tool reports the token as ``"<unset>"`` (it
    was stripped) while the benign overlay var survives (the env was
    still forwarded, just minus the secret).

    :param monkeypatch: Seeds the binding token into the runner process
        ``os.environ`` so the merge branch would otherwise inherit it.
    """
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "bug-binding-token-secret")
    # Non-empty ``config.env`` forces the ``dict(os.environ) | config.env``
    # merge branch — the vulnerable path. The overlay marker proves the
    # subprocess still receives forwarded env (so the strip is targeted,
    # not a blanket wipe).
    spec = AgentSpec(
        spec_version=1,
        mcp_servers=[
            MCPServerConfig(
                name="env-probe",
                transport="stdio",
                command=sys.executable,
                args=[_ENV_PROBE_SERVER],
                env={"MCP_OVERLAY_MARKER": "overlay-value"},
            )
        ],
    )
    manager = RunnerMcpManager()
    try:
        await manager.schemas_for(spec)
        token_view = await manager.call_tool(
            spec, "env-probe__read_env", {"name": RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR}
        )
        overlay_view = await manager.call_tool(
            spec, "env-probe__read_env", {"name": "MCP_OVERLAY_MARKER"}
        )
        # Token absent from the subprocess env. "set:..." here would mean
        # the strip was skipped and the agent payload could impersonate
        # the runner against the control-plane tunnel.
        assert token_view == "<unset>", (
            f"binding token leaked into the MCP subprocess env: {token_view!r}"
        )
        # Overlay survived — proves the env was forwarded, so the
        # assertion above is about the secret specifically, not an
        # empty environment that would pass vacuously.
        assert overlay_view == "set:overlay-value", (
            f"config.env overlay did not reach the MCP subprocess: {overlay_view!r}"
        )
    finally:
        await manager.shutdown()
