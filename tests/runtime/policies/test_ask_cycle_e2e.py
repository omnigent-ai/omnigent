"""
End-to-end ASK cycle tests — engine + elicitation helper
composed in the same sequence the workflow uses.

Each test runs the canonical cycle:

1. ``engine.evaluate(ctx)`` → ASK result with accumulated
   label writes.
2. Caller hands the result to :func:`_await_elicitation`
   with stub register / emit / park callbacks.
3. Verdict drives labels-apply-or-drop per §7.2.
4. Next ``engine.evaluate(ctx)`` sees the post-elicitation
   state.

This is the complete ASK-cycle contract — wires the engine
and elicitation helper together the same way ``_run_agent_loop``
does in production.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.errors import ElicitationDeclinedError
from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runtime.policies import _await_elicitation
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.runtime.policies.conftest import make_fixed_policy

# ── Harness ────────────────────────────────────────────


class _ElicitationHarness:
    """
    Bundle the register/emit/park seams so tests read cleanly.

    :param verdict: JSON-encoded MCP ``ElicitResult`` body
        (e.g. ``'{"action": "accept"}'``), the literal string
        ``"TIMEOUT"`` to make :meth:`park` raise
        :class:`TimeoutError`, or ``None`` to model a no-row
        wake.
    """

    def __init__(self, verdict: str | None) -> None:
        self._verdict = verdict
        self.registered_elicitation_ids: list[str] = []
        self.registered_params_json: list[str] = []
        self.emitted_events: list[dict[str, Any]] = []

    def register(
        self,
        elicitation_id: str,
        task_id: str,
        params_json: str,
    ) -> None:
        """
        Record the elicitation_id and params_json so the test
        can later correlate verdict routing AND verify the
        persisted params match the emitted event.

        :param elicitation_id: The id assigned by the helper,
            e.g. ``"elicit_abc123"``.
        :param task_id: Parked workflow id (unused).
        :param params_json: JSON-encoded params block — what
            the production registration would persist on the
            ``pending_tool_calls.arguments`` column.
        """
        self.registered_elicitation_ids.append(elicitation_id)
        self.registered_params_json.append(params_json)

    def emit(self, event: dict[str, Any]) -> None:
        """
        Record the SSE event — tests inspect the
        ``response.elicitation_request`` shape for spec parity.

        :param event: The event dict the helper publishes.
        """
        self.emitted_events.append(event)

    async def park(
        self,
        elicitation_id: str,
        timeout_s: int,
    ) -> str | None:
        """
        Return the pre-configured verdict string, or raise
        TimeoutError when verdict is ``"TIMEOUT"``.

        :param elicitation_id: The id passed to :meth:`register`
            (unused; tests assert on the recorded list).
        :param timeout_s: Resolved timeout (unused).
        :returns: The verdict string passed at construction.
        """
        if self._verdict == "TIMEOUT":
            raise TimeoutError(f"no verdict within {timeout_s}s")
        return self._verdict


async def _run_ask_cycle(
    engine: PolicyEngine,
    ctx: EvaluationContext,
    harness: _ElicitationHarness,
) -> tuple[PolicyResult, bool]:
    """
    Drive one full ASK cycle through the engine + elicitation
    helper. Returns the composed result + final approval
    outcome.

    :param engine: The engine under test.
    :param ctx: Evaluation context for the phase.
    :param harness: Stub register/emit/park bundle.
    :returns: Tuple of (composed result, approved bool).
    """
    result = await engine.evaluate(ctx)
    assert result.action == PolicyAction.ASK, (
        f"Harness expects ASK from evaluate(); got {result.action}"
    )
    try:
        approved = await _await_elicitation(
            task_id="task_1",
            root_task_id="task_1",
            result=result,
            phase=ctx.phase,
            content_preview=str(ctx.content),
            policy_engine=engine,
            register=harness.register,
            emit=harness.emit,
            park=harness.park,
        )
    except ElicitationDeclinedError:
        approved = False
    return result, approved


def _ask_policy(
    name: str,
    *,
    phase: Phase = Phase.TOOL_CALL,
    tool_name: str | None = "run_shell",
    condition: dict[str, str | list[str]] | None = None,
    set_labels: dict[str, str] | None = None,
    reason: str = "approval required",
) -> FunctionPolicy:
    """Build an ASKing FunctionPolicy — the typical ASK source."""
    return make_fixed_policy(
        name=name,
        on=[PhaseSelector(phase=phase, tool_name=tool_name)],
        condition=condition,
        action=PolicyAction.ASK,
        reason=reason,
        set_labels=set_labels,
    )


def _build_engine(
    store: SqlAlchemyConversationStore,
    policies: list,
    *,
    initial_labels: dict[str, str] | None = None,
) -> PolicyEngine:
    """Build engine + fresh conversation."""
    conv = store.create_conversation()
    return PolicyEngine(
        policies=policies,
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels=initial_labels or {},
        conversation_store=store,
    )


# ── Happy path: ASK → approve → labels land ──────────


@pytest.mark.asyncio
async def test_ask_cycle_approve_lands_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """End-to-end: engine ASKs with pending label writes;
    caller approves; labels reach the store AND the hot
    cache. Next evaluation sees the new state."""
    policy = _ask_policy(
        "confirm_dangerous",
        set_labels={"approved_once": "true"},
    )
    engine = _build_engine(conversation_store, [policy])
    harness = _ElicitationHarness('{"action": "accept"}')
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {"cmd": "ls"}},
        tool_name="run_shell",
    )

    result, approved = await _run_ask_cycle(engine, ctx, harness)
    assert approved is True
    # Engine-composed result carried the pending writes.
    assert result.set_labels == {"approved_once": "true"}
    # Post-approval hot cache reflects the write.
    assert engine.labels == {"approved_once": "true"}
    # Persisted — next workflow replay sees the same state.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {"approved_once": "true"}


@pytest.mark.asyncio
async def test_ask_cycle_decline_drops_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """ASK → decline → labels DROPPED. Load-bearing §7.2
    invariant: a denied ASK must leave no trace. If this
    regresses, users could effectively approve operations
    by denying them."""
    policy = _ask_policy(
        "confirm_dangerous",
        set_labels={"approved_once": "true"},
    )
    engine = _build_engine(conversation_store, [policy])
    harness = _ElicitationHarness('{"action": "decline"}')
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {"cmd": "ls"}},
        tool_name="run_shell",
    )

    result, approved = await _run_ask_cycle(engine, ctx, harness)
    assert approved is False
    # set_labels returned on the result (caller would know
    # what was SUPPOSED to land), but NOT applied to the store.
    assert result.set_labels == {"approved_once": "true"}
    assert engine.labels == {}
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {}


@pytest.mark.asyncio
async def test_ask_cycle_cancel_drops_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """ASK → cancel → labels DROPPED. Per MCP semantics,
    ``cancel`` is a non-accept verdict — the §7.2 fail-closed
    invariant treats it identically to ``decline``."""
    policy = _ask_policy(
        "confirm_dangerous",
        set_labels={"approved_once": "true"},
    )
    engine = _build_engine(conversation_store, [policy])
    harness = _ElicitationHarness('{"action": "cancel"}')
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {"cmd": "ls"}},
        tool_name="run_shell",
    )

    _, approved = await _run_ask_cycle(engine, ctx, harness)
    assert approved is False
    assert engine.labels == {}


@pytest.mark.asyncio
async def test_ask_cycle_timeout_drops_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """ASK → timeout → labels DROPPED. Timeout path yields
    same side-effect-free outcome as explicit decline."""
    policy = _ask_policy(
        "gate",
        set_labels={"integrity": "0"},
    )
    engine = _build_engine(conversation_store, [policy])
    harness = _ElicitationHarness("TIMEOUT")
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {}},
        tool_name="run_shell",
    )

    _, approved = await _run_ask_cycle(engine, ctx, harness)
    assert approved is False
    assert engine.labels == {}
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {}


# ── Multi-policy ASK composition cycle ────────────────


@pytest.mark.asyncio
async def test_ask_cycle_multiple_askers_combined_approval(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """When multiple policies ASK on the same phase, one
    combined approval resolves them all. On approve, every
    ASKing policy's set_labels lands. Proves §4 ASK
    composition + §7.2 single-approval-per-phase."""
    p1 = _ask_policy("first", set_labels={"a": "1"})
    p2 = _ask_policy("second", set_labels={"b": "2"})
    p3 = _ask_policy("third", set_labels={"c": "3"})
    engine = _build_engine(conversation_store, [p1, p2, p3])
    harness = _ElicitationHarness('{"action": "accept"}')
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {}},
        tool_name="run_shell",
    )

    result, approved = await _run_ask_cycle(engine, ctx, harness)
    assert approved is True
    # deciding_policy is derived from deciding_policies[0].
    assert result.deciding_policy == "first"
    # All three ASKing policies are captured in deciding_policies.
    assert result.deciding_policies == ["first", "second", "third"]
    # Combined reason mentions all three policies.
    assert "first:" in result.reason
    assert "second:" in result.reason
    assert "third:" in result.reason
    # All three policies' set_labels landed — single
    # approval authorized every write.
    assert engine.labels == {"a": "1", "b": "2", "c": "3"}


@pytest.mark.asyncio
async def test_ask_cycle_multiple_askers_combined_decline(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Same multi-policy scenario with a decline. NONE of
    the labels land — all-or-nothing semantics."""
    p1 = _ask_policy("first", set_labels={"a": "1"})
    p2 = _ask_policy("second", set_labels={"b": "2"})
    engine = _build_engine(conversation_store, [p1, p2])
    harness = _ElicitationHarness('{"action": "decline"}')
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {}},
        tool_name="run_shell",
    )

    _, approved = await _run_ask_cycle(engine, ctx, harness)
    assert approved is False
    assert engine.labels == {}


