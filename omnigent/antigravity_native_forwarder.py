"""Forward a native Antigravity (agy) session into an Omnigent session.

This is the Phase 2 Unit F2 forwarder: it mirrors a running ``agy`` TUI
conversation into the Omnigent transcript so the web/mobile chat view shows the
conversation. It plays the same ROLE as
:mod:`omnigent.codex_native_forwarder` / :mod:`omnigent.claude_native_forwarder`
but is simpler — instead of a connect-RPC stream it **tails agy's plaintext
JSONL transcript** file.

agy writes an append-only, step-granular transcript at::

    ~/.gemini/antigravity-cli/brain/<conversationId>/.system_generated/logs/transcript_full.jsonl

Each line is one *step* object, finalized when the step completes (no token
streaming). Verified line shape (see ``docs/claude/antigravity-sidecar-spike.md``
and the Step-0 empirical findings)::

    {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
     "status": "DONE", "created_at": "...", "content": "assistant text",
     "thinking": "optional reasoning", "tool_calls": [{"name": ..., "args": {...}}]}

Step taxonomy (``(source, type)``):

* ``USER_EXPLICIT`` / ``USER_INPUT`` — a user turn. ``content`` wraps the prompt
  in ``<USER_REQUEST>...</USER_REQUEST>`` plus metadata blocks.
* ``MODEL`` / ``PLANNER_RESPONSE`` — assistant text (``content``), optional
  ``thinking``, and/or ``tool_calls`` (the step that *initiates* tools).
* ``MODEL`` / anything-else-with-content (``RUN_COMMAND``, ``LIST_DIRECTORY``,
  ``VIEW_FILE``, ``GENERIC``, ``CODE_ACTION``, ...) — a tool *result*.
* ``SYSTEM`` / ``CONVERSATION_HISTORY`` | ``SYSTEM_MESSAGE`` | ``EPHEMERAL_MESSAGE``
  — system noise, skipped.

**Identity discovery.** Empirically (Step 0) agy does NOT adopt the launcher's
``ANTIGRAVITY_CONVERSATION_ID`` env var: it generates its own UUID conversation
id and always writes under the *default* ``~/.gemini/antigravity-cli`` app data
dir (it ignores ``ANTIGRAVITY_EXECUTABLE_DATA_DIR`` for the conversation store).
So the forwarder cannot resolve the transcript path from the bridge-state
``conversation_id``; it **discovers** agy's real id by watching the brain root
for conversation dirs created at/after launch, then records that id in bridge
state.

Discovery is *verifiable*, not merely positional, because the brain root is
shared by every agy on the host — two near-simultaneous ``omnigent
antigravity`` launches would otherwise let one forwarder bind the other's brain
dir (mirroring the wrong transcript AND PATCHing the wrong
``external_session_id``). Three guards make a fresh-launch discovery safe:

1. **Exclude claimed dirs.** Any conversation id already recorded by *another*
   live antigravity bridge dir (``~/.omnigent/antigravity-native/*/state.json``)
   is never bound.
2. **Refuse ambiguity.** If more than one *unclaimed* candidate qualifies in
   the discovery window, the forwarder refuses to bind that poll (it keeps
   polling) rather than guess by mtime.
3. **Positive ownership check.** The single remaining candidate is confirmed to
   be hosted by a live agy via the connect-RPC ``GetConversationMetadata``
   (reusing :mod:`omnigent.antigravity_native_rpc`) before it is persisted.

A *resume* skips discovery entirely: the real id is already in bridge state and
its transcript dir exists, so the state fast-path binds it without any RPC.

**Restart/resume dedup is durable.** The forwarder tails the transcript from
byte offset 0 with a high-water-mark dedup on ``step_index``. That mark lives in
memory for the run, but it is also *persisted* to bridge state
(``forwarded_step_index``) after every POSTed batch. On a (re)start — a
supervisor crash-restart OR an ``omnigent antigravity --resume`` — the parser
seeds its high-water from the persisted cursor, so re-reading the file from 0
re-POSTs only steps *beyond* the already-mirrored prefix instead of duplicating
the whole transcript. (External conversation items are persisted with a random
key and are NOT deduped server-side, so a re-post would otherwise duplicate the
mirrored transcript.) The byte offset itself is not persisted — it is not stable
across an agy file rewrite — but the step-index cursor is, and the high-water
dedup makes a from-0 re-read idempotent.

**Governance is POST-HOC (audit-only), never a pre-execution gate.** When
``audit_policies`` is enabled, the tail loop additionally evaluates each mirrored
tool call against the session's policies (``POST /policies/evaluate``) and, on a
DENY/ASK, surfaces a warning conversation item — plus a one-time degrade notice
that this harness is audit-only. This is **observational**: agy writes a step
only at ``DONE`` (the tool already ran) and its ``hooks.json`` ``PreToolUse`` hook
does NOT fire in 1.0.8, so there is no pre-execution interception point on the
supported surface (see ``docs/claude/antigravity-native-governance-design.md``).
An optional best-effort turn-interrupt on a violation is wired OFF (the agy
cancel-RPC contract is unverified). The classification/rendering lives in
:mod:`omnigent.antigravity_native_audit`; the async POST + interrupt are here.

The module is split so the parse/map logic is unit-testable without real
tailing or sleeps:

* :func:`step_to_events` — pure ``step -> [OutboundEvent]`` mapping.
* :class:`TranscriptParser` — stateful line buffering + dedup-by-``step_index``
  + turn-status edges, driven only by ``feed(...)`` calls.
* :func:`_audit_batch` — post-hoc policy audit over a delivered batch. At-least-
  once: a detected violation whose warning POST fails freezes the durable cursor
  so it is re-warned on restart (fail-OPEN only for policy-engine eval errors).
* :func:`forward_antigravity_transcript_to_session` — the thin async tail loop.
* :func:`supervise_forwarder` — restart supervisor matching the codex/claude
  forwarder shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

from omnigent._native_post_delivery import post_session_event_with_retry
from omnigent.antigravity_native_audit import (
    audit_verdict_is_violation,
    audit_violation_warning_text,
    build_audit_evaluation_request,
    build_degrade_notice_item,
    build_policy_violation_item,
)
from omnigent.antigravity_native_bridge import (
    AGY_APP_DATA_DIR,
    AntigravityNativeBridgeState,
    bridge_root,
    read_bridge_state,
    update_conversation_id,
    update_forwarded_steps,
)
from omnigent.antigravity_native_rpc import (
    conversation_id_owned_by_pid,
    interrupt_turn,
    resolve_language_server_port,
)
from omnigent.claude_native_bridge import url_component

_logger = logging.getLogger(__name__)

_AGENT_NAME = "antigravity-native-ui"

# agy ALWAYS writes its conversation store under the default app data dir, even
# when ``ANTIGRAVITY_EXECUTABLE_DATA_DIR`` is set (verified Step 0). The per-
# conversation transcript lives at
# ``<brain_root>/<conversationId>/.system_generated/logs/transcript_full.jsonl``.
# Path single-sourced from the bridge module (see ``AGY_APP_DATA_DIR``).
_DEFAULT_AGY_APP_DATA_DIR = AGY_APP_DATA_DIR
_BRAIN_SUBDIR = "brain"
_TRANSCRIPT_RELPATH = Path(".system_generated") / "logs" / "transcript_full.jsonl"

# Substring identifying the agy binary among a pane's process-tree descendants.
# Matches the binary path (``.../bin/agy``), mirroring the ``pgrep -f bin/agy``
# filter used in ``antigravity_native_rpc`` so the two agree on what "an agy
# process" is.
_AGY_PROCESS_MARKER = "bin/agy"
# Timeout for the ``tmux list-panes`` probe used to find this session's pane pid.
_TMUX_LIST_PANES_TIMEOUT_S = 5.0
# Bounded wait before the ambiguity fallback fails loudly. When the deterministic
# pane→pid binding is unavailable (no local tmux pane — e.g. a truly remote
# runner) AND more than one unclaimed candidate is in-window, the forwarder waits
# this long for the ambiguity to resolve on its own (the other launch records its
# id) before raising rather than livelocking forever.
_AMBIGUITY_RESOLUTION_TIMEOUT_S = 30.0

# Step field names (verified against real transcripts).
_FIELD_STEP_INDEX = "step_index"
_FIELD_SOURCE = "source"
_FIELD_TYPE = "type"
_FIELD_CONTENT = "content"
_FIELD_TOOL_CALLS = "tool_calls"

# Step ``source`` values.
_SOURCE_USER = "USER_EXPLICIT"
_SOURCE_MODEL = "MODEL"
_SOURCE_SYSTEM = "SYSTEM"

# Step ``type`` values that carry conversation content.
_TYPE_USER_INPUT = "USER_INPUT"
_TYPE_PLANNER_RESPONSE = "PLANNER_RESPONSE"

# ``tool_calls`` entries carry only ``{name, args}`` (no id). ``args`` always
# includes these display-only fields alongside the real tool arguments; they
# are stripped from the mirrored function-call arguments.
_TOOL_ARG_DISPLAY_KEYS = frozenset({"toolAction", "toolSummary"})

_POST_MAX_ATTEMPTS = 3
_POST_RETRY_DELAY_SECONDS = 0.1
_POST_RETRY_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Cap on bytes read from the transcript per poll (gov FIX C). A large/bursty or
# long-stalled transcript would otherwise materialize its entire undelivered
# suffix in one ``read()`` (events + delivered_prefix + audit records all built
# from it). The tail loop catches up over multiple polls instead. 1 MiB comfortably
# holds many steps per poll while bounding peak memory; overridable at the module
# level for tests. The read advances ``offset`` only to the last complete line so
# a partial final line is re-read next poll (see :func:`_read_transcript_from_offset`).
_MAX_TRANSCRIPT_READ_BYTES = 1024 * 1024

_DEFAULT_POLL_INTERVAL_S = 0.25
# How long to wait for agy to create its conversation/transcript before the
# forwarder gives up one run (the supervisor restarts it). Generous: a host-
# spawned TUI cold-starts and the conversation dir only appears after the first
# user turn.
_TRANSCRIPT_DISCOVERY_TIMEOUT_S = 600.0

_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

_STATUS_RUNNING = "running"
_STATUS_IDLE = "idle"

# ── Post-hoc policy audit (see :mod:`omnigent.antigravity_native_audit`) ──────
# Bounded timeout for the audit ``POST /policies/evaluate``. The server can park
# a ``tool_call`` ASK server-side (URL elicitation) for a non-read-only caller,
# which would block a synchronous POST; this timeout keeps the transcript mirror
# loop from hanging on a parked ASK. A timeout is treated as fail-open (logged,
# no warning). Short because the verdict for the non-ASK path returns promptly.
# Overridable at the module level (e.g. ``monkeypatch.setattr``) if a deployment
# needs a different bound; no per-session config is warranted for an internal,
# fail-open audit knob.
_AUDIT_EVAL_TIMEOUT_S = 10.0
# Best-effort turn-interrupt on an audit DENY/ASK. OFF by default and FAIL-OPEN:
# the audit warning is always surfaced regardless. Wired off because agy's cancel
# RPC request contract is unverified and the forwarder lacks agy's internal
# cascade id (see :func:`omnigent.antigravity_native_rpc.interrupt_turn`). Flip to
# ``True`` only once that contract is verified end-to-end.
_INTERRUPT_ON_AUDIT_DENY = False


@dataclass(frozen=True)
class OutboundEvent:
    """
    One Omnigent session event to POST for an agy transcript step.

    :param event_type: Omnigent session event type, e.g.
        ``"external_conversation_item"`` or ``"external_session_status"``.
    :param data: Event ``data`` payload posted under
        ``{"type": event_type, "data": data}``.
    :param step_index: The agy transcript ``step_index`` this event was derived
        from. Carried so the delivery layer can advance the DURABLE resume cursor
        (``forwarded_step_index``) only up to the highest *contiguously
        delivered* step — i.e. stop at the first step whose POST failed — rather
        than blindly persisting the in-memory PARSE high-water (which advances on
        parse, before delivery). Every event for a given step shares its index;
        the in-memory dedup high-water (live same-run de-dup) and this durable
        cursor are deliberately decoupled (see :class:`TranscriptParser`).
    """

    event_type: str
    data: dict[str, object]
    step_index: int


@dataclass
class _ToolCallIdAllocator:
    """
    Correlate agy tool invocations with their following result steps.

    agy ``tool_calls`` entries carry no id, and the result arrives as a separate
    later step that also has no shared id. To render the ``function_call`` /
    ``function_call_output`` pair the web UI expects, the forwarder synthesizes a
    ``call_id`` at invocation time and reuses it for the matching result.

    The pairing is FIFO: agy emits a tool result immediately after the step that
    initiated it, in order, so the oldest still-unmatched invocation owns the
    next result. Ids are positional (``agy_call_<conversation>_<n>``) and the
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


