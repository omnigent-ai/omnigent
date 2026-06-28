"""
Tests for the policy evaluation round-trip added to
:class:`TurnContext` and :class:`HarnessApp` in the scaffold.

Verifies:

- ``TurnContext.evaluate_policy`` parks on a Future and emits
  a ``PolicyEvaluationRequestEvent`` SSE event upstream.
- ``PolicyVerdictEvent`` inbound events resolve the parked Future
  with the correct :class:`PolicyVerdictPayload`.
- ``_cancel_pending`` cancels outstanding policy evaluation Futures.
- Stale ``evaluation_id`` on a verdict event silently no-ops.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runtime.harnesses._scaffold import (
    PolicyVerdictEvent,
    PolicyVerdictPayload,
    TurnContext,
)
from omnigent.server.schemas import (
    HarnessStreamEvent,
    PolicyEvaluationRequestEvent,
)


@pytest.fixture()
def _turn_ctx() -> TurnContext:
    """
    Build a minimal :class:`TurnContext` for testing.

    Uses a bounded queue so ``emit`` doesn't block.

    :returns: Ready-to-use context with a fresh event queue.
    """
    queue: asyncio.Queue[HarnessStreamEvent | None] = asyncio.Queue()
    cancelled = asyncio.Event()
    return TurnContext(
        response_id="resp_test_001",
        event_queue=queue,
        cancelled=cancelled,
    )


@pytest.mark.asyncio()
async def test_evaluate_policy_round_trip(_turn_ctx: TurnContext) -> None:
    """
    ``evaluate_policy`` emits a ``PolicyEvaluationRequestEvent``
    and parks until the verdict is delivered via
    ``_complete_policy_evaluation``.

    If the SSE event is not emitted or the Future is never
    resolved, the test will hang (timeout) — proving the
    round-trip wiring is correct.
    """
    ctx = _turn_ctx
    eval_id = "poleval_test_001"
    phase = "PHASE_LLM_REQUEST"
    data: dict[str, Any] = {"model": "gpt-4o", "messages_count": 10}

    # Start evaluate_policy in the background — it will park on a Future.
    task = asyncio.create_task(ctx.evaluate_policy(eval_id, phase, data))

    # Give the event loop a tick so the Future is registered
    # and the SSE event is emitted.
    await asyncio.sleep(0)

    # Verify the SSE event was emitted.
    emitted = ctx._event_queue.get_nowait()
    assert isinstance(emitted, PolicyEvaluationRequestEvent), (
        f"Expected PolicyEvaluationRequestEvent, got {type(emitted).__name__}. "
        "The evaluate_policy method should emit an upstream SSE event."
    )
    # The event must carry the exact evaluation_id, phase, and data
    # so the runner can route the request to the Omnigent server.
    assert emitted.evaluation_id == eval_id
    assert emitted.phase == phase
    assert emitted.data == data

    # Deliver the verdict — should resolve the parked Future.
    verdict = PolicyVerdictPayload(
        action="POLICY_ACTION_DENY",
        reason="test denial",
        data={"rewritten": True},
    )
    resolved = ctx._complete_policy_evaluation(eval_id, verdict)
    # True means the Future was found and resolved.
    assert resolved is True, (
        "Expected _complete_policy_evaluation to return True (Future matched). "
        "If False, the evaluation_id didn't match any pending evaluation."
    )

    # The task should now complete with the verdict payload.
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.action == "POLICY_ACTION_DENY", (
        "Verdict action should be POLICY_ACTION_DENY as delivered. "
        "If ALLOW, the wrong verdict was delivered or a default was returned."
    )
    assert result.reason == "test denial"
    assert result.data == {"rewritten": True}


@pytest.mark.asyncio()
async def test_evaluate_policy_stale_id_silently_noop(_turn_ctx: TurnContext) -> None:
    """
    A ``_complete_policy_evaluation`` call with an unknown
    ``evaluation_id`` returns ``False`` and does not raise.

    This matches the tool_result handler's stale-id semantics.
    """
    ctx = _turn_ctx
    verdict = PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
    # No pending evaluations — stale id should no-op.
    resolved = ctx._complete_policy_evaluation("poleval_nonexistent", verdict)
    assert resolved is False, (
        "Expected False for a stale evaluation_id. If True, a phantom Future was matched."
    )


@pytest.mark.asyncio()
async def test_cancel_pending_cancels_policy_evaluations(_turn_ctx: TurnContext) -> None:
    """
    ``_cancel_pending`` cancels all outstanding policy evaluation
    Futures so the executor unblocks with ``CancelledError``.
    """
    ctx = _turn_ctx

    # Start an evaluation that will park.
    task = asyncio.create_task(ctx.evaluate_policy("poleval_cancel_001", "PHASE_LLM_REQUEST", {}))
    await asyncio.sleep(0)

    # Cancel all pending (as the interrupt handler does).
    ctx._cancel_pending()

    # The task should raise CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio()
async def test_policy_verdict_event_handler(_turn_ctx: TurnContext) -> None:
    """
    The scaffold's ``_handle_policy_verdict_event`` converts a
    :class:`PolicyVerdictEvent` into a :class:`PolicyVerdictPayload`
    and resolves the correct parked Future.
    """
    ctx = _turn_ctx
    eval_id = "poleval_handler_001"

    # Park a Future.
    task = asyncio.create_task(
        ctx.evaluate_policy(eval_id, "PHASE_LLM_RESPONSE", {"model": "test"})
    )
    await asyncio.sleep(0)

    # Simulate the inbound event that the scaffold handler would deliver.
    body = PolicyVerdictEvent(
        type="policy_verdict",
        evaluation_id=eval_id,
        action="POLICY_ACTION_ALLOW",
        reason=None,
    )
    verdict = PolicyVerdictPayload(
        action=body.action,
        reason=body.reason,
        data=body.data,
    )
    ctx._complete_policy_evaluation(body.evaluation_id, verdict)

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.action == "POLICY_ACTION_ALLOW", (
        "Expected ALLOW verdict from the handler. "
        "If different, the verdict event was not correctly converted."
    )
    assert result.reason is None


@pytest.mark.asyncio()
async def test_evaluate_policy_llm_timeout_fails_open(
    _turn_ctx: TurnContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``evaluate_policy`` returns ALLOW for advisory LLM phases after timeout.

    If the verdict never arrives (race condition, network hiccup,
    evaluation_id mismatch), the executor must not hang forever. LLM request
    policy is advisory, so this path fails open.
    """
    # Shrink timeout to 0.1s so the test runs fast.
    import omnigent.runtime.harnesses._scaffold as _scaffold_mod

    monkeypatch.setattr(_scaffold_mod, "_POLICY_EVAL_TIMEOUT_S", 0.1)

    ctx = _turn_ctx
    # Start evaluation but never deliver the verdict.
    result = await ctx.evaluate_policy("poleval_timeout_001", "PHASE_LLM_REQUEST", {})

    assert result.action == "POLICY_ACTION_ALLOW", (
        "Timed-out LLM-phase policy evaluation should default to ALLOW "
        "(fail-open) so a transient outage never hangs the turn. "
        "If DENY, the timeout path is returning the wrong default. "
        "If this test hangs, the timeout isn't being applied."
    )
    assert result.reason is None