# ── State flows across ASK cycles ─────────────────────


@pytest.mark.asyncio
async def test_approved_labels_visible_in_next_evaluation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """After an approval applies `integrity: 0`, a later
    condition-gated policy can read that state and fire
    accordingly. Demonstrates ASK → label → condition-driven
    downstream behavior — the core IFC loop through ASK."""
    # First policy ASKs, writes integrity=0 on approve.
    taint = _ask_policy(
        "confirm_taint",
        set_labels={"integrity": "0"},
    )
    # Second policy fires UNCONDITIONALLY on run_shell (no
    # tool narrowing on our selector → matches every
    # run_shell invocation) with a condition-gate on
    # integrity=0. Only enforces after taint is established.
    shell_guard = make_fixed_policy(
        name="shell_guard",
        on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name="run_shell")],
        condition={"integrity": "0"},
        action=PolicyAction.DENY,
        reason="tainted; shell disallowed",
    )
    engine = _build_engine(conversation_store, [taint, shell_guard])
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {"cmd": "ls"}},
        tool_name="run_shell",
    )

    # First cycle: ASK then approve.
    harness1 = _ElicitationHarness('{"action": "accept"}')
    _, approved = await _run_ask_cycle(engine, ctx, harness1)
    assert approved is True
    assert engine.labels["integrity"] == "0"

    # Second cycle: same ctx, now shell_guard's condition
    # matches → DENY short-circuits before the ASKing
    # policy fires.
    result2 = await engine.evaluate(ctx)
    assert result2.action == PolicyAction.DENY
    assert result2.deciding_policy == "shell_guard"


