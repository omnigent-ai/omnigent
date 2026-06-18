"""Runner-side registry of asyncio Futures awaiting policy ASK verdicts.

When the runner needs the user to approve a gated tool call, it
mints an ``elicitation_id``, parks a Future here, and waits for the
AP server's approval-event POST to arrive on
``/v1/sessions/{id}/events``. The runner's session-event handler
resolves the Future on receipt.

Lifted out of ``omnigent.runner.app`` so the gate that lives in
``omnigent.runner.tool_dispatch`` can register and wait without
threading the dict through every dispatch entry point. The dict is
process-global; elicitation ids are UUIDs so there's no collision
concern, and an in-flight Future's scope is naturally one approval
round-trip.

Lifecycle contract:

* :func:`register` creates a Future and inserts it. Returns the
  Future so the caller can await it (typically wrapped in
  :func:`asyncio.wait_for`).
* The caller MUST call :func:`cleanup` in a ``finally`` block.
  The registry has no GC of its own; leaked entries accumulate.
* :func:`resolve` is called by the session-event handler when an
  ``approval`` event arrives. Idempotent and no-op when the id is
  unknown or the Future is already done.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

# Default wait budget for a UI verdict, in seconds. Held at one day
# (86400s) — matching the deciding policy's default ``ask_timeout``: an ASK
# is a human-in-the-loop gate and should outlive a user stepping away rather
# than auto-refuse on its own. The old 120s default silently refused (treated
# as DENY) any prompt a user didn't answer within two minutes — the
# runner-side mirror of the cost-policy auto-resolve bug. Callers that resolve
# a per-policy ``ask_timeout`` should still pass ``timeout_seconds`` explicitly;
# this is only the fallback when none is provided. Headless/unattended agents
# that want a fast fail-closed should pass a finite ``timeout_seconds``.
_DEFAULT_WAIT_SECONDS: float = 86400.0

# Module-global registry: elicitation_id → asyncio.Future[bool].
# True = approved, False = declined/timed-out. Future is owned by the
# caller that registered it — this module is just the routing table
# the session-event handler reads to set the result.
_pending: dict[str, asyncio.Future[bool]] = {}

# Per-session count of outstanding ASK verdicts (a session may have more
# than one parked at once — e.g. parallel tool calls that each tripped a
# checkpoint). Maintained by :func:`wait_for_user_approval` around its
# park, since that's the single entry point that knows the conversation
# id. Read by :func:`has_pending` so the runner's message-ingest path can
# tell a session is awaiting a human approval and must NOT have that gate
# perturbed by a mid-turn message injection (e.g. a parent agent's
# ``sys_session_send`` to a blocked child — that message would otherwise
# steer the parked turn past the human gate). See the ingest guard in
# ``omnigent/runner/app.py``.
_session_pending: dict[str, int] = {}


def has_pending(conversation_id: str) -> bool:
    """
    Whether *conversation_id* currently has an outstanding ASK verdict.

    ``True`` between :func:`wait_for_user_approval` parking and its exit
    (verdict, decline, timeout, or cancellation). Lets callers treat a
    session as "awaiting human approval" without threading the
    elicitation id around.

    :param conversation_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: ``True`` when at least one approval is parked for the session.
    """
    return _session_pending.get(conversation_id, 0) > 0


def register(elicitation_id: str) -> asyncio.Future[bool]:
    """
    Create and store a Future for an outstanding ASK verdict.

    Must be called from inside an asyncio event loop so the Future
    binds to it. Each ``elicitation_id`` should be unique (the
    server mints these as UUIDs); re-registering the same id
    silently overwrites the prior entry.

    :param elicitation_id: Correlation id, e.g. ``"elicit_abc123"``.
    :returns: The newly created Future. Caller awaits with
        :func:`asyncio.wait_for` to bound the wait.
    """
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    _pending[elicitation_id] = fut
    return fut


def cleanup(elicitation_id: str) -> None:
    """
    Remove an entry from the registry.

    Idempotent — popping an unknown id is a no-op. Callers must
    invoke this in a ``finally`` block paired with :func:`register`
    so cancelled / timed-out Futures don't leak.

    :param elicitation_id: Correlation id to drop.
    """
    _pending.pop(elicitation_id, None)


def resolve(elicitation_id: str, approved: bool) -> bool:
    """
    Set the verdict on a registered Future.

    Called by the runner's session-event handler when an
    ``approval`` event arrives. Idempotent: returns ``False`` for
    unknown ids or already-completed Futures so callers can
    distinguish "delivered" from "no-op."

    :param elicitation_id: Correlation id from the approval event.
    :param approved: ``True`` on ``action == "accept"``, ``False``
        on decline or other terminal actions.
    :returns: ``True`` if the Future was unresolved and was set;
        ``False`` if no Future was registered or it was already
        completed (e.g. timed out before the verdict arrived).
    """
    fut = _pending.get(elicitation_id)
    if fut is None or fut.done():
        return False
    fut.set_result(approved)
    return True


async def wait_for_user_approval(
    *,
    elicitation_id: str,
    conversation_id: str,
    publish_event: Callable[[str, dict[str, Any]], None],
    timeout_seconds: float | None = None,
) -> bool:
    """
    Park on a registered Future until the user delivers a verdict.

    Centralizes the register → wait_for → cleanup → publish
    sequence so every ASK-escalation site emits a
    ``response.elicitation_resolved`` event on the way out — the
    Omnigent server's pending-elicitations index (powers the sidebar
    badge) decrements when it sees that event, so emitting on
    every exit path (verdict, timeout, cancellation) keeps the
    badge in lockstep with the underlying awaiter.

    Returns ``False`` on timeout. Cancellation propagates: if the
    caller's task is cancelled the ``finally`` still emits the
    resolved event so the badge clears.

    :param elicitation_id: Correlation id minted by the Omnigent server's
        policy evaluator and returned in the ``pending`` verdict,
        e.g. ``"elicit_abc123"``.
    :param conversation_id: Session/conversation id the prompt
        was published on, e.g. ``"conv_abc123"``.
    :param publish_event: Callable that puts an SSE event on the
        runner's per-session outbound queue. Same shape the
        runner's ``_publish_event`` helper uses.
    :param timeout_seconds: Maximum seconds to wait before
        treating the prompt as refused, e.g. the spec-resolved
        ``ask_timeout`` from the server's pending verdict. ``None``
        falls back to :data:`_DEFAULT_WAIT_SECONDS`.
    :returns: ``True`` on accept, ``False`` on decline / timeout.
    """
    effective_timeout = _DEFAULT_WAIT_SECONDS if timeout_seconds is None else timeout_seconds
    fut = register(elicitation_id)
    # Mark the session as awaiting approval for the lifetime of this park
    # so ``has_pending`` reports it. Decremented in ``finally`` on every
    # exit path (verdict, timeout, cancellation) so the flag never leaks.
    _session_pending[conversation_id] = _session_pending.get(conversation_id, 0) + 1
    try:
        approved = await asyncio.wait_for(fut, timeout=effective_timeout)
    except asyncio.TimeoutError:
        approved = False
    finally:
        cleanup(elicitation_id)
        _remaining = _session_pending.get(conversation_id, 0) - 1
        if _remaining > 0:
            _session_pending[conversation_id] = _remaining
        else:
            _session_pending.pop(conversation_id, None)
        # Signal the Omnigent server's pending-elicitations index that
        # this prompt is done. Idempotent on the happy path (the
        # AP-side dispatch already cleared the entry); on timeout
        # / cancellation this event is the ONLY signal the server
        # gets, so it must fire on every exit path.
        publish_event(
            conversation_id,
            {
                "type": "response.elicitation_resolved",
                "elicitation_id": elicitation_id,
            },
        )
    return approved


def reset_for_tests() -> None:
    """
    Clear the registry. For test isolation only — leaked Futures
    from one test silently change the behavior of the next.
    """
    _pending.clear()
    _session_pending.clear()
