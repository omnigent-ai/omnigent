"""Every TOOL_CALL policy path carries ``data.read_only``.

One test per production site that builds a TOOL_CALL EvaluationContext:
runner gate, server ``function_call`` relay, MCP proxy first call + retry,
and the native-hook ``/policies/evaluate`` builder. Each captures the context
the engine receives and checks the declaration resolved from the spec, not
from the caller's payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runner.policy import RunnerToolPolicyGate
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import (
    _build_evaluation_context,
    _evaluate_tool_call_policy,
    _handle_mcp_tools_call,
    _pending_policy_ask_writes,
    _PendingPolicyAskWrites,
)
from omnigent.spec.types import AgentSpec, LocalToolInfo, MCPServerConfig, Phase, PolicyAction

_SESSION_ID = "conv_ro"

_SPEC = AgentSpec(
    spec_version=1,
    name="ro-agent",
    local_tools=[
        LocalToolInfo(name="reader", path="m.reader", language="python", read_only=True),
        LocalToolInfo(name="writer", path="m.writer", language="python", read_only=False),
    ],
    mcp_servers=[
        MCPServerConfig(name="github", url="http://github", read_only_tools=["get_issue"])
    ],
)


@dataclass
class _CapturingEngine:
    """Engine double that records every context and DENYs to stop the handler."""

    seen: list[EvaluationContext] = field(default_factory=list)

    async def evaluate(self, ctx: EvaluationContext, **_: Any) -> PolicyResult:
        self.seen.append(ctx)
        return PolicyResult(action=PolicyAction.DENY, reason="stop")

    def apply_label_writes(self, set_labels: dict[str, str]) -> None:
        pass

    def apply_state_updates(self, updates: list[Any]) -> None:
        pass


@dataclass
class _Store:
    conv: Conversation

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.conv if session_id == self.conv.id else None


def _conv() -> Conversation:
    return Conversation(
        id=_SESSION_ID,
        created_at=0,
        updated_at=0,
        root_conversation_id=_SESSION_ID,
        agent_id="ag_test",
    )


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _CapturingEngine:
    eng = _CapturingEngine()
    monkeypatch.setattr(sessions_mod, "_load_agent_spec_for_session", lambda conv, store: _SPEC)
    monkeypatch.setattr(sessions_mod, "_build_policy_engine_from_spec", lambda *a, **k: eng)
    return eng


@pytest.mark.asyncio
async def test_runner_gate_path() -> None:
    gate = RunnerToolPolicyGate.from_spec(_SPEC)
    seen: list[EvaluationContext] = []

    async def _capture(ctx: EvaluationContext, phase: Phase) -> Any:
        seen.append(ctx)
        return None

    gate._policies = [object()]  # type: ignore[list-item]  # non-empty → evaluates
    gate._evaluate_policies = _capture  # type: ignore[method-assign]
    await gate.evaluate_tool_call("reader", {})
    assert seen[0].content["read_only"] is True


@dataclass
class _Body:
    type: str
    data: dict[str, Any]


@pytest.mark.asyncio
async def test_function_call_relay_path(engine: _CapturingEngine) -> None:
    body = _Body(type="function_call", data={"name": "writer", "arguments": "{}"})
    await _evaluate_tool_call_policy(
        _SESSION_ID,
        _conv(),
        body,
        _Store(_conv()),
        None,
        None,  # type: ignore[arg-type]
    )
    assert engine.seen[0].content["read_only"] is False


@pytest.mark.asyncio
async def test_mcp_proxy_first_call_path(engine: _CapturingEngine) -> None:
    await _handle_mcp_tools_call(
        rpc_id=1,
        session_id=_SESSION_ID,
        params={"name": "github__get_issue", "arguments": {}},
        conversation_store=_Store(_conv()),  # type: ignore[arg-type]
        agent_store=None,  # type: ignore[arg-type]
        runner_router=None,
    )
    assert engine.seen[0].content["read_only"] is True


@pytest.mark.asyncio
async def test_mcp_proxy_retry_path(engine: _CapturingEngine) -> None:
    # The proxy accepts a retry only for a server-issued elicitation bound to this call.
    _pending_policy_ask_writes["e1"] = _PendingPolicyAskWrites(
        state_updates=None,
        set_labels=None,
        from_mcp=True,
        reviewed_arguments={},
        mcp_call_identity=(_SESSION_ID, "github__get_issue", None, None),
    )
    try:
        await _handle_mcp_tools_call(
            rpc_id=1,
            session_id=_SESSION_ID,
            params={
                "name": "github__get_issue",
                "arguments": {},
                "requestState": json.dumps({"session_id": _SESSION_ID, "elicitation_id": "e1"}),
                "inputResponses": {"e1": {"action": "accept"}},
            },
            conversation_store=_Store(_conv()),  # type: ignore[arg-type]
            agent_store=None,  # type: ignore[arg-type]
            runner_router=None,
        )
    finally:
        _pending_policy_ask_writes.pop("e1", None)
    assert engine.seen[0].content["read_only"] is True


def test_native_hook_path_ignores_forged_wire_value() -> None:
    # A sandboxed harness posts the event; it must not be able to declare
    # its own tool read-only.
    forged = {"name": "undeclared_tool", "arguments": {}, "read_only": True}
    ctx = _build_evaluation_context(Phase.TOOL_CALL, forged, {}, spec=_SPEC)
    assert ctx.content["read_only"] is None
    ctx = _build_evaluation_context(Phase.TOOL_CALL, {"name": "reader"}, {}, spec=_SPEC)
    assert ctx.content["read_only"] is True


# ── sub-agent sessions resolve against the child's own spec ──

_CHILD = AgentSpec(
    spec_version=1,
    name="researcher",
    local_tools=[
        LocalToolInfo(name="lookup_notes", path="m.lookup", language="python", read_only=True)
    ],
)
_ROOT = AgentSpec(spec_version=1, name="root-agent", sub_agents=[_CHILD])


def test_native_hook_sub_agent_resolves_child_declaration() -> None:
    # The policy engine for a child session is built from the ROOT
    # spec; a tool declared only on the sub-agent must still resolve.
    data = {"name": "lookup_notes", "arguments": {}}
    ctx = _build_evaluation_context(
        Phase.TOOL_CALL, data, {}, spec=_ROOT, sub_agent_name="researcher"
    )
    assert ctx.content["read_only"] is True
    ctx = _build_evaluation_context(Phase.TOOL_CALL, data, {}, spec=_ROOT)
    assert ctx.content["read_only"] is None


@pytest.mark.asyncio
async def test_function_call_relay_sub_agent_path(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _CapturingEngine()
    monkeypatch.setattr(sessions_mod, "_load_agent_spec_for_session", lambda conv, store: _ROOT)
    monkeypatch.setattr(sessions_mod, "_build_policy_engine_from_spec", lambda *a, **k: eng)
    conv = _conv()
    conv.sub_agent_name = "researcher"
    body = _Body(type="function_call", data={"name": "lookup_notes", "arguments": "{}"})
    await _evaluate_tool_call_policy(_SESSION_ID, conv, body, _Store(conv), None, None)  # type: ignore[arg-type]
    assert eng.seen[0].content["read_only"] is True
