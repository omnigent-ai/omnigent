"""The ``omnigent.transcript/1`` portable session transcript.

A transcript is newline-delimited JSON: the first line is a
:class:`TranscriptHeader` naming the schema, every later line is one
:class:`TranscriptEntry`. The entry vocabulary is deliberately small and
harness-neutral (``message``, ``reasoning``, ``tool_call``, ``tool_result``,
``error``, ``compaction``, ``note``) so a reviewer can read the file without
an Omnigent server, and so a reader never has to grow a case per harness.

Two properties the format guarantees:

* **Sealed content is explicit.** When a provider returned something the
  client could not read (encrypted reasoning, a provider-hosted tool
  result), the entry says so instead of silently looking complete.
* **Compaction is a boundary, not a gap.** The entries before a compaction
  stay in the file; the ``compaction`` entry carries the summary the model
  continued from and names the last entry it covers.

Both the server route (``GET /v1/sessions/{id}/export``) and the CLI
(``omnigent session export``) build the file through :func:`iter_transcript_lines`
from the flat API item shape (:meth:`ConversationItem.to_api_dict`), and
``omnigent session import`` reads it back through :func:`read_transcript` /
:func:`item_from_entry`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Schema identifier written on the header line and required on read.
TRANSCRIPT_SCHEMA = "omnigent.transcript/1"

#: Schema versions :func:`read_transcript` accepts.
SUPPORTED_SCHEMAS: frozenset[str] = frozenset({TRANSCRIPT_SCHEMA})

EntryKind = Literal[
    "message",
    "reasoning",
    "tool_call",
    "tool_result",
    "error",
    "compaction",
    "note",
]

EntryRole = Literal["user", "assistant", "tool", "system"]

#: Session fields carried under ``header.settings`` so a re-import can
#: restore the same per-session configuration.
_SETTING_KEYS: tuple[str, ...] = (
    "harness_override",
    "model_override",
    "reasoning_effort",
    "cost_control_mode_override",
    "terminal_launch_args",
)

#: Envelope keys the API puts on every item; everything else is payload.
_ITEM_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"id", "type", "status", "response_id", "created_at", "created_by"}
)

_SEALED_REASONING_ENCRYPTED = "reasoning returned encrypted; only the summary is readable"
_SEALED_REASONING_WITHHELD = "reasoning content was not returned by the provider"
_SEALED_PROVIDER_TOOL = "executed inside the provider; only what it echoed back is visible"


class TranscriptSchemaError(ValueError):
    """The input is not a transcript this reader understands."""


class TranscriptHeader(BaseModel):
    """First line of a transcript: what session this is and how it ran.

    :param schema_: The schema identifier, serialized as ``schema``.
    :param session: The exporting server's session id, e.g. ``"conv_abc123"``.
    :param created: When the session was created, ISO 8601 UTC.
    :param exported: When this file was written, ISO 8601 UTC.
    :param title: The session title, if any.
    :param agent: The bound agent's display name, if known.
    :param agent_id: The bound agent's id on the exporting server.
    :param harness: Canonical harness the session ran on, e.g. ``"codex"``.
    :param model: The model the session last reported running on.
    :param workspace: Absolute workspace path on the runner, if any.
    :param parent_session: Parent session id for a sub-agent session.
    :param root_session: Root of the spawn tree this session belongs to.
    :param settings: Per-session overrides worth restoring on import
        (``model_override``, ``reasoning_effort``, ...). Only set keys appear.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(alias="schema", default=TRANSCRIPT_SCHEMA)
    session: str
    created: str | None = None
    exported: str | None = None
    title: str | None = None
    agent: str | None = None
    agent_id: str | None = None
    harness: str | None = None
    model: str | None = None
    workspace: str | None = None
    parent_session: str | None = None
    root_session: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)

    def to_line(self) -> str:
        """Serialize as one JSON line (unset optionals omitted)."""
        return json.dumps(self.model_dump(by_alias=True, exclude_none=True))


