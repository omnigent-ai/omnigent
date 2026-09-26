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
    OpenCodeClientError,
    OpenCodeEvent,
)
from omnigent.harnesses.opencode_native.permissions import (
    OpenCodePermissionRequest,
    PolicyDecision,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
)
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


def _user_file_block(file: Mapping[str, Any]) -> _JsonObject:
    """Render a v2 ``Prompt.FileAttachment`` as a user content block."""
    mime = file.get("mime")
    data = file.get("data")
    if isinstance(mime, str) and mime.startswith("image/") and isinstance(data, str) and data:
        return {"type": "input_image", "image_url": f"data:{mime};base64,{data}"}
    name = file.get("name")
    label = name if isinstance(name, str) and name else (mime or "attachment")
    return {"type": "input_text", "text": f"[attachment: {label}]"}


@dataclass(frozen=True)
class FormQuestion:
    """
    One v2 form field rendered as a web ``ask_user_question`` entry.

    :param key: Form field key; also the web question id.
    :param kind: v2 field type, e.g. ``"string"`` or ``"multiselect"``.
    :param question: The web question payload.
    :param values_by_label: Option label -> option value.
    """

    key: str
    kind: str
    question: _JsonObject
    values_by_label: dict[str, str]


def form_questions(fields: object) -> list[FormQuestion] | None:
    """
    Map v2 ``Form.Field`` entries onto web ``ask_user_question`` questions.

    ``string`` with options -> single select; ``multiselect`` -> multi select;
    ``boolean`` -> Yes/No; ``number``/``integer`` and option-less ``string`` ->
    free text (the web form always offers a custom text row); ``external`` ->
    a Done acknowledgement naming the URL. Hidden fields keep their default.

    :param fields: ``form.fields`` from ``form.created``.
    :returns: The questions, or ``None`` when a visible field cannot be rendered.
    """
    if not isinstance(fields, list) or not fields:
        return None
    questions: list[FormQuestion] = []
    for raw_field in fields:
        if not isinstance(raw_field, Mapping):
            return None
        key = _str_field(raw_field, "key")
        kind = _str_field(raw_field, "type")
        if key is None or kind is None:
            return None
        if raw_field.get("hidden") is True:
            continue
        title = _str_field(raw_field, "title")
        prompt = _str_field(raw_field, "description") or title or key
        options: list[_JsonObject] = []
        values: dict[str, str] = {}
        multi = False
        if kind in ("string", "multiselect"):
            raw_options = raw_field.get("options")
            if raw_options is not None and not isinstance(raw_options, list):
                return None
            for option in raw_options or []:
                if not isinstance(option, Mapping):
                    return None
                label = _str_field(option, "label")
                value = option.get("value")
                if label is None or not isinstance(value, str):
                    return None
                entry: _JsonObject = {"label": label}
                description = _str_field(option, "description")
                if description is not None:
                    entry["description"] = description
                options.append(entry)
                values[label] = value
            if kind == "multiselect":
                if not options:
                    return None
                multi = True
        elif kind == "boolean":
            options = [{"label": "Yes"}, {"label": "No"}]
        elif kind == "external":
            url = _str_field(raw_field, "url")
            if url is None:
                return None
            prompt = f"{prompt}\n\nOpen {url} and choose Done when finished."
            options = [{"label": "Done"}]
        elif kind not in ("number", "integer"):
            return None
        question: _JsonObject = {
            "question": prompt,
            "options": options,
            "multiSelect": multi,
            "id": key,
        }
        if title is not None:
            question["header"] = title
        questions.append(
            FormQuestion(key=key, kind=kind, question=question, values_by_label=values)
        )
    return questions


def _parse_boolean(raw: str) -> bool | None:
    """Parse a Yes/No answer (or typed true/false) into a bool."""
    token = raw.strip().lower()
    if token in ("yes", "y", "true"):
        return True
    if token in ("no", "n", "false"):
        return False
    return None


