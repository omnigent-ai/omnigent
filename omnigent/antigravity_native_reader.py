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
from dataclasses import dataclass, field
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
    AntigravityRpcError,
    _candidate_agy_rpc_ports,
    _conversation_matches,
    get_available_models,
    get_trajectory_steps,
    stream_agent_state_updates,
)
from omnigent.antigravity_native_steps import (
    PendingInteraction,
    _step_index,
    _trajectory_id,
    map_step_to_events,
    output_text_delta_event,
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

# Step status the STREAM path keys partial text on (Task T-D). A PLANNER_RESPONSE
# step carries its growing partial at ``plannerResponse.modifiedResponse`` while
# ``status == CORTEX_STEP_STATUS_GENERATING`` (``response`` is absent until DONE,
# where ``response == modifiedResponse``). The reader emits incremental
# ``output_text_delta`` events during GENERATING; the committed ``message`` is
# left to the mapper (it gates on DONE itself) once the step settles. The DONE
# constant is intentionally not duplicated here — the mapper owns that gate. See
# design §10.2.
_STATUS_GENERATING = "CORTEX_STEP_STATUS_GENERATING"

# Terminal step statuses — a step in one of these will not produce further
# content, so its identity is safe to record in the de-dup set (see
# :func:`_is_settled`). DONE carries the committed output; ERROR means the step
# failed before producing any. PENDING/RUNNING/WAITING/GENERATING are NOT
# terminal: a tool-result step passes through them before DONE, so recording it
# early would dedup and drop the eventual DONE output.
_STATUS_DONE = "CORTEX_STEP_STATUS_DONE"
_STATUS_ERROR = "CORTEX_STEP_STATUS_ERROR"
_TERMINAL_STATUSES = frozenset({_STATUS_DONE, _STATUS_ERROR})

# Dedup key for a step within a run. ``step_index`` is ``None`` for USER_INPUT
# (no trajectory slot) and proto-omitted (treated as ``None`` here) for step 0;
# pairing it with ``trajectory_id`` keeps the key stable per step. USER_INPUT
# maps to ``[]`` so a key collision across turns only skips re-processing a
# no-op, never drops content.
_StepKey = tuple[str | None, int | None]

# Telemetry event types (design §10.3 + §10.4).
_EXTERNAL_SESSION_USAGE = "external_session_usage"
_EXTERNAL_MODEL_CHANGE = "external_model_change"

OnPendingInteraction = Callable[[PendingInteraction], Awaitable[None]]
StopPredicate = Callable[[], bool]


def _model_usage_from_step(step: dict[str, object]) -> dict[str, object] | None:
    """
    Extract ``modelUsage`` from a PLANNER_RESPONSE DONE step.

    Returns ``None`` when the step has no usable usage data (wrong type, wrong
    status, missing field, or all zero/invalid values).  The design (§10.3)
    specifies that agy encodes all usage counts as STRING ints; we parse them
    defensively — a missing or non-numeric value is treated as 0 and excluded
    from the output unless it contributes.

    :param step: One RPC step dict.
    :returns: A dict with any of ``cumulative_input_tokens`` /
        ``cumulative_output_tokens`` / ``cumulative_cache_read_input_tokens`` /
        ``model`` (raw enum), or ``None`` when the step carries no usage.
    """
    if step.get("type") != _TYPE_PLANNER_RESPONSE or step.get("status") != _STATUS_DONE:
        return None
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return None
    raw_usage = metadata.get("modelUsage")
    if not isinstance(raw_usage, dict):
        return None

    def _to_int(val: object) -> int:
        """Parse a string-encoded int defensively; return 0 on failure."""
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                return 0
        return 0

    data: dict[str, object] = {}
    input_tokens = _to_int(raw_usage.get("inputTokens"))
    output_tokens = _to_int(raw_usage.get("outputTokens"))
    cache_read = _to_int(raw_usage.get("cacheReadTokens"))
    model_enum = raw_usage.get("model")

    if input_tokens > 0:
        data["cumulative_input_tokens"] = input_tokens
    if output_tokens > 0:
        data["cumulative_output_tokens"] = output_tokens
    if cache_read > 0:
        data["cumulative_cache_read_input_tokens"] = cache_read
    if isinstance(model_enum, str) and model_enum:
        data["model"] = model_enum  # resolved to displayName by caller

    if not data:
        return None
    return data


def _requested_model_enum_from_step(step: dict[str, object]) -> str | None:
    """
    Extract the model enum from a USER_INPUT step's plannerConfig.

    Reads ``step.userInput.userConfig.plannerConfig.requestedModel.model``
    (design §10.4). Returns ``None`` when the field is absent or the step is not
    a USER_INPUT.

    :param step: One RPC step dict.
    :returns: The model enum string, e.g. ``"MODEL_PLACEHOLDER_M20"``, or
        ``None`` when absent.
    """
    if step.get("type") != _TYPE_USER_INPUT:
        return None
    user_input = step.get("userInput")
    if not isinstance(user_input, dict):
        return None
    user_config = user_input.get("userConfig")
    if not isinstance(user_config, dict):
        return None
    planner_config = user_config.get("plannerConfig")
    if not isinstance(planner_config, dict):
        return None
    requested_model = planner_config.get("requestedModel")
    if not isinstance(requested_model, dict):
        return None
    model = requested_model.get("model")
    return model if isinstance(model, str) and model else None


def _resolve_display_name(model_enum: str, catalog: dict[str, object]) -> str:
    """
    Resolve a model enum to its human-readable ``displayName``.

    Iterates the ``catalog["models"]`` dict (live shape from
    :func:`get_available_models`) and returns the first entry whose ``model``
    field matches ``model_enum``. Falls back to the raw enum when the catalog
    is absent, malformed, or does not contain the enum — so an unknown enum is
    always reported rather than silently dropped.

    :param model_enum: agy model enum string, e.g. ``"MODEL_PLACEHOLDER_M20"``.
    :param catalog: Parsed response from ``GetAvailableModels``.
    :returns: The ``displayName`` string, e.g. ``"Gemini 2.5 Flash"``, or the
        raw enum as a fallback.
    """
    models = catalog.get("models")
    if not isinstance(models, dict):
        return model_enum
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("model") == model_enum:
            display = entry.get("displayName")
            if isinstance(display, str) and display:
                return display
    return model_enum


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

    # One allocator + one set of cross-poll/cross-frame trackers per reader run,
    # shared by BOTH the stream path and the poll fallback so a fall-through after
    # a partial stream does not re-post already-mirrored steps or re-open turns.
    state = _ReaderState(
        allocator=_ToolCallIdAllocator(conversation_id=cascade_id),
        seen=set(),
        interacted=set(),
        port=port,
    )

    # STREAM-primary (Task T-D): consume the connect server-stream for live
    # ``output_text_delta`` typing parity. On a stream error (transport
    # ``httpx.HTTPError`` or a connect-trailer ``AntigravityRpcError``) fall back
    # to the committed-only poll loop — graceful degradation to Phase-1 behaviour
    # — rather than letting the error kill the reader. The shared ``state`` makes
    # the fallback idempotent against whatever the stream already delivered.
    try:
        await _stream_loop(
            port=port,
            cascade_id=cascade_id,
            client=client,
            session_id=session_id,
            on_pending_interaction=on_pending_interaction,
            state=state,
            stop=should_stop,
        )
    except (httpx.HTTPError, AntigravityRpcError) as exc:
        _logger.warning(
            "agy RPC reader stream failed; falling back to poll (committed-only, "
            "no live deltas): cascade=%s port=%s error=%r",
            cascade_id,
            port,
            exc,
        )
        await _poll_loop(
            port=port,
            cascade_id=cascade_id,
            client=client,
            session_id=session_id,
            on_pending_interaction=on_pending_interaction,
            state=state,
            poll_interval_s=poll_interval_s,
            stop=should_stop,
        )


@dataclass
class _ReaderState:
    """
    Per-run trackers shared across the stream loop and the poll fallback.

    Kept in one object so a fall-through from a partially consumed stream to the
    poll loop reuses the same de-dup/turn/interaction state — a step already
    mirrored over the stream is not re-posted by the poll loop, and an open turn
    is not re-opened.

    :param allocator: Per-run fallback tool-call id allocator (real agy ids are
        preferred by the mapper; this only covers resume-mid-turn results that
        lack ``metadata.toolCall.id``).
    :param seen: ``(trajectory_id, step_index)`` identities whose COMMITTED items
        have been posted, so the on-connect snapshot replay (and steady-state
        re-reads) post nothing.
    :param interacted: Identities whose WAITING interaction was already handed to
        the bridge, so a re-sent WAITING frame does not re-fire it.
    :param prefixes: Per-PLANNER ``step_index`` → the length of ``modifiedResponse``
        already forwarded as deltas, so each frame emits only the NEW suffix.
        Cleared for a step when its committed ``message`` is posted (stream path
        only; the poll path never populates it).
    :param turn_active: Whether a turn is currently considered open (a RUNNING
        edge fired and no closing IDLE edge yet).
    :param posted_model_enum: The last model enum already mirrored via
        ``external_model_change``. ``None`` = none posted yet.  Tracks the raw
        enum (NOT the displayName) so de-dup comparison is enum-stable.
    :param model_catalog: Cached result of ``GetAvailableModels`` for this
        reader run (fetched once on first model-change detection; ``None``
        until needed).
    :param port: Validated connect-RPC port used for lazy catalog fetch.
    :param cumulative_input_tokens: Running session total of input tokens
        accumulated across all PLANNER_RESPONSE DONE steps this run. The server
        treats ``external_session_usage.cumulative_input_tokens`` as a SET
        (new value = current total), so we must sum, not emit per-call values.
        Reset to 0 at the start of each reader run; T-G /clear rotation must
        also zero this when it rotates to a fresh conversation.
    :param cumulative_output_tokens: Running session total of output tokens.
    :param cumulative_cache_read_input_tokens: Running session total of
        cache-read input tokens.
    """

    allocator: _ToolCallIdAllocator
    seen: set[_StepKey]
    interacted: set[_StepKey]
    prefixes: dict[int, str] = field(default_factory=dict)
    turn_active: bool = False
    posted_model_enum: str | None = None
    model_catalog: dict[str, object] | None = None
    port: int = 0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_cache_read_input_tokens: int = 0


async def _poll_loop(
    *,
    port: int,
    cascade_id: str,
    client: httpx.AsyncClient,
    session_id: str,
    on_pending_interaction: OnPendingInteraction,
    state: _ReaderState,
    poll_interval_s: float,
    stop: StopPredicate,
) -> None:
    """
    Poll ``GetCascadeTrajectorySteps`` and mirror new committed steps.

    The Phase-1 read path (committed-only, no live deltas) — now also the
    graceful fallback when the stream errors. On each poll it reads the full
    snapshot and, for every not-yet-seen step, emits the committed items + status
    edges via :func:`_emit_step` and hands a WAITING step to the bridge once.

    A poll failure — ``httpx.HTTPError`` (transport AND non-2xx) or ``ValueError``
    (a non-JSON 200 body) — is logged and swallowed so a transient fault never
    kills the loop; the next poll recovers.

    :param port: Validated connect-RPC port.
    :param cascade_id: agy cascade id (equal to the conversation id).
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param on_pending_interaction: Async callback for a distinct WAITING
        interaction.
    :param state: Per-run shared trackers (de-dup, turn, interactions).
    :param poll_interval_s: Seconds between polls.
    :param stop: Predicate consulted once per iteration; ``True`` exits.
    :returns: None.
    """
    while not stop():
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
            await _process_committed_step(
                step,
                client=client,
                session_id=session_id,
                cascade_id=cascade_id,
                state=state,
                on_pending_interaction=on_pending_interaction,
            )

        await _sleep(poll_interval_s)


async def _stream_loop(
    *,
    port: int,
    cascade_id: str,
    client: httpx.AsyncClient,
    session_id: str,
    on_pending_interaction: OnPendingInteraction,
    state: _ReaderState,
    stop: StopPredicate,
) -> None:
    """
    Consume ``StreamAgentStateUpdates`` for live deltas + committed items.

    For each frame's ``mainTrajectoryUpdate.stepsUpdate.steps[]`` (design §10.2):

    * A PLANNER_RESPONSE step with ``status == GENERATING`` → compute the NEW
      suffix of ``plannerResponse.modifiedResponse`` past the per-``step_index``
      forwarded prefix and, when non-empty, emit one incremental
      ``external_output_text_delta`` (stable per-step ``message_id``,
      ``final=False``). The prefix tracker advances so the next frame emits only
      the next suffix — deltas never overlap or duplicate.
    * Any step reaching a committed state (DONE / non-planner result) → emit its
      committed items via :func:`map_step_to_events`, deduped by
      ``(trajectory_id, step_index)`` so the on-connect snapshot replay and the
      cumulative re-sends do not double-post. For the planner step this is the
      committed ``message`` (its deltas already preceded it, satisfying the
      flush-barrier reconciliation contract); its prefix tracker is then cleared.
    * A WAITING step → handed to the bridge exactly once.

    The stream is consumed and then RE-ENTERED while ``stop`` stays falsy (a real
    connect stream returns when the turn settles, so re-entry resumes live updates
    for the next turn). ``stop`` is consulted once per re-entry, mirroring the
    poll loop. A transport ``httpx.HTTPError`` or a connect-trailer
    ``AntigravityRpcError`` propagates to the caller, which falls back to the poll
    loop.

    :param port: Validated connect-RPC port.
    :param cascade_id: agy cascade id (equal to the conversation id).
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param on_pending_interaction: Async callback for a distinct WAITING
        interaction.
    :param state: Per-run shared trackers (de-dup, turn, interactions, prefixes).
    :param stop: Predicate consulted once per stream (re-)entry; ``True`` exits.
    :returns: None.
    :raises httpx.HTTPError: On a stream transport failure (caller falls back).
    :raises AntigravityRpcError: On a connect end-of-stream trailer error (caller
        falls back).
    """
    while not stop():
        async for frame in stream_agent_state_updates(port, cascade_id):
            for step in _frame_steps(frame):
                await _process_stream_step(
                    step,
                    client=client,
                    session_id=session_id,
                    cascade_id=cascade_id,
                    state=state,
                    on_pending_interaction=on_pending_interaction,
                )


def _frame_steps(frame: dict[str, object]) -> list[dict[str, object]]:
    """
    Extract the trajectory steps from one ``StreamAgentStateUpdates`` frame.

    The steps live at ``mainTrajectoryUpdate.stepsUpdate.steps[]`` (design
    §10.2). A frame without that path (e.g. a non-trajectory update) yields no
    steps. Only dict entries are returned so a malformed step never crashes the
    loop.

    :param frame: One parsed DATA-frame ``update`` dict from the stream.
    :returns: The frame's step dicts (possibly empty).
    """
    main = frame.get("mainTrajectoryUpdate")
    if not isinstance(main, dict):
        return []
    steps_update = main.get("stepsUpdate")
    if not isinstance(steps_update, dict):
        return []
    steps = steps_update.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


async def _process_committed_step(
    step: dict[str, object],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    cascade_id: str,
    state: _ReaderState,
    on_pending_interaction: OnPendingInteraction,
) -> None:
    """
    Emit one step's committed items + status edges + interaction (poll path).

    De-dups by ``(trajectory_id, step_index)`` against ``state.seen`` so a
    re-read posts nothing, then emits via :func:`_emit_step` and hands a WAITING
    step to the bridge once. This is the committed-only path (no deltas).

    De-dup is only RECORDED once the step is *settled* (:func:`_is_settled`): a
    tool-result step is observed non-contiguously through PENDING/RUNNING/WAITING
    before DONE (verified in the live fixtures), and the mapper emits its output
    only at DONE. Marking it ``seen`` on a pre-DONE sighting (where the mapper
    returns ``[]``) would dedup the later DONE and silently DROP the output —
    likelier on the stream path, which observes every intermediate status frame
    than on the coarse poll. So a not-yet-settled step is re-emitted (a safe
    no-op: it maps to ``[]`` and fires no status edge) until it settles, and the
    interaction is still handed off via its own ``interacted`` dedup meanwhile.

    :param step: One RPC step dict.
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param cascade_id: agy cascade id (namespaces ids).
    :param state: Per-run shared trackers.
    :param on_pending_interaction: Async callback for a distinct interaction.
    :returns: None.
    """
    key = _step_key(step)
    if key not in state.seen:
        if _is_settled(step):
            state.seen.add(key)
        state.turn_active = await _emit_step(
            step,
            client=client,
            session_id=session_id,
            cascade_id=cascade_id,
            allocator=state.allocator,
            turn_active=state.turn_active,
        )
        # Telemetry: model-change detection on USER_INPUT (design §10.4).
        await _maybe_emit_model_change(
            step,
            client=client,
            session_id=session_id,
            state=state,
        )
        # Telemetry: token usage on PLANNER_RESPONSE DONE (design §10.3).
        await _maybe_emit_session_usage(
            step,
            client=client,
            session_id=session_id,
            state=state,
        )
    await _maybe_handle_interaction(
        step,
        key=key,
        interacted=state.interacted,
        on_pending_interaction=on_pending_interaction,
    )


def _is_settled(step: dict[str, object]) -> bool:
    """
    Return whether a step has reached a state safe to record as de-duped.

    A step is settled when re-emitting it can produce nothing new, so recording
    its identity in ``seen`` will not drop later content:

    * a USER_INPUT step (always terminal; maps to ``[]`` permanently — the user
      turn is persisted by the direct ``POST /events``);
    * any step whose ``status`` is terminal — DONE (the mapper emits its content)
      or ERROR (the command failed before producing output, so none is coming).

    A PENDING/RUNNING/WAITING/GENERATING step is NOT settled: its content (if
    any) only appears at DONE, so it must stay re-evaluable until then.

    :param step: One RPC step dict.
    :returns: ``True`` when the step is terminal or a USER_INPUT.
    """
    if step.get("type") == _TYPE_USER_INPUT:
        return True
    return step.get("status") in _TERMINAL_STATUSES


async def _process_stream_step(
    step: dict[str, object],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    cascade_id: str,
    state: _ReaderState,
    on_pending_interaction: OnPendingInteraction,
) -> None:
    """
    Emit one streamed step: incremental deltas, then committed items on DONE.

    Dispatch by step ``status`` (design §10.2 discriminator):

    * A GENERATING PLANNER_RESPONSE emits only the NEW ``modifiedResponse``
      suffix as an ``output_text_delta`` (no committed item yet); the step is NOT
      added to ``state.seen`` so its eventual DONE frame still commits.
    * Any other step is routed through the committed path
      (:func:`_process_committed_step`): DONE planner → the committed ``message``
      (deltas already preceded it); tool-result DONE → ``function_call_output``;
      WAITING → the bridge. Committing a planner step clears its delta prefix
      tracker.

    :param step: One RPC step dict from a stream frame.
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param cascade_id: agy cascade id (namespaces ids + message ids).
    :param state: Per-run shared trackers (incl. the per-step prefix tracker).
    :param on_pending_interaction: Async callback for a distinct interaction.
    :returns: None.
    """
    if _is_generating_planner(step):
        await _emit_partial_delta(
            step,
            client=client,
            session_id=session_id,
            cascade_id=cascade_id,
            prefixes=state.prefixes,
        )
        return

    await _process_committed_step(
        step,
        client=client,
        session_id=session_id,
        cascade_id=cascade_id,
        state=state,
        on_pending_interaction=on_pending_interaction,
    )
    # Once committed, the live block is retired by the committed message; drop the
    # prefix tracker so a later same-index step (e.g. an agy timeout-retry reusing
    # the slot) starts a fresh delta stream rather than diffing against stale text.
    idx = _step_index(step)
    if idx is not None:
        state.prefixes.pop(idx, None)


def _is_generating_planner(step: dict[str, object]) -> bool:
    """
    Return whether a step is a PLANNER_RESPONSE still generating its text.

    Only such a step contributes incremental ``output_text_delta`` events; every
    other status/type is handled by the committed path.

    :param step: One RPC step dict.
    :returns: ``True`` for a ``CORTEX_STEP_TYPE_PLANNER_RESPONSE`` whose
        ``status`` is ``CORTEX_STEP_STATUS_GENERATING``.
    """
    return step.get("type") == _TYPE_PLANNER_RESPONSE and step.get("status") == _STATUS_GENERATING


def _partial_planner_text(step: dict[str, object]) -> str | None:
    """
    Extract the growing partial assistant text from a GENERATING planner step.

    The partial lives at ``plannerResponse.modifiedResponse`` (design §10.2);
    ``response`` is absent during generation. Returns ``None`` when the planner
    block or the partial field is missing.

    :param step: A GENERATING PLANNER_RESPONSE step dict.
    :returns: The current cumulative ``modifiedResponse`` text, or ``None``.
    """
    planner = step.get("plannerResponse")
    if not isinstance(planner, dict):
        return None
    modified = planner.get("modifiedResponse")
    return modified if isinstance(modified, str) else None


async def _emit_partial_delta(
    step: dict[str, object],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    cascade_id: str,
    prefixes: dict[int, str],
) -> None:
    """
    Emit the NEW suffix of a GENERATING planner step's partial text as a delta.

    Frames are cumulative snapshots, so the reader prefix-diffs: the delta is
    ``modifiedResponse`` minus the prefix already forwarded for this step's
    ``step_index``. When the new cumulative text does not extend the forwarded
    prefix (a no-growth re-send, or a non-extending rewrite), nothing is emitted.
    The tracker then advances to the full cumulative text so subsequent frames
    emit only further growth — deltas never overlap or duplicate, and they
    concatenate to the full text.

    :param step: A GENERATING PLANNER_RESPONSE step dict.
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param cascade_id: agy cascade id (namespaces the stable message id).
    :param prefixes: Per-``step_index`` forwarded-prefix tracker (mutated here).
    :returns: None.
    """
    idx = _step_index(step)
    if idx is None:
        return
    text = _partial_planner_text(step)
    if text is None:
        return
    forwarded = prefixes.get(idx, "")
    # Only forward growth that extends what we already sent. A frame that does not
    # start with the forwarded prefix (an unexpected non-monotonic rewrite) or
    # that has not grown yields no delta; we still re-anchor the tracker to the
    # latest cumulative text so we never re-emit the overlap.
    if text.startswith(forwarded) and len(text) > len(forwarded):
        suffix = text[len(forwarded) :]
        await _post_event(
            client,
            session_id,
            output_text_delta_event(
                conversation_id=cascade_id,
                step_idx=idx,
                delta=suffix,
                final=False,
            ),
        )
    prefixes[idx] = text


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


async def _maybe_emit_session_usage(
    step: dict[str, object],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    state: _ReaderState,
) -> None:
    """
    Emit ``external_session_usage`` when a PLANNER_RESPONSE DONE carries usage.

    The server treats ``cumulative_input_tokens`` / ``cumulative_output_tokens``
    / ``cumulative_cache_read_input_tokens`` as SET semantics — the posted value
    IS the new session total, and the server prices the per-turn delta as
    (new − old). agy's ``step.metadata.modelUsage`` fields are PER-MODEL-CALL
    (not cumulative), so we accumulate them in ``state`` and emit the running
    totals. This matches codex's behaviour (``tokenUsage.total`` is a cumulative
    thread-wide counter, forwarded as SET values by
    :class:`~omnigent.codex_native_forwarder._SessionUsageCoalescer`).

    NOTE: ``state.cumulative_*`` accumulators are zeroed at reader-run start;
    T-G /clear rotation must also zero them when rotating to a fresh conversation
    so the new session's cost badge starts from 0.

    De-dup is via ``state.seen``: this function is only called inside the
    ``key not in state.seen`` branch of :func:`_process_committed_step`, so
    a replay of the same DONE step (already in ``seen``) never reaches here.

    :param step: One RPC step dict (must be a PLANNER_RESPONSE DONE).
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id.
    :param state: Per-run trackers (accumulators + port + model_catalog).
    :returns: None.
    """
    per_call = _model_usage_from_step(step)
    if not per_call:
        return

    def _int_field(d: dict[str, object], key: str) -> int:
        """Return ``d[key]`` as an int, or 0 when absent / not an int."""
        val = d.get(key, 0)
        return val if isinstance(val, int) else 0

    # Accumulate per-call values into running session totals (SET semantics).
    state.cumulative_input_tokens += _int_field(per_call, "cumulative_input_tokens")
    state.cumulative_output_tokens += _int_field(per_call, "cumulative_output_tokens")
    state.cumulative_cache_read_input_tokens += _int_field(
        per_call, "cumulative_cache_read_input_tokens"
    )
    # Resolve the raw model enum to a displayName if the catalog is available.
    model_enum = per_call.get("model")
    display_name: str | None = None
    if isinstance(model_enum, str) and model_enum:
        catalog = await _ensure_catalog(state)
        display_name = _resolve_display_name(model_enum, catalog)
    # Build the cumulative payload (SET-semantics running totals).
    payload: dict[str, object] = {}
    if state.cumulative_input_tokens > 0:
        payload["cumulative_input_tokens"] = state.cumulative_input_tokens
    if state.cumulative_output_tokens > 0:
        payload["cumulative_output_tokens"] = state.cumulative_output_tokens
    if state.cumulative_cache_read_input_tokens > 0:
        payload["cumulative_cache_read_input_tokens"] = state.cumulative_cache_read_input_tokens
    if display_name is not None:
        payload["model"] = display_name
    if not payload:
        return
    step_idx = _step_index(step) or 0
    await _post_event(
        client,
        session_id,
        OutboundEvent(
            event_type=_EXTERNAL_SESSION_USAGE,
            data=payload,
            step_index=step_idx,
        ),
    )


async def _maybe_emit_model_change(
    step: dict[str, object],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    state: _ReaderState,
) -> None:
    """
    Emit ``external_model_change`` when a USER_INPUT step carries a new model enum.

    Tracks the per-run ``state.posted_model_enum`` baseline; emits only when the
    turn's requested model differs from the last-emitted enum (design §10.4).
    Effort is encoded in the model enum (no separate field), so one change event
    covers both. The catalog is fetched once per run and cached in ``state``.

    De-dup: this function is only called inside the ``key not in state.seen``
    branch, so a replayed USER_INPUT (already in ``seen``) never reaches here.
    The enum-vs-posted_enum comparison further deduplicates same-model turns.

    :param step: One RPC step dict (must be a USER_INPUT).
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id.
    :param state: Per-run trackers (posted_model_enum + model_catalog + port).
    :returns: None.
    """
    model_enum = _requested_model_enum_from_step(step)
    if model_enum is None:
        return
    if model_enum == state.posted_model_enum:
        return
    catalog = await _ensure_catalog(state)
    display_name = _resolve_display_name(model_enum, catalog)
    step_idx = _step_index(step) or 0
    await _post_event(
        client,
        session_id,
        OutboundEvent(
            event_type=_EXTERNAL_MODEL_CHANGE,
            data={"model": display_name},
            step_index=step_idx,
        ),
    )
    state.posted_model_enum = model_enum


async def _ensure_catalog(state: _ReaderState) -> dict[str, object]:
    """
    Return the cached ``GetAvailableModels`` catalog, fetching it when needed.

    Fetched at most once per reader run; stored in ``state.model_catalog``.
    Falls back to an empty dict on error so a catalog failure never kills the
    telemetry path (the display-name resolver falls back to the raw enum).

    :param state: Per-run shared trackers.
    :returns: The catalog dict (possibly empty on fetch failure).
    """
    if state.model_catalog is not None:
        return state.model_catalog
    try:
        catalog = await asyncio.to_thread(get_available_models, state.port)
    except Exception:
        _logger.warning(
            "agy RPC reader: GetAvailableModels failed; "
            "model display names will fall back to raw enums",
            exc_info=True,
        )
        catalog = {}
    state.model_catalog = catalog
    return catalog