class TranscriptEntry(BaseModel):
    """One line of a transcript after the header.

    Fields past ``kind`` are present only when they apply to that kind;
    ``None`` fields are omitted from the serialized line.

    :param turn: 1-based turn number; one turn per user request and the
        agent's response to it.
    :param seq: 1-based position of the entry in the file.
    :param time: When the item was recorded, ISO 8601 UTC.
    :param role: Who produced it.
    :param kind: The neutral entry kind.
    :param id: The exporting server's item id.
    :param origin_type: The producer's own item type (``"message"``,
        ``"native_tool"``, ...). Readers may ignore it; a re-import uses it
        to rebuild the exact item.
    :param text: Readable text for messages, reasoning summaries, errors and
        compaction summaries.
    :param content: Raw message content blocks (the harness's own shape).
    :param agent: Agent name that produced an assistant-side entry.
    :param tool: Tool name for ``tool_call`` / ``tool_result``.
    :param tool_input: Parsed tool arguments as raw JSON.
    :param tool_input_raw: The argument string when it was not valid JSON.
    :param tool_output: The tool's result text.
    :param call_id: Correlates a ``tool_call`` with its ``tool_result``.
    :param namespace: Tool namespace the call was emitted under.
    :param code: Stable error classifier for ``error`` entries.
    :param source: Error source (``"llm"``, ``"execution"``, ...).
    :param level: Error rendering level.
    :param covers_through: For ``compaction``, the id of the last entry the
        summary replaces in the model's context.
    :param model: Model that produced a compaction summary.
    :param token_count: Approximate size of a compaction summary.
    :param sealed: ``True`` when the provider withheld content from the
        client; what the model saw is not fully in this file.
    :param sealed_reason: Why the entry is sealed.
    :param note_type: For ``note`` entries, the producer's event type.
    :param data: For ``note`` entries, the raw payload.
    :param meta: Durable context injected as a message rather than typed
        by a person (``is_meta``).
    :param interrupted: The assistant message was cut off by a stop.
    """

    model_config = ConfigDict(extra="ignore")

    turn: int
    seq: int
    time: str | None = None
    role: EntryRole
    kind: EntryKind
    id: str | None = None
    origin_type: str | None = None
    text: str | None = None
    content: list[dict[str, Any]] | None = None
    agent: str | None = None
    tool: str | None = None
    tool_input: Any | None = None
    tool_input_raw: str | None = None
    tool_output: str | None = None
    call_id: str | None = None
    namespace: str | None = None
    code: str | None = None
    source: str | None = None
    level: str | None = None
    covers_through: str | None = None
    model: str | None = None
    token_count: int | None = None
    sealed: bool = False
    sealed_reason: str | None = None
    note_type: str | None = None
    data: dict[str, Any] | None = None
    meta: bool = False
    interrupted: bool = False

    def to_line(self) -> str:
        """Serialize as one JSON line (unset optionals and false flags omitted)."""
        payload = self.model_dump(exclude_none=True)
        for flag in ("sealed", "meta", "interrupted"):
            if payload.get(flag) is False:
                del payload[flag]
        return json.dumps(payload)


@dataclass(frozen=True)
class Transcript:
    """A parsed transcript: its header and entries in file order."""

    header: TranscriptHeader
    entries: tuple[TranscriptEntry, ...]