@pytest.mark.asyncio
async def test_declined_ask_does_not_poison_next_evaluation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """After a DECLINED ASK, the label state must stay
    clean — a subsequent re-evaluation sees the original
    context and can ASK again (or ALLOW)."""
    policy = _ask_policy("retry_gate", set_labels={"dangerous": "1"})
    engine = _build_engine(conversation_store, [policy])
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {}},
        tool_name="run_shell",
    )

    # First cycle: decline.
    harness1 = _ElicitationHarness('{"action": "decline"}')
    _, approved1 = await _run_ask_cycle(engine, ctx, harness1)
    assert approved1 is False
    # State unchanged.
    assert engine.labels == {}

    # Second cycle: re-evaluation sees the same clean state.
    # The policy ASKs AGAIN (not stuck post-decline).
    harness2 = _ElicitationHarness('{"action": "accept"}')
    _, approved2 = await _run_ask_cycle(engine, ctx, harness2)
    assert approved2 is True
    assert engine.labels == {"dangerous": "1"}


# ── Emitted elicitation event shape verification ──────


def _assert_mcp_elicitation_shape(
    event: dict[str, Any],
    *,
    expected_id: str,
    expected_policy_name: str,
    expected_phase: str,
    expected_reason_fragment: str,
    expected_preview_fragment: str,
) -> None:
    """Assert one emitted event matches the MCP elicitation
    primitive byte-for-byte (``ElicitRequestFormParams`` plus
    the policy-context extras MCP's ``extra="allow"`` permits).
    Drift here breaks every MCP-aware consumer."""
    assert event["type"] == "response.elicitation_request"
    assert event["method"] == "elicitation/create"
    assert event["elicitation_id"] == expected_id
    params = event["params"]
    assert params["mode"] == "form"
    assert params["requestedSchema"] == {}
    assert expected_reason_fragment in params["message"]
    assert params["policy_name"] == expected_policy_name
    assert params["phase"] == expected_phase
    assert expected_preview_fragment in params["content_preview"]