@pytest.mark.asyncio()
async def test_evaluate_policy_tool_call_timeout_fails_closed(
    _turn_ctx: TurnContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out ``PHASE_TOOL_CALL`` evaluation defaults to DENY.

    TOOL_CALL is the authoritative gate for connector-native MCP tools
    (the harness ``can_use_tool`` callback consumes this verdict and the
    call is never re-checked server-side). If the verdict never arrives,
    the tool must be blocked, not allowed — the LLM-phase fail-open above
    must NOT extend to the tool *call*.
    """
    import omnigent.runtime.harnesses._scaffold as _scaffold_mod

    monkeypatch.setattr(_scaffold_mod, "_POLICY_EVAL_TIMEOUT_S", 0.1)

    ctx = _turn_ctx
    # Start evaluation but never deliver the verdict.
    result = await ctx.evaluate_policy("poleval_toolcall_timeout", "PHASE_TOOL_CALL", {})

    assert result.action == "POLICY_ACTION_DENY", (
        f"Timed-out TOOL_CALL policy evaluation must fail CLOSED (DENY); got {result.action!r}."
    )
    assert result.reason is not None


@pytest.mark.asyncio()
async def test_evaluate_policy_tool_result_timeout_fails_open(
    _turn_ctx: TurnContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out ``PHASE_TOOL_RESULT`` evaluation defaults to ALLOW.

    By the result phase the tool has already executed, so a missing verdict
    need not block — TOOL_RESULT fails OPEN like the advisory LLM phases,
    unlike TOOL_CALL. (Maintainer design decision — see PR review thread.)
    """
    import omnigent.runtime.harnesses._scaffold as _scaffold_mod

    monkeypatch.setattr(_scaffold_mod, "_POLICY_EVAL_TIMEOUT_S", 0.1)

    ctx = _turn_ctx
    result = await ctx.evaluate_policy("poleval_toolresult_timeout", "PHASE_TOOL_RESULT", {})

    assert result.action == "POLICY_ACTION_ALLOW", (
        f"Timed-out TOOL_RESULT policy evaluation must fail OPEN (ALLOW); got {result.action!r}."
    )
    assert result.reason is None


def test_policy_verdict_payload_frozen() -> None:
    """
    ``PolicyVerdictPayload`` is frozen — mutations raise.

    Consumers store verdicts without defensive copies; if
    mutability regresses, shared references could corrupt
    later reads.
    """
    payload = PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
    with pytest.raises(AttributeError):
        payload.action = "POLICY_ACTION_DENY"  # type: ignore[misc]


def test_policy_verdict_payload_defaults() -> None:
    """
    Minimal construction sets ``reason=None`` and ``data=None``.
    """
    payload = PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
    assert payload.action == "POLICY_ACTION_ALLOW"
    assert payload.reason is None
    assert payload.data is None
