"""Wake a parent agent when one of its descendants blocks on an approval.

A blocked sub-agent (e.g. a codex worker asking to run ``git fetch``) never
reaches a terminal status, so the completion-only parent-wake in
``omnigent/runner/app.py`` leaves the orchestrator idle and unaware. This
module observes every elicitation at the
``pending_elicitations.record_publish`` chokepoint. The approval prompt itself
is mirrored into ancestor chats for the human, so for a child session the
parent *agent* is woken only after an escalation grace passes with the block
still unanswered (once per block, re-arming when it clears); a delivered wake
is then paired with a resolution notice — carrying the human's verdict — when
the block clears so the parent is never left waiting on a stale notice or
narrating a guessed verdict into the transcript. Delivery is an injected
``wake_dispatch`` coroutine (kept free of HTTP/server knowledge) wired up by the Omnigent server
in ``configure_subagent_block_notifier``. See
``designs/SUBAGENT_ELICITATION_VISIBILITY_V2.md``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import enum
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation
    from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

_REQUEST_TYPE = "response.elicitation_request"
_RESOLVED_TYPE = "response.elicitation_resolved"

# Max length of the elicitation prompt echoed into the parent's notice.
_REASON_MAX_CHARS = 200

# MCP ElicitResult verdicts a resolution event may carry.
_VERDICT_ACTIONS = ("accept", "decline", "cancel")

# Bounded in-handler retry for a transient delivery miss (parent runner briefly
# unroutable during a reconnect/relaunch). Total attempts = 1 + _WAKE_RETRIES.
_WAKE_RETRIES = 2
# Backoff between retry attempts; short because the window we cover (a runner
# rebinding) is on the order of a second, not minutes.
_WAKE_RETRY_BACKOFF_S = 0.5

# How long a block may sit unanswered before the parent agent is woken.
# The approval card is already mirrored into the parent chat, so an
# attended session resolves silently; the wake is the unattended fallback.
_BLOCK_WAKE_ESCALATION_DELAY_S = 120.0

# Injected delivery: post ``notice`` to the parent and wake it. Returns True when
# the wake was confirmed delivered, False when it could not be (no runner bound /
# caught transport error) so the notifier can release the debounce and retry.
WakeDispatch = Callable[[str, "Conversation", str], Awaitable[bool]]


class _WakeOutcome(enum.Enum):
    """
    Result of one bounded-retry wake delivery.

    ``DELIVERED`` — the dispatch confirmed delivery; ``MOOT`` — the block
    resolved mid-flight so nothing (stale) was sent and the cleared
    debounce slot must be left alone; ``FAILED`` — every attempt failed,
    so the caller releases the arm and a re-publish can retry.
    """

    DELIVERED = "delivered"
    MOOT = "moot"
    FAILED = "failed"


async def _sleep(seconds: float) -> None:
    """
    Indirection over :func:`asyncio.sleep` so tests can stub the retry
    backoff without clobbering the process-global ``asyncio.sleep``.

    :param seconds: Seconds to sleep, e.g. ``0.5``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


