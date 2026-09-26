"""SSE consumer that mirrors OpenCode v2 events into an Omnigent session.

The runner owns this forwarder (parallel to the codex-native forwarder). It
consumes the ``opencode serve`` event stream (``GET /api/event``), keeps the
events for this conversation's OpenCode session and its subagent child
sessions, and translates them into Omnigent session events posted to
``/v1/sessions/{id}/events``.

Each v2 event type maps to one ``_on_<name>`` handler in ``_HANDLERS`` at the
bottom of the module. Unknown events are logged and ignored. Durable
transcript items are deduped by stable OpenCode ids so the web UI and the TUI
driving the same session never double-post.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias, TypedDict
from urllib.parse import quote

import httpx

from omnigent.harnesses.opencode_native.bridge import update_active_message_id
from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeEvent,
)
from omnigent.harnesses.opencode_native.permissions import PolicyDecision
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

_AGENT_NAME = "opencode"
# Omnigent session-event types (must match the server's ingestion route;
# shared with the codex-native and claude-native forwarders).
_EXTERNAL_ITEM = "external_conversation_item"
_EXTERNAL_STATUS = "external_session_status"
_EXTERNAL_COMPACTION_STATUS = "external_compaction_status"
_EXTERNAL_SESSION_USAGE = "external_session_usage"
_EXTERNAL_MODEL_CHANGE = "external_model_change"
_EXTERNAL_ELICITATION_RESOLVED = "external_elicitation_resolved"
_EXTERNAL_OUTPUT_TEXT_DELTA = "external_output_text_delta"
_EXTERNAL_OUTPUT_REASONING_DELTA = "external_output_reasoning_delta"
_EXTERNAL_TOOL_OUTPUT_DELTA = "external_tool_output_delta"
_EXTERNAL_SESSION_INTERRUPTED = "external_session_interrupted"
_EXTERNAL_SUBAGENT_START = "external_subagent_start"

_STATUS_RUNNING = "running"
_STATUS_IDLE = "idle"
_STATUS_FAILED = "failed"

# Appended to a failed edge's output when opencode reports a provider-auth
# error so the web surface can prompt a re-auth.
_OPENCODE_REAUTH_HINT = (
    "OpenCode needs you to re-authenticate. Run `opencode auth login` and retry."
)
# v2 ``Session.StructuredError.type`` values (core/src/session/to-session-error.ts).
_AUTH_ERROR_TYPE = "provider.auth"
_ABORTED_ERROR_TYPE = "aborted"
_AUTH_STATUS_CODES = frozenset({401, 403})

# Bound the dedupe set so a long-lived session can't grow it without limit.
_MAX_DEDUPE_KEYS = 8192
# Web status label cap for a retry notice.
_MAX_BLOCKED_ON_CHARS = 200

_JsonMapping: TypeAlias = Mapping[str, object]


# Policy verdict resolver: receives a normalized policy input and returns a
# verdict mapping (or None when no policy is configured / reachable).
PolicyEvaluator = Callable[[_JsonMapping], Awaitable[_JsonMapping | None]]


@dataclass
class OpenCodeForwarderState:
    """
    Mutable dedupe state shared by every mirrored OpenCode session.

    :param seen: Bounded set of dedupe keys already posted.
    """

    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def mark(self, key: str) -> bool:
        """
        Record *key*; return ``True`` the first time it is seen.

        :param key: Stable dedupe key, e.g. ``"opencode:ses_1:tool-call:call_1"``.
        :returns: ``True`` when newly seen, ``False`` for a duplicate.
        """
        if key in self.seen:
            return False
        self.seen[key] = None
        while len(self.seen) > _MAX_DEDUPE_KEYS:
            self.seen.popitem(last=False)
        return True


@dataclass
class _SessionTurn:
    """
    Streaming state for one mirrored OpenCode session.

    :param session_id: OpenCode session id, e.g. ``"ses_abc"``.
    :param conversation_id: Omnigent conversation the session mirrors into;
        ``None`` for a subagent child until its conversation is minted.
    """

    session_id: str
    conversation_id: str | None
    turn_active: bool = False
    # Assistant message of the step in flight (``session.step.started``).
    assistant_message_id: str | None = None
    # Id the turn's ``running`` edge went out with; stamped on every item.
    running_response_id: str | None = None
    # Model of the step in flight, ``provider/id``.
    step_model: str | None = None
    # (assistantMessageID, ordinal) -> final text awaiting the step-end flush.
    pending_text: dict[tuple[str, int], str] = field(default_factory=dict)
    # (assistantMessageID, ordinal) -> text streamed so far without an end.
    streamed_text: dict[tuple[str, int], str] = field(default_factory=dict)
    # (assistantMessageID, ordinal) -> next live-preview chunk index.
    delta_index: dict[tuple[str, int], int] = field(default_factory=dict)
    # (assistantMessageID, ordinal) reasoning blocks already opened.
    reasoning_started: set[tuple[str, int]] = field(default_factory=set)
    # Tool call id -> tool name from ``session.tool.input.started``.
    tool_names: dict[str, str] = field(default_factory=dict)
    # Tool call id -> last ``metadata.output`` streamed as a delta.
    tool_output: dict[str, str] = field(default_factory=dict)
    # Retry notice currently shown on the running edge.
    retry_label: str | None = None


def _str_field(data: Mapping[str, Any], key: str) -> str | None:
    """Return ``data[key]`` when it is a non-empty string."""
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _int_field(data: Mapping[str, Any], key: str) -> int | None:
    """Return ``data[key]`` when it is an int (not a bool)."""
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event_session_id(event: OpenCodeEvent) -> str | None:
    """Return the OpenCode session an event belongs to (``form.created`` nests it)."""
    session_id = _str_field(event.data, "sessionID")
    if session_id is not None:
        return session_id
    form = event.data.get("form")
    if isinstance(form, Mapping):
        return _str_field(form, "sessionID")
    return None


def _model_ref(value: object) -> str | None:
    """Render a v2 ``Model.Ref`` ``{id, providerID, variant?}`` as ``provider/id``."""
    if not isinstance(value, Mapping):
        return None
    provider = value.get("providerID")
    model_id = value.get("id")
    if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
        return f"{provider}/{model_id}"
    return None


def opencode_tool_content_text(content: object, *, error: object = None) -> str:
    """
    Flatten a v2 tool result into ``function_call_output`` text.

    :param content: ``Tool.Content[]`` (``{type:"text", text}`` /
        ``{type:"file", uri, mime, name?}``) from ``session.tool.success`` /
        ``.failed`` or a completed tool state.
    :param error: ``Session.StructuredError`` ``{type, message}`` for a failed
        tool, else ``None``.
    :returns: The output text; failures are prefixed with ``[error]``.
    """
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "file":
                name = item.get("name") or item.get("uri") or item.get("mime") or "file"
                parts.append(f"[file: {name}]")
    text = "\n".join(part for part in parts if part)
    if isinstance(error, Mapping):
        message = error.get("message")
        detail = message if isinstance(message, str) and message else error.get("type")
        prefix = f"[error] {detail}" if detail else "[error]"
        return f"{prefix}\n{text}" if text else prefix
    return text


class _AssistantUsage(TypedDict):
    cost: float
    tokens: _JsonObject
    model: str | None
    model_id: str | None


class _UsageTotals(TypedDict):
    cost: float
    tokens: _JsonObject


def _int_or_zero(value: object) -> int:
    """Coerce an OpenCode token count (int or float) to a non-negative int."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0


