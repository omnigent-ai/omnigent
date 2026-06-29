"""Pure step→item mapper for the native Antigravity (agy) RPC stream.

This module is the RPC-based read path's mapper, and (since the Task 12 cutover)
the home of the shared event types it produces: :class:`OutboundEvent`, the
:class:`_ToolCallIdAllocator`, and the ``_AGENT_NAME`` / ``_TOOL_ARG_DISPLAY_KEYS``
constants. These were relocated here from the retired transcript forwarder; the
RPC read driver (:mod:`omnigent.antigravity_native_reader`) imports them from
this module.

Key differences from the retired transcript-based ``step_to_events`` mapper:

1. **No ``output_text_delta`` event.** The old forwarder emitted one delta per
   assistant text step so the web UI could render streamed text and then
   reconcile to the committed item. The RPC stream already delivers complete
   steps (no token streaming), so the delta round-trip causes a double-render
   in the UI. This mapper drops it entirely.

2. **USER_INPUT → ``[]`` (skip).** The user turn is already persisted by the
   direct ``POST /events`` that the server hook fires before agy processes it.
   Emitting it again from the RPC transcript would duplicate the user message.

3. **RPC field names.** The RPC response uses ``CORTEX_STEP_TYPE_*`` type
   enums, camelCase keys (``plannerResponse``, ``runCommand``, ``stepIndex``),
   and ``argumentsJson`` (a JSON string) instead of the transcript's flat
   ``type``, ``content``, and ``tool_calls[].args`` (a dict).

4. **Real agy tool-call ids.** The RPC carries a stable, agy-assigned id on
   both the invocation (``plannerResponse.toolCalls[].id``) and the result
   (``metadata.toolCall.id``). The mapper uses those ids directly so
   ``function_call`` / ``function_call_output`` pairs are keyed by the real
   shared id (order-independent), not by FIFO position. The
   :class:`_ToolCallIdAllocator` is retained as a fallback only for the
   resume-mid-turn case where a result step lacks the ``metadata.toolCall.id``
   field.

:func:`map_step_to_events` is the public API; all other symbols are private.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from omnigent.antigravity_native_bridge import strip_fork_history

_logger = logging.getLogger(__name__)

# Omnigent ``agent`` label stamped on mirrored assistant/function-call items so
# the web UI attributes them to the native agy agent. Relocated here (Task 12
# cutover) from the retired transcript forwarder.
_AGENT_NAME = "antigravity-native-ui"

# agy ``tool_calls`` entries' ``args`` always include these display-only fields
# alongside the real tool arguments; they are stripped from the mirrored
# function-call arguments. Relocated here (Task 12 cutover) from the retired
# transcript forwarder.
_TOOL_ARG_DISPLAY_KEYS = frozenset({"toolAction", "toolSummary"})


@dataclass(frozen=True)
class OutboundEvent:
    """
    One Omnigent session event to POST for an agy step.

    Relocated here (Task 12 cutover) from the retired transcript forwarder; it is
    the shared event shape produced by both this mapper and the RPC read driver
    (:mod:`omnigent.antigravity_native_reader`).

    :param event_type: Omnigent session event type, e.g.
        ``"external_conversation_item"`` or ``"external_session_status"``.
    :param data: Event ``data`` payload posted under
        ``{"type": event_type, "data": data}``.
    :param step_index: The agy step index this event was derived from. Retained
        from the transcript-forwarder shape; the RPC read path does not advance a
        durable per-step cursor (that was retired with the forwarder), so it
        stamps a best-effort index and the field is informational there.
    """

    event_type: str
    data: dict[str, object]
    step_index: int


@dataclass
class _ToolCallIdAllocator:
    """
    Correlate agy tool invocations with their following result steps (fallback).

    Relocated here (Task 12 cutover) from the retired transcript forwarder. The
    RPC read path prefers the real agy-assigned ``id`` on both the invocation and
    the result, so this positional allocator is used only as a fallback for the
    resume-mid-turn case where a result step lacks ``metadata.toolCall.id``.

    The pairing is FIFO: the oldest still-unmatched invocation owns the next
    result. Ids are positional (``agy_call_<conversation>_<n>``) and the
    invocation counter only advances when an invocation is actually emitted, so
    replaying the same step prefix reproduces identical ids and pairings — which
    is what dedup needs across a restart.

    A result with no pending invocation (e.g. a transcript that begins mid-turn
    on resume) gets its own standalone id so it is never silently dropped.

    :param conversation_id: agy conversation id used to namespace ids, e.g.
        ``"8ca97c49-..."``.
    :param invocation_count: Number of invocation ids minted so far.
    :param orphan_output_count: Number of standalone (unpaired) output ids
        minted so far.
    :param pending_call_ids: Invocation ids awaiting their result step, oldest
        first.
    """

    conversation_id: str
    invocation_count: int = 0
    orphan_output_count: int = 0
    pending_call_ids: list[str] = field(default_factory=list)

    def claim_call_id(self) -> str:
        """
        Mint and enqueue a call id for one tool invocation.

        :returns: Stable invocation call id, e.g. ``"agy_call_8ca97c49_0"``.
        """
        call_id = f"agy_call_{self.conversation_id}_{self.invocation_count}"
        self.invocation_count += 1
        self.pending_call_ids.append(call_id)
        return call_id

    def match_output_id(self) -> str:
        """
        Return the call id for the next tool result, pairing FIFO.

        :returns: The oldest pending invocation's call id, or a fresh standalone
            id (``agy_call_<conversation>_orphan_<n>``) when none is pending.
        """
        if self.pending_call_ids:
            return self.pending_call_ids.pop(0)
        call_id = f"agy_call_{self.conversation_id}_orphan_{self.orphan_output_count}"
        self.orphan_output_count += 1
        return call_id


# RPC step type constants (CORTEX_STEP_TYPE_* enum values).
_TYPE_USER_INPUT = "CORTEX_STEP_TYPE_USER_INPUT"
_TYPE_PLANNER_RESPONSE = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
_TYPE_RUN_COMMAND = "CORTEX_STEP_TYPE_RUN_COMMAND"
_TYPE_LIST_DIRECTORY = "CORTEX_STEP_TYPE_LIST_DIRECTORY"
_TYPE_ASK_QUESTION = "CORTEX_STEP_TYPE_ASK_QUESTION"

# RPC step status constants (CORTEX_STEP_STATUS_* enum values).
_STATUS_DONE = "CORTEX_STEP_STATUS_DONE"
_STATUS_WAITING = "CORTEX_STEP_STATUS_WAITING"
_STATUS_ERROR = "CORTEX_STEP_STATUS_ERROR"


def _source_traj_info(step: dict[str, object]) -> dict[str, object] | None:
    """
    Return a step's ``metadata.sourceTrajectoryStepInfo`` block, or ``None``.

    The shared accessor for :func:`_step_index` and :func:`_trajectory_id`, which
    both read fields out of this block. Returns ``None`` when ``metadata`` or the
    nested block is missing or not a dict.

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :returns: The ``sourceTrajectoryStepInfo`` dict, or ``None`` when absent.
    """
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return None
    traj_info = metadata.get("sourceTrajectoryStepInfo")
    return traj_info if isinstance(traj_info, dict) else None


def _step_index(step: dict[str, object]) -> int | None:
    """
    Extract the trajectory step index from a RPC step dict.

    The index lives at ``metadata.sourceTrajectoryStepInfo.stepIndex``; it is
    absent (proto default-omits zero) for step-0 steps and for USER_INPUT steps
    (which have no trajectory slot).  Accepts both bare ``int`` and digit strings
    (agy sends some numerics as strings).

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :returns: The step index as ``int``, or ``None`` when absent or
        non-numeric.
    """
    traj_info = _source_traj_info(step)
    if traj_info is None:
        return None
    idx = traj_info.get("stepIndex")
    if isinstance(idx, int):
        return idx
    if isinstance(idx, str) and idx.isdigit():
        return int(idx)
    return None


class PendingInteraction(TypedDict):
    """
    A step that is WAITING for user interaction.

    Produced by :func:`pending_interaction` for CORTEX_STEP_STATUS_WAITING steps
    that carry a ``requestedInteraction`` block.  Downstream Tasks 7/8 consume
    this to drive the elicitation bridge.

    :param kind: Interaction type — ``"ask_question"`` or ``"permission"``.
    :param trajectory_id: agy trajectory id from ``sourceTrajectoryStepInfo``.
    :param step_index: Step index from ``sourceTrajectoryStepInfo`` (0 when absent).
    :param spec: The raw ``requestedInteraction.askQuestion`` or
        ``requestedInteraction.permission`` block.
    """

    kind: Literal["ask_question", "permission"]
    trajectory_id: str
    step_index: int
    spec: dict[str, object]


def _trajectory_id(step: dict[str, object]) -> str | None:
    """
    Extract the trajectory id from a RPC step dict.

    The id lives at ``metadata.sourceTrajectoryStepInfo.trajectoryId``; it is
    absent for USER_INPUT steps (which have no trajectory slot of their own).

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :returns: The trajectory id string, or ``None`` when absent.
    """
    traj_info = _source_traj_info(step)
    if traj_info is None:
        return None
    tid = traj_info.get("trajectoryId")
    return tid if isinstance(tid, str) else None


def _execution_discriminator(step: dict[str, object]) -> str | None:
    """
    Per-turn-unique dedup discriminator for a step with no ``step_index``.

    USER_INPUT steps carry no ``sourceTrajectoryStepInfo.stepIndex`` and share a
    per-conversation-stable ``trajectory_id``, so a ``(trajectory_id, None)`` key
    collides across every turn. ``metadata.executionId`` is a per-turn uuid (and
    ``metadata.createdAt`` a per-turn timestamp); either disambiguates turns.

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :returns: ``executionId`` (preferred) or ``createdAt`` when a non-empty
        string, else ``None``.
    """
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for field_name in ("executionId", "createdAt"):
        value = metadata.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def _merge_is_multi_select(
    ask_block: dict[str, object],
    step: dict[str, object],
) -> dict[str, object]:
    """
    Return a fresh copy of ``ask_block`` with ``is_multi_select`` injected.

    ``requestedInteraction.askQuestion`` does not carry ``is_multi_select``; it
    lives in ``metadata.toolCall.argumentsJson`` (a JSON-encoded string of the
    original tool-call arguments).  This helper parses that string and merges
    the flag into each ``questions[i]`` by index, defaulting to ``False`` when
    the string is absent, malformed, or missing a particular entry.

    The original ``ask_block`` and ``step`` dicts are never mutated.

    :param ask_block: The ``requestedInteraction.askQuestion`` dict.
    :param step: The full step dict (used to read ``metadata.toolCall.argumentsJson``).
    :returns: A new spec dict with ``is_multi_select`` present on every question.
    """
    # Parse argumentsJson from metadata.toolCall.
    args_questions: list[object] = []
    try:
        metadata = step.get("metadata")
        if isinstance(metadata, dict):
            tool_call = metadata.get("toolCall")
            if isinstance(tool_call, dict):
                raw = tool_call.get("argumentsJson")
                if isinstance(raw, str):
                    parsed: object = json.loads(raw)
                    if isinstance(parsed, dict):
                        aq = parsed.get("questions")
                        if isinstance(aq, list):
                            args_questions = aq
    except Exception:
        _logger.warning(
            "agy RPC ask_question WAITING: failed to parse argumentsJson for is_multi_select"
        )

    # Build a fresh spec dict — never mutate the input block.
    source_questions = ask_block.get("questions")
    if not isinstance(source_questions, list):
        return dict(ask_block)

    merged_questions: list[object] = []
    for i, q in enumerate(source_questions):
        if not isinstance(q, dict):
            merged_questions.append(q)
            continue
        is_multi_select = False
        if i < len(args_questions):
            aq_entry = args_questions[i]
            if isinstance(aq_entry, dict):
                flag = aq_entry.get("is_multi_select")
                if isinstance(flag, bool):
                    is_multi_select = flag
        new_q: dict[str, object] = {**q, "is_multi_select": is_multi_select}
        merged_questions.append(new_q)

    return {**ask_block, "questions": merged_questions}


def pending_interaction(step: dict[str, object]) -> PendingInteraction | None:
    """
    Extract a pending interaction from a WAITING step.

    Returns ``None`` unless ``step["status"] == CORTEX_STEP_STATUS_WAITING``.
    This is the crux: DONE steps may still carry ``requestedInteraction`` (they
    do in the recorded fixtures), so the implementation keys on *status*, not on
    the presence of the interaction block.

    For a WAITING step, the ``requestedInteraction`` block is inspected:

    * ``requestedInteraction.askQuestion`` present → ``kind="ask_question"``,
      ``spec`` = the ``askQuestion`` block (exposes
      ``questions[].{question, options[].{id, text}}``).
    * ``requestedInteraction.permission`` present → ``kind="permission"``,
      ``spec`` = the ``permission`` block (exposes
      ``resource.{action, target}`` and ``actionDescription``).

    ``trajectory_id`` and ``step_index`` come from
    ``metadata.sourceTrajectoryStepInfo``; ``step_index`` defaults to ``0``
    when the proto omits it (mirrors :func:`_step_index` behaviour).

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :returns: A :class:`PendingInteraction` dict, or ``None`` when the step is
        not WAITING or the interaction block cannot be resolved.
    """
    if step.get("status") != _STATUS_WAITING:
        return None

    requested = step.get("requestedInteraction")
    if not isinstance(requested, dict):
        return None

    trajectory_id = _trajectory_id(step)
    if trajectory_id is None:
        _logger.warning("agy RPC WAITING step missing trajectoryId")
        return None

    raw_idx = _step_index(step)
    step_idx = raw_idx if raw_idx is not None else 0

    ask = requested.get("askQuestion")
    if isinstance(ask, dict):
        return PendingInteraction(
            kind="ask_question",
            trajectory_id=trajectory_id,
            step_index=step_idx,
            spec=_merge_is_multi_select(ask, step),
        )

    permission = requested.get("permission")
    if isinstance(permission, dict):
        return PendingInteraction(
            kind="permission",
            trajectory_id=trajectory_id,
            step_index=step_idx,
            spec=permission,
        )

    _logger.warning(
        "agy RPC WAITING step has unrecognized requestedInteraction keys: %s",
        list(requested.keys()),
    )
    return None


def _response_id(conversation_id: str, step_idx: int) -> str:
    """
    Build a stable Omnigent response id for a RPC step.

    Mirrors the forwarder's ``_response_id`` format so ids are consistent
    across the transcript and RPC paths.

    :param conversation_id: agy conversation id.
    :param step_idx: Step index from ``sourceTrajectoryStepInfo.stepIndex``.
    :returns: Response id, e.g. ``"agy_8ca97c49_2"``.
    """
    return f"agy_{conversation_id}_{step_idx}"


def _json_string(value: dict[str, object]) -> str | None:
    """
    Serialize ``value`` to a compact JSON string.

    :param value: Dict to serialize.
    :returns: JSON string, or ``None`` when serialization fails.
    """
    try:
        return json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _strip_tool_display_args(args: dict[str, object]) -> dict[str, object]:
    """
    Drop agy's display-only keys from parsed tool-call arguments.

    :param args: Parsed tool-call arguments dict.
    :returns: Arguments with ``toolAction`` / ``toolSummary`` removed.
    """
    return {key: val for key, val in args.items() if key not in _TOOL_ARG_DISPLAY_KEYS}


def _real_call_id(entry: dict[str, object]) -> str | None:
    """
    Extract the agy-assigned tool-call id from an invocation entry.

    The RPC carries a stable id in ``plannerResponse.toolCalls[].id``; using
    it directly makes the invocation↔output pairing order-independent (both
    ends share the same id) rather than relying on FIFO position.

    :param entry: One ``plannerResponse.toolCalls[]`` dict.
    :returns: The id string, or ``None`` when absent.
    """
    cid = entry.get("id")
    return cid if isinstance(cid, str) and cid else None


def _result_call_id(step: dict[str, object]) -> str | None:
    """
    Extract the agy-assigned tool-call id from a tool-result step.

    The RPC carries the id at ``metadata.toolCall.id``; it matches the id on
    the invocation step so the pair can be correlated without FIFO ordering.

    :param step: A tool-result step dict (RUN_COMMAND, LIST_DIRECTORY, etc.).
    :returns: The id string, or ``None`` when absent.
    """
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return None
    tool_call = metadata.get("toolCall")
    if not isinstance(tool_call, dict):
        return None
    cid = tool_call.get("id")
    return cid if isinstance(cid, str) and cid else None


def planner_message_id(conversation_id: str, step_idx: int) -> str:
    """
    Build the stable streaming ``message_id`` for a PLANNER_RESPONSE step.

    The streaming read driver (Task T-D) tags every ``external_output_text_delta``
    for one assistant step with this id so the SPA coalesces the deltas into a
    single live block and then retires that block when the committed ``message``
    arrives — the reconciliation contract that prevents the double-render. One id
    per ``(conversation_id, step_idx)``; identical across all of the step's
    deltas.

    :param conversation_id: agy conversation id (equal to the cascade id).
    :param step_idx: The PLANNER_RESPONSE step's trajectory step index.
    :returns: Stable message id, e.g. ``"antigravity:8ca97c49:2:planner"``.
    """
    return f"antigravity:{conversation_id}:{step_idx}:planner"


def output_text_delta_event(
    *,
    conversation_id: str,
    step_idx: int,
    delta: str,
    final: bool,
) -> OutboundEvent:
    """
    Build an incremental assistant ``output_text_delta`` for a planner step.

    Relocated here from the retired transcript forwarder (Task 12 makes this
    module the home of :class:`OutboundEvent` and the event builders). Unlike the
    forwarder's one-shot delta (which carried the whole DONE message at once with
    ``final=True``), the streaming reader emits a *suffix* delta per frame while
    the step is GENERATING (``final=False``); the committed ``message`` then
    arrives separately via :func:`map_step_to_events` on DONE. The stable
    :func:`planner_message_id` lets the SPA coalesce the deltas into one live
    block and reconcile it against that committed item (no double-render).

    :param conversation_id: agy conversation id.
    :param step_idx: Owning step index (the planner step's trajectory index).
    :param delta: The NEW suffix of ``modifiedResponse`` since the last forwarded
        prefix for this step — NOT the cumulative text.
    :param final: ``True`` only on a terminal delta for the message; the
        streaming reader emits incremental deltas with ``False`` and relies on
        the committed ``message`` (not a ``final`` delta) to close the block.
    :returns: One ``external_output_text_delta`` event.
    """
    return OutboundEvent(
        event_type="external_output_text_delta",
        data={
            "delta": delta,
            "message_id": planner_message_id(conversation_id, step_idx),
            "index": 0,
            "final": final,
        },
        step_index=step_idx,
    )


def output_reasoning_delta_event(
    *,
    step_idx: int,
    delta: str,
    started: bool,
) -> OutboundEvent:
    """
    Build an incremental reasoning delta for a planner step's ``thinking``.

    Gemini Thinking-model variants stream chain-of-thought at
    ``plannerResponse.thinking`` (design §10.2), which grows across GENERATING
    frames just like ``modifiedResponse``. The streaming reader prefix-diffs it
    and emits a *suffix* delta per frame so the SPA paints a live reasoning block
    (``sse.ts`` maps ``response.reasoning_text.delta`` → its ``reasoning_delta``).
    Reasoning streams BEFORE the response (§10.2 ordering), and — unlike text —
    has no committed conversation item: the SPA finalizes the reasoning block when
    the committed assistant ``message`` arrives. The in-process antigravity
    executor reaches the same SPA state by a different route: it emits only the
    ``reasoning_text`` deltas and relies on an IMPLICIT reasoning-start (the SPA
    opens the block on the first ``reasoning_delta``), whereas this path emits an
    EXPLICIT ``response.reasoning.started`` first; both end with no committed
    reasoning item. The SPA's
    reasoning block is not keyed by a per-step id (unlike the text deltas'
    ``message_id``), so this carries no conversation id — only ``started`` and the
    growth ``delta``; ``step_index`` rides on the envelope for ordering/debug.

    ``started`` is ``True`` only on the FIRST delta of a step's reasoning so the
    server can precede it with a single ``response.reasoning.started`` (the
    SPA's "thinking…" / new-block marker); later deltas pass ``False``.

    :param step_idx: Owning step index (the planner step's trajectory index);
        recorded on the envelope for ordering/debug parity with the text path.
    :param delta: The NEW suffix of ``thinking`` since the last forwarded prefix
        for this step — NOT the cumulative text.
    :param started: ``True`` only on the first reasoning delta for the step, so the
        server emits one ``response.reasoning.started`` before this delta.
    :returns: One ``external_output_reasoning_delta`` event.
    """
    return OutboundEvent(
        event_type="external_output_reasoning_delta",
        data={
            "delta": delta,
            "started": started,
        },
        step_index=step_idx,
    )


def _message_event(
    *,
    conversation_id: str,
    step_idx: int,
    text: str,
) -> OutboundEvent:
    """
    Build an assistant ``message`` conversation item.

    The RPC path emits only assistant messages (role ``"assistant"``) via this
    function; user turns are skipped by the caller.

    :param conversation_id: agy conversation id.
    :param step_idx: Step index.
    :param text: Assistant text (``plannerResponse.modifiedResponse`` or
        ``plannerResponse.response``).
    :returns: One ``external_conversation_item`` event.
    """
    return OutboundEvent(
        event_type="external_conversation_item",
        data={
            "item_type": "message",
            "item_data": {
                "role": "assistant",
                "agent": _AGENT_NAME,
                "content": [{"type": "output_text", "text": text}],
            },
            "response_id": _response_id(conversation_id, step_idx),
        },
        step_index=step_idx,
    )


def _user_input_text(user_input: object) -> str:
    """
    Extract the user's turn text from a USER_INPUT step's ``userInput``.

    The live wire (agy 1.0.10/1.0.11) carries the turn text both as a single
    ``userResponse`` string and as ``items[].text`` (the same ``items`` list
    :func:`omnigent.antigravity_native_rpc.send_user_cascade_message` sends).
    Prefer ``userResponse``; fall back to joining the item texts.

    On a forked session the first typed turn carries the replayed prior
    transcript fenced in fork-history sentinels (text-preamble replay, see
    :func:`omnigent.antigravity_native_bridge.wrap_fork_preamble`). agy records
    that whole text as the USER_INPUT step, so the block is stripped here
    (:func:`omnigent.antigravity_native_bridge.strip_fork_history`) — otherwise
    the copied prior conversation would be DUPLICATED in the mirrored timeline
    (it already lives there as the source conversation's items). A no-op on the
    common case (no sentinel).

    :param user_input: The step's ``userInput`` value (expected ``dict``).
    :returns: The user's text, or ``""`` when absent/unparseable.
    """
    if not isinstance(user_input, dict):
        return ""
    response = user_input.get("userResponse")
    if isinstance(response, str) and response.strip():
        return strip_fork_history(response)
    items = user_input.get("items")
    if isinstance(items, list):
        parts = [
            item.get("text")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        joined = "\n".join(part for part in parts if part)
        if joined.strip():
            return strip_fork_history(joined)
    return ""


def _user_message_event(*, text: str) -> OutboundEvent:
    """
    Build a committed user ``message`` conversation item for a USER_INPUT step.

    Mirrors :func:`_message_event` but for the user's turn (role ``"user"``,
    ``input_text`` content, no ``response_id``/``agent``). The web UI reconciles
    its optimistic input bubble against this committed item via
    ``session.input.consumed``; without it the bubble has no committed
    counterpart and renders below the assistant reply (#1155). USER_INPUT steps
    carry no trajectory ``stepIndex`` (the reader dedups them by per-turn
    ``executionId``), so ``step_index`` is unused here.

    :param text: The user's turn text (from :func:`_user_input_text`).
    :returns: One ``external_conversation_item`` event.
    """
    return OutboundEvent(
        event_type="external_conversation_item",
        data={
            "item_type": "message",
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        step_index=0,
    )


def _planner_error_event(
    *, conversation_id: str, step_idx: int, step: dict[str, object]
) -> OutboundEvent:
    """
    Build a committed assistant ``message`` item for an ERROR planner step.

    A model/turn ERROR is otherwise dropped (the planner branch only commits at
    DONE), so the turn looks like a silent empty reply. Surface a visible marker —
    preferring any ``plannerResponse`` error text, falling back to a generic
    message — mirroring the tool-level marker (:func:`_tool_error_output`). The
    reader pairs this with a ``failed`` session-status edge.

    :param conversation_id: agy conversation id (namespaces the response id).
    :param step_idx: The ERROR planner step's index.
    :param step: The ERROR PLANNER_RESPONSE step.
    :returns: One ``external_conversation_item`` event (role ``"assistant"``).
    """
    detail = ""
    planner = step.get("plannerResponse")
    if isinstance(planner, dict):
        err = planner.get("error") or planner.get("errorMessage")
        if isinstance(err, str) and err.strip():
            detail = f": {err.strip()}"
    text = f"[antigravity: the model did not complete this turn (status ERROR){detail}]"
    return OutboundEvent(
        event_type="external_conversation_item",
        data={
            "item_type": "message",
            "item_data": {
                "role": "assistant",
                "agent": _AGENT_NAME,
                "content": [{"type": "output_text", "text": text}],
            },
            "response_id": _response_id(conversation_id, step_idx),
        },
        step_index=step_idx,
    )


def _function_call_events(
    *,
    conversation_id: str,
    step_idx: int,
    tool_calls: list[object],
    allocator: _ToolCallIdAllocator,
) -> list[OutboundEvent]:
    """
    Build ``function_call`` items for a PLANNER_RESPONSE's tool calls.

    The RPC ``toolCalls`` entries carry ``id``, ``name``, and ``argumentsJson``
    (a JSON string).  The real agy ``id`` is used as the ``call_id`` directly
    so the output step can pair by the same id without FIFO ordering.  The
    allocator is used as a fallback only when the ``id`` field is absent (e.g.
    a resume-mid-turn snapshot that pre-dates the id field).

    ``argumentsJson`` is parsed to a dict and display keys are stripped before
    re-serializing as the canonical arguments text.

    :param conversation_id: agy conversation id.
    :param step_idx: Owning step index.
    :param tool_calls: ``plannerResponse.toolCalls`` list.
    :param allocator: Fallback call-id allocator when real id is absent.
    :returns: One ``external_conversation_item`` event per valid tool call.
    """
    response_id = _response_id(conversation_id, step_idx)
    events: list[OutboundEvent] = []
    for entry in tool_calls:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            _logger.warning("agy RPC tool_call missing name: step_idx=%s", step_idx)
            continue
        raw_args_json = entry.get("argumentsJson")
        if isinstance(raw_args_json, str):
            try:
                raw_args: object = json.loads(raw_args_json)
            except json.JSONDecodeError:
                _logger.warning(
                    "agy RPC tool_call argumentsJson not valid JSON: step_idx=%s name=%s",
                    step_idx,
                    name,
                )
                continue
        else:
            raw_args = {}
        args = raw_args if isinstance(raw_args, dict) else {}
        arguments_text = _json_string(_strip_tool_display_args(args))
        if arguments_text is None:
            _logger.warning(
                "agy RPC tool_call args not JSON serializable: step_idx=%s name=%s",
                step_idx,
                name,
            )
            continue
        # Prefer the real agy-assigned id; fall back to the allocator only
        # when absent (resume-mid-turn case).
        real_id = _real_call_id(entry)
        call_id = real_id if real_id is not None else allocator.claim_call_id()
        events.append(
            OutboundEvent(
                event_type="external_conversation_item",
                data={
                    "item_type": "function_call",
                    "item_data": {
                        "agent": _AGENT_NAME,
                        "name": name,
                        "arguments": arguments_text,
                        "call_id": call_id,
                    },
                    "response_id": response_id,
                },
                step_index=step_idx,
            )
        )
    return events


def _function_call_output_event(
    *,
    conversation_id: str,
    step_idx: int,
    output: str,
    real_id: str | None,
    allocator: _ToolCallIdAllocator,
) -> OutboundEvent:
    """
    Build a ``function_call_output`` item for one completed agy tool step.

    Prefers the real agy ``metadata.toolCall.id`` for pairing; falls back to
    the allocator's FIFO match only when the id is absent.

    :param conversation_id: agy conversation id.
    :param step_idx: Tool-result step index.
    :param output: Human-readable tool result text.
    :param real_id: agy-assigned call id from ``metadata.toolCall.id``, or
        ``None`` when absent.
    :param allocator: Fallback call-id correlator when real id is absent.
    :returns: One ``external_conversation_item`` event.
    """
    call_id = real_id if real_id is not None else allocator.match_output_id()
    return OutboundEvent(
        event_type="external_conversation_item",
        data={
            "item_type": "function_call_output",
            "item_data": {"call_id": call_id, "output": output},
            "response_id": _response_id(conversation_id, step_idx),
        },
        step_index=step_idx,
    )


def _run_command_output(step: dict[str, object]) -> str | None:
    """
    Extract the combined output text from a RUN_COMMAND step.

    :param step: RUN_COMMAND step dict.
    :returns: ``runCommand.combinedOutput.full`` text, or ``None`` when absent.
    """
    run_command = step.get("runCommand")
    if not isinstance(run_command, dict):
        return None
    combined = run_command.get("combinedOutput")
    if not isinstance(combined, dict):
        return None
    full = combined.get("full")
    return full if isinstance(full, str) else None


def _tool_result_output(step: dict[str, object], step_type: str) -> str | None:
    """
    Extract a text output from a completed tool-result step.

    Dispatches by ``step_type`` to the appropriate nested field.

    :param step: The tool-result step dict.
    :param step_type: The step's ``CORTEX_STEP_TYPE_*`` string.
    :returns: Serialized output text, or ``None`` when nothing is extractable.
    """
    if step_type == _TYPE_RUN_COMMAND:
        return _run_command_output(step)
    if step_type == _TYPE_LIST_DIRECTORY:
        list_dir = step.get("listDirectory")
        if not isinstance(list_dir, dict):
            return None
        return _json_string(list_dir)
    if step_type == _TYPE_ASK_QUESTION:
        ask = step.get("askQuestion")
        if not isinstance(ask, dict):
            return None
        return _json_string(ask)
    return None


def _tool_error_output(step: dict[str, object]) -> str:
    """
    Build a best-effort ``function_call_output`` text for a terminal-ERROR tool step.

    An agy tool step that ends in ``CORTEX_STEP_STATUS_ERROR`` (e.g. a WAITING
    interaction that timed out / was cancelled, or a command that failed before
    producing output) carries no result text, but its invocation
    ``function_call`` was already mirrored, so the pair MUST still be closed or
    the web UI strands a perpetual in-progress tool card. This returns a short
    error marker naming the tool when available.

    :param step: The terminal-ERROR tool-result step dict.
    :returns: A non-empty error marker string.
    """
    metadata = step.get("metadata")
    if isinstance(metadata, dict):
        tool_call = metadata.get("toolCall")
        if isinstance(tool_call, dict):
            name = tool_call.get("name")
            if isinstance(name, str) and name:
                return f"[antigravity: {name} did not complete (status ERROR)]"
    return "[antigravity: tool did not complete (status ERROR)]"


def map_step_to_events(
    step: dict[str, object],
    *,
    conversation_id: str,
    allocator: _ToolCallIdAllocator,
) -> list[OutboundEvent]:
    """
    Map one agy RPC step to Omnigent conversation-item events.

    This is the pure, no-delta, no-USER_INPUT mapping layer for the RPC-based
    read path. It produces ``external_conversation_item`` events
    (``message`` / ``function_call`` / ``function_call_output``) and emits no
    ``external_output_text_delta`` and no user-message mirror (the user turn is
    persisted by the direct ``POST /events`` hook).

    Mapping:

    * ``CORTEX_STEP_TYPE_USER_INPUT`` → ``[]`` (skipped — the user turn is
      already persisted by the direct ``POST /events`` hook; emitting it here
      would duplicate the user message).
    * ``CORTEX_STEP_TYPE_PLANNER_RESPONSE`` **at status DONE** → one ``message``
      item (role assistant) when ``plannerResponse.modifiedResponse`` (or
      ``response``) is non-empty, then one ``function_call`` item per
      ``plannerResponse.toolCalls`` entry. A non-DONE (GENERATING) planner → ``[]``
      here; its partial text is conveyed only via the streaming reader's
      ``output_text_delta`` events, so committing a message pre-DONE would
      double-render (and double-post on the poll path). **No ``output_text_delta``
      from the mapper** — the committed item is delta-free (the live double-render
      fix).  ``modifiedResponse`` takes precedence over ``response`` because it is
      the post-moderation text (both fields present in the live DONE fixtures; they
      are equal when no moderation occurred).
    * A tool-result step — a known result type
      (``CORTEX_STEP_TYPE_RUN_COMMAND`` / ``LIST_DIRECTORY`` / ``ASK_QUESTION``)
      OR any step carrying a ``metadata.toolCall.id`` (generically catches
      result types with no dedicated extractor, e.g. VIEW_FILE / CODE_ACTION) —
      → one ``function_call_output`` keyed on that id once it reaches a terminal
      status. The text is the type-specific extractor's output, an error marker
      on ERROR, or an empty string when neither is available. WAITING steps →
      ``[]`` (no result yet; Task 5 extracts the pending interaction). Closing
      EVERY tool call this way is required because the invocation side emits a
      ``function_call`` for every tool unconditionally, and an unpaired one
      strands a perpetual in-progress card.
    * Any other step type (CHECKPOINT, CONVERSATION_HISTORY, unrecognized
      system steps with no ``toolCall.id``) → ``[]`` (system noise; no
      conversation content).

    Step-index handling: ``sourceTrajectoryStepInfo.stepIndex`` is proto-omitted
    when zero.  A missing index is treated as ``0`` so slot-0 steps (which in
    practice are USER_INPUT and are already skipped) are never silently dropped.

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :param conversation_id: agy conversation id (namespaces response ids and
        call ids).
    :param allocator: Fallback tool-call id allocator, used only when a step
        lacks the real agy ``id`` field (resume-mid-turn case).
    :returns: Ordered events to POST for this step (possibly empty).
    """
    step_type = step.get("type")
    if not isinstance(step_type, str):
        return []

    # USER_INPUT: commit the user's turn as a ``message`` item so the web UI
    # reconciles its optimistic bubble against a committed item (parity with
    # claude/codex/cursor native, which all mirror the user turn from their read
    # path). The turn OPENS on this step — before the planner response — so the
    # user message commits first and renders above the reply.
    #
    # This replaces the earlier "skip; the user turn is already persisted by a
    # direct POST /events hook" assumption, which never held for the production
    # web/mobile flow: the pure-RPC ``SendUserCascadeMessage`` write path fires
    # no such POST, so the user message was NEVER committed — the optimistic
    # bubble had no committed counterpart and dropped below the streamed reply
    # (#1155). The reader dedups USER_INPUT by its per-turn ``executionId``, so
    # this emits exactly once per turn.
    if step_type == _TYPE_USER_INPUT:
        text = _user_input_text(step.get("userInput"))
        if not text:
            return []
        return [_user_message_event(text=text)]

    status = step.get("status")

    # PLANNER_RESPONSE: emit the COMMITTED assistant message and/or function_call(s),
    # but ONLY at terminal (DONE) status. A pre-DONE planner (GENERATING) carries a
    # growing partial ``modifiedResponse`` that the streaming reader conveys via
    # incremental ``output_text_delta`` events; committing a message for it here too
    # would double-render — and on the poll path (which does NOT intercept GENERATING)
    # a step caught GENERATING then DONE would post TWO messages. Gating on DONE
    # (symmetric with the tool-result gate below) yields exactly one committed message,
    # with the FINAL text, on both the stream and poll paths. ERROR/other non-DONE →
    # no committed item (any partial already streamed as deltas).
    if step_type == _TYPE_PLANNER_RESPONSE:
        if status == _STATUS_ERROR:
            # A model/turn ERROR (safety block, rate-limit, provider overload,
            # internal error) must surface as a VISIBLE error item — otherwise the
            # turn clears to idle with no text and is indistinguishable from a
            # normal empty reply (the user sees nothing; no retry hint). Emit a
            # committed error message (the reader also closes the turn as FAILED).
            idx = _step_index(step)
            return [
                _planner_error_event(
                    conversation_id=conversation_id,
                    step_idx=idx if idx is not None else 0,
                    step=step,
                )
            ]
        if status != _STATUS_DONE:
            return []
        # Treat absent stepIndex as 0 (proto omits zero-valued scalar).
        idx = _step_index(step)
        step_idx = idx if idx is not None else 0
        events: list[OutboundEvent] = []
        planner = step.get("plannerResponse")
        if isinstance(planner, dict):
            response_text = planner.get("response")
            # modifiedResponse is the post-moderation text; prefer it over
            # response when present.  Both fields appear in live fixtures and
            # are equal when no moderation has occurred.
            modified = planner.get("modifiedResponse")
            text = modified if isinstance(modified, str) and modified else response_text
            if isinstance(text, str) and text:
                # ONE message event — NO delta (the double-render fix).
                events.append(
                    _message_event(
                        conversation_id=conversation_id,
                        step_idx=step_idx,
                        text=text,
                    )
                )
            tool_calls = planner.get("toolCalls")
            if isinstance(tool_calls, list) and tool_calls:
                events.extend(
                    _function_call_events(
                        conversation_id=conversation_id,
                        step_idx=step_idx,
                        tool_calls=tool_calls,
                        allocator=allocator,
                    )
                )
        return events

    # Tool-result steps. The invocation side (_function_call_events) emits a
    # function_call for EVERY entry in plannerResponse.toolCalls regardless of
    # tool name, so the result side MUST close every one of them — the reader is
    # the SOLE completion signal and the server pairs strictly by call_id, so a
    # function_call with no paired function_call_output strands a perpetual
    # in-progress tool card and breaks transcript reconstruction.
    #
    # A step is a tool result iff it is one of the known result types OR agy
    # stamped a ``metadata.toolCall.id`` on it (the id that pairs with the
    # invocation). The latter generically catches result types this mapper has
    # no extractor for (e.g. VIEW_FILE / CODE_ACTION on agy 1.0.10) — system
    # steps (CHECKPOINT / CONVERSATION_HISTORY / USER_INPUT) carry no toolCall.id
    # and so are NOT treated as tool results.
    #
    # Status handling:
    #   * Non-terminal (WAITING / RUNNING / PENDING / GENERATING) → [] — no
    #     result yet (for WAITING the interaction bridge surfaces the prompt;
    #     the step's later terminal transition closes the call).
    #   * terminal (DONE / ERROR) → exactly one function_call_output keyed on
    #     the toolCall.id, with best-effort text:
    #       - the type-specific extractor when it yields text;
    #       - an explicit error marker on ERROR;
    #       - else an empty string (e.g. a successful RUN_COMMAND whose
    #         combinedOutput proto-omitted empty output, or an unmapped result
    #         type) — empty-but-paired beats a dangling call.
    result_call_id = _result_call_id(step)
    is_known_tool_result = step_type in (
        _TYPE_RUN_COMMAND,
        _TYPE_LIST_DIRECTORY,
        _TYPE_ASK_QUESTION,
    )
    if is_known_tool_result or result_call_id is not None:
        if status not in (_STATUS_DONE, _STATUS_ERROR):
            return []
        idx = _step_index(step)
        step_idx = idx if idx is not None else 0
        output = _tool_result_output(step, step_type)
        if output is None:
            if status == _STATUS_ERROR:
                output = _tool_error_output(step)
            else:
                output = ""
                if not is_known_tool_result:
                    _logger.warning(
                        "agy RPC tool-result step has no extractor; closing the "
                        "call with empty output: type=%s status=%s call_id=%s",
                        step_type,
                        status,
                        result_call_id,
                    )
        return [
            _function_call_output_event(
                conversation_id=conversation_id,
                step_idx=step_idx,
                output=output,
                real_id=result_call_id,
                allocator=allocator,
            )
        ]

    # CHECKPOINT / CONVERSATION_HISTORY / unrecognized system steps → skip.
    return []
