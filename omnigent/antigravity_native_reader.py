"""RPC read driver for a native Antigravity (agy) session.

This is the read-path driver that replaces the transcript-tail forwarder's read
loop (:func:`omnigent.antigravity_native_forwarder.forward_antigravity_transcript_to_session`).
Instead of tailing agy's plaintext JSONL transcript, it polls agy's connect-RPC
``GetCascadeTrajectorySteps`` surface for trajectory steps, maps each new step to
Omnigent conversation items, POSTs them, emits ``external_session_status`` edges
on turn transitions, and hands ``WAITING`` steps (questions / permission asks) to
the Task 8 interaction bridge through an injected callback.

How it differs from the transcript forwarder it supersedes:

* **Read transport is the RPC, not the file.** Steps come from
  :func:`omnigent.antigravity_native_rpc.get_trajectory_steps` rather than a byte
  tail. The RPC returns the *full* trajectory step list on every call (a
  snapshot), so the driver de-dups *within the run* by ``(trajectory_id,
  step_index)`` identity and posts only steps it has not yet seen.

* **No durable cursor.** The transcript forwarder persisted a ``forwarded_steps``
  resume cursor to bridge state so a restart did not re-mirror the whole file.
  This driver keeps an *in-memory* seen-set only; the durable cursor (and its
  JSONL) is retired in the Task 12 cutover. A restart re-reads from the start —
  acceptable because the reader is recreated per session by the Task 11 runner,
  not crash-restarted mid-conversation, and the mapper's USER_INPUT-skip plus the
  server's own item handling bound the blast radius.

* **The mapper carries the item logic.** :func:`map_step_to_events` is the pure,
  no-delta, skip-USER_INPUT mapping layer (Task 4). It deliberately does NOT emit
  status edges — that was always the stateful parser's job. This driver is now
  that stateful layer: it replicates the transcript parser's RUNNING/IDLE
  transition emission (a turn opens on a USER_INPUT step and closes on an
  assistant-text PLANNER_RESPONSE that issues no tool calls), deduped so an edge
  fires only on a real transition.

Discovery mirrors the forwarder's discipline — *poll until ready, never guess*:

1. **Cascade id.** agy mints its own conversation UUID (it ignores the launcher's
   ``ANTIGRAVITY_CONVERSATION_ID``) and the launcher seeds bridge state with an
   ``agy_conv_*`` placeholder until the real id is discovered and persisted. The
   reader polls :func:`read_bridge_state` until ``conversation_id`` is present and
   is NOT a placeholder; that real id is the cascade id (agy uses one UUID for
   both the conversation and the cascade).
2. **RPC port.** The reader enumerates candidate agy connect-RPC ports
   (:func:`_candidate_agy_rpc_ports`) and binds the one that confirms it hosts the
   cascade id (:func:`_conversation_matches`). It keeps polling until a port
   confirms ownership — a recycled/foreign port is rejected, never written to.

Everything that touches the network (the RPC client) or the clock (sleeps) is
funnelled through module-level seams so the unit tests drive the loop with a
scripted step source and a captured post sink, no real agy and no real sockets.
The loop is finite under test via an injectable ``stop`` predicate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from omnigent._native_post_delivery import post_session_event_with_retry
from omnigent.antigravity_native_bridge import (
    is_placeholder_conversation_id,
    read_bridge_state,
)

# These come from the forwarder, which still owns them until the Task 12 cutover
# relocates them. The reader reuses the SAME ``OutboundEvent`` shape and the SAME
# allocator so the mapped events post identically to the transcript path's.
from omnigent.antigravity_native_forwarder import (
    OutboundEvent,
    _ToolCallIdAllocator,
)
from omnigent.antigravity_native_rpc import (
    _candidate_agy_rpc_ports,
    _conversation_matches,
    get_trajectory_steps,
)
from omnigent.antigravity_native_steps import (
    PendingInteraction,
    _step_index,
    _trajectory_id,
    map_step_to_events,
    pending_interaction,
)
from omnigent.claude_native_bridge import url_component

_logger = logging.getLogger(__name__)

# Default seconds between RPC polls. The RPC returns a full snapshot each call
# and steps finalize only at DONE (no token streaming), so a sub-second cadence
# keeps the mirror responsive without hammering the loopback server.
_DEFAULT_POLL_INTERVAL_S = 0.25

# POST retry policy, kept identical to the transcript forwarder's so mirrored
# items are delivered with the same transient-retry semantics. Conversation
# items persist with a random primary key and are NOT deduped server-side, so an
# ambiguous transport failure is not retried (handled inside
# :func:`post_session_event_with_retry`).
_POST_MAX_ATTEMPTS = 3
_POST_RETRY_DELAY_SECONDS = 0.1
_POST_RETRY_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Session-status edge values (mirror the transcript forwarder's vocabulary).
_STATUS_RUNNING = "running"
_STATUS_IDLE = "idle"

# RPC step type/status constants needed for the status-transition heuristic. The
# item-mapping constants live in the mapper; the driver only needs the few it
# keys turn transitions on.
_TYPE_USER_INPUT = "CORTEX_STEP_TYPE_USER_INPUT"
_TYPE_PLANNER_RESPONSE = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"

# Dedup key for a step within a run. ``step_index`` is ``None`` for USER_INPUT
# (no trajectory slot) and proto-omitted (treated as ``None`` here) for step 0;
# pairing it with ``trajectory_id`` keeps the key stable per step. USER_INPUT
# maps to ``[]`` so a key collision across turns only skips re-processing a
# no-op, never drops content.
_StepKey = tuple[str | None, int | None]

OnPendingInteraction = Callable[[PendingInteraction], Awaitable[None]]
StopPredicate = Callable[[], bool]


async def _sleep(seconds: float) -> None:
    """
    Stubbable indirection for the poll/backoff sleep.

    Exists so tests can drive the loop without real delays without patching
    ``asyncio.sleep`` through the imported module singleton.

    :param seconds: Delay in seconds.
    :returns: None after the sleep completes.
    """
    await asyncio.sleep(seconds)


def _step_key(step: dict[str, object]) -> _StepKey:
    """
    Build the within-run dedup key for one RPC step.

    Reuses the mapper's ``trajectory_id`` + ``step_index`` extraction so the key
    is identical to the identity :func:`pending_interaction` keys on — a step is
    "the same step" for de-dup, status, and interaction purposes consistently.

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :returns: ``(trajectory_id, step_index)`` identity tuple (either element may
        be ``None``).
    """
    return (_trajectory_id(step), _step_index(step))


def _is_user_turn_step(step: dict[str, object]) -> bool:
    """
    Return whether a step opens a turn (a USER_INPUT step).

    The RPC equivalent of the transcript forwarder's
    :func:`_is_turn_boundary_running`: a user input step starts a turn (agy then
    runs the model + tools).

    :param step: One RPC step dict.
    :returns: ``True`` for a ``CORTEX_STEP_TYPE_USER_INPUT`` step.
    """
    return step.get("type") == _TYPE_USER_INPUT


def _is_assistant_text_close_step(step: dict[str, object]) -> bool:
    """
    Return whether a step closes a turn (assistant text, no further tool calls).

    The RPC equivalent of the transcript forwarder's
    :func:`_is_assistant_text_step`: a PLANNER_RESPONSE that carries assistant
    text (``modifiedResponse`` or ``response``) and issues NO tool calls is the
    closing edge of a turn — agy answered and stopped. A planner step that only
    invokes a tool does not close the turn (the tool result, and possibly more
    planner steps, follow).

    :param step: One RPC step dict.
    :returns: ``True`` when the step is a PLANNER_RESPONSE with non-empty text
        and an empty/absent ``toolCalls`` list.
    """
    if step.get("type") != _TYPE_PLANNER_RESPONSE:
        return False
    planner = step.get("plannerResponse")
    if not isinstance(planner, dict):
        return False
    modified = planner.get("modifiedResponse")
    response = planner.get("response")
    text = modified if isinstance(modified, str) and modified else response
    if not isinstance(text, str) or not text.strip():
        return False
    tool_calls = planner.get("toolCalls")
    return not (isinstance(tool_calls, list) and tool_calls)


def _status_event(status: str) -> OutboundEvent:
    """
    Build an ``external_session_status`` edge.

    ``step_index`` is unused by the RPC read path (there is no durable per-step
    cursor to advance — that was retired with the transcript forwarder), so it is
    stamped 0; the field is retained only because :class:`OutboundEvent` is shared
    with the transcript path.

    :param status: Session status, e.g. ``"running"`` or ``"idle"``.
    :returns: One ``external_session_status`` event.
    """
    return OutboundEvent(
        event_type="external_session_status",
        data={"status": status},
        step_index=0,
    )


async def _post_event(
    client: httpx.AsyncClient,
    session_id: str,
    event: OutboundEvent,
) -> None:
    """
    POST one mapped event with the shared bounded-retry delivery loop.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: The mapped event to deliver.
    :returns: None. Delivery failures are logged inside the retry loop; an
        ambiguous conversation-item failure is intentionally not retried (a
        re-post would duplicate the item).
    """
    url = f"/v1/sessions/{url_component(session_id)}/events"
    payload: dict[str, object] = {"type": event.event_type, "data": event.data}
    await post_session_event_with_retry(
        client=client,
        url=url,
        payload=payload,
        event_type=event.event_type,
        max_attempts=_POST_MAX_ATTEMPTS,
        retry_status_codes=_POST_RETRY_STATUS_CODES,
        sleep=_sleep,
        retry_delay=lambda attempt: _POST_RETRY_DELAY_SECONDS * attempt,
        logger_name=__name__,
    )


def _resolve_cascade_id(bridge_dir: Path) -> str | None:
    """
    Return agy's real cascade id from bridge state, or ``None`` if not ready.

    The launcher seeds bridge state's ``conversation_id`` with an ``agy_conv_*``
    placeholder until the forwarder/executor discovers and persists agy's real
    UUID; a placeholder means "not ready yet" (it never names a live cascade), so
    it is rejected here. agy uses one UUID for both the conversation and the
    cascade, so the resolved conversation id IS the cascade id.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: The real cascade id, or ``None`` when bridge state is missing or
        still holds the placeholder.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return None
    if is_placeholder_conversation_id(state.conversation_id):
        return None
    return state.conversation_id


