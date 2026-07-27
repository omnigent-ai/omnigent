from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)


class OmnigentError(RuntimeError):
    """Base error for the Omnigent client and its event parsing."""


@dataclass(frozen=True, slots=True)
class ElicitationOption:
    """One selectable choice in an ``AskUserQuestion`` form question."""

    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ElicitationQuestion:
    """One question in an ``AskUserQuestion`` form elicitation.

    ``key`` is what the answer map is keyed by when resolving — the server's
    question ``id`` if present, else the question text (matches the web form).
    """

    key: str
    question: str
    options: list[ElicitationOption]
    multi_select: bool = False


@dataclass(frozen=True, slots=True)
class ElicitationRequest:
    """A server-initiated request parsed off the event stream.

    The Omnigent server parks a running turn when a tool call trips an approval
    policy OR the agent asks the user to choose (``AskUserQuestion``), emitting
    ``response.elicitation_request``. Two shapes the bot renders differently:

    - **binary** (``questions`` empty): a yes/no approval → Approve / Deny card.
    - **form** (``questions`` non-empty): a multiple-choice ask → one option
      button per choice; the click resolves with the chosen label as ``content``.
    """

    elicitation_id: str
    message: str
    # Session that owns the resolve endpoint. Usually the streaming session,
    # but a mirrored sub-agent prompt carries its own ``target_session_id``.
    session_id: str
    policy_name: str | None = None
    content_preview: str | None = None
    # MCP elicitation mode: "form" (inline) or "url" (out-of-band page).
    mode: str = "form"
    # Non-empty for a form-mode ``AskUserQuestion`` elicitation.
    questions: list[ElicitationQuestion] = field(default_factory=list)
    # True when the elicitation asks for typed/structured input we can't collect
    # with Slack buttons (a non-empty requestedSchema that isn't AskUserQuestion).
    needs_typed_input: bool = False

    @property
    def is_form(self) -> bool:
        return bool(self.questions)

    @property
    def is_supported(self) -> bool:
        """Whether the bot can render this elicitation natively in Slack.

        Classified by the *decision shape*, NOT the delivery ``mode``. A
        ``url``-mode elicitation just carries a suggested out-of-band approve
        page; the verdict can still be posted to the resolve endpoint, so a
        ``url``-mode binary approval or ``AskUserQuestion`` renders natively
        (Approve/Deny card, or option buttons) exactly like a ``form``-mode one.
        Only a request for free-form typed input we can't collect with buttons
        (a non-empty ``requestedSchema`` that isn't an ``AskUserQuestion``) is
        unsupported — that's surfaced with a link to resolve in the web UI.
        """
        if self.is_form:
            return True
        return not self.needs_typed_input


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            event = _decode_sse_event(event_name, data_lines)
            event_name = None
            data_lines = []
            if event is None:
                continue
            if event == "[DONE]":
                break
            if isinstance(event, str):
                continue
            yield event
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    event = _decode_sse_event(event_name, data_lines)
    if isinstance(event, dict):
        yield event


def session_status(event: dict[str, Any]) -> tuple[str, str | None] | None:
    """Parse a ``session.status`` event into ``(status, response_id)``.

    Returns ``None`` for any other event. ``response_id`` is ``None`` when the
    field is absent — critically, this distinguishes the AUTHORITATIVE turn edge
    (the Stop hook stamps the turn's ``response_id`` on the terminal
    ``idle``/``waiting``/``failed``) from the PTY-activity watcher's mid-answer
    flaps (bare ``idle`` with NO ``response_id``, emitted on sub-second pane
    lulls while the agent is still generating). The turn-end rule that consumes
    this lives in ``OmnigentClient._run_turn_once``.
    """
    if event.get("type") != "session.status":
        return None
    status = event.get("status")
    if not isinstance(status, str):
        return None
    response_id = event.get("response_id")
    return status, response_id if isinstance(response_id, str) and response_id else None


def extract_elicitation_resolved(event: dict[str, Any]) -> str | None:
    """Return the ``elicitation_id`` of a ``response.elicitation_resolved`` event.

    The server pushes this when an elicitation is resolved — by our own Slack
    verdict, or externally (web UI / another client). The turn loop keeps reading
    the stream while a card is shown, so it observes resolution as a normal push
    event (the web UI's model) rather than polling ``pending_elicitations``.
    """
    if event.get("type") != "response.elicitation_resolved":
        return None
    eid = event.get("elicitation_id")
    return eid if isinstance(eid, str) and eid else None