def _response_id(conversation_id: str, step_index: int) -> str:
    """
    Build a stable Omnigent response id for an agy step.

    The transcript has no per-turn id, so the step index is used. This groups
    nothing across steps (each step is its own response id) but is stable across
    re-reads, which is all the web UI needs to attribute a mirrored item.

    :param conversation_id: agy conversation id, e.g. ``"8ca97c49-..."``.
    :param step_index: agy ``step_index``, e.g. ``2``.
    :returns: Response id, e.g. ``"agy_8ca97c49_2"``.
    """
    return f"agy_{conversation_id}_{step_index}"


def unwrap_user_request(content: str) -> str:
    """
    Extract the human prompt from an agy ``USER_INPUT`` ``content`` blob.

    agy wraps the prompt as
    ``<USER_REQUEST>...</USER_REQUEST>`` followed by ``<ADDITIONAL_METADATA>``
    and ``<USER_SETTINGS_CHANGE>`` blocks. Only the request body is the user's
    text; the metadata blocks are stripped.

    :param content: Raw ``USER_INPUT`` content string.
    :returns: The unwrapped prompt text, stripped. Falls back to the whole
        content (stripped) when no ``<USER_REQUEST>`` wrapper is present.
    """
    open_tag = "<USER_REQUEST>"
    close_tag = "</USER_REQUEST>"
    start = content.find(open_tag)
    end = content.find(close_tag)
    if start != -1 and end != -1 and end > start:
        return content[start + len(open_tag) : end].strip()
    return content.strip()


def _strip_tool_display_args(args: dict[str, object]) -> dict[str, object]:
    """
    Drop agy's display-only keys from tool-call arguments.

    :param args: Raw ``tool_calls[].args`` dict, e.g.
        ``{"CommandLine": "git status", "toolAction": "...", "toolSummary": ...}``.
    :returns: Arguments with ``toolAction`` / ``toolSummary`` removed.
    """
    return {key: value for key, value in args.items() if key not in _TOOL_ARG_DISPLAY_KEYS}


def _function_call_events(
    *,
    conversation_id: str,
    step_index: int,
    tool_calls: list[object],
    allocator: _ToolCallIdAllocator,
) -> list[OutboundEvent]:
    """
    Build ``function_call`` items for one PLANNER_RESPONSE's tool calls.

    Only the *invocation* is emitted here; the agy transcript reports the
    result as a separate following tool-result step, mirrored elsewhere as a
    ``function_call_output``. The two are correlated by call id positionally
    (agy emits results in the same order as the calls) — but because agy carries
    no shared id, the output is keyed on its own synthesized id at result time.

    :param conversation_id: agy conversation id.
    :param step_index: Owning step index.
    :param tool_calls: ``tool_calls`` list from the PLANNER_RESPONSE step.
    :param allocator: Positional call-id allocator (advanced per emitted call).
    :returns: One ``external_conversation_item`` event per valid tool call.
    """
    response_id = _response_id(conversation_id, step_index)
    events: list[OutboundEvent] = []
    for entry in tool_calls:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            _logger.warning("agy tool_call missing name: step_index=%s", step_index)
            continue
        raw_args = entry.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        arguments_text = _json_string(_strip_tool_display_args(args))
        if arguments_text is None:
            _logger.warning(
                "agy tool_call args not JSON serializable: step_index=%s name=%s",
                step_index,
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
                step_index=step_index,
            )
        )
    return events


def _function_call_output_event(
    *,
    conversation_id: str,
    step_index: int,
    output: str,
    allocator: _ToolCallIdAllocator,
) -> OutboundEvent:
    """
    Build a ``function_call_output`` item for one agy tool-result step.

    The result is keyed on the next positional call id so it lines up with the
    matching ``function_call`` invocation emitted just before it (agy reports a
    result step immediately after the initiating PLANNER_RESPONSE, in order).

    :param conversation_id: agy conversation id.
    :param step_index: Tool-result step index.
    :param output: Human-readable tool result text (the step ``content``).
    :param allocator: Call-id correlator; the oldest pending invocation id is
        paired with this output.
    :returns: One ``external_conversation_item`` event.
    """
    call_id = allocator.match_output_id()
    return OutboundEvent(
        event_type="external_conversation_item",
        data={
            "item_type": "function_call_output",
            "item_data": {"call_id": call_id, "output": output},
            "response_id": _response_id(conversation_id, step_index),
        },
        step_index=step_index,
    )


def _message_event(
    *,
    conversation_id: str,
    step_index: int,
    role: str,
    text: str,
) -> OutboundEvent:
    """
    Build a ``message`` conversation item for a user/assistant turn.

    :param conversation_id: agy conversation id.
    :param step_index: Owning step index.
    :param role: ``"user"`` or ``"assistant"``.
    :param text: Message text.
    :returns: One ``external_conversation_item`` event.
    """
    if role == "user":
        item_data: dict[str, object] = {
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
    else:
        item_data = {
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": text}],
        }
    return OutboundEvent(
        event_type="external_conversation_item",
        data={
            "item_type": "message",
            "item_data": item_data,
            "response_id": _response_id(conversation_id, step_index),
        },
        step_index=step_index,
    )


def _output_text_delta_event(
    *,
    conversation_id: str,
    step_index: int,
    text: str,
) -> OutboundEvent:
    """
    Build a coarse assistant ``output_text_delta`` for a PLANNER_RESPONSE.

    agy writes assistant text only at step ``DONE`` (no token streaming), so the
    whole message is emitted as a single delta. This mirrors the codex
    forwarder's pattern of pairing a delta with the durable assistant message so
    the web UI renders streamed text and then reconciles to the committed item.
    The delta carries a stable ``message_id`` (one per step) so the client can
    coalesce it.

    :param conversation_id: agy conversation id.
    :param step_index: Owning step index.
    :param text: Assistant message text.
    :returns: One ``external_output_text_delta`` event.
    """
    message_id = f"antigravity:{conversation_id}:{step_index}:planner"
    return OutboundEvent(
        event_type="external_output_text_delta",
        data={
            "delta": text,
            "message_id": message_id,
            "index": 0,
            "final": True,
        },
        step_index=step_index,
    )


def step_to_events(
    step: Mapping[str, object],
    *,
    conversation_id: str,
    allocator: _ToolCallIdAllocator,
) -> list[OutboundEvent]:
    """
    Map one agy transcript step to Omnigent conversation-item events.

    This is the pure mapping layer — no I/O, no dedup, no status edges (those
    live in :class:`TranscriptParser`). It is the unit-testable core.

    Mapping:

    * ``USER_INPUT`` -> a ``message`` item (role user), prompt unwrapped from
      ``<USER_REQUEST>``.
    * ``PLANNER_RESPONSE`` -> an ``output_text_delta`` plus a ``message`` item
      (role assistant) for its ``content`` when non-empty, then one
      ``function_call`` item per ``tool_calls`` entry.
    * any other ``MODEL`` step with ``content`` (RUN_COMMAND / LIST_DIRECTORY /
      VIEW_FILE / GENERIC / CODE_ACTION / ...) -> a ``function_call_output``
      item carrying the result text.
    * ``SYSTEM`` steps and ``thinking`` -> skipped (no reasoning channel, per
      the task scope; ``# TODO`` below for usage).

    ``# TODO(usage via RPC)``: per-turn token usage is NOT in the transcript, so
    no ``external_session_usage`` is emitted here. It must be pulled from agy's
    connect-RPC surface in a later unit.

    :param step: One parsed transcript step object.
    :param conversation_id: agy conversation id (namespaces ids).
    :param allocator: Positional tool-call id allocator, mutated as calls/
        results are emitted so invocations and outputs line up across the run.
    :returns: Ordered events to POST for this step (possibly empty).
    """
    step_index = step.get(_FIELD_STEP_INDEX)
    if not isinstance(step_index, int):
        return []
    source = step.get(_FIELD_SOURCE)
    step_type = step.get(_FIELD_TYPE)
    content = step.get(_FIELD_CONTENT)
    content_text = content if isinstance(content, str) else ""

    if step_type == _TYPE_USER_INPUT and source == _SOURCE_USER:
        text = unwrap_user_request(content_text)
        if not text:
            return []
        return [
            _message_event(
                conversation_id=conversation_id,
                step_index=step_index,
                role="user",
                text=text,
            )
        ]

    if source != _SOURCE_MODEL:
        # SYSTEM / CONVERSATION_HISTORY / SYSTEM_MESSAGE / EPHEMERAL_MESSAGE.
        return []

    if step_type == _TYPE_PLANNER_RESPONSE:
        events: list[OutboundEvent] = []
        if content_text:
            events.append(
                _output_text_delta_event(
                    conversation_id=conversation_id,
                    step_index=step_index,
                    text=content_text,
                )
            )
            events.append(
                _message_event(
                    conversation_id=conversation_id,
                    step_index=step_index,
                    role="assistant",
                    text=content_text,
                )
            )
        tool_calls = step.get(_FIELD_TOOL_CALLS)
        if isinstance(tool_calls, list) and tool_calls:
            events.extend(
                _function_call_events(
                    conversation_id=conversation_id,
                    step_index=step_index,
                    tool_calls=tool_calls,
                    allocator=allocator,
                )
            )
        return events

    # Any other MODEL step with content is a tool result.
    if content_text:
        return [
            _function_call_output_event(
                conversation_id=conversation_id,
                step_index=step_index,
                output=content_text,
                allocator=allocator,
            )
        ]
    return []


def _is_turn_boundary_running(step: Mapping[str, object]) -> bool:
    """
    Return whether a step marks the beginning of an active turn.

    A user input step starts a turn (agy then runs the model + tools).

    :param step: Parsed transcript step.
    :returns: ``True`` for a ``USER_INPUT`` step.
    """
    return step.get(_FIELD_TYPE) == _TYPE_USER_INPUT and step.get(_FIELD_SOURCE) == _SOURCE_USER


def _is_assistant_text_step(step: Mapping[str, object]) -> bool:
    """
    Return whether a step is a PLANNER_RESPONSE carrying assistant text.

    Such a step (when it has no further tool calls) is the closing edge of a
    turn — agy answers and stops. Used as the heuristic ``idle`` edge given the
    transcript has no explicit turn-complete marker.

    :param step: Parsed transcript step.
    :returns: ``True`` when the step is a PLANNER_RESPONSE with text and no
        tool calls.
    """
    if step.get(_FIELD_TYPE) != _TYPE_PLANNER_RESPONSE:
        return False
    content = step.get(_FIELD_CONTENT)
    if not isinstance(content, str) or not content.strip():
        return False
    tool_calls = step.get(_FIELD_TOOL_CALLS)
    return not (isinstance(tool_calls, list) and tool_calls)


@dataclass
class TranscriptParser:
    """
    Stateful agy-transcript-to-Omnigent-events translator.

    Owns everything that needs memory across lines: partial-line buffering,
    dedup by ``step_index``, the positional tool-call id allocator, and turn
    status edges. The tail loop feeds it raw byte chunks; it returns the
    ordered events to POST. It performs no I/O.

    **PARSE-time dedup vs DURABLE cursor.** The parser de-dups re-reads *within
    the current run* (a from-0 re-read after an in-place truncation, or a
    same-poll re-read) using a per-run seen-set plus the resume floor — NOT a
    strict high-water, because agy can write ``step_index`` out of order (see
    :meth:`_process_line`). This dedup is deliberately NOT the durable resume
    cursor: the durable ``forwarded_step_index`` persisted to bridge state must
    reflect DELIVERY, not parsing, or a failed POST whose step was parsed would be
    marked forwarded and permanently skipped on resume.  The tail loop therefore
    advances the durable cursor from the per-step delivery result (see
    :func:`_post_events`), never from the parse state.

    :param conversation_id: agy conversation id (namespaces ids).
    :param emit_status: When ``True``, emit ``external_session_status``
        running/idle edges at turn boundaries. The tail loop also persists the
        running/idle state to bridge state separately.
    :param initial_step_high_water: LEGACY ``<=`` resume floor — steps at or below
        it are suppressed. Seeded ONLY from a legacy bridge state's
        ``forwarded_step_index`` (one written before ``forwarded_steps`` existed);
        for a new-format resume it is ``-1`` and :attr:`initial_delivered_steps`
        carries the cursor instead. ``-1`` (the default) means "nothing delivered
        by floor", so ``step_index=0`` is accepted on the first feed. NOT advanced
        to the per-batch high-water on resume — doing so would suppress a
        not-yet-written out-of-order lower step (see :attr:`initial_delivered_steps`).
    :param initial_delivered_steps: The exact SET of agy ``step_index`` values
        already *acked* before this parser was constructed, read from the persisted
        ``forwarded_steps`` cursor on a (re)start. A step is suppressed iff it is a
        MEMBER of this set (NOT ``<=`` a floor), because agy writes ``step_index``
        both non-contiguously AND out of order: a ``<=`` floor advanced past a
        ``{12, 14}`` batch would drop a later-arriving ``13``. The default (empty)
        means "no set cursor" — a fresh launch or a legacy resume that uses
        :attr:`initial_step_high_water` instead.
    """

    conversation_id: str
    emit_status: bool = True
    initial_step_high_water: int = -1
    initial_delivered_steps: frozenset[int] = frozenset()
    _buffer: str = ""
    # PARSE-time dedup (in-memory, live-run only). A step is suppressed iff it was
    # acked before this run (``initial_delivered_steps`` SET membership, or the
    # legacy ``initial_step_high_water`` ``<=`` floor) OR its step_index was already
    # emitted this run (``_seen_steps`` — a from-0 re-read after an in-place
    # truncation, or a same-poll re-read). Both the resume cursor and the in-run
    # dedup are MEMBERSHIP tests, NOT a strict high-water, because agy 1.0.10 can
    # write step_index OUT OF ORDER (e.g. 14 before 13 — verified live against the
    # real binary); a strict high-water would suppress the later-but-lower index as
    # a false duplicate and silently DROP that step from the mirror. ``_step_high_water``
    # is retained as the highest-PARSED marker (for ``step_high_water``); it is NOT
    # the dedup criterion and NOT the durable resume cursor (which the tail loop
    # advances from the per-step DELIVERY result; see ``_post_events``).
    _step_high_water: int = -1
    _seen_steps: set[int] = field(default_factory=set)
    _allocator: _ToolCallIdAllocator | None = None
    _turn_active: bool = False

    def __post_init__(self) -> None:
        """
        Initialize the dedup high-water and tool-call id allocator.

        :returns: None.
        """
        self._step_high_water = self.initial_step_high_water
        if self._allocator is None:
            self._allocator = _ToolCallIdAllocator(conversation_id=self.conversation_id)

    @property
    def step_high_water(self) -> int:
        """
        Return the highest agy ``step_index`` PARSED so far.

        Advances on parse, before delivery. Dedup itself is by the resume floor
        plus a per-run seen-set (see :meth:`_process_line`), NOT this marker — agy
        can write ``step_index`` out of order, so a strict high-water would drop a
        later-but-lower step. This is intentionally NOT the value the tail loop
        persists as the durable resume cursor: that cursor must reflect DELIVERY
        (see :func:`_post_events`), so a step parsed but not successfully POSTed is
        re-posted on resume rather than silently skipped.

        :returns: Highest parsed ``step_index``, or ``-1`` when none has been
            parsed yet.
        """
        return self._step_high_water

    def feed(self, chunk: str) -> list[OutboundEvent]:
        """
        Feed appended transcript text and return events for complete lines.

        Only complete (newline-terminated) JSON lines are parsed; a partial
        trailing line is buffered until its newline arrives. Malformed lines and
        lines without a usable ``step_index`` are skipped (never crash). A step
        whose ``step_index`` was already processed is skipped (dedup) so
        re-reads after a restart never double-post.

        :param chunk: Newly appended transcript text (may contain zero or more
            complete lines plus a partial trailing line).
        :returns: Ordered events to POST for the newly completed steps.
        """
        self._buffer += chunk
        events: list[OutboundEvent] = []
        while True:
            newline = self._buffer.find("\n")
            if newline == -1:
                break
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1 :]
            events.extend(self._process_line(line))
        return events

    def _process_line(self, line: str) -> list[OutboundEvent]:
        """
        Parse and translate one complete transcript line.

        :param line: One transcript line (without the trailing newline).
        :returns: Events for this line, or ``[]`` when skipped.
        """
        stripped = line.strip()
        if not stripped:
            return []
        try:
            step = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            _logger.warning("agy forwarder skipping malformed transcript line")
            return []
        if not isinstance(step, dict):
            return []
        step_index = step.get(_FIELD_STEP_INDEX)
        if not isinstance(step_index, int):
            return []
        if (
            step_index in self.initial_delivered_steps
            or step_index <= self.initial_step_high_water
            or step_index in self._seen_steps
        ):
            # Suppress iff: acked before this run (``initial_delivered_steps`` — the
            # SET cursor, MEMBERSHIP not ``<=``, so a not-yet-written out-of-order
            # lower step is re-posted not dropped), OR below a legacy ``<=`` resume
            # floor (only set when resuming a pre-``forwarded_steps`` state), OR
            # already emitted this run (``_seen_steps`` — a from-0 re-read after
            # truncation / same-poll re-read). The seen-set (not a high-water) means
            # a later-but-lower step_index — agy 1.0.10 writes them out of order —
            # is NOT mistaken for a duplicate within the run either.
            _logger.debug(
                "agy forwarder skipping already-processed step: conversation=%s step_index=%s",
                self.conversation_id,
                step_index,
            )
            return []
        self._seen_steps.add(step_index)
        self._step_high_water = max(self._step_high_water, step_index)
        return self._translate_step(step, step_index)

    def _translate_step(self, step: dict[str, object], step_index: int) -> list[OutboundEvent]:
        """
        Map a deduped step to status edges + conversation-item events.

        :param step: Parsed transcript step.
        :param step_index: The step's ``step_index`` (already validated by the
            caller), stamped on every event — including the status edges — so the
            delivery layer can attribute each POST to its step when advancing the
            durable resume cursor.
        :returns: Ordered events (status edge, then item events).
        """
        assert self._allocator is not None  # set in __post_init__
        events: list[OutboundEvent] = []
        if self.emit_status and _is_turn_boundary_running(step) and not self._turn_active:
            self._turn_active = True
            events.append(self._status_event(_STATUS_RUNNING, step_index))
        events.extend(
            step_to_events(
                step,
                conversation_id=self.conversation_id,
                allocator=self._allocator,
            )
        )
        if self.emit_status and _is_assistant_text_step(step) and self._turn_active:
            self._turn_active = False
            events.append(self._status_event(_STATUS_IDLE, step_index))
        return events

    def _status_event(self, status: str, step_index: int) -> OutboundEvent:
        """
        Build an ``external_session_status`` edge.

        :param status: Session status, e.g. ``"running"`` or ``"idle"``.
        :param step_index: The step that triggered this edge, stamped so the
            edge is committed/withheld together with that step's other events.
        :returns: One ``external_session_status`` event.
        """
        return OutboundEvent(
            event_type="external_session_status",
            data={"status": status},
            step_index=step_index,
        )

    def reset_buffer(self) -> None:
        """
        Drop any buffered partial line after a truncation/rotation rewind.

        The high-water-mark dedup integer is intentionally retained (in memory
        for the run, and mirrored to bridge state by the tail loop) so
        already-posted steps are not re-emitted when the file is reread from
        offset 0 — whether the rewind is a same-run truncation or a fresh
        forwarder (re)start.

        :returns: None.
        """
        self._buffer = ""

    @property
    def turn_active(self) -> bool:
        """
        Return whether a turn is currently considered active.

        :returns: ``True`` between a user-input edge and the next assistant
            closing edge.
        """
        return self._turn_active