def _resolve_rpc_port(cascade_id: str) -> int | None:
    """
    Return the agy connect-RPC port that hosts ``cascade_id``, or ``None``.

    Mirrors :func:`omnigent.antigravity_native_rpc.resolve_language_server_port`'s
    port-first discipline: enumerate every live agy connect-RPC port and bind the
    one whose ``GetConversationMetadata`` confirms it hosts this cascade id. A
    recycled/foreign port (a different live agy) is rejected because it cannot
    echo this id.

    :param cascade_id: agy cascade id (equal to the conversation id) to locate.
    :returns: A validated connect-RPC port hosting ``cascade_id``, or ``None``
        when no running agy could be matched yet.
    """
    for port in _candidate_agy_rpc_ports():
        if _conversation_matches(port, cascade_id):
            return port
    return None


async def _discover(
    bridge_dir: Path,
    *,
    poll_interval_s: float,
    stop: StopPredicate,
) -> tuple[str, int] | None:
    """
    Resolve ``(cascade_id, port)``, polling until ready or asked to stop.

    Two stages, each "poll until ready, never guess": first the real cascade id
    from bridge state (past the launcher placeholder), then the connect-RPC port
    that confirms ownership of that cascade. Discovery work (file read + blocking
    httpx TLS probes) runs in a worker thread so the event loop stays responsive.

    Readiness is checked BEFORE ``stop`` each round, so a discovery that resolves
    immediately consumes none of the caller's poll budget — ``stop`` is a
    "give up while still waiting" valve (the runner owns restart), not a cost the
    happy path pays. Discovery therefore always attempts at least one resolution.

    :param bridge_dir: Native Antigravity bridge directory.
    :param poll_interval_s: Seconds to wait between discovery polls.
    :param stop: Predicate consulted only when a round did NOT resolve; when it
        returns ``True`` the discovery loop gives up (the runner owns restart).
    :returns: ``(cascade_id, port)`` once both resolve, or ``None`` if ``stop``
        fired before discovery completed.
    """
    while True:
        cascade_id = await asyncio.to_thread(_resolve_cascade_id, bridge_dir)
        if cascade_id is not None:
            port = await asyncio.to_thread(_resolve_rpc_port, cascade_id)
            if port is not None:
                _logger.info(
                    "agy RPC reader bound: bridge_dir=%s cascade=%s port=%s",
                    bridge_dir,
                    cascade_id,
                    port,
                )
                return cascade_id, port
        if stop():
            return None
        await _sleep(poll_interval_s)


