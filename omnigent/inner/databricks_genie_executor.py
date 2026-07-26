"""
``harness: databricks-genie`` executor.

Drives a remote Databricks **AI/BI Genie space** in Agent mode over the Genie
Responses API (``POST /api/2.0/genie/agents/{agent_id}/responses``). The
endpoint is OpenAI-shaped, so it is driven with the core ``openai`` client
pointed at the agent's base URL. Genie keeps the conversation server-side, so
each Omnigent turn posts exactly one user message and continues the
conversation via the id captured from the previous turn's ``response.created``
event.

The response's streamed output items become Omnigent events: Genie's planning
becomes :class:`~omnigent.inner.executor.ReasoningChunk`, each SQL query it runs
becomes a :class:`~omnigent.inner.executor.ToolCallRequest` /
:class:`~omnigent.inner.executor.ToolCallComplete` pair (Genie executes them
itself — Omnigent only observes), and the final report becomes
:class:`~omnigent.inner.executor.TextChunk` text. Query results are re-rendered
from the machine-readable ``columns`` / ``preview_rows`` metadata rather than
Genie's own Markdown, which Databricks documents as unstable across requests.

Auth reuses the Databricks CLI: ``databricks auth login`` writes OAuth/PAT
credentials into the CLI config file (path in ``docs/AGENT_YAML_SPEC.md``), and
:func:`~omnigent.inner.databricks_executor._resolve_databricks_auth` turns the
profile into an httpx auth hook that re-mints the bearer token on every
request, so a long turn survives OAuth access-token expiry. The Genie agent id
is the space id carried in ``executor.model`` (a Genie space is the
conversational unit, so it maps onto "model") and the profile in
``executor.auth`` / ``executor.profile``; the
:mod:`omnigent.inner.databricks_genie_harness` wrap passes both to this
executor.

Agent mode is a Databricks **Beta** API gated on a workspace preview toggle;
without it the endpoint answers 404 ``FEATURE_DISABLED``.

The client's SSE iterator is blocking, so it is consumed on a worker thread via
:func:`~omnigent.inner.executor.iterate_blocking_stream` to keep the event loop
responsive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolArgs,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    iterate_blocking_stream,
)

if TYPE_CHECKING:
    import httpx

_logger = logging.getLogger(__name__)

# Display guard: cap the number of result rows rendered inline so a large query
# result doesn't blow up the turn's response payload. Genie previews upstream
# too; this is a belt-and-braces bound on what we echo back.
_MAX_RESULT_ROWS = 50

# HTTP deadline for the streamed response. An Agent-mode turn plans, runs real
# warehouse queries, and writes a report, so this is generous while still
# bounding a wedged stream.
_DEFAULT_TIMEOUT_SECONDS = 900.0

# Responses API model slug for a Genie agent; which agent is selected by the
# space id in the base URL, not by this value.
_GENIE_AGENT_MODEL = "genie-agent"

# Name reported for the SQL calls Genie runs itself, when an item omits one.
_GENIE_TOOL_NAME = "execute_sql"

# ``output_text`` metadata status marking a result Genie could not fetch — its
# own text is then the only thing left to show.
_FETCH_FAILED_STATUS = "fetch_failed"

# ``response.completed`` is the only event that ends a turn successfully. A
# stream that stops without one — an idle proxy, a connection closed mid-report
# — carries a partial answer that must not be presented as a finished turn.
_TRUNCATED_STREAM_MESSAGE = (
    "databricks-genie: the response stream ended before Genie completed the turn; "
    "any text already streamed is partial."
)


class DatabricksGenieError(Exception):
    """Raised for databricks-genie setup failures.

    Carries an actionable, human-readable message (currently a missing
    ``databricks-sdk`` install plus its install hint).
    :meth:`DatabricksGenieExecutor.run_turn` converts it — and any other
    exception raised while talking to Genie — into an
    :class:`~omnigent.inner.executor.ExecutorError` turn event.
    """


def _extract_text(msg: Message) -> str:
    """Return the plain text of a single message dict.

    Handles both string ``content`` and a list of multimodal parts (joining the
    ``text`` of each ``{"type": "text", "text": ...}`` block), mirroring the
    peer executors' message handling.

    :param msg: An executor-facing message dict, e.g.
        ``{"role": "user", "content": "hi"}``.
    :returns: The message's text, or ``""`` when it carries none.
    """
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return str(content)


def _latest_user_text(messages: list[Message]) -> str:
    """Return the text of the most recent ``user`` message.

    Genie maintains its own conversation history server-side, so only the latest
    user turn is forwarded; prior turns are already known to the Genie space.

    :param messages: The turn's message history (oldest first).
    :returns: The latest user message's text, or ``""`` when there is none.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _extract_text(msg)
    return ""