class OpenCodeNativeForwarder:
    """
    Translate one OpenCode session's v2 event stream into Omnigent events.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param opencode_session_id: OpenCode session id to mirror.
    :param opencode_client: Client connected to the ``opencode serve``
        server (events, history, permission and form replies).
    :param server_client: HTTP client for the Omnigent server (event posts).
    :param bridge_dir: Native OpenCode bridge directory (active-id
        persistence). ``None`` disables bridge writes (tests).
    :param workspace: Session workspace, used for permission normalization.
    :param policy_evaluator: Optional async policy resolver. Production wires
        one that POSTs each request to ``/v1/sessions/{id}/policies/evaluate``
        (see ``omnigent.runner.native.orchestration._build_opencode_policy_evaluator``),
        where an ``ask`` verdict parks a human approval card.
    :param default_decision: Decision used when no evaluator is provided or it
        returns ``None``. Defaults to ``reject`` so an unconfigured policy
        fails closed.
    """

    def __init__(
        self,
        *,
        session_id: str,
        opencode_session_id: str,
        opencode_client: OpenCodeClient,
        server_client: httpx.AsyncClient,
        bridge_dir: Path | None = None,
        workspace: str | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
        default_decision: PolicyDecision = "reject",
    ) -> None:
        self._session_id = session_id
        self._opencode_session_id = opencode_session_id
        self._opencode = opencode_client
        self._server = server_client
        self._bridge_dir = bridge_dir
        self._workspace = workspace
        self._policy_evaluator = policy_evaluator
        self._default_decision = default_decision
        self.state = OpenCodeForwarderState()
        # OpenCode session id -> streaming state; the root plus subagent children.
        self._turns: dict[str, _SessionTurn] = {
            opencode_session_id: _SessionTurn(
                session_id=opencode_session_id, conversation_id=session_id
            )
        }
        self._permission_tasks: dict[str, asyncio.Task[None]] = {}
        self._form_tasks: dict[str, asyncio.Task[None]] = {}
        # assistantMessageID -> step usage (root session only).
        self._usage_by_message: dict[str, _AssistantUsage] = {}
        # Authoritative session totals from ``session.usage.updated``.
        self._session_totals: _UsageTotals | None = None
        self._last_usage_signature: tuple[tuple[str, object], ...] | None = None
        # Last model mirrored to Omnigent (``provider/id``), to dedupe switches.
        self._last_model: str | None = None

    async def run(self, *, max_reconnects: int | None = None) -> None:
        """
        Run the SSE consume loop with reconnect/backoff.

        :param max_reconnects: Reconnect cap (``None`` = unbounded); used by
            tests to bound the loop.
        """
        attempt = 0
        backoff = 0.5
        try:
            while True:
                try:
                    await self._consume_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - reconnect on any transient SSE failure.
                    _logger.warning(
                        "OpenCode forwarder SSE error for session=%s; reconnecting",
                        self._session_id,
                        exc_info=True,
                    )
                attempt += 1
                if max_reconnects is not None and attempt > max_reconnects:
                    return
                await asyncio.sleep(min(backoff, 5.0))
                backoff = min(backoff * 2, 5.0)
        finally:
            await self._cancel_background_tasks()

    async def _cancel_background_tasks(self) -> None:
        """Cancel and await every parked permission and form task."""
        tasks = [*self._permission_tasks.values(), *self._form_tasks.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._permission_tasks.clear()
        self._form_tasks.clear()

    async def _consume_once(self) -> None:
        """Consume the event stream once, dispatching each event."""
        async for event in self._opencode.stream_events():
            await self.handle_event(event)

    async def handle_event(self, event: OpenCodeEvent) -> None:
        """
        Translate one OpenCode event into Omnigent session events.

        :param event: A decoded ``/api/event`` frame.
        """
        if not self._event_targets_session(event):
            return
        handler = _HANDLERS.get(event.type)
        if handler is None:
            _logger.debug(
                "OpenCode forwarder ignoring event type=%s for session=%s",
                event.type,
                self._session_id,
            )
            return
        await handler(self, event)

    def _event_targets_session(self, event: OpenCodeEvent) -> bool:
        """
        Return whether *event* belongs to a mirrored session.

        Events carry ``data.sessionID`` (``form.created`` nests it under
        ``form``). Events without a session id pass through.
        """
        session_id = _event_session_id(event)
        return session_id is None or session_id in self._turns

    async def _active_turn(self, event: OpenCodeEvent) -> _SessionTurn | None:
        """Return the mirrored session state an event belongs to."""
        session_id = _event_session_id(event) or self._opencode_session_id
        return self._turns.get(session_id)

    def _is_root(self, turn: _SessionTurn) -> bool:
        """Whether *turn* is this conversation's own OpenCode session."""
        return turn.session_id == self._opencode_session_id

    def _key(self, *parts: str) -> str:
        """
        Build a forwarder-scoped dedupe key.

        :param parts: Key segments, e.g. ``("tool-call", "call_1")``.
        :returns: ``"opencode:<root sessionID>:<part>:..."``.
        """
        return "opencode:" + ":".join((self._opencode_session_id, *parts))

    async def _post_event(
        self,
        event_type: str,
        data: _JsonObject,
        *,
        conversation_id: str | None = None,
    ) -> httpx.Response | None:
        """
        POST one Omnigent session event.

        :param event_type: Omnigent event type, e.g. ``"external_session_status"``.
        :param data: Event data payload.
        :param conversation_id: Target conversation; defaults to this session's.
        :returns: The HTTP response, or ``None`` on transport failure.
        """
        target = conversation_id or self._session_id
        url = f"/v1/sessions/{quote(target, safe='')}/events"
        try:
            return await self._server.post(url, json={"type": event_type, "data": data})
        except httpx.HTTPError:
            _logger.warning(
                "OpenCode forwarder failed to post %s for session=%s",
                event_type,
                target,
                exc_info=True,
            )
            return None

    async def _post_status(
        self, turn: _SessionTurn, status: str, *, extra: _JsonMapping | None = None
    ) -> None:
        """Publish a coarse session status edge into *turn*'s conversation."""
        data: _JsonObject = {"status": status}
        if extra:
            data.update(extra)
        await self._post_event(_EXTERNAL_STATUS, data, conversation_id=turn.conversation_id)

    def _response_id(self, turn: _SessionTurn, message_id: str | None) -> str:
        """Per-turn ``response_id``: the running edge's id, else the message id."""
        return turn.running_response_id or message_id or turn.session_id

    async def _begin_turn_if_needed(self, turn: _SessionTurn) -> None:
        """Emit the turn's id-bearing ``running`` edge once, when the id is known.

        ``session.execution.started`` / ``session.status busy`` open the turn
        before ``session.step.started`` supplies the assistant message id, so
        the edge is deferred until that id exists; the mirrored items carry
        the same id, which lets the web render in-flight tool calls live.
        """
        turn.turn_active = True
        if turn.running_response_id is None and turn.assistant_message_id is not None:
            turn.running_response_id = turn.assistant_message_id
            await self._post_status(
                turn, _STATUS_RUNNING, extra={"response_id": turn.running_response_id}
            )

    async def _end_turn(
        self,
        turn: _SessionTurn,
        *,
        status: str = _STATUS_IDLE,
        extra: _JsonMapping | None = None,
    ) -> None:
        """Post the terminal edge stamped with the turn's id and reset per-turn state."""
        await self._flush_pending_text(turn)
        turn.turn_active = False
        turn.delta_index.clear()
        turn.reasoning_started.clear()
        turn.tool_output.clear()
        turn.retry_label = None
        terminal_id = turn.running_response_id or turn.assistant_message_id
        merged: _JsonObject = {"response_id": terminal_id or turn.session_id}
        if extra:
            merged.update(extra)
        if self._bridge_dir is not None and self._is_root(turn):
            update_active_message_id(self._bridge_dir, None, status="idle")
        await self._post_status(turn, status, extra=merged)
        turn.assistant_message_id = None
        turn.running_response_id = None

    async def _finish_turn(self, turn: _SessionTurn) -> None:
        """End an active turn as idle; a second terminal signal is a no-op."""
        if not turn.turn_active:
            return
        await self._post_session_usage()
        await self._end_turn(turn)

    async def _on_execution_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.started`` — open the turn."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._begin_turn_if_needed(turn)

    async def _on_execution_succeeded(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.succeeded`` — flush, usage, idle."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._finish_turn(turn)

    async def _on_session_status(self, event: OpenCodeEvent) -> None:
        """Handle ``session.status {busy|idle}``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        status = event.data.get("status")
        if not isinstance(status, Mapping):
            return
        status_type = status.get("type")
        if status_type == "busy":
            await self._begin_turn_if_needed(turn)
        elif status_type == "idle":
            await self._finish_turn(turn)

    async def _on_step_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.step.started`` — record the assistant id and model."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        if message_id is None:
            return
        turn.assistant_message_id = message_id
        turn.step_model = _model_ref(event.data.get("model"))
        if self._is_root(turn):
            if self._bridge_dir is not None:
                update_active_message_id(self._bridge_dir, message_id, status="busy")
            await self._observe_model(turn.step_model, explicit=False)
        await self._begin_turn_if_needed(turn)

    @staticmethod
    def _stream_id(message_id: str, kind: str, ordinal: int) -> str:
        """Live-preview id shared by text deltas and the item that retires them."""
        return f"opencode:{message_id}:{kind}:{ordinal}"

    async def _post_assistant_text(
        self, turn: _SessionTurn, text: str, *, message_id: str | None, stream_id: str
    ) -> None:
        """Persist a finalized assistant message that retires its live preview."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": _AGENT_NAME,
                    "content": [{"type": "output_text", "text": text}],
                },
                "response_id": self._response_id(turn, message_id),
                "message_id": stream_id,
            },
            conversation_id=turn.conversation_id,
        )

    async def _on_text_delta(self, event: OpenCodeEvent) -> None:
        """Handle ``session.text.delta`` — stream a live assistant preview chunk."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        delta = event.data.get("delta")
        if message_id is None or ordinal is None or not isinstance(delta, str) or not delta:
            return
        key = (message_id, ordinal)
        await self._begin_turn_if_needed(turn)
        index = turn.delta_index.get(key, 0)
        turn.delta_index[key] = index + 1
        turn.streamed_text[key] = turn.streamed_text.get(key, "") + delta
        await self._post_event(
            _EXTERNAL_OUTPUT_TEXT_DELTA,
            {
                "delta": delta,
                "message_id": self._stream_id(message_id, "text", ordinal),
                "index": index,
                "final": False,
            },
            conversation_id=turn.conversation_id,
        )

    async def _on_text_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.text.ended`` — buffer the full text for the step-end flush."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        text = event.data.get("text")
        if message_id is None or ordinal is None or not isinstance(text, str):
            return
        turn.streamed_text.pop((message_id, ordinal), None)
        turn.pending_text[(message_id, ordinal)] = text

    async def _flush_pending_text(self, turn: _SessionTurn) -> None:
        """Persist buffered assistant text as durable chat items, once each."""
        # Ordinal order, not arrival order: text around a tool call must read in sequence.
        for (message_id, ordinal), text in sorted(
            turn.pending_text.items(), key=lambda item: item[0][1]
        ):
            turn.pending_text.pop((message_id, ordinal), None)
            if not text:
                continue
            if not self.state.mark(self._key("text-final", message_id, str(ordinal))):
                continue
            await self._post_assistant_text(
                turn,
                text,
                message_id=message_id,
                stream_id=self._stream_id(message_id, "text", ordinal),
            )

    async def _on_step_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.step.ended`` / ``.failed`` — flush text, record usage."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        await self._flush_pending_text(turn)
        message_id = _str_field(event.data, "assistantMessageID")
        if message_id is None or not self._is_root(turn):
            return
        self._record_step_usage(message_id, event.data, turn.step_model)
        await self._post_session_usage()

    async def _on_reasoning_delta(self, event: OpenCodeEvent) -> None:
        """Handle ``session.reasoning.delta`` — transient reasoning chunk."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        delta = event.data.get("delta")
        if message_id is None or ordinal is None or not isinstance(delta, str) or not delta:
            return
        key = (message_id, ordinal)
        started = key not in turn.reasoning_started
        turn.reasoning_started.add(key)
        await self._begin_turn_if_needed(turn)
        await self._post_event(
            _EXTERNAL_OUTPUT_REASONING_DELTA,
            {"delta": delta, "started": started},
            conversation_id=turn.conversation_id,
        )

    async def _on_reasoning_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.reasoning.ended`` — post the whole block if no delta streamed."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        text = event.data.get("text")
        if message_id is None or ordinal is None or not isinstance(text, str) or not text:
            return
        key = (message_id, ordinal)
        if key in turn.reasoning_started:
            return
        turn.reasoning_started.add(key)
        await self._begin_turn_if_needed(turn)
        await self._post_event(
            _EXTERNAL_OUTPUT_REASONING_DELTA,
            {"delta": text, "started": True},
            conversation_id=turn.conversation_id,
        )

    async def _post_tool_call(
        self,
        turn: _SessionTurn,
        call_id: str,
        tool: str,
        arguments: _JsonObject,
        *,
        message_id: str | None,
    ) -> None:
        """Mirror a tool invocation as a function_call item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call",
                "item_data": {
                    "agent": _AGENT_NAME,
                    "name": tool,
                    "arguments": json.dumps(arguments, ensure_ascii=True),
                    "call_id": call_id,
                },
                "response_id": self._response_id(turn, message_id),
            },
            conversation_id=turn.conversation_id,
        )

    async def _post_tool_output(
        self, turn: _SessionTurn, call_id: str, output: str, *, message_id: str | None
    ) -> None:
        """Mirror a tool result as a function_call_output item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call_output",
                "item_data": {"call_id": call_id, "output": output},
                "response_id": self._response_id(turn, message_id),
            },
            conversation_id=turn.conversation_id,
        )

    async def _on_tool_input_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.input.started`` — remember the tool's name."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        name = _str_field(event.data, "name")
        if call_id is not None and name is not None:
            turn.tool_names[call_id] = name

    async def _on_tool_called(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.called`` — mirror the call as ``function_call``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        if call_id is None or not self.state.mark(self._key("tool-call", call_id)):
            return
        raw_input = event.data.get("input")
        arguments = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        await self._begin_turn_if_needed(turn)
        # Text the model wrote before the call lands above it in the chat.
        await self._flush_pending_text(turn)
        await self._post_tool_call(
            turn,
            call_id,
            turn.tool_names.get(call_id, "tool"),
            arguments,
            message_id=_str_field(event.data, "assistantMessageID"),
        )

    async def _on_tool_success(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.success`` — mirror the result as ``function_call_output``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        if call_id is None or not self.state.mark(self._key("tool-out", call_id)):
            return
        turn.tool_output.pop(call_id, None)
        await self._post_tool_output(
            turn,
            call_id,
            opencode_tool_content_text(event.data.get("content")),
            message_id=_str_field(event.data, "assistantMessageID"),
        )

    async def _on_tool_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.failed`` — post an ``[error]`` output.

        A malformed-input failure arrives without ``session.tool.called``, so
        the call half is posted first to keep the output paired.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        if call_id is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        if self.state.mark(self._key("tool-call", call_id)):
            await self._post_tool_call(
                turn, call_id, turn.tool_names.get(call_id, "tool"), {}, message_id=message_id
            )
        if not self.state.mark(self._key("tool-out", call_id)):
            return
        turn.tool_output.pop(call_id, None)
        output = opencode_tool_content_text(
            event.data.get("content"), error=event.data.get("error")
        )
        await self._post_tool_output(turn, call_id, output, message_id=message_id)

    async def _on_tool_progress(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.progress`` — stream incremental tool output.

        v2 progress metadata is a replacement snapshot. Built-in tools report
        ids only (shell ``{shellID}``, subagent ``{sessionID, status}``), so
        output streams only when a tool reports a growing ``metadata.output``
        string; only the new suffix is forwarded.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        metadata = event.data.get("metadata")
        if call_id is None or not isinstance(metadata, Mapping):
            return
        output = metadata.get("output")
        if not isinstance(output, str):
            return
        previous = turn.tool_output.get(call_id, "")
        turn.tool_output[call_id] = output
        if len(output) <= len(previous) or not output.startswith(previous):
            return
        await self._post_event(
            _EXTERNAL_TOOL_OUTPUT_DELTA,
            {"call_id": call_id, "delta": output[len(previous) :]},
            conversation_id=turn.conversation_id,
        )

    def _record_step_usage(
        self, message_id: str, data: Mapping[str, Any], model: str | None
    ) -> None:
        """Cache one assistant message's ``cost`` (USD) + ``tokens`` + model."""
        tokens = data.get("tokens")
        cost = data.get("cost")
        if not isinstance(tokens, Mapping) and not isinstance(cost, (int, float)):
            return
        self._usage_by_message[message_id] = {
            "cost": float(cost) if isinstance(cost, (int, float)) else 0.0,
            "tokens": {key: value for key, value in tokens.items() if isinstance(key, str)}
            if isinstance(tokens, Mapping)
            else {},
            "model": model,
            "model_id": model.split("/", 1)[1] if model else None,
        }

    async def _on_usage_updated(self, event: OpenCodeEvent) -> None:
        """Handle ``session.usage.updated`` — authoritative cumulative totals."""
        turn = await self._active_turn(event)
        if turn is None or not self._is_root(turn):
            return
        tokens = event.data.get("tokens")
        cost = event.data.get("cost")
        if not isinstance(tokens, Mapping) or not isinstance(cost, (int, float)):
            return
        self._session_totals = {
            "cost": float(cost),
            "tokens": {key: value for key, value in tokens.items() if isinstance(key, str)},
        }
        await self._post_session_usage()

    async def _post_session_usage(self) -> None:
        """Post cumulative cost/tokens + context occupancy as ``external_session_usage``.

        Cumulative fields come from ``session.usage.updated`` when seen, else
        the sum of per-step usage; the latest step's input + cache tokens drive
        the context ring. Deduped so repeated edges don't spam identical posts.
        """
        if not self._usage_by_message and self._session_totals is None:
            return
        cum_cost = 0.0
        cum_in = cum_out = cum_cache = 0
        latest: _AssistantUsage | None = None
        for entry in self._usage_by_message.values():
            cum_cost += entry["cost"]
            tokens = entry["tokens"]
            cum_in += _int_or_zero(tokens.get("input"))
            cum_out += _int_or_zero(tokens.get("output"))
            cache = tokens.get("cache")
            if isinstance(cache, Mapping):
                cum_cache += _int_or_zero(cache.get("read"))
            latest = entry
        if self._session_totals is not None:
            totals = self._session_totals["tokens"]
            cum_cost = self._session_totals["cost"]
            cum_in = _int_or_zero(totals.get("input"))
            cum_out = _int_or_zero(totals.get("output"))
            totals_cache = totals.get("cache")
            cum_cache = (
                _int_or_zero(totals_cache.get("read")) if isinstance(totals_cache, Mapping) else 0
            )
        data: _JsonObject = {
            "cumulative_cost_usd": round(cum_cost, 6),
            "cumulative_input_tokens": cum_in,
            "cumulative_output_tokens": cum_out,
            "cumulative_cache_read_input_tokens": cum_cache,
        }
        if latest is not None:
            latest_tokens = latest["tokens"]
            raw_cache = latest_tokens.get("cache")
            latest_cache = raw_cache if isinstance(raw_cache, Mapping) else {}
            ctx = (
                _int_or_zero(latest_tokens.get("input"))
                + _int_or_zero(latest_cache.get("read"))
                + _int_or_zero(latest_cache.get("write"))
            )
            if ctx > 0:
                data["context_tokens"] = ctx
            model_id = latest["model_id"]
            if model_id:
                try:
                    from omnigent.llms.context_window import get_model_context_window

                    data["context_window"] = get_model_context_window(model_id)
                except Exception:  # noqa: BLE001 - context window is best effort.
                    pass
            model = latest["model"]
            if model:
                data["model"] = model
        signature = tuple(sorted(data.items()))
        if signature == self._last_usage_signature:
            return
        self._last_usage_signature = signature
        await self._post_event(_EXTERNAL_SESSION_USAGE, data)

    async def _observe_model(self, model: str | None, *, explicit: bool) -> None:
        """Mirror a model change to Omnigent (``external_model_change``), deduped.

        The first model seen on a step is only recorded: it is the model the
        session already runs, not a switch. An explicit
        ``session.model.selected`` always mirrors.
        """
        if model is None or model == self._last_model:
            return
        previous = self._last_model
        self._last_model = model
        if previous is None and not explicit:
            return
        await self._post_event(_EXTERNAL_MODEL_CHANGE, {"model": model})

    async def _on_model_selected(self, event: OpenCodeEvent) -> None:
        """Handle ``session.model.selected`` — a TUI ``/model`` or API switch."""
        turn = await self._active_turn(event)
        if turn is None or not self._is_root(turn):
            return
        await self._observe_model(_model_ref(event.data.get("model")), explicit=True)

    async def _on_execution_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.failed`` — failed (or re-auth) status edge.

        ``error`` is a ``Session.StructuredError`` ``{type, message, status?}``.
        ``aborted`` is a user interrupt and takes the idle path; ``provider.auth``
        or an HTTP 401/403 status carries the re-auth hint.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        error = event.data.get("error")
        _logger.warning("OpenCode session error for session=%s: %s", self._session_id, error)
        error_map: Mapping[str, Any] = error if isinstance(error, Mapping) else {}
        error_type = error_map.get("type")
        if error_type == _ABORTED_ERROR_TYPE:
            await self._end_turn(turn)
            return
        message = error_map.get("message")
        if not isinstance(message, str) or not message.strip():
            message = "OpenCode session ended with an error."
        is_auth = error_type == _AUTH_ERROR_TYPE or error_map.get("status") in _AUTH_STATUS_CODES
        extra: _JsonObject = {"output": message.strip()}
        if is_auth:
            extra["output"] = f"{message.strip()}\n\n{_OPENCODE_REAUTH_HINT}"
            extra["reauth_required"] = True
        if self._is_root(turn):
            await self._post_session_usage()
        await self._end_turn(turn, status=_STATUS_FAILED, extra=extra)


def opencode_tool_output_text(state: _JsonMapping) -> str:
    """
    Extract shared durable output from a completed OpenCode tool state.

    :param state: The opencode tool part ``state`` (``output`` /
        ``metadata.output``).
    :returns: A string suitable for ``function_call_output``.
    """
    output = state.get("output")
    if isinstance(output, str) and output:
        return output
    metadata = state.get("metadata")
    if isinstance(metadata, Mapping):
        meta_out = metadata.get("output")
        if isinstance(meta_out, str) and meta_out:
            return meta_out
    if output is not None and not isinstance(output, str):
        return json.dumps(output, ensure_ascii=True)
    return ""


# Event type -> handler. Keys are v2 ``/api/event`` ``type`` discriminators
# (packages/schema/src/session-event.ts, session-status-event.ts,
# permission.ts, form.ts in OpenCode v2.0.18).
_HANDLERS: dict[str, Callable[[OpenCodeNativeForwarder, OpenCodeEvent], Awaitable[None]]] = {
    "session.execution.started": OpenCodeNativeForwarder._on_execution_started,
    "session.execution.succeeded": OpenCodeNativeForwarder._on_execution_succeeded,
    "session.status": OpenCodeNativeForwarder._on_session_status,
    "session.step.started": OpenCodeNativeForwarder._on_step_started,
    "session.model.selected": OpenCodeNativeForwarder._on_model_selected,
    "session.text.delta": OpenCodeNativeForwarder._on_text_delta,
    "session.text.ended": OpenCodeNativeForwarder._on_text_ended,
    "session.step.ended": OpenCodeNativeForwarder._on_step_ended,
    "session.reasoning.delta": OpenCodeNativeForwarder._on_reasoning_delta,
    "session.reasoning.ended": OpenCodeNativeForwarder._on_reasoning_ended,
    "session.tool.input.started": OpenCodeNativeForwarder._on_tool_input_started,
    "session.tool.called": OpenCodeNativeForwarder._on_tool_called,
    "session.tool.success": OpenCodeNativeForwarder._on_tool_success,
    "session.tool.failed": OpenCodeNativeForwarder._on_tool_failed,
    "session.tool.progress": OpenCodeNativeForwarder._on_tool_progress,
    "session.usage.updated": OpenCodeNativeForwarder._on_usage_updated,
    "session.step.failed": OpenCodeNativeForwarder._on_step_ended,
    "session.execution.failed": OpenCodeNativeForwarder._on_execution_failed,
}