async def supervise_reader(
    bridge_dir: Path,
    session_id: str,
    *,
    client: httpx.AsyncClient,
    on_pending_interaction: OnPendingInteraction,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    stop: StopPredicate | None = None,
) -> None:
    """
    Poll agy's RPC for trajectory steps and mirror them into the Omnigent session.

    The read-path driver: it discovers the cascade id + connect-RPC port (polling
    until ready), then on each poll reads the full trajectory step snapshot, and
    for every step it has not seen before this run:

    * maps the step to conversation-item events (:func:`map_step_to_events`) and
      POSTs each one (USER_INPUT maps to ``[]`` so it posts nothing — the user
      turn is already persisted by the direct ``POST /events`` hook);
    * emits an ``external_session_status`` RUNNING edge when a user turn opens and
      an IDLE edge when an assistant-text step closes it, each only on a real
      transition (deduped via an in-memory turn-active flag);
    * when the step is ``WAITING`` for user interaction, invokes
      ``on_pending_interaction`` exactly once for that interaction (the Task 8
      bridge drives the elicitation + answer).

    De-dup is by ``(trajectory_id, step_index)`` identity in an in-memory
    seen-set (no durable cursor — retired in Task 12), so re-reading the same
    snapshot posts nothing. A single :class:`_ToolCallIdAllocator` is reused
    across polls so fallback ids stay stable; real agy tool-call ids (used by the
    mapper) make invocation↔output pairing order-independent regardless.

    Error handling: an RPC failure on a poll — ``httpx.HTTPError`` (transport AND
    non-2xx both raise it) or a ``ValueError`` (a non-JSON 200 body) — is logged
    and swallowed so a transient fault never kills the loop; the next poll
    recovers.

    :param bridge_dir: Native Antigravity bridge directory (identifies the
        session whose agy conversation to mirror).
    :param session_id: Omnigent conversation id to mirror into, e.g.
        ``"conv_abc123"``.
    :param client: HTTP client for Omnigent event posts.
    :param on_pending_interaction: Async callback handed each distinct WAITING
        interaction (the Task 8 interaction bridge). Invoked at most once per
        ``(trajectory_id, step_index)``.
    :param poll_interval_s: Seconds between RPC polls (and discovery polls).
    :param stop: Optional predicate consulted once per loop iteration; when it
        returns ``True`` the loop exits. ``None`` (production) loops until the
        task is cancelled. Provided so tests drive a bounded number of
        iterations.
    :returns: None.
    """
    should_stop: StopPredicate = stop if stop is not None else (lambda: False)

    discovered = await _discover(bridge_dir, poll_interval_s=poll_interval_s, stop=should_stop)
    if discovered is None:
        return
    cascade_id, port = discovered

    allocator = _ToolCallIdAllocator(conversation_id=cascade_id)
    seen: set[_StepKey] = set()
    interacted: set[_StepKey] = set()
    turn_active = False

    while not should_stop():
        try:
            steps = await asyncio.to_thread(get_trajectory_steps, port, cascade_id)
        except httpx.HTTPError as exc:
            _logger.warning(
                "agy RPC reader poll failed (transport/status); retrying: "
                "cascade=%s port=%s error=%r",
                cascade_id,
                port,
                exc,
            )
            await _sleep(poll_interval_s)
            continue
        except ValueError as exc:
            # A 2xx whose body was not valid JSON. Treat as transient like an
            # HTTP error: log and keep polling rather than crash the loop.
            _logger.warning(
                "agy RPC reader poll returned a non-JSON body; retrying: "
                "cascade=%s port=%s error=%r",
                cascade_id,
                port,
                exc,
            )
            await _sleep(poll_interval_s)
            continue

        for step in steps:
            key = _step_key(step)
            if key in seen:
                continue
            seen.add(key)
            turn_active = await _emit_step(
                step,
                client=client,
                session_id=session_id,
                cascade_id=cascade_id,
                allocator=allocator,
                turn_active=turn_active,
            )
            await _maybe_handle_interaction(
                step,
                key=key,
                interacted=interacted,
                on_pending_interaction=on_pending_interaction,
            )

        await _sleep(poll_interval_s)