def is_hard_terminal_event(event: dict[str, Any]) -> bool:
    """True for an explicit turn/response failure or cancellation.

    These end the turn regardless of ``response_id`` tracking — a fallback for a
    session that fails without a clean id-matched ``session.status`` edge.
    """
    return event.get("type") in {
        "response.failed",
        "response.cancelled",
        "turn.failed",
        "turn.cancelled",
    }


def extract_delta(event: dict[str, Any]) -> str | None:
    if event.get("type") != "response.output_text.delta":
        return None
    delta = event.get("delta")
    return delta if isinstance(delta, str) else None


def extract_elicitation_request(
    event: dict[str, Any], stream_session_id: str
) -> ElicitationRequest | None:
    """Parse a ``response.elicitation_request`` event into an approval request.

    ``stream_session_id`` is the session whose stream this event arrived on; it
    is the resolve target unless the event names a ``target_session_id`` (a
    sub-agent prompt mirrored into an ancestor stream).
    """
    if event.get("type") != "response.elicitation_request":
        return None
    elicitation_id = event.get("elicitation_id")
    if not isinstance(elicitation_id, str) or not elicitation_id:
        return None
    params = event.get("params")
    params = params if isinstance(params, dict) else {}
    target = params.get("target_session_id")
    message = params.get("message")
    policy_name = params.get("policy_name")
    content_preview = params.get("content_preview")
    mode = params.get("mode")
    questions = _parse_ask_user_question(params.get("ask_user_question"))
    # A non-empty requestedSchema means the server wants typed/structured input.
    # AskUserQuestion (parsed into `questions`) is the one such shape we render;
    # anything else with a schema we can't collect via buttons.
    schema = params.get("requestedSchema")
    needs_typed_input = bool(isinstance(schema, dict) and schema) and not questions
    return ElicitationRequest(
        elicitation_id=elicitation_id,
        message=message if isinstance(message, str) and message else "Approve this action?",
        session_id=target if isinstance(target, str) and target else stream_session_id,
        policy_name=policy_name if isinstance(policy_name, str) else None,
        content_preview=content_preview if isinstance(content_preview, str) else None,
        mode=mode if isinstance(mode, str) and mode else "form",
        questions=questions,
        needs_typed_input=needs_typed_input,
    )


def _parse_ask_user_question(raw: Any) -> list[ElicitationQuestion]:
    """Parse the ``ask_user_question`` params extra into typed questions.

    The server stamps this on a form-mode elicitation (Claude Code's built-in
    ``AskUserQuestion`` tool, and the agy/codex equivalents). Each answer is
    keyed by the question ``id`` when present, else its text — matching the web
    form so selections round-trip to the agent identically. Malformed or empty
    payloads yield an empty list (the elicitation renders as binary approve/deny).
    """
    if not isinstance(raw, dict):
        return []
    questions_raw = raw.get("questions")
    if not isinstance(questions_raw, list):
        return []
    questions: list[ElicitationQuestion] = []
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        text = entry.get("question")
        if not isinstance(text, str) or not text:
            continue
        options: list[ElicitationOption] = []
        for opt in entry.get("options") or []:
            if not isinstance(opt, dict):
                continue
            label = opt.get("label")
            if not isinstance(label, str) or not label:
                continue
            description = opt.get("description")
            desc = description if isinstance(description, str) and description else None
            options.append(ElicitationOption(label=label, description=desc))
        if not options:
            continue
        qid = entry.get("id")
        key = qid if isinstance(qid, str) and qid else text
        questions.append(
            ElicitationQuestion(
                key=key,
                question=text,
                options=options,
                multi_select=entry.get("multiSelect") is True,
            )
        )
    return questions


def extract_policy_denied(event: dict[str, Any]) -> str | None:
    """Return the deny reason for a ``response.policy_denied`` event.

    The DENY counterpart to an elicitation ASK: a native harness tool call was
    hard-blocked by policy with no approval offered. Observational — there's
    nothing to respond to; the bot just surfaces why the action didn't happen.
    """
    if event.get("type") != "response.policy_denied":
        return None
    reason = event.get("reason")
    return reason if isinstance(reason, str) and reason else "Blocked by policy."