def brain_root() -> Path:
    """
    Return the agy brain root that holds per-conversation transcripts.

    agy always writes its conversation store under the default app data dir,
    even when ``ANTIGRAVITY_EXECUTABLE_DATA_DIR`` is set (verified Step 0), so
    this is a fixed path rather than derived from bridge-state ``data_dir``.

    :returns: Absolute brain root, e.g.
        ``~/.gemini/antigravity-cli/brain``.
    """
    return _DEFAULT_AGY_APP_DATA_DIR / _BRAIN_SUBDIR


def transcript_path_for_conversation(conversation_id: str) -> Path:
    """
    Return the transcript path for an agy conversation id.

    :param conversation_id: agy conversation id, e.g. ``"8ca97c49-..."``.
    :returns: Absolute transcript path under the brain root.
    """
    return brain_root() / conversation_id / _TRANSCRIPT_RELPATH


def _claimed_conversation_ids(*, exclude_bridge_dir: Path) -> set[str]:
    """
    Collect agy conversation ids already claimed by *other* live bridge dirs.

    Every concurrent ``omnigent antigravity`` launch owns its own bridge dir
    under :func:`omnigent.antigravity_native_bridge.bridge_root`; each records
    the agy conversation id its forwarder discovered. To avoid two forwarders
    binding the same brain dir, a fresh discovery skips any id another bridge
    dir already claims. The caller's own bridge dir is excluded (its own id, if
    any, is not yet committed during discovery and must not block itself).

    :param exclude_bridge_dir: This forwarder's bridge dir, skipped in the scan.
    :returns: The set of conversation ids claimed by other antigravity bridge
        dirs (empty when none / on any scan error).
    """
    claimed: set[str] = set()
    try:
        exclude_resolved = exclude_bridge_dir.resolve()
    except OSError:
        exclude_resolved = exclude_bridge_dir
    try:
        entries = list(bridge_root().iterdir())
    except OSError:
        return claimed
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            if entry.resolve() == exclude_resolved:
                continue
        except OSError:
            continue
        other = read_bridge_state(entry)
        if other is None:
            continue
        # The launcher seeds ``conversation_id`` with the minted
        # ``agy_conv_*`` placeholder, which never names a real brain dir and so
        # cannot collide with a discovered UUID — but excluding it is harmless
        # and keeps the guard simple.
        if other.conversation_id:
            claimed.add(other.conversation_id)
    return claimed


def _candidate_conversation_dirs(*, since: float, claimed: set[str]) -> list[str]:
    """
    List unclaimed brain conversation ids created at/after a launch time.

    :param since: Lower bound on a brain dir's mtime (the forwarder start time
        minus a small skew), so a stale prior conversation is not bound.
    :param claimed: Conversation ids already owned by other bridge dirs.
    :returns: Unclaimed conversation ids whose brain dir mtime is at/after
        ``since`` (order unspecified).
    """
    try:
        entries = list(brain_root().iterdir())
    except OSError:
        return []
    candidates: list[str] = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < since:
            continue
        if entry.name in claimed:
            continue
        candidates.append(entry.name)
    return candidates


@dataclass(frozen=True)
class PaneTarget:
    """
    The runner-owned tmux pane hosting this session's agy process.

    Threaded from the launcher into the forwarder so discovery can tie itself to
    *this* session's agy process (rather than guessing by newest brain dir).
    Only present when the runner tmux is reachable from this CLI process (local
    server, or a remote server whose runner shares this host). For a truly
    remote runner the launcher passes ``None`` and discovery falls back to the
    claimed-exclusion + bounded-ambiguity path.

    :param tmux_socket: Runner tmux server socket path, e.g.
        ``"/tmp/omnigent-x/tmux.sock"``.
    :param tmux_target: tmux ``-t`` target for the agy pane, e.g. ``"main"``.
    """

    tmux_socket: Path
    tmux_target: str


