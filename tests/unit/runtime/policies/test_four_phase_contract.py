"""
Four-phase enforcement contract tests (Phase 5 contract).

Demonstrates exactly how the workflow should call
:func:`_enforce_policy` at each of the four enforcement
sites (POLICIES.md §5.1 - §5.4). Each test builds the
EvaluationContext the workflow will build, runs
`_enforce_policy`, and verifies the engine's response
shape matches what the workflow branches on.

These tests are the **contract the workflow wiring (Phase 6)
must honor** — if the workflow builds contexts matching what
these tests pass, runtime behavior will match these
assertions.

Covers:
- INPUT phase: user message content (str)
- TOOL_CALL phase: function_call dict with tool_name
- TOOL_RESULT phase: function_call_output dict with tool_name
- OUTPUT phase: assistant response text (str)

For each phase, tests verify:
- ALLOW path: no blocking, labels land
- DENY path: sentinel result with reason
- ASK path: accumulated labels withheld (caller approves
  via _await_elicitation)
"""

from __future__ import annotations

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import _enforce_policy
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.runtime.policies.conftest import make_fixed_policy

# ── INPUT phase ────────────────────────────────────────


def _input_ctx(text: str) -> EvaluationContext:
    """Build the context the workflow would assemble from
    a user message's text content."""
    return EvaluationContext(
        phase=Phase.REQUEST,
        content=text,
        tool_name=None,
    )


