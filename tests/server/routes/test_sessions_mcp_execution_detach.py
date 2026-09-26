"""Tests for the MCP proxy's runner-execution detach signal."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runner.mcp_execution_registry import (
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


@pytest.mark.asyncio
async def test_tunnel_disconnect_marks_retained_execution_as_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner can distinguish a reattachable disconnect from a tool error."""
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
        return _DisconnectedRunnerClient()

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _runner_client)

    response = await _handle_mcp_tools_call(
        rpc_id=7,
        session_id=_SESSION_ID,
        params={
            "name": "deployment__deploy",
            "arguments": {},
            MCP_OPERATION_ID_PARAM: "mcpop_detach",
        },
        conversation_store=_ConversationStore(),  # type: ignore[arg-type]
        agent_store=object(),  # type: ignore[arg-type]
        runner_router=object(),  # type: ignore[arg-type]
    )

    payload = json.loads(bytes(response.body))
    assert payload["id"] == 7
    assert payload["error"] == {
        "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
        "message": RUNNER_MCP_EXECUTION_DETACHED_MESSAGE,
    }
