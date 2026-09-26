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
from typing import Any, TypeAlias
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
        if self._is_root(turn) and self._bridge_dir is not None:
            update_active_message_id(self._bridge_dir, message_id, status="busy")
        await self._begin_turn_if_needed(turn)


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
}