# OS seam: return the pane process pid for a tmux target, or ``None``. Isolated
# so tests can stub it without a real tmux server. ``tmux list-panes`` prints
# ``#{pane_pid}`` — the pid of the process tmux spawned in the pane (the shell,
# under which agy runs once the client attaches).
def _tmux_pane_pid(pane: PaneTarget) -> int | None:
    """
    Return the pane process pid for a tmux target.

    :param pane: The runner-owned tmux pane to inspect.
    :returns: The pane's process pid (the shell tmux spawned), or ``None`` when
        tmux is missing, the probe fails/times out, or the target has no single
        parseable pid (e.g. the pane has not started yet under
        ``tmux_start_on_attach``).
    """
    try:
        completed = subprocess.run(
            [
                "tmux",
                "-S",
                str(pane.tmux_socket),
                "list-panes",
                "-t",
                pane.tmux_target,
                "-F",
                "#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            timeout=_TMUX_LIST_PANES_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _logger.debug("tmux list-panes failed for agy pane target=%s", pane.tmux_target)
        return None
    if completed.returncode != 0:
        return None
    pids = [line for line in completed.stdout.split() if line.isdigit()]
    # A single-pane agy terminal has exactly one pane pid. If tmux reports
    # several (an unexpected multi-pane layout) we cannot tell which hosts agy,
    # so we decline rather than guess.
    if len(pids) != 1:
        return None
    return int(pids[0])


# OS seam: walk a pane pid's descendants and return ALL agy pids (in tree
# order). Isolated so tests can stub it without spawning a real process tree.
def _agy_pids_under_pane(pane_pid: int) -> list[int]:
    """
    Return every agy descendant pid of a tmux pane process, in tree order.

    agy runs as a child (the pane shell ``exec``s / spawns it once a client
    attaches), so agy processes are found by walking the pane pid's process tree
    and matching the agy binary marker against each descendant's argv. The pane
    can contain MORE than one ``bin/agy`` process — e.g. a launcher/wrapper
    ``bin/agy`` that re-execs or supervises the real language-server ``bin/agy``.
    All matches are returned (rather than just the first) so the caller can pick
    the one that actually serves a conversation via its connect-RPC port, instead
    of blindly trusting the first cmdline match (which may be the wrapper).

    :param pane_pid: The tmux pane process pid, e.g. ``72750``.
    :returns: All agy descendant pids in process-tree order (pane process first,
        then ``children(recursive=True)`` order). Empty when the pane process is
        gone, has no agy descendant yet (the TUI is still cold-starting), or
        psutil cannot read the tree.
    """
    try:
        pane_proc = psutil.Process(pane_pid)
        descendants = [pane_proc, *pane_proc.children(recursive=True)]
    except (psutil.Error, OSError):
        return []
    agy_pids: list[int] = []
    for proc in descendants:
        try:
            cmdline = proc.cmdline()
        except (psutil.Error, OSError):
            continue
        if any(_AGY_PROCESS_MARKER in part for part in cmdline):
            # ``int()`` pins the type: psutil is untyped, so ``proc.pid`` is Any.
            agy_pids.append(int(proc.pid))
    return agy_pids


# Discovery seam: resolve THIS session's conversation id deterministically by
# tying to its own agy process (pane pid → agy children → the one whose
# connect-RPC port confirms a candidate). Tests stub this to avoid a live
# agy/tmux.
def _conversation_id_for_pane(pane: PaneTarget, candidates: list[str]) -> str | None:
    """
    Resolve which candidate id this session's pane-owned agy process hosts.

    The deterministic Finding-2 path: find the agy process(es) running under this
    session's tmux pane, then ask *their* connect-RPC port(s) which candidate id
    is owned (:func:`omnigent.antigravity_native_rpc.conversation_id_owned_by_pid`).
    Because the binding is to this session's own process tree — not "newest dir"
    — two near-simultaneous same-host launches each resolve their own
    conversation, eliminating the cross-launch ambiguity and the resulting
    livelock.

    A pane can host several ``bin/agy`` processes (e.g. a launcher/wrapper that
    re-execs or supervises the real language-server process). The first cmdline
    match is not necessarily the LS process that owns the connect-RPC port, so
    every agy descendant is probed and the FIRST one whose connect-RPC server
    confirms a candidate conversation is selected. A wrapper with no live LS port
    (or one that owns none of the candidates) is skipped via the existing
    ownership check rather than blindly trusted.

    :param pane: This session's runner-owned tmux pane.
    :param candidates: Unclaimed in-window brain-dir conversation ids to test.
    :returns: The candidate id this session's agy owns, or ``None`` when no agy
        descendant is resolvable yet or none owns a candidate (the caller keeps
        polling).
    """
    pane_pid = _tmux_pane_pid(pane)
    if pane_pid is None:
        return None
    agy_pids = _agy_pids_under_pane(pane_pid)
    if not agy_pids:
        _logger.debug(
            "agy forwarder: no agy descendant under pane pid=%s yet (cold start?)",
            pane_pid,
        )
        return None
    # Probe every agy descendant; the real language-server process is the one
    # whose connect-RPC port confirms a candidate conversation. A wrapper without
    # a live LS port resolves no port (and so no candidate) and is skipped.
    for agy_pid in agy_pids:
        owned = conversation_id_owned_by_pid(agy_pid, candidates)
        if owned is not None:
            return owned
    _logger.debug(
        "agy forwarder: %d agy descendant(s) under pane pid=%s own none of the "
        "%d candidate(s) yet; waiting",
        len(agy_pids),
        pane_pid,
        len(candidates),
    )
    return None


# Verification seam: confirm a live agy hosts ``conversation_id`` via the
# connect-RPC ``GetConversationMetadata`` before a fresh discovery is bound.
# ``None`` (the resolver returning no port) means no reachable agy owns it, so
# the candidate is not bound this poll. Tests stub this to avoid a live agy.
def _conversation_is_owned_by_live_agy(conversation_id: str) -> bool:
    """
    Return whether a live agy process currently hosts ``conversation_id``.

    Reuses :func:`omnigent.antigravity_native_rpc.resolve_language_server_port`,
    which enumerates agy processes and validates each candidate's connect-RPC
    server against the id via ``GetConversationMetadata`` — so a ``True`` here
    is a positive ownership confirmation, not a guess. Used as the final gate on
    a fresh discovery so the forwarder never binds (and PATCHes) a brain dir no
    live agy actually owns.

    :param conversation_id: Candidate agy conversation id, e.g.
        ``"68caaeac-..."``.
    :returns: ``True`` when a reachable agy reports metadata for it.
    """
    return resolve_language_server_port(conversation_id) is not None


class AmbiguousDiscoveryError(RuntimeError):
    """
    Raised when discovery stays ambiguous past the bounded fallback wait.

    Only reachable on the *fallback* path (no deterministic pane→pid binding,
    e.g. a truly remote runner) when more than one unclaimed candidate persists
    in the discovery window past :data:`_AMBIGUITY_RESOLUTION_TIMEOUT_S`. Failing
    loudly here (rather than polling forever) turns the old silent livelock into
    a surfaced, supervisor-retried error.
    """


def _discover_conversation_id(
    *,
    since: float,
    bridge_dir: Path,
    pane: PaneTarget | None,
    ambiguity_deadline: float | None,
) -> str | None:
    """
    Discover this launch's conversation id, deterministically when possible.

    agy ignores the launcher's ``ANTIGRAVITY_CONVERSATION_ID`` and mints its own
    id, and every agy on the host shares one brain root, so "newest dir by
    mtime" can bind a concurrent launch's conversation. Discovery is made safe:

    1. Exclude ids already claimed by another live antigravity bridge dir.
    2. **Deterministic bind (preferred).** When a tmux *pane* for this session
       is known, find the agy process under that pane and ask *its* connect-RPC
       port which candidate it owns
       (:func:`_conversation_id_for_pane`). This ties discovery to *this*
       session's own agy pid, so even two unclaimed in-window candidates from
       near-simultaneous launches each resolve to the right conversation — no
       ambiguity, no livelock. ``None`` (pane/agy/port not ready) keeps polling.
    3. **Fallback (no pane — e.g. truly remote runner).** Without a pid to
       identify "our" agy:

       * one unclaimed candidate → confirm it is hosted by a live agy via
         connect-RPC, then bind it;
       * more than one unclaimed candidate → cannot safely guess. Wait for the
         ambiguity to resolve on its own (another launch records its id) up to
         ``ambiguity_deadline``; if it persists past the deadline, raise
         :class:`AmbiguousDiscoveryError` rather than livelock forever.

    :param since: Lower bound on a brain dir's mtime (forwarder start minus a
        small skew the caller applies).
    :param bridge_dir: This forwarder's bridge dir (excluded from the claimed
        scan).
    :param pane: This session's runner-owned tmux pane, or ``None`` when the
        runner tmux is not reachable from this process (remote runner).
    :param ambiguity_deadline: ``time.monotonic()`` cutoff after which sustained
        fallback ambiguity raises, or ``None`` to never raise (only used when
        *pane* is ``None``).
    :returns: The resolved conversation id, or ``None`` when none has appeared
        yet / is not yet bindable — the caller keeps polling.
    :raises AmbiguousDiscoveryError: On the fallback path only, when multiple
        unclaimed candidates persist past *ambiguity_deadline*.
    """
    claimed = _claimed_conversation_ids(exclude_bridge_dir=bridge_dir)
    candidates = _candidate_conversation_dirs(since=since, claimed=claimed)
    if not candidates:
        return None
    if pane is not None:
        # Deterministic: bind only the candidate THIS session's agy owns. Safe
        # with any number of in-window candidates — the pid identifies ours.
        resolved = _conversation_id_for_pane(pane, candidates)
        if resolved is None:
            _logger.debug(
                "agy forwarder: pane-owned agy has not bound any of %d candidate(s) yet; waiting",
                len(candidates),
            )
        return resolved
    # Fallback: no pid to identify our agy.
    if len(candidates) > 1:
        if ambiguity_deadline is not None and time.monotonic() >= ambiguity_deadline:
            raise AmbiguousDiscoveryError(
                f"agy forwarder could not deterministically bind a conversation: "
                f"{len(candidates)} unclaimed conversation dirs persisted in the "
                f"discovery window past the {_AMBIGUITY_RESOLUTION_TIMEOUT_S:.0f}s "
                f"fallback wait and no tmux pane was available to tie this session "
                f"to its own agy process: {sorted(candidates)}"
            )
        _logger.warning(
            "agy forwarder refusing to bind: %d unclaimed conversation dirs in "
            "the discovery window (concurrent launches?); will keep polling: %s",
            len(candidates),
            sorted(candidates),
        )
        return None
    candidate = candidates[0]
    if not _conversation_is_owned_by_live_agy(candidate):
        _logger.debug(
            "agy forwarder candidate %s not yet owned by a reachable agy; waiting",
            candidate,
        )
        return None
    return candidate


async def _resolve_transcript(
    *,
    bridge_dir: Path,
    discovery_floor: float,
    poll_interval_s: float,
    timeout_s: float,
    pane: PaneTarget | None,
) -> tuple[str, Path] | None:
    """
    Resolve agy's real conversation id and transcript path, polling until ready.

    Discovery order, re-checked each poll until the transcript file exists:

    1. If bridge state already records an agy conversation id whose transcript
       file exists, use it (a resume / restart fast-path).
    2. Otherwise discover this launch's brain dir — deterministically via the
       tmux *pane*'s agy pid when available, else via the bounded-ambiguity
       fallback — persist that id to bridge state, and use it once its
       transcript file exists (see :func:`_discover_conversation_id`).

    :param bridge_dir: Native Antigravity bridge directory.
    :param discovery_floor: Lower bound on brain-dir mtime for discovery.
    :param poll_interval_s: Seconds between polls.
    :param timeout_s: Maximum seconds to wait before giving up this run.
    :param pane: This session's runner-owned tmux pane for deterministic
        discovery, or ``None`` (remote runner — fallback path).
    :returns: ``(conversation_id, transcript_path)`` once the file exists, or
        ``None`` on timeout.
    :raises AmbiguousDiscoveryError: When discovery has no pane and stays
        ambiguous past the bounded fallback wait.
    """
    deadline = time.monotonic() + timeout_s
    # The fallback ambiguity guard only arms when there is no deterministic
    # pane binding; with a pane, ambiguity is resolved by the pid and never
    # raises. Measured from the first poll so a sustained two-candidate standoff
    # fails loudly instead of livelocking.
    ambiguity_deadline = (
        None if pane is not None else time.monotonic() + _AMBIGUITY_RESOLUTION_TIMEOUT_S
    )
    persisted_id: str | None = None
    while True:
        # ``_resolve_transcript_once`` may call ``_discover_conversation_id``
        # → ``_conversation_id_for_pane`` / ``_conversation_is_owned_by_live_agy``
        # → tmux/lsof/pgrep subprocesses and blocking httpx TLS probes. Run it in
        # a thread so the event loop stays responsive; the data it touches
        # (bridge-state files, brain-dir mtimes, the read-only pane target) is
        # safe for concurrent reads, and ``persisted_id`` is a plain local with
        # no shared mutable state across the boundary.
        resolved = await asyncio.to_thread(
            _resolve_transcript_once,
            bridge_dir=bridge_dir,
            discovery_floor=discovery_floor,
            persisted_id=persisted_id,
            pane=pane,
            ambiguity_deadline=ambiguity_deadline,
        )
        if resolved is not None:
            conversation_id, path, newly_persisted = resolved
            if newly_persisted:
                persisted_id = conversation_id
            if path.is_file():
                return conversation_id, path
        if time.monotonic() >= deadline:
            _logger.warning(
                "agy forwarder timed out resolving transcript: bridge_dir=%s",
                bridge_dir,
            )
            return None
        await asyncio.sleep(poll_interval_s)


def _resolve_transcript_once(
    *,
    bridge_dir: Path,
    discovery_floor: float,
    persisted_id: str | None,
    pane: PaneTarget | None,
    ambiguity_deadline: float | None,
) -> tuple[str, Path, bool] | None:
    """
    Attempt one transcript resolution pass (no waiting).

    :param bridge_dir: Native Antigravity bridge directory.
    :param discovery_floor: Lower bound on brain-dir mtime for discovery.
    :param persisted_id: agy conversation id already persisted this run, or
        ``None``.
    :param pane: This session's runner-owned tmux pane for deterministic
        discovery, or ``None`` (remote runner — fallback path).
    :param ambiguity_deadline: ``time.monotonic()`` cutoff for the fallback
        ambiguity guard, or ``None``.
    :returns: ``(conversation_id, transcript_path, newly_persisted)`` when a
        candidate id is known (the caller still checks the file exists), or
        ``None`` when no candidate has appeared yet. ``newly_persisted`` is
        ``True`` when this pass wrote the id to bridge state.
    :raises AmbiguousDiscoveryError: When the fallback path stays ambiguous past
        *ambiguity_deadline*.
    """
    if persisted_id is not None:
        return persisted_id, transcript_path_for_conversation(persisted_id), False

    state = read_bridge_state(bridge_dir)
    state_id = _agy_conversation_id_from_state(state)
    if state_id is not None:
        candidate = transcript_path_for_conversation(state_id)
        if candidate.is_file():
            return state_id, candidate, False

    discovered = _discover_conversation_id(
        since=discovery_floor,
        bridge_dir=bridge_dir,
        pane=pane,
        ambiguity_deadline=ambiguity_deadline,
    )
    if discovered is None:
        return None
    # Persist the discovered id so a restart resumes against the same
    # conversation without re-discovering (and so the executor unit can target
    # it). Preserve the running turn id if any.
    update_conversation_id(
        bridge_dir,
        discovered,
        active_turn_id=state.active_turn_id if state is not None else None,
    )
    return discovered, transcript_path_for_conversation(discovered), True


def _agy_conversation_id_from_state(
    state: AntigravityNativeBridgeState | None,
) -> str | None:
    """
    Return a usable agy conversation id from bridge state, if present.

    The launcher seeds ``conversation_id`` with the Omnigent-minted
    ``agy_conv_*`` value, which agy ignores; that value never names a real brain
    dir, so it is rejected here. Only an id whose transcript dir actually exists
    is honored (i.e. one the forwarder previously discovered and persisted).

    :param state: Parsed bridge state, or ``None``.
    :returns: A conversation id whose brain dir exists, or ``None``.
    """
    if state is None:
        return None
    conversation_id = state.conversation_id
    if not conversation_id:
        return None
    conversation_dir = brain_root() / conversation_id
    if conversation_dir.is_dir():
        return conversation_id
    return None


def _persisted_resume_cursor(bridge_dir: Path, conversation_id: str) -> tuple[int, frozenset[int]]:
    """
    Return the parser's resume-cursor seed for a (re)started forwarder run.

    Reads the persisted cursor from bridge state, but only when it belongs to
    *conversation_id* — a cursor recorded for a *different* (prior) agy
    conversation must not suppress the new transcript's steps. The mismatch case
    is defensive: ``_resolve_transcript_once`` already resets the cursor on a
    conversation change when it persists the newly discovered id.

    Two seed shapes, mutually exclusive:

    * **New format** (``forwarded_steps`` recorded): return ``(-1, the set)`` — the
      legacy ``<=`` floor is disabled and the parser suppresses by SET membership.
      This is what avoids dropping a not-yet-written out-of-order lower step on
      restart (a ``<=`` floor at the batch high-water would suppress it).
    * **Legacy format** (only ``forwarded_step_index``): return
      ``(that index, empty set)`` — the old ``<=`` floor, so a state written by a
      prior build still resumes (it cannot represent the set).

    :param bridge_dir: Native Antigravity bridge directory.
    :param conversation_id: agy conversation id the forwarder resolved for this
        run, e.g. ``"68caaeac-..."``.
    :returns: ``(initial_step_high_water, initial_delivered_steps)`` for this
        conversation. ``(-1, frozenset())`` when nothing is recorded (fresh
        launch) or the cursor belongs to a different conversation.
    """
    state = read_bridge_state(bridge_dir)
    if state is None or state.conversation_id != conversation_id:
        return -1, frozenset()
    if state.forwarded_steps is not None:
        return -1, frozenset(state.forwarded_steps)
    if state.forwarded_step_index is not None:
        return state.forwarded_step_index, frozenset()
    return -1, frozenset()


def _json_string(value: dict[str, object]) -> str | None:
    """
    Serialize a dict for OpenAI-compatible function-call arguments.

    :param value: JSON-serializable dictionary, e.g. ``{"command": "pwd"}``.
    :returns: JSON string, or ``None`` when serialization fails.
    """
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


async def _sleep(seconds: float) -> None:
    """
    Stubbable indirection for forwarder sleeps.

    Exists so tests can stub retry/poll delays without patching
    ``asyncio.sleep`` through the imported module singleton.

    :param seconds: Delay in seconds.
    :returns: None after the sleep completes.
    """
    await asyncio.sleep(seconds)


async def _patch_external_session_id(
    client: httpx.AsyncClient,
    session_id: str,
    conversation_id: str,
) -> bool:
    """
    Persist agy's discovered conversation id onto the Omnigent session.

    agy mints its own UUID and ignores any id the launcher assigns, so the
    launcher cannot capture ``external_session_id`` up front. Once the forwarder
    discovers agy's real id (by-pid ownership of the running agy process), it
    PATCHes it here so a later ``omnigent antigravity --resume`` reads the real
    id and passes it to ``agy --conversation`` (resume is same-machine only —
    agy's brain store is not portable).

    Best-effort and idempotent: setting the id to the value it already holds is
    harmless, so this is safe to call once per run regardless of whether the id
    was freshly discovered or read back from bridge state on a restart. Failures
    are logged, not raised — a failed PATCH must not crash the read-path mirror —
    but they are reported via the return value so the caller's once-per-supervisor
    latch only fires on a real success (a transient failure stays retryable).

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param conversation_id: agy's real conversation id, e.g. ``"68caaeac-..."``.
    :returns: ``True`` when the PATCH succeeded; ``False`` on a transport error
        or a ``>= 400`` response (so the caller can keep it retryable).
    """
    url = f"/v1/sessions/{url_component(session_id)}"
    try:
        response = await client.patch(url, json={"external_session_id": conversation_id})
    except httpx.HTTPError as exc:
        _logger.warning(
            "failed to PATCH agy external_session_id: session=%s conversation=%s error=%r",
            session_id,
            conversation_id,
            exc,
        )
        return False
    if response.status_code >= 400:
        _logger.warning(
            "failed to PATCH agy external_session_id: "
            "session=%s conversation=%s status=%s body=%s",
            session_id,
            conversation_id,
            response.status_code,
            response.text[:1000],
        )
        return False
    return True


async def _post_session_event(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event_type: str,
    data: dict[str, object],
) -> httpx.Response | None:
    """
    Post one Omnigent session event with bounded transient retries.

    Delegates to :func:`omnigent._native_post_delivery.post_session_event_with_retry`
    which is the shared retry loop for all native forwarders. Conversation items
    persist with a random primary key and no server-side dedup, so an ambiguous
    transport failure (request sent, response lost) is NOT retried — a re-post
    would duplicate the item. Other event types are idempotent/transient and are
    retried.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event_type: Session event type, e.g. ``"external_conversation_item"``.
    :param data: Event data payload.
    :returns: Final HTTP response, or ``None`` when all attempts raised
        transport errors (or after an ambiguous conversation-item failure).
    """
    url = f"/v1/sessions/{url_component(session_id)}/events"
    payload: dict[str, object] = {"type": event_type, "data": data}
    return await post_session_event_with_retry(
        client=client,
        url=url,
        payload=payload,
        event_type=event_type,
        max_attempts=_POST_MAX_ATTEMPTS,
        retry_status_codes=_POST_RETRY_STATUS_CODES,
        sleep=_sleep,
        retry_delay=lambda attempt: _POST_RETRY_DELAY_SECONDS * attempt,
        logger_name=__name__,
    )


# ── Post-hoc policy audit (see :mod:`omnigent.antigravity_native_audit`) ──────


async def _evaluate_tool_call_audit(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    tool_name: str,
    tool_input: Mapping[str, object],
    model: str | None,
) -> dict[str, object] | None:
    """
    POST one tool call to ``/policies/evaluate`` for a post-hoc audit verdict.

    **Post-hoc, fail-open.** The tool has already executed (agy writes a step
    only at ``DONE``); this only *observes* the call. A bounded timeout
    (:data:`_AUDIT_EVAL_TIMEOUT_S`) keeps the transcript mirror from hanging if
    the server parks a ``tool_call`` ASK server-side, and any error/timeout
    returns ``None`` (no verdict → no warning) so the audit never breaks
    forwarding. ``mcp__omnigent__*`` tools are skipped upstream by
    :func:`build_audit_evaluation_request` (already relay-enforced); connector
    MCP tools (e.g. ``mcp__github__*``) are still evaluated.

    :param client: Connected Omnigent HTTP client.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param tool_name: agy tool name, e.g. ``"run_command"``.
    :param tool_input: The tool's arguments.
    :param model: agy model label for ``context.model``, or ``None``.
    :returns: The parsed ``EvaluationResponse`` dict, or ``None`` when the tool
        was not evaluated (mcp tool, transport/timeout error, or non-JSON / >=400
        response) — all of which are treated as "no verdict" (fail-open).
    """
    request = build_audit_evaluation_request(
        tool_name=tool_name, tool_input=tool_input, model=model
    )
    if request is None:
        return None
    url = f"/v1/sessions/{url_component(session_id)}/policies/evaluate"
    try:
        response = await client.post(url, json=request, timeout=_AUDIT_EVAL_TIMEOUT_S)
    except httpx.HTTPError as exc:
        _logger.warning(
            "agy policy audit eval transport error (fail-open): session=%s tool=%s error=%r",
            session_id,
            tool_name,
            exc,
        )
        return None
    if response.status_code >= 400:
        _logger.warning(
            "agy policy audit eval failed (fail-open): session=%s tool=%s status=%s body=%s",
            session_id,
            tool_name,
            response.status_code,
            response.text[:500],
        )
        return None
    try:
        parsed = response.json()
    except ValueError:
        _logger.warning(
            "agy policy audit eval returned non-JSON (fail-open): session=%s tool=%s",
            session_id,
            tool_name,
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def _audit_tool_calls_from_events(
    events: Iterable[OutboundEvent],
) -> list[tuple[int, int, str, dict[str, object]]]:
    """
    Extract ``(step_index, call_ordinal, tool_name, tool_input)`` from a batch.

    The forwarder already emits one ``function_call`` ``external_conversation_item``
    per agy tool call (display-only ``args`` keys already stripped), so the audit
    reuses those rather than re-parsing the transcript. The ``arguments`` JSON
    string is decoded back to a dict for the policy ``tool_input``; a non-decodable
    value yields an empty input (the tool name still audits).

    ``call_ordinal`` is the zero-based position of the call *within its step*
    (reset per ``step_index``), so two violating calls in one ``PLANNER_RESPONSE``
    step get distinct warning response ids (see
    :func:`omnigent.antigravity_native_audit.build_policy_violation_item`).

    :param events: The events posted for one poll batch.
    :returns: One ``(step_index, call_ordinal, tool_name, tool_input)`` record per
        function-call invocation, in order.
    """
    records: list[tuple[int, int, str, dict[str, object]]] = []
    current_step: int | None = None
    call_ordinal = 0
    for event in events:
        if event.event_type != "external_conversation_item":
            continue
        data = event.data
        if data.get("item_type") != "function_call":
            continue
        item = data.get("item_data")
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = item.get("arguments")
        tool_input: dict[str, object] = {}
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                tool_input = decoded
        # Reset the ordinal at each new step so it indexes calls within the step.
        if event.step_index != current_step:
            current_step = event.step_index
            call_ordinal = 0
        records.append((event.step_index, call_ordinal, name, tool_input))
        call_ordinal += 1
    return records


@dataclass(frozen=True)
class _AuditOutcome:
    """
    Per-batch audit outcome used to gate the durable resume cursor (at-least-once).

    The cursor may advance over a step only once that step was both mirror-
    delivered AND fully audited — "fully audited" meaning every violating tool
    call in it had its ``[Policy violation]`` warning POST return success.  A
    warning-POST failure FREEZES the cursor at the prior step (exactly like a
    mirror-POST failure) so the un-acked violation is re-evaluated and re-warned
    on restart instead of being silently dropped (the warning route has no
    server-side dedup, so the alternative — advancing past a failed warning —
    permanently loses the violation).

    Steps that could NOT be evaluated (eval/transport/timeout errors, a parked
    ASK that timed out) are FAIL-OPEN: they do NOT freeze the cursor, because a
    policy-engine error must never wedge the mirror forever.  Only an
    evaluated-violation whose warning POST failed freezes it.

    :param first_unaudited_step: ``step_index`` of the FIRST tool-call step whose
        warning POST failed — the freeze point.  The caller must not advance the
        durable cursor to or past this step (so a restart re-audits it).  ``None``
        when every violating call in the batch was warned (or there were no
        violations / no tool calls), meaning the audit places no ceiling on the
        cursor and the mirror-delivered high-water stands.
    """

    first_unaudited_step: int | None


async def _audit_batch(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    conversation_id: str,
    events: list[OutboundEvent],
    model: str | None,
) -> _AuditOutcome:
    """
    Run the post-hoc policy audit for every tool call in one delivered batch.

    For each ``function_call`` item: evaluate it, and on a violation
    (DENY, or ASK treated DENY-style) POST a warning conversation item and —
    when :data:`_INTERRUPT_ON_AUDIT_DENY` is enabled (OFF by default) —
    best-effort interrupt the in-flight turn.

    **At-least-once (gov FIX A).** The audit reports the first step it could NOT
    confirm warned so the caller can gate the durable cursor on audit success
    too: a step commits only once every violating call in it had its warning POST
    delivered.  Two failure classes are distinguished:

    * **Evaluation/transport error** (verdict ``None`` from
      :func:`_evaluate_tool_call_audit` — a 5xx, timeout, parked ASK, non-JSON,
      or an ``mcp__omnigent__*`` skip): **fail-open**.  The call is treated as audited
      (the cursor may advance over it) so a policy-engine error never freezes
      the mirror.  The violation, if any, is simply not observable.
    * **Evaluated violation whose warning POST FAILED** (``_post_session_event``
      returned ``None`` or ``>= 400``): **freeze**.  The call's step is recorded
      as :attr:`_AuditOutcome.first_unaudited_step`, so the cursor stops before
      it and a restart re-evaluates + re-warns it.  Logged at ERROR (gov FIX B).

    A successfully-warned violation (or an ALLOW/UNSPECIFIED non-violation)
    counts as audited.  Evaluation + warning attempts continue for the rest of
    the batch even after a freeze (the live UI benefits), but the recorded freeze
    point is the EARLIEST failed step, so the caller never advances past it.

    :param client: Connected Omnigent HTTP client.
    :param session_id: Omnigent conversation id.
    :param conversation_id: agy conversation id (namespaces warning ids + targets
        a turn interrupt).
    :param events: The events posted for one poll batch (the mirror-delivered
        prefix; see the tail loop).
    :param model: agy model label for the audit context, or ``None``.
    :returns: An :class:`_AuditOutcome` whose ``first_unaudited_step`` is the
        earliest step whose warning POST failed (the cursor freeze point), or
        ``None`` when every violating call was warned.
    """
    first_unaudited_step: int | None = None
    for step_index, call_ordinal, tool_name, tool_input in _audit_tool_calls_from_events(events):
        warned = await _audit_one_tool_call(
            client,
            session_id,
            conversation_id=conversation_id,
            step_index=step_index,
            call_ordinal=call_ordinal,
            tool_name=tool_name,
            tool_input=tool_input,
            model=model,
        )
        if not warned and first_unaudited_step is None:
            # Earliest step we could not confirm warned: the cursor freeze point.
            # Records the step (not the call) because the durable cursor is
            # step-granular; the whole step is re-audited on restart.
            first_unaudited_step = step_index
    return _AuditOutcome(first_unaudited_step=first_unaudited_step)


async def _audit_one_tool_call(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    conversation_id: str,
    step_index: int,
    call_ordinal: int,
    tool_name: str,
    tool_input: dict[str, object],
    model: str | None,
) -> bool:
    """
    Evaluate one tool call and, on a violation, POST its warning (gov FIX A/B).

    :param client: Connected Omnigent HTTP client.
    :param session_id: Omnigent conversation id.
    :param conversation_id: agy conversation id (namespaces the warning id).
    :param step_index: The transcript step the tool call came from.
    :param call_ordinal: Zero-based position of the call within its step.
    :param tool_name: agy tool name, e.g. ``"run_command"``.
    :param tool_input: The tool's arguments.
    :param model: agy model label for the audit context, or ``None``.
    :returns: ``True`` when the step's cursor may advance over this call — i.e.
        the call was a non-violation, could not be evaluated (fail-open), or was a
        violation whose warning POST was DELIVERED.  ``False`` ONLY when an
        evaluated violation's warning POST failed (``None``/``>= 400``), so the
        caller freezes the durable cursor and the violation is re-warned on
        restart.
    """
    verdict = await _evaluate_tool_call_audit(
        client,
        session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        model=model,
    )
    # ``None`` = could-not-evaluate (eval/transport/timeout error or mcp skip):
    # fail-open. A non-violation verdict (ALLOW/UNSPECIFIED): nothing to warn.
    # Either way the cursor may advance over this call.
    if verdict is None or not audit_verdict_is_violation(verdict):
        return True
    warning = build_policy_violation_item(
        conversation_id=conversation_id,
        step_index=step_index,
        call_ordinal=call_ordinal,
        text=audit_violation_warning_text(verdict),
    )
    response = await _post_session_event(
        client, session_id, event_type="external_conversation_item", data=warning
    )
    delivered = response is not None and response.status_code < 400
    if not delivered:
        # Evaluated→violation→warning POST failed: do NOT advance the cursor past
        # this step, so a restart re-evaluates and re-warns it (at-least-once).
        # The warning route has no server-side dedup, so the cost of this freeze
        # is a RARE duplicate warning on restart vs. silently losing the
        # violation — the product owner's chosen tradeoff (gov FIX A).
        status = "none" if response is None else str(response.status_code)
        _logger.error(
            "failed to deliver post-hoc policy-violation warning for already-executed tool: "
            "session=%s tool=%s step_index=%s call_ordinal=%s status=%s "
            "(cursor frozen; warning re-attempted on restart)",
            session_id,
            tool_name,
            step_index,
            call_ordinal,
            status,
        )
        return False
    _logger.warning(
        "agy post-hoc policy violation (already executed): session=%s tool=%s verdict=%s",
        session_id,
        tool_name,
        verdict.get("result"),
    )
    if _INTERRUPT_ON_AUDIT_DENY:
        await _maybe_interrupt_turn(conversation_id)
    return True


async def _maybe_interrupt_turn(conversation_id: str) -> None:
    """
    Best-effort, fail-open turn interrupt for an audit violation (OFF by default).

    Resolves agy's connect-RPC port for the conversation and calls
    :func:`omnigent.antigravity_native_rpc.interrupt_turn`. Currently a no-op at
    the RPC layer (the cancel contract is unverified — see that function), and
    fully guarded by :data:`_INTERRUPT_ON_AUDIT_DENY` at the call site. Never
    raises: any failure is swallowed so the audit warning (already posted) stands
    on its own.

    :param conversation_id: agy conversation id whose turn to stop.
    :returns: None.
    """
    try:
        port = await asyncio.to_thread(resolve_language_server_port, conversation_id)
    except (OSError, subprocess.SubprocessError):
        # Port discovery shells out to ``lsof`` / enumerates processes; a probe
        # failure must not break forwarding. interrupt_turn itself is fail-open.
        _logger.debug(
            "agy best-effort turn interrupt could not resolve port (ignored): conversation=%s",
            conversation_id,
            exc_info=True,
        )
        return
    if port is None:
        return
    await interrupt_turn(port, conversation_id)


@dataclass(frozen=True)
class _BatchDelivery:
    """
    Per-batch delivery outcome used to advance the durable resume cursor.

    :param delivered_steps: The SET of ``step_index`` values fully delivered in
        this batch with NO failed step at a lower index — the gap-free delivered
        prefix over step VALUES (not arrival order). The caller unions this into
        the persisted ``forwarded_steps`` cursor. A set (not just the high-water)
        so the resume cursor records EXACTLY which steps were acked: a step absent
        from it — including a not-yet-written out-of-order lower step — is re-posted
        on restart instead of being suppressed by a ``<=`` floor.
    :param contiguous_high_water: ``max(delivered_steps)`` or ``None`` when empty.
        Retained as the gap-free watermark for the at-least-once audit gate and
        logging; the durable cursor itself is the set.
    :param fully_delivered: ``True`` iff every event in the batch was delivered
        (no POST failed). When ``False`` the run has hit a delivery gap, so the
        caller must FREEZE the durable cursor for the rest of the run: a later
        batch that succeeds in isolation must not advance the cursor past the
        failed step (that would permanently skip it on resume).
    """

    delivered_steps: frozenset[int]
    contiguous_high_water: int | None
    fully_delivered: bool


async def _post_events(
    client: httpx.AsyncClient,
    session_id: str,
    events: Iterable[OutboundEvent],
) -> _BatchDelivery:
    """
    Post a batch of outbound events in order, reporting per-step delivery.

    Events for a step may arrive interleaved and out of ``step_index`` order
    (agy 1.0.10 writes the transcript out of order). This posts each and tracks
    per-step delivery keyed by ``step_index`` so the caller can advance the
    DURABLE resume cursor (``forwarded_steps``) only as far as is safe.

    Delivery success is per the shared retry helper: a POST is considered
    delivered only when :func:`_post_session_event` returns a ``< 400`` response.
    A ``None`` return (an ambiguous conversation-item transport failure, or
    transport errors after all retries) and a final ``>= 400`` are both treated
    as NOT delivered.

    :attr:`_BatchDelivery.delivered_steps` is the SET of fully-delivered steps
    with NO failed step at a lower ``step_index`` — the gap-free delivered prefix
    over step VALUES (not arrival order), so an out-of-order batch never acks past
    a failed lower step (which would drop it on resume). The failed step and
    everything at/above it are excluded so a restart re-posts them (an acceptable
    duplicate for a higher step that did succeed, versus permanent step loss).
    Posting still continues for every event (the live UI benefits).

    The at-least-once duplicate is also SUB-step: a single step bundles a
    ``message`` plus N ``function_call`` items (and status edges), and delivery is
    tracked per ``step_index`` (a step is delivered iff EVERY event for it
    succeeded). So if item k of a step fails, the WHOLE step re-posts on restart —
    re-emitting the already-committed sibling items 1..k-1 (the events route has no
    server-side dedup). Same at-least-once tradeoff as the step-level one, one
    granularity down.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id.
    :param events: Ordered events to POST (grouped by step, increasing index).
    :returns: A :class:`_BatchDelivery` carrying the delivered step set, its
        gap-free high-water, and whether the whole batch delivered.
    """
    # Per-step delivery keyed by step_index (NOT arrival order): a step is
    # delivered iff EVERY event for it succeeded, regardless of how the events
    # interleave. agy 1.0.10 writes step_index out of order, so a positional scan
    # would mis-attribute the contiguous prefix.
    step_ok: dict[int, bool] = {}
    fully_delivered = True
    for event in events:
        delivered = await _post_session_event_delivered(client, session_id, event=event)
        if not delivered:
            fully_delivered = False
        step_ok[event.step_index] = step_ok.get(event.step_index, True) and delivered
    # Gap-free delivered set over step VALUES: every fully-delivered step that has
    # NO failed step at a lower index. Computed from the step_index sets (not
    # arrival order) so an out-of-order batch (e.g. 12, 14, 13, 15) acks the true
    # gap-free prefix — never past a failed lower step (a resume drop). The caller
    # unions this set into the persisted cursor; ``contiguous_high_water`` is its
    # max, kept for the audit gate + logging.
    failed_steps = [step for step, ok in step_ok.items() if not ok]
    min_failed = min(failed_steps) if failed_steps else None
    delivered_steps = frozenset(
        step for step, ok in step_ok.items() if ok and (min_failed is None or step < min_failed)
    )
    contiguous_high_water = max(delivered_steps) if delivered_steps else None
    return _BatchDelivery(
        delivered_steps=delivered_steps,
        contiguous_high_water=contiguous_high_water,
        fully_delivered=fully_delivered,
    )


async def _post_session_event_delivered(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event: OutboundEvent,
) -> bool:
    """
    POST one event and report whether it was delivered (and committed).

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id.
    :param event: The event to POST.
    :returns: ``True`` only when the server returned a ``< 400`` response
        (item committed). ``False`` on a ``None`` return (ambiguous transport
        failure or retries exhausted) or a ``>= 400`` status — both of which
        mean the step is NOT durably mirrored and must be re-postable on resume.
    """
    response = await _post_session_event(
        client,
        session_id,
        event_type=event.event_type,
        data=event.data,
    )
    if response is None:
        _logger.warning(
            "failed to post agy session event: type=%s step_index=%s",
            event.event_type,
            event.step_index,
        )
        return False
    if response.status_code >= 400:
        _logger.warning(
            "failed to post agy session event: type=%s step_index=%s status=%s body=%s",
            event.event_type,
            event.step_index,
            response.status_code,
            response.text[:1000],
        )
        return False
    return True


async def forward_antigravity_transcript_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    pane: PaneTarget | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    discovery_floor: float | None = None,
    auth: httpx.Auth | None = None,
    ap_transport: httpx.AsyncBaseTransport | None = None,
    transcript_discovery_timeout_s: float = _TRANSCRIPT_DISCOVERY_TIMEOUT_S,
    model: str | None = None,
    audit_policies: bool = False,
    _external_session_id_patched: list[bool] | None = None,
    _audit_notice_posted: list[bool] | None = None,
) -> None:
    """
    Tail agy's transcript and mirror each step into the Omnigent session.

    Resolves agy's real conversation id + transcript path (discovering the id
    because agy ignores the launcher's), polls until the file exists, then tails
    it append-only: each new complete JSON line is mapped to Omnigent events and
    POSTed. Robust to malformed/partial lines and to truncation/rotation.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param headers: Static HTTP headers for Omnigent requests.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Antigravity bridge directory.
    :param pane: This session's runner-owned tmux pane (socket + target). When
        present, discovery binds deterministically to the agy process running
        under that pane, eliminating the newest-dir guess and the concurrent-
        launch livelock. ``None`` (a truly remote runner whose tmux is not
        reachable here) uses the bounded-ambiguity fallback.
    :param poll_interval_s: Seconds between transcript polls.
    :param discovery_floor: Lower bound (epoch seconds) on a brain dir's mtime
        for conversation discovery. ``None`` uses the forwarder start time minus
        a small skew, so only conversations created around/after launch match.
    :param auth: Optional HTTP auth for long-lived remote sessions.
    :param ap_transport: Optional HTTP transport for the Omnigent client (tests
        pass ``httpx.MockTransport(...)``).
    :param transcript_discovery_timeout_s: Max seconds to wait for the
        transcript to appear before returning (the supervisor restarts).
    :param model: agy model label stamped onto the post-hoc audit context (only
        used when ``audit_policies`` is ``True``), or ``None`` when unknown.
    :param audit_policies: When ``True``, run the post-hoc tool-call policy audit
        and post the one-time audit-only degrade notice. Post-hoc — agy cannot
        block a tool before it runs (see
        :mod:`omnigent.antigravity_native_audit`). At-least-once: a detected
        violation whose warning POST fails freezes the durable cursor so it is
        re-warned on restart (fail-open only for policy-engine eval errors; see
        :func:`_tail_transcript`). ``False`` (the default) just mirrors the
        transcript.
    :param _external_session_id_patched: Single-element mutable list used as a
        supervisor-lifetime latch. When the list holds ``[True]`` the PATCH is
        skipped; the supervisor allocates the list and passes the same reference
        across restarts so the PATCH fires exactly once per supervisor lifetime.
        ``None`` (the default) means each call manages its own one-shot: the
        PATCH fires once and the run returns / is cancelled before any restart.
        Tests may pass ``[False]`` to observe the PATCH, or ``[True]`` to skip.
    :param _audit_notice_posted: Supervisor-lifetime latch (single-element list)
        for the one-time audit-only degrade notice; threaded the same way as
        ``_external_session_id_patched`` so a crash-restart does not re-post it.
    :returns: None. Runs until cancelled or the discovery times out.
    """
    if discovery_floor is None:
        # Small negative skew so a conversation dir created microseconds before
        # the forwarder task starts still qualifies.
        discovery_floor = time.time() - 2.0
    resolved = await _resolve_transcript(
        bridge_dir=bridge_dir,
        discovery_floor=discovery_floor,
        poll_interval_s=poll_interval_s,
        timeout_s=transcript_discovery_timeout_s,
        pane=pane,
    )
    if resolved is None:
        return
    conversation_id, transcript_path = resolved
    # Seed the dedup cursor from the persisted resume cursor so a (re)start
    # — supervisor crash-restart OR ``omnigent antigravity --resume`` — re-reads
    # the transcript from offset 0 but re-POSTs only steps NOT already acked. The
    # new-format cursor is the SET of acked steps (membership suppression); a
    # legacy state seeds the old ``<=`` floor instead. Only honor the cursor when
    # it belongs to THIS conversation id (a stale cursor from a prior conversation
    # must not suppress a new transcript's steps); ``_resolve_transcript`` already
    # reset it on a conversation change via ``update_conversation_id``.
    initial_step_high_water, initial_delivered_steps = await asyncio.to_thread(
        _persisted_resume_cursor, bridge_dir, conversation_id
    )
    _logger.info(
        "agy forwarder tailing transcript: session=%s conversation=%s path=%s "
        "resume_from_step_index=%s resume_acked_steps=%d",
        session_id,
        conversation_id,
        transcript_path,
        initial_step_high_water,
        len(initial_delivered_steps),
    )
    parser = TranscriptParser(
        conversation_id=conversation_id,
        initial_step_high_water=initial_step_high_water,
        initial_delivered_steps=initial_delivered_steps,
    )
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        auth=auth,
        timeout=httpx.Timeout(30.0),
        transport=ap_transport,
    ) as ap_client:
        # Persist agy's real id onto the Omnigent session so a later resume can
        # target it (the launcher could not — agy ignores the id it assigns).
        # Guarded by the supervisor-lifetime latch so the PATCH fires at most
        # once even if the supervisor restarts this run on a crash.
        if _external_session_id_patched is None or not _external_session_id_patched[0]:
            patched = await _patch_external_session_id(ap_client, session_id, conversation_id)
            # Latch only on a real success so a transient PATCH failure stays
            # retryable on the next supervisor restart (otherwise the real agy id
            # is never persisted and a later --resume opens a fresh conversation).
            if patched and _external_session_id_patched is not None:
                _external_session_id_patched[0] = True
        await _tail_transcript(
            transcript_path=transcript_path,
            parser=parser,
            ap_client=ap_client,
            session_id=session_id,
            bridge_dir=bridge_dir,
            poll_interval_s=poll_interval_s,
            model=model,
            audit_policies=audit_policies,
            audit_notice_posted=_audit_notice_posted,
        )


async def _tail_transcript(
    *,
    transcript_path: Path,
    parser: TranscriptParser,
    ap_client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    poll_interval_s: float,
    model: str | None = None,
    audit_policies: bool = False,
    audit_notice_posted: list[bool] | None = None,
) -> None:
    """
    Tail one transcript file, posting events for appended steps until cancelled.

    Opens the file fresh each poll cycle (cheap, and tolerant of the file being
    replaced by agy). The on-disk file is the source of truth; the parser's
    PARSE high-water-mark dedup makes re-reads *within the run* idempotent, so a
    reopened/rewound file never double-posts.

    After each batch the DURABLE resume cursor ``forwarded_steps`` (the SET of
    acked step indices) is unioned into bridge state (:func:`update_forwarded_steps`)
    so a later forwarder (re)start — supervisor crash-restart OR ``--resume`` —
    seeds its dedup from it and re-POSTs only steps NOT in the set. Crucially the
    cursor records the per-step DELIVERY result returned by :func:`_post_events`
    (the gap-free delivered prefix, stopping at the first failed POST), NOT the
    parser's PARSE high-water. Persisting the parse high-water would mark a step
    whose POST failed as forwarded and permanently skip it on resume (silent data
    loss); recording only the delivered set means a failed step is re-posted on
    resume instead — an acceptable duplicate. A failed POST therefore freezes the
    cursor below it even though the parser has already advanced past it in memory.
    The cursor is a SET, not a ``<=`` high-water, specifically so a lower step agy
    writes out of order *after* a higher one (e.g. ``13`` after ``14``) in a batch
    that lands after the cursor was last persisted is re-posted on restart rather
    than suppressed by a floor that already passed it.

    **At-least-once policy audit (gov FIX A).** When ``audit_policies`` is on,
    the cursor advance is *also* gated on audit success: a step commits only once
    it was both contiguously mirror-delivered AND fully audited — i.e. every
    violating tool call in it had its ``[Policy violation]`` warning POST
    delivered. The audit therefore runs BEFORE the cursor advances (not after, as
    in the prior at-most-once design), over the mirror-delivered prefix; an
    evaluated-violation whose warning POST fails freezes the cursor at the prior
    step (:func:`_audit_batch` returns the freeze point), exactly like a mirror
    freeze, so a restart re-delivers + re-evaluates + re-warns the un-acked tail.
    This is the product owner's **never-miss** choice: a detected violation
    survives crash/POST-failure even at the cost of a RARE duplicate warning (and
    duplicate mirror items) for the un-acked tail on restart — the warning route
    has no server-side dedup, so exactly-once is impossible without a server
    change. Two failure classes are deliberately distinguished: a policy-engine
    EVAL error (5xx/timeout/parked-ASK/non-JSON) is **fail-open** — it must NOT
    wedge the mirror, so the cursor advances and the violation is simply
    unobservable; only an EVALUATED violation whose warning POST FAILED freezes.

    What is guaranteed: every step at or below the persisted cursor was mirror-
    delivered and (under audit) had all its violation warnings delivered. What
    duplicates are possible on a crash/POST-failure restart: re-delivered mirror
    items AND a re-posted ``[Policy violation]`` for any step in the un-acked tail
    (the tail above the frozen cursor). What is NOT guaranteed: that an
    unobservable verdict (eval error) ever surfaces a warning.

    .. note:: **Mid-turn restart and tool-call pairing.**
       The cursor advances per batch, including mid-turn. A
       ``function_call`` and its ``function_call_output`` are correlated by a
       positional id minted from the in-memory FIFO allocator, which is
       reproduced by *replaying* the step prefix. Because a restart skips the
       persisted prefix instead of replaying it, a restart in the narrow window
       *between* a tool invocation being POSTed and its result step being POSTed
       leaves the result unpaired — it is emitted as a standalone
       ``orphan`` output (still rendered, never dropped). This is a deliberate
       correctness-over-cosmetics tradeoff: the alternative (re-posting from
       step 0 to re-derive the pairing) duplicates the entire mirrored
       transcript on every restart, which is the bug this cursor fixes.

    :param transcript_path: Resolved transcript path.
    :param parser: Stateful transcript parser (owns dedup + buffering).
    :param ap_client: Connected Omnigent HTTP client.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: Native Antigravity bridge directory (cursor is persisted
        here).
    :param poll_interval_s: Seconds between polls.
    :param model: agy model label stamped onto the post-hoc audit context, or
        ``None`` when unknown. Only used when ``audit_policies`` is ``True``.
    :param audit_policies: When ``True``, run the post-hoc tool-call policy audit
        (POST ``/policies/evaluate`` per delivered tool call; surface a warning on
        a DENY/ASK). Post-hoc and at-least-once — a violation whose warning POST
        fails freezes the durable cursor so it is re-warned on restart (fail-open
        only for eval errors; see :func:`_audit_batch`). ``False`` (the default)
        mirrors the transcript with no audit.
    :param audit_notice_posted: Supervisor-lifetime latch (single-element list)
        for the one-time audit-only degrade notice; ``[True]`` once posted so a
        crash-restart does not re-post it. ``None`` manages a local one-shot.
    :returns: None. Runs until cancelled.
    """
    offset = 0
    # Run-level delivery/audit gate. Once any mirror POST OR (under audit) any
    # violation-warning POST in this run fails, the durable cursor must FREEZE: a
    # same-run re-read never retries the failed step (the PARSE high-water
    # suppresses it), so it stays un-acked, and a later batch that delivers in
    # isolation must not advance the cursor past it — that would permanently skip
    # the failed step (and lose its violation warning) on resume. The cursor only
    # resumes advancing on a fresh run (which re-posts + re-audits from the
    # durable cursor) — the at-least-once tradeoff (gov FIX A).
    delivery_stalled = False
    # One-time audit-only degrade notice. Posted before the first batch so the
    # user is told upfront that this harness only audits tool calls after they
    # run (it cannot block). Latched per supervisor lifetime like the
    # external-session-id PATCH so a crash-restart does not re-post it. The latch
    # is "attempted-once" BY DESIGN: it is set after the POST returns regardless
    # of whether the POST succeeded, so a transient failure to deliver the notice
    # is not retried (one best-effort attempt per supervisor lifetime — matching
    # the external-session-id PATCH latch and avoiding a notice storm on a flapping
    # server).
    if audit_policies and (audit_notice_posted is None or not audit_notice_posted[0]):
        await _post_session_event(
            ap_client,
            session_id,
            event_type="external_conversation_item",
            data=build_degrade_notice_item(conversation_id=parser.conversation_id),
        )
        if audit_notice_posted is not None:
            audit_notice_posted[0] = True
    while True:
        events, offset = await asyncio.to_thread(
            _read_transcript_from_offset,
            transcript_path,
            offset,
            parser,
        )
        if events:
            delivery = await _post_events(ap_client, session_id, events)
            # The cursor may only advance when the run has NOT already stalled on
            # a prior batch's gap (mirror OR audit). Capture that pre-batch state
            # so a mid-batch gap in THIS batch still commits the prefix it
            # produced (the prefix that advanced the cursor).
            cursor_may_advance = not delivery_stalled
            if not delivery.fully_delivered:
                delivery_stalled = True
            # ── At-least-once audit (gov FIX A): audit BEFORE advancing the
            # cursor, and gate the advance on audit success too. The cursor
            # advances only over steps that were both contiguously mirror-
            # delivered AND fully audited (every violating call's warning POST
            # delivered). A failed warning POST freezes the cursor exactly like a
            # failed mirror POST, so on restart the parser re-seeds from the
            # frozen cursor, re-delivers + re-audits the un-acked tail, and the
            # violation warning is re-posted instead of silently lost. The cost is
            # a RARE duplicate ``[Policy violation]`` (and duplicate mirror items)
            # for the un-acked tail on restart — the product owner's chosen
            # at-least-once tradeoff (the warning route has no server-side dedup).
            #
            # Audit ONLY the mirror-delivered prefix of this batch (steps in
            # ``delivery.delivered_steps``), never stalled/past-mirror-gap steps: a
            # step that failed to MIRROR is re-delivered AND re-audited together on
            # restart. Fail-open: an eval/transport error does NOT freeze (see
            # :func:`_audit_batch`); only an evaluated-violation whose warning POST
            # failed does.
            committed_steps = delivery.delivered_steps
            if audit_policies and cursor_may_advance and delivery.delivered_steps:
                delivered_prefix = [e for e in events if e.step_index in delivery.delivered_steps]
                audit = await _audit_batch(
                    ap_client,
                    session_id,
                    conversation_id=parser.conversation_id,
                    events=delivered_prefix,
                    model=model,
                )
                if audit.first_unaudited_step is not None:
                    # Freeze: drop the un-acked step and everything at/above it so a
                    # restart re-audits them, and stall the run so no later batch
                    # advances past the gap (mirrors the mirror-delivery freeze).
                    committed_steps = frozenset(
                        step
                        for step in delivery.delivered_steps
                        if step < audit.first_unaudited_step
                    )
                    delivery_stalled = True
            # Advance the DURABLE cursor from the gated DELIVERY+AUDIT result, NOT
            # the parser's PARSE high-water: advancing on parse would mark a
            # failed-POST step as forwarded and permanently skip it on resume. The
            # persist helper unions monotonically, so a no-progress write is a no-op
            # and a frozen cursor never rewinds. The cursor is the SET of acked
            # steps (not a ``<=`` high-water) so a not-yet-written out-of-order
            # lower step is re-posted on restart, not dropped.
            if cursor_may_advance and committed_steps:
                await asyncio.to_thread(
                    update_forwarded_steps,
                    bridge_dir,
                    committed_steps,
                )
        await _sleep(poll_interval_s)


def _read_transcript_from_offset(
    transcript_path: Path,
    offset: int,
    parser: TranscriptParser,
) -> tuple[list[OutboundEvent], int]:
    """
    Read a bounded chunk of a transcript from a byte offset and return events.

    Detects truncation/rotation: when the file is smaller than ``offset`` it was
    rewritten, so reading restarts from 0 (dedup suppresses already-seen steps).

    **Bounded read (gov FIX C).** At most :data:`_MAX_TRANSCRIPT_READ_BYTES` are
    read per call so a large/bursty/long-stalled transcript never materializes
    its whole undelivered suffix (events + delivered prefix + audit records) in
    one batch. When the cap is hit mid-suffix, ``offset`` advances only to the end
    of the last COMPLETE line in the chunk, so a partial final line is re-read
    next poll and the loop catches up over multiple polls. Reading in binary
    keeps byte offsets exact (text-mode ``tell()`` is opaque and a ``replace``-
    decoded re-encode would not reproduce the original byte length).

    :param transcript_path: Resolved transcript path.
    :param offset: Byte offset already consumed.
    :param parser: Stateful transcript parser.
    :returns: ``(events, new_offset)``. On a missing file, ``([], offset)``.
    """
    try:
        size = transcript_path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        # Truncated/rotated in place — restart from the beginning.
        # Unverified assumption: agy step_index is monotonic and append-only
        # within a conversation, so the in-memory high-water from forwarded
        # steps correctly suppresses re-posts even after a rewind.  If a future
        # agy version compacted the file and renumbered step_index from 0, steps
        # 0..high-water would be silently suppressed.  That is unobserved and
        # unlikely given agy's append-only design; document here as a known
        # tradeoff rather than add defensive complexity.
        offset = 0
        parser.reset_buffer()
    if size == offset:
        return [], offset
    try:
        with transcript_path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(_MAX_TRANSCRIPT_READ_BYTES)
    except OSError:
        return [], offset
    if not raw:
        return [], offset
    # Decide the safe end of this chunk. Two independent concerns:
    #  (1) Cap hit (more bytes remain past this read): trim to the last complete
    #      LINE so a partial final line is re-read next poll. The parser buffers a
    #      partial trailing line itself, so feeding a line-incomplete tail is fine.
    #  (2) The retained bytes may end mid multi-byte UTF-8 char — because the cap
    #      split one, OR (the common live-tail case) agy flushed a char partially
    #      and we read straight to EOF. ``decode(errors="replace")`` is LOSSY and
    #      ``new_offset`` advances past the bytes, so a split char could never be
    #      rejoined with its continuation on a later poll — it would corrupt into
    #      U+FFFD. Hold back any incomplete trailing sequence in BOTH the capped
    #      and EOF cases (line buffering happens after decode and cannot repair a
    #      byte-level split).
    capped = offset + len(raw) < size
    if capped:
        last_newline = raw.rfind(b"\n")
        if last_newline != -1:
            # Trim to the last complete line; the partial tail is re-read next poll.
            raw = raw[: last_newline + 1]
    # No-op when ``raw`` ends in a newline or a complete char (a newline is a
    # single byte); only an incomplete trailing multi-byte sequence is dropped, to
    # be re-read whole once its continuation bytes are flushed. Advancing only by
    # the retained bytes keeps ``offset`` on a char boundary every poll.
    raw = _trim_incomplete_utf8_tail(raw)
    if not raw:
        # Nothing safe to consume yet: the chunk is a lone incomplete multi-byte
        # sequence (a sub-4-byte cap mid-char, or agy paused mid-char at EOF).
        # Re-read next poll once more bytes land; the byte offset does not advance.
        return [], offset
    new_offset = offset + len(raw)
    chunk = raw.decode("utf-8", errors="replace")
    events = parser.feed(chunk) if chunk else []
    return events, new_offset


def _trim_incomplete_utf8_tail(raw: bytes) -> bytes:
    """
    Drop a trailing incomplete UTF-8 multi-byte sequence from a byte chunk.

    Used on the bounded-read path (gov FIX C) to hold back a partial trailing
    multi-byte char so it is never split across two ``replace``-decoded reads
    (which would corrupt that one character). Applies both when the cap splits a
    long line AND when a live ``agy`` flush ends the file mid-char at EOF. A
    UTF-8 sequence is at most 4 bytes, so at most the last 3 bytes are examined.

    :param raw: The retained byte chunk for this poll (after any cap/line trim).
        It may end in a newline or a complete char (both no-ops here) or — the
        case this guards — an incomplete trailing multi-byte sequence.
    :returns: ``raw`` with any incomplete trailing UTF-8 sequence removed (the
        whole chunk when it is itself one incomplete sequence).
    """
    # Walk back over continuation bytes (0b10xxxxxx) to the lead byte; if the lead
    # byte's declared length exceeds the bytes present, the sequence is incomplete.
    for back in range(1, min(4, len(raw)) + 1):
        byte = raw[-back]
        if byte & 0b1100_0000 != 0b1000_0000:
            # Lead byte (or ASCII). Determine its expected sequence length.
            if byte & 0b1000_0000 == 0:
                expected = 1
            elif byte & 0b1110_0000 == 0b1100_0000:
                expected = 2
            elif byte & 0b1111_0000 == 0b1110_0000:
                expected = 3
            elif byte & 0b1111_1000 == 0b1111_0000:
                expected = 4
            else:
                # Invalid lead byte; leave it for ``replace`` to handle.
                return raw
            return raw[: len(raw) - back] if back < expected else raw
    # All examined bytes were continuation bytes (no lead byte in the last 4):
    # the tail is a broken sequence — drop those continuation bytes.
    return raw[: len(raw) - min(4, len(raw))]


def _pane_target_from_tmux(
    tmux_socket: Path | None,
    tmux_target: str | None,
) -> PaneTarget | None:
    """
    Build a :class:`PaneTarget` when this process can reach the runner tmux.

    Deterministic discovery needs a tmux pane it can actually probe with
    ``tmux list-panes``. That requires both a socket and a target AND the socket
    to exist on this host — the same condition the launcher uses to decide a
    direct local tmux attach is possible (a remote runner's socket does not
    exist locally). When any of those is missing, returns ``None`` so the
    forwarder uses the bounded-ambiguity fallback.

    :param tmux_socket: Runner tmux server socket path, or ``None``.
    :param tmux_target: tmux ``-t`` target, or ``None``.
    :returns: A :class:`PaneTarget` when the pane is locally reachable, else
        ``None``.
    """
    if tmux_socket is None or tmux_target is None:
        return None
    try:
        if not tmux_socket.exists():
            return None
    except OSError:
        return None
    return PaneTarget(tmux_socket=tmux_socket, tmux_target=tmux_target)


async def supervise_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    tmux_socket: Path | None = None,
    tmux_target: str | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    discovery_floor: float | None = None,
    auth: httpx.Auth | None = None,
    ap_transport: httpx.AsyncBaseTransport | None = None,
    model: str | None = None,
    audit_policies: bool = False,
) -> None:
    """
    Run :func:`forward_antigravity_transcript_to_session` under a supervisor.

    Mirrors the codex/claude native forwarders' ``supervise_forwarder`` role:
    the tail loop runs forever, so any normal return or non-cancellation
    ``Exception`` is treated as a crash and the forwarder is restarted with
    bounded exponential backoff. ``asyncio.CancelledError`` exits cleanly so the
    launcher's teardown runs as before.

    The ``external_session_id`` PATCH is latched per supervisor lifetime: it
    fires once on the first successful run (when agy's real conversation id is
    discovered), and is skipped on subsequent restarts. This mirrors the
    ``external_session_id_mirrored`` flag in the claude-native forwarder.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param headers: Static HTTP headers for Omnigent requests.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Antigravity bridge directory.
    :param tmux_socket: Runner tmux server socket path for this session's agy
        terminal, e.g. ``"/tmp/omnigent-x/tmux.sock"``. Combined with
        *tmux_target* (and only when the socket is locally reachable) to bind
        discovery deterministically to this session's own agy process. ``None``
        (remote runner) uses the bounded-ambiguity fallback.
    :param tmux_target: tmux ``-t`` target for this session's agy pane, e.g.
        ``"main"``. See *tmux_socket*.
    :param poll_interval_s: Seconds between transcript polls.
    :param discovery_floor: Lower bound (epoch seconds) on a brain dir's mtime
        for conversation discovery. Captured once here so restarts keep matching
        the same launch window instead of drifting forward.
    :param auth: Optional HTTP auth for long-lived remote sessions.
    :param ap_transport: Optional HTTP transport for the Omnigent client.
    :param model: agy model label stamped onto the post-hoc audit context (only
        used when ``audit_policies`` is ``True``), or ``None`` when unknown.
    :param audit_policies: When ``True``, run the post-hoc tool-call policy audit
        + one-time audit-only degrade notice (post-hoc, at-least-once — a
        violation whose warning POST fails freezes the durable cursor so it is
        re-warned on restart; agy cannot block a tool before it runs). ``False``
        (the default) mirrors only.
    :returns: None. Cancel the task to stop it.
    """
    if discovery_floor is None:
        discovery_floor = time.time() - 2.0
    pane = _pane_target_from_tmux(tmux_socket, tmux_target)
    if pane is None:
        _logger.info(
            "agy forwarder discovery has no local tmux pane (remote runner?); "
            "using bounded-ambiguity fallback: session=%s",
            session_id,
        )
    # Single-element list so the latch is mutable without closing over a
    # rebindable name. Allocated once per supervisor lifetime and threaded
    # into each run so the PATCH fires exactly once regardless of restarts.
    external_session_id_patched: list[bool] = [False]
    # Same latch shape for the one-time audit-only degrade notice so a crash-
    # restart of the forwarder does not re-post it.
    audit_notice_posted: list[bool] = [False]
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = time.monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_antigravity_transcript_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                pane=pane,
                poll_interval_s=poll_interval_s,
                discovery_floor=discovery_floor,
                auth=auth,
                ap_transport=ap_transport,
                model=model,
                audit_policies=audit_policies,
                _external_session_id_patched=external_session_id_patched,
                _audit_notice_posted=audit_notice_posted,
            )
            _logger.warning(
                "agy transcript forwarder returned unexpectedly; restarting; "
                "session=%s bridge_dir=%s",
                session_id,
                bridge_dir,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Supervisor restarts on ANY non-cancellation Exception (the tail loop
            # is long-lived; a crash must not kill mirroring) — a legitimate
            # boundary catch. BLE001 is waived for these antigravity boundary files
            # via per-file-ignores in pyproject.toml.
            crash_exc = exc
        run_duration_s = time.monotonic() - run_started_at
        if run_duration_s >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            _logger.error(
                "agy transcript forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)