def _parse_number(raw: str, *, integer: bool) -> int | float | None:
    """Parse a typed number; ``None`` when it is not a valid number."""
    try:
        number = float(raw.strip())
    except ValueError:
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


def _field_active(field: Mapping[str, Any], answer: Mapping[str, Any]) -> bool:
    """Evaluate a field's ``when`` conditions (all must hold) against *answer*."""
    conditions = field.get("when")
    if not isinstance(conditions, list):
        return True
    for condition in conditions:
        if not isinstance(condition, Mapping):
            return False
        key = condition.get("key")
        if not isinstance(key, str) or key not in answer:
            return False
        value = answer[key]
        target = condition.get("value")
        hit = target in value if isinstance(value, list) else value == target
        if (condition.get("op") == "eq") != hit:
            return False
    return True


def form_answer(
    questions: list[FormQuestion],
    fields: list[Any],
    content: Mapping[str, Any],
) -> dict[str, Any] | None:
    """
    Convert the web form result into a v2 ``Form.Answer``.

    :param questions: The questions built by :func:`form_questions`.
    :param fields: The original ``form.fields`` (for ``when`` conditions).
    :param content: ``ElicitationResult.content`` keyed by question id.
    :returns: ``{field key: value}``, or ``None`` when an answer is invalid
        (the caller cancels the form).
    """
    answer: dict[str, Any] = {}
    for question in questions:
        raw = content.get(question.key)
        if question.kind == "external":
            answer[question.key] = True
            continue
        if raw is None:
            continue
        if question.kind == "multiselect":
            items = [raw] if isinstance(raw, str) else raw
            if not isinstance(items, list):
                return None
            answer[question.key] = [
                question.values_by_label.get(item, item) for item in items if isinstance(item, str)
            ]
            continue
        if not isinstance(raw, str):
            return None
        if question.kind == "string":
            answer[question.key] = question.values_by_label.get(raw, raw)
        elif question.kind == "boolean":
            parsed_bool = _parse_boolean(raw)
            if parsed_bool is None:
                return None
            answer[question.key] = parsed_bool
        else:
            parsed_number = _parse_number(raw, integer=question.kind == "integer")
            if parsed_number is None:
                return None
            answer[question.key] = parsed_number
    by_key = {
        f["key"]: f for f in fields if isinstance(f, Mapping) and isinstance(f.get("key"), str)
    }
    return {
        key: value
        for key, value in answer.items()
        if by_key.get(key, {}).get("type") == "external"
        or _field_active(by_key.get(key, {}), answer)
    }


@dataclass
class _PendingChild:
    """
    A subagent child session awaiting its Omnigent conversation.

    :param parent_id: Parent OpenCode session id.
    :param agent: OpenCode agent the child runs, e.g. ``"explore"``.
    :param title: Child session title (the subagent task description).
    :param tool_use_id: Parent ``subagent`` tool call id, once known.
    """

    parent_id: str
    agent: str
    title: str
    tool_use_id: str | None = None


def _child_session_id(response: httpx.Response | None) -> str | None:
    """Read the minted child conversation id from an ``external_subagent_start`` ack."""
    if response is None or response.status_code >= 400 or not response.content:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    child = body.get("child_session_id") if isinstance(body, dict) else None
    return child if isinstance(child, str) and child else None