@pytest.mark.asyncio
async def test_input_phase_allow(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """No policies on INPUT → engine returns ALLOW."""
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    result = await _enforce_policy(engine, _input_ctx("hello"))
    assert result.action == PolicyAction.ALLOW
    assert result.reason is None


@pytest.mark.asyncio
async def test_input_phase_deny(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A fixed policy on INPUT with DENY action fires on any
    INPUT evaluation; workflow's INPUT site would produce a
    sentinel message in response."""
    policy = make_fixed_policy(
        name="block_input",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.DENY,
        reason="prohibited content",
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    result = await _enforce_policy(engine, _input_ctx("bad"))
    assert result.action == PolicyAction.DENY
    assert result.reason == "prohibited content"
    # Workflow reads deciding_policy for observability.
    assert result.deciding_policy == "block_input"


# ── TOOL_CALL phase ───────────────────────────────────


def _tool_call_ctx(name: str, args: dict) -> EvaluationContext:
    """Build the context the workflow would assemble inside
    `_call_tool` before dispatch."""
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": name, "arguments": args},
        tool_name=name,
    )


@pytest.mark.asyncio
async def test_tool_call_allow(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A tool_call on a tool with no matching policy ALLOWs."""
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    r = await _enforce_policy(engine, _tool_call_ctx("web_search", {"q": "x"}))
    assert r.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_tool_call_deny_by_tool_name(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Tool-narrowed policy DENYs only its specific tool —
    others pass freely."""
    policy = make_fixed_policy(
        name="no_shell",
        on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name="run_shell")],
        action=PolicyAction.DENY,
        reason="shell disallowed",
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    # run_shell → DENY.
    r1 = await _enforce_policy(
        engine,
        _tool_call_ctx("run_shell", {"cmd": "ls"}),
    )
    assert r1.action == PolicyAction.DENY
    # Different tool passes.
    r2 = await _enforce_policy(
        engine,
        _tool_call_ctx("web_search", {"q": "x"}),
    )
    assert r2.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_tool_call_ask_withholds_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """ASK at tool_call → caller parks for approval; the
    set_labels on the result are NOT yet applied (§7.2)."""
    policy = make_fixed_policy(
        name="confirm_shell",
        on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name="run_shell")],
        action=PolicyAction.ASK,
        reason="please confirm",
        set_labels={"shell_approved": "maybe"},
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    r = await _enforce_policy(engine, _tool_call_ctx("run_shell", {"cmd": "ls"}))
    assert r.action == PolicyAction.ASK
    # Result carries pending writes for the caller to apply
    # on approve.
    assert r.set_labels == {"shell_approved": "maybe"}
    # But the engine did NOT apply them — hot cache is empty.
    assert engine.labels == {}


# ── TOOL_RESULT phase ─────────────────────────────────


def _tool_result_ctx(name: str, output: str) -> EvaluationContext:
    """Build the context the workflow assembles from a
    function_call_output item after tool dispatch.

    ``content`` is the raw tool output string, mirroring the
    workflow's TOOL_RESULT contract — symmetric with INPUT /
    OUTPUT phases.
    """
    return EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content=output,
        tool_name=name,
    )


@pytest.mark.asyncio
async def test_tool_result_allow_with_label_write(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A fixed policy tainting integrity on tool_result —
    workflow would see ALLOW with accumulated writes, and
    the writes persist."""
    policy = make_fixed_policy(
        name="taint_on_web",
        on=[PhaseSelector(phase=Phase.TOOL_RESULT, tool_name="web_search")],
        action=PolicyAction.ALLOW,
        set_labels={"integrity": "0"},
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={"integrity": "1"},
        conversation_store=conversation_store,
    )
    r = await _enforce_policy(
        engine,
        _tool_result_ctx("web_search", "results..."),
    )
    assert r.action == PolicyAction.ALLOW
    # Labels landed on ALLOW (no ASK path in this test).
    assert engine.labels["integrity"] == "0"


# ── OUTPUT phase ──────────────────────────────────────


def _output_ctx(text: str) -> EvaluationContext:
    """Build the context the workflow assembles from the
    LLM's final assistant response text."""
    return EvaluationContext(
        phase=Phase.RESPONSE,
        content=text,
        tool_name=None,
    )


@pytest.mark.asyncio
async def test_output_phase_allow(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """No OUTPUT policies → response passes through."""
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    r = await _enforce_policy(engine, _output_ctx("The answer is 42."))
    assert r.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_output_phase_deny(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """OUTPUT DENY → workflow must replace the response
    with a sentinel before persistence. The pre-persistence
    ordering is load-bearing (POLICIES.md §11.4) — a DENY
    at OUTPUT must mean the raw content never hits the
    store."""
    policy = make_fixed_policy(
        name="redact_output",
        on=[PhaseSelector(phase=Phase.RESPONSE)],
        action=PolicyAction.DENY,
        reason="sensitive content in response",
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    r = await _enforce_policy(engine, _output_ctx("confidential details"))
    assert r.action == PolicyAction.DENY
    # Workflow uses the reason in its sentinel.
    assert r.reason == "sensitive content in response"


# ── Cross-phase: one policy, multiple phases ──────────


@pytest.mark.asyncio
async def test_policy_fires_on_multiple_phases(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A policy with multiple PhaseSelectors fires on each
    matching phase. Workflow treats each enforcement site
    independently — one engine.evaluate per phase — so the
    contract is that the selector match is per-call."""
    policy = make_fixed_policy(
        name="log_input_and_output",
        on=[
            PhaseSelector(phase=Phase.REQUEST),
            PhaseSelector(phase=Phase.RESPONSE),
        ],
        action=PolicyAction.ALLOW,
        set_labels={"observed": "true"},
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    # Fires on INPUT.
    await _enforce_policy(engine, _input_ctx("user message"))
    assert engine.labels["observed"] == "true"

    # Clear and fire again on OUTPUT — same write lands.
    engine._labels.clear()
    conversation_store.set_labels(conv.id, {"observed": "false"})
    await _enforce_policy(engine, _output_ctx("response"))
    assert engine.labels["observed"] == "true"

    # Does NOT fire on TOOL_CALL (not in selector list).
    engine._labels.clear()
    conversation_store.set_labels(conv.id, {"observed": "false"})
    await _enforce_policy(
        engine,
        _tool_call_ctx("web_search", {}),
    )
    # No write — policy didn't match TOOL_CALL.
    assert engine.labels == {}
