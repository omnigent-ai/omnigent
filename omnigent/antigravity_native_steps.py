"""Pure step→item mapper for the native Antigravity (agy) RPC stream.

This module replaces the delta-emitting ``step_to_events`` in
:mod:`omnigent.antigravity_native_forwarder` for the RPC-based read path (Task
12 cutover will relocate :class:`~omnigent.antigravity_native_forwarder.OutboundEvent`
and :class:`~omnigent.antigravity_native_forwarder._ToolCallIdAllocator` here).

Key differences from the transcript-based ``step_to_events``:

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

:func:`map_step_to_events` is the public API; all other symbols are private.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, TypedDict

from omnigent.antigravity_native_forwarder import (
    _AGENT_NAME,
    _TOOL_ARG_DISPLAY_KEYS,
    OutboundEvent,
    _ToolCallIdAllocator,
)

_logger = logging.getLogger(__name__)

# RPC step type constants (CORTEX_STEP_TYPE_* enum values).
_TYPE_USER_INPUT = "CORTEX_STEP_TYPE_USER_INPUT"
_TYPE_PLANNER_RESPONSE = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
_TYPE_RUN_COMMAND = "CORTEX_STEP_TYPE_RUN_COMMAND"
_TYPE_LIST_DIRECTORY = "CORTEX_STEP_TYPE_LIST_DIRECTORY"
_TYPE_ASK_QUESTION = "CORTEX_STEP_TYPE_ASK_QUESTION"
_TYPE_CHECKPOINT = "CORTEX_STEP_TYPE_CHECKPOINT"
_TYPE_CONVERSATION_HISTORY = "CORTEX_STEP_TYPE_CONVERSATION_HISTORY"

# RPC step status constants (CORTEX_STEP_STATUS_* enum values).
_STATUS_DONE = "CORTEX_STEP_STATUS_DONE"
_STATUS_WAITING = "CORTEX_STEP_STATUS_WAITING"

# RPC step source constant for model-generated steps.
_SOURCE_USER = "CORTEX_STEP_SOURCE_USER_EXPLICIT"


def _step_index(step: dict[str, object]) -> int | None:
    """
    Extract the trajectory step index from a RPC step dict.

    The index lives at ``metadata.sourceTrajectoryStepInfo.stepIndex``; it is
    absent for USER_INPUT steps (which have no trajectory slot of their own).

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :returns: The step index integer, or ``None`` when absent.
    """
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return None
    traj_info = metadata.get("sourceTrajectoryStepInfo")
    if not isinstance(traj_info, dict):
        return None
    idx = traj_info.get("stepIndex")
    return int(idx) if isinstance(idx, int) else None


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
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        return None
    traj_info = metadata.get("sourceTrajectoryStepInfo")
    if not isinstance(traj_info, dict):
        return None
    tid = traj_info.get("trajectoryId")
    return tid if isinstance(tid, str) else None


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
    except (json.JSONDecodeError, Exception):
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
    :param text: Assistant text (``plannerResponse.response``).
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


def _function_call_events(
    *,
    conversation_id: str,
    step_idx: int,
    tool_calls: list[object],
    allocator: _ToolCallIdAllocator,
) -> list[OutboundEvent]:
    """
    Build ``function_call`` items for a PLANNER_RESPONSE's tool calls.

    The RPC ``toolCalls`` entries carry ``name`` and ``argumentsJson`` (a JSON
    string); ``argumentsJson`` is parsed to a dict and display keys are stripped
    before re-serializing as the canonical arguments text.

    :param conversation_id: agy conversation id.
    :param step_idx: Owning step index.
    :param tool_calls: ``plannerResponse.toolCalls`` list.
    :param allocator: Positional call-id allocator (advanced per emitted call).
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
        call_id = allocator.claim_call_id()
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
    allocator: _ToolCallIdAllocator,
) -> OutboundEvent:
    """
    Build a ``function_call_output`` item for one completed agy tool step.

    :param conversation_id: agy conversation id.
    :param step_idx: Tool-result step index.
    :param output: Human-readable tool result text.
    :param allocator: Call-id correlator; oldest pending id is paired.
    :returns: One ``external_conversation_item`` event.
    """
    call_id = allocator.match_output_id()
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