def _history_message_settled(message: Mapping[str, Any]) -> bool:
    """Return whether a v2 ``Session.Message.Info`` can no longer change."""
    kind = message.get("type")
    if kind == "assistant":
        time_info = message.get("time")
        return isinstance(time_info, Mapping) and isinstance(
            time_info.get("completed"), (int, float)
        )
    if kind == "compaction":
        return message.get("status") != "running"
    return True


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
        # inboxID -> user prompt payload, posted when the prompt is delivered.
        self._inbox_items: dict[str, _JsonObject] = {}
        # Child session id -> subagent start details until its conversation exists.
        self._pending_children: dict[str, _PendingChild] = {}
        # Newest history message known settled; the reconnect catch-up cursor.
        self._last_seen_message_id: str | None = None

    async def run(self, *, max_reconnects: int | None = None) -> None:
        """
        Run the SSE consume loop with reconnect/backoff and gap-fill.

        The first connection pre-marks persisted history; every reconnect
        replays history persisted after the last settled message.

        :param max_reconnects: Reconnect cap (``None`` = unbounded); used by
            tests to bound the loop.
        """
        attempt = 0
        backoff = 0.5
        try:
            while True:
                if attempt == 0:
                    await self.seed_dedupe_from_history()
                else:
                    await self.catch_up_from_history()
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
        ``form``). The root session and known subagent children pass; a
        ``session.created`` whose ``parentID`` is mirrored passes so the child
        can be registered. Events without a session id pass through.
        """
        session_id = _event_session_id(event)
        if session_id is None or session_id in self._turns:
            return True
        if event.type == "session.created":
            parent_id = _str_field(event.data, "parentID")
            return parent_id is not None and parent_id in self._turns
        return False

    async def _active_turn(self, event: OpenCodeEvent) -> _SessionTurn | None:
        """Return the event's session state once its Omnigent conversation exists."""
        session_id = _event_session_id(event) or self._opencode_session_id
        turn = self._turns.get(session_id)
        if turn is None:
            return None
        if turn.conversation_id is None:
            await self._start_child_conversation(session_id)
        return turn if turn.conversation_id is not None else None

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
        await self._persist_partial_text(turn)
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
        """Handle ``session.status {busy|idle|retry}``."""
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
        elif status_type == "retry":
            message = status.get("message")
            await self._post_retry_status(
                turn,
                _int_field(status, "attempt"),
                message if isinstance(message, str) else None,
            )

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
        if turn.retry_label is not None:
            turn.retry_label = None
            await self._post_status(
                turn, _STATUS_RUNNING, extra={"response_id": self._response_id(turn, message_id)}
            )

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
        """Handle ``session.tool.progress`` — register subagents, stream output.

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
        child_id = _str_field(metadata, "sessionID")
        if child_id is not None and turn.tool_names.get(call_id) == "subagent":
            await self._link_child_to_call(turn, child_id, call_id)
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

    async def _persist_partial_text(self, turn: _SessionTurn) -> None:
        """Persist streamed text whose ``session.text.ended`` never arrived."""
        for (message_id, ordinal), text in list(turn.streamed_text.items()):
            turn.streamed_text.pop((message_id, ordinal), None)
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

    async def _on_execution_interrupted(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.interrupted {reason}``.

        A ``user`` interrupt (web Stop or TUI Esc) is an ordinary idle; any
        other reason (``shutdown``/``superseded``/``inactivity``) also posts
        ``external_session_interrupted`` so the web marks the turn cut short.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        if event.data.get("reason") != "user":
            await self._post_event(
                _EXTERNAL_SESSION_INTERRUPTED,
                {"response_id": self._response_id(turn, turn.assistant_message_id)},
                conversation_id=turn.conversation_id,
            )
        await self._end_turn(turn)

    async def _post_retry_status(
        self, turn: _SessionTurn, attempt: int | None, message: str | None
    ) -> None:
        """Show a provider retry on the running edge (``blocked_on``), deduped."""
        label = f"Retrying (attempt {attempt})" if attempt else "Retrying"
        if message:
            label = f"{label}: {message}"
        label = label[:_MAX_BLOCKED_ON_CHARS]
        if label == turn.retry_label:
            return
        turn.retry_label = label
        await self._begin_turn_if_needed(turn)
        await self._post_status(
            turn,
            _STATUS_RUNNING,
            extra={
                "response_id": self._response_id(turn, turn.assistant_message_id),
                "blocked_on": label,
            },
        )

    async def _on_retry_scheduled(self, event: OpenCodeEvent) -> None:
        """Handle ``session.retry.scheduled {attempt, at, error}``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        error = event.data.get("error")
        message = error.get("message") if isinstance(error, Mapping) else None
        await self._post_retry_status(
            turn,
            _int_field(event.data, "attempt"),
            message if isinstance(message, str) else None,
        )

    async def _on_compaction_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.compaction.started`` (auto or manual)."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._post_event(
                _EXTERNAL_COMPACTION_STATUS,
                {"status": "in_progress"},
                conversation_id=turn.conversation_id,
            )

    async def _on_compaction_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.compaction.ended``."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._post_event(
                _EXTERNAL_COMPACTION_STATUS,
                {"status": "completed"},
                conversation_id=turn.conversation_id,
            )

    async def _on_compaction_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.compaction.failed``."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._post_event(
                _EXTERNAL_COMPACTION_STATUS,
                {"status": "failed"},
                conversation_id=turn.conversation_id,
            )

    async def _post_message_content(
        self,
        turn: _SessionTurn,
        role: str,
        content: list[_JsonObject],
        *,
        response_id: str,
    ) -> None:
        """Persist a message item with arbitrary content blocks."""
        item_data: _JsonObject = {"role": role, "content": content}
        if role == "assistant":
            item_data["agent"] = _AGENT_NAME
        await self._post_event(
            _EXTERNAL_ITEM,
            {"item_type": "message", "item_data": item_data, "response_id": response_id},
            conversation_id=turn.conversation_id,
        )

    async def _post_user_payload(
        self, turn: _SessionTurn, message_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Post a user prompt (text + attachments) once per message id."""
        content: list[_JsonObject] = []
        text = payload.get("text")
        if isinstance(text, str) and text:
            content.append({"type": "input_text", "text": text})
        files = payload.get("files")
        for file in files if isinstance(files, list) else []:
            if isinstance(file, Mapping):
                content.append(_user_file_block(file))
        if not content or not self.state.mark(self._key("user", message_id)):
            return
        await self._post_message_content(turn, "user", content, response_id=message_id)

    async def _on_inbox_enqueued(self, event: OpenCodeEvent) -> None:
        """Handle ``session.inbox.enqueued`` — hold a user prompt until delivered."""
        inbox_id = _str_field(event.data, "inboxID")
        item = event.data.get("item")
        if inbox_id is None or not isinstance(item, Mapping) or item.get("type") != "user":
            return
        payload = item.get("payload")
        if isinstance(payload, Mapping):
            self._inbox_items[inbox_id] = dict(payload)

    async def _on_inbox_delivered(self, event: OpenCodeEvent) -> None:
        """Handle ``session.inbox.delivered`` — the prompt joined the transcript.

        The delivered inbox id is the user message id, so the prompt posts in
        transcript order (a queued prompt appears when it runs, not when sent).
        """
        turn = await self._active_turn(event)
        inbox_id = _str_field(event.data, "inboxID")
        if turn is None or inbox_id is None:
            return
        payload = self._inbox_items.pop(inbox_id, None)
        if payload is not None:
            await self._post_user_payload(turn, inbox_id, payload)

    async def _on_inbox_cancelled(self, event: OpenCodeEvent) -> None:
        """Handle ``session.inbox.cancelled`` — drop a queued prompt."""
        inbox_id = _str_field(event.data, "inboxID")
        if inbox_id is not None:
            self._inbox_items.pop(inbox_id, None)

    async def _on_permission_asked(self, event: OpenCodeEvent) -> None:
        """Handle ``permission.asked`` — evaluate policy in a background task.

        The evaluator can park on a human approval card, so it never runs
        inline: that would stall this session's event loop, including the
        ``permission.replied`` that withdraws the card when the TUI answers.
        """
        request = parse_permission_request(event.data)
        if request is None:
            return
        if not self.state.mark(self._key("perm", request.request_id)):
            return
        task = asyncio.create_task(self._handle_permission(request))
        self._permission_tasks[request.request_id] = task
        task.add_done_callback(
            lambda _t, rid=request.request_id: self._permission_tasks.pop(rid, None)
        )

    async def _handle_permission(self, request: OpenCodePermissionRequest) -> None:
        """Resolve one permission and reply ``once`` or ``reject`` (never ``always``)."""
        decision = await self._resolve_permission(request_dict=request)
        # ``ask`` means no human resolution was obtained upstream: fail closed.
        reply = decision_to_reply(decision) or "reject"
        # Marked before replying so our own ``permission.replied`` echo is ignored.
        self.state.mark(self._key("perm-replied", request.request_id))
        try:
            # No reply message: in v2 a reject message tells the model to continue.
            await self._opencode.reply_permission(
                request.session_id or self._opencode_session_id, request.request_id, reply
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - surface it: OpenCode stays blocked on the request.
            _logger.warning(
                "OpenCode permission reply failed for request=%s",
                request.request_id,
                exc_info=True,
            )
            turn = self._turns.get(request.session_id or self._opencode_session_id)
            if turn is not None:
                await self._post_status(
                    turn,
                    _STATUS_RUNNING,
                    extra={"blocked_on": f"permission reply failed for {request.request_id}"},
                )

    async def _resolve_permission(
        self, *, request_dict: OpenCodePermissionRequest
    ) -> PolicyDecision:
        """
        Resolve a permission request to a normalized decision.

        :param request_dict: The parsed permission request.
        :returns: The normalized policy decision.
        """
        if self._policy_evaluator is None:
            return self._default_decision
        normalized = normalize_for_policy(
            request_dict,
            omnigent_session_id=self._session_id,
            workspace=self._workspace,
        )
        try:
            verdict = await self._policy_evaluator(normalized)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - policy errors fail closed.
            _logger.warning("OpenCode policy evaluation failed", exc_info=True)
            return "ask"
        if verdict is None:
            return self._default_decision
        return map_verdict_to_decision(verdict)

    async def _on_permission_replied(self, event: OpenCodeEvent) -> None:
        """Handle ``permission.replied`` — first answer wins.

        Our own reply is marked before it is sent, so its echo is ignored.
        Otherwise the TUI answered first: cancel the still-parked evaluation
        and clear the web card.
        """
        request_id = _str_field(event.data, "requestID")
        if request_id is None or not self.state.mark(self._key("perm-replied", request_id)):
            return
        task = self._permission_tasks.pop(request_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._post_event(_EXTERNAL_ELICITATION_RESOLVED, {"elicitation_id": request_id})

    async def _on_form_created(self, event: OpenCodeEvent) -> None:
        """Handle ``form.created`` — park a web question card in the background."""
        form = event.data.get("form")
        if not isinstance(form, Mapping):
            return
        form_id = _str_field(form, "id")
        session_id = _str_field(form, "sessionID")
        if form_id is None or session_id is None:
            return
        if not self.state.mark(self._key("form", form_id)):
            return
        task = asyncio.create_task(self._handle_form(session_id, form_id, dict(form)))
        self._form_tasks[form_id] = task
        task.add_done_callback(lambda _t, fid=form_id: self._form_tasks.pop(fid, None))

    async def _handle_form(self, session_id: str, form_id: str, form: dict[str, Any]) -> None:
        """Park one form as a web card and reply with the mapped answer.

        Any outcome other than a valid ``accept`` cancels the form so the
        OpenCode turn is never wedged. ``CancelledError`` propagates: it means
        the TUI answered first (see :meth:`_on_permission_replied`-style flow).
        """
        fields = form.get("fields")
        questions = form_questions(fields)
        if questions is None or not isinstance(fields, list):
            await self._cancel_form_quietly(session_id, form_id)
            return
        try:
            if not questions:
                # Marked before replying so our own form.replied echo is ignored.
                self.state.mark(self._key("form-replied", form_id))
                await self._opencode.reply_form(session_id, form_id, {})
                return
            title = _str_field(form, "title")
            first_prompt = questions[0].question["question"]
            verdict = await self._park_elicitation(
                form_id,
                message=title or "OpenCode is asking a question",
                payload={"questions": [question.question for question in questions]},
                preview=first_prompt[:1024] if isinstance(first_prompt, str) else None,
            )
            if verdict is None or verdict.get("action") != "accept":
                await self._cancel_form_quietly(session_id, form_id)
                return
            content = verdict.get("content")
            answer = form_answer(questions, fields, content if isinstance(content, dict) else {})
            if answer is None:
                await self._cancel_form_quietly(session_id, form_id)
                return
            # Marked before replying so our own form.replied echo is ignored.
            self.state.mark(self._key("form-replied", form_id))
            await self._opencode.reply_form(session_id, form_id, answer)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OpenCodeClientError) as exc:
            _logger.warning("OpenCode form reply failed for form=%s: %s", form_id, exc)
            await self._cancel_form_quietly(session_id, form_id)
            turn = self._turns.get(session_id)
            if turn is not None:
                await self._post_status(
                    turn,
                    _STATUS_RUNNING,
                    extra={"blocked_on": f"form reply failed for {form_id}"},
                )

    async def _cancel_form_quietly(self, session_id: str, form_id: str) -> None:
        """Best-effort cancel a form; a TUI answer commonly makes this 404."""
        # Marked before cancelling so our own form.cancelled echo is ignored.
        self.state.mark(self._key("form-replied", form_id))
        try:
            await self._opencode.cancel_form(session_id, form_id)
        except (httpx.HTTPError, OpenCodeClientError):
            _logger.debug("OpenCode form cancel for form=%s failed", form_id, exc_info=True)

    async def _park_elicitation(
        self,
        elicitation_id: str,
        *,
        message: str,
        payload: dict[str, Any],
        preview: str | None,
    ) -> dict[str, Any] | None:
        """POST the native permission hook for a form; return the web verdict.

        Returns ``None`` for every "no answer" outcome (transport error, status
        >= 400, empty body, or non-dict JSON). The structured
        ``ask_user_question`` is the payload the web UI renders.
        """
        body: dict[str, Any] = {
            "elicitation_id": elicitation_id,
            "operation_type": "question",
            "agent": "OpenCode",
            "policy_name": "opencode_native_question",
            "message": message,
            "ask_user_question": payload,
        }
        if preview is not None:
            body["content_preview"] = preview
        url = f"/v1/sessions/{quote(self._session_id, safe='')}/hooks/native-permission-request"
        try:
            response = await self._server.post(url, json=body)
        except httpx.HTTPError:
            _logger.warning(
                "OpenCode form hook POST failed for session=%s form=%s",
                self._session_id,
                elicitation_id,
                exc_info=True,
            )
            return None
        if response.status_code >= 400:
            _logger.warning(
                "OpenCode form hook rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
            return None
        if not response.content:
            return None
        try:
            result = response.json()
        except ValueError:
            _logger.warning("OpenCode form hook returned non-JSON: %s", response.text[:512])
            return None
        return result if isinstance(result, dict) else None

    async def _on_form_resolved(self, event: OpenCodeEvent) -> None:
        """Handle ``form.replied`` / ``form.cancelled`` — withdraw the web card.

        Our own reply/cancel is marked before it is sent, so its echo is
        ignored. Otherwise the TUI answered first: cancel the still-parked
        task and clear the web card.
        """
        form_id = _str_field(event.data, "id")
        if form_id is None or not self.state.mark(self._key("form-replied", form_id)):
            return
        task = self._form_tasks.pop(form_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._post_event(_EXTERNAL_ELICITATION_RESOLVED, {"elicitation_id": form_id})

    async def _on_session_created(self, event: OpenCodeEvent) -> None:
        """Handle ``session.created {parentID}`` — register a subagent child session."""
        child_id = _str_field(event.data, "sessionID")
        parent_id = _str_field(event.data, "parentID")
        if child_id is None or parent_id is None or child_id in self._turns:
            return
        self._turns[child_id] = _SessionTurn(session_id=child_id, conversation_id=None)
        self._pending_children[child_id] = _PendingChild(
            parent_id=parent_id,
            agent=_str_field(event.data, "agent") or "subagent",
            title=_str_field(event.data, "title") or "",
        )

    async def _link_child_to_call(self, parent: _SessionTurn, child_id: str, call_id: str) -> None:
        """Bind a child session to its parent ``subagent`` call and mint it."""
        if child_id not in self._turns:
            self._turns[child_id] = _SessionTurn(session_id=child_id, conversation_id=None)
            self._pending_children[child_id] = _PendingChild(
                parent_id=parent.session_id, agent="subagent", title=""
            )
        pending = self._pending_children.get(child_id)
        if pending is not None and pending.tool_use_id is None:
            pending.tool_use_id = call_id
        await self._start_child_conversation(child_id)

    async def _start_child_conversation(self, child_id: str) -> None:
        """POST ``external_subagent_start`` on the parent and adopt the child id."""
        turn = self._turns.get(child_id)
        pending = self._pending_children.get(child_id)
        if turn is None or pending is None or turn.conversation_id is not None:
            return
        parent = self._turns.get(pending.parent_id)
        if parent is not None and parent.conversation_id is None:
            await self._start_child_conversation(parent.session_id)
        parent_conversation = parent.conversation_id if parent is not None else None
        response = await self._post_event(
            _EXTERNAL_SUBAGENT_START,
            {
                "subagent_id": child_id,
                "agent_type": pending.agent,
                "description": pending.title,
                "tool_use_id": pending.tool_use_id or child_id,
            },
            conversation_id=parent_conversation or self._session_id,
        )
        child_conversation = _child_session_id(response)
        if child_conversation is None:
            _logger.warning("OpenCode subagent start failed for child session=%s", child_id)
            return
        self._pending_children.pop(child_id, None)
        turn.conversation_id = child_conversation

    async def seed_dedupe_from_history(self) -> None:
        """
        Pre-mark persisted history so a restart never re-posts it.

        Best effort: a history failure leaves the dedupe set empty. Rebuilds
        cumulative usage and the last mirrored model from assistant messages.
        """
        try:
            messages = await self._opencode.list_messages(self._opencode_session_id)
        except Exception:  # noqa: BLE001 - seeding is best effort.
            _logger.debug("OpenCode forwarder could not seed dedupe from history", exc_info=True)
            return
        settled_prefix = True
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            self._mark_history_message(message)
            message_id = _str_field(message, "id")
            if settled_prefix and message_id is not None and _history_message_settled(message):
                self._last_seen_message_id = message_id
            else:
                settled_prefix = False
        try:
            await self._post_session_usage()
        except Exception:  # noqa: BLE001 - usage re-post is best effort.
            _logger.debug(
                "OpenCode forwarder could not re-post usage after seeding", exc_info=True
            )

    def _mark_history_message(self, message: Mapping[str, Any]) -> None:
        """Pre-mark one history message's dedupe keys and record its usage."""
        message_id = _str_field(message, "id")
        if message_id is None:
            return
        kind = message.get("type")
        if kind == "user":
            self.state.mark(self._key("user", message_id))
            return
        if kind != "assistant":
            return
        model = _model_ref(message.get("model"))
        self._record_step_usage(message_id, message, model)
        if model is not None:
            self._last_model = model
        content = message.get("content")
        text_ordinal = 0
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text":
                self.state.mark(self._key("text-final", message_id, str(text_ordinal)))
                text_ordinal += 1
            elif item.get("type") == "tool":
                call_id = _str_field(item, "id")
                state = item.get("state")
                status = state.get("status") if isinstance(state, Mapping) else None
                if call_id is None or status == "streaming":
                    continue
                self.state.mark(self._key("tool-call", call_id))
                if status in ("completed", "error"):
                    self.state.mark(self._key("tool-out", call_id))

    async def catch_up_from_history(self) -> None:
        """
        Replay history persisted after the last settled message.

        The live stream never replays missed events, so after a reconnect the
        forwarder re-reads ``GET /api/session/{id}/message`` past the cursor
        and feeds unseen content through the normal post paths; dedupe keys
        suppress anything already posted.
        """
        try:
            messages = await self._opencode.list_messages(
                self._opencode_session_id, after_id=self._last_seen_message_id
            )
        except Exception:  # noqa: BLE001 - catch-up is best effort.
            _logger.debug("OpenCode forwarder could not catch up from history", exc_info=True)
            return
        turn = self._turns[self._opencode_session_id]
        settled_prefix = True
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            await self._replay_history_message(turn, message)
            message_id = _str_field(message, "id")
            if settled_prefix and message_id is not None and _history_message_settled(message):
                self._last_seen_message_id = message_id
            else:
                settled_prefix = False
        try:
            await self._post_session_usage()
        except Exception:  # noqa: BLE001 - usage re-post is best effort.
            _logger.debug(
                "OpenCode forwarder could not re-post usage after catch-up", exc_info=True
            )

    async def _replay_history_message(
        self, turn: _SessionTurn, message: Mapping[str, Any]
    ) -> None:
        """Post one history message's unseen user text, assistant text, and tools."""
        message_id = _str_field(message, "id")
        if message_id is None:
            return
        kind = message.get("type")
        if kind == "user":
            await self._post_user_payload(turn, message_id, message)
            return
        if kind != "assistant":
            return
        self._record_step_usage(message_id, message, _model_ref(message.get("model")))
        content = message.get("content")
        text_ordinal = 0
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text":
                text = item.get("text")
                key = self._key("text-final", message_id, str(text_ordinal))
                if isinstance(text, str) and text and self.state.mark(key):
                    await self._post_assistant_text(
                        turn,
                        text,
                        message_id=message_id,
                        stream_id=self._stream_id(message_id, "text", text_ordinal),
                    )
                text_ordinal += 1
            elif item.get("type") == "tool":
                await self._replay_tool(turn, message_id, item)

    async def _replay_tool(
        self, turn: _SessionTurn, message_id: str, item: Mapping[str, Any]
    ) -> None:
        """Post a history tool's call and (when settled) its output."""
        call_id = _str_field(item, "id")
        state = item.get("state")
        if call_id is None or not isinstance(state, Mapping):
            return
        status = state.get("status")
        if status == "streaming":
            return
        name = _str_field(item, "name") or "tool"
        turn.tool_names[call_id] = name
        raw_input = state.get("input")
        arguments = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        if self.state.mark(self._key("tool-call", call_id)):
            await self._post_tool_call(turn, call_id, name, arguments, message_id=message_id)
        if status == "completed" and self.state.mark(self._key("tool-out", call_id)):
            output = opencode_tool_content_text(state.get("content"))
            await self._post_tool_output(turn, call_id, output, message_id=message_id)
        elif status == "error" and self.state.mark(self._key("tool-out", call_id)):
            output = opencode_tool_content_text(state.get("content"), error=state.get("error"))
            await self._post_tool_output(turn, call_id, output, message_id=message_id)


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
    "session.execution.interrupted": OpenCodeNativeForwarder._on_execution_interrupted,
    "session.retry.scheduled": OpenCodeNativeForwarder._on_retry_scheduled,
    "session.compaction.started": OpenCodeNativeForwarder._on_compaction_started,
    "session.compaction.ended": OpenCodeNativeForwarder._on_compaction_ended,
    "session.compaction.failed": OpenCodeNativeForwarder._on_compaction_failed,
    "session.inbox.enqueued": OpenCodeNativeForwarder._on_inbox_enqueued,
    "session.inbox.delivered": OpenCodeNativeForwarder._on_inbox_delivered,
    "session.inbox.cancelled": OpenCodeNativeForwarder._on_inbox_cancelled,
    "permission.asked": OpenCodeNativeForwarder._on_permission_asked,
    "permission.replied": OpenCodeNativeForwarder._on_permission_replied,
    "form.created": OpenCodeNativeForwarder._on_form_created,
    "form.replied": OpenCodeNativeForwarder._on_form_resolved,
    "form.cancelled": OpenCodeNativeForwarder._on_form_resolved,
    "session.created": OpenCodeNativeForwarder._on_session_created,
}