def _md_cell(value: object) -> str:
    """Sanitize a value for one GitHub-Flavored-Markdown table cell.

    Collapses internal whitespace (so a multi-line review stays on a single
    table row instead of shattering the layout) and escapes pipes (so a value
    containing ``|`` doesn't open a spurious column).

    :param value: A raw cell value; ``None`` renders as an empty cell.
    :returns: A single-line, pipe-safe cell string.
    """
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).strip().replace("|", r"\|")


def _get(node: object, name: str) -> object:
    """Read *name* off one node of a streamed response.

    Genie's Beta fields are unknown to the OpenAI SDK's models, so a node is
    either a parsed model (attribute access) or the raw dict the SDK left
    unvalidated (key access).

    :param node: A stream event, output item, content part, or metadata blob.
    :param name: The field to read.
    :returns: The field's value, or ``None`` when the node carries none.
    """
    if isinstance(node, Mapping):
        return node.get(name)
    return getattr(node, name, None)


def _get_str(node: object, name: str) -> str:
    """Read *name* off *node*, or ``""`` when it is absent or not text."""
    value = _get(node, name)
    return value if isinstance(value, str) else ""


def _as_sequence(value: object) -> Sequence[object]:
    """Return *value* as a sequence of nodes, or empty when it is not a list."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _column_names(columns: object) -> list[str]:
    """Return the result table's labels from Genie's ``columns`` metadata."""
    return [
        col if isinstance(col, str) else _get_str(col, "name") for col in _as_sequence(columns)
    ]


def _row_cells(row: object, headers: Sequence[str]) -> list[str]:
    """Return one preview row's sanitized cells, ordered by *headers* when keyed."""
    if isinstance(row, Mapping):
        return [_md_cell(row.get(name)) for name in headers]
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        return [_md_cell(value) for value in row]
    return [_md_cell(row)]


def _render_table(metadata: object) -> str:
    """Render a result chunk's metadata as a GitHub-Flavored-Markdown table.

    Emits a ``| col | col |`` header, a ``| --- | --- |`` delimiter row, and one
    ``| ... |`` line per row — the form the web UI renders as an HTML table.
    Cells are sanitized via :func:`_md_cell`; rows beyond
    :data:`_MAX_RESULT_ROWS` are summarized rather than printed.

    :param metadata: The ``output_text`` part's ``metadata`` blob, carrying
        ``columns`` and ``preview_rows``.
    :returns: The Markdown table, or ``""`` when the metadata carries no rows.
    """
    rows = _as_sequence(_get(metadata, "preview_rows"))
    if not rows:
        return ""

    headers = _column_names(_get(metadata, "columns"))
    if not headers and isinstance(rows[0], Mapping):
        # Keyed rows with no column metadata still know their own labels;
        # without this they would render as a table of empty cells.
        headers = [str(key) for key in rows[0]]
    shown = [_row_cells(row, headers) for row in rows[:_MAX_RESULT_ROWS]]
    width = max(len(headers), max(len(cells) for cells in shown))
    if not width:
        return ""
    labels = [headers[i] if i < len(headers) else f"col{i + 1}" for i in range(width)]

    def _line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(cells[i] if i < len(cells) else "" for i in range(width)) + " |"

    table = [_line([_md_cell(label) for label in labels]), _line(["---"] * width)]
    table.extend(_line(cells) for cells in shown)

    omitted = len(rows) - len(shown)
    if omitted > 0:
        table.append(f"\n… ({omitted} more row{'s' if omitted != 1 else ''})")
    return "\n".join(table)


def _content_text(part: object) -> str:
    """Render one ``output_text`` content part of an assistant message.

    A query result arrives as model-written Markdown *plus* the stable
    ``columns`` / ``preview_rows`` metadata; re-rendering from the metadata
    keeps the row cap and pipe escaping ours. A part with no metadata — or one
    Genie marks :data:`_FETCH_FAILED_STATUS` — has only its own text to show.

    :param part: The content part.
    :returns: The text to emit for this part.
    """
    text = _get_str(part, "text")
    metadata = _get(part, "metadata")
    if metadata is None or _get_str(metadata, "status") == _FETCH_FAILED_STATUS:
        return text
    return _render_table(metadata) or text