async def _emit_step(
    step: dict[str, object],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    cascade_id: str,
    allocator: _ToolCallIdAllocator,
    turn_active: bool,
) -> bool:
    """
    Emit one new step's status edges + mapped conversation items.

    Replicates the transcript parser's ordering: a RUNNING status edge (when this
    step opens a turn) is posted BEFORE the step's items, and an IDLE edge (when
    this step closes the turn) AFTER them. Status edges fire only on a real
    transition, deduped via the ``turn_active`` flag threaded through the loop.

    :param step: One new (not-yet-seen) RPC step dict.
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param cascade_id: agy cascade id (namespaces response/call ids).
    :param allocator: Per-run tool-call id allocator (fallback ids only).
    :param turn_active: Whether a turn is currently considered open on entry.
    :returns: The updated ``turn_active`` flag after this step.
    """
    if _is_user_turn_step(step) and not turn_active:
        turn_active = True
        await _post_event(client, session_id, _status_event(_STATUS_RUNNING))

    for event in map_step_to_events(step, conversation_id=cascade_id, allocator=allocator):
        await _post_event(client, session_id, event)

    if _is_assistant_text_close_step(step) and turn_active:
        turn_active = False
        await _post_event(client, session_id, _status_event(_STATUS_IDLE))

    return turn_active


async def _maybe_handle_interaction(
    step: dict[str, object],
    *,
    key: _StepKey,
    interacted: set[_StepKey],
    on_pending_interaction: OnPendingInteraction,
) -> None:
    """
    Hand a WAITING step's pending interaction to the bridge, exactly once.

    A non-WAITING step yields no interaction. A WAITING step is handed to the
    callback only the first time its ``(trajectory_id, step_index)`` is seen as
    pending, so a re-read of the same WAITING snapshot does not re-fire the
    bridge.

    :param step: One new RPC step dict.
    :param key: The step's identity key (already computed by the caller).
    :param interacted: Set of interaction keys already handed to the bridge
        (mutated here).
    :param on_pending_interaction: Async callback for a distinct interaction.
    :returns: None.
    """
    pending = pending_interaction(step)
    if pending is None:
        return
    if key in interacted:
        return
    interacted.add(key)
    await on_pending_interaction(pending)
