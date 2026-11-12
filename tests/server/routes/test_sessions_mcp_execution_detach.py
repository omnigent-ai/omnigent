"""Tests for the MCP proxy's runner-execution detach signal."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.entities.conversation import Conversation
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runner.mcp_execution_registry import (
    MCP_ATTACH_ONLY_PARAM,
    MCP_ATTACH_UNAVAILABLE_MESSAGE,
    MCP_OPERATION_ID_PARAM,
    RUNNER_MCP_EXECUTION_DETACHED_CODE,
    RUNNER_MCP_EXECUTION_DETACHED_MESSAGE,
)
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import _handle_mcp_tools_call
from omnigent.spec.types import PolicyAction

_SESSION_ID = "conv_mcp_detach"


class _ConversationStore:
    def get_conversation(self, session_id: str) -> Conversation | None:
        if session_id != _SESSION_ID:
            return None
        return Conversation(
            id=_SESSION_ID,
            created_at=0,
            updated_at=0,
            root_conversation_id=_SESSION_ID,
            agent_id="agent_mcp_detach",
        )


class _AllowPolicyEngine:
    async def evaluate(self, ctx: EvaluationContext) -> PolicyResult:
        del ctx
        return PolicyResult(action=PolicyAction.ALLOW)

    def apply_label_writes(self, set_labels: dict[str, str]) -> None:
        del set_labels


class _DisconnectedRunnerClient:
    async def post(self, *_args: object, **_kwargs: object) -> None:
        raise ConnectionError("tunnel closed before request completed")


class _RecordingRunnerClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        self.bodies.append(json)
        return httpx.Response(
            200, json={"result": {"output": "done"}}, request=httpx.Request("POST", url)
        )


def _route_session_to(monkeypatch: pytest.MonkeyPatch, runner_client: object) -> None:
    engine = _AllowPolicyEngine()
    monkeypatch.setattr(
        sessions_mod,
        "_load_agent_spec_for_session",
        lambda conv, agent_store: object(),
    )
    monkeypatch.setattr(
        sessions_mod,
        "_build_policy_engine_from_spec",
        lambda spec, session_id, conversation_store, conversation=None: engine,
    )

    async def _runner_client(session_id: str, runner_router: Any) -> Any:
        del session_id, runner_router
        return runner_client

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _runner_client)


async def _call_tool(params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    response = await _handle_mcp_tools_call(
        rpc_id=7,
        session_id=_SESSION_ID,
        params={"name": "deployment__deploy", "arguments": {}, **params},
        conversation_store=_ConversationStore(),  # type: ignore[arg-type]
        agent_store=object(),  # type: ignore[arg-type]
        runner_router=object(),  # type: ignore[arg-type]
        **kwargs,
    )
    return json.loads(bytes(response.body))


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_only", [False, True])
async def test_attach_only_marker_reaches_the_runner_operation(
    monkeypatch: pytest.MonkeyPatch, attach_only: bool
) -> None:
    runner = _RecordingRunnerClient()
    _route_session_to(monkeypatch, runner)
    params: dict[str, Any] = {MCP_OPERATION_ID_PARAM: "mcpop_attach"}
    if attach_only:
        params[MCP_ATTACH_ONLY_PARAM] = True

    payload = await _call_tool(params)

    assert "error" not in payload
    expected: dict[str, Any] = {"id": "mcpop_attach", "step": "initial"}
    if attach_only:
        expected["attach"] = True
    assert [body["_omnigent_operation"] for body in runner.bodies] == [expected]
    assert runner.bodies[0]["params"] == {"name": "deployment__deploy", "arguments": {}}


@pytest.mark.asyncio
async def test_attach_only_call_never_runs_a_server_executed_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _RecordingRunnerClient()
    _route_session_to(monkeypatch, runner)
    executed: list[dict[str, Any]] = []

    async def _execute_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        executed.append(arguments)
        return {"result": {"output": "done"}}

    payload = await _call_tool(
        {MCP_OPERATION_ID_PARAM: "mcpop_attach", MCP_ATTACH_ONLY_PARAM: True},
        execute_tool=_execute_tool,
    )

    assert payload["error"] == {"code": -32000, "message": MCP_ATTACH_UNAVAILABLE_MESSAGE}
    assert executed == []
    assert runner.bodies == []


@pytest.mark.asyncio
async def test_tunnel_disconnect_marks_retained_execution_as_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner can distinguish a reattachable disconnect from a tool error."""
    _route_session_to(monkeypatch, _DisconnectedRunnerClient())

    payload = await _call_tool({MCP_OPERATION_ID_PARAM: "mcpop_detach"})

    assert payload["id"] == 7
    assert payload["error"] == {
        "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
        "message": RUNNER_MCP_EXECUTION_DETACHED_MESSAGE,
    }