@pytest.mark.asyncio
async def test_emitted_elicitation_request_matches_mcp_shape(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Emitted SSE event matches MCP's elicitation primitive
    byte-for-byte. See ``omnigent/runtime/policies/approval.py``
    ``_elicitation_request_event`` and
    ``designs/SERVER_HARNESS_CONTRACT.md`` §"Universal API
    additions"."""
    policy = _ask_policy(
        "confirm_write",
        phase=Phase.TOOL_CALL,
        tool_name="write_file",
        reason="writes require review",
    )
    engine = _build_engine(conversation_store, [policy])
    harness = _ElicitationHarness('{"action": "accept"}')
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "write_file", "arguments": {"path": "secrets.txt"}},
        tool_name="write_file",
    )

    await _run_ask_cycle(engine, ctx, harness)
    assert len(harness.emitted_events) == 1
    _assert_mcp_elicitation_shape(
        harness.emitted_events[0],
        expected_id=harness.registered_elicitation_ids[0],
        expected_policy_name="confirm_write",
        expected_phase="tool_call",
        expected_reason_fragment="confirm_write: writes require review",
        expected_preview_fragment="write_file",
    )


@pytest.mark.asyncio
async def test_registered_params_json_matches_emitted_params(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The persisted ``arguments`` column on the pending row
    must match the SSE event's ``params`` block. Ensures a
    debugger / replayer inspecting the stored row sees
    exactly what the consumer was shown."""
    policy = _ask_policy("gate", set_labels={"x": "1"})
    engine = _build_engine(conversation_store, [policy])
    harness = _ElicitationHarness('{"action": "accept"}')
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "run_shell", "arguments": {}},
        tool_name="run_shell",
    )

    await _run_ask_cycle(engine, ctx, harness)

    # Exactly one register call per elicitation — if 0, the
    # helper skipped registration (the approval dispatcher would
    # then 404 on the verdict event). If >1, we'd be writing
    # duplicate pending rows for a single ASK, confusing the
    # wake-up routing.
    assert len(harness.registered_params_json) == 1
    persisted_params = json.loads(harness.registered_params_json[0])
    # Persisted params block must be byte-equivalent to what
    # the consumer was shown on the SSE stream — debugger /
    # replayer guarantee.
    assert persisted_params == harness.emitted_events[0]["params"]