@dataclass(frozen=True, slots=True)
class OutputFile:
    """A file artifact the agent produced during the turn."""

    file_id: str
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class SessionActivity:
    """The server's view of whether a session is busy right now.

    ``status`` is the rolled-up session status (``running``/``waiting`` = busy,
    ``idle``/``failed`` = free, ``None`` = snapshot unreadable). ``pending_elicitation``
    is ``True`` when the session is parked awaiting a decision. Mirrors the web
    UI's send-gating: these are the two states where a new prompt should wait.
    """

    status: str | None
    pending_elicitation: bool

    @property
    def is_busy(self) -> bool:
        # Matches the web UI's computeIsWorking: the server is actively working.
        return self.status in ("running", "waiting", "launching")

    @property
    def needs_user_action(self) -> bool:
        return self.pending_elicitation


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Server-authoritative session config, for the first-message summary.

    ``harness`` is the runtime the session runs on (e.g. ``claude-native``);
    ``agent_name`` is the configured agent (e.g. ``debby``). Either may be
    ``None`` if the snapshot is unreadable or omits the field.
    """

    harness: str | None
    agent_name: str | None


def extract_output_file(event: dict[str, Any]) -> OutputFile | None:
    """Parse a ``response.output_file.done`` event into a file artifact."""
    if event.get("type") != "response.output_file.done":
        return None
    file_id = event.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    filename = event.get("filename")
    return OutputFile(
        file_id=file_id,
        filename=filename if isinstance(filename, str) and filename else None,
    )


def extract_todos(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return the current todo list for a ``session.todos`` event.

    Each entry carries ``content`` (str), ``status`` (``pending`` /
    ``in_progress`` / ``completed``) and ``activeForm`` (str) keys. Returns
    ``None`` for non-todo events; an empty list is a real "no todos" update.
    """
    if event.get("type") != "session.todos":
        return None
    todos = event.get("todos")
    if not isinstance(todos, list):
        return None
    return [item for item in todos if isinstance(item, dict)]


def extract_error_text(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type"))
    if event_type == "response.error":
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        message = event.get("message")
        if isinstance(message, str):
            return message
    if event_type in {"response.failed", "turn.failed"}:
        response = event.get("response")
        if isinstance(response, dict):
            last_error = response.get("error") or response.get("last_error")
            if isinstance(last_error, dict):
                message = last_error.get("message")
                if isinstance(message, str):
                    return message
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        if isinstance(error, str):
            return error
    return None


def extract_assistant_text(event_or_item: dict[str, Any]) -> str | None:
    if event_or_item.get("type") == "response.output_item.done":
        item = event_or_item.get("item")
        return extract_assistant_text(item) if isinstance(item, dict) else None

    item_type = event_or_item.get("type")
    if item_type != "message":
        return None

    data = event_or_item.get("data")
    message = data if isinstance(data, dict) else event_or_item
    if message.get("role") != "assistant":
        return None

    content = message.get("content")
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip() or None


def _decode_sse_event(
    event_name: str | None, data_lines: list[str]
) -> dict[str, Any] | str | None:
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return data
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        # Skip a single malformed frame (e.g. a proxy injecting a partial chunk)
        # rather than failing the whole turn — a good answer may have already
        # streamed. Log for operators; keep the raw payload out of any user-facing
        # text. The stream's own turn-end / idle detection still terminates it.
        _logger.warning("Skipping malformed SSE JSON payload (%d chars)", len(data))
        return None
    if not isinstance(payload, dict):
        return None
    if event_name and "type" not in payload:
        payload["type"] = event_name
    return payload


def _first_str(payload: Any, keys: tuple[str, ...], *, nested: tuple[str, ...] = ()) -> str | None:
    """First string value found at ``keys`` on ``payload``, recursing into
    ``nested`` keys. Used to pull ids out of variously-nested API responses.
    """
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    for key in nested:
        value = _first_str(payload.get(key), keys, nested=nested)
        if value:
            return value
    return None


def _extract_session_id(payload: Any) -> str | None:
    return _first_str(payload, ("id", "session_id", "conversation_id"), nested=("session", "data"))


def _extract_runner_id(payload: Any) -> str | None:
    return _first_str(payload, ("id", "runner_id"), nested=("runner", "data"))


def host_id_of(host: dict[str, Any]) -> str | None:
    """Return a host's id from either the ``id`` or ``host_id`` key, or None.

    Shared by the client (host selection) and setup (host menu / workspace
    default) so the "which key holds the id" knowledge lives in one place.
    """
    return _first_str(host, ("id", "host_id"))


def _extract_list(payload: Any, key: str) -> list[Any] | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, list) else None


def _is_host_online(host: dict[str, Any]) -> bool:
    if host.get("online") is True or host.get("host_online") is True:
        return True
    status = host.get("status")
    return isinstance(status, str) and status.lower() == "online"
