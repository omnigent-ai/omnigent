"""Forward Codex app-server notifications into Omnigent sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent._native_post_delivery import post_may_have_been_delivered
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexMessage,
    client_for_transport,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    clear_active_turn_id_if_matches,
    codex_home_for_bridge_dir,
    read_bridge_state,
    read_codex_config_model,
    update_active_turn_id,
    update_thread_id,
    write_bridge_state,
)
from omnigent.codex_native_elicitation import (
    codex_elicitation_id,
)
from omnigent.codex_native_elicitation import (
    is_codex_request_id as _is_codex_request_id,
)
from omnigent.entities.session_resources import terminal_resource_id

_logger = logging.getLogger(__name__)

_AGENT_NAME = "codex-native-ui"
_SUBSCRIBE_RETRY_DELAY_SECONDS = 0.2
# How long to wait for a freshly launched Codex TUI to create its
# app-server thread (emit ``thread/started``) before giving up. Generous
# because a host-spawned TUI cold-starts over the runner.
_THREAD_START_TIMEOUT_SECONDS = 30.0
_NO_ROLLOUT_FRAGMENT = "no rollout found for thread id"
# A freshly created thread passes through a second transient state: its rollout
# file exists but is still empty (the TUI created the thread but no turn has
# populated it yet), and ``thread/resume`` then fails with a thread-store
# "... rollout ... is empty" error. Treated as the same retryable not-ready
# state as a missing rollout. Acute with the fresh-launch host auto-create,
# whose listener races the TUI's just-created empty rollout.
_EMPTY_ROLLOUT_FRAGMENT = "is empty"
_POST_MAX_ATTEMPTS = 3
_POST_RETRY_DELAY_SECONDS = 0.1
_POST_RETRY_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_DELTA_FLUSH_INTERVAL_SECONDS = 0.05
_DELTA_FLUSH_CHAR_THRESHOLD = 64
_EXTERNAL_REASONING_EFFORT_CHANGE_TYPE = "external_reasoning_effort_change"
_EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE = "external_codex_collaboration_mode_change"
# Per-attempt client budget for the elicitation long-poll, slightly above
# the server-side wait (``_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S``) so
# the server's own timeout (empty-body fail-ask) wins over a client cut.
# Also reused as the total re-POST budget across severed long-polls.
_CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS = 86405.0
# Fail unreachable-server connects fast into the backoff loop instead of
# inheriting the day-long read budget.
_CODEX_ELICITATION_CONNECT_TIMEOUT_SECONDS = 30.0
# First retry must land inside the server's re-park grace (proxies sever
# idle long-polls); later retries back off.
_CODEX_ELICITATION_RETRY_INITIAL_BACKOFF_SECONDS = 1.0
_CODEX_ELICITATION_RETRY_MAX_BACKOFF_SECONDS = 30.0
_CODEX_MCP_ELICITATION_REQUEST_METHOD = "mcpServer/elicitation/request"
_CODEX_TOOL_REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"
_CODEX_COMMAND_EXECUTION_REQUEST_APPROVAL_METHOD = "item/commandExecution/requestApproval"
_CODEX_FILE_CHANGE_REQUEST_APPROVAL_METHOD = "item/fileChange/requestApproval"
_CODEX_PERMISSIONS_REQUEST_APPROVAL_METHOD = "item/permissions/requestApproval"
_CODEX_EXEC_COMMAND_APPROVAL_METHOD = "execCommandApproval"
_CODEX_APPLY_PATCH_APPROVAL_METHOD = "applyPatchApproval"
_CODEX_SERVER_REQUEST_RESOLVED_METHOD = "serverRequest/resolved"
_EXTERNAL_SESSION_INTERRUPTED_TYPE = "external_session_interrupted"
_EXTERNAL_ELICITATION_RESOLVED_TYPE = "external_elicitation_resolved"
# Codex AgentControl collab-agent spawn event fields.
_CODEX_COLLAB_AGENT_ITEM_TYPE = "collabAgentToolCall"
_CODEX_COLLAB_SPAWN_TOOL = "spawnAgent"
_CODEX_COLLAB_RUNNING_STATUSES = frozenset({"pendingInit", "running"})
_CODEX_COLLAB_FAILED_STATUSES = frozenset({"errored", "notFound"})
# Omnigent control event type sent when a Codex child thread is discovered.
_EXTERNAL_CODEX_SUBAGENT_START_TYPE = "external_codex_subagent_start"
_PLAN_IMPLEMENTATION_QUESTION_ID = "plan_implementation"
_PLAN_IMPLEMENTATION_TITLE = "Implement this plan?"
_PLAN_IMPLEMENTATION_YES = "Yes, implement this plan"
_PLAN_IMPLEMENTATION_CLEAR_CONTEXT = "Yes, clear context and implement"
_PLAN_IMPLEMENTATION_NO = "No, stay in Plan mode"
_PLAN_IMPLEMENTATION_CODING_MESSAGE = "Implement the plan."
_PLAN_IMPLEMENTATION_CLEAR_CONTEXT_PREFIX = (
    "A previous agent produced the plan below to accomplish the user's task. "
    "Implement the plan in a fresh context. Treat the plan as the source of "
    "user intent, re-read files as needed, and carry the work through "
    "implementation and verification."
)
_CODEX_ELICITATION_REQUEST_METHODS = frozenset(
    {
        _CODEX_MCP_ELICITATION_REQUEST_METHOD,
        _CODEX_TOOL_REQUEST_USER_INPUT_METHOD,
        _CODEX_COMMAND_EXECUTION_REQUEST_APPROVAL_METHOD,
        _CODEX_FILE_CHANGE_REQUEST_APPROVAL_METHOD,
        _CODEX_PERMISSIONS_REQUEST_APPROVAL_METHOD,
        _CODEX_EXEC_COMMAND_APPROVAL_METHOD,
        _CODEX_APPLY_PATCH_APPROVAL_METHOD,
    }
)


@dataclass
class _ForwarderTarget:
    """
    Mutable AP/Codex target currently owned by the forwarder.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param thread_id: Codex app-server thread id, e.g.
        ``"0196..."``.
    :param delta_coalescer: Text-delta coalescer posting to
        ``session_id``.
    :param usage_coalescer: Token-usage coalescer posting to
        ``session_id``.
    :param elicitation_tracker: Background Codex elicitation hook
        tracker posting to ``session_id``.
    """

    session_id: str
    thread_id: str
    delta_coalescer: _OutputTextDeltaCoalescer
    usage_coalescer: _SessionUsageCoalescer
    elicitation_tracker: _CodexElicitationTaskTracker


@dataclass(frozen=True)
class _CodexToolCall:
    """
    Normalized view of one completed Codex built-in tool call.

    :param call_id: Codex item id reused as the Omnigent call id, e.g.
        ``"call_abc"``.
    :param name: Omnigent function-call name, e.g. ``"shell"``.
    :param arguments: Tool arguments dict, e.g. ``{"command": "pwd"}``.
    :param output: Tool result text rendered as the
        ``function_call_output``, e.g. ``"/repo\n"``.
    """

    call_id: str
    name: str
    arguments: dict[str, Any]
    output: str


@dataclass
class _PartialTextBuffer:
    """
    In-memory visible text collected from one streaming Codex item.

    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :param item_id: Codex item id, e.g. ``"item_abc123"``, or ``None``
        when a delta omitted it.
    :param parts: Ordered text fragments emitted for this item.
    """

    item_type: str
    item_id: str | None
    parts: list[str] = field(default_factory=list)

    def append(self, delta: str) -> None:
        """
        Append one text fragment to the item buffer.

        :param delta: Text fragment, e.g. ``"hel"``.
        :returns: None.
        """
        self.parts.append(delta)

    def text(self) -> str:
        """
        Return the concatenated item text.

        :returns: Joined text fragments.
        """
        return "".join(self.parts)


@dataclass
class _CodexForwarderState:
    """
    Mutable state for one long-lived Codex forwarder connection.

    :param model: Latest known Codex model for this thread, e.g.
        ``"gpt-5.2-codex"``.
    :param posted_model: Last model already mirrored to Omnigent via an
        ``external_model_change`` post (the dedupe baseline). Seeded from
        the resume/startup model so the spawn default is not echoed back as
        a change; only a later in-TUI ``/model`` switch is mirrored. ``None``
        until seeded.
    :param effort: Latest known Codex reasoning effort for this thread, e.g.
        ``"medium"``. ``None`` means Codex is using its model/default effort.
    :param posted_effort: Last reasoning effort already mirrored to Omnigent
        via ``external_reasoning_effort_change``. ``None`` is a valid mirrored
        value, so ``posted_effort_known`` tracks whether the baseline has been
        seeded.
    :param posted_effort_known: Whether ``posted_effort`` has been mirrored at
        least once. Without this, the initial ``None`` default would be
        indistinguishable from "not yet posted".
    :param collaboration_mode: Latest known Codex collaboration mode kind, e.g.
        ``"plan"`` or ``"default"``.
    :param posted_collaboration_mode: Last collaboration mode kind already
        mirrored to Omnigent via
        ``external_codex_collaboration_mode_change``.
    :param parent_session_id: Omnigent parent session id, e.g.
        ``"conv_parent"``. Set by ``supervise_forwarder`` so collab-agent
        helpers can register child sessions without extra parameter
        threading.
    :param codex_client: Connected Codex app-server client. Set by
        ``supervise_forwarder`` so child backfill can issue
        ``thread/resume`` requests.
    :param subagents_by_thread: Maps Codex child thread ids to Omnigent child
        session ids, e.g. ``{"thread_child": "conv_child"}``.
    :param pending_child_threads: Codex child thread ids announced by
        ``thread/started`` but not yet mapped to AP child sessions,
        mapped to their spawning parent thread id when known, e.g.
        ``{"thread_child": "thread_parent"}``.
    :param subscribed_child_threads: Codex child thread ids whose backlog
        has been replayed for this connection (guards against re-replay
        if the same collab item is observed multiple times).
    :param synced_item_keys: Stable item keys already posted to Omnigent this
        connection, e.g. ``{"thread_c:turn_c:item-1"}``. In-memory only;
        guards replay-vs-live overlap within one forwarder lifetime.
    :param posted_user_turns: Turn ids whose ``userMessage`` has been
        posted to Omnigent this connection, e.g. ``{"turn_123"}``. Used to
        enforce user-before-assistant ordering: before posting a turn's
        assistant reply, the forwarder recovers and posts the turn's user
        message if the live stream missed it (see
        :func:`_ensure_user_message_posted`).
    :param partial_text_by_turn: Visible assistant/plan text fragments keyed
        by turn id, e.g. ``{"turn_123": [_PartialTextBuffer(...)]}``.
        Normal completed items remain the durable source of truth; this
        buffer is only consumed when Codex reports an interrupted turn with no
        completed item for the streamed text.
    :param _anon_item_counters: Per-(thread, turn) counters used to
        assign deterministic positional keys to items that lack a stable
        ``id`` field.
    :param completed_plan_text_by_turn: Completed proposed-plan text
        keyed by turn id.
    :param plan_thread_by_turn: Codex thread id keyed by plan turn id.
    :param prompted_plan_turns: Turn ids that already exposed the
        implementation prompt, either natively or through the Omnigent bridge.
    """

    model: str | None = None
    posted_model: str | None = None
    effort: str | None = None
    posted_effort: str | None = None
    posted_effort_known: bool = False
    collaboration_mode: str | None = None
    posted_collaboration_mode: str | None = None
    parent_session_id: str | None = None
    codex_client: CodexAppServerClient | None = None
    subagents_by_thread: dict[str, str] = field(default_factory=dict)
    pending_child_threads: dict[str, str | None] = field(default_factory=dict)
    subscribed_child_threads: set[str] = field(default_factory=set)
    synced_item_keys: set[str] = field(default_factory=set)
    posted_user_turns: set[str] = field(default_factory=set)
    partial_text_by_turn: dict[str, list[_PartialTextBuffer]] = field(default_factory=dict)
    _anon_item_counters: dict[tuple[str, str], int] = field(default_factory=dict)
    completed_plan_text_by_turn: dict[str, str] = field(default_factory=dict)
    plan_thread_by_turn: dict[str, str] = field(default_factory=dict)
    prompted_plan_turns: set[str] = field(default_factory=set)

    def note_resume_response(self, response: CodexMessage) -> None:
        """
        Record thread settings returned by ``thread/resume``.

        :param response: Codex JSON-RPC response envelope.
        :returns: None.
        """
        result = response.get("result")
        if not isinstance(result, dict):
            return
        self._note_model_fields(result)
        # Do NOT seed ``posted_model`` here. Omnigent must learn the session's
        # ACTUAL model — including the spawn default — because the cost-budget
        # gate resolves the model as ``conv.model_override or spec.llm.model``,
        # and for codex the spawn model (read from ``config.toml`` / the
        # ``--model`` flag) is frequently NOT ``spec.llm.model``. If we seeded
        # the baseline to the spawn model, an unchanged session would never
        # post ``external_model_change``, ``model_override`` would stay
        # ``None``, and the gate would mis-resolve a cheap session as the
        # (possibly expensive/absent) spec model and wrongly DENY it. Leaving
        # ``posted_model`` ``None`` makes the first ``_sync_model_change``
        # mirror the real model; the dedupe still suppresses re-posts after.

    def note_thread_settings_updated(self, params: dict[str, Any]) -> None:
        """
        Record thread settings from a ``thread/settings/updated`` notification.

        :param params: Codex notification params.
        :returns: None.
        """
        settings = params.get("threadSettings")
        if isinstance(settings, dict):
            self._note_model_fields(settings)
            self._note_effort_fields(settings)
            self._note_collaboration_mode_fields(settings)

    def record_completed_plan(self, params: dict[str, Any]) -> None:
        """
        Remember a completed Codex proposed-plan item for its terminal prompt.

        :param params: Codex ``item/completed`` params.
        :returns: None.
        """
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "plan":
            return
        turn_id = _turn_id_from_payload(params)
        thread_id = params.get("threadId")
        text = item.get("text")
        if not (
            isinstance(turn_id, str)
            and turn_id
            and isinstance(thread_id, str)
            and thread_id
            and isinstance(text, str)
            and text.strip()
        ):
            return
        self.completed_plan_text_by_turn[turn_id] = text
        self.plan_thread_by_turn[turn_id] = thread_id

    def mark_prompted(self, turn_id: str) -> None:
        """
        Mark a plan turn as having exposed its implementation prompt.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: None.
        """
        self.prompted_plan_turns.add(turn_id)

    def plan_prompt_context(self, turn_id: str) -> tuple[str, str] | None:
        """
        Return plan text and thread id for a not-yet-prompted turn.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: ``(thread_id, plan_text)`` or ``None``.
        """
        if turn_id in self.prompted_plan_turns:
            return None
        plan_text = self.completed_plan_text_by_turn.get(turn_id)
        thread_id = self.plan_thread_by_turn.get(turn_id)
        if not plan_text or not thread_id:
            return None
        return thread_id, plan_text

    def session_for_child_thread(self, thread_id: str) -> str | None:
        """
        Return the Omnigent child session id for a known Codex child thread.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :returns: Omnigent child session id, e.g. ``"conv_child"``, or ``None``
            when the thread is unknown.
        """
        return self.subagents_by_thread.get(thread_id)

    def note_child_thread(self, thread_id: str, session_id: str) -> None:
        """
        Record the Omnigent child session id for a Codex child thread.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :param session_id: Omnigent child session id, e.g. ``"conv_child"``.
        :returns: None.
        """
        self.subagents_by_thread[thread_id] = session_id
        self.pending_child_threads.pop(thread_id, None)

    def note_parent_rotation(self, session_id: str) -> None:
        """
        Record that the forwarder moved to a new parent AP session.

        :param session_id: New parent AP session id, e.g.
            ``"conv_new_parent"``.
        :returns: None.
        """
        self.parent_session_id = session_id
        self.pending_child_threads.clear()

    def note_pending_child_thread(
        self,
        thread_id: str,
        parent_thread_id: str | None,
    ) -> None:
        """
        Record a Codex child thread before its AP child session exists.

        :param thread_id: Codex child thread id announced by
            ``thread/started``, e.g. ``"thread_child"``.
        :param parent_thread_id: Codex parent thread id recorded in
            ``source.subAgent.thread_spawn.parent_thread_id``, e.g.
            ``"thread_parent"``. ``None`` when Codex omitted it.
        :returns: None.
        """
        if thread_id not in self.subagents_by_thread:
            self.pending_child_threads[thread_id] = parent_thread_id

    def is_pending_child_thread(
        self,
        thread_id: str,
        parent_thread_id: str | None,
    ) -> bool:
        """
        Return whether a thread is an announced-but-unregistered child.

        :param thread_id: Codex thread id, e.g. ``"thread_child"``.
        :param parent_thread_id: Active parent thread id to match, e.g.
            ``"thread_parent"``.
        :returns: ``True`` when the thread was proven to be a child
            by ``source.subAgent.thread_spawn`` metadata but has no AP
            child session mapping yet, and the recorded parent matches.
        """
        recorded_parent_thread_id = self.pending_child_threads.get(thread_id)
        if recorded_parent_thread_id is None:
            return thread_id in self.pending_child_threads
        return recorded_parent_thread_id == parent_thread_id

    def needs_child_thread_backfill(self, thread_id: str) -> bool:
        """
        Return whether a child thread's backlog should be replayed.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :returns: ``True`` until the child has been subscribed this connection.
        """
        return thread_id not in self.subscribed_child_threads

    def note_child_thread_subscribed(self, thread_id: str) -> None:
        """
        Record that a child thread's backlog was replayed this connection.

        :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
        :returns: None.
        """
        self.subscribed_child_threads.add(thread_id)

    def note_user_message_posted(self, turn_id: str) -> None:
        """
        Record that a turn's user message has been posted to AP.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: None.
        """
        self.posted_user_turns.add(turn_id)

    def has_posted_user_message(self, turn_id: str) -> bool:
        """
        Return whether a turn's user message was already posted to AP.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: ``True`` when the turn's user message has been posted.
        """
        return turn_id in self.posted_user_turns

    def record_partial_text_delta(
        self,
        *,
        turn_id: str,
        item_type: str,
        item_id: str | None,
        delta: str,
    ) -> None:
        """
        Remember one visible text delta for possible interrupted-turn durability.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :param item_type: Codex item type, e.g. ``"agentMessage"``.
        :param item_id: Codex item id, e.g. ``"item_abc123"``, or
            ``None`` when omitted.
        :param delta: Text fragment, e.g. ``"hel"``.
        :returns: None.
        """
        buffers = self.partial_text_by_turn.setdefault(turn_id, [])
        for buffer in buffers:
            if buffer.item_type == item_type and buffer.item_id == item_id:
                buffer.append(delta)
                return
        buffer = _PartialTextBuffer(item_type=item_type, item_id=item_id)
        buffer.append(delta)
        buffers.append(buffer)

    def discard_partial_text_item(
        self,
        *,
        turn_id: str,
        item_type: str,
        item_id: str | None,
    ) -> None:
        """
        Drop buffered deltas for an item whose completed record was observed.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :param item_type: Codex item type, e.g. ``"agentMessage"``.
        :param item_id: Codex item id, e.g. ``"item_abc123"``, or
            ``None`` when omitted.
        :returns: None.
        """
        buffers = self.partial_text_by_turn.get(turn_id)
        if not buffers:
            return
        remaining = [
            buffer
            for buffer in buffers
            if not (buffer.item_type == item_type and buffer.item_id == item_id)
        ]
        if remaining:
            self.partial_text_by_turn[turn_id] = remaining
        else:
            self.partial_text_by_turn.pop(turn_id, None)

    def consume_partial_text_for_turn(self, turn_id: str) -> list[_PartialTextBuffer]:
        """
        Remove and return buffered visible text for one turn.

        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: Ordered partial-text buffers for the turn.
        """
        return self.partial_text_by_turn.pop(turn_id, [])

    def claim_item_key(self, item_key: str) -> bool:
        """
        Claim a transcript item key for Omnigent posting.

        Returns ``True`` when the caller should post the item. Returns
        ``False`` when the key was already posted this connection, so the
        caller should skip it and avoid a duplicate write.

        :param item_key: Stable dedup key, e.g.
            ``"thread_c:turn_c:item-1"``.
        :returns: ``True`` when the item should be posted.
        """
        if item_key in self.synced_item_keys:
            _logger.info("Codex forwarder skipped duplicate item: key=%s", item_key)
            return False
        self.synced_item_keys.add(item_key)
        return True

    def peek_anon_item_key(self, thread_id: str, turn_id: str) -> str:
        """
        Return the current positional key for an anonymous (no-id) item.

        Reads but does NOT advance the counter. Use ``advance_anon_counter``
        after a successful ``claim_item_key`` to mark the slot consumed.
        Two calls without an intervening advance return the same key, which
        is what dedup requires: replay and live deliveries of the same
        anonymous item must produce the same key so the second delivery
        is correctly dropped.

        :param thread_id: Codex thread id, e.g. ``"thread_123"``.
        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: Positional dedup key, e.g.
            ``"thread_123:turn_123:anon-0"``.
        """
        scope = (thread_id, turn_id)
        idx = self._anon_item_counters.get(scope, 0)
        return f"{thread_id}:{turn_id}:anon-{idx}"

    def advance_anon_counter(self, thread_id: str, turn_id: str) -> None:
        """
        Advance the anonymous item counter for a (thread, turn) scope.

        Called after ``claim_item_key`` succeeds for an anonymous item so
        the next anonymous item in the same turn gets a fresh key.

        :param thread_id: Codex thread id, e.g. ``"thread_123"``.
        :param turn_id: Codex turn id, e.g. ``"turn_123"``.
        :returns: None.
        """
        scope = (thread_id, turn_id)
        self._anon_item_counters[scope] = self._anon_item_counters.get(scope, 0) + 1

    def _note_model_fields(self, payload: dict[str, Any]) -> None:
        """
        Record model from a Codex settings-like payload.

        :param payload: Payload with ``model``.
        :returns: None.
        """
        model = payload.get("model")
        if isinstance(model, str) and model:
            self.model = model

    def _note_effort_fields(self, payload: dict[str, Any]) -> None:
        """
        Record reasoning effort from a Codex settings-like payload.

        App-server's public ``ThreadSettings`` wire field is ``effort``. The
        other two names are accepted because upstream docs and lower-level
        snapshots use ``reasoningEffort`` / ``reasoning_effort`` when referring
        to the same concept.

        :param payload: Settings payload with ``effort`` or an equivalent
            reasoning-effort key, e.g. ``{"effort": "medium"}``.
        :returns: None.
        """
        for key in ("effort", "reasoningEffort", "reasoning_effort"):
            if key not in payload:
                continue
            effort = payload[key]
            if effort is None or (isinstance(effort, str) and effort):
                self.effort = effort
            return

    def _note_collaboration_mode_fields(self, payload: dict[str, Any]) -> None:
        """
        Record Codex collaboration mode from a settings-like payload.

        :param payload: Settings payload with ``collaborationMode``, e.g.
            ``{"collaborationMode": {"mode": "plan", "settings": {...}}}``.
        :returns: None.
        """
        raw_mode = payload.get("collaborationMode")
        if not isinstance(raw_mode, dict):
            raw_mode = payload.get("collaboration_mode")
        if not isinstance(raw_mode, dict):
            return
        mode = raw_mode.get("mode")
        if isinstance(mode, str) and mode:
            self.collaboration_mode = mode


@dataclass(frozen=True)
class _CodexTurnStatusEdge:
    """
    Omnigent session-status edge derived from Codex turn lifecycle state.

    :param status: Omnigent session status, e.g. ``"running"`` or ``"idle"``.
    :param turn_id: Codex turn id that caused the edge, e.g.
        ``"turn_abc123"``.
    :param source: Lifecycle source that produced the edge, e.g.
        ``"turn/started"``.
    """

    status: str
    turn_id: str | None
    source: str


# Codex ``item/completed`` item types that represent a built-in tool call.
# Each maps to a builder that extracts a normalized :class:`_CodexToolCall`.
# ``_TOOL_ITEM_BUILDERS`` is populated after the builders are defined.
_ToolItemBuilder = Callable[[str, dict[str, Any]], "_CodexToolCall | None"]


@dataclass(frozen=True)
class _DeltaChunk:
    """
    One queued text delta with optional stream identity.

    :param message_id: Stable native message stream id, e.g.
        ``"codex:thread_123:turn_123:agentMessage:item_agent"``, or
        ``None`` for generic unscoped deltas.
    :param delta: Text fragment, e.g. ``"hel"``.
    """

    message_id: str | None
    delta: str


@dataclass(frozen=True)
class _DeltaFlushBarrier:
    """
    Queue marker that asks the delta worker to flush buffered text.

    :param done: Future completed after all preceding buffered deltas
        have been posted to AP.
    """

    done: asyncio.Future[None]


@dataclass(frozen=True)
class _DeltaFlushStop:
    """
    Queue marker that asks the delta worker to flush and exit.

    :param done: Future completed after the worker has flushed all
        buffered deltas and stopped.
    """

    done: asyncio.Future[None]


class _OutputTextDeltaCoalescer:
    """
    Coalesce high-frequency Codex text deltas before posting to AP.

    Codex can emit many tiny ``item/agentMessage/delta`` notifications.
    Posting each one through Omnigent as an awaited HTTP request makes the
    forwarder drain behind Codex. This worker keeps event ingestion
    cheap while preserving the order of flushed text relative to
    explicit flush barriers.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param flush_interval_seconds: Maximum time to hold the first
        buffered delta before posting it.
    :param flush_char_threshold: Maximum buffered character count before
        posting immediately.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        flush_interval_seconds: float = _DELTA_FLUSH_INTERVAL_SECONDS,
        flush_char_threshold: int = _DELTA_FLUSH_CHAR_THRESHOLD,
    ) -> None:
        """
        Initialize the coalescer.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param flush_interval_seconds: Maximum buffering delay in
            seconds, e.g. ``0.05``.
        :param flush_char_threshold: Character threshold that triggers
            an immediate flush, e.g. ``64``.
        """
        self._client = client
        self._session_id = session_id
        self._flush_interval_seconds = flush_interval_seconds
        self._flush_char_threshold = flush_char_threshold
        self._queue: asyncio.Queue[_DeltaChunk | _DeltaFlushBarrier | _DeltaFlushStop] = (
            asyncio.Queue()
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._next_index_by_message_id: dict[str, int] = {}

    async def append(self, delta: str, *, message_id: str | None = None) -> None:
        """
        Queue one assistant text delta for coalesced delivery.

        :param delta: Assistant text fragment, e.g. ``"hel"``.
        :param message_id: Optional stable native message stream id,
            e.g. ``"codex:thread_123:turn_123:agentMessage:item_agent"``.
        :returns: None.
        """
        if not delta:
            return
        self._ensure_worker()
        self._queue.put_nowait(_DeltaChunk(message_id=message_id, delta=delta))

    async def flush(self) -> None:
        """
        Flush all deltas queued before this call.

        :returns: None after all earlier deltas have been posted.
        """
        if self._worker_task is None:
            return
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._queue.put_nowait(_DeltaFlushBarrier(done=done))
        await done

    async def close(self) -> None:
        """
        Flush pending deltas and stop the background worker.

        :returns: None after the worker has stopped.
        """
        if self._worker_task is None:
            return
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._queue.put_nowait(_DeltaFlushStop(done=done))
        await done
        await self._worker_task
        self._worker_task = None

    def _ensure_worker(self) -> None:
        """
        Start the background worker if it is not already running.

        :returns: None.
        """
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(
                self._run(),
                name="codex-native-delta-coalescer",
            )

    async def _run(self) -> None:
        """
        Drain queued deltas and flush barriers in FIFO order.

        :returns: None after a stop marker is processed.
        """
        buffer: list[str] = []
        buffer_message_id: str | None = None
        buffered_chars = 0
        flush_deadline: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            timeout = None
            if buffer and flush_deadline is not None:
                timeout = max(0.0, flush_deadline - loop.time())
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                await self._flush_buffer(buffer, message_id=buffer_message_id)
                buffer = []
                buffer_message_id = None
                buffered_chars = 0
                flush_deadline = None
                continue
            if isinstance(item, _DeltaChunk):
                if buffer and item.message_id != buffer_message_id:
                    await self._flush_buffer(buffer, message_id=buffer_message_id)
                    buffer = []
                    buffer_message_id = None
                    buffered_chars = 0
                    flush_deadline = None
                if not buffer:
                    flush_deadline = loop.time() + self._flush_interval_seconds
                    buffer_message_id = item.message_id
                buffer.append(item.delta)
                buffered_chars += len(item.delta)
                if "\n" in item.delta or buffered_chars >= self._flush_char_threshold:
                    await self._flush_buffer(buffer, message_id=buffer_message_id)
                    buffer = []
                    buffer_message_id = None
                    buffered_chars = 0
                    flush_deadline = None
                continue
            if isinstance(item, _DeltaFlushBarrier):
                await self._flush_buffer(buffer, message_id=buffer_message_id)
                buffer = []
                buffer_message_id = None
                buffered_chars = 0
                flush_deadline = None
                item.done.set_result(None)
                continue
            await self._flush_buffer(buffer, message_id=buffer_message_id)
            item.done.set_result(None)
            return

    async def _flush_buffer(self, buffer: list[str], *, message_id: str | None) -> None:
        """
        Post a non-empty coalesced delta buffer to AP.

        :param buffer: Buffered text fragments, e.g. ``["hel", "lo"]``.
        :param message_id: Stable native message stream id for the
            buffer, e.g. ``"codex:thread_123:turn_123:agentMessage:item"``.
        :returns: None.
        """
        if not buffer:
            return
        delta = "".join(buffer)
        index: int | None = None
        final: bool | None = None
        if message_id is not None:
            index = self._next_index_by_message_id.get(message_id, 0)
            self._next_index_by_message_id[message_id] = index + 1
            final = False
        try:
            await _post_output_text_delta(
                self._client,
                self._session_id,
                delta,
                message_id=message_id,
                index=index,
                final=final,
            )
        except Exception:  # noqa: BLE001 - preserve the long-lived forwarder.
            _logger.warning("Codex forwarder delta flush failed", exc_info=True)


class _SessionUsageCoalescer:
    """
    Coalesce Codex token-usage updates before posting to AP.

    Codex can emit ``thread/tokenUsage/updated`` while assistant text
    is still streaming. This coalescer records only the latest values
    (latest-only, deduped) so repeated frames collapse to one post. The
    caller flushes it per usage frame (so the web UI cost badge updates
    live mid-turn) and again at turn/session boundaries (a no-op when
    nothing changed).

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        model: str | None = None,
    ) -> None:
        """
        Initialize the usage coalescer.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param model: Model name to attach to token posts, e.g. ``"gpt-5.5"``.
            Needed for child coalescers, created where ``forwarder_state`` is
            ``None`` and ``record()`` receives no model — without it the server
            cannot price the child's cumulative tokens. ``None`` for the parent
            coalescer, which learns its model via :meth:`record`.
        :returns: None.
        """
        self._client = client
        self._session_id = session_id
        self._pending: dict[str, int] = {}
        self._last_posted: dict[str, int] = {}
        self._model: str | None = model

    def record(self, params: dict[str, Any], model: str | None = None) -> None:
        """
        Record the latest usage values from one Codex notification.

        :param params: Codex ``thread/tokenUsage/updated`` params.
        :param model: Latest known Codex model for this thread, e.g.
            ``"gpt-5.1-codex"``. Retained so :meth:`flush` can attach it
            to every token post; the server needs it to price cumulative
            tokens into ``total_cost_usd``. ``None`` leaves the prior
            value unchanged (Codex sends usage and settings separately,
            so a usage frame on its own carries no model).
        :returns: None.
        """
        if model:
            self._model = model
        data = _session_usage_data_from_params(params)
        if data is None:
            return
        self._pending.update(data)

    async def flush(self) -> None:
        """
        Post changed pending usage values to AP.

        :returns: None after the pending usage update has been
            attempted.
        """
        if not self._pending:
            return
        data = {
            key: value
            for key, value in self._pending.items()
            if self._last_posted.get(key) != value
        }
        if not data:
            self._pending.clear()
            return
        # Attach the model to every token-bearing post (not via the
        # changed-keys dedup, so it rides along even when only token
        # counts changed) — the server reprices cumulative tokens into
        # ``total_cost_usd`` per turn and needs the model each time.
        payload: dict[str, Any] = dict(data)
        if self._model:
            payload["model"] = self._model
        response = await _post_session_event(
            self._client,
            self._session_id,
            event_type="external_session_usage",
            data=payload,
        )
        _log_failed_session_event_post("external_session_usage", response)
        if response is not None and response.status_code < 400:
            self._last_posted.update(data)
            self._pending.clear()

    async def close(self) -> None:
        """
        Flush pending usage updates.

        :returns: None after the final usage flush has been attempted.
        """
        await self.flush()


@dataclass(frozen=True)
class _PendingCodexElicitation:
    """
    Background Omnigent hook wait for one Codex server-to-client request.

    :param thread_id: Codex thread id from the request params, e.g.
        ``"thread_abc123"``. ``None`` when the request did not carry
        thread scope.
    :param turn_id: Codex turn id from the request params, e.g.
        ``"turn_abc123"``. ``None`` when the request did not carry turn
        scope.
    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param elicitation_id: Omnigent elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    """

    thread_id: str | None
    turn_id: str | None
    request_id: int | str
    elicitation_id: str


class _CodexElicitationTaskTracker:
    """
    Run Codex elicitation hook waits off the event-drain path.

    A real Codex TUI can answer a server-to-client request before the
    Omnigent web/REPL hook does. If the forwarder awaits the Omnigent hook inline,
    it stops draining app-server events and the web UI sees a stuck
    approval card until the hook timeout. This tracker lets the hook
    wait in the background and resolves it once the app-server emits the
    exact ``serverRequest/resolved`` notification for the same request id.
    """

    def __init__(self) -> None:
        """
        Initialize an empty pending-task tracker.

        :returns: None.
        """
        self._pending: dict[asyncio.Task[None], _PendingCodexElicitation] = {}
        self._posted_resolutions: set[str] = set()

    def start(
        self,
        client: httpx.AsyncClient,
        codex_client: CodexAppServerClient,
        *,
        session_id: str,
        event: CodexMessage,
    ) -> None:
        """
        Start one Omnigent hook bridge in the background.

        :param client: HTTP client for Omnigent hook posts.
        :param codex_client: Connected Codex app-server client used
            to send JSON-RPC results.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param event: Codex JSON-RPC request envelope.
        :returns: None.
        """
        params = event.get("params")
        params = params if isinstance(params, dict) else {}
        method = event.get("method")
        request_id = event.get("id")
        if not isinstance(method, str) or not _is_codex_request_id(request_id):
            _logger.warning("Codex forwarder cannot track malformed elicitation request")
            return
        task = asyncio.create_task(
            self._run_one(
                client,
                codex_client,
                session_id=session_id,
                event=event,
            ),
            name="codex-native-elicitation-hook",
        )
        self._pending[task] = _PendingCodexElicitation(
            thread_id=_thread_id_from_params(params),
            turn_id=_turn_id_from_payload(params.get("turn")) or _turn_id_from_payload(params),
            request_id=request_id,
            elicitation_id=codex_elicitation_id(
                session_id,
                method,
                request_id,
            ),
        )
        task.add_done_callback(self._discard_done)

    async def resolve_by_server_notification(
        self,
        client: httpx.AsyncClient,
        *,
        session_id: str,
        params: dict[str, Any],
    ) -> None:
        """
        Mark the hook wait resolved by Codex's explicit notification.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param params: ``serverRequest/resolved`` params, e.g.
            ``{"threadId": "thread_abc", "requestId": 12}``.
        :returns: None.
        """
        request_id = params.get("requestId")
        thread_id = _thread_id_from_params(params)
        for _task, pending in list(self._pending.items()):
            if _pending_elicitation_matches_resolution(
                pending,
                request_id=request_id,
                thread_id=thread_id,
            ):
                await self._post_resolved_once(client, session_id, pending)
                return

    async def resolve_by_terminal_turn_event(
        self,
        client: httpx.AsyncClient,
        *,
        session_id: str,
        params: dict[str, Any],
    ) -> None:
        """
        Clear pending hook waits after Codex accepts a terminal turn.

        This is a conservative fallback for a missed
        ``serverRequest/resolved`` notification. Codex documents
        ``turn/completed`` as the terminal lifecycle event, including
        interrupted and failed turns, and terminal cleanup implies the
        app-server no longer has live server-to-client requests for that
        turn.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param params: Codex ``turn/completed`` params, e.g.
            ``{"threadId": "thread_abc", "turn": {"id": "turn_abc"}}``.
        :returns: None.
        """
        thread_id = _thread_id_from_params(params)
        turn_id = _turn_id_from_payload(params.get("turn")) or _turn_id_from_payload(params)
        for _task, pending in list(self._pending.items()):
            if _pending_elicitation_matches_terminal_turn(
                pending,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                await self._post_resolved_once(client, session_id, pending)

    async def drain(self) -> None:
        """
        Wait for currently pending hook waits without cancelling them.

        :returns: None after every task that was pending at entry has
            reached a terminal state.
        """
        if not self._pending:
            return
        await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def close(self) -> None:
        """
        Cancel all pending hook waits and wait for their cleanup.

        :returns: None after all background hook tasks have finished.
        """
        if not self._pending:
            return
        tasks = list(self._pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._posted_resolutions.clear()

    async def _run_one(
        self,
        client: httpx.AsyncClient,
        codex_client: CodexAppServerClient,
        *,
        session_id: str,
        event: CodexMessage,
    ) -> None:
        """
        Run one hook bridge and log non-cancellation failures.

        :param client: HTTP client for Omnigent hook posts.
        :param codex_client: Connected Codex app-server client.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param event: Codex JSON-RPC request envelope.
        :returns: None.
        """
        try:
            await _handle_codex_elicitation_request(
                client,
                codex_client,
                session_id=session_id,
                event=event,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the long-lived forwarder alive.
            _logger.warning(
                "Codex forwarder elicitation hook task failed: method=%s",
                event.get("method"),
                exc_info=True,
            )

    def _discard_done(self, task: asyncio.Task[None]) -> None:
        """
        Remove a completed task and consume its terminal state.

        :param task: Completed hook task.
        :returns: None.
        """
        pending = self._pending.pop(task, None)
        if task.cancelled():
            if pending is not None:
                self._posted_resolutions.discard(pending.elicitation_id)
            return
        task.exception()
        if pending is not None:
            self._posted_resolutions.discard(pending.elicitation_id)

    async def _post_resolved_once(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        pending: _PendingCodexElicitation,
    ) -> None:
        """
        Post one Omnigent resolution signal, suppressing duplicates.

        :param client: HTTP client for Omnigent event posts.
        :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        :param pending: Pending hook wait metadata to resolve.
        :returns: None.
        """
        if pending.elicitation_id in self._posted_resolutions:
            return
        posted = await _post_external_elicitation_resolved(
            client,
            session_id,
            elicitation_id=pending.elicitation_id,
        )
        if posted:
            self._posted_resolutions.add(pending.elicitation_id)


def _pending_elicitation_matches_resolution(
    pending: _PendingCodexElicitation,
    *,
    request_id: Any,
    thread_id: str | None,
) -> bool:
    """
    Return whether a Codex resolution targets a pending hook wait.

    :param pending: Pending hook wait metadata.
    :param request_id: Codex ``serverRequest/resolved.requestId``,
        e.g. ``12``.
    :param thread_id: Codex ``serverRequest/resolved.threadId``, e.g.
        ``"thread_abc"``, or ``None`` if absent.
    :returns: ``True`` when the notification matches the same request id
        and, when present, the same thread id.
    """
    if pending.request_id != request_id:
        return False
    return pending.thread_id is None or thread_id is None or pending.thread_id == thread_id


def _pending_elicitation_matches_terminal_turn(
    pending: _PendingCodexElicitation,
    *,
    thread_id: str | None,
    turn_id: str | None,
) -> bool:
    """
    Return whether a terminal Codex turn clears a pending hook wait.

    :param pending: Pending hook wait metadata.
    :param thread_id: Codex terminal event thread id, e.g.
        ``"thread_abc"``, or ``None`` if absent.
    :param turn_id: Codex terminal event turn id, e.g.
        ``"turn_abc"``, or ``None`` if absent.
    :returns: ``True`` when the terminal event shares a concrete turn
        or thread scope with the pending request.
    """
    if pending.thread_id is not None and thread_id is not None and pending.thread_id != thread_id:
        return False
    if pending.turn_id is not None and turn_id is not None:
        return pending.turn_id == turn_id
    if pending.thread_id is not None and thread_id is not None:
        return pending.thread_id == thread_id
    return False


async def _sleep(seconds: float) -> None:
    """
    Stubbable indirection for Codex forwarder sleeps.

    Exists so tests can stub retry delays without patching
    ``asyncio.sleep`` through the imported module singleton.

    :param seconds: Delay in seconds.
    :returns: None after the sleep completes.
    """
    await asyncio.sleep(seconds)


async def supervise_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    app_server_url: str,
    thread_id: str,
    client: CodexAppServerClient | None = None,
    auth: httpx.Auth | None = None,
    ap_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """
    Mirror Codex app-server notifications into an Omnigent session.

    :param base_url: Omnigent server base URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param headers: Static HTTP headers for Omnigent requests.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param app_server_url: Codex app-server transport, e.g.
        ``"ws://127.0.0.1:9876"``. Used to (re)connect a fallback
        client when ``client`` is ``None`` and persisted to bridge
        state on thread rotation, so the executor keeps reaching the
        live app-server after a native ``/clear``.
    :param thread_id: Codex thread id to subscribe to.
    :param client: Optional already-connected client. Fresh Codex
        sessions pass the listener that observed ``thread/started``;
        the forwarder still calls ``thread/resume`` once the id is
        known so that connection receives turn/item notifications.
    :param auth: Optional HTTP auth for long-lived remote sessions.
    :param ap_transport: Optional HTTP transport for the Omnigent client,
        e.g. ``httpx.MockTransport(...)`` for tests.
    :returns: None. Runs until cancelled or the app-server connection
        closes.
    """
    if client is None:
        client = client_for_transport(app_server_url, client_name="omnigent-codex-forwarder")
        await client.connect()
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        auth=auth,
        timeout=httpx.Timeout(30.0),
        transport=ap_transport,
    ) as ap_client:
        target = _ForwarderTarget(
            session_id=session_id,
            thread_id=thread_id,
            delta_coalescer=_OutputTextDeltaCoalescer(ap_client, session_id),
            usage_coalescer=_SessionUsageCoalescer(ap_client, session_id),
            elicitation_tracker=_CodexElicitationTaskTracker(),
        )
        forwarder_state = _CodexForwarderState(
            parent_session_id=session_id,
            codex_client=client,
        )
        # Released when the live event stream shows the thread became
        # active (its first turn materializes the rollout). Lets the
        # subscribe task park instead of blind-polling ``thread/resume``
        # for a fresh, still-empty thread. Recreated per thread on rotation.
        thread_active = asyncio.Event()
        subscribe_task = asyncio.create_task(
            _subscribe_until_ready(
                client,
                ap_client,
                session_id=target.session_id,
                bridge_dir=bridge_dir,
                thread_id=target.thread_id,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
                forwarder_state=forwarder_state,
                ready_signal=thread_active,
            ),
            name="codex-native-forwarder-subscribe",
        )
        await _sleep(0)
        try:
            async for event in client.iter_events():
                try:
                    rotated = await _maybe_rotate_session_on_thread_started(
                        ap_client=ap_client,
                        target=target,
                        bridge_dir=bridge_dir,
                        app_server_url=app_server_url,
                        event=event,
                    )
                    if rotated:
                        forwarder_state.note_parent_rotation(target.session_id)
                        subscribe_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await subscribe_task
                        # Fresh thread after a /clear rotation — start its
                        # own active signal so the new subscription parks
                        # until the rotated thread's first turn.
                        thread_active = asyncio.Event()
                        subscribe_task = asyncio.create_task(
                            _subscribe_until_ready(
                                client,
                                ap_client,
                                session_id=target.session_id,
                                bridge_dir=bridge_dir,
                                thread_id=target.thread_id,
                                usage_coalescer=target.usage_coalescer,
                                elicitation_tracker=target.elicitation_tracker,
                                forwarder_state=forwarder_state,
                                ready_signal=thread_active,
                            ),
                            name="codex-native-forwarder-subscribe",
                        )
                        continue
                    # Release the subscribe task as soon as the thread shows
                    # activity (rollout now exists), so it resumes instead of
                    # waiting forever on an idle fresh thread.
                    if not thread_active.is_set() and _event_indicates_thread_active(event):
                        thread_active.set()
                    await _handle_event(
                        ap_client,
                        session_id=target.session_id,
                        bridge_dir=bridge_dir,
                        event=event,
                        delta_coalescer=target.delta_coalescer,
                        usage_coalescer=target.usage_coalescer,
                        elicitation_tracker=target.elicitation_tracker,
                        expected_thread_id=target.thread_id,
                        codex_client=client,
                        forwarder_state=forwarder_state,
                    )
                except Exception:  # noqa: BLE001 - keep the long-lived mirror alive.
                    _logger.warning("Codex forwarder event handling failed", exc_info=True)
        finally:
            await target.delta_coalescer.close()
            await target.usage_coalescer.close()
            await target.elicitation_tracker.close()
            subscribe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subscribe_task
            await client.close()


async def _maybe_rotate_session_on_thread_started(
    *,
    ap_client: httpx.AsyncClient,
    target: _ForwarderTarget,
    bridge_dir: Path,
    app_server_url: str,
    event: CodexMessage,
) -> bool:
    """
    Rotate Omnigent ownership when Codex starts a new native thread.

    Native Codex ``/clear`` starts a fresh app-server thread in the
    existing terminal. The forwarder must move the Omnigent session binding
    to a fresh conversation and then subscribe this same app-server
    connection to the new thread; otherwise web messages keep targeting
    the old thread and streaming appears to end.

    :param ap_client: Omnigent HTTP client used for session rotation.
    :param target: Mutable current AP/Codex target.
    :param bridge_dir: Native Codex bridge directory.
    :param app_server_url: Codex app-server transport, e.g.
        ``"ws://127.0.0.1:9876"``. Persisted to bridge state for the
        replacement session.
    :param event: Codex app-server notification envelope.
    :returns: ``True`` when rotation occurred.
    """
    new_thread_id = _thread_id_from_started_event(event)
    if new_thread_id is None or new_thread_id == target.thread_id:
        return False
    # A Codex AgentControl child thread emits ``thread/started`` when it
    # begins. That event must not rotate the parent Omnigent session — the child
    # is discovered later via a ``collabAgentToolCall`` item and routed to
    # its own Omnigent child session by ``_handle_event``.
    if _thread_started_is_subagent(event):
        return False
    old_delta_coalescer = target.delta_coalescer
    await old_delta_coalescer.flush()
    old_usage_coalescer = target.usage_coalescer
    await old_usage_coalescer.flush()
    old_elicitation_tracker = target.elicitation_tracker
    old_session_id = target.session_id
    new_session_id = await _create_thread_replacement_session(
        client=ap_client,
        old_session_id=old_session_id,
        bridge_dir=bridge_dir,
        app_server_url=app_server_url,
        new_thread_id=new_thread_id,
    )
    target.session_id = new_session_id
    target.thread_id = new_thread_id
    target.delta_coalescer = _OutputTextDeltaCoalescer(ap_client, new_session_id)
    target.usage_coalescer = _SessionUsageCoalescer(ap_client, new_session_id)
    target.elicitation_tracker = _CodexElicitationTaskTracker()
    await old_delta_coalescer.close()
    await old_usage_coalescer.close()
    await old_elicitation_tracker.close()
    _logger.info(
        "Codex forwarder rotated Omnigent session after native thread switch: "
        "old_session=%s new_session=%s new_thread=%s",
        old_session_id,
        new_session_id,
        new_thread_id,
    )
    return True


async def _create_thread_replacement_session(
    *,
    client: httpx.AsyncClient,
    old_session_id: str,
    bridge_dir: Path,
    app_server_url: str,
    new_thread_id: str,
) -> str:
    """
    Create and activate the Omnigent session for a new native Codex thread.

    :param client: Omnigent HTTP client.
    :param old_session_id: Session being rotated away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Codex bridge directory.
    :param app_server_url: Codex app-server transport, e.g.
        ``"ws://127.0.0.1:9876"``. Written to the replacement session's
        bridge state so the executor reaches the live app-server after
        rotation (a unix path here would clobber the ws:// URL).
    :param new_thread_id: Newly started Codex thread id, e.g.
        ``"thread_new"``.
    :returns: New Omnigent session id, e.g. ``"conv_new"``.
    :raises httpx.HTTPStatusError: If Omnigent rejects the create, bind,
        external-session update, or terminal transfer calls.
    :raises RuntimeError: If the old session snapshot or create
        response is malformed.
    """
    old = await _fetch_session_snapshot(client, old_session_id)
    agent_id = old.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise RuntimeError(f"session {old_session_id!r} has no agent_id")
    runner_id = old.get("runner_id")
    labels = old.get("labels") if isinstance(old.get("labels"), dict) else {}
    labels = {str(key): str(value) for key, value in labels.items()}
    state = read_bridge_state(bridge_dir)
    if CODEX_NATIVE_BRIDGE_ID_LABEL_KEY not in labels:
        labels[CODEX_NATIVE_BRIDGE_ID_LABEL_KEY] = old_session_id

    create_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "labels": labels,
        },
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    new_session_id = created.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("Codex thread replacement response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = await client.patch(
            f"/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    external_resp = await client.patch(
        f"/v1/sessions/{url_component(new_session_id)}",
        json={"external_session_id": new_thread_id},
    )
    external_resp.raise_for_status()

    terminal_id = terminal_resource_id("codex", "main")
    transfer_resp = await client.post(
        (
            f"/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=new_session_id,
            socket_path=app_server_url,
            thread_id=new_thread_id,
            codex_home=(
                state.codex_home
                if state is not None
                else str(codex_home_for_bridge_dir(bridge_dir))
            ),
        ),
    )

    clear_resp = await client.patch(
        f"/v1/sessions/{url_component(old_session_id)}",
        json={"runner_id": ""},
    )
    if clear_resp.status_code >= 400:
        _logger.warning(
            "Failed to clear old codex-native runner binding after thread switch; "
            "old_session=%s new_session=%s status=%s body=%s",
            old_session_id,
            new_session_id,
            clear_resp.status_code,
            clear_resp.text,
        )
    return new_session_id


async def _fetch_session_snapshot(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    """
    Fetch an Omnigent session snapshot for Codex session rotation.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Decoded JSON session snapshot.
    :raises httpx.HTTPStatusError: If Omnigent rejects the request.
    :raises RuntimeError: If the response is not a JSON object.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Codex session snapshot response was not an object")
    return payload


async def _subscribe_until_ready(
    client: CodexAppServerClient,
    ap_client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    thread_id: str,
    usage_coalescer: _SessionUsageCoalescer,
    elicitation_tracker: _CodexElicitationTaskTracker,
    forwarder_state: _CodexForwarderState | None = None,
    ready_signal: asyncio.Event | None = None,
) -> None:
    """
    Subscribe this app-server connection to a Codex thread.

    A resume session's thread already has a persisted rollout, so the
    first ``thread/resume`` succeeds and any prior message items are
    replayed immediately.

    A fresh TUI-created thread, however, has *no* rollout until its first
    turn runs — Codex defers materialization for a new thread, so
    ``thread/resume`` rejects it with ``no rollout found``. Rather than
    blind-poll that state (which hammers the app-server for the entire
    idle window before the user's first turn), this parks on
    *ready_signal* and only retries once the caller observes the thread
    become active on the live event stream (its first turn, which
    materializes the rollout). ``thread/status/changed``/turn/item events
    reach the connection without a successful resume, so the caller can
    detect activity and set the signal. A short poll still covers the
    brief window between "thread active" and the rollout being flushed.

    :param client: Codex app-server client.
    :param ap_client: Omnigent HTTP client used for replayed items.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id.
    :param usage_coalescer: Token-usage coalescer for replayed
        app-server events.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param forwarder_state: Optional mutable forwarder state that
        receives thread metadata from the resume response.
    :param ready_signal: Set by the caller when it observes the thread
        become active (rollout now exists). While unset, a not-ready
        thread parks here instead of polling. ``None`` falls back to the
        fixed-interval retry (used where no live event stream drives the
        signal).
    :returns: None.
    """
    saw_not_ready = False
    while True:
        try:
            params: dict[str, Any] = {"threadId": thread_id}
            if not saw_not_ready:
                params["excludeTurns"] = True
            response = await client.request("thread/resume", params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - app-server error envelopes are surfaced as RuntimeError.
            if _is_thread_not_ready_error(exc):
                if not saw_not_ready:
                    _logger.info(
                        "Codex thread %s is not ready yet (no/empty rollout); "
                        "retrying subscription",
                        thread_id,
                    )
                saw_not_ready = True
                if ready_signal is not None and not ready_signal.is_set():
                    # Idle fresh thread: no rollout until the first turn, and
                    # no reason to poll meanwhile. Park until the caller's
                    # event loop observes the thread go active.
                    await ready_signal.wait()
                else:
                    # No signal wired (fallback), or the thread is active but
                    # its rollout isn't flushed yet (brief race) — a short
                    # poll covers that window.
                    await _sleep(_SUBSCRIBE_RETRY_DELAY_SECONDS)
                continue
            _logger.warning("failed to subscribe to Codex thread %s", thread_id, exc_info=True)
            return
        if forwarder_state is not None:
            forwarder_state.note_resume_response(response)
            # Source of truth for the cost policy is config.toml's model (what
            # /model writes). Read it now so model_override reflects it from
            # the first tool call, not a turn later. Falls back to the resume
            # response's model when config.toml has none.
            _refresh_model_from_config(bridge_dir, forwarder_state)
            await _sync_model_change(
                ap_client, session_id=session_id, forwarder_state=forwarder_state
            )
        await _replay_resume_response(
            ap_client,
            session_id=session_id,
            bridge_dir=bridge_dir,
            response=response,
            usage_coalescer=usage_coalescer,
            elicitation_tracker=elicitation_tracker,
            forwarder_state=forwarder_state,
        )
        return


def _event_indicates_thread_active(event: CodexMessage) -> bool:
    """
    Return whether an app-server notification implies the thread is now active.

    A fresh thread's rollout is only materialized once its first turn
    starts, so the subscription's ``thread/resume`` keeps failing until
    then. These notifications all imply a turn has begun (hence the
    rollout now exists), and — crucially — they reach a connection
    *without* a successful resume, so the forwarder's main loop can use
    them to release :func:`_subscribe_until_ready` from its parked wait:

    - any ``turn/*`` or ``item/*`` notification, and
    - ``thread/status/changed`` transitioning to an ``active`` status.

    :param event: A Codex JSON-RPC notification envelope.
    :returns: ``True`` if the event implies the thread became active.
    """
    method = event.get("method")
    if not isinstance(method, str):
        return False
    if method.startswith(("turn/", "item/")):
        return True
    if method == "thread/status/changed":
        params = event.get("params")
        status = params.get("status") if isinstance(params, dict) else None
        return isinstance(status, dict) and status.get("type") == "active"
    return False


def _is_thread_not_ready_error(exc: Exception) -> bool:
    """
    Return whether a subscription failure is Codex's fresh-thread not-ready gap.

    Covers the two transient states a freshly created thread passes through
    before its first turn populates the rollout: the rollout file is missing
    (``no rollout found for thread id``) or present-but-empty
    (``... rollout ... is empty``). Both are retryable — once a turn writes
    the rollout, ``thread/resume`` succeeds.

    :param exc: Exception raised by ``thread/resume``.
    :returns: ``True`` for either retryable not-ready state.
    """
    message = str(exc)
    if _NO_ROLLOUT_FRAGMENT in message:
        return True
    return "rollout" in message and _EMPTY_ROLLOUT_FRAGMENT in message


async def _replay_resume_response(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    response: CodexMessage,
    usage_coalescer: _SessionUsageCoalescer,
    elicitation_tracker: _CodexElicitationTaskTracker,
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Mirror message items returned by ``thread/resume``.

    Passes ``forwarder_state`` into each replayed event so the dedup gate
    in ``_handle_completed_item`` can skip items that the live stream
    already delivered.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: Native Codex bridge directory.
    :param response: Codex ``thread/resume`` response envelope.
    :param usage_coalescer: Token-usage coalescer for replayed
        app-server events.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param forwarder_state: Optional mutable state for dedup and
        sub-agent registration.
    :returns: None.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return
    thread_id = thread.get("id")
    thread_id = thread_id if isinstance(thread_id, str) and thread_id else None
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = _turn_id_from_payload(turn)
        items = turn.get("items")
        if not turn_id or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            await _handle_event(
                client,
                session_id=session_id,
                bridge_dir=bridge_dir,
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": item,
                    },
                },
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                expected_thread_id=thread_id,
                forwarder_state=forwarder_state,
            )
    await _post_resume_terminal_status(
        client,
        session_id=session_id,
        bridge_dir=bridge_dir,
        thread_id=thread_id,
        turns=turns,
    )


async def _post_resume_terminal_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    thread_id: str | None,
    turns: list[Any],
) -> None:
    """
    Publish a missing terminal status edge from ``thread/resume`` data.

    A reconnect can miss the live ``turn/started`` and
    ``turn/completed`` / ``turn/failed`` notifications. When the resume
    payload explicitly says the latest turn on the current thread is
    terminal, the forwarder can close the Omnigent session status even though no
    live terminal boundary was observed. It deliberately does not infer
    terminal state from transcript items alone.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id from the resume payload, e.g.
        ``"thread_123"``.
    :param turns: Raw Codex resume turn list.
    :returns: None.
    """
    if thread_id is None:
        return
    edge = _resume_terminal_status_edge_for_latest_turn(bridge_dir, thread_id, turns)
    await _post_turn_status_edge(client, session_id, edge)


def _resume_terminal_status_edge_for_latest_turn(
    bridge_dir: Path,
    thread_id: str,
    turns: list[Any],
) -> _CodexTurnStatusEdge | None:
    """
    Return the Omnigent terminal status represented by the latest resume turn.

    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id from the resume payload, e.g.
        ``"thread_123"``.
    :param turns: Raw Codex resume turn list.
    :returns: Terminal status edge when the latest turn is terminal and
        belongs to the bridge's current thread; otherwise ``None``.
    """
    state = read_bridge_state(bridge_dir)
    if state is None or state.thread_id != thread_id:
        return None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        turn_id = _turn_id_from_payload(turn)
        if turn_id is None:
            return None
        if state.active_turn_id is not None and state.active_turn_id != turn_id:
            return None
        status = _omnigent_status_from_resume_turn(turn)
        if status is None:
            return None
        update_active_turn_id(bridge_dir, None)
        return _CodexTurnStatusEdge(
            status=status,
            turn_id=turn_id,
            source="thread/resume",
        )
    return None


def _omnigent_status_from_resume_turn(turn: dict[str, Any]) -> str | None:
    """
    Convert an explicit Codex resume turn status to Omnigent session status.

    :param turn: Codex resume turn object, e.g.
        ``{"id": "turn_123", "status": "completed"}``.
    :returns: Omnigent status literal for terminal turns, or ``None`` for active
        or unrecognized statuses.
    """
    status = turn.get("status")
    if isinstance(status, dict):
        status = status.get("type") or status.get("status")
    if status in {"completed", "interrupted", "cancelled", "canceled"}:
        return "idle"
    if status in {"failed", "errored"}:
        return "failed"
    return None


async def _handle_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    event: CodexMessage,
    usage_coalescer: _SessionUsageCoalescer,
    elicitation_tracker: _CodexElicitationTaskTracker,
    delta_coalescer: _OutputTextDeltaCoalescer | None = None,
    expected_thread_id: str | None = None,
    codex_client: CodexAppServerClient | None = None,
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Forward one Codex app-server notification.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param event: Codex notification envelope.
    :param usage_coalescer: Coalescer for high-frequency usage
        notifications.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param delta_coalescer: Optional coalescer for high-frequency
        assistant text deltas.
    :param expected_thread_id: Current Codex thread id. When provided,
        events carrying a different ``threadId`` are stale and ignored.
    :param codex_client: Optional Codex app-server client. Required
        when ``event`` is a server-to-client request that needs a
        JSON-RPC response.
    :param forwarder_state: Optional mutable state for Plan-mode prompt
        synthesis and thread setting tracking.
    :returns: None.
    """
    method = event.get("method")
    params = event.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return
    if forwarder_state is not None and _thread_started_is_subagent(event):
        child_thread_id = _thread_id_from_started_event(event)
        if child_thread_id is not None:
            forwarder_state.note_pending_child_thread(
                child_thread_id,
                _parent_thread_id_from_started_event(event),
            )
        return
    # Resolve routing: parent thread, known child thread, or stale/ignored.
    route_session_id, is_child = _resolve_event_session(
        params, method, expected_thread_id, forwarder_state, fallback_session_id=session_id
    )
    if route_session_id is None:
        return
    # item/started: register collab-agent children early (before item/completed)
    # so live child events can be routed to the child session immediately.
    # Only meaningful for the parent thread; children don't spawn grandchildren here.
    if method == "item/started" and not is_child and forwarder_state is not None:
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == _CODEX_COLLAB_AGENT_ITEM_TYPE:
            await _handle_collab_item(client, params, item, forwarder_state)
        elif isinstance(item, dict) and item.get("type") == "agentMessage":
            # Post the turn's user message NOW — before the assistant's text
            # deltas start streaming. The live ``userMessage`` event can be
            # missed on a fresh thread (subscription lands after it fires);
            # if recovery waited until the assistant's ``item/completed``,
            # the deltas would stream into a transient assistant bubble that
            # renders ABOVE the still-pending user bubble until the turn
            # reconciles. Recovering at assistant-start commits the user
            # message first (it has already materialized in the rollout by
            # now), so the web UI renders the question above the reply. The
            # ``item/completed`` guard below remains the backstop for the
            # resume-backfill path, which replays only ``item/completed``.
            await _ensure_user_message_posted(client, route_session_id, params, forwarder_state)
        return
    if method == _CODEX_SERVER_REQUEST_RESOLVED_METHOD:
        # Resolve on the session the elicitation was published on (a child
        # thread when is_child), not the parent — otherwise a child-thread
        # approval card never flips for the web user watching the child.
        await elicitation_tracker.resolve_by_server_notification(
            client,
            session_id=route_session_id,
            params=params,
        )
        return
    if await _maybe_handle_codex_request(
        client,
        session_id=route_session_id,
        event=event,
        method=method,
        delta_coalescer=delta_coalescer if not is_child else None,
        elicitation_tracker=elicitation_tracker,
        codex_client=codex_client,
        forwarder_state=forwarder_state,
    ):
        return
    # Child token-usage events must post to the child session, not the
    # parent's coalescer. A fresh coalescer is created per-event for
    # children and flushed immediately so accumulated data is not lost.
    # Seed it with the session model so the server can price the child's tokens.
    child_coalescer = (
        _SessionUsageCoalescer(
            client,
            route_session_id,
            model=forwarder_state.model if forwarder_state is not None else None,
        )
        if is_child
        else None
    )
    if await _maybe_handle_turn_event(
        client,
        session_id=route_session_id,
        bridge_dir=bridge_dir if not is_child else Path(),
        method=method,
        params=params,
        usage_coalescer=child_coalescer if is_child else usage_coalescer,
        delta_coalescer=delta_coalescer if not is_child else None,
        elicitation_tracker=elicitation_tracker,
        codex_client=codex_client,
        forwarder_state=forwarder_state if not is_child else None,
    ):
        if child_coalescer is not None:
            await child_coalescer.flush()
        return
    if not is_child and await _maybe_handle_delta_event(
        client,
        session_id=route_session_id,
        bridge_dir=bridge_dir,
        method=method,
        params=params,
        delta_coalescer=delta_coalescer,
        forwarder_state=forwarder_state,
    ):
        return
    if method == "item/completed":
        await _handle_completed_event(
            client,
            session_id=route_session_id,
            params=params,
            delta_coalescer=delta_coalescer if not is_child else None,
            forwarder_state=forwarder_state,
        )


def _resolve_event_session(
    params: dict[str, Any],
    method: str,
    expected_thread_id: str | None,
    forwarder_state: _CodexForwarderState | None,
    *,
    fallback_session_id: str,
) -> tuple[str | None, bool]:
    """
    Resolve which Omnigent session should receive a Codex event.

    Returns ``(session_id, is_child)`` where ``session_id`` is ``None``
    when the event should be silently dropped (stale or unrecognized
    thread). ``is_child`` is ``True`` when the event belongs to a known
    Codex child thread rather than the parent.

    :param params: Codex notification params.
    :param method: Codex method value, e.g. ``"item/completed"``.
    :param expected_thread_id: Active parent Codex thread id, e.g.
        ``"thread_parent"``.
    :param forwarder_state: Optional state holding child-thread mappings.
    :param fallback_session_id: Parent session id used when
        ``forwarder_state`` has no ``parent_session_id`` (e.g. in tests
        that call ``_handle_event`` directly).
    :returns: ``(route_session_id, is_child)`` tuple.
    """
    event_thread_id = _thread_id_from_params(params)
    # Route to a known child session when the event targets a child thread.
    if forwarder_state is not None and event_thread_id is not None:
        child_session_id = forwarder_state.session_for_child_thread(event_thread_id)
        if child_session_id is not None:
            return child_session_id, True
    parent_session_id = (
        forwarder_state.parent_session_id
        if forwarder_state is not None and forwarder_state.parent_session_id is not None
        else fallback_session_id
    )
    # Approval requests from announced child threads must not be dropped just
    # because AP child-session registration is racing behind the request
    # frame. Unknown non-parent threads still hit the stale-thread guard below;
    # only ``thread/started`` events with ``source.subAgent.thread_spawn`` earn
    # this temporary parent routing.
    targets_unregistered_thread = (
        expected_thread_id is not None
        and event_thread_id is not None
        and event_thread_id != expected_thread_id
    )
    targets_pending_child_thread = (
        forwarder_state is not None
        and event_thread_id is not None
        and forwarder_state.is_pending_child_thread(event_thread_id, expected_thread_id)
    )
    if (
        method in _CODEX_ELICITATION_REQUEST_METHODS
        and targets_unregistered_thread
        and targets_pending_child_thread
    ):
        _logger.info(
            "Codex forwarder routed unregistered child-thread elicitation to parent: "
            "method=%s event_thread=%s active_thread=%s",
            method,
            event_thread_id,
            expected_thread_id,
        )
        return parent_session_id, False
    # Drop stale events for threads that are neither the parent nor a child.
    if _event_targets_different_thread(params, method, expected_thread_id):
        return None, False
    return parent_session_id, False


def _event_targets_different_thread(
    params: dict[str, Any],
    method: str,
    expected_thread_id: str | None,
) -> bool:
    """
    Return whether an event belongs to a stale Codex thread.

    :param params: Codex notification params.
    :param method: Codex method value, e.g. ``"item/completed"``.
    :param expected_thread_id: Active Codex thread id, e.g.
        ``"thread_123"``.
    :returns: ``True`` when the event should be ignored as stale.
    """
    event_thread_id = _thread_id_from_params(params)
    if expected_thread_id is None or event_thread_id is None:
        return False
    if event_thread_id == expected_thread_id:
        return False
    _logger.info(
        "Codex forwarder ignored stale thread event: method=%s event_thread=%s active_thread=%s",
        method,
        event_thread_id,
        expected_thread_id,
    )
    return True


async def _maybe_handle_codex_request(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    event: CodexMessage,
    method: str,
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    elicitation_tracker: _CodexElicitationTaskTracker,
    codex_client: CodexAppServerClient | None,
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Handle Codex server-to-client requests if this event is one.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex notification/request envelope.
    :param method: Codex method value, e.g.
        ``"item/tool/requestUserInput"``.
    :param delta_coalescer: Optional text coalescer to flush before a
        blocking request.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param codex_client: Optional app-server client used to answer
        JSON-RPC requests.
    :param forwarder_state: Optional Plan-mode prompt state.
    :returns: ``True`` when the event was a request and needs no
        further dispatch.
    """
    if _is_codex_elicitation_request(event):
        if codex_client is None:
            _logger.warning(
                "Codex forwarder cannot answer elicitation request without app-server client: "
                "method=%s",
                method,
            )
            return True
        if delta_coalescer is not None:
            await delta_coalescer.flush()
        if forwarder_state is not None:
            _note_native_plan_implementation_prompt(forwarder_state, event)
        elicitation_tracker.start(
            client,
            codex_client,
            session_id=session_id,
            event=event,
        )
        return True
    if isinstance(event.get("id"), int | str) and isinstance(method, str):
        _logger.warning("Codex forwarder ignored unsupported server request: method=%s", method)
        return True
    return False


def _refresh_model_from_config(bridge_dir: Path, forwarder_state: _CodexForwarderState) -> None:
    """
    Update the forwarder's known model from this session's ``config.toml``.

    Reads the source-of-truth model via the shared
    :func:`~omnigent.codex_native_bridge.read_codex_config_model` (the
    ``model`` key an in-TUI ``/model`` writes — see that function for why
    config.toml is the source of truth and its caveats) and stores it on
    ``forwarder_state.model`` so a following ``_sync_model_change`` mirrors
    it to Omnigent as ``model_override``. This mirror is a fallback to the codex
    hook, which stamps the live model onto the evaluation request at gate
    time; the gate prefers the hook's value. No-op when the model can't be
    determined, leaving the prior value.

    :param bridge_dir: The session's native-Codex bridge directory.
    :param forwarder_state: Mutable forwarder state whose ``model`` is
        updated in place.
    :returns: None.
    """
    model = read_codex_config_model(bridge_dir)
    if model:
        forwarder_state.model = model


async def _sync_model_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Mirror a Codex TUI ``/model`` switch to Omnigent (web picker + cost gate).

    The active model is recorded on ``forwarder_state.model`` by
    ``_refresh_model_from_config`` (read from ``config.toml``, the source of
    truth for codex — see ``read_codex_config_model``) at subscription and at
    each ``turn/started``, and also by ``thread/settings/updated`` when Codex
    emits one. When that differs from the last-mirrored ``posted_model``
    baseline, POST an
    ``external_model_change`` event so the Omnigent server persists
    ``conv.model_override`` — which keeps the web model dropdown in sync and
    lets the cost-budget policy re-evaluate against the new model. Codex
    model ids are stable per model (unlike Claude's per-turn concrete id),
    so the raw id is posted as-is. Best-effort: a failed post leaves the
    baseline unchanged so the next settings update retries.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param forwarder_state: Mutable forwarder state carrying the current
        model and the last-mirrored baseline.
    :returns: None.
    """
    model = forwarder_state.model
    if not model or model == forwarder_state.posted_model:
        return
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_model_change",
        data={"model": model},
    )
    _log_failed_session_event_post("external_model_change", response)
    if response is not None and response.status_code < 400:
        forwarder_state.posted_model = model


async def _sync_reasoning_effort_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Mirror Codex's active reasoning effort to Omnigent session metadata.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param forwarder_state: Mutable forwarder state carrying the current
        Codex effort and last-mirrored baseline.
    :returns: None.
    """
    effort = forwarder_state.effort
    if forwarder_state.posted_effort_known and effort == forwarder_state.posted_effort:
        return
    response = await _post_session_event(
        client,
        session_id,
        event_type=_EXTERNAL_REASONING_EFFORT_CHANGE_TYPE,
        data={"reasoning_effort": effort},
    )
    _log_failed_session_event_post(_EXTERNAL_REASONING_EFFORT_CHANGE_TYPE, response)
    if response is not None and response.status_code < 400:
        forwarder_state.posted_effort = effort
        forwarder_state.posted_effort_known = True


async def _sync_codex_collaboration_mode_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Mirror Codex's active collaboration mode kind to Omnigent labels.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param forwarder_state: Mutable forwarder state carrying the current
        Codex collaboration mode and last-mirrored baseline.
    :returns: None.
    """
    mode = forwarder_state.collaboration_mode
    if not mode or mode == forwarder_state.posted_collaboration_mode:
        return
    response = await _post_session_event(
        client,
        session_id,
        event_type=_EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE,
        data={"mode": mode},
    )
    _log_failed_session_event_post(_EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE, response)
    if response is not None and response.status_code < 400:
        forwarder_state.posted_collaboration_mode = mode


async def _maybe_handle_turn_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
    usage_coalescer: _SessionUsageCoalescer,
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    elicitation_tracker: _CodexElicitationTaskTracker,
    codex_client: CodexAppServerClient | None,
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Handle turn/thread-level Codex events.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method value, e.g. ``"turn/started"``.
    :param params: Codex notification params.
    :param usage_coalescer: Token-usage coalescer.
    :param delta_coalescer: Optional text-delta coalescer.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param codex_client: Optional app-server client for Plan prompts.
    :param forwarder_state: Optional forwarder state.
    :returns: ``True`` when this event was handled.
    """
    if method == "turn/started":
        if delta_coalescer is not None:
            await delta_coalescer.flush()
        await _handle_turn_started(client, session_id, bridge_dir, params)
        if forwarder_state is not None:
            # An in-TUI ``/model`` switch writes config.toml (the cost-policy
            # source of truth) but emits no notification. Re-read it at turn
            # start so a switch made since the last turn lands ``model_override``
            # on Omnigent before this turn's first tool call reaches the cost gate.
            _refresh_model_from_config(bridge_dir, forwarder_state)
            await _sync_model_change(
                client, session_id=session_id, forwarder_state=forwarder_state
            )
        return True
    if method in {"turn/completed", "turn/failed"}:
        await _handle_terminal_turn_boundary(
            client,
            session_id=session_id,
            bridge_dir=bridge_dir,
            method=method,
            params=params,
            usage_coalescer=usage_coalescer,
            delta_coalescer=delta_coalescer,
            elicitation_tracker=elicitation_tracker,
            codex_client=codex_client,
            forwarder_state=forwarder_state,
        )
        return True
    if method == "thread/tokenUsage/updated":
        _handle_usage_update(usage_coalescer, params, forwarder_state)
        # Flush immediately so the web UI cost badge updates live mid-turn.
        # Codex emits these only every few seconds; the coalescer dedups, so the
        # turn-boundary flush becomes a cheap no-op.
        await usage_coalescer.flush()
        return True
    if method == "thread/settings/updated":
        if forwarder_state is not None:
            forwarder_state.note_thread_settings_updated(params)
            await _sync_model_change(
                client, session_id=session_id, forwarder_state=forwarder_state
            )
            await _sync_reasoning_effort_change(
                client, session_id=session_id, forwarder_state=forwarder_state
            )
            await _sync_codex_collaboration_mode_change(
                client, session_id=session_id, forwarder_state=forwarder_state
            )
        return True
    if method == "turn/plan/updated":
        if delta_coalescer is not None:
            await delta_coalescer.flush()
        await _handle_turn_plan_updated(client, session_id, params)
        return True
    return False


async def _maybe_handle_delta_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Handle Codex streaming text/plan delta events.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method value, e.g.
        ``"item/agentMessage/delta"``.
    :param params: Codex notification params.
    :param delta_coalescer: Text-delta coalescer required for delta
        events.
    :param forwarder_state: Optional forwarder state used to recover a
        missed user message before streaming recovered assistant deltas.
    :returns: ``True`` when this event was a delta event.
    :raises RuntimeError: If a delta event arrives without a text
        coalescer.
    """
    if method == "item/agentMessage/delta":
        if delta_coalescer is None:
            raise RuntimeError("Codex assistant delta handling requires a text-delta coalescer")
        await _handle_agent_message_delta(
            client,
            session_id,
            bridge_dir,
            params,
            delta_coalescer,
            forwarder_state,
        )
        return True
    if method == "item/plan/delta":
        if delta_coalescer is None:
            raise RuntimeError("Codex plan delta handling requires a text-delta coalescer")
        await _handle_plan_delta(
            client,
            session_id,
            bridge_dir,
            params,
            delta_coalescer,
            forwarder_state,
        )
        return True
    return False


async def _handle_completed_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Flush pending text and mirror one completed Codex item.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param delta_coalescer: Optional text-delta coalescer to flush
        before the completed item.
    :param forwarder_state: Optional state that records completed
        Plan-mode items.
    :returns: None.
    """
    if delta_coalescer is not None:
        await delta_coalescer.flush()
    if forwarder_state is not None:
        forwarder_state.record_completed_plan(params)
    await _handle_completed_item(client, session_id, params, forwarder_state=forwarder_state)


async def _handle_terminal_turn_boundary(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
    usage_coalescer: _SessionUsageCoalescer,
    delta_coalescer: _OutputTextDeltaCoalescer | None,
    elicitation_tracker: _CodexElicitationTaskTracker,
    codex_client: CodexAppServerClient | None,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Handle a Codex terminal turn completion/failure boundary.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method, e.g. ``"turn/completed"``.
    :param params: Codex notification params.
    :param usage_coalescer: Coalescer holding latest token usage.
    :param delta_coalescer: Optional text-delta coalescer to flush
        before terminal status and usage.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param codex_client: Optional app-server client used for
        synthesized Plan-mode implementation prompts.
    :param forwarder_state: Optional Plan-mode prompt state.
    :returns: None.
    """
    if delta_coalescer is not None:
        await delta_coalescer.flush()
    await _maybe_persist_interrupted_partial_text(
        client,
        session_id=session_id,
        method=method,
        params=params,
        forwarder_state=forwarder_state,
    )
    handled = await _handle_terminal_turn_event(client, session_id, bridge_dir, method, params)
    if handled:
        await elicitation_tracker.resolve_by_terminal_turn_event(
            client,
            session_id=session_id,
            params=params,
        )
    if (
        handled
        and method == "turn/completed"
        and codex_client is not None
        and forwarder_state is not None
    ):
        await _maybe_handle_plan_implementation_prompt(
            client,
            codex_client,
            session_id=session_id,
            bridge_dir=bridge_dir,
            params=params,
            forwarder_state=forwarder_state,
        )
    if handled:
        await usage_coalescer.flush()


def _handle_usage_update(
    usage_coalescer: _SessionUsageCoalescer,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Record a Codex usage notification without blocking visible output.

    :param usage_coalescer: Coalescer receiving latest token usage.
    :param params: Codex ``thread/tokenUsage/updated`` params.
    :param forwarder_state: Optional forwarder state; its ``model`` is
        attached to the post so the server can price cumulative tokens.
    :returns: None.
    """
    model = forwarder_state.model if forwarder_state is not None else None
    usage_coalescer.record(params, model=model)


async def _handle_turn_plan_updated(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
) -> None:
    """
    Mirror a Codex plan update as a visible assistant message.

    Codex emits plan changes as app-server notifications rather than
    ordinary assistant text. Omnigent web currently renders persisted message
    items, not a dedicated plan item type, so the native bridge converts
    the structured plan into a compact assistant message.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``turn/plan/updated`` params.
    :returns: None.
    """
    text = _plan_text_from_update(params)
    if not text:
        return
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=_response_id(params),
    )


def _is_codex_elicitation_request(event: CodexMessage) -> bool:
    """
    Return whether an app-server frame asks this client for input.

    :param event: Codex app-server envelope.
    :returns: ``True`` for supported server-to-client request methods
        that include a JSON-RPC id.
    """
    return (
        _is_codex_request_id(event.get("id"))
        and isinstance(event.get("method"), str)
        and event["method"] in _CODEX_ELICITATION_REQUEST_METHODS
    )


async def _handle_codex_elicitation_request(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    session_id: str,
    event: CodexMessage,
) -> None:
    """
    Forward one Codex input request to Omnigent and reply to app-server.

    The Omnigent hook publishes the web elicitation and blocks until the
    user answers or the wait budget expires. Non-empty 2xx responses
    are Codex JSON-RPC ``result`` payloads and are sent back to the
    app-server with the original request id. Empty 2xx responses mean
    Omnigent timed out or saw the upstream disconnect, so the forwarder
    leaves the request unanswered for the native Codex UI path.

    :param client: HTTP client for Omnigent hook posts.
    :param codex_client: Connected Codex app-server client used to
        send JSON-RPC results.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex JSON-RPC request envelope.
    :returns: None.
    """
    request_id = event.get("id")
    result = await _codex_elicitation_hook_result(
        client,
        session_id,
        event=event,
    )
    if result is None:
        return
    await codex_client.respond(request_id, result)


async def _codex_elicitation_hook_result(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event: CodexMessage,
) -> dict[str, Any] | None:
    """
    POST a Codex-shaped elicitation request and parse its result body.

    Empty 2xx responses mean Omnigent timed out or saw the upstream
    disconnect, so the caller should leave the native Codex request
    unanswered or drop a synthetic prompt.

    :param client: HTTP client for Omnigent hook posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex JSON-RPC request envelope.
    :returns: Parsed JSON-RPC result payload, or ``None``.
    """
    method = event.get("method")
    request_id = event.get("id")
    response = await _post_codex_elicitation_request(
        client,
        session_id,
        event=event,
    )
    if response is None:
        return None
    if response.status_code >= 400:
        _logger.warning(
            "Codex elicitation hook rejected request: method=%s status=%s body=%s",
            method,
            response.status_code,
            response.text[:512],
        )
        return None
    if not response.content:
        _logger.info(
            "Codex elicitation hook returned empty body; leaving app-server request pending: "
            "method=%s request_id=%r",
            method,
            request_id,
        )
        return None
    try:
        result = response.json()
    except ValueError:
        _logger.warning(
            "Codex elicitation hook returned non-JSON body: method=%s body=%s",
            method,
            response.text[:512],
        )
        return None
    if not isinstance(result, dict):
        _logger.warning(
            "Codex elicitation hook returned non-object result: method=%s result=%r",
            method,
            result,
        )
        return None
    return result


async def _elicitation_retry_sleep(seconds: float) -> None:
    """
    Indirection over :func:`asyncio.sleep` for the elicitation re-POST
    backoff, so tests can stub it without clobbering the process-global
    ``asyncio.sleep``.

    :param seconds: Seconds to sleep, e.g. ``1.0``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


async def _post_codex_elicitation_request(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event: CodexMessage,
) -> httpx.Response | None:
    """
    POST a Codex server-to-client request to the Omnigent hook endpoint,
    re-POSTing across severed long-polls.

    This is deliberately separate from ``_post_session_event``:
    elicitation hook posts are long-poll request/reply calls, not
    idempotent event writes. Proxies sever long-polls and the server can
    restart mid-wait; a single failed POST used to abandon the prompt to
    the native-TUI path — invisible for a headless sub-agent session.
    Codex elicitation ids are deterministic per (session, method, rpc id),
    so a re-POST of the same envelope re-parks the SAME elicitation
    server-side (keeping the approval card alive) and can collect a
    verdict that landed between attempts via the server's pre-resolved
    tombstone. Retries transport errors and 5xx responses within the
    ``_CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS`` budget; 2xx and 4xx
    responses are final.

    :param client: HTTP client for Omnigent hook posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex JSON-RPC request envelope.
    :returns: The final hook response, or ``None`` when the retry budget
        ran out — the caller leaves the native request unanswered, as
        before.
    """
    url = f"/v1/sessions/{url_component(session_id)}/hooks/codex-elicitation-request"
    timeout = httpx.Timeout(
        _CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS,
        connect=_CODEX_ELICITATION_CONNECT_TIMEOUT_SECONDS,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS
    backoff_s = _CODEX_ELICITATION_RETRY_INITIAL_BACKOFF_SECONDS
    while True:
        response: httpx.Response | None = None
        try:
            response = await client.post(url, json=event, timeout=timeout)
        except httpx.HTTPError:
            _logger.warning(
                "Codex elicitation hook POST failed; retrying: method=%s",
                event.get("method"),
                exc_info=True,
            )
        if response is not None and response.status_code < 500:
            return response
        if response is not None:
            # 5xx = proxy gateway error on a severed long-poll, or a
            # restarting server — the verdict may still be pending.
            _logger.warning(
                "Codex elicitation hook returned %s; retrying: method=%s",
                response.status_code,
                event.get("method"),
            )
        if loop.time() + backoff_s >= deadline:
            _logger.warning(
                "Codex elicitation hook retry budget exhausted: method=%s",
                event.get("method"),
            )
            return None
        await _elicitation_retry_sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _CODEX_ELICITATION_RETRY_MAX_BACKOFF_SECONDS)


def _note_native_plan_implementation_prompt(
    forwarder_state: _CodexForwarderState,
    event: CodexMessage,
) -> None:
    """
    Dedupe against Codex builds that emit the Plan prompt natively.

    The current Codex TUI owns the final Plan-mode picker locally, but
    if a future app-server starts emitting it as ``requestUserInput``,
    the Omnigent bridge should relay that native request and skip its
    synthetic fallback for the same turn.

    :param forwarder_state: Mutable forwarder state.
    :param event: Codex server-to-client request envelope.
    :returns: None.
    """
    if event.get("method") != _CODEX_TOOL_REQUEST_USER_INPUT_METHOD:
        return
    params = event.get("params")
    if not isinstance(params, dict):
        return
    if not _is_plan_implementation_request_user_input(params):
        return
    turn_id = _turn_id_from_payload(params)
    if turn_id is not None:
        forwarder_state.mark_prompted(turn_id)


def _is_plan_implementation_request_user_input(params: dict[str, Any]) -> bool:
    """
    Return whether ``requestUserInput`` is the Plan implementation picker.

    :param params: Codex ``item/tool/requestUserInput`` params.
    :returns: ``True`` for the final Plan-mode implementation prompt.
    """
    questions = params.get("questions")
    if not isinstance(questions, list):
        return False
    for question in questions:
        if not isinstance(question, dict):
            continue
        if question.get("id") == _PLAN_IMPLEMENTATION_QUESTION_ID:
            return True
        if question.get("question") == _PLAN_IMPLEMENTATION_TITLE:
            return True
    return False


async def _maybe_handle_plan_implementation_prompt(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Publish and resolve the Plan-mode implementation prompt in Omnigent Web.

    Codex's terminal UI asks ``Implement this plan?`` after a completed
    Plan-mode turn, but that picker is local to the TUI. The app-server
    does emit the completed ``plan`` item, so the forwarder synthesizes
    the same user-facing question through the existing Codex
    ``requestUserInput`` hook and starts the selected follow-up turn.

    :param client: HTTP client for Omnigent hook posts.
    :param codex_client: Connected Codex app-server client.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``turn/completed`` params.
    :param forwarder_state: Mutable forwarder state.
    :returns: None.
    """
    turn_id = _turn_id_from_payload(params.get("turn")) or _turn_id_from_payload(params)
    if turn_id is None:
        return
    context = forwarder_state.plan_prompt_context(turn_id)
    if context is None:
        return
    thread_id, plan_text = context
    forwarder_state.mark_prompted(turn_id)
    result = await _codex_elicitation_hook_result(
        client,
        session_id,
        event=_plan_implementation_request_event(thread_id, turn_id),
    )
    selected = _selected_plan_implementation_answer(result)
    if selected == _PLAN_IMPLEMENTATION_NO or selected is None:
        return
    if selected == _PLAN_IMPLEMENTATION_YES:
        await _start_plan_implementation_turn(
            codex_client,
            bridge_dir=bridge_dir,
            thread_id=thread_id,
            text=_PLAN_IMPLEMENTATION_CODING_MESSAGE,
            forwarder_state=forwarder_state,
        )
        return
    if selected == _PLAN_IMPLEMENTATION_CLEAR_CONTEXT:
        await _start_clear_context_plan_implementation_turn(
            codex_client,
            bridge_dir=bridge_dir,
            plan_text=plan_text,
            forwarder_state=forwarder_state,
        )


def _plan_implementation_request_event(thread_id: str, turn_id: str) -> CodexMessage:
    """
    Build a Codex ``requestUserInput`` request for the Plan prompt.

    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :param turn_id: Codex turn id that produced the plan, e.g.
        ``"turn_123"``.
    :returns: Codex JSON-RPC request envelope.
    """
    return {
        "id": f"plan_implementation:{turn_id}",
        "method": _CODEX_TOOL_REQUEST_USER_INPUT_METHOD,
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": f"{turn_id}:plan_implementation",
            "questions": [
                {
                    "id": _PLAN_IMPLEMENTATION_QUESTION_ID,
                    "header": "Plan",
                    "question": _PLAN_IMPLEMENTATION_TITLE,
                    "isOther": False,
                    "isSecret": False,
                    "options": [
                        {
                            "label": _PLAN_IMPLEMENTATION_YES,
                            "description": "Switch to Default and start coding.",
                        },
                        {
                            "label": _PLAN_IMPLEMENTATION_CLEAR_CONTEXT,
                            "description": "Fresh thread with this plan.",
                        },
                        {
                            "label": _PLAN_IMPLEMENTATION_NO,
                            "description": "Continue planning with the model.",
                        },
                    ],
                }
            ],
        },
    }


def _selected_plan_implementation_answer(result: dict[str, Any] | None) -> str | None:
    """
    Extract the selected Plan prompt label from a Codex hook result.

    :param result: Codex ``requestUserInput`` result payload.
    :returns: Selected option label, or ``None`` when absent.
    """
    if result is None:
        return None
    answers = result.get("answers")
    if not isinstance(answers, dict):
        return None
    question_answer = answers.get(_PLAN_IMPLEMENTATION_QUESTION_ID)
    if not isinstance(question_answer, dict):
        return None
    values = question_answer.get("answers")
    if not isinstance(values, list) or not values:
        return None
    selected = values[0]
    return selected if isinstance(selected, str) and selected else None


async def _start_plan_implementation_turn(
    codex_client: CodexAppServerClient,
    *,
    bridge_dir: Path,
    thread_id: str,
    text: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Start a Codex Default-mode implementation turn on an existing thread.

    :param codex_client: Connected Codex app-server client.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :param text: User input for the turn.
    :param forwarder_state: Mutable state with the current model.
    :returns: None.
    """
    collaboration_mode = _default_collaboration_mode(forwarder_state)
    if collaboration_mode is None:
        _logger.warning("Codex plan implementation skipped: current model is unknown")
        return
    response = await codex_client.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "collaborationMode": collaboration_mode,
        },
    )
    turn_id = response.get("result", {}).get("turn", {}).get("id")
    if isinstance(turn_id, str) and turn_id:
        update_active_turn_id(bridge_dir, turn_id)


async def _start_clear_context_plan_implementation_turn(
    codex_client: CodexAppServerClient,
    *,
    bridge_dir: Path,
    plan_text: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Start a fresh Codex thread and implement the completed plan there.

    :param codex_client: Connected Codex app-server client.
    :param bridge_dir: Native Codex bridge directory.
    :param plan_text: Completed plan markdown from the prior thread.
    :param forwarder_state: Mutable state with the current model.
    :returns: None.
    """
    if not forwarder_state.model:
        _logger.warning(
            "Codex clear-context plan implementation skipped: current model is unknown"
        )
        return
    thread_response = await codex_client.request(
        "thread/start",
        {"model": forwarder_state.model, "sessionStartSource": "clear"},
    )
    thread_id = thread_response.get("result", {}).get("thread", {}).get("id")
    if not isinstance(thread_id, str) or not thread_id:
        _logger.warning("Codex clear-context plan implementation skipped: new thread id missing")
        return
    update_thread_id(bridge_dir, thread_id)
    text = f"{_PLAN_IMPLEMENTATION_CLEAR_CONTEXT_PREFIX}\n\n{plan_text}"
    await _start_plan_implementation_turn(
        codex_client,
        bridge_dir=bridge_dir,
        thread_id=thread_id,
        text=text,
        forwarder_state=forwarder_state,
    )


def _default_collaboration_mode(
    forwarder_state: _CodexForwarderState,
) -> dict[str, Any] | None:
    """
    Build Codex's Default collaboration mode for ``turn/start``.

    ``developer_instructions: null`` deliberately asks Codex
    app-server to fill in the built-in Default-mode instructions via
    its own normalization path.

    :param forwarder_state: Mutable state with the current model.
    :returns: Codex ``CollaborationMode`` JSON object, or ``None``.
    """
    if not forwarder_state.model:
        return None
    return {
        "mode": "default",
        "settings": {
            "model": forwarder_state.model,
            "reasoning_effort": None,
            "developer_instructions": None,
        },
    }


async def _handle_turn_started(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
) -> None:
    """
    Forward a Codex terminal turn start event.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``turn/started`` params.
    :returns: None.
    """
    edge = _turn_started_status_edge(bridge_dir, params)
    await _post_turn_status_edge(client, session_id, edge)


def _turn_started_status_edge(
    bridge_dir: Path,
    params: dict[str, Any],
) -> _CodexTurnStatusEdge:
    """
    Record a Codex turn start and return the Omnigent running edge.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``turn/started`` params.
    :returns: Running status edge for the observed turn start.
    """
    turn = params.get("turn")
    turn_id = _turn_id_from_payload(turn) or _turn_id_from_payload(params)
    update_active_turn_id(bridge_dir, turn_id)
    return _CodexTurnStatusEdge(
        status="running",
        turn_id=turn_id,
        source="turn/started",
    )


async def _handle_terminal_turn_event(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
) -> bool:
    """
    Forward a terminal-observed Codex turn completion/failure event.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex method, e.g. ``"turn/completed"``.
    :param params: Codex turn event params.
    :returns: ``True`` when the terminal event belonged to the active
        turn and was forwarded, ``False`` when it was stale.
    """
    edge = _terminal_turn_status_edge(bridge_dir, method, params)
    if edge is None:
        terminal_turn_id = _terminal_turn_id_from_params(params)
        _logger.info(
            "Codex forwarder ignored stale terminal turn event: method=%s turn_id=%s",
            method,
            terminal_turn_id,
        )
        return False
    await _post_turn_status_edge(client, session_id, edge)
    return True


def _terminal_turn_status_edge(
    bridge_dir: Path,
    method: str,
    params: dict[str, Any],
) -> _CodexTurnStatusEdge | None:
    """
    Return the terminal Omnigent edge for a Codex terminal turn event.

    The edge is produced when the event clears the recorded active turn, or
    when it safely recovers a missed ``turn/started`` for the bridge's current
    thread. Stale or ambiguous terminal events return ``None``.

    :param bridge_dir: Native Codex bridge directory.
    :param method: Codex terminal method, e.g. ``"turn/completed"``.
    :param params: Codex turn event params.
    :returns: Terminal status edge, or ``None`` when the event is stale.
    """
    terminal_turn_id = _terminal_turn_id_from_params(params)
    if not clear_active_turn_id_if_matches(bridge_dir, terminal_turn_id):
        if not _terminal_turn_boundary_matches_idle_bridge(bridge_dir, params, terminal_turn_id):
            return None
        source = f"{method}:recovered"
    else:
        source = method
    return _CodexTurnStatusEdge(
        status="idle" if method == "turn/completed" else "failed",
        turn_id=terminal_turn_id,
        source=source,
    )


def _terminal_turn_id_from_params(params: dict[str, Any]) -> str | None:
    """
    Extract the terminal turn id from Codex turn-boundary params.

    :param params: Codex ``turn/completed`` / ``turn/failed`` params.
    :returns: Codex turn id, e.g. ``"turn_abc123"``, or ``None``.
    """
    turn = params.get("turn")
    return _turn_id_from_payload(turn) or _turn_id_from_payload(params)


def _terminal_turn_boundary_matches_idle_bridge(
    bridge_dir: Path,
    params: dict[str, Any],
    terminal_turn_id: str | None,
) -> bool:
    """
    Return whether a terminal boundary can close a missed-start turn.

    A Codex listener can miss ``turn/started`` while reconnecting. If no
    active turn is recorded, but a later ``turn/completed`` / ``turn/failed``
    event carries the current thread id, the forwarder may safely publish the
    terminal status edge. If another active turn is recorded, the event is
    stale or ambiguous and must stay ignored.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex terminal turn event params.
    :param terminal_turn_id: Terminal turn id from the event, e.g.
        ``"turn_abc123"``.
    :returns: ``True`` when the event belongs to the bridge's current idle
        thread and can publish the terminal status edge.
    """
    if terminal_turn_id is None:
        return False
    state = read_bridge_state(bridge_dir)
    if state is None or state.active_turn_id is not None:
        return False
    return _thread_id_from_params(params) == state.thread_id


def _claim_completed_item(
    params: dict[str, Any],
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState | None,
) -> bool:
    """
    Claim one completed Codex transcript item for Omnigent posting.

    Returns ``True`` when the caller should post the item; ``False`` when
    it was already posted this connection (dedup gate). Also advances the
    anonymous-item counter on a successful claim so the next anonymous
    item in the same (thread, turn) gets a fresh key.

    When ``forwarder_state`` is ``None``, dedup is disabled and the
    function always returns ``True`` (used in tests that bypass
    ``supervise_forwarder``).

    :param params: Codex ``item/completed`` params.
    :param item: Codex item payload.
    :param forwarder_state: Optional mutable state holding synced-item
        keys and anonymous-item counters.
    :returns: ``True`` when the item should be posted to AP.
    """
    if forwarder_state is None:
        return True
    item_key, is_anon = _completed_item_key(params, item, forwarder_state)
    if not forwarder_state.claim_item_key(item_key):
        return False
    if is_anon:
        thread_id = _thread_id_from_params(params) or "thread"
        turn_id = params.get("turnId")
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else "turn"
        forwarder_state.advance_anon_counter(thread_id, turn_id)
    return True


async def _handle_completed_item(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    *,
    forwarder_state: _CodexForwarderState | None = None,
) -> None:
    """
    Forward one Codex completed item event when it maps to Omnigent history.

    Deduplicates via ``_claim_completed_item`` so replay and live deliveries
    of the same item only write once. Collab items are dispatched separately.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param forwarder_state: Optional mutable state for dedup tracking.
    :returns: None.
    """
    item = params.get("item")
    if not isinstance(item, dict):
        return
    item_type = item.get("type")
    turn_id = _turn_id_from_payload(params)
    if item_type in {"agentMessage", "plan"} and forwarder_state is not None and turn_id:
        item_id = item.get("id")
        forwarder_state.discard_partial_text_item(
            turn_id=turn_id,
            item_type=item_type,
            item_id=item_id if isinstance(item_id, str) and item_id else None,
        )
    _logger.info(
        "Codex forwarder observed completed item: turn_id=%s item_type=%s",
        turn_id,
        item_type,
    )
    # Collab-agent items register child sessions; they do not append transcript
    # records and must not go through the dedup gate.
    if item_type == _CODEX_COLLAB_AGENT_ITEM_TYPE:
        if forwarder_state is not None:
            await _handle_collab_item(client, params, item, forwarder_state)
        return
    if not _claim_completed_item(params, item, forwarder_state):
        return
    if item_type == "userMessage":
        await _post_user_message(client, session_id, params, item)
        if forwarder_state is not None:
            turn_id = _turn_id_from_payload(params)
            if turn_id:
                forwarder_state.note_user_message_posted(turn_id)
        return
    if item_type == "agentMessage":
        # User-before-assistant ordering guarantee. On a fresh thread the
        # forwarder subscribes via ``thread/resume`` only after the first
        # turn starts, so the early ``userMessage`` event can stream past
        # before the subscription lands — it is then recovered only via a
        # later resume backfill, which can post it AFTER this reply. Since
        # Omnigent assigns each mirrored item a position by POST arrival order
        # and the web UI renders strictly by position, that inverts the
        # bubbles. Recover and post the turn's user message first so it
        # always takes the earlier position.
        await _ensure_user_message_posted(client, session_id, params, forwarder_state)
        await _post_agent_message(client, session_id, params, item)
        return
    if item_type == "plan":
        await _post_plan_item(client, session_id, params, item)
        return
    if item_type in _TOOL_ITEM_TYPES:
        await _post_tool_item(client, session_id, params, item)


async def _maybe_persist_interrupted_partial_text(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    method: str,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Mirror an interrupted Codex turn and persist any buffered visible text.

    Normal Codex turns emit durable ``item/completed`` records, so their
    streamed deltas remain transient. Interrupted turns can end with only
    streamed deltas and a terminal ``turn/completed`` status of
    ``interrupted``. In that case, publish ``session.interrupted`` and
    persist the visible partial answer as a real assistant message before
    the session goes idle.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param method: Codex terminal method, e.g. ``"turn/completed"``.
    :param params: Codex terminal notification params.
    :param forwarder_state: Mutable forwarder state carrying partial text.
    :returns: None.
    """
    if method != "turn/completed":
        return
    if not _turn_status_is_interrupted(_turn_status_from_params(params)):
        return
    turn_id = _terminal_turn_id_from_params(params)
    response_id = _response_id(_params_with_turn_id(params, turn_id)) if turn_id else None
    await _post_session_interrupted(client, session_id, response_id=response_id)
    if forwarder_state is None:
        return
    if turn_id is None:
        return
    buffers = forwarder_state.consume_partial_text_for_turn(turn_id)
    buffers_to_persist = [
        buffer
        for buffer in buffers
        if _claim_partial_text_buffer(params, turn_id, buffer, forwarder_state)
    ]
    text = "".join(buffer.text() for buffer in buffers_to_persist)
    if not text:
        return
    scoped_params = _params_with_turn_id(params, turn_id)
    await _ensure_user_message_posted(client, session_id, scoped_params, forwarder_state)
    await _post_interrupted_partial_agent_message(client, session_id, scoped_params, text)


def _claim_partial_text_buffer(
    params: dict[str, Any],
    turn_id: str,
    buffer: _PartialTextBuffer,
    forwarder_state: _CodexForwarderState,
) -> bool:
    """
    Claim the completed-item dedup key for a persisted partial text buffer.

    :param params: Codex terminal notification params.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param buffer: Partial text buffer being persisted.
    :param forwarder_state: Mutable forwarder state with item dedup keys.
    :returns: ``True`` when the partial buffer should be persisted.
    """
    if buffer.item_id is None:
        return True
    thread_id = _thread_id_from_params(params) or "thread"
    return forwarder_state.claim_item_key(f"{thread_id}:{turn_id}:{buffer.item_id}")


async def _post_interrupted_partial_agent_message(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    text: str,
) -> None:
    """
    Persist an interrupted Codex turn's visible partial assistant text.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex turn params including ``turnId``.
    :param text: Partial assistant text, e.g. ``"The answer is"``.
    :returns: None.
    """
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": _AGENT_NAME,
            "interrupted": True,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=_response_id(params),
    )


async def _handle_collab_item(
    client: httpx.AsyncClient,
    params: dict[str, Any],
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Handle a Codex ``collabAgentToolCall`` completed item.

    Registers newly discovered child threads and posts Omnigent status updates
    from the collab-agent state snapshot in the item. Does not write
    durable transcript records — the transcript for each child arrives
    via that child's own ``item/completed`` stream.

    :param client: HTTP client for Omnigent event posts.
    :param params: Codex ``item/completed`` params.
    :param item: Codex ``collabAgentToolCall`` item.
    :param forwarder_state: Mutable state for child-thread mappings.
    :returns: None.
    """
    if item.get("tool") != _CODEX_COLLAB_SPAWN_TOOL:
        return
    parent_session_id = _parent_session_id_from_forwarder_state(forwarder_state)
    if parent_session_id is None:
        return
    parent_thread_id = _collab_parent_thread_id(params, item)
    for child_thread_id in _collab_receiver_thread_ids(item):
        await _ensure_child_session(
            client,
            parent_session_id=parent_session_id,
            parent_thread_id=parent_thread_id,
            child_thread_id=child_thread_id,
            item=item,
            forwarder_state=forwarder_state,
        )
    await _post_collab_agent_statuses(client, item=item, forwarder_state=forwarder_state)


def _parent_session_id_from_forwarder_state(
    forwarder_state: _CodexForwarderState,
) -> str | None:
    """
    Return the parent Omnigent session id stored on the forwarder state.

    Set by ``supervise_forwarder`` when the loop starts. Returns ``None``
    when called from a context that did not set a parent session (e.g.
    direct handler tests that bypass ``supervise_forwarder``).

    :param forwarder_state: Mutable forwarder state.
    :returns: Parent session id, e.g. ``"conv_parent"``, or ``None``.
    """
    return forwarder_state.parent_session_id


async def _ensure_child_session(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    parent_thread_id: str | None,
    child_thread_id: str,
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Ensure a Codex child thread has an Omnigent child session row.

    Registers the child via ``_register_child_session`` when unknown,
    then backfills its history at most once per connection.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param parent_thread_id: Parent Codex thread id, or ``None``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param item: Codex ``collabAgentToolCall`` item with spawn metadata.
    :param forwarder_state: Mutable state for child-thread mappings.
    :returns: None.
    """
    child_session_id = forwarder_state.session_for_child_thread(child_thread_id)
    if child_session_id is None:
        child_session_id = await _register_child_session(
            client,
            parent_session_id=parent_session_id,
            parent_thread_id=parent_thread_id,
            child_thread_id=child_thread_id,
            item=item,
        )
        if child_session_id is None:
            return
        forwarder_state.note_child_thread(child_thread_id, child_session_id)
    # Backfill is done via the codex_client stored on the state.
    codex_client = forwarder_state.codex_client
    if codex_client is not None and forwarder_state.needs_child_thread_backfill(child_thread_id):
        await _backfill_child_thread(
            client,
            codex_client,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            child_thread_id=child_thread_id,
            forwarder_state=forwarder_state,
        )


async def _register_child_session(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    parent_thread_id: str | None,
    child_thread_id: str,
    item: dict[str, Any],
) -> str | None:
    """
    POST ``external_codex_subagent_start`` and return the child session id.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param parent_thread_id: Parent Codex thread id, or ``None``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param item: Codex ``collabAgentToolCall`` item.
    :returns: Omnigent child session id, or ``None`` on failure.
    """
    data: dict[str, Any] = {"thread_id": child_thread_id}
    if parent_thread_id is not None:
        data["parent_thread_id"] = parent_thread_id
    tool_call_id = item.get("id")
    if isinstance(tool_call_id, str) and tool_call_id:
        data["tool_call_id"] = tool_call_id
    response = await _post_session_event(
        client,
        parent_session_id,
        event_type=_EXTERNAL_CODEX_SUBAGENT_START_TYPE,
        data=data,
    )
    if response is None or response.status_code >= 400:
        _log_failed_session_event_post(_EXTERNAL_CODEX_SUBAGENT_START_TYPE, response)
        return None
    return _extract_child_session_id(response, child_thread_id)


def _extract_child_session_id(
    response: httpx.Response,
    child_thread_id: str,
) -> str | None:
    """
    Extract the child session id from an ``external_codex_subagent_start`` response.

    :param response: Omnigent HTTP response.
    :param child_thread_id: Codex child thread id for error logging.
    :returns: Omnigent child session id, or ``None`` when absent or malformed.
    """
    child_session_id = response.json().get("child_session_id")
    if not isinstance(child_session_id, str) or not child_session_id:
        _logger.warning(
            "Codex sub-agent registration missing child_session_id: thread_id=%s",
            child_thread_id,
        )
        return None
    return child_session_id


async def _backfill_child_thread(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    parent_session_id: str,
    child_session_id: str,
    child_thread_id: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Replay a child thread's backlog and upsert its name metadata.

    Called at most once per connection per child (guarded by
    ``subscribed_child_threads``). Fetches the child's rollout via
    ``thread/resume``, upserts the nickname/role labels, and replays
    any already-completed items. Live items arriving after discovery
    flow through the normal routing path; the dedup key prevents
    overlap.

    :param client: HTTP client for Omnigent event posts.
    :param codex_client: Connected Codex app-server client.
    :param parent_session_id: Parent Omnigent session id, e.g.
        ``"conv_parent"``.
    :param child_session_id: Omnigent child session id, e.g.
        ``"conv_child"``.
    :param child_thread_id: Codex child thread id, e.g.
        ``"thread_child"``.
    :param forwarder_state: Mutable state for sub-agent mappings.
    :returns: None.
    """
    response = await _resume_child_thread_or_log(
        client, codex_client, child_session_id=child_session_id, child_thread_id=child_thread_id
    )
    if response is None:
        return
    await _apply_child_resume(
        client,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        child_thread_id=child_thread_id,
        response=response,
        forwarder_state=forwarder_state,
    )


async def _resume_child_thread_or_log(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    child_session_id: str,
    child_thread_id: str,
) -> CodexMessage | None:
    """
    Request ``thread/resume`` for a child thread, logging errors.

    :param client: HTTP client for Omnigent status posts on failure.
    :param codex_client: Connected Codex app-server client.
    :param child_session_id: Omnigent child session id, e.g. ``"conv_child"``.
    :param child_thread_id: Codex child thread id, e.g.
        ``"thread_child"``.
    :returns: JSON-RPC response on success, or ``None`` on error.
    """
    try:
        return await codex_client.request("thread/resume", {"threadId": child_thread_id})
    except RuntimeError as exc:
        if _is_thread_not_ready_error(exc):
            _logger.info("Codex child thread %s not ready yet; skipping backfill", child_thread_id)
        else:
            _logger.warning(
                "Codex forwarder failed to backfill child thread %s",
                child_thread_id,
                exc_info=True,
            )
            await _post_status(client, child_session_id, "failed")
        return None


async def _apply_child_resume(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    child_session_id: str,
    child_thread_id: str,
    response: CodexMessage,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Upsert child name labels and replay its backlogged transcript.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param child_session_id: Omnigent child session id, e.g. ``"conv_child"``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param response: Validated ``thread/resume`` response envelope.
    :param forwarder_state: Mutable state for sub-agent mappings.
    :returns: None.
    """
    await _upsert_child_name_from_resume(
        client,
        parent_session_id=parent_session_id,
        child_thread_id=child_thread_id,
        response=response,
    )
    # Seed the session model (sub-agents inherit it) so replayed child token
    # usage is priced into the child's total_cost_usd — see _SessionUsageCoalescer.
    usage_coalescer = _SessionUsageCoalescer(client, child_session_id, model=forwarder_state.model)
    # A fresh tracker is used for child replay rather than the parent's,
    # because child items do not trigger elicitation requests on the parent.
    child_elicitation_tracker = _CodexElicitationTaskTracker()
    try:
        await _replay_resume_response(
            client,
            session_id=child_session_id,
            bridge_dir=Path(),
            response=response,
            usage_coalescer=usage_coalescer,
            elicitation_tracker=child_elicitation_tracker,
            forwarder_state=forwarder_state,
        )
    finally:
        await child_elicitation_tracker.close()
    forwarder_state.note_child_thread_subscribed(child_thread_id)


def _codex_child_name_data(
    child_thread_id: str,
    thread: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the name-metadata payload for a Codex child upsert.

    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param thread: Codex thread object from a ``thread/resume`` response.
    :returns: Data dict with at least ``thread_id``; name fields added when
        present on the thread object.
    """
    data: dict[str, Any] = {"thread_id": child_thread_id}
    agent_nickname = thread.get("agentNickname")
    if isinstance(agent_nickname, str) and agent_nickname:
        data["agent_nickname"] = agent_nickname
    agent_role = thread.get("agentRole")
    if isinstance(agent_role, str) and agent_role:
        data["agent_role"] = agent_role
    source = _thread_spawn_source(thread)
    if source is not None:
        parent_thread_id = source.get("parent_thread_id")
        if isinstance(parent_thread_id, str) and parent_thread_id:
            data["parent_thread_id"] = parent_thread_id
        prompt = thread.get("preview") or source.get("prompt")
        if isinstance(prompt, str) and prompt:
            data["prompt"] = prompt
    return data


async def _upsert_child_name_from_resume(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    child_thread_id: str,
    response: CodexMessage,
) -> None:
    """
    Upsert ``agent_nickname`` / ``agent_role`` from a child resume response.

    Idempotent — the server merges labels. No-ops when the resume carries
    no name fields beyond the thread id.

    :param client: HTTP client for Omnigent event posts.
    :param parent_session_id: Parent Omnigent session id, e.g. ``"conv_parent"``.
    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param response: Codex ``thread/resume`` response envelope.
    :returns: None.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return
    data = _codex_child_name_data(child_thread_id, thread)
    if len(data) <= 1:
        return
    response_obj = await _post_session_event(
        client, parent_session_id, event_type=_EXTERNAL_CODEX_SUBAGENT_START_TYPE, data=data
    )
    _log_failed_session_event_post(_EXTERNAL_CODEX_SUBAGENT_START_TYPE, response_obj)


def _thread_spawn_source(thread: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return the ``thread_spawn`` source metadata from a Codex thread object.

    :param thread: Codex thread object from a ``thread/started`` or
        ``thread/resume`` payload.
    :returns: The ``thread_spawn`` dict when present, otherwise ``None``.
    """
    source = thread.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subAgent")
    if not isinstance(subagent, dict):
        return None
    thread_spawn = subagent.get("thread_spawn")
    return thread_spawn if isinstance(thread_spawn, dict) else None


async def _post_collab_agent_statuses(
    client: httpx.AsyncClient,
    *,
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Publish Omnigent status updates from a Codex collab-agent state snapshot.

    :param client: HTTP client for Omnigent event posts.
    :param item: Codex ``collabAgentToolCall`` item carrying
        ``agentsStates``.
    :param forwarder_state: Mutable state for child-thread mappings.
    :returns: None.
    """
    states = item.get("agentsStates")
    if not isinstance(states, dict):
        return
    for thread_id, state in states.items():
        if not isinstance(thread_id, str) or not isinstance(state, dict):
            continue
        child_session_id = forwarder_state.session_for_child_thread(thread_id)
        if child_session_id is None:
            continue
        ap_status = _omnigent_status_from_collab_state(state)
        if ap_status is not None:
            await _post_status(client, child_session_id, ap_status)


def _omnigent_status_from_collab_state(state: dict[str, Any]) -> str | None:
    """
    Convert a Codex collab-agent state dict to an Omnigent session status.

    :param state: Codex ``CollabAgentState`` dict, e.g.
        ``{"status": "running"}``.
    :returns: Omnigent status literal, e.g. ``"running"``, or ``None`` when
        the Codex status is unrecognized.
    """
    status = state.get("status")
    if status in _CODEX_COLLAB_RUNNING_STATUSES:
        return "running"
    if status in _CODEX_COLLAB_FAILED_STATUSES:
        return "failed"
    if status in {"completed", "interrupted", "shutdown"}:
        return "idle"
    return None


def _collab_receiver_thread_ids(item: dict[str, Any]) -> list[str]:
    """
    Extract receiver thread ids from a Codex collab-agent item.

    :param item: Codex ``collabAgentToolCall`` item.
    :returns: Deduplicated receiver thread ids in original order.
    """
    raw = item.get("receiverThreadIds")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in raw:
        if isinstance(value, str) and value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _collab_parent_thread_id(
    params: dict[str, Any],
    item: dict[str, Any],
) -> str | None:
    """
    Return the Codex parent thread id for a collab-agent spawn.

    :param params: Codex notification params.
    :param item: Codex ``collabAgentToolCall`` item.
    :returns: Parent thread id, or ``None`` when not determinable.
    """
    sender = item.get("senderThreadId")
    if isinstance(sender, str) and sender:
        return sender
    return _thread_id_from_params(params)


async def _handle_agent_message_delta(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Forward one live Codex assistant text delta to AP.

    Codex app-server emits ``item/agentMessage/delta`` while a turn is
    running. Omnigent normally persists only the completed ``agentMessage`` item,
    so this path publishes a transient text-delta SSE event and relies on
    the later ``item/completed`` notification for durable completed-turn
    history. The same text is also buffered in memory so an interrupted turn
    that never emits a completed item can still persist the visible partial
    answer.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``item/agentMessage/delta`` params, e.g.
        ``{"turnId": "turn_123", "itemId": "item_123",
        "delta": "hi"}``.
    :param delta_coalescer: Coalescer for high-frequency assistant text
        deltas.
    :param forwarder_state: Optional forwarder state used to recover the
        turn's user message before streaming a recovered assistant delta.
    :returns: None.
    """
    turn_id = _turn_id_from_payload(params)
    delta = params.get("delta")
    if not isinstance(delta, str):
        _logger.warning("Codex agentMessage delta missing string delta: turn_id=%s", turn_id)
        return
    if not _is_active_turn_delta(bridge_dir, turn_id):
        edge = _delta_recovery_status_edge(bridge_dir, params, turn_id)
        if edge is not None:
            await _ensure_user_message_posted(client, session_id, params, forwarder_state)
            await _post_turn_status_edge(client, session_id, edge)
            _record_partial_text_delta(
                forwarder_state,
                turn_id=turn_id,
                item_type="agentMessage",
                item_id=_item_id_from_delta_params(params),
                delta=delta,
            )
            await delta_coalescer.append(
                delta,
                message_id=_streaming_message_id(params, "agentMessage"),
            )
            return
        _logger.info("Codex forwarder ignored stale assistant delta: turn_id=%s", turn_id)
        return
    _record_partial_text_delta(
        forwarder_state,
        turn_id=turn_id,
        item_type="agentMessage",
        item_id=_item_id_from_delta_params(params),
        delta=delta,
    )
    await delta_coalescer.append(
        delta,
        message_id=_streaming_message_id(params, "agentMessage"),
    )


async def _handle_plan_delta(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
    delta_coalescer: _OutputTextDeltaCoalescer,
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Forward one live Codex plan text delta to AP.

    Plan mode streams visible plan prose through
    ``item/plan/delta`` rather than ``item/agentMessage/delta``.
    Omnigent uses the same transient output-text delta channel for both,
    and the later completed ``plan`` item or structured plan update
    provides the durable completed-turn transcript state. Interrupted turns
    consume the buffered deltas so the visible partial plan is still durable.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``item/plan/delta`` params, e.g.
        ``{"turnId": "turn_123", "itemId": "item_plan",
        "delta": "1. Inspect"}``.
    :param delta_coalescer: Coalescer for high-frequency assistant text
        deltas.
    :param forwarder_state: Optional forwarder state used to recover the
        turn's user message before streaming a recovered plan delta.
    :returns: None.
    """
    turn_id = _turn_id_from_payload(params)
    delta = params.get("delta")
    if not isinstance(delta, str):
        _logger.warning("Codex plan delta missing string delta: turn_id=%s", turn_id)
        return
    if not _is_active_turn_delta(bridge_dir, turn_id):
        edge = _delta_recovery_status_edge(bridge_dir, params, turn_id)
        if edge is not None:
            await _ensure_user_message_posted(client, session_id, params, forwarder_state)
            await _post_turn_status_edge(client, session_id, edge)
            _record_partial_text_delta(
                forwarder_state,
                turn_id=turn_id,
                item_type="plan",
                item_id=_item_id_from_delta_params(params),
                delta=delta,
            )
            await delta_coalescer.append(
                delta,
                message_id=_streaming_message_id(params, "plan"),
            )
            return
        _logger.info("Codex forwarder ignored stale plan delta: turn_id=%s", turn_id)
        return
    _record_partial_text_delta(
        forwarder_state,
        turn_id=turn_id,
        item_type="plan",
        item_id=_item_id_from_delta_params(params),
        delta=delta,
    )
    await delta_coalescer.append(
        delta,
        message_id=_streaming_message_id(params, "plan"),
    )


async def _ensure_user_message_posted(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState | None,
) -> None:
    """
    Guarantee a turn's user message is posted before its assistant reply.

    The forwarder's live stream normally delivers ``userMessage`` before
    ``agentMessage`` for a turn, so this is a no-op. But on a fresh thread
    the subscription can miss the early ``userMessage`` event; this
    recovers it via a targeted ``thread/resume`` and posts it through the
    normal claim/post path so it takes an earlier Omnigent position than the
    reply. The recovered item carries Codex's resume id (e.g. ``item-1``),
    matching the id the resume backfill would later use — so the dedup
    gate drops the backfill's duplicate.

    No-op when ``forwarder_state`` is absent (tests bypassing
    ``supervise_forwarder``), when no Codex client is wired, or when the
    turn's user message was already posted this connection.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params for the assistant
        message whose turn's user message must already be posted.
    :param forwarder_state: Mutable forwarder state tracking posted user
        turns and holding the Codex app-server client.
    :returns: None.
    """
    if forwarder_state is None:
        return
    turn_id = _turn_id_from_payload(params)
    if not turn_id or forwarder_state.has_posted_user_message(turn_id):
        return
    codex_client = forwarder_state.codex_client
    thread_id = _thread_id_from_params(params)
    if codex_client is None or thread_id is None:
        return
    try:
        response = await codex_client.request("thread/resume", {"threadId": thread_id})
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - degrade to current behavior on resume failure.
        _logger.warning(
            "Codex forwarder could not resume to recover user message: thread=%s turn=%s",
            thread_id,
            turn_id,
            exc_info=True,
        )
        return
    user_item = _find_turn_user_message(response, turn_id)
    if user_item is None:
        return
    recovered_params: dict[str, Any] = {
        "threadId": thread_id,
        "turnId": turn_id,
        "item": user_item,
    }
    if not _claim_completed_item(recovered_params, user_item, forwarder_state):
        return
    await _post_user_message(client, session_id, recovered_params, user_item)
    forwarder_state.note_user_message_posted(turn_id)


def _find_turn_user_message(response: CodexMessage, turn_id: str) -> dict[str, Any] | None:
    """
    Locate a turn's ``userMessage`` item in a ``thread/resume`` response.

    :param response: Codex ``thread/resume`` response envelope.
    :param turn_id: Codex turn id whose user message to find, e.g.
        ``"turn_123"``.
    :returns: The ``userMessage`` item dict, or ``None`` when the turn or
        its user message is absent.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if not isinstance(turn, dict) or _turn_id_from_payload(turn) != turn_id:
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("type") == "userMessage":
                return item
    return None


async def _post_user_message(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Persist a Codex user message observed from the TUI.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex notification params.
    :param item: Codex ``userMessage`` item.
    :returns: None.
    """
    text = _user_message_text(item)
    # An image/file-only message has no text but must still be posted: the
    # server drains its optimistic pending-input entry (FIFO) and folds the
    # image in by file_id (``_merge_pending_file_blocks``). Bailing here would
    # leak the pending entry — the user bubble would never persist (rendering
    # the reply above the dangling image) and the NEXT message would drain
    # this stale entry, folding the prior image into it. Only a truly empty
    # message (no text, no file block) is skipped.
    has_file_block = _user_message_has_file_content(item)
    if not text and not has_file_block:
        return
    # Text-only / text+image post the text; image-only posts empty content and
    # relies on the server-side pending fold to supply the image block.
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}] if text else []
    item_data: dict[str, Any] = {
        "role": "user",
        "content": content,
    }
    if _is_codex_skill_wrapper(text):
        item_data["is_meta"] = True
        _logger.debug(
            "Marked Codex skill wrapper as meta for session=%s source_id=%s",
            session_id,
            _source_id(params, item),
        )
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data=item_data,
        response_id=_response_id(params),
    )


async def _post_agent_message(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Persist a Codex assistant message observed from the TUI/app-server.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex notification params.
    :param item: Codex ``agentMessage`` item.
    :returns: None.
    """
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=_response_id(params),
    )


async def _post_tool_item(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Mirror one completed Codex built-in tool call into Omnigent history.

    A native Codex session runs Codex's own tools (shell commands, file
    edits, web search) rather than client-tunneled dynamic tools, so a
    single ``item/completed`` notification carries both the invocation
    and its result. This translates that one item into the AP
    ``function_call`` / ``function_call_output`` pair the web UI renders.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param item: Codex tool item, e.g.
        ``{"type": "commandExecution", "id": "call_abc",
        "command": "/bin/zsh -lc 'pwd'", "aggregatedOutput": "/repo\n",
        "exitCode": 0}``.
    :returns: None.
    """
    tool_call = _codex_tool_call_from_item(item)
    if tool_call is None:
        return
    arguments_text = _json_string(tool_call.arguments)
    if arguments_text is None:
        _logger.warning(
            "Codex tool call arguments are not JSON serializable: call_id=%s tool=%s",
            tool_call.call_id,
            tool_call.name,
        )
        return
    await _post_external_item(
        client,
        session_id,
        item_type="function_call",
        item_data={
            "agent": _AGENT_NAME,
            "name": tool_call.name,
            "arguments": arguments_text,
            "call_id": tool_call.call_id,
        },
        response_id=_response_id(params),
    )
    await _post_external_item(
        client,
        session_id,
        item_type="function_call_output",
        item_data={"call_id": tool_call.call_id, "output": tool_call.output},
        response_id=_response_id(params),
    )


async def _post_plan_item(
    client: httpx.AsyncClient,
    session_id: str,
    params: dict[str, Any],
    item: dict[str, Any],
) -> None:
    """
    Persist one completed Codex plan item as assistant text.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param params: Codex ``item/completed`` params.
    :param item: Codex ``plan`` thread item.
    :returns: None.
    """
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return
    await _post_external_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=_response_id(params),
    )


def _codex_tool_call_from_item(item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Translate a completed Codex tool item into a normalized tool call.

    :param item: Codex tool item from an ``item/completed`` notification,
        e.g. ``{"type": "commandExecution", "id": "call_abc", ...}``.
    :returns: Normalized tool call, or ``None`` for a malformed item that
        should be dropped rather than mirrored with invented fields.
    """
    call_id = item.get("id")
    item_type = item.get("type")
    if not isinstance(call_id, str) or not call_id:
        _logger.warning("Codex tool item missing string id: type=%s", item_type)
        return None
    builder = _TOOL_ITEM_BUILDERS.get(item_type) if isinstance(item_type, str) else None
    if builder is None:
        return None
    return builder(call_id, item)


def _command_execution_tool_call(call_id: str, item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Build a tool call from a Codex ``commandExecution`` item.

    :param call_id: Codex item id, e.g. ``"call_abc"``.
    :param item: Codex ``commandExecution`` item, e.g.
        ``{"command": "/bin/zsh -lc 'pwd'", "cwd": "/repo",
        "aggregatedOutput": "/repo\n", "exitCode": 0}``.
    :returns: Normalized tool call, or ``None`` when the command is
        missing.
    """
    command = item.get("command")
    if not isinstance(command, str) or not command:
        _logger.warning("Codex commandExecution missing command: call_id=%s", call_id)
        return None
    arguments: dict[str, Any] = {"command": command}
    cwd = item.get("cwd")
    if isinstance(cwd, str) and cwd:
        arguments["cwd"] = cwd
    output = item.get("aggregatedOutput")
    # A command that prints nothing (e.g. ``touch x``) legitimately has no
    # aggregated output; Codex reports that as "" or null. AP's
    # function_call_output requires a string, so "" is the faithful
    # representation of "no output captured" here — not an invented default.
    output_text = output if isinstance(output, str) else ""
    exit_code = item.get("exitCode")
    # Codex reports a non-zero exit separately from stdout/stderr; surface
    # it inline so a failed command does not look successful in the UI.
    if isinstance(exit_code, int) and exit_code != 0:
        suffix = f"[exit code: {exit_code}]"
        output_text = f"{output_text}\n{suffix}" if output_text else suffix
    return _CodexToolCall(call_id=call_id, name="shell", arguments=arguments, output=output_text)


def _file_change_tool_call(call_id: str, item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Build a tool call from a Codex ``fileChange`` item.

    :param call_id: Codex item id, e.g. ``"call_abc"``.
    :param item: Codex ``fileChange`` item, e.g.
        ``{"changes": [{"path": "/repo/x.py", "kind": {"type": "add"},
        "diff": "print('hi')\n"}], "status": "completed"}``.
    :returns: Normalized tool call, or ``None`` when no changes are
        present.
    """
    changes = item.get("changes")
    if not isinstance(changes, list) or not changes:
        _logger.warning("Codex fileChange missing changes: call_id=%s", call_id)
        return None
    summary_lines: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        kind = change.get("kind")
        kind_type = kind.get("type") if isinstance(kind, dict) else None
        label = kind_type if isinstance(kind_type, str) and kind_type else "change"
        summary_lines.append(f"{label} {path}")
    output_text = "\n".join(summary_lines)
    return _CodexToolCall(
        call_id=call_id,
        name="apply_patch",
        arguments={"changes": changes},
        output=output_text,
    )


def _web_search_tool_call(call_id: str, item: dict[str, Any]) -> _CodexToolCall | None:
    """
    Build a tool call from a Codex ``webSearch`` item.

    Codex does not surface the search results, so the queries it ran are
    the only result data available and are used as the output text.

    :param call_id: Codex item id, e.g. ``"ws_abc"``.
    :param item: Codex ``webSearch`` item, e.g.
        ``{"query": "python latest version",
        "action": {"type": "search", "queries": ["python latest"]}}``.
    :returns: Normalized tool call, or ``None`` when no query is present.
    """
    query = item.get("query")
    action = item.get("action")
    queries = action.get("queries") if isinstance(action, dict) else None
    query_list = [q for q in queries if isinstance(q, str)] if isinstance(queries, list) else []
    if not query_list and isinstance(query, str) and query:
        query_list = [query]
    if not query_list:
        _logger.warning("Codex webSearch missing query: call_id=%s", call_id)
        return None
    return _CodexToolCall(
        call_id=call_id,
        name="web_search",
        arguments={"query": query_list[0]},
        output="\n".join(query_list),
    )


# Codex built-in tool item types this forwarder mirrors into Omnigent history.
# ``mcpToolCall`` is intentionally absent: its event shape has not been
# verified, so it is logged-but-skipped rather than mirrored with guessed
# fields. Add it here once its real shape is captured.
_TOOL_ITEM_BUILDERS: dict[str, _ToolItemBuilder] = {
    "commandExecution": _command_execution_tool_call,
    "fileChange": _file_change_tool_call,
    "webSearch": _web_search_tool_call,
}
_TOOL_ITEM_TYPES = frozenset(_TOOL_ITEM_BUILDERS)


async def _post_external_item(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    item_type: str,
    item_data: dict[str, Any],
    response_id: str,
) -> None:
    """
    Post one external conversation item to AP.

    The forwarder does not send a dedup key to the server — items are
    persisted with a random primary key. Avoiding re-posts on resume is
    the producer's own responsibility.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param item_type: Conversation item type, e.g. ``"message"``.
    :param item_data: Conversation item payload.
    :param response_id: Response id for the mirrored Codex turn.
    :returns: None.
    """
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_conversation_item",
        data={
            "item_type": item_type,
            "item_data": item_data,
            "response_id": response_id,
        },
    )
    if response is None:
        _logger.warning("failed to post Codex conversation item")
        return
    if response.status_code >= 400:
        _logger.warning(
            "failed to post Codex conversation item: status=%s body=%s",
            response.status_code,
            response.text[:1000],
        )


async def _post_status(
    client: httpx.AsyncClient,
    session_id: str,
    status: str,
    *,
    response_id: str | None = None,
) -> None:
    """
    Publish a native Codex status edge.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param status: Session status, e.g. ``"running"``.
    :param response_id: Optional response id for this status edge,
        e.g. ``"codex_turn_abc123"``.
    :returns: None.
    """
    data = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_session_status",
        data=data,
    )
    _log_failed_session_event_post("external_session_status", response)


async def _post_turn_status_edge(
    client: httpx.AsyncClient,
    session_id: str,
    edge: _CodexTurnStatusEdge | None,
) -> None:
    """
    Publish one Codex turn lifecycle edge if a valid edge was derived.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param edge: Derived lifecycle edge, or ``None`` when no status should
        be published.
    :returns: None.
    """
    if edge is None:
        return
    _logger.info(
        "Codex forwarder publishing turn status: source=%s turn_id=%s status=%s",
        edge.source,
        edge.turn_id,
        edge.status,
    )
    response_id = _response_id(_params_with_turn_id({}, edge.turn_id)) if edge.turn_id else None
    await _post_status(client, session_id, edge.status, response_id=response_id)


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    elicitation_id: str,
) -> bool:
    """
    Post a native-side elicitation resolution signal to AP.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Omnigent elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: ``True`` when Omnigent accepted the event.
    """
    response = await _post_session_event(
        client,
        session_id,
        event_type=_EXTERNAL_ELICITATION_RESOLVED_TYPE,
        data={"elicitation_id": elicitation_id},
    )
    _log_failed_session_event_post(_EXTERNAL_ELICITATION_RESOLVED_TYPE, response)
    return response is not None and response.status_code < 400


async def _post_output_text_delta(
    client: httpx.AsyncClient,
    session_id: str,
    delta: str,
    *,
    message_id: str | None = None,
    index: int | None = None,
    final: bool | None = None,
) -> None:
    """
    Publish a transient Codex assistant text delta.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param delta: Assistant text fragment, e.g. ``"hello"``.
    :param message_id: Optional stable native message stream id,
        e.g. ``"codex:thread_123:turn_123:agentMessage:item_agent"``.
    :param index: Optional zero-based chunk index for ``message_id``,
        e.g. ``0``.
    :param final: Optional final-chunk marker for ``message_id``,
        e.g. ``False``.
    :returns: None.
    """
    data: dict[str, Any] = {"delta": delta}
    if message_id is not None:
        data["message_id"] = message_id
    if index is not None:
        data["index"] = index
    if final is not None:
        data["final"] = final
    response = await _post_session_event(
        client,
        session_id,
        event_type="external_output_text_delta",
        data=data,
    )
    _log_failed_session_event_post("external_output_text_delta", response)


async def _post_session_interrupted(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    response_id: str | None = None,
) -> None:
    """
    Publish a Codex-observed interrupted-turn signal into AP.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param response_id: Optional interrupted response id, e.g.
        ``"codex_turn_abc123"``.
    :returns: None.
    """
    data: dict[str, Any] = {}
    if response_id is not None:
        data["response_id"] = response_id
    response = await _post_session_event(
        client,
        session_id,
        event_type=_EXTERNAL_SESSION_INTERRUPTED_TYPE,
        data=data,
    )
    _log_failed_session_event_post(_EXTERNAL_SESSION_INTERRUPTED_TYPE, response)


def _session_usage_data_from_params(params: dict[str, Any]) -> dict[str, int] | None:
    """
    Extract Omnigent session-usage fields from a Codex usage notification.

    :param params: Codex ``thread/tokenUsage/updated`` params.
    :returns: A dict with any of ``context_tokens`` / ``context_window``
        (context ring), ``cumulative_input_tokens`` /
        ``cumulative_output_tokens`` /
        ``cumulative_cache_read_input_tokens`` (priced into session cost by
        the server), or ``None`` when the notification has no usable usage
        values.
    """
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return None
    total = token_usage.get("total")
    if not isinstance(total, dict):
        return None
    cumulative_input_tokens = total.get("inputTokens")
    context_window = total.get("contextWindow")
    output_tokens = total.get("outputTokens")
    cached_input_tokens = total.get("cachedInputTokens")
    data: dict[str, int] = {}
    if isinstance(cumulative_input_tokens, int) and cumulative_input_tokens >= 0:
        # Codex's ``tokenUsage.total`` is CUMULATIVE across the whole thread
        # (the CLI subtracts prior totals to recover per-turn deltas), so
        # ``total.inputTokens`` / ``outputTokens`` are the session's cumulative
        # token counts. Forward them as the cumulative fields the server prices
        # into ``total_cost_usd`` (SET semantics) — codex-native produces no
        # ``response.completed``, so the Omnigent relay never accounts its cost.
        data["cumulative_input_tokens"] = cumulative_input_tokens
        # Codex's ``inputTokens`` is INCLUSIVE of cached tokens
        # (``non_cached_input = input_tokens - cached_input_tokens`` in
        # codex-rs ``protocol.rs``). Forward the cumulative cached count so the
        # server can price the cached portion at the (cheaper) cache-read rate
        # instead of billing the whole input at the full input rate. Same
        # cumulative (SET) semantics as ``cumulative_input_tokens``.
        if isinstance(cached_input_tokens, int) and cached_input_tokens >= 0:
            data["cumulative_cache_read_input_tokens"] = cached_input_tokens
    # ``context_tokens`` drives the context-window ring in the web UI. It
    # must reflect the CURRENT context occupancy (how much of the window
    # the latest turn consumed), NOT the cumulative total across all turns.
    # Codex's ``tokenUsage.last`` carries the per-turn breakdown; fall back
    # to ``total.inputTokens`` only when ``last`` is unavailable (first
    # frame before a turn completes).
    last = token_usage.get("last")
    last_input = last.get("inputTokens") if isinstance(last, dict) else None
    if isinstance(last_input, int) and last_input >= 0:
        data["context_tokens"] = last_input
    elif isinstance(cumulative_input_tokens, int) and cumulative_input_tokens >= 0:
        data["context_tokens"] = cumulative_input_tokens
    if isinstance(output_tokens, int) and output_tokens >= 0:
        data["cumulative_output_tokens"] = output_tokens
    if isinstance(context_window, int) and context_window > 0:
        data["context_window"] = context_window
    if not data:
        return None
    return data


async def _post_session_event(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event_type: str,
    data: dict[str, Any],
) -> httpx.Response | None:
    """
    Post one Omnigent session event with bounded transient retries.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event_type: Session event type, e.g.
        ``"external_conversation_item"``.
    :param data: Event data payload, e.g.
        ``{"status": "running"}``.
    :returns: Final HTTP response, or ``None`` when all attempts raised
        transport errors — or, for ``external_conversation_item``, after
        a single ambiguous transport failure (the item may already be
        committed server-side, so retrying risks a duplicate).
    """
    url = f"/v1/sessions/{url_component(session_id)}/events"
    payload = {"type": event_type, "data": data}
    for attempt in range(1, _POST_MAX_ATTEMPTS + 1):
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Conversation items persist with a random primary key and no
            # server-side dedup, so an ambiguous failure (request sent,
            # response lost — the server may have committed it) must not
            # be retried: a re-post would duplicate the item.
            # Other event types are idempotent / transient, so retrying
            # them on the same errors is safe and preserves delivery.
            if event_type == "external_conversation_item" and post_may_have_been_delivered(exc):
                _logger.warning(
                    "skipping Codex session event after an ambiguous transport "
                    "failure (may already be committed); not retrying to avoid "
                    "a duplicate: type=%s error=%r",
                    event_type,
                    exc,
                )
                return None
            if _is_final_post_attempt(attempt):
                _log_post_transport_failure(event_type, exc)
                return None
            await _sleep(_post_retry_delay(attempt))
            continue
        if _post_response_is_final(response, attempt):
            return response
        await _sleep(_post_retry_delay(attempt))
    return None


def _post_response_is_final(response: httpx.Response, attempt: int) -> bool:
    """
    Return whether a session-event POST response should stop retries.

    :param response: HTTP response from AP.
    :param attempt: One-based attempt number, e.g. ``1``.
    :returns: ``True`` when the caller should return ``response``.
    """
    if response.status_code < 400:
        return True
    if not _should_retry_post_status(response.status_code):
        return True
    return _is_final_post_attempt(attempt)


def _is_final_post_attempt(attempt: int) -> bool:
    """
    Return whether an Omnigent event POST attempt is the final try.

    :param attempt: One-based attempt number, e.g. ``3``.
    :returns: ``True`` when no further retry is allowed.
    """
    return attempt >= _POST_MAX_ATTEMPTS


def _log_post_transport_failure(event_type: str, exc: httpx.HTTPError) -> None:
    """
    Log an exhausted Omnigent session-event transport failure.

    :param event_type: Session event type, e.g.
        ``"external_conversation_item"``.
    :param exc: Final transport error.
    :returns: None.
    """
    _logger.warning(
        "failed to post Codex session event after retries: type=%s attempts=%s error=%r",
        event_type,
        _POST_MAX_ATTEMPTS,
        exc,
    )


def _log_failed_session_event_post(
    event_type: str,
    response: httpx.Response | None,
) -> None:
    """
    Log failed best-effort session events such as status and usage.

    :param event_type: Session event type, e.g.
        ``"external_session_status"``.
    :param response: Final Omnigent response, or ``None`` after transport
        errors exhausted all retries.
    :returns: None.
    """
    if response is None:
        _logger.warning("failed to post Codex session event: type=%s", event_type)
        return
    if response.status_code >= 400:
        _logger.warning(
            "failed to post Codex session event: type=%s status=%s body=%s",
            event_type,
            response.status_code,
            response.text[:1000],
        )


def _should_retry_post_status(status_code: int) -> bool:
    """
    Return whether an Omnigent event POST status is transient.

    :param status_code: HTTP status code, e.g. ``503``.
    :returns: ``True`` when the forwarder should retry.
    """
    return status_code in _POST_RETRY_STATUS_CODES


def _post_retry_delay(attempt: int) -> float:
    """
    Return the retry delay for a failed Omnigent event POST attempt.

    :param attempt: One-based failed attempt number, e.g. ``1``.
    :returns: Delay in seconds before the next attempt.
    """
    return _POST_RETRY_DELAY_SECONDS * attempt


def _turn_id_from_payload(payload: object) -> str | None:
    """
    Extract a turn id from a Codex payload.

    :param payload: Codex notification params or nested turn object.
    :returns: Turn id, or ``None`` when absent.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("id") or payload.get("turnId")
    return value if isinstance(value, str) and value else None


def _turn_status_from_params(params: dict[str, Any]) -> str | None:
    """
    Extract a Codex turn status from terminal notification params.

    :param params: Codex terminal params, e.g.
        ``{"turn": {"id": "turn_123", "status": "interrupted"}}``.
    :returns: Status string, e.g. ``"interrupted"``, or ``None``.
    """
    status: object = params.get("status")
    turn = params.get("turn")
    if isinstance(turn, dict):
        status = turn.get("status")
    if isinstance(status, dict):
        status = status.get("type") or status.get("status")
    return status if isinstance(status, str) and status else None


def _turn_status_is_interrupted(status: str | None) -> bool:
    """
    Return whether a Codex turn status represents user interruption.

    :param status: Codex turn status, e.g. ``"interrupted"``.
    :returns: ``True`` for interrupted/cancelled terminal statuses.
    """
    if status is None:
        return False
    normalized = status.replace("_", "").replace("-", "").lower()
    return normalized in {"interrupted", "cancelled", "canceled"}


def _params_with_turn_id(params: dict[str, Any], turn_id: str) -> dict[str, Any]:
    """
    Return params with a top-level ``turnId`` for Omnigent response ids.

    :param params: Codex notification params.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :returns: Shallow-copied params containing ``turnId``.
    """
    scoped = dict(params)
    scoped["turnId"] = turn_id
    return scoped


def _thread_id_from_started_event(event: CodexMessage) -> str | None:
    """
    Extract a thread id from a Codex ``thread/started`` event.

    :param event: Codex app-server notification envelope.
    :returns: Thread id, e.g. ``"thread_abc"``, or ``None``.
    """
    if event.get("method") != "thread/started":
        return None
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return None
    thread_id = thread.get("id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _parent_thread_id_from_started_event(event: CodexMessage) -> str | None:
    """
    Extract the spawning parent thread id from a child ``thread/started``.

    :param event: Codex app-server notification envelope.
    :returns: Parent Codex thread id from
        ``source.subAgent.thread_spawn.parent_thread_id``, e.g.
        ``"thread_parent"``, or ``None`` when absent.
    """
    if event.get("method") != "thread/started":
        return None
    params = event.get("params")
    if not isinstance(params, dict):
        return None
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return None
    source = _thread_spawn_source(thread)
    if source is None:
        return None
    parent_thread_id = source.get("parent_thread_id")
    return parent_thread_id if isinstance(parent_thread_id, str) and parent_thread_id else None


def _thread_started_is_subagent(event: CodexMessage) -> bool:
    """
    Return whether a ``thread/started`` event announces a child sub-agent.

    Codex AgentControl children emit ``thread/started`` when they begin.
    These events carry a ``source.subAgent.thread_spawn`` object that
    distinguishes them from a top-level session rotation triggered by
    the user running ``/clear``.

    :param event: Codex app-server notification envelope.
    :returns: ``True`` when the started thread declares itself a
        sub-agent via ``source.subAgent.thread_spawn``.
    """
    if event.get("method") != "thread/started":
        return False
    params = event.get("params")
    if not isinstance(params, dict):
        return False
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return False
    return _thread_spawn_source(thread) is not None


async def wait_for_thread_started(
    client: CodexAppServerClient,
    *,
    timeout: float = _THREAD_START_TIMEOUT_SECONDS,
) -> str:
    """
    Wait for a freshly launched Codex TUI to create its app-server thread.

    A cold-start Codex TUI (launched with ``--remote`` and no ``resume``)
    creates a new thread, and the app-server emits a ``thread/started``
    notification to connected listeners. *client* must already be connected
    so it observes that notification. The returned id is then used to
    subscribe the forwarder and to drive web-UI message injection, so the
    terminal and chat share one thread. The host-spawned runner auto-create
    uses this because — unlike the local CLI — it has no TTY to ``resume`` an
    existing thread into, and ``resume`` of a not-yet-persisted thread fails.

    :param client: A connected :class:`CodexAppServerClient` listening for
        app-server notifications.
    :param timeout: Seconds to wait for ``thread/started`` before failing.
    :returns: The Codex thread id, e.g.
        ``"019e8720-98d7-7b23-ac0a-bfb0eb02e0c9"``.
    :raises TimeoutError: If no ``thread/started`` arrives within *timeout*.
    :raises RuntimeError: If the event stream ends before a thread starts.
    """
    async with asyncio.timeout(timeout):
        async for event in client.iter_events():
            thread_id = _thread_id_from_started_event(event)
            if thread_id is not None:
                return thread_id
    raise RuntimeError("Codex app-server event stream ended before thread startup.")


def _thread_id_from_params(params: dict[str, Any]) -> str | None:
    """
    Extract the thread id carried by a Codex notification params object.

    :param params: Codex notification params, e.g.
        ``{"threadId": "thread_abc"}``.
    :returns: Thread id, or ``None`` when the event does not carry one.
    """
    thread_id = params.get("threadId")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        nested_thread_id = thread.get("id")
        if isinstance(nested_thread_id, str) and nested_thread_id:
            return nested_thread_id
    return None


def _is_active_turn_delta(bridge_dir: Path, turn_id: str | None) -> bool:
    """
    Return whether a Codex delta belongs to the current active turn.

    :param bridge_dir: Native Codex bridge directory.
    :param turn_id: Codex turn id from the delta notification, e.g.
        ``"turn_123"``.
    :returns: ``True`` when the bridge state identifies the same
        active turn.
    """
    if turn_id is None:
        return False
    state = read_bridge_state(bridge_dir)
    return state is not None and state.active_turn_id == turn_id


def _item_id_from_delta_params(params: dict[str, Any]) -> str | None:
    """
    Extract a Codex item id from a streaming delta notification.

    :param params: Codex delta params, e.g.
        ``{"itemId": "item_abc123"}``.
    :returns: Item id, or ``None`` when absent.
    """
    item_id = params.get("itemId")
    return item_id if isinstance(item_id, str) and item_id else None


def _streaming_message_id(params: dict[str, Any], item_type: str) -> str | None:
    """
    Build a stable Omnigent live-delta stream id for a Codex item.

    Omnigent Web uses this id to keep terminal-observed live text in a
    provisional native block, then replace that block when the durable
    completed item arrives. Returning ``None`` preserves the generic
    Responses-style text stream for malformed deltas that carry no
    usable Codex identity.

    :param params: Codex delta params, e.g.
        ``{"threadId": "thread_123", "turnId": "turn_123",
        "itemId": "item_agent"}``.
    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :returns: Stable message id, e.g.
        ``"codex:thread_123:turn_123:agentMessage:item_agent"``, or
        ``None``.
    """
    thread_id = _thread_id_from_params(params)
    turn_id = _turn_id_from_payload(params)
    item_id = _item_id_from_delta_params(params)
    if thread_id is None and turn_id is None and item_id is None:
        return None
    parts = ["codex"]
    if thread_id is not None:
        parts.append(thread_id)
    if turn_id is not None:
        parts.append(turn_id)
    parts.append(item_type)
    if item_id is not None:
        parts.append(item_id)
    return ":".join(parts)


def _record_partial_text_delta(
    forwarder_state: _CodexForwarderState | None,
    *,
    turn_id: str | None,
    item_type: str,
    item_id: str | None,
    delta: str,
) -> None:
    """
    Record a visible Codex text delta for interrupted-turn durability.

    :param forwarder_state: Mutable forwarder state, or ``None`` when direct
        tests bypass stateful supervision.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :param item_id: Codex item id, e.g. ``"item_abc123"``, or ``None``.
    :param delta: Text fragment, e.g. ``"hel"``.
    :returns: None.
    """
    if forwarder_state is None or turn_id is None:
        return
    forwarder_state.record_partial_text_delta(
        turn_id=turn_id,
        item_type=item_type,
        item_id=item_id,
        delta=delta,
    )


def _try_recover_active_turn_from_delta(
    bridge_dir: Path,
    params: dict[str, Any],
    turn_id: str | None,
) -> bool:
    """
    Adopt a Codex delta turn when subscription missed ``turn/started``.

    Fresh remote Codex sessions can begin a TUI turn while the observer
    connection is still retrying ``thread/resume``. In that race the
    first plan delta is already scoped by ``threadId``/``turnId`` but
    bridge state has no active turn yet. Treat that as the current turn
    only when the thread matches the bridge state; an already-active
    different turn remains protected from stale deltas.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex delta notification params.
    :param turn_id: Turn id extracted from *params*.
    :returns: ``True`` when the delta was adopted as the active turn.
    """
    if turn_id is None:
        return False
    state = read_bridge_state(bridge_dir)
    if state is None or state.active_turn_id is not None:
        return False
    thread_id = params.get("threadId")
    if thread_id != state.thread_id:
        return False
    update_active_turn_id(bridge_dir, turn_id)
    return True


def _delta_recovery_status_edge(
    bridge_dir: Path,
    params: dict[str, Any],
    turn_id: str | None,
) -> _CodexTurnStatusEdge | None:
    """
    Recover a missed turn start from a scoped Codex delta.

    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex delta notification params.
    :param turn_id: Turn id extracted from *params*, e.g.
        ``"turn_abc123"``.
    :returns: Running status edge when the delta adopts the turn, or
        ``None`` when the delta is stale or ambiguous.
    """
    if not _try_recover_active_turn_from_delta(bridge_dir, params, turn_id):
        return None
    return _CodexTurnStatusEdge(
        status="running",
        turn_id=turn_id,
        source="delta:recovered",
    )


def _user_message_text(item: dict[str, Any]) -> str:
    """
    Convert a Codex ``userMessage`` item into plain text.

    :param item: Codex ``userMessage`` item.
    :returns: Joined text content.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n\n".join(parts)


def _user_message_has_file_content(item: dict[str, Any]) -> bool:
    """
    Return whether a Codex ``userMessage`` carries a non-text block.

    Codex echoes an attached image/file as a non-text content block (an
    image-only message arrives as ``[{"type": "image", "url": ...}]`` with
    no text block). Callers use this to decide whether a text-less message
    is still real and must be persisted, versus a genuinely empty one.

    :param item: Codex ``userMessage`` item.
    :returns: ``True`` when any content block is a non-text (image/file)
        block, ``False`` otherwise.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if isinstance(block_type, str) and block_type and block_type != "text":
            return True
    return False


def _is_codex_skill_wrapper(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<skill>") and stripped.endswith("</skill>")


def _json_string(value: dict[str, Any]) -> str | None:
    """
    Serialize a dict for OpenAI-compatible function call arguments.

    :param value: JSON-serializable dictionary, e.g.
        ``{"command": "pwd"}``.
    :returns: JSON string, or ``None`` when serialization fails.
    """
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _plan_text_from_update(params: dict[str, Any]) -> str | None:
    """
    Render a Codex ``turn/plan/updated`` payload as Markdown text.

    :param params: Codex plan update params.
    :returns: Markdown plan text, or ``None`` when no valid plan steps
        are present.
    """
    plan = params.get("plan")
    if not isinstance(plan, list) or not plan:
        return None
    lines: list[str] = []
    explanation = params.get("explanation")
    if isinstance(explanation, str) and explanation:
        lines.append(explanation)
        lines.append("")
    lines.append("Plan:")
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        if not isinstance(step, str) or not step:
            continue
        status = entry.get("status")
        marker = _plan_status_marker(status)
        lines.append(f"{marker} {step}")
    if len(lines) == 1 or (len(lines) == 3 and lines[-1] == "Plan:"):
        return None
    return "\n".join(lines)


def _plan_status_marker(status: Any) -> str:
    """
    Return a readable Markdown marker for a Codex plan step status.

    :param status: Codex step status value.
    :returns: Markdown list marker.
    """
    if status == "completed":
        return "- [x]"
    if status in {"inProgress", "in_progress"}:
        return "- [~]"
    return "- [ ]"


def _response_id(params: dict[str, Any]) -> str:
    """
    Build a stable Omnigent response id for a Codex notification.

    :param params: Codex notification params.
    :returns: Response id, e.g. ``"codex_turn_abc123"``.
    """
    turn_id = params.get("turnId")
    if isinstance(turn_id, str) and turn_id:
        return f"codex_{turn_id}"
    return "codex_native"


def _source_id(params: dict[str, Any], item: dict[str, Any]) -> str:
    """
    Build a stable per-record label for one Codex item.

    Only used for debug-log correlation — it is not sent to the server
    and is not a dedup key (the server persists external items with a
    random primary key).

    :param params: Codex notification params.
    :param item: Codex item payload.
    :returns: Record label, e.g. ``"turn_abc:item_xyz"``.
    """
    turn_id = params.get("turnId")
    item_id = item.get("id")
    left = turn_id if isinstance(turn_id, str) and turn_id else "thread"
    right = item_id if isinstance(item_id, str) and item_id else "item"
    return f"{left}:{right}"


def _completed_item_key(
    params: dict[str, Any],
    item: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> tuple[str, bool]:
    """
    Build a total dedup key for one durable Codex transcript item.

    The key is always non-empty so dedup is never silently disabled.
    Items with stable Codex-assigned ``id`` fields use
    ``threadId:turnId:item.id`` — identical across replay and live
    deliveries of the same item, so the second delivery is correctly
    dropped by the dedup gate.

    Items without a stable ``id`` fall back to a per-(thread, turn)
    positional counter. The counter is peeked here and only advanced by
    the caller after a successful claim. This guarantees *distinctness
    within a turn* (two genuinely different anonymous items get different
    keys) and ensures the key is never ``None`` (which would silently
    disable dedup). It does **not** guarantee cross-delivery dedup for
    anonymous items: if replay and live each deliver an anonymous item in
    the same (thread, turn), both advance the counter from the same
    starting value and therefore collide — one will be dropped. However,
    because Codex emits a stable ``id`` on all durable transcript items
    in practice, this anonymous path is a safety net for malformed events,
    not a primary dedup mechanism.

    :param params: Codex ``item/completed`` params.
    :param item: Codex item payload.
    :param forwarder_state: Mutable state holding per-(thread, turn)
        anonymous item counters.
    :returns: ``(key, is_anonymous)`` where ``key`` is the dedup key and
        ``is_anonymous`` is ``True`` when a positional counter was used.
    """
    thread_id = _thread_id_from_params(params) or "thread"
    turn_id = params.get("turnId")
    turn_id = turn_id if isinstance(turn_id, str) and turn_id else "turn"
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        return f"{thread_id}:{turn_id}:{item_id}", False
    return forwarder_state.peek_anon_item_key(thread_id, turn_id), True