async def _escalation_sleep(seconds: float) -> None:
    """
    Indirection over :func:`asyncio.sleep` for the escalation grace, kept
    separate from :func:`_sleep` so tests can gate the grace and the retry
    backoff independently.

    :param seconds: Seconds to sleep, e.g. ``120.0``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


class SubagentBlockNotifier:
    """
    Observe elicitation events and wake a blocked child's parent.

    One instance is constructed and registered per Omnigent server process.
    Its :meth:`observe` method is registered with
    :func:`omnigent.runtime.pending_elicitations.set_elicitation_observer`;
    every tracked elicitation event flows through it.

    :param conversation_store: Store used to resolve a child session's
        immediate parent (``parent_conversation_id``).
    :param wake_dispatch: Coroutine that delivers a notice to a parent
        session and wakes it, returning ``True`` on a confirmed delivery
        and ``False`` when it could not deliver. See :data:`WakeDispatch`.
    :param loop: The Omnigent server's event loop, captured at registration
        time so :meth:`observe` (which runs synchronously on the publish
        path, possibly off the loop) can schedule async handling onto it.
    """

    def __init__(
        self,
        conversation_store: ConversationStore,
        wake_dispatch: WakeDispatch,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._conversation_store = conversation_store
        self._wake_dispatch = wake_dispatch
        self._loop = loop
        self._lock = threading.Lock()
        # Debounce: armed ids (one wake per block), cleared + re-armed on resolve.
        self._notified: set[str] = set()
        # Per-block resolve signal, registered by the handler before it
        # dispatches a wake so a resolve racing the in-flight wake is never
        # missed; observe() sets it on the resolved event.
        self._resolution_signals: dict[str, asyncio.Event] = {}
        # Verdict captured off the resolved event ("accept"/"decline"/"cancel"),
        # stored only while a handler waits on the matching signal so entries
        # never outlive the notice they inform.
        self._verdicts: dict[str, str] = {}
        # Strong refs so scheduled wake futures aren't GC'd mid-flight; close() cancels them.
        self._inflight: set[concurrent.futures.Future[None]] = set()

    def observe(self, conversation_id: str, event: dict[str, Any]) -> None:
        """
        React to one tracked elicitation event (the registered observer).

        Runs synchronously on the publish path (no I/O): a ``resolved`` event
        clears the debounce and signals any handler waiting to send the
        resolution notice; a ``request`` event arms the debounce and schedules
        :meth:`handle_request` onto the captured loop for the off-hot-path
        grace + parent lookup. Arm and clear both happen here, so they order
        deterministically. Other event types are ignored.

        :param conversation_id: Session the event was published on,
            e.g. ``"conv_child123"``.
        :param event: The elicitation request/resolved event dict.
        :returns: None.
        """
        elicitation_id = event.get("elicitation_id")
        # No id → can't be matched by its resolve, so never arm.
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        event_type = event.get("type")
        if event_type == _RESOLVED_TYPE:
            with self._lock:
                self._notified.discard(elicitation_id)
                signal = self._resolution_signals.get(elicitation_id)
                action = _resolution_action(event)
                if signal is not None and action is not None:
                    self._verdicts[elicitation_id] = action
            if signal is not None:
                # Event.set is not thread-safe; hop onto the loop. A gone
                # loop (shutdown) means the waiting handler dies with it.
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(signal.set)
            return
        if event_type != _REQUEST_TYPE:
            return
        with self._lock:
            if elicitation_id in self._notified:
                return  # already armed for this block — debounce
            self._notified.add(elicitation_id)
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.handle_request(conversation_id, event),
                self._loop,
            )
        except RuntimeError:
            # Loop gone (shutdown): release the arm so a later block isn't wrongly debounced.
            with self._lock:
                self._notified.discard(elicitation_id)
            _logger.debug(
                "subagent block notifier: loop unavailable, dropping wake for %s",
                conversation_id,
            )
            return
        with self._lock:
            self._inflight.add(future)
        future.add_done_callback(self._discard_inflight)

    def _discard_inflight(self, future: concurrent.futures.Future[None]) -> None:
        """
        Drop a completed wake future from the in-flight set.

        Registered as the done-callback on every future scheduled by
        :meth:`observe`, and locked against a concurrent observe-add or
        :meth:`close`. Delivery swallows its own transport errors, so a future
        completing with an exception is unexpected and gets logged rather than
        silently discarded.

        :param future: The just-completed (or cancelled) wake future.
        :returns: None.
        """
        with self._lock:
            self._inflight.discard(future)
        if future.cancelled():
            return  # teardown via close() — expected, not an error
        exc = future.exception()
        if exc is not None:
            _logger.warning(
                "subagent block notifier: wake handling raised unexpectedly",
                exc_info=exc,
            )

    def close(self) -> None:
        """
        Cancel any in-flight wake futures. For lifespan teardown.

        Idempotent. After ``close`` the notifier should also be
        unregistered via
        :func:`omnigent.runtime.pending_elicitations.set_elicitation_observer`
        so no new work is scheduled.

        :returns: None.
        """
        with self._lock:
            inflight = list(self._inflight)
            self._inflight.clear()
        for future in inflight:
            future.cancel()

    async def handle_request(
        self,
        conversation_id: str,
        event: dict[str, Any],
    ) -> None:
        """
        Escalate one unanswered block to the parent, then close the loop.

        The approval prompt is mirrored into ancestor chats the moment it
        publishes, so the human's surface is immediate; this handler waits
        out ``_BLOCK_WAKE_ESCALATION_DELAY_S`` and only wakes the parent
        *agent* if the block is still unanswered (the debounce arm doubles
        as the cancellation token — :meth:`observe` clears it on resolve).
        For a top-level session (no parent) this is a no-op.

        Delivery is attempted up to ``1 + _WAKE_RETRIES`` times (the parent's
        runner may be briefly unroutable during a reconnect/relaunch). An
        exhausted failure *releases* the arm so a re-publish of the same
        elicitation re-fires; a confirmed delivery keeps it held (one wake
        per block) and the handler then waits for the resolve signal to send
        the parent a resolution notice, so it is never left acting on a
        stale block notice.

        :param conversation_id: The blocked child session id,
            e.g. ``"conv_child123"``.
        :param event: The ``response.elicitation_request`` event dict.
        :returns: None.
        """
        elicitation_id = event.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        await _escalation_sleep(_BLOCK_WAKE_ESCALATION_DELAY_S)
        with self._lock:
            if elicitation_id not in self._notified:
                return  # answered within the grace — the parent never needs to know
        child = await asyncio.to_thread(
            self._conversation_store.get_conversation,
            conversation_id,
        )
        if child is None or child.parent_conversation_id is None:
            # Top-level session: no parent; the resolve event clears the arm.
            return
        parent_id = child.parent_conversation_id
        notice = _format_block_notice(child, event)
        # Register the resolve signal before dispatching so a resolve that
        # lands while the wake is in flight is never missed; the arm check
        # and registration share one critical section, so a resolve landing
        # earlier makes this return instead of registering.
        with self._lock:
            if elicitation_id not in self._notified:
                return
            signal = asyncio.Event()
            self._resolution_signals[elicitation_id] = signal
        try:
            outcome = await self._deliver_with_retry(
                parent_id, child, notice, armed_id=elicitation_id
            )
            if outcome is _WakeOutcome.FAILED:
                # Release the arm so a later publish of this id can retry.
                with self._lock:
                    self._notified.discard(elicitation_id)
                return
            if outcome is _WakeOutcome.MOOT:
                return
            await signal.wait()
            with self._lock:
                verdict = self._verdicts.pop(elicitation_id, None)
            await self._deliver_with_retry(
                parent_id, child, _format_resolution_notice(child, verdict), armed_id=None
            )
        finally:
            with self._lock:
                self._resolution_signals.pop(elicitation_id, None)
                self._verdicts.pop(elicitation_id, None)

    async def _deliver_with_retry(
        self,
        parent_id: str,
        child: Conversation,
        notice: str,
        *,
        armed_id: str | None,
    ) -> _WakeOutcome:
        """
        Attempt one notice dispatch with a bounded retry.

        With ``armed_id`` set (block notices) the arm is re-checked before
        each attempt: a block that resolved while an earlier attempt was in
        flight clears the arm and this reports ``MOOT`` so the caller leaves
        the cleared slot alone. ``None`` (resolution notices — the block is
        already resolved) skips that gate. A dispatch that returns ``False``
        or raises is retried after a short backoff; an exhausted run reports
        ``FAILED``. A raising dispatch is caught broadly (its error taxonomy
        is unknown to this module) so a delivery failure never crashes the
        publish path; ``CancelledError`` (a ``BaseException`` raised by
        :meth:`close`) is not caught and still tears the handler down.

        :param parent_id: Parent session id to wake, e.g. ``"conv_parent123"``.
        :param child: The blocked child :class:`Conversation`.
        :param notice: Pre-formatted ``[System: …]`` notice text.
        :param armed_id: Correlation id whose arm gates the dispatch, or
            ``None`` to dispatch unconditionally.
        :returns: The :class:`_WakeOutcome` of the delivery.
        """
        for attempt in range(1 + _WAKE_RETRIES):
            if armed_id is not None:
                with self._lock:
                    if armed_id not in self._notified:
                        # Resolved during the grace/backoff — block's gone, the
                        # slot is already clear; nothing (stale) to wake.
                        return _WakeOutcome.MOOT
            try:
                if await self._wake_dispatch(parent_id, child, notice):
                    return _WakeOutcome.DELIVERED
            except Exception:
                # Broad: injected dispatch error types are unknown; don't crash the publish path.
                _logger.warning(
                    "subagent block notifier: wake dispatch raised for parent=%s child=%s "
                    "(attempt %d/%d)",
                    parent_id,
                    child.id,
                    attempt + 1,
                    1 + _WAKE_RETRIES,
                    exc_info=True,
                )
            if attempt < _WAKE_RETRIES:
                await _sleep(_WAKE_RETRY_BACKOFF_S)
        _logger.warning(
            "subagent block notifier: notice undelivered after %d attempt(s) for "
            "parent=%s child=%s",
            1 + _WAKE_RETRIES,
            parent_id,
            child.id,
        )
        return _WakeOutcome.FAILED


def _format_block_notice(child: Conversation, event: dict[str, Any]) -> str:
    """
    Build the ``[System: …]`` notice posted into the parent session.

    Mirrors the shape of the runner's terminal-completion wake notice
    (``_format_subagent_wake_notice``). Describes the situation and asks
    the parent to surface it — it does not prescribe a specific tool.

    :param child: The blocked child :class:`Conversation`, used for its
        ``<agent>:<title>`` label.
    :param event: The ``response.elicitation_request`` event dict; its
        ``params.message`` (when present) is echoed as the reason.
    :returns: A one-line ``[System: …]`` notice, e.g. ``"[System:
        sub-agent codex/auth-refactor is blocked awaiting human
        approval: Codex wants to run 'git fetch'. Its approval prompt
        is mirrored into this conversation but has gone unanswered —
        surface the situation to the human and do not wait silently. It
        cannot continue until the request is resolved.]"``.
    """
    label = _child_label(child)
    reason = _block_reason(event)
    detail = f": {reason}" if reason else ""
    return (
        f"[System: sub-agent {label} is blocked awaiting human approval{detail}. "
        "Its approval prompt is mirrored into this conversation but has gone "
        "unanswered — surface the situation to the human and do not wait "
        "silently. It cannot continue until the request is resolved.]"
    )


def _format_resolution_notice(child: Conversation, action: str | None) -> str:
    """
    Build the follow-up notice sent after a woken block resolves.

    Sent only when a block notice was actually delivered, so the parent
    stops acting on it (offering to relay answers, polling the child)
    once the human has dealt with the prompt directly. The human's
    verdict is stated verbatim so the parent agent cannot narrate an
    approval that did not happen (a decline must never be retold as
    "approved" into the transcript); when no verdict was recorded
    (timeout, severed wait, older runner) the notice says so and points
    at the fail-closed reading.

    :param child: The previously blocked child :class:`Conversation`,
        used for its ``<agent>:<title>`` label.
    :param action: The MCP verdict from the resolution event —
        ``"accept"``, ``"decline"``, or ``"cancel"`` — or ``None``
        when the resolution carried no verdict.
    :returns: A one-line ``[System: …]`` notice, e.g. ``"[System:
        sub-agent codex/auth-refactor's pending approval has been
        resolved (action: decline — NOT approved) and it is
        continuing. …]"``.
    """
    label = _child_label(child)
    return (
        f"[System: sub-agent {label}'s pending approval has been resolved "
        f"{_verdict_clause(action)} and it is continuing. No action is needed "
        "on the earlier block notice — if you are waiting on its output, the "
        "result will arrive in your inbox as usual.]"
    )


def _verdict_clause(action: str | None) -> str:
    """
    Render the human's verdict as an un-misreadable clause.

    Every branch names the gate outcome explicitly so an agent reading
    the transcript cannot infer "approved" from the bare word
    "resolved".

    :param action: The MCP verdict, or ``None`` when unrecorded.
    :returns: A parenthesized clause, e.g. ``"(action: decline — NOT
        approved)"``.
    """
    if action == "accept":
        return "(action: accept)"
    if action == "decline" or action == "cancel":
        return f"(action: {action} — NOT approved)"
    return "(no human verdict recorded — treat the call as NOT approved)"


def _resolution_action(event: dict[str, Any]) -> str | None:
    """
    Extract the MCP verdict from a ``response.elicitation_resolved`` event.

    :param event: The resolved event dict; reads ``action``.
    :returns: ``"accept"``/``"decline"``/``"cancel"``, or ``None``
        when the event predates verdict carriage or carries a
        malformed value.
    """
    action = event.get("action")
    if isinstance(action, str) and action in _VERDICT_ACTIONS:
        return action
    return None


def _child_label(child: Conversation) -> str:
    """
    Derive a human label from a sub-agent conversation title.

    Named sub-agents store ``"<agent>:<title>"`` in
    :attr:`Conversation.title`. Returns ``"<agent>/<title>"`` for that
    form, falling back to the raw title or, last, the conversation id so
    the notice always names *something*.

    :param child: The child :class:`Conversation`.
    :returns: A label like ``"codex/auth-refactor"``.
    """
    title = child.title or ""
    if ":" in title:
        agent, _, remainder = title.partition(":")
        sa_title = remainder.partition(":closed:")[0]
        return f"{agent}/{sa_title}" if sa_title else agent
    return title or child.id


def _block_reason(event: dict[str, Any]) -> str | None:
    """
    Extract a short human reason from an elicitation request event.

    :param event: The ``response.elicitation_request`` event dict; reads
        the nested ``params.message``.
    :returns: The trimmed/truncated prompt text, or ``None`` when the
        event carried no message.
    """
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    message = params.get("message")
    if not isinstance(message, str):
        return None
    message = message.strip()
    if not message:
        return None
    if len(message) > _REASON_MAX_CHARS:
        return message[: _REASON_MAX_CHARS - 1].rstrip() + "…"
    return message
