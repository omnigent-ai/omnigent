"""Unit tests for :mod:`omnigent.runner.pending_approvals`.

The runner's pending-approvals registry routes policy-ASK verdicts
between two coroutines in the same process: the dispatch path
(which registers a Future and waits on it) and the session-event
handler (which receives the user's verdict and resolves the
Future). Tests here pin the contract directly:

* :func:`register` returns a fresh Future bound to the calling loop.
* :func:`resolve` sets the Future's result and returns whether it
  was delivered (so callers can distinguish "delivered" from
  "no-op").
* :func:`cleanup` is idempotent and pops the entry so unbounded
  growth doesn't accumulate across many ASK round-trips.
* :func:`wait_for_user_approval` is the single sequence every
  ASK-escalation site uses; it must publish
  ``response.elicitation_resolved`` on every exit path so the
  Omnigent server's sidebar index decrements in lockstep.

These invariants drive the cross-session sidebar badge — a
regression in any of them leaves stuck or phantom prompts in the
UI.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import pending_approvals


@pytest.fixture(autouse=True)
def _clean_pending_approvals() -> None:
    """
    Reset the module-global registry between tests.

    The registry is process-global; a leaked Future from one test
    would silently change the behavior of every later test by
    intercepting their resolve() calls.
    """
    pending_approvals.reset_for_tests()
    yield
    pending_approvals.reset_for_tests()


@pytest.mark.asyncio
async def test_register_returns_fresh_future() -> None:
    """``register`` creates a new Future and stores it under the id."""
    fut = pending_approvals.register("elicit_1")
    # Future must be pending immediately — if done(), the caller's
    # subsequent ``wait_for`` would return immediately with a bogus
    # value (likely None) and the user would never see the prompt.
    assert isinstance(fut, asyncio.Future)
    assert not fut.done()


@pytest.mark.asyncio
async def test_resolve_sets_result_and_returns_true() -> None:
    """``resolve`` delivers the verdict and signals delivery."""
    fut = pending_approvals.register("elicit_a")
    delivered = pending_approvals.resolve("elicit_a", True)
    # True = the Future was waiting and got the verdict. A False
    # here would indicate the routing table dropped the entry
    # between register and resolve.
    assert delivered is True
    assert fut.done()
    assert fut.result() is True


@pytest.mark.asyncio
async def test_resolve_unknown_id_returns_false() -> None:
    """Resolving an id with no registered Future is a no-op signal.

    The session-event handler calls resolve on every approval
    event; some of those may arrive after the waiter timed out
    or for ids that were never tracked (e.g. claude-native
    elicitations resolved through a different registry). False
    is the signal "no waiter took this verdict."
    """
    delivered = pending_approvals.resolve("elicit_never_seen", True)
    assert delivered is False


@pytest.mark.asyncio
async def test_resolve_already_done_returns_false() -> None:
    """A second resolve on the same id is rejected.

    Once a Future is resolved (or timed out), the wait coroutine
    has already moved on. Setting the result again would raise
    ``InvalidStateError`` — return False instead so resolve()
    stays idempotent.
    """
    fut = pending_approvals.register("elicit_dup")
    fut.set_result(True)
    second = pending_approvals.resolve("elicit_dup", False)
    assert second is False


@pytest.mark.asyncio
async def test_cleanup_drops_entry() -> None:
    """``cleanup`` removes the entry so future resolves are no-ops."""
    pending_approvals.register("elicit_drop")
    pending_approvals.cleanup("elicit_drop")
    # After cleanup, the id is unknown — resolve must report
    # no-delivery rather than reaching into a stale Future.
    assert pending_approvals.resolve("elicit_drop", True) is False


@pytest.mark.asyncio
async def test_cleanup_unknown_id_is_noop() -> None:
    """Cleanup is idempotent — popping an unknown id does not raise."""
    # No exception is the entire assertion. If this raised,
    # the wait_for_user_approval helper's finally block would
    # fail to clean up on the already-resolved path.
    pending_approvals.cleanup("elicit_never_seen")


@pytest.mark.asyncio
async def test_wait_for_user_approval_returns_true_on_accept() -> None:
    """The wait helper returns True when resolved with approved=True.

    Drives the full register → resolve → cleanup cycle through
    the public surface to catch any wiring regression.
    """
    publishes: list[tuple[str, dict[str, Any]]] = []

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        publishes.append((conv_id, event))

    async def _resolve_after_delay() -> None:
        # Give wait_for_user_approval a tick to register before we
        # resolve, otherwise the resolve would race and find no
        # registered Future.
        await asyncio.sleep(0.01)
        pending_approvals.resolve("elicit_happy", True)

    resolve_task = asyncio.create_task(_resolve_after_delay())
    approved = await pending_approvals.wait_for_user_approval(
        elicitation_id="elicit_happy",
        conversation_id="conv_x",
        publish_event=_publish,
        timeout_seconds=5.0,
    )
    # Drain the helper task so pytest doesn't see a dangling
    # reference after the test exits.
    await resolve_task
    assert approved is True
    # The resolved event MUST fire even on the happy path so the
    # AP-side index decrements once the wait completes (idempotent
    # with the dispatch-time decrement). One publish, with the
    # right event type and id.
    assert len(publishes) == 1
    conv_id, event = publishes[0]
    assert conv_id == "conv_x"
    assert event == {
        "type": "response.elicitation_resolved",
        "elicitation_id": "elicit_happy",
    }


@pytest.mark.asyncio
async def test_wait_for_user_approval_returns_false_on_timeout() -> None:
    """Timeout collapses to False, AND still publishes the resolved event.

    The resolved event on timeout is what clears the sidebar badge
    when the user walked away — without it, the badge stays stuck
    after the runner gives up. This is the runner-side leak the
    PR is fixing.
    """
    publishes: list[tuple[str, dict[str, Any]]] = []

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        publishes.append((conv_id, event))

    approved = await pending_approvals.wait_for_user_approval(
        elicitation_id="elicit_timeout",
        conversation_id="conv_y",
        publish_event=_publish,
        timeout_seconds=0.05,
    )
    # False = the caller treats the prompt as refused. True here
    # would mean the timeout path silently dispatched the tool —
    # a much worse failure mode than a stuck badge.
    assert approved is False
    # Resolved event still fires from the finally block. If
    # len == 0, the timeout path skipped cleanup and the sidebar
    # would show a phantom prompt forever.
    assert len(publishes) == 1
    assert publishes[0][1]["elicitation_id"] == "elicit_timeout"


@pytest.mark.asyncio
async def test_wait_for_user_approval_publishes_on_cancellation() -> None:
    """Cancellation of the wait task still emits the resolved event.

    If the surrounding turn is cancelled mid-wait, the finally
    block must still publish so the AP-side index isn't left
    holding a stale entry. Without this, cancelling a turn with
    an outstanding prompt leaks the badge.
    """
    publishes: list[tuple[str, dict[str, Any]]] = []

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        publishes.append((conv_id, event))

    async def _wait() -> bool:
        return await pending_approvals.wait_for_user_approval(
            elicitation_id="elicit_cancel",
            conversation_id="conv_z",
            publish_event=_publish,
            timeout_seconds=10.0,
        )

    task = asyncio.create_task(_wait())
    # Let the wait register its Future before we cancel.
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Even though the task raised, the resolved event fired from
    # finally. Missing publish here would mean cancelled turns
    # leave permanent stuck badges on their session.
    assert len(publishes) == 1
    assert publishes[0][1]["elicitation_id"] == "elicit_cancel"


# ---------------------------------------------------------------------------
# has_pending — session is "awaiting human approval"
# ---------------------------------------------------------------------------
#
# ``has_pending(conversation_id)`` is read by the runner's message-ingest
# path to decide whether an incoming message may be forwarded as a mid-turn
# injection. While a session is parked on a human approval the forward is
# suppressed so a parent agent's ``sys_session_send`` cannot steer the gated
# turn past the gate. These tests pin the flag's lifecycle across every exit
# path; a leaked or missing flag either wedges every later message to the
# session (stuck "True") or reopens the gate-jump bug (premature "False").


def _noop_publish(_conv_id: str, _event: dict[str, Any]) -> None:
    """Publish sink for waits whose published events aren't under test."""


