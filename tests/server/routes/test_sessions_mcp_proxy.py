"""Unit tests for the AP-server MCP proxy error handling in sessions routes."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from omnigent.runner.routing import RoutedRunner
from omnigent.server.routes.sessions import (
    _build_evaluation_context,
    _handle_mcp_tools_list,
    _session_mcp_read_only_hints,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig, Phase


class _RaisingRunnerClient:
    """Runner HTTP client stub whose POST always fails with a leaky error.

    The error text embeds an internal-looking host so the test can prove it
    does NOT survive into the client-facing JSON-RPC response.
    """

    raw_error = "Connection to internal-runner-host:9443 failed"

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        """Raise a transport error carrying sensitive text.

        :returns: Never returns.
        :raises httpx.ConnectError: Always.
        """
        raise httpx.ConnectError(self.raw_error)


class _RaisingRunnerRouter:
    """RunnerRouter stub that hands back a client whose POST raises."""

    def client_for_session_resources(self, conversation_id: str) -> RoutedRunner:
        """Return a routed runner whose client fails on use.

        :param conversation_id: Ignored session id.
        :returns: A :class:`RoutedRunner` wrapping the raising client.
        """
        del conversation_id
        return RoutedRunner(
            runner_id="runner_test",
            client=_RaisingRunnerClient(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_mcp_tools_list_runner_failure_is_genericized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A runner MCP failure returns a fixed message, not the raw exception.

    The ``tools/list`` proxy delegates to the runner's ``/mcp/execute``. When
    that call raises, the JSON-RPC error returned to the caller must carry the
    fixed string ``"Runner MCP execute failed."`` and MUST NOT include the raw
    transport error (which can embed internal hosts). The raw cause must still
    be logged for operators. A failure here means the log-and-genericize
    contract for the AP-server MCP error path regressed.

    :param caplog: Pytest log capture fixture.
    """
    with caplog.at_level(logging.WARNING, logger="omnigent.server.routes.sessions"):
        response = await _handle_mcp_tools_list(
            rpc_id=7,
            session_id="conv_test",
            runner_router=_RaisingRunnerRouter(),  # type: ignore[arg-type]
        )

    payload = json.loads(bytes(response.body))
    # JSON-RPC envelope is preserved (id echoed, application error code).
    assert payload["id"] == 7
    assert payload["error"]["code"] == -32000
    # The client-facing message is the fixed generic string...
    assert payload["error"]["message"] == "Runner MCP execute failed."
    # ...and the raw transport detail (internal host) is absent from it.
    assert _RaisingRunnerClient.raw_error not in json.dumps(payload)
    # ...but IS logged server-side for operators (the other half of the
    # contract — if missing, the failure has no diagnostic record).
    assert _RaisingRunnerClient.raw_error in caplog.text


class _StubRunnerClient:
    """Runner HTTP client stub that returns a canned tools/list result."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json=self._payload,
            request=httpx.Request("POST", "http://runner/v1/sessions/conv_test/mcp/execute"),
        )


class _StubRunnerRouter:
    """RunnerRouter stub that hands back a client returning a canned result."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def client_for_session_resources(self, conversation_id: str) -> RoutedRunner:
        del conversation_id
        return RoutedRunner(
            runner_id="runner_test",
            client=_StubRunnerClient(self._payload),  # type: ignore[arg-type]
        )


def _hint_payload(hints: object) -> dict[str, Any]:
    return {"result": {"schemas": [], "tool_names": [], "read_only_hints": hints}}


def _trusting_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="a",
        mcp_servers=[MCPServerConfig(name="srv", url="http://srv/mcp", trust_annotations=True)],
    )


def _read_only(session_id: str, tool: str, **extra: Any) -> bool | None:
    ctx = _build_evaluation_context(
        Phase.TOOL_CALL,
        {"name": tool, "arguments": {}, **extra},
        {},
        spec=_trusting_spec(),
        session_id=session_id,
    )
    return ctx.content["read_only"]


@pytest.mark.asyncio
async def test_mcp_tools_list_caches_read_only_hints() -> None:
    """Runner hints are cached per session and reach the TOOL_CALL context."""
    sid = "conv_hint_hit"
    try:
        router = _StubRunnerRouter(
            _hint_payload({"srv__get": True, "srv__put": False, "bad": "yes", "n": 1})
        )
        await _handle_mcp_tools_list(rpc_id=1, session_id=sid, runner_router=router)  # type: ignore[arg-type]
        assert _session_mcp_read_only_hints.get(sid) == {"srv__get": True, "srv__put": False}
        assert _read_only(sid, "srv__get") is True
        assert _read_only(sid, "srv__put") is False
        # A hint in the tool-call payload is never honoured.
        assert (
            _read_only(sid, "srv__other", read_only=True, read_only_hints={"srv__other": True})
            is None
        )
    finally:
        _session_mcp_read_only_hints.pop(sid, None)


def test_read_only_hint_cache_miss_is_null() -> None:
    """Without a cache entry the hint resolves to ``null``."""
    assert _read_only("conv_hint_miss", "srv__get") is None


@pytest.mark.asyncio
async def test_mcp_tools_list_hints_are_per_session_and_refreshed() -> None:
    """Sessions (including sub-agents) keep separate entries; relist replaces them."""
    parent, child = "conv_hint_parent", "conv_hint_child"
    try:
        await _handle_mcp_tools_list(
            rpc_id=1,
            session_id=parent,
            runner_router=_StubRunnerRouter(_hint_payload({"srv__get": True})),  # type: ignore[arg-type]
        )
        assert _read_only(child, "srv__get") is None
        await _handle_mcp_tools_list(
            rpc_id=1,
            session_id=parent,
            runner_router=_StubRunnerRouter(_hint_payload({})),  # type: ignore[arg-type]
        )
        assert _session_mcp_read_only_hints.get(parent) is None
    finally:
        _session_mcp_read_only_hints.pop(parent, None)
        _session_mcp_read_only_hints.pop(child, None)