def _block_separator(previous: str) -> str:
    """Return the newlines that open a fresh Markdown block after *previous*.

    Genie ships prose and result tables as separate chunks; a table glued onto
    the end of a sentence is not a table to any Markdown renderer. Callers put
    this separator *inside* the next chunk, so concatenating every emitted
    :class:`TextChunk` still reproduces ``TurnComplete.response`` exactly.

    :param previous: The text emitted immediately before, or ``""`` when this
        is the turn's first chunk.
    :returns: ``""``, ``"\\n"``, or ``"\\n\\n"``.
    """
    if not previous:
        return ""
    trailing = len(previous) - len(previous.rstrip("\n"))
    return "\n" * max(0, 2 - trailing)


def _message_texts(item: object) -> list[str]:
    """Return the rendered ``output_text`` parts of one assistant message item."""
    texts: list[str] = []
    for part in _as_sequence(_get(item, "content")):
        part_type = _get_str(part, "type")
        if part_type and part_type != "output_text":
            continue
        rendered = _content_text(part)
        if rendered:
            texts.append(rendered)
    return texts


def _reasoning_text(item: object) -> str:
    """Return a reasoning item's text, whichever field Genie carries it in.

    Each field may hold the text directly or a list of parts, so both shapes are
    read; anything else falls through to the next field.
    """
    for key in ("content", "summary"):
        value = _get(item, key)
        if isinstance(value, str):
            if value:
                return value
            continue
        parts = [
            part if isinstance(part, str) else _get_str(part, "text")
            for part in _as_sequence(value)
        ]
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined
    return _get_str(item, "text")