def map_step_to_events(
    step: dict[str, object],
    *,
    conversation_id: str,
    allocator: _ToolCallIdAllocator,
) -> list[OutboundEvent]:
    """
    Map one agy RPC step to Omnigent conversation-item events.

    This is the pure, no-delta, no-USER_INPUT mapping layer for the RPC-based
    read path. It produces the same item types as
    :func:`omnigent.antigravity_native_forwarder.step_to_events` minus the
    ``external_output_text_delta`` event and minus the user-message mirror.

    Mapping:

    * ``CORTEX_STEP_TYPE_USER_INPUT`` → ``[]`` (skipped — the user turn is
      already persisted by the direct ``POST /events`` hook; emitting it here
      would duplicate the user message).
    * ``CORTEX_STEP_TYPE_PLANNER_RESPONSE`` → one ``message`` item (role
      assistant) when ``plannerResponse.response`` is non-empty, then one
      ``function_call`` item per ``plannerResponse.toolCalls`` entry. **No
      ``output_text_delta``** — that is the live double-render fix.
    * ``CORTEX_STEP_TYPE_RUN_COMMAND`` / ``LIST_DIRECTORY`` / ``ASK_QUESTION``
      (status DONE) → one ``function_call_output`` item carrying the result
      text. WAITING steps → ``[]`` (no result yet; Task 5 extracts the pending
      interaction).
    * ``CORTEX_STEP_TYPE_CHECKPOINT`` / ``CONVERSATION_HISTORY`` → ``[]``
      (system noise; no conversation content).

    :param step: One step dict from ``GetCascadeTrajectorySteps``.
    :param conversation_id: agy conversation id (namespaces response ids and
        call ids).
    :param allocator: Positional tool-call id allocator, mutated as calls and
        results are emitted so invocations and outputs line up across the run.
    :returns: Ordered events to POST for this step (possibly empty).
    """
    step_type = step.get("type")
    if not isinstance(step_type, str):
        return []

    # USER_INPUT: skip entirely — user turn already persisted by direct POST.
    # Mirror the source check from step_to_events: key on type+source, not
    # just type, so a hypothetically mis-typed MODEL step is not silently eaten.
    metadata = step.get("metadata")
    source: object = None
    if isinstance(metadata, dict):
        source = metadata.get("source")
    if step_type == _TYPE_USER_INPUT and source == _SOURCE_USER:
        return []
    # Also skip USER_INPUT without a recognized source (conservative).
    if step_type == _TYPE_USER_INPUT:
        return []

    status = step.get("status")

    # PLANNER_RESPONSE: emit assistant text message and/or function_call(s).
    if step_type == _TYPE_PLANNER_RESPONSE:
        idx = _step_index(step)
        if idx is None:
            return []
        events: list[OutboundEvent] = []
        planner = step.get("plannerResponse")
        if isinstance(planner, dict):
            response_text = planner.get("response")
            # Use modifiedResponse when present (it is the post-moderation text);
            # fall back to response.
            modified = planner.get("modifiedResponse")
            text = modified if isinstance(modified, str) and modified else response_text
            if isinstance(text, str) and text:
                # ONE message event — NO delta (the double-render fix).
                events.append(
                    _message_event(
                        conversation_id=conversation_id,
                        step_idx=idx,
                        text=text,
                    )
                )
            tool_calls = planner.get("toolCalls")
            if isinstance(tool_calls, list) and tool_calls:
                events.extend(
                    _function_call_events(
                        conversation_id=conversation_id,
                        step_idx=idx,
                        tool_calls=tool_calls,
                        allocator=allocator,
                    )
                )
        return events

    # Tool-result steps: emit function_call_output only when DONE.
    # WAITING → no output yet (pending interaction; Task 5 handles extraction).
    # ERROR → no output to report (command failed before producing output).
    if step_type in (
        _TYPE_RUN_COMMAND,
        _TYPE_LIST_DIRECTORY,
        _TYPE_ASK_QUESTION,
    ):
        if status != _STATUS_DONE:
            return []
        idx = _step_index(step)
        if idx is None:
            return []
        output = _tool_result_output(step, step_type)
        if output is None:
            return []
        return [
            _function_call_output_event(
                conversation_id=conversation_id,
                step_idx=idx,
                output=output,
                allocator=allocator,
            )
        ]

    # CHECKPOINT / CONVERSATION_HISTORY / unrecognized system steps → skip.
    return []
