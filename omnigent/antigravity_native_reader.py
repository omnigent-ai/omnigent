"""RPC read driver for a native Antigravity (agy) session.

This is the read-path driver that replaced the retired transcript-tail forwarder
(``omnigent.antigravity_native_forwarder``, deleted in the Task 12 cutover).
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
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from omnigent._native_post_delivery import post_session_event_with_retry
from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    AntigravityNativeBridgeState,
    is_placeholder_conversation_id,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.antigravity_native_rpc import (
    AntigravityRpcError,
    _candidate_agy_rpc_ports,
    _conversation_matches,
    get_all_cascade_trajectories,
    get_available_models,
    get_trajectory_steps,
    stream_agent_state_updates,
)

# ``OutboundEvent`` + ``_ToolCallIdAllocator`` live in the mapper module since the
# Task 12 cutover (relocated from the retired transcript forwarder). The reader
# reuses the SAME event shape and allocator so the mapped events post identically.
from omnigent.antigravity_native_steps import (
    OutboundEvent,
    PendingInteraction,
    _execution_discriminator,
    _step_index,
    _ToolCallIdAllocator,
    _trajectory_id,
    map_step_to_events,
    output_reasoning_delta_event,
    output_text_delta_event,
    pending_interaction,
)
from omnigent.claude_native_bridge import url_component
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

_logger = logging.getLogger(__name__)

# Default seconds between RPC polls. The RPC returns a full snapshot each call
# and steps finalize only at DONE (no token streaming), so a sub-second cadence
# keeps the mirror responsive without hammering the loopback server.
_DEFAULT_POLL_INTERVAL_S = 0.25

# Default seconds between Task T-G ``/clear``-rotation checks
# (``GetAllCascadeTrajectories``). Coarse on purpose: a ``/clear`` is a rare,
# human-initiated event, so a few seconds of detection latency is fine and keeps
# this off the hot path — far slower than the per-turn poll cadence so it does not
# hammer the loopback RPC.
_DEFAULT_ROTATION_INTERVAL_S = 3.0

# Backoff between connect-stream re-entries in :func:`_stream_loop`. In steady
# state the re-opened stream blocks awaiting frames, so this delay is paid only
# once per turn-settle (negligible). It bounds a busy-spin if agy ever returns an
# immediate clean trailer repeatedly (plausible right after cold-start before any
# turn, or a transient non-streamable state), which would otherwise re-POST the
# stream at zero delay and pin a CPU — the poll fallback only triggers on an
# exception, never a clean immediate return.
_STREAM_REENTRY_BACKOFF_S = 0.5

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

# Root-cascade trajectory type in ``GetAllCascadeTrajectories`` summaries. Only a
# root cascade is a rotation candidate — a subagent/child trajectory carries a
# different ``trajectoryType`` and must never be rotated to (its turns are not the
# user's top-level conversation). Live-verified value (agy 1.0.10; see the
# ``get_all_cascade_trajectories`` capture).
_TRAJECTORY_TYPE_CASCADE = "CORTEX_TRAJECTORY_TYPE_CASCADE"

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
# pairing it with ``trajectory_id`` keeps the key stable per step. The third
# element is a per-turn discriminator used ONLY for a step that has no
# ``step_index``: a USER_INPUT step has a per-conversation-stable
# ``trajectory_id`` and no index, so ``(trajectory_id, None)`` would collide
# across every turn and silently dedup turns ≥2 — folding ``executionId`` /
# ``createdAt`` in keeps each turn distinct (see :func:`_step_key`).
_StepKey = tuple[str | None, int | None, str | None]

# Telemetry event types (design §10.3 + §10.4).
_EXTERNAL_SESSION_USAGE = "external_session_usage"
_EXTERNAL_MODEL_CHANGE = "external_model_change"

# Async callback handed each distinct WAITING interaction. It receives the SAME
# ``cascade_id`` + connect-RPC ``port`` the reader discovered and is using, so the
# Task 8 interaction bridge it drives targets agy's live conversation WITHOUT
# re-discovering them (re-discovery could bind a recycled/foreign port). Args:
# ``(cascade_id, port, pending)``.
OnPendingInteraction = Callable[[str, int, PendingInteraction], Awaitable[None]]
StopPredicate = Callable[[], bool]

# Elicitation hook long-poll budget (shared by the runner + CLI reader wiring).
# The ``antigravity-elicitation-request`` hook is a request/reply that blocks on a
# human, so the request timeout is intentionally long (just over a day); a severed
# long-poll (proxy idle cut, restarting server) is re-POSTed within this budget so
# the SAME elicitation re-parks server-side rather than abandoning the approval
# card. The first retry lands inside the server's re-park grace; later retries back
# off. Mirrors the codex forwarder's elicitation re-POST policy.
_AGY_ELICITATION_REQUEST_TIMEOUT_SECONDS = 86405.0
_AGY_ELICITATION_CONNECT_TIMEOUT_SECONDS = 30.0
_AGY_ELICITATION_RETRY_INITIAL_BACKOFF_SECONDS = 1.0
_AGY_ELICITATION_RETRY_MAX_BACKOFF_SECONDS = 30.0

# Omnigent client timeout for the reader's event-POST + telemetry traffic (NOT the
# elicitation long-poll, which sets its own per-request timeout above).
_READER_CLIENT_TIMEOUT_SECONDS = 30.0


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

    The live wire (agy 1.0.10) carries the enum as a STRING at
    ``step.userInput.userConfig.plannerConfig.planModel`` (design §10.4) — the
    same field :func:`send_user_cascade_message` sends. A TUI-origin step using
    the older ``requestedModel.model`` (dict) shape is supported as a fallback.
    Returns ``None`` when the field is absent or the step is not a USER_INPUT.

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
    plan_model = planner_config.get("planModel")
    if isinstance(plan_model, str) and plan_model:
        return plan_model
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


def _parse_activity_timestamp(value: object) -> datetime | None:
    """
    Parse one ISO-8601 activity timestamp from a cascade summary, robustly.

    agy emits ``lastUserInputTime`` / ``lastModifiedTime`` as ISO-8601 strings
    with a trailing ``Z`` (UTC), e.g. ``"2026-06-23T17:50:29.232919Z"``.
    :func:`datetime.fromisoformat` did not accept a literal ``Z`` before Python
    3.11, so the ``Z`` is normalised to ``+00:00`` first. A naive parse (no
    offset, e.g. a future agy dropping the suffix) is pinned to UTC so all
    comparisons in :func:`_detect_rotated_cascade` are offset-aware and never
    raise ``TypeError`` on a naive/aware mix.

    A missing (``None``), non-string, or malformed value yields ``None`` — the
    caller treats such an entry as having NO activity (not a rotation candidate,
    and a bound entry with no parseable activity blocks rotation), so a parse
    failure can never spuriously rotate.

    :param value: The raw timestamp field from a trajectory summary.
    :returns: An offset-aware UTC :class:`datetime`, or ``None`` when absent or
        unparseable.
    """
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _summary_activity(summary: dict[str, object]) -> datetime | None:
    """
    Return a cascade summary's newest activity time, or ``None`` if never used.

    Activity is the user's last turn (``lastUserInputTime``) when present, else
    the cascade's ``lastModifiedTime``. A freshly ``/clear``-minted cascade has
    BOTH absent (``null``) until its first turn runs, so it reports ``None`` and
    is not yet a rotation candidate — the rotation only fires once the new
    conversation has actually been used, never on the bare mint.

    :param summary: One ``trajectorySummaries`` entry.
    :returns: The newest offset-aware activity :class:`datetime`, or ``None``
        when the cascade has no parseable activity timestamp yet.
    """
    return _parse_activity_timestamp(
        summary.get("lastUserInputTime")
    ) or _parse_activity_timestamp(summary.get("lastModifiedTime"))


def _detect_rotated_cascade(summaries: dict[str, object], bound_cascade_id: str) -> str | None:
    """
    Return the id of a newer-active root cascade than the bound one, else ``None``.

    The pure core of Task T-G ``/clear``-rotation detection. A TUI ``/clear`` mints
    a NEW root cascade and leaves the bound one idle; the new cascade is invisible
    to the bound ``StreamAgentStateUpdates`` stream (bound to one cascade) but
    appears here in ``GetAllCascadeTrajectories`` as a more-recently-active sibling.

    Selection:

    * Consider ONLY root cascades (``trajectoryType ==
      CORTEX_TRAJECTORY_TYPE_CASCADE``) — a subagent/child trajectory is never a
      rotation target (rotating to it would mirror a sub-conversation, not the
      user's top-level one).
    * The "current" cascade is the one with the newest activity timestamp
      (:func:`_summary_activity`: ``lastUserInputTime`` preferred, else
      ``lastModifiedTime``). An entry with NO parseable activity (a bare ``/clear``
      mint, both timestamps ``null``) is not a candidate.
    * Rotate ONLY when the current cascade is BOTH a different id than the bound
      one AND strictly more recently active than the bound cascade's OWN activity.
      The bound entry's activity is looked up from ``summaries`` itself; if the
      bound entry is absent (we cannot prove the bound conversation is staler), we
      do NOT rotate — returning ``None`` keeps the reader on its current binding
      rather than chasing a sibling on incomplete information.

    Deterministic on ties: a sibling whose activity merely EQUALS the bound
    cascade's (not strictly newer) is not a rotation, so a steady state never
    flaps. Selection of the newest among multiple siblings is by ``>`` so the
    single most-recent wins.

    :param summaries: The ``trajectorySummaries`` map from
        :func:`~omnigent.antigravity_native_rpc.get_all_cascade_trajectories`
        (keyed by root conversation id).
    :param bound_cascade_id: The conversation id this reader is currently bound to.
    :returns: The newer-active root cascade's id when a rotation is warranted,
        else ``None``.
    """
    bound_summary = summaries.get(bound_cascade_id)
    if not isinstance(bound_summary, dict):
        # The bound conversation is not in the summary map — we cannot prove it is
        # staler than any sibling, so we must not rotate blindly. Stay bound.
        return None
    bound_activity = _summary_activity(bound_summary)

    best_id: str | None = None
    best_activity: datetime | None = None
    for cascade_id, summary in summaries.items():
        if not isinstance(summary, dict):
            continue
        if summary.get("trajectoryType") != _TRAJECTORY_TYPE_CASCADE:
            continue  # never rotate to a subagent/child trajectory
        if cascade_id == bound_cascade_id:
            continue
        activity = _summary_activity(summary)
        if activity is None:
            continue  # a never-used (e.g. bare /clear-minted) cascade is not current
        if best_activity is None or activity > best_activity:
            best_id, best_activity = cascade_id, activity

    if best_id is None or best_activity is None:
        return None
    # Rotate only when the most-recent sibling is STRICTLY newer than the bound
    # cascade's own activity. A bound cascade that itself has no parseable activity
    # (bound_activity is None) is treated as the oldest, so any active sibling wins
    # — this is the /clear-before-first-turn case: a freshly-bound cascade that
    # never took a turn, then a sibling the user actually used, MUST rotate (else
    # the reader stays on the dead pre-/clear cascade). Siblings with no activity
    # are already excluded above, so this only fires for a genuinely-active sibling.
    if bound_activity is None or best_activity > bound_activity:
        return best_id
    return None


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
    :returns: A ``(trajectory_id, step_index, discriminator)`` identity tuple.
        The first two elements may be ``None``; the third is ``None`` for any
        step that has a ``step_index`` (its ``(trajectory_id, step_index)``
        pair is already unique) and a per-turn-unique discriminator only for a
        step that lacks one.
    """
    idx = _step_index(step)
    # A step WITH a step_index keeps its (trajectory_id, step_index) identity
    # (discriminator None) — unchanged dedup. A step withOUT one (USER_INPUT)
    # would otherwise collide on (trajectory_id, None) across every turn and be
    # silently de-duped after turn 1, dropping per-turn status + model-change;
    # fold a per-turn-unique discriminator so each turn keys distinctly.
    discriminator = None if idx is not None else _execution_discriminator(step)
    return (_trajectory_id(step), idx, discriminator)


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
    :returns: ``True`` when the step is a DONE PLANNER_RESPONSE with non-empty
        text and an empty/absent ``toolCalls`` list.
    """
    # The turn-close edge must fire only on the DONE closing step. A GENERATING
    # planner frame already carries growing ``modifiedResponse`` text with no
    # ``toolCalls`` yet, so without this gate ``_emit_step`` would fire the IDLE
    # status edge mid-response (the spinner closes early) on the stream path.
    if step.get("status") != _STATUS_DONE:
        return False
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


async def _watch_for_rotation(
    *,
    port: int,
    bound_cascade_id: str,
    interval_s: float,
    skip_cascade_ids: frozenset[str],
    on_rotation: Callable[[str], None],
) -> None:
    """
    Poll ``GetAllCascadeTrajectories`` for a ``/clear`` rotation, then signal once.

    The Task T-G rotation DETECTOR, run as a background task alongside the
    stream/poll reader body (which is bound to one cascade and so cannot itself
    observe a sibling conversation). Every ``interval_s`` it fetches the cascade
    summaries and asks :func:`_detect_rotated_cascade` whether a newer-active root
    cascade exists; the FIRST time one does, it invokes ``on_rotation`` with the
    new cascade id and returns (one rotation per detector — the caller tears the
    reader body down, rotates the session, and starts a fresh detector on rebind).

    ``skip_cascade_ids`` are cascades a PRIOR rotation attempt already failed to
    bind (e.g. the Omnigent session-create errored); ignoring them here is what
    prevents a hot re-detect/re-fail loop when rotation cannot complete — the
    reader then keeps serving the old binding without this detector flapping.

    Termination is by CANCELLATION only (``supervise_reader``'s ``finally`` always
    cancels this task): the detector deliberately does NOT consult the reader's
    ``stop`` predicate, so it never races the bounded body loop's shared
    iteration counter under test. Best-effort: a fetch failure
    (``httpx.HTTPError`` / ``ValueError``) is logged and skipped (the next tick
    retries) so a transient agy fault never kills the detector.

    :param port: Validated connect-RPC port (the bound reader's port).
    :param bound_cascade_id: The cascade id the reader is currently bound to.
    :param interval_s: Seconds between rotation checks (kept coarse — a few seconds
        — so the loopback RPC is not hammered).
    :param skip_cascade_ids: Cascade ids a prior rotation attempt failed to bind;
        never re-signalled.
    :param on_rotation: Called once with the detected new cascade id.
    :returns: None.
    """
    while True:
        await _sleep(interval_s)
        try:
            body = await asyncio.to_thread(get_all_cascade_trajectories, port)
        except (httpx.HTTPError, ValueError) as exc:
            _logger.warning(
                "agy rotation detector: GetAllCascadeTrajectories failed; retrying: "
                "bound_cascade=%s port=%s error=%r",
                bound_cascade_id,
                port,
                exc,
            )
            continue
        summaries = body.get("trajectorySummaries")
        if not isinstance(summaries, dict):
            continue
        new_cascade_id = _detect_rotated_cascade(summaries, bound_cascade_id)
        if new_cascade_id is None or new_cascade_id in skip_cascade_ids:
            continue
        _logger.info(
            "agy rotation detector: bound conversation %s was rotated away (newer-active "
            "cascade %s, likely a TUI /clear); signalling rebind",
            bound_cascade_id,
            new_cascade_id,
        )
        on_rotation(new_cascade_id)
        return


async def supervise_reader(
    bridge_dir: Path,
    session_id: str,
    *,
    client: httpx.AsyncClient,
    on_pending_interaction: OnPendingInteraction,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    stop: StopPredicate | None = None,
    detect_rotation_interval_s: float = _DEFAULT_ROTATION_INTERVAL_S,
    skip_cascade_ids: frozenset[str] = frozenset(),
) -> str | None:
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

    Task T-G ``/clear`` rotation: the stream/poll reader body runs as a CANCELLABLE
    task and, ALONGSIDE it, a :func:`_watch_for_rotation` background task polls
    ``GetAllCascadeTrajectories`` every ``detect_rotation_interval_s`` (the stream
    is bound to one cascade and cannot see a sibling). When it detects a
    newer-active root cascade (a TUI ``/clear`` mints one) it records the new id and
    CANCELS the body task — necessary because after a ``/clear`` the bound cascade
    goes idle and the stream blocks inside a deadline-less ``aiter_bytes`` read,
    where the cooperative ``stop`` checkpoint is never reached; cancellation
    interrupts that wedged read. This function then RETURNS the new cascade id so
    :func:`run_reader_with_bridge` can rotate the Omnigent session and rebind. When
    the body ends on its own (the ``stop`` predicate fired, or the stream+poll both
    exited) it returns ``None``; an EXTERNAL cancellation of this coroutine (no
    rotation recorded) propagates rather than being mistaken for a rotation.

    :param bridge_dir: Native Antigravity bridge directory (identifies the
        session whose agy conversation to mirror).
    :param session_id: Omnigent conversation id to mirror into, e.g.
        ``"conv_abc123"``.
    :param client: HTTP client for Omnigent event posts.
    :param on_pending_interaction: Async callback handed each distinct WAITING
        interaction (the Task 8 interaction bridge), as
        ``(cascade_id, port, pending)`` — the SAME cascade id + connect-RPC port
        the reader discovered, so the bridge targets agy's live conversation
        without re-discovering. Invoked at most once per
        ``(trajectory_id, step_index)``.
    :param poll_interval_s: Seconds between RPC polls (and discovery polls).
    :param stop: Optional predicate consulted once per loop iteration; when it
        returns ``True`` the loop exits. ``None`` (production) loops until the
        task is cancelled. Provided so tests drive a bounded number of
        iterations.
    :param detect_rotation_interval_s: Seconds between ``/clear``-rotation checks
        (kept coarse so the loopback RPC is not hammered).
    :param skip_cascade_ids: Cascade ids a prior rotation attempt failed to bind;
        the rotation detector never re-signals them (prevents a hot
        detect/rotate-fail loop — the reader keeps the old binding instead).
    :returns: The detected new cascade id when a ``/clear`` rotation was observed
        (the caller rotates + rebinds), or ``None`` when the reader body ended on
        its own (``stop`` fired / both paths exited).
    """
    should_stop: StopPredicate = stop if stop is not None else (lambda: False)

    discovered = await _discover(bridge_dir, poll_interval_s=poll_interval_s, stop=should_stop)
    if discovered is None:
        return None
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

    # Task T-G: the rotation detector records the new cascade id here and the
    # ``stop`` predicate the reader body consults flips to True so the body exits
    # cleanly at its next checkpoint (between stream re-entries / poll iterations).
    # That checkpoint, however, is never reached while the stream is wedged on a
    # deadline-less idle read (after a ``/clear`` the bound cascade goes IDLE and
    # ``aiter_bytes`` blocks with no frame and no trailer), so the detector ALSO
    # cancels the body task below — cancellation interrupts the wedged read where a
    # cooperative ``stop`` re-check cannot. One-shot per supervise_reader run; the
    # caller rebinds with a fresh detector.
    rotation_holder: list[str] = []

    # The reader body runs as a cancellable task (created below) so a detected
    # rotation can interrupt a stream blocked inside ``aiter_bytes`` — a plain
    # ``await`` of the body would never return, since neither ``_stream_loop``'s
    # outer ``while`` nor its post-``async for`` ``stop`` checkpoint is reached
    # while the read is wedged. ``_on_rotation`` references the task through this
    # holder so it is safe even though the task is created AFTER the detector's
    # callback is defined (the detector cannot fire before its task is scheduled
    # and runs its first ``await``, by which point ``body_holder`` is populated).
    body_holder: list[asyncio.Task[None]] = []

    def _on_rotation(new_cascade_id: str) -> None:
        if not rotation_holder:
            rotation_holder.append(new_cascade_id)
            # Interrupt a stream wedged on a deadline-less idle read; a cooperative
            # ``_body_should_stop`` re-check would never run while it blocks.
            if body_holder:
                body_holder[0].cancel()

    def _body_should_stop() -> bool:
        # The reader body stops either on the caller's stop OR once a rotation was
        # detected (so it does not keep mirroring the now-dead conversation). This
        # covers the cooperative exits (between stream re-entries / poll iterations);
        # a stream blocked mid-read is interrupted by ``_on_rotation``'s cancel.
        return should_stop() or bool(rotation_holder)

    async def _run_body() -> None:
        # STREAM-primary (Task T-D): consume the connect server-stream for live
        # ``output_text_delta`` typing parity. On a stream error (transport
        # ``httpx.HTTPError`` or a connect-trailer ``AntigravityRpcError``) fall
        # back to the committed-only poll loop — graceful degradation to Phase-1
        # behaviour — rather than letting the error kill the reader. The shared
        # ``state`` makes the fallback idempotent against whatever the stream
        # already delivered. Both paths consult ``_body_should_stop`` so a detected
        # rotation ends them cooperatively; a rotation that lands while the stream
        # is blocked instead cancels this task.
        try:
            await _stream_loop(
                port=port,
                cascade_id=cascade_id,
                client=client,
                session_id=session_id,
                on_pending_interaction=on_pending_interaction,
                state=state,
                stop=_body_should_stop,
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
                stop=_body_should_stop,
            )

    body_task = asyncio.create_task(_run_body(), name="antigravity-reader-body")
    body_holder.append(body_task)

    # Started AFTER ``body_task`` exists so ``_on_rotation`` can never fire before
    # the task is available to cancel (the holder is already populated).
    rotation_task = asyncio.create_task(
        _watch_for_rotation(
            port=port,
            bound_cascade_id=cascade_id,
            interval_s=detect_rotation_interval_s,
            skip_cascade_ids=skip_cascade_ids,
            on_rotation=_on_rotation,
        ),
        name="antigravity-rotation-detector",
    )

    try:
        await body_task
    except asyncio.CancelledError:
        # A rotation cancels the body on purpose (``_on_rotation`` set
        # ``rotation_holder`` before cancelling), so fall through to return the new
        # cascade id. An EXTERNAL cancellation of ``supervise_reader`` (shutdown)
        # arrives with NO rotation recorded — re-raise so it propagates rather than
        # being swallowed as a phantom rotation.
        if not rotation_holder:
            raise
    finally:
        # Stop the rotation detector (it may still be sleeping between ticks) and
        # the off-loop interaction bridge before returning, so a cancelled/stopped
        # reader leaks neither task. Order: detector first (it owns no human-facing
        # state), then the interaction task (may be awaiting a day-long verdict).
        rotation_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rotation_task
        # Ensure the body task is finalized on EVERY exit path (notably an external
        # cancel, where ``await body_task`` re-raised above before it could be
        # awaited to completion) so no task leaks. A rotation/normal exit leaves it
        # already done, making this a no-op.
        if not body_task.done():
            body_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await body_task
        active = state.interaction_task
        if active is not None and not active.done():
            active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active

    return rotation_holder[0] if rotation_holder else None


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
    :param seen: :func:`_step_key` identities whose COMMITTED items have been
        posted, so the on-connect snapshot replay (and steady-state re-reads)
        post nothing.
    :param interacted: Identities whose WAITING interaction was already handed to
        the bridge, so a re-sent WAITING frame does not re-fire it.
    :param prefixes: Per-PLANNER ``step_index`` → the length of ``modifiedResponse``
        already forwarded as deltas, so each frame emits only the NEW suffix.
        Cleared for a step when its committed ``message`` is posted (stream path
        only; the poll path never populates it).
    :param reasoning_prefixes: Per-PLANNER ``step_index`` → the ``thinking`` text
        already forwarded as reasoning deltas, mirroring ``prefixes`` for the
        reasoning stream (design §10.2). A step's first entry doubles as the
        "reasoning started" marker: absent ⇒ the next reasoning delta is the
        step's first (carries ``started=True``). Cleared with ``prefixes`` when the
        committed ``message`` is posted (stream path only).
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
        Reset to 0 at the start of each reader run; a T-G /clear rotation rebinds
        with a FRESH ``_ReaderState`` (``supervise_reader`` is re-entered), so the
        new conversation's cost badge starts from 0 automatically.
    :param cumulative_output_tokens: Running session total of output tokens.
    :param cumulative_cache_read_input_tokens: Running session total of
        cache-read input tokens.
    :param interaction_task: The single in-flight interaction-bridge task, or
        ``None`` when none is running. The bridge runs OFF the reader loop so
        streaming/mirroring continues while a human answers (a long-poll can last
        a day); at most one runs at a time because the in-flight bridge owns agy's
        WAITING-timeout retries (a second would double-fire). Cancelled on reader
        teardown.
    """

    allocator: _ToolCallIdAllocator
    seen: set[_StepKey]
    interacted: set[_StepKey]
    prefixes: dict[int, str] = field(default_factory=dict)
    reasoning_prefixes: dict[int, str] = field(default_factory=dict)
    turn_active: bool = False
    posted_model_enum: str | None = None
    model_catalog: dict[str, object] | None = None
    port: int = 0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_cache_read_input_tokens: int = 0
    interaction_task: asyncio.Task[None] | None = None


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
    poll loop, and a small :data:`_STREAM_REENTRY_BACKOFF_S` sleep separates
    re-entries so an immediate clean trailer cannot busy-spin re-POSTing the
    stream at zero delay (the steady-state stream blocks awaiting frames, so the
    delay is paid only once per turn-settle). A transport ``httpx.HTTPError`` or a
    connect-trailer ``AntigravityRpcError`` propagates to the caller, which falls
    back to the poll loop.

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
            # NOTE on /clear rotation: this stream is bound to ONE cascade and only
            # ever reports THAT cascade's id, so it can NEVER observe a sibling
            # conversation — a per-frame "did the conversation change?" guard here
            # would be a guaranteed no-op. ``/clear`` rotation is handled OUT OF
            # BAND by :func:`_watch_for_rotation` (via ``GetAllCascadeTrajectories``,
            # which lists every live root cascade); on detection ``supervise_reader``
            # flips this loop's ``stop`` and returns the new cascade id so
            # :func:`run_reader_with_bridge` rotates the Omnigent session + rebinds.
            for step in _frame_steps(frame):
                await _process_stream_step(
                    step,
                    client=client,
                    session_id=session_id,
                    cascade_id=cascade_id,
                    state=state,
                    on_pending_interaction=on_pending_interaction,
                )
        # Backoff before re-opening the stream so an immediate clean trailer
        # (no frames) cannot busy-spin re-POSTing at zero delay. Skipped when
        # asked to stop so a shutdown is not delayed by the settle backoff.
        if not stop():
            await _sleep(_STREAM_REENTRY_BACKOFF_S)


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

    De-dups by :func:`_step_key` against ``state.seen`` so a re-read posts
    nothing, then emits via :func:`_emit_step` and hands a WAITING step to the
    bridge once. This is the committed-only path (no deltas).

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
    _maybe_handle_interaction(
        step,
        key=key,
        cascade_id=cascade_id,
        state=state,
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

    * A GENERATING PLANNER_RESPONSE emits the NEW suffix of its growing reasoning
      (``plannerResponse.thinking``) as an ``output_reasoning_delta`` and then the
      NEW suffix of its growing text (``modifiedResponse``) as an
      ``output_text_delta`` — reasoning FIRST (§10.2: thinking streams before the
      response). No committed item yet; the step is NOT added to ``state.seen`` so
      its eventual DONE frame still commits.
    * Any other step is routed through the committed path
      (:func:`_process_committed_step`): DONE planner → the committed ``message``
      (deltas already preceded it); tool-result DONE → ``function_call_output``;
      WAITING → the bridge. Committing a planner step clears its text + reasoning
      prefix trackers.

    :param step: One RPC step dict from a stream frame.
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param cascade_id: agy cascade id (namespaces ids + message ids).
    :param state: Per-run shared trackers (incl. the per-step prefix trackers).
    :param on_pending_interaction: Async callback for a distinct interaction.
    :returns: None.
    """
    if _is_generating_planner(step):
        # Reasoning precedes the response (§10.2), so emit its delta first.
        await _emit_partial_reasoning_delta(
            step,
            client=client,
            session_id=session_id,
            reasoning_prefixes=state.reasoning_prefixes,
        )
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
    # Once committed, the live block is retired by the committed message; drop both
    # prefix trackers so a later same-index step (e.g. an agy timeout-retry reusing
    # the slot) starts a fresh delta stream rather than diffing against stale text.
    idx = _step_index(step)
    if idx is not None:
        state.prefixes.pop(idx, None)
        state.reasoning_prefixes.pop(idx, None)


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


def _partial_planner_thinking(step: dict[str, object]) -> str | None:
    """
    Extract the growing reasoning text from a GENERATING planner step.

    Gemini Thinking-model variants stream chain-of-thought at
    ``plannerResponse.thinking`` (design §10.2), which grows across frames like
    ``modifiedResponse``. Non-thinking models omit the field. Returns ``None``
    when the planner block or the ``thinking`` field is missing (no reasoning to
    surface — the no-regression case for plain text streaming).

    :param step: A GENERATING PLANNER_RESPONSE step dict.
    :returns: The current cumulative ``thinking`` text, or ``None``.
    """
    planner = step.get("plannerResponse")
    if not isinstance(planner, dict):
        return None
    thinking = planner.get("thinking")
    return thinking if isinstance(thinking, str) else None


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


async def _emit_partial_reasoning_delta(
    step: dict[str, object],
    *,
    client: httpx.AsyncClient,
    session_id: str,
    reasoning_prefixes: dict[int, str],
) -> None:
    """
    Emit the NEW suffix of a GENERATING planner step's ``thinking`` as a delta.

    The reasoning analogue of :func:`_emit_partial_delta`: ``thinking`` is a
    cumulative snapshot per frame, so the reader prefix-diffs against the suffix
    already forwarded for this step's ``step_index`` and emits only the growth.
    A no-growth re-send (or a non-extending rewrite) emits nothing; the tracker
    still advances so deltas never overlap and concatenate to the full reasoning.

    The step's FIRST reasoning delta carries ``started=True`` (keyed off the
    prefix tracker being absent for the step) so the server precedes it with one
    ``response.reasoning.started``; later deltas pass ``False``. A planner with no
    ``thinking`` field never enters the emit branch — no reasoning events, no
    regression to the text stream. (No cascade id is needed: the SPA reasoning
    block is not keyed by a per-step id the way the text deltas' ``message_id``
    is — see :func:`output_reasoning_delta_event`.)

    :param step: A GENERATING PLANNER_RESPONSE step dict.
    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id to mirror into.
    :param reasoning_prefixes: Per-``step_index`` forwarded-prefix tracker
        (mutated here); a step's first emitted delta also records its membership.
    :returns: None.
    """
    idx = _step_index(step)
    if idx is None:
        return
    text = _partial_planner_thinking(step)
    if text is None:
        return
    # Absent ⇒ this is the step's first reasoning delta (carries ``started``).
    started = idx not in reasoning_prefixes
    forwarded = reasoning_prefixes.get(idx, "")
    # Mirror the text path: only forward growth that extends the forwarded prefix.
    # A no-growth / non-extending frame emits nothing but still re-anchors the
    # tracker. ``started`` is only consumed when an actual delta is emitted, so a
    # first frame with empty ``thinking`` does not waste the marker.
    if text.startswith(forwarded) and len(text) > len(forwarded):
        suffix = text[len(forwarded) :]
        await _post_event(
            client,
            session_id,
            output_reasoning_delta_event(
                step_idx=idx,
                delta=suffix,
                started=started,
            ),
        )
    # Re-anchor unconditionally (mirrors the text path's ``prefixes[idx] = text``):
    # a non-monotonic rewrite emits no delta but must still advance the tracker, or
    # reasoning deltas freeze permanently for this step once a rewrite occurs.
    reasoning_prefixes[idx] = text


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


def _maybe_handle_interaction(
    step: dict[str, object],
    *,
    key: _StepKey,
    cascade_id: str,
    state: _ReaderState,
    on_pending_interaction: OnPendingInteraction,
) -> None:
    """
    Spawn the bridge for a WAITING step's interaction OFF the reader loop.

    A non-WAITING step yields no interaction. A WAITING step is handed to the
    callback only the first time its ``(trajectory_id, step_index)`` is seen as
    pending, so a re-read of the same WAITING snapshot does not re-fire it.

    The callback (the Task 8 bridge) runs as a tracked background task rather than
    inline so the reader keeps streaming/mirroring while a human answers — the
    elicitation long-poll can last up to a day, and an inline ``await`` here would
    freeze the whole stream/poll loop for that duration (no deltas, no tool-output,
    no status edges, and the idle HTTP stream could be severed).

    SINGLE-IN-FLIGHT GUARD: at most one interaction task runs at a time. agy
    re-issues a timed-out WAITING step at a HIGHER ``step_index``; the in-flight
    ``bridge_interaction`` already owns those retries via its own freshest-WAITING
    re-read, so spawning a second task for a retry step would surface a duplicate
    elicitation and a competing delivery. Subsequent WAITING steps are skipped
    while a task is active; its done-callback then clears the slot so a genuinely
    new later interaction can fire.

    The callback gets the SAME ``cascade_id`` + ``port`` (from ``state``) the
    reader discovered, so the bridge targets agy's live conversation without
    re-discovering (which could bind a recycled/foreign port).

    :param step: One new RPC step dict.
    :param key: The step's identity key (already computed by the caller).
    :param cascade_id: agy cascade id (equal to the conversation id) bound here.
    :param state: Per-run reader state — ``interacted`` (dedup) and the single
        ``interaction_task`` slot are mutated here; ``port`` is read.
    :param on_pending_interaction: Async callback for a distinct interaction.
    :returns: None.
    """
    pending = pending_interaction(step)
    if pending is None:
        return
    if key in state.interacted:
        return
    active = state.interaction_task
    if active is not None and not active.done():
        # An interaction is already being handled off-loop; its bridge owns agy's
        # WAITING-timeout retries, so don't double-fire on the retry step.
        return
    state.interacted.add(key)

    async def _run_bridge() -> None:
        await on_pending_interaction(cascade_id, state.port, pending)

    def _clear_slot(completed: asyncio.Task[None]) -> None:
        if state.interaction_task is completed:
            state.interaction_task = None
        if not completed.cancelled():
            exc = completed.exception()
            if exc is not None:
                _logger.warning(
                    "agy interaction bridge task failed (cascade=%s): %r",
                    cascade_id,
                    exc,
                )

    task = asyncio.create_task(_run_bridge(), name="antigravity-interaction-bridge")
    state.interaction_task = task
    task.add_done_callback(_clear_slot)


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

    NOTE: ``state.cumulative_*`` accumulators are zeroed at reader-run start; a
    T-G /clear rotation rebinds with a fresh ``_ReaderState`` (``supervise_reader``
    is re-entered), so the new session's cost badge starts from 0 automatically.

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


# ── Shared reader wiring (elicitation bridge + supervise_reader spawn) ────────
#
# The runner's host-spawned web path and the CLI's ``omnigent antigravity``
# attach fallback both run the SAME thing once agy is live: an Omnigent HTTP
# client, a ``supervise_reader`` loop, and an ``on_pending_interaction`` callback
# wired to the Task 8 interaction bridge (real-time elicitation over the Task 9
# hook + RPC step reads + the default deliver). This wiring lives here so the two
# callers share one definition instead of duplicating the bridge/elicitation
# plumbing. (The retired transcript forwarder used a post-hoc policy audit instead
# of real-time elicitation; that audit is gone — agy exposes no firing PreToolUse
# hook, so a tool cannot be blocked before it runs, and the live elicitation card
# is the honest enforcement surface.)


async def _agy_elicitation_retry_sleep(seconds: float) -> None:
    """
    Indirection over :func:`asyncio.sleep` for the agy elicitation re-POST
    backoff, so tests can stub it without clobbering the process-global
    ``asyncio.sleep``. Mirrors
    :func:`omnigent.codex_native_forwarder._elicitation_retry_sleep`.

    :param seconds: Seconds to sleep, e.g. ``1.0``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


async def _post_agy_elicitation_request(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    elicitation_id: str,
    params: ElicitationRequestParams,
) -> httpx.Response | None:
    """
    POST one agy elicitation to the Omnigent hook, re-POSTing across severed
    long-polls.

    Mirrors :func:`omnigent.codex_native_forwarder._post_codex_elicitation_request`:
    the hook is a long-poll request/reply that blocks on a human, so a single
    failed POST must not abandon the prompt. The elicitation id is deterministic
    per ``(cascade_id, trajectory_id, step_index)`` (built by
    :func:`omnigent.antigravity_native_interactions.agy_elicitation_id`), so a
    re-POST of the same body re-parks the SAME elicitation server-side (keeping
    the approval card alive) and can collect a verdict that landed between
    attempts. Transport errors and 5xx responses are retried within the
    ``_AGY_ELICITATION_REQUEST_TIMEOUT_SECONDS`` budget; 2xx and 4xx are final.

    :param client: HTTP client for Omnigent hook posts (the reader's client).
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Deterministic agy elicitation id, e.g.
        ``"elicit_agy_<digest>"``.
    :param params: The web-renderable elicitation params.
    :returns: The final hook response, or ``None`` when the retry budget ran
        out — the caller surfaces no verdict (agy's own WAITING timeout reclaims
        the step).
    """
    url = f"/v1/sessions/{url_component(session_id)}/hooks/antigravity-elicitation-request"
    body: dict[str, object] = {
        "elicitation_id": elicitation_id,
        "params": params.model_dump(),
    }
    timeout = httpx.Timeout(
        _AGY_ELICITATION_REQUEST_TIMEOUT_SECONDS,
        connect=_AGY_ELICITATION_CONNECT_TIMEOUT_SECONDS,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _AGY_ELICITATION_REQUEST_TIMEOUT_SECONDS
    backoff_s = _AGY_ELICITATION_RETRY_INITIAL_BACKOFF_SECONDS
    while True:
        response: httpx.Response | None = None
        try:
            response = await client.post(url, json=body, timeout=timeout)
        except httpx.HTTPError:
            _logger.warning(
                "Antigravity elicitation hook POST failed; retrying: elicitation_id=%s",
                elicitation_id,
                exc_info=True,
            )
        if response is not None and response.status_code < 500:
            return response
        if response is not None:
            # 5xx = proxy gateway error on a severed long-poll, or a restarting
            # server — the verdict may still be pending, so re-POST.
            _logger.warning(
                "Antigravity elicitation hook returned %s; retrying: elicitation_id=%s",
                response.status_code,
                elicitation_id,
            )
        if loop.time() + backoff_s >= deadline:
            _logger.warning(
                "Antigravity elicitation hook retry budget exhausted: elicitation_id=%s",
                elicitation_id,
            )
            return None
        await _agy_elicitation_retry_sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _AGY_ELICITATION_RETRY_MAX_BACKOFF_SECONDS)


async def _request_agy_elicitation(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    elicitation_id: str,
    params: ElicitationRequestParams,
) -> ElicitationResult | None:
    """
    Production ``request_elicitation`` for the agy interaction bridge.

    Publishes the elicitation under ``elicitation_id`` via the Task 9 hook
    (``POST /v1/sessions/{id}/hooks/antigravity-elicitation-request``) and
    long-poll-awaits the human verdict. Mirrors codex's
    ``_codex_elicitation_hook_result`` body handling:

    * a 2xx with a body → parse it as :class:`ElicitationResult`;
    * a 2xx with an EMPTY body → ``None`` (the server timed out / saw the
      upstream disconnect — agy's own WAITING timeout reclaims the step);
    * a 4xx, an exhausted-retry ``None`` response, or a non-JSON / non-object
      body → ``None`` (logged; no verdict delivered).

    :param client: HTTP client for Omnigent hook posts (the reader's client).
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Deterministic agy elicitation id.
    :param params: The web-renderable elicitation params.
    :returns: The parsed :class:`ElicitationResult`, or ``None`` on
        timeout / rejection / malformed body.
    """
    response = await _post_agy_elicitation_request(
        client,
        session_id,
        elicitation_id=elicitation_id,
        params=params,
    )
    if response is None:
        return None
    if response.status_code >= 400:
        _logger.warning(
            "Antigravity elicitation hook rejected request (likely a misconfigured "
            "elicitation hook — a 4xx is a client/config error, not transient): "
            "status=%s elicitation_id=%s body=%s",
            response.status_code,
            elicitation_id,
            response.text[:512],
        )
        return None
    if not response.content:
        _logger.info(
            "Antigravity elicitation hook returned empty body; no verdict: elicitation_id=%s",
            elicitation_id,
        )
        return None
    try:
        return ElicitationResult.model_validate(response.json())
    except ValueError:
        _logger.warning(
            "Antigravity elicitation hook returned a non-ElicitationResult body: "
            "elicitation_id=%s body=%s",
            elicitation_id,
            response.text[:512],
        )
        return None


# ── Task T-G: /clear-rotation session rotation ───────────────────────────────
#
# A TUI ``/clear`` mints a NEW agy root cascade ON THE SAME live agy process and
# leaves the bound one idle. The reader detects it via ``GetAllCascadeTrajectories``
# (see :func:`_detect_rotated_cascade`) and then must move Omnigent ownership onto a
# fresh conversation bound to the NEW cascade — otherwise web turns keep targeting
# the old (now-dead) conversation and streaming appears to end. This mirrors the
# claude forwarder's ``_create_clear_replacement_session`` session-rotation
# sequence: agy, like claude, is a SINGLE long-lived process hosting many cascades,
# so the rotation TRANSFERS the existing terminal onto the replacement session
# (it does NOT re-spawn agy) and inherits the OLD session's bridge-id label (so the
# replacement resolves to the SAME ``bridge_dir`` the reader is already using — agy's
# bridge_dir is keyed off the launcher's bridge-id, not the session id), then
# rewrites bridge state with the new conversation id so the reader rebinds.
#
# THE LOOP BUG this fixes (found by live e2e): the prior implementation also PATCHed
# the replacement session's ``external_session_id=new_cascade``. But POST
# /v1/sessions for an antigravity-native session makes the runner auto-cold-start a
# brand-new agy (``_auto_create_antigravity_terminal`` fired for EVERY such session),
# which minted its OWN cascade AND set the new session's external_session_id; the
# rotation's external_session_id PATCH then hit that already-set, set-once-immutable
# field → 400 → rotation aborted, but the cold-start had already rebound the reader
# to its fresh cascade → the detector re-fired → an infinite session-spawn loop. The
# fix: drop the external_session_id PATCH entirely (claude never does it) AND gate
# the runner's antigravity auto-create on an inbound-transfer check (mirroring
# claude's ``_terminal_inbound`` guard) so the cold-start is skipped for a rotation
# target and the existing agy is transferred instead.

# Deterministic agy terminal resource id (matches
# ``antigravity_native._TERMINAL_NAME`` / ``_TERMINAL_SESSION_KEY``). Single
# terminal per agy session ("main"), transferred old→new on rotation so the live
# tmux pane keeps running under the new conversation. Built from the lightweight
# ``session_resources`` helper (as the codex forwarder does) rather than importing
# the heavy ``antigravity_native`` runner module into the reader.
_AGY_TERMINAL_RESOURCE_ID = terminal_resource_id("antigravity", "main")


async def _fetch_session_snapshot(client: httpx.AsyncClient, session_id: str) -> dict[str, object]:
    """
    Fetch an Omnigent session snapshot for agy ``/clear`` rotation.

    Mirrors :func:`omnigent.codex_native_forwarder._fetch_session_snapshot`: the
    rotation needs the old session's ``agent_id`` (required to create the
    replacement), ``runner_id`` (to re-bind it to the same runner), and ``labels``
    (to inherit the bridge-id so the new session resolves to the same bridge_dir).

    :param client: Omnigent HTTP client (the reader's client).
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Decoded JSON session snapshot.
    :raises httpx.HTTPStatusError: If Omnigent rejects the request.
    :raises RuntimeError: If the response body is not a JSON object.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Antigravity session snapshot response was not an object")
    return payload


async def _rotate_session_for_cascade(
    *,
    client: httpx.AsyncClient,
    old_session_id: str,
    new_cascade_id: str,
    bridge_dir: Path,
) -> str | None:
    """
    Create + activate a replacement Omnigent session bound to a new agy cascade.

    The Task T-G rotation effect: after a TUI ``/clear`` mints ``new_cascade_id`` on
    the SAME live agy process, this moves Omnigent ownership onto a fresh
    conversation bound to that cascade, MIRRORING claude's
    ``_create_clear_replacement_session`` (verified against
    ``omnigent/claude_native_forwarder.py``). agy — like claude — is ONE long-lived
    process hosting multiple cascades; a ``/clear`` mints a new cascade on that same
    process, so the rotation TRANSFERS the existing terminal onto the new session
    (it does NOT re-spawn agy) and rewrites bridge state so the reader rebinds to the
    new cascade on the SAME process:

    1. GET the old session snapshot (``agent_id`` / ``runner_id`` / ``labels``).
    2. POST ``/v1/sessions`` with the old ``agent_id`` + inherited ``labels`` — the
       labels carry agy's bridge-id (:data:`ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY`),
       so the NEW session resolves to the SAME ``bridge_dir`` the reader uses (agy's
       bridge_dir is keyed off the launcher's bridge-id, NOT the session id — so we
       inherit the old label, exactly as claude carries its ``BRIDGE_ID_LABEL_KEY``).
    3. PATCH the new session's ``runner_id`` to the old runner (so the same runner
       owns it), when the old session had one.
    4. POST the terminal ``/transfer`` to move the live agy tmux pane old→new (the
       pane — the SAME agy process — keeps running under the new conversation).
       NO ``external_session_id`` PATCH is made: unlike a resume launch, the new
       cascade ``new_cascade_id`` is ALREADY live on the existing agy and reached via
       the rewritten bridge state below, not via a later ``--resume``. (The old code
       PATCHed it, which 400'd on the auto-cold-started session's already-set,
       set-once-immutable field and looped the rotation — see the module header.)
    5. Rewrite agy bridge state in ``bridge_dir`` with the new session id + new
       conversation id (the reader re-reads this on rebind to bind the new cascade).
    6. PATCH the old session's ``runner_id`` to ``""`` to release it (best-effort;
       a failure is logged, not raised — the new session is already live).

    Best-effort: ANY failure (snapshot, create, bind, transfer, state write) is
    logged at WARNING and yields ``None``, and the caller keeps serving the OLD
    binding rather than crashing or losing the reader. The bridge-state rewrite is
    performed only AFTER the new session is created and bound, so a mid-sequence
    failure never leaves bridge state pointing at a session that does not exist —
    and, mirroring claude, it lands AFTER the transfer so the runner's auto-create
    guard still sees the OLD session owning the terminal while the new session binds
    (see :func:`omnigent.runner.app._antigravity_native_terminal_arrives_via_transfer`).

    :param client: Omnigent HTTP client (the reader's client).
    :param old_session_id: The Omnigent session being rotated away from.
    :param new_cascade_id: agy's freshly ``/clear``-minted cascade/conversation id.
    :param bridge_dir: The agy bridge directory the reader is using (rewritten in
        place so the new session shares it).
    :returns: The new Omnigent session id on success, or ``None`` on any failure
        (the caller then stays on the old binding).
    """
    try:
        old = await _fetch_session_snapshot(client, old_session_id)
        agent_id = old.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise RuntimeError(f"session {old_session_id!r} has no agent_id")
        runner_id = old.get("runner_id")
        raw_labels = old.get("labels")
        labels = (
            {str(key): str(value) for key, value in raw_labels.items()}
            if isinstance(raw_labels, dict)
            else {}
        )
        # Inherit the old session's bridge-id so the new session resolves to the
        # SAME bridge_dir; only fall back to stamping the old session id when the
        # label is somehow absent (keeps a deterministic, non-empty bridge id).
        if ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY not in labels:
            labels[ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY] = old_session_id

        create_resp = await client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "labels": labels},
        )
        create_resp.raise_for_status()
        created = create_resp.json()
        new_session_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(new_session_id, str) or not new_session_id:
            raise RuntimeError("Antigravity session replacement response did not include id")

        if isinstance(runner_id, str) and runner_id:
            bind_resp = await client.patch(
                f"/v1/sessions/{url_component(new_session_id)}",
                json={"runner_id": runner_id},
            )
            bind_resp.raise_for_status()

        transfer_resp = await client.post(
            (
                f"/v1/sessions/{url_component(old_session_id)}"
                f"/resources/terminals/{url_component(_AGY_TERMINAL_RESOURCE_ID)}/transfer"
            ),
            json={"target_session_id": new_session_id},
        )
        transfer_resp.raise_for_status()
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        _logger.warning(
            "agy /clear rotation failed to create the replacement session; staying on "
            "the old binding (old_session=%s new_cascade=%s): %r",
            old_session_id,
            new_cascade_id,
            exc,
        )
        return None

    # The new session is live + bound; commit bridge state so the reader's rebind
    # discovers the new cascade. Done last so a mid-sequence failure above never
    # points bridge state at a half-created session.
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id=new_session_id,
            conversation_id=new_cascade_id,
        ),
    )

    # Release the old session's runner binding (best-effort): the new session is
    # already serving, so a failure here is logged, not raised.
    try:
        clear_resp = await client.patch(
            f"/v1/sessions/{url_component(old_session_id)}",
            json={"runner_id": ""},
        )
        if clear_resp.status_code >= 400:
            _logger.warning(
                "agy /clear rotation: failed to release old runner binding; "
                "old_session=%s new_session=%s status=%s body=%s",
                old_session_id,
                new_session_id,
                clear_resp.status_code,
                clear_resp.text,
            )
    except httpx.HTTPError:
        _logger.warning(
            "agy /clear rotation: error releasing old runner binding; "
            "old_session=%s new_session=%s",
            old_session_id,
            new_session_id,
            exc_info=True,
        )

    _logger.info(
        "agy reader rotated Omnigent session after /clear: old_session=%s new_session=%s "
        "new_cascade=%s",
        old_session_id,
        new_session_id,
        new_cascade_id,
    )
    return new_session_id


async def run_reader_with_bridge(
    *,
    base_url: str,
    headers: dict[str, str],
    auth: httpx.Auth | None,
    session_id: str,
    bridge_dir: Path,
) -> None:
    """
    Run the agy RPC streaming reader + interaction bridge for one session.

    The single, shared read-path entry point used by BOTH host-spawned (runner)
    and CLI-fallback launches. It owns the long-lived Omnigent HTTP client (the
    reader takes a client but does NOT own its lifecycle) and runs
    :func:`supervise_reader`, wiring its ``on_pending_interaction`` callback to the
    Task 8 interaction bridge:

    * ``request_elicitation`` → :func:`_request_agy_elicitation` (POSTs the Task 9
      ``antigravity-elicitation-request`` hook and long-poll-awaits the human),
      mirroring codex's ``_handle_codex_elicitation_request``;
    * ``get_steps`` → ``get_trajectory_steps(port, cascade_id)`` offloaded to a
      worker thread (the RPC is synchronous);
    * ``deliver`` → the bridge default (``handle_user_interaction`` in a thread).

    The reader discovers the cascade id + connect-RPC port and hands BOTH to the
    callback, so the bridge targets agy's live conversation without
    re-discovering (which could bind a recycled/foreign port).

    Task T-G ``/clear`` rotation: this LOOPS. :func:`supervise_reader` returns the
    new cascade id when it detects a TUI ``/clear`` (via
    :func:`_watch_for_rotation`); this then rotates the Omnigent session onto a
    fresh conversation bound to the new cascade (:func:`_rotate_session_for_cascade`,
    which rewrites bridge state in ``bridge_dir``) and re-enters
    :func:`supervise_reader` — which rediscovers the new cascade from the same
    ``bridge_dir`` with a fresh :class:`_ReaderState`. If the session rotation
    FAILS, the old binding is kept (the failed cascade is added to ``skip``) so the
    reader keeps serving rather than being lost; if rotation succeeds, ``session_id``
    advances so the interaction-bridge elicitation hook targets the new session.
    The loop ends only when ``supervise_reader`` returns ``None`` (the reader was
    cancelled / its bounded ``stop`` fired).

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param headers: Auth headers for the Omnigent client (best-effort static
        bearer; ``auth`` carries any refresh-capable flow).
    :param auth: Refresh-capable httpx auth flow, or ``None`` when unauthenticated
        (the local-server runner path and the CLI attach fallback, which have no
        token to refresh — the bearer in ``headers``, if any, is used as-is).
    :param session_id: Omnigent conversation id to mirror into, e.g.
        ``"conv_abc123"``.
    :param bridge_dir: Native Antigravity bridge directory for this session.
    :returns: None. Runs until cancelled.
    """
    # Lazy import: the interaction bridge pulls server-route handlers; keeping it
    # out of module import keeps the reader importable from the lightweight CLI
    # process without eagerly loading the server stack.
    from omnigent.antigravity_native_interactions import bridge_interaction

    # Mutable current session id: rotation advances it, and the elicitation hook
    # closure below reads it through this holder so a post-rotation interaction
    # POSTs to the NEW session (a closure over the bare ``session_id`` argument
    # would keep targeting the rotated-away session).
    current = {"session_id": session_id}

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        auth=auth,
        timeout=httpx.Timeout(_READER_CLIENT_TIMEOUT_SECONDS),
    ) as client:

        async def _on_pending(cascade_id: str, port: int, pending: PendingInteraction) -> None:
            """Drive the interaction bridge for one WAITING interaction."""

            async def _request_elicitation(
                eid: str, params: ElicitationRequestParams
            ) -> ElicitationResult | None:
                return await _request_agy_elicitation(
                    client, current["session_id"], elicitation_id=eid, params=params
                )

            async def _get_steps() -> list[dict[str, object]]:
                return await asyncio.to_thread(get_trajectory_steps, port, cascade_id)

            await bridge_interaction(
                cascade_id,
                pending,
                port=port,
                get_steps=_get_steps,
                request_elicitation=_request_elicitation,
            )

        # Cascades a prior rotation attempt failed to bind — the detector skips them
        # so a persistent rotation failure does not hot-loop detect→fail→detect.
        failed_rotations: set[str] = set()
        while True:
            new_cascade_id = await supervise_reader(
                bridge_dir,
                current["session_id"],
                client=client,
                on_pending_interaction=_on_pending,
                skip_cascade_ids=frozenset(failed_rotations),
            )
            if new_cascade_id is None:
                # The reader body ended on its own (cancelled / bounded stop): done.
                return
            # A /clear rotation was detected: move Omnigent ownership onto a fresh
            # conversation bound to the new cascade, then rebind by re-entering
            # supervise_reader (which rediscovers from the rewritten bridge state).
            new_session_id = await _rotate_session_for_cascade(
                client=client,
                old_session_id=current["session_id"],
                new_cascade_id=new_cascade_id,
                bridge_dir=bridge_dir,
            )
            if new_session_id is None:
                # Rotation failed — keep serving the old binding, but never re-fire
                # on this same cascade (it would just fail again every few seconds).
                failed_rotations.add(new_cascade_id)
                continue
            current["session_id"] = new_session_id