def _parse_arguments(raw: object) -> ToolArgs:
    """Parse a ``function_call.arguments`` payload.

    Databricks documents these keys as unstable, so anything that is not a JSON
    object is preserved under ``"raw"`` rather than reshaped into an assumed
    schema.

    :param raw: The item's ``arguments`` field.
    :returns: The call's arguments as a JSON-shaped dict.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        return {"raw": raw}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def _failure_message(event: object) -> str:
    """Describe a terminal ``response.failed`` / ``error`` stream event.

    :param event: The stream event; its error carries ``type``, ``code``, and
        ``message``, and the event itself is the fallback carrier.
    :returns: A message naming every field the event supplied.
    """
    error = _get(_get(event, "response"), "error") or _get(event, "error") or event
    labelled = [
        f"{label}={value}"
        for label, value in (("type", _get_str(error, "type")), ("code", _get_str(error, "code")))
        if value
    ]
    detail = f" [{', '.join(labelled)}]" if labelled else ""
    return (
        f"databricks-genie: Genie reported a failed response{detail}: "
        f"{_get_str(error, 'message') or error}"
    )


def _incomplete_message(event: object) -> str:
    """Describe a terminal ``response.incomplete`` stream event.

    :param event: The stream event; ``incomplete_details.reason`` names why
        Genie stopped early when it carries one.
    :returns: A message naming the reason Genie gave, when it gave one.
    """
    details = _get(_get(event, "response"), "incomplete_details") or _get(
        event, "incomplete_details"
    )
    reason = _get_str(details, "reason") or _get_str(details, "type")
    detail = f" (reason={reason})" if reason else ""
    return (
        f"databricks-genie: Genie ended the response before it was complete{detail}; "
        "any text already streamed is partial."
    )


@dataclass
class _CallPairing:
    """Correlates Genie's ``function_call`` items with their outputs.

    Downstream consumers pair a completion to its request strictly by
    ``call_id`` and drop uncorrelated ones, so ids are synthesized when Genie
    omits them, and an id-less output takes the oldest call still awaiting one.
    An output naming a call that is unknown — or already completed — is dropped
    rather than emitted against someone else's id.
    """

    _names: dict[str, str] = field(default_factory=dict)
    _awaiting: list[str] = field(default_factory=list)

    def request(self, item: object) -> ToolCallRequest:
        """Record a ``function_call`` item and return its observed-call event."""
        call_id = (
            _get_str(item, "call_id")
            or _get_str(item, "id")
            or f"genie_call_{len(self._names) + 1}"
        )
        name = _get_str(item, "name") or _GENIE_TOOL_NAME
        self._names[call_id] = name
        self._awaiting.append(call_id)
        return ToolCallRequest(
            name=name,
            args=_parse_arguments(_get(item, "arguments")),
            metadata={"call_id": call_id},
        )

    def complete(self, item: object) -> ToolCallComplete | None:
        """Return the completion event for a ``function_call_output`` item.

        :param item: The output item.
        :returns: The paired completion, or ``None`` when no call is awaiting
            this output — such an event would render as a result card with no
            call to attach to, or a second card on a call already finished.
        """
        call_id = _get_str(item, "call_id")
        if not call_id:
            # Genie streams outputs in call order, so an id-less one belongs to
            # the oldest call that has not been completed yet.
            call_id = self._awaiting[0] if self._awaiting else ""
        if call_id not in self._awaiting:
            _logger.debug(
                "databricks-genie: dropping function_call_output with no call awaiting it (%r)",
                call_id,
            )
            return None
        self._awaiting.remove(call_id)
        return ToolCallComplete(
            name=_get_str(item, "name") or self._names[call_id],
            result=_get(item, "output"),
            metadata={"call_id": call_id},
        )

    def cancel_awaiting(self) -> list[ToolCallComplete]:
        """Return a CANCELLED completion for every call still awaiting output.

        A turn that ends before Genie reports a query's result would otherwise
        leave that SQL card rendering as permanently in progress.
        """
        cancelled = [
            ToolCallComplete(
                name=self._names[call_id],
                status=ToolCallStatus.CANCELLED,
                metadata={"call_id": call_id},
            )
            for call_id in self._awaiting
        ]
        self._awaiting.clear()
        return cancelled


def _item_events(item: object, pairing: _CallPairing) -> Iterator[ExecutorEvent]:
    """Map one completed output item onto its executor events.

    :param item: The item carried by ``response.output_item.done``.
    :param pairing: The turn's call correlation state.
    :returns: The events this item produces, in order.
    """
    item_type = _get_str(item, "type")
    if item_type == "reasoning":
        reasoning = _reasoning_text(item)
        if reasoning:
            yield ReasoningChunk(delta=reasoning, event_type="reasoning_text")
    elif item_type == "function_call":
        yield pairing.request(item)
    elif item_type == "function_call_output":
        completion = pairing.complete(item)
        if completion is not None:
            yield completion
    elif item_type == "message":
        for chunk in _message_texts(item):
            yield TextChunk(text=chunk)


class DatabricksGenieExecutor(Executor):
    """Executor that drives a Databricks Genie space over the Genie Responses API.

    :param space_id: The Genie space id — the same value as its Agent-mode
        agent id — to converse with (from ``executor.model``). When unset,
        :meth:`run_turn` yields an :class:`ExecutorError` instructing the user
        to set it.
    :param profile: The Databricks profile from ``~/.databrickscfg`` used to
        authenticate. ``None`` lets the SDK use its own resolution order
        (``DATABRICKS_CONFIG_PROFILE`` env / ``DEFAULT`` section).
    :param client: An injected ``OpenAI`` client (tests pass a fake). When
        ``None``, one is built lazily on the first turn so a missing
        ``databricks-sdk`` install — or an expired login — surfaces as a turn
        error, not a boot crash.
    :param timeout_seconds: HTTP deadline for the streamed response.
    :param enable_viz: Whether to ask Genie to attach visualizations to its
        answer. Off by default, and the field is omitted from the request
        entirely when off.
    """

    def __init__(
        self,
        *,
        space_id: str | None,
        profile: str | None = None,
        client: object | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        enable_viz: bool = False,
    ) -> None:
        self._space_id = space_id
        self._profile = profile
        self._client = client
        # Set only for a client this executor built; an injected one belongs to
        # its caller, so :meth:`close` must not close it.
        self._http_client: httpx.Client | None = None
        self._timeout_seconds = timeout_seconds
        self._enable_viz = enable_viz
        # Captured from the first ``response.created``; later turns continue
        # that conversation instead of starting a new one.
        self._conversation_id: str | None = None

    def supports_streaming(self) -> bool:
        """Agent-mode responses stream reasoning, SQL, and report text over SSE."""
        return True

    def supports_tool_calling(self) -> bool:
        """Genie has no Omnigent-dispatched tools; it queries the space directly."""
        return False

    def handles_tools_internally(self) -> bool:
        """Genie runs its own SQL, so its calls are observations, not requests."""
        return True

    def _ensure_client(self, agent_id: str) -> object:
        """Blocking: return the Responses client, building one lazily on first use.

        Resolving credentials reads the Databricks CLI config file and mints an
        OAuth token over HTTP, so callers run this off the event loop.

        :param agent_id: The Genie agent (space) id the base URL targets.
        :returns: The (possibly newly constructed) ``OpenAI`` client.
        :raises DatabricksGenieError: When ``databricks-sdk`` is not installed.
        """
        if self._client is not None:
            return self._client

        import httpx
        from openai import OpenAI

        from omnigent.inner.databricks_executor import _resolve_databricks_auth
        from omnigent.inner.open_responses_sdk import _OPENAI_KEY_PLACEHOLDER

        try:
            auth, host = _resolve_databricks_auth(self._profile)
        except ImportError as exc:
            from omnigent.onboarding.databricks_config import DATABRICKS_EXTRA_INSTALL_HINT

            raise DatabricksGenieError(
                "the databricks-sdk package is required for the databricks-genie "
                "harness but is not installed; it ships in the omnigent[databricks] "
                f"extra. {DATABRICKS_EXTRA_INSTALL_HINT}"
            ) from exc

        http_client = httpx.Client(auth=auth)
        try:
            self._client = OpenAI(
                base_url=f"{host.rstrip('/')}/api/2.0/genie/agents/{agent_id}",
                # The bearer token rides on the httpx auth hook, which re-mints
                # it per request so a turn outliving the OAuth token still
                # works; the key is only the placeholder the SDK insists on.
                api_key=_OPENAI_KEY_PLACEHOLDER,
                http_client=http_client,
                timeout=self._timeout_seconds,
                # The SDK's default retry set includes 409, which here means "a
                # response is already being generated for this conversation" —
                # never retryable, so retrying only delays an actionable error.
                max_retries=0,
            )
        except Exception:
            # Nothing else holds this client yet, so it would leak its
            # connection pool if the constructor rejected our arguments.
            http_client.close()
            raise
        self._http_client = http_client
        return self._client

    async def close(self) -> None:
        """Close the httpx client backing the Responses client, if we built one.

        The adapter calls this at harness shutdown. A caller-injected client is
        left alone (it belongs to whoever passed it in), a second call is a
        no-op, and closing runs off the event loop because it is blocking.
        """
        http_client = self._http_client
        if http_client is None:
            return
        self._http_client = None
        self._client = None
        await asyncio.to_thread(http_client.close)

    def _create_stream(self, client: object, prompt: str) -> Iterator[object]:
        """Blocking: post the turn and return the response's SSE iterator.

        Genie keeps the conversation server-side, so ``input`` carries exactly
        one user message — continuity comes from ``conversation_id``, never from
        replayed history.

        :param client: The Responses client.
        :param prompt: The user's natural-language message.
        :returns: The stream of response events.
        """
        extra_body: dict[str, object] = {}
        if self._conversation_id:
            extra_body["conversation_id"] = self._conversation_id
        if self._enable_viz:
            extra_body["enable_viz"] = True

        kwargs: dict[str, object] = {
            "model": _GENIE_AGENT_MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            "stream": True,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        stream = client.responses.create(**kwargs)  # type: ignore[attr-defined]  # duck-typed client
        return cast("Iterator[object]", stream)

    def _capture_conversation_id(self, event: object) -> None:
        """Remember the conversation Genie opened, for the next turn's request."""
        conversation_id = _get_str(_get(event, "response"), "conversation_id") or _get_str(
            event, "conversation_id"
        )
        if conversation_id:
            self._conversation_id = conversation_id

    def _request_failure(self, exc: Exception) -> str:
        """Turn a failure from the Genie endpoint into an actionable message.

        Agent mode answers 404 ``FEATURE_DISABLED`` until the workspace enables
        the Beta preview, and 409 ``RESOURCE_CONFLICT`` while another turn of
        the same conversation is still running. Both are user-fixable, unlike a
        generic transport error.

        :param exc: The exception raised while opening or reading the stream.
        :returns: The message for the turn's :class:`ExecutorError`.
        """
        if isinstance(exc, DatabricksGenieError):
            # Already a self-describing setup message; don't bury it in a prefix.
            return str(exc)
        status = getattr(exc, "status_code", None)
        detail = str(exc)
        if status == 404 and "FEATURE_DISABLED" in detail:
            return (
                "databricks-genie: the Genie Agent-mode APIs are Beta and are not enabled "
                "for this workspace; ask a workspace admin to turn on the Genie agent "
                f"preview, then retry. ({detail})"
            )
        if status == 409:
            named = f" {self._conversation_id}" if self._conversation_id else ""
            return (
                f"databricks-genie: the Genie conversation{named} already has a turn in "
                f"progress; wait for it to finish before sending another message. ({detail})"
            )
        return f"databricks-genie request failed: {exc}"

    async def _stream_events(self, stream: Iterator[object]) -> AsyncIterator[ExecutorEvent]:
        """Map one response's streamed items onto executor events.

        Ends with either a :class:`TurnComplete` carrying every text chunk
        concatenated — downstream policy prefers it over the streamed deltas —
        or a single :class:`ExecutorError`, never both. Only
        ``response.completed`` ends a turn successfully: a stream that stops
        without it delivered a partial answer, which must not be reported as a
        finished one. Any SQL call still awaiting its output is cancelled first,
        so the turn leaves no card rendering as in progress.

        :param stream: The response's blocking SSE iterator.
        """
        text: list[str] = []
        pairing = _CallPairing()
        completed = False
        error: str | None = None
        try:
            async for event in iterate_blocking_stream(stream):
                event_type = _get_str(event, "type")
                if event_type == "response.created":
                    self._capture_conversation_id(event)
                elif event_type == "response.completed":
                    # Read the id here too: if Genie ever omits it from
                    # ``response.created``, the next turn would silently open a
                    # new conversation instead of continuing this one.
                    self._capture_conversation_id(event)
                    completed = True
                    break
                elif event_type in ("response.failed", "error"):
                    error = _failure_message(event)
                    break
                elif event_type == "response.incomplete":
                    error = _incomplete_message(event)
                    break
                elif event_type == "response.output_item.done":
                    for item_event in _item_events(_get(event, "item"), pairing):
                        if not isinstance(item_event, TextChunk):
                            yield item_event
                            continue
                        chunk = _block_separator(text[-1] if text else "") + item_event.text
                        text.append(chunk)
                        yield TextChunk(text=chunk)
        except Exception as exc:  # noqa: BLE001 — stream boundary surfaces any error as a turn error
            error = self._request_failure(exc)

        if error is None and not completed:
            error = _TRUNCATED_STREAM_MESSAGE
        if error is not None:
            for cancelled in pairing.cancel_awaiting():
                yield cancelled
            yield ExecutorError(message=error)
            return

        yield TurnComplete(response="".join(text))

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 — Genie runs its own SQL; no Omnigent tools
        system_prompt: str,  # noqa: ARG002 — the Genie space carries its own instructions
        config: ExecutorConfig | None = None,  # noqa: ARG002 — space id comes from __init__
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one Genie turn and yield its events.

        Yields a single :class:`ExecutorError` — and nothing else — when no
        space id is configured, there is no user message, or the turn fails
        before the stream opens (credential resolution or the opening POST).

        Once the stream is open, yields the response's reasoning, observed SQL
        calls, and report text as they arrive, followed by either a
        :class:`TurnComplete` carrying the full answer or — when the stream
        fails or ends without ``response.completed`` — a cancellation for each
        in-flight SQL call and a final :class:`ExecutorError`. A mid-stream
        failure therefore does not suppress the events already emitted, so
        callers must not treat an :class:`ExecutorError` as "nothing happened".
        """
        space_id = self._space_id
        if not space_id:
            yield ExecutorError(
                message=(
                    "no Genie space id configured for the databricks-genie harness; "
                    "set executor.model to the Genie space id in the agent spec."
                )
            )
            return

        prompt = _latest_user_text(messages)
        if not prompt.strip():
            yield ExecutorError(message="databricks-genie: no user message to send to Genie.")
            return

        try:
            # Both steps block: building the client resolves credentials (file
            # I/O plus an OAuth token mint) and creating the stream posts the
            # turn, so neither may run on the event loop.
            client = await asyncio.to_thread(self._ensure_client, space_id)
            stream = await asyncio.to_thread(self._create_stream, client, prompt)
        except Exception as exc:  # noqa: BLE001 — any setup/auth/HTTP error becomes a turn error
            yield ExecutorError(message=self._request_failure(exc))
            return

        async for event in self._stream_events(stream):
            yield event