@pytest.mark.asyncio
async def test_has_pending_false_when_nothing_parked() -> None:
    """A session with no parked approval is not awaiting one.

    If this returned True spuriously, the ingest path would suppress
    mid-turn injection for sessions that have no gate at all — silently
    breaking normal steering.
    """
    assert pending_approvals.has_pending("conv_none") is False


@pytest.mark.asyncio
async def test_has_pending_true_while_parked_then_false_after_accept() -> None:
    """The flag is set for the lifetime of a park and cleared on accept.

    Proves the exact window the ingest guard depends on: True from the
    moment the wait registers until the verdict resolves it, then False.
    A flag that stayed True after resolution would wedge every subsequent
    message to the session.
    """
    wait_task = asyncio.create_task(
        pending_approvals.wait_for_user_approval(
            elicitation_id="elicit_hp1",
            conversation_id="conv_hp1",
            publish_event=_noop_publish,
            timeout_seconds=5.0,
        )
    )
    # Let the wait register + bump the session counter before observing.
    await asyncio.sleep(0.01)
    assert pending_approvals.has_pending("conv_hp1") is True

    pending_approvals.resolve("elicit_hp1", True)
    assert await wait_task is True
    # Cleared on the verdict path — the gate is resolved, so messages may
    # flow (drive a continuation) again.
    assert pending_approvals.has_pending("conv_hp1") is False


