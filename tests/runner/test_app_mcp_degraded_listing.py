"""Per-turn MCP listing bookkeeping around degraded ``tools/list`` results.

The runner lists a session's MCP tools once per MCP config. A listing that
reports failed servers is re-requested on the following turns until it comes
back clean, and a session whose last MCP server was removed syncs once so the
server can clear the failures it retained for the diagnostics surface.

Every turn whose spec declares MCP servers also issues one eager listing for
tool-call routing, so the config-gated setup listing shows up as the extra
``tools/list`` call beyond that one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, MCPServerConfig
from tests.runner.conftest import (
    _FakeMcpManager,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_SESSION_ID = "5f1e6b0d5c8a4c2e9b7a3d1f0e2c4b6a"
_AGENT_ID = "0e36e3219954d2deaef06b8e2a936f38"
_TOOL_NAME = "jira__search_issues"
_FAILURE = "ConnectError: All connection attempts failed"
# tools/list calls per turn: the eager routing listing alone, or plus the
# config-gated setup listing.
_EAGER_ONLY = 1
_EAGER_AND_SETUP = 2


def _spec(*, with_mcp: bool) -> AgentSpec:
    servers = [MCPServerConfig(name="jira", transport="http", url="http://x")] if with_mcp else []
    return AgentSpec(spec_version=1, name="t", mcp_servers=servers)


class _ToolsListServerClient(NullServerClient):
    """Server client stub whose ``tools/list`` reply can flag failed servers."""

    def __init__(self) -> None:
        self.failures: dict[str, str] = {}
        self.tools_list_calls = 0

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        body = kwargs.get("json")
        if not (url.endswith("/mcp") and isinstance(body, dict)):
            return await super().post(url, **kwargs)
        if body.get("method") != "tools/list":
            return await super().post(url, **kwargs)
        self.tools_list_calls += 1
        result: dict[str, Any] = {
            "tools": [
                {
                    "name": _TOOL_NAME,
                    "description": "fake mcp tool",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
        if self.failures:
            result["_meta"] = {"omnigent/mcpFailures": dict(self.failures)}

        class _Response(NullServerClient._Response):
            def json(self) -> dict[str, Any]:
                return {"result": result}

        return _Response()


def _build_app(
    specs: dict[str, AgentSpec],
) -> tuple[FastAPI, _ScriptedHarnessClient, _ToolsListServerClient, asyncio.Event]:
    """Wire a runner app whose spec resolver reads ``specs["current"]``."""
    finished = asyncio.Event()
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ],
        stream_finished=finished,
    )
    server_client = _ToolsListServerClient()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return specs["current"]

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        mcp_manager=_FakeMcpManager(tool_name=_TOOL_NAME),
    )
    return app, harness_client, server_client, finished


async def _run_turn(
    client: httpx.AsyncClient,
    finished: asyncio.Event,
    server_client: _ToolsListServerClient,
) -> int:
    """Post one user turn, wait for its stream, and return its tools/list count."""
    before = server_client.tools_list_calls
    finished.clear()
    resp = await client.post(
        f"/v1/sessions/{_SESSION_ID}/events",
        json={
            "type": "message",
            "role": "user",
            "agent_id": _AGENT_ID,
            "model": "test-agent",
            "input": [{"type": "input_text", "text": "hi"}],
            "harness": "openai-agents",
            "has_mcp_servers": True,
        },
    )
    assert resp.status_code == 202, resp.text
    await asyncio.wait_for(finished.wait(), timeout=5.0)
    # Stream-end bookkeeping runs after the last frame; let it settle.
    await asyncio.sleep(0.05)
    return server_client.tools_list_calls - before


def _advertised_tools(body: dict[str, Any]) -> set[str]:
    tools = body.get("tools") or []
    return {t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)}


@pytest.mark.asyncio
async def test_clean_listing_is_cached_for_an_unchanged_config() -> None:
    app, harness_client, server_client, finished = _build_app({"current": _spec(with_mcp=True)})
    async with _runner_client(app) as client:
        assert await _run_turn(client, finished, server_client) == _EAGER_AND_SETUP
        assert await _run_turn(client, finished, server_client) == _EAGER_ONLY

    assert all(_TOOL_NAME in _advertised_tools(b) for b in harness_client.posted_bodies)


@pytest.mark.asyncio
async def test_degraded_listing_is_retried_each_turn_until_the_servers_recover() -> None:
    app, harness_client, server_client, finished = _build_app({"current": _spec(with_mcp=True)})
    server_client.failures = {"jira": _FAILURE}
    async with _runner_client(app) as client:
        assert await _run_turn(client, finished, server_client) == _EAGER_AND_SETUP
        assert await _run_turn(client, finished, server_client) == _EAGER_AND_SETUP, (
            "a degraded listing must be re-requested on the next turn"
        )

        server_client.failures = {}
        assert await _run_turn(client, finished, server_client) == _EAGER_AND_SETUP, (
            "the recovery listing must be requested"
        )
        assert await _run_turn(client, finished, server_client) == _EAGER_ONLY, (
            "a clean listing for an unchanged config is cached again"
        )

    # Healthy tools stay advertised while a sibling server is failing.
    assert all(_TOOL_NAME in _advertised_tools(b) for b in harness_client.posted_bodies)


@pytest.mark.asyncio
async def test_removing_the_last_mcp_server_syncs_once_and_drops_its_tools() -> None:
    specs = {"current": _spec(with_mcp=True)}
    app, harness_client, server_client, finished = _build_app(specs)
    server_client.failures = {"jira": _FAILURE}
    async with _runner_client(app) as client:
        assert await _run_turn(client, finished, server_client) == _EAGER_AND_SETUP
        assert _TOOL_NAME in _advertised_tools(harness_client.posted_bodies[-1])

        # Re-seeding the session re-resolves the (now server-less) spec.
        specs["current"] = _spec(with_mcp=False)
        seed = await client.post(
            "/v1/sessions", json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID}
        )
        assert seed.status_code == 201, seed.text

        # No servers means no eager listing; the one call is the empty-config sync.
        assert await _run_turn(client, finished, server_client) == 1, (
            "an emptied config must sync once"
        )
        assert _TOOL_NAME not in _advertised_tools(harness_client.posted_bodies[-1])

        assert await _run_turn(client, finished, server_client) == 0, (
            "the empty config is not re-synced"
        )