def _iso(ts: Any) -> str | None:
    """Render a unix-epoch value as ISO 8601 UTC, or ``None`` when absent."""
    if isinstance(ts, bool) or not isinstance(ts, int | float):
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _blocks_text(blocks: Any) -> str | None:
    """Join the ``text`` of content blocks; ``None`` when there is none."""
    if not isinstance(blocks, list):
        return None
    parts = [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(parts) if parts else None


def header_from_session(
    session: Mapping[str, Any], *, exported_at: datetime | None = None
) -> TranscriptHeader:
    """Build the header from a ``GET /v1/sessions/{id}`` response dict.

    :param session: The session snapshot, as returned by the API.
    :param exported_at: Export time; defaults to now.
    :returns: The populated header.
    """
    settings = {k: session[k] for k in _SETTING_KEYS if session.get(k) is not None}
    stamp = exported_at or datetime.now(UTC)
    return TranscriptHeader(
        session=str(session["id"]),
        created=_iso(session.get("created_at")),
        exported=stamp.isoformat().replace("+00:00", "Z"),
        title=session.get("title"),
        agent=session.get("agent_name"),
        agent_id=session.get("agent_id"),
        harness=session.get("harness"),
        model=session.get("llm_model"),
        workspace=session.get("workspace"),
        parent_session=session.get("parent_session_id"),
        root_session=session.get("root_conversation_id"),
        settings=settings,
    )


def _parse_tool_input(arguments: Any) -> tuple[Any, str | None]:
    """Split a tool-arguments value into ``(tool_input, tool_input_raw)``."""
    if not isinstance(arguments, str):
        return arguments, None
    try:
        return json.loads(arguments), None
    except ValueError:
        return None, arguments


def entry_from_item(item: Mapping[str, Any], *, turn: int, seq: int) -> TranscriptEntry:
    """Map one flat API item onto a transcript entry.

    :param item: The item as rendered by :meth:`ConversationItem.to_api_dict`.
    :param turn: Turn number assigned by the caller.
    :param seq: File position assigned by the caller.
    :returns: The entry; unknown item types become ``note`` entries that
        carry the raw payload so nothing is dropped.
    """
    item_type = str(item.get("type") or "unknown")
    payload = {k: v for k, v in item.items() if k not in _ITEM_ENVELOPE_KEYS}
    base: dict[str, Any] = {
        "turn": turn,
        "seq": seq,
        "time": _iso(item.get("created_at")),
        "id": item.get("id"),
        "origin_type": item_type,
    }

    if item_type == "message":
        role = payload.get("role")
        return TranscriptEntry(
            **base,
            role="assistant" if role == "assistant" else "user",
            kind="message",
            text=_blocks_text(payload.get("content")),
            content=payload.get("content"),
            agent=payload.get("model"),
            meta=bool(payload.get("is_meta")),
            interrupted=bool(payload.get("interrupted")),
        )
    if item_type == "function_call":
        tool_input, raw = _parse_tool_input(payload.get("arguments"))
        return TranscriptEntry(
            **base,
            role="assistant",
            kind="tool_call",
            tool=payload.get("name"),
            tool_input=tool_input,
            tool_input_raw=raw,
            call_id=payload.get("call_id"),
            namespace=payload.get("namespace"),
            agent=payload.get("model"),
        )
    if item_type == "function_call_output":
        return TranscriptEntry(
            **base,
            role="tool",
            kind="tool_result",
            call_id=payload.get("call_id"),
            tool_output=payload.get("output"),
        )
    if item_type == "reasoning":
        content = payload.get("content")
        readable = isinstance(content, list) and len(content) > 0
        sealed_reason: str | None = None
        if not readable:
            sealed_reason = (
                _SEALED_REASONING_ENCRYPTED
                if payload.get("encrypted_content")
                else _SEALED_REASONING_WITHHELD
            )
        return TranscriptEntry(
            **base,
            role="assistant",
            kind="reasoning",
            text=_blocks_text(payload.get("summary")),
            content=content if readable else None,
            agent=payload.get("model"),
            sealed=not readable,
            sealed_reason=sealed_reason,
        )
    if item_type == "error":
        return TranscriptEntry(
            **base,
            role="system",
            kind="error",
            text=payload.get("message"),
            code=payload.get("code"),
            source=payload.get("source"),
            level=payload.get("level"),
        )
    if item_type == "compaction":
        return TranscriptEntry(
            **base,
            role="system",
            kind="compaction",
            text=payload.get("summary"),
            covers_through=payload.get("last_item_id"),
            model=payload.get("model"),
            token_count=payload.get("token_count"),
        )
    if item_type == "native_tool":
        native = payload.get("item")
        tool = native.get("type") if isinstance(native, dict) else None
        return TranscriptEntry(
            **base,
            role="assistant",
            kind="tool_call",
            tool=tool or "native_tool",
            tool_input=native,
            call_id=native.get("id") if isinstance(native, dict) else None,
            sealed=True,
            sealed_reason=_SEALED_PROVIDER_TOOL,
        )
    if item_type == "terminal_command":
        if payload.get("kind") == "input":
            return TranscriptEntry(
                **base,
                role="user",
                kind="tool_call",
                tool="terminal",
                tool_input={"command": payload.get("input")},
            )
        stdout = payload.get("stdout") or ""
        stderr = payload.get("stderr") or ""
        return TranscriptEntry(
            **base,
            role="tool",
            kind="tool_result",
            tool="terminal",
            tool_output=stdout if not stderr else f"{stdout}{stderr}",
            data={"stdout": stdout, "stderr": stderr} if stderr else None,
        )
    if item_type == "slash_command":
        args = payload.get("arguments") or ""
        name = payload.get("name") or ""
        return TranscriptEntry(
            **base,
            role="user",
            kind="note",
            note_type=item_type,
            text=f"/{name} {args}".rstrip(),
            data=payload,
        )
    # routing_decision, resource_event, and anything added later.
    text = payload.get("rationale") if item_type == "routing_decision" else None
    return TranscriptEntry(
        **base,
        role="system",
        kind="note",
        note_type=item_type,
        text=text,
        data=payload,
    )


class EntryNumbering:
    """Assign ``turn`` / ``seq`` to items fed in order, one page at a time.

    A new turn starts whenever the ``response_id`` changes, so a streaming
    producer can number across pages without holding the whole session.
    """

    def __init__(self) -> None:
        self._turn = 0
        self._seq = 0
        self._response_id: str | None = None

    def entry(self, item: Mapping[str, Any]) -> TranscriptEntry:
        """Number the next item and map it to an entry."""
        response_id = item.get("response_id")
        if self._turn == 0 or response_id != self._response_id:
            self._turn += 1
            self._response_id = response_id
        self._seq += 1
        return entry_from_item(item, turn=self._turn, seq=self._seq)


def iter_transcript_entries(items: Iterable[Mapping[str, Any]]) -> Iterator[TranscriptEntry]:
    """Number items into entries, starting a new turn at each ``response_id``.

    :param items: Flat API items in chronological order.
    :returns: Entries with ``turn`` and ``seq`` assigned.
    """
    numbering = EntryNumbering()
    for item in items:
        yield numbering.entry(item)


def iter_transcript_lines(
    session: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
    *,
    exported_at: datetime | None = None,
) -> Iterator[str]:
    """Yield the transcript one newline-terminated JSON line at a time.

    :param session: The session snapshot dict (see :func:`header_from_session`).
    :param items: Flat API items in chronological order.
    :param exported_at: Export time; defaults to now.
    :returns: The header line, then one line per item.
    """
    yield header_from_session(session, exported_at=exported_at).to_line() + "\n"
    for entry in iter_transcript_entries(items):
        yield entry.to_line() + "\n"


def read_transcript(lines: Iterable[str]) -> Transcript:
    """Parse a transcript, refusing anything that is not a supported schema.

    :param lines: The file's lines (a file object works).
    :returns: The parsed header and entries.
    :raises TranscriptSchemaError: If the header is missing, names an
        unsupported schema, or a line is not a JSON object.
    """
    header: TranscriptHeader | None = None
    entries: list[TranscriptEntry] = []
    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError as exc:
            raise TranscriptSchemaError(f"line {line_no}: not valid JSON ({exc})") from exc
        if not isinstance(record, dict):
            raise TranscriptSchemaError(f"line {line_no}: expected a JSON object")
        if header is None:
            schema = record.get("schema")
            if schema is None:
                raise TranscriptSchemaError(
                    "line 1: missing 'schema' — not an omnigent transcript"
                )
            if schema not in SUPPORTED_SCHEMAS:
                raise TranscriptSchemaError(
                    f"unsupported transcript schema {schema!r}; "
                    f"this reader understands {sorted(SUPPORTED_SCHEMAS)}"
                )
            header = TranscriptHeader.model_validate(record)
            continue
        try:
            entries.append(TranscriptEntry.model_validate(record))
        except ValueError as exc:
            raise TranscriptSchemaError(f"line {line_no}: invalid entry ({exc})") from exc
    if header is None:
        raise TranscriptSchemaError("empty file — no transcript header")
    return Transcript(header=header, entries=tuple(entries))


def _message_blocks(entry: TranscriptEntry) -> list[dict[str, Any]]:
    """Return raw content blocks, synthesizing a text block when only text survived."""
    if entry.content is not None:
        return entry.content
    block_type = "output_text" if entry.role == "assistant" else "input_text"
    return [{"type": block_type, "text": entry.text or ""}]


def item_from_entry(entry: TranscriptEntry) -> dict[str, Any]:
    """Rebuild a ``{"type", "data"}`` item payload from an entry.

    The inverse of :func:`entry_from_item` for everything the file can
    carry. Sealed reasoning comes back with its summary only, which is all
    the file ever had.

    :param entry: One parsed transcript entry.
    :returns: A create payload in the ``SessionEventInput`` shape, with
        ``data`` keyed by the entity field names (``agent``, not ``model``).
    """
    origin = entry.origin_type
    data: dict[str, Any]
    if entry.kind == "message":
        data = {"role": entry.role, "content": _message_blocks(entry)}
        if entry.agent is not None:
            data["agent"] = entry.agent
        if entry.meta:
            data["is_meta"] = True
        if entry.interrupted:
            data["interrupted"] = True
        return {"type": "message", "data": data}
    if entry.kind == "tool_call":
        if origin == "native_tool" or (origin is None and entry.sealed):
            return {"type": "native_tool", "data": {"item": entry.tool_input or {}}}
        if origin == "terminal_command" or (origin is None and entry.tool == "terminal"):
            command = (
                entry.tool_input.get("command") if isinstance(entry.tool_input, dict) else None
            )
            return {"type": "terminal_command", "data": {"kind": "input", "input": command}}
        arguments = (
            entry.tool_input_raw
            if entry.tool_input_raw is not None
            else json.dumps(entry.tool_input)
        )
        data = {
            "agent": entry.agent or "",
            "name": entry.tool or "",
            "arguments": arguments,
            "call_id": entry.call_id or "",
        }
        if entry.namespace is not None:
            data["namespace"] = entry.namespace
        return {"type": "function_call", "data": data}
    if entry.kind == "tool_result":
        if origin == "terminal_command" or (origin is None and entry.tool == "terminal"):
            streams = entry.data or {}
            return {
                "type": "terminal_command",
                "data": {
                    "kind": "output",
                    "stdout": streams.get("stdout", entry.tool_output or ""),
                    "stderr": streams.get("stderr", ""),
                },
            }
        return {
            "type": "function_call_output",
            "data": {"call_id": entry.call_id or "", "output": entry.tool_output or ""},
        }
    if entry.kind == "reasoning":
        summary = [{"type": "summary_text", "text": entry.text}] if entry.text else []
        return {
            "type": "reasoning",
            "data": {"agent": entry.agent or "", "summary": summary, "content": entry.content},
        }
    if entry.kind == "error":
        data = {
            "source": entry.source or "execution",
            "code": entry.code or "unknown",
            "message": entry.text or entry.code or "unknown error",
        }
        if entry.level is not None:
            data["level"] = entry.level
        return {"type": "error", "data": data}
    if entry.kind == "compaction":
        return {
            "type": "compaction",
            "data": {
                "summary": entry.text or "",
                "last_item_id": entry.covers_through or "",
                "model": entry.model,
                "token_count": entry.token_count or 0,
            },
        }
    # note: the raw payload is the item, keyed by the producer's type.
    note_type = entry.note_type or origin or "note"
    data = dict(entry.data or {})
    if "model" in data and _agent_serializes_as_model(note_type):
        data["agent"] = data.pop("model")
    return {"type": note_type, "data": data}


def _agent_serializes_as_model(item_type: str) -> bool:
    """Whether this item type's ``agent`` field is rendered as ``model`` on the wire."""
    from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS

    cls = ITEM_TYPE_TO_DATA_CLS.get(item_type)
    if cls is None:
        return False
    field = cls.model_fields.get("agent")
    return field is not None and field.serialization_alias == "model"