@pytest.mark.asyncio
async def test_has_pending_cleared_on_timeout() -> None:
    """A timed-out park clears the flag (the finally decrement fires).

    Without this, a session whose human walked away would reject all
    further messages forever even though the gate has lapsed.
    """
    approved = await pending_approvals.wait_for_user_approval(
        elicitation_id="elicit_hp_timeout",
        conversation_id="conv_hp_timeout",
        publish_event=_noop_publish,
        timeout_seconds=0.05,
    )
    assert approved is False
    assert pending_approvals.has_pending("conv_hp_timeout") is False


@pytest.mark.asyncio
async def test_has_pending_cleared_on_cancellation() -> None:
    """Cancelling the wait task clears the flag via the finally decrement."""
    task = asyncio.create_task(
        pending_approvals.wait_for_user_approval(
            elicitation_id="elicit_hp_cancel",
            conversation_id="conv_hp_cancel",
            publish_event=_noop_publish,
            timeout_seconds=10.0,
        )
    )
    await asyncio.sleep(0.01)
    assert pending_approvals.has_pending("conv_hp_cancel") is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending_approvals.has_pending("conv_hp_cancel") is False


@pytest.mark.asyncio
async def test_has_pending_counts_concurrent_parks() -> None:
    """Two approvals parked on one session: the flag tracks the count.

    Parallel tool calls can each trip a checkpoint, so the session may
    hold more than one parked approval. The flag must stay True until the
    LAST one resolves — a naive boolean (cleared by the first verdict)
    would reopen the gate while a sibling approval is still outstanding.
    """
    t1 = asyncio.create_task(
        pending_approvals.wait_for_user_approval(
            elicitation_id="elicit_multi_1",
            conversation_id="conv_multi",
            publish_event=_noop_publish,
            timeout_seconds=5.0,
        )
    )
    t2 = asyncio.create_task(
        pending_approvals.wait_for_user_approval(
            elicitation_id="elicit_multi_2",
            conversation_id="conv_multi",
            publish_event=_noop_publish,
            timeout_seconds=5.0,
        )
    )
    await asyncio.sleep(0.01)
    assert pending_approvals.has_pending("conv_multi") is True

    # Resolve the first — the session is still awaiting the second.
    pending_approvals.resolve("elicit_multi_1", True)
    assert await t1 is True
    assert pending_approvals.has_pending("conv_multi") is True

    # Resolve the second — now the gate is fully clear.
    pending_approvals.resolve("elicit_multi_2", False)
    assert await t2 is False
    assert pending_approvals.has_pending("conv_multi") is False
