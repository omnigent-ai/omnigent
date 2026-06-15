"""Import existing Claude Code and Codex chat transcripts into Omnigent.

Claude Code and Codex each persist a finished local conversation as a
JSON-Lines transcript:

- Claude Code: ``~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl`` — one
  record per line with a top-level ``type`` and a nested
  ``message: {"role", "content"}`` (the Anthropic message shape).
- Codex: ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl`` — one
  record per line wrapping an OpenAI Responses-API item under ``payload``
  (``session_meta`` / ``turn_context`` / ``response_item`` envelopes).

This module converts either format into a list of Omnigent conversation items
(:class:`ImportedItem`) carrying user/assistant messages and tool calls. Each
item is shaped to match the ``initial_items`` contract of ``POST /v1/sessions``
(see :mod:`omnigent.entities.conversation` — ``MessageData`` /
``FunctionCallData`` / ``FunctionCallOutputData``), so a caller can seed a
history-only session from a finished transcript.

Scope: messages and tool calls only. Reasoning/thinking blocks, image and file
attachments, and harness-internal metadata records are skipped. The wire shape
uses the ``"agent"`` key on assistant/function items (``"model"`` is the
output-only serialization alias and is rejected on input).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ImportedItem",
    "ParsedTranscript",
    "TranscriptImportError",
    "detect_source",
    "parse_claude_lines",
    "parse_codex_lines",
    "parse_transcript",
    "parse_transcript_file",
    "to_initial_items",
]

# Source labels (also used as the default ``agent`` label when the transcript
# carries no model id of its own).
CLAUDE = "claude"
CODEX = "codex"

# Claude top-level record ``type`` values that are structural metadata and
# never carry a user/assistant message. Used only for format detection.
_CLAUDE_METADATA_TYPES = frozenset(
    {
        "permission-mode",
        "branch-update",
        "queue-operation",
        "progress",
        "pr-link",
        "summary",
        "system",
    }
)

# Prefixes/markers that flag a Claude user "message" as CLI scaffolding
# (slash-command echoes, local-command caveats, shell markup) rather than text
# the user actually typed. Such content is dropped.
_CLAUDE_SCAFFOLDING_PREFIXES = (
    "<command-message>",
    "<command-args>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)

# Content-block ``type`` values that carry plain message text.
_TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text", "text"})

# Substituted for a tool output that carries only non-text content (e.g. an
# image), so the call/output pairing is preserved without embedding a base64
# payload. Images and files are out of this importer's scope.
_NON_TEXT_OUTPUT_PLACEHOLDER = "[non-text tool output omitted]"


class TranscriptImportError(Exception):
    """Raised when a transcript cannot be parsed into importable items."""


@dataclass(frozen=True)
class ImportedItem:
    """A single Omnigent conversation item parsed from a transcript.

    :param item_type: Omnigent item type — ``"message"``,
        ``"function_call"``, or ``"function_call_output"``.
    :param data: The item payload, shaped to match the corresponding
        :mod:`omnigent.entities.conversation` data model (e.g.
        ``{"role": "user", "content": [...]}``). Assistant messages and
        function calls carry ``"agent"`` (NOT ``"model"`` — ``model`` is the
        output-only serialization alias and is rejected on input).
    """

    item_type: str
    data: dict[str, object]


@dataclass(frozen=True)
class ParsedTranscript:
    """The result of parsing a transcript file.

    :param source: Detected or declared source, ``"claude"`` or ``"codex"``.
    :param items: Ordered conversation items ready to seed a session.
    :param title: A one-line title synthesized from the first user message, or
        ``None`` when no user text is present.
    """

    source: str
    items: list[ImportedItem]
    title: str | None

    @property
    def message_count(self) -> int:
        """Number of message items (user + assistant)."""
        return sum(1 for item in self.items if item.item_type == "message")

    @property
    def tool_call_count(self) -> int:
        """Number of tool/function call items."""
        return sum(1 for item in self.items if item.item_type == "function_call")

    @property
    def tool_output_count(self) -> int:
        """Number of tool/function output items."""
        return sum(1 for item in self.items if item.item_type == "function_call_output")


# ── JSON helpers (keep the parsers free of bare ``Any``) ───────────────────


def _as_record(value: object) -> dict[str, object] | None:
    """Return *value* as a ``dict[str, object]`` when it is a JSON object."""
    if isinstance(value, dict):
        return {str(key): val for key, val in value.items()}
    return None


def _as_str(value: object) -> str | None:
    """Return *value* when it is a string, else ``None``."""
    return value if isinstance(value, str) else None


def _load_record(line: str) -> dict[str, object] | None:
    """Parse one JSONL line into a record dict, or ``None`` to skip it.

    Blank lines, malformed JSON, and non-object records are skipped rather
    than raising, mirroring how the live forwarders tolerate partial writes.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return _as_record(parsed)


def _text_from_blocks(content: object, wanted: frozenset[str]) -> str:
    """Join the text of content blocks whose ``type`` is in *wanted*.

    A plain string *content* is returned verbatim. Blocks without usable text
    (images, files, unknown types) are skipped.

    :param content: A string or a list of content blocks.
    :param wanted: Block ``type`` values to keep.
    :returns: The joined text, or ``""`` when nothing usable is present.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for raw_block in content:
        block = _as_record(raw_block)
        if block is None:
            continue
        if _as_str(block.get("type")) not in wanted:
            continue
        text = _as_str(block.get("text"))
        if text:
            parts.append(text)
    return "\n".join(parts)


# ── Item builders ──────────────────────────────────────────────────────────


def _user_message(text: str) -> ImportedItem:
    """Build a user message item from plain text."""
    return ImportedItem(
        "message", {"role": "user", "content": [{"type": "input_text", "text": text}]}
    )


def _assistant_message(text: str, agent: str) -> ImportedItem:
    """Build an assistant message item from plain text and an agent label."""
    return ImportedItem(
        "message",
        {"role": "assistant", "agent": agent, "content": [{"type": "output_text", "text": text}]},
    )


def _function_call(agent: str, name: str, arguments: str, call_id: str) -> ImportedItem:
    """Build a function_call item. *arguments* is a JSON-encoded string."""
    return ImportedItem(
        "function_call",
        {"agent": agent, "name": name, "arguments": arguments, "call_id": call_id},
    )


def _function_call_output(call_id: str, output: str) -> ImportedItem:
    """Build a function_call_output item paired to *call_id*."""
    return ImportedItem("function_call_output", {"call_id": call_id, "output": output})


# ── Claude Code transcript parsing ──────────────────────────────────────────


def _is_claude_scaffolding(text: str) -> bool:
    """True when a Claude user string is CLI scaffolding, not a real message."""
    stripped = text.lstrip()
    if stripped.startswith(_CLAUDE_SCAFFOLDING_PREFIXES):
        return True
    return "<command-name>" in text or "<bash-" in text


def _stringify_output(raw: object) -> str | None:
    """Coerce a tool output value into a string, or ``None`` when absent.

    Strings pass through. A content-block list is reduced to its text — image
    and file blocks are out of scope, so a list with no text yields a short
    placeholder rather than a base64 dump, which keeps the call/output pairing
    intact. A missing or otherwise non-string value yields ``None``.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return _text_from_blocks(raw, _TEXT_BLOCK_TYPES) or _NON_TEXT_OUTPUT_PLACEHOLDER
    return None


def _claude_tool_result_output(block: dict[str, object]) -> str:
    """Extract a tool_result block's textual output (``""`` when absent)."""
    return _stringify_output(block.get("content")) or ""


def _claude_user_items(content: object) -> list[ImportedItem]:
    """Convert a Claude user record's content into items.

    Text becomes a single user message; ``tool_result`` blocks become
    function_call_output items. CLI scaffolding and images are dropped.
    """
    if isinstance(content, str):
        if not content.strip() or _is_claude_scaffolding(content):
            return []
        return [_user_message(content)]
    if not isinstance(content, list):
        return []
    items: list[ImportedItem] = []
    text_parts: list[str] = []
    for raw_block in content:
        block = _as_record(raw_block)
        if block is None:
            continue
        block_type = _as_str(block.get("type"))
        if block_type == "text":
            text = _as_str(block.get("text"))
            if text and text.strip() and not _is_claude_scaffolding(text):
                text_parts.append(text)
        elif block_type == "tool_result":
            tool_use_id = _as_str(block.get("tool_use_id"))
            if tool_use_id:
                items.append(_function_call_output(tool_use_id, _claude_tool_result_output(block)))
    joined = "\n".join(text_parts)
    if joined:
        items.insert(0, _user_message(joined))
    return items


def _claude_assistant_items(content: object, agent: str) -> list[ImportedItem]:
    """Convert a Claude assistant record's content into items.

    Text becomes a single assistant message; ``tool_use`` blocks become
    function_call items. ``thinking`` blocks and images are dropped.
    """
    if isinstance(content, str):
        return [_assistant_message(content, agent)] if content.strip() else []
    if not isinstance(content, list):
        return []
    items: list[ImportedItem] = []
    text_parts: list[str] = []
    for raw_block in content:
        block = _as_record(raw_block)
        if block is None:
            continue
        block_type = _as_str(block.get("type"))
        if block_type == "text":
            text = _as_str(block.get("text"))
            if text and text.strip():
                text_parts.append(text)
        elif block_type == "tool_use":
            call_id = _as_str(block.get("id"))
            name = _as_str(block.get("name"))
            if call_id and name:
                arguments = json.dumps(block.get("input") or {}, separators=(",", ":"))
                items.append(_function_call(agent, name, arguments, call_id))
    joined = "\n".join(text_parts)
    if joined:
        items.insert(0, _assistant_message(joined, agent))
    return items


def parse_claude_lines(lines: Iterable[str]) -> list[ImportedItem]:
    """Parse Claude Code transcript JSONL lines into Omnigent items.

    :param lines: The transcript's lines (with or without trailing newlines).
    :returns: Ordered conversation items (messages + tool calls).
    """
    items: list[ImportedItem] = []
    latest_model: str | None = None
    for line in lines:
        record = _load_record(line)
        if record is None:
            continue
        if record.get("isSidechain") is True or record.get("isMeta") is True:
            continue
        message = _as_record(record.get("message"))
        if message is None:
            continue
        model = _as_str(message.get("model"))
        if model:
            latest_model = model
        role = _as_str(message.get("role"))
        content = message.get("content")
        if role == "user":
            items.extend(_claude_user_items(content))
        elif role == "assistant":
            items.extend(_claude_assistant_items(content, latest_model or CLAUDE))
    return items


# ── Codex rollout parsing ───────────────────────────────────────────────────


# Codex response_item payload types that carry a tool call / its output. Both
# the OpenAI function-tool shape and the freeform ``custom_tool_call`` shape
# (e.g. ``apply_patch``, Codex's primary file-editing tool) are imported.
_CODEX_CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
_CODEX_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})


def parse_codex_lines(lines: Iterable[str]) -> list[ImportedItem]:
    """Parse Codex rollout JSONL lines into Omnigent items.

    Only ``response_item`` records carry conversation content. ``message``
    payloads become user/assistant messages; ``function_call`` and
    ``custom_tool_call`` (e.g. ``apply_patch``) become function_call items;
    ``function_call_output`` and ``custom_tool_call_output`` become outputs.
    ``session_meta`` / ``turn_context`` (structural), ``event_msg`` (a
    duplicate of message response_items), ``reasoning``, ``developer`` /
    ``system`` messages, and search-tool payloads (``tool_search_*`` /
    ``web_search_*``) are skipped.

    :param lines: The rollout's lines.
    :returns: Ordered conversation items (messages + tool calls).
    """
    items: list[ImportedItem] = []
    # call_ids of emitted calls whose output has not yet been seen, in order.
    # Used to pair an output that omits its own call_id: Codex emits parallel
    # tool calls as a burst followed by their outputs in matching order, so a
    # FIFO pairs correctly where a single "most recent call" would not.
    pending_call_ids: list[str] = []
    for line in lines:
        record = _load_record(line)
        if record is None or _as_str(record.get("type")) != "response_item":
            continue
        payload = _as_record(record.get("payload"))
        if payload is None:
            continue
        payload_type = _as_str(payload.get("type"))
        if payload_type == "message":
            text = _text_from_blocks(payload.get("content"), _TEXT_BLOCK_TYPES)
            role = _as_str(payload.get("role"))
            if not text:
                continue
            if role == "user":
                items.append(_user_message(text))
            elif role == "assistant":
                items.append(_assistant_message(text, CODEX))
        elif payload_type in _CODEX_CALL_TYPES:
            name = _as_str(payload.get("name"))
            call_id = _as_str(payload.get("call_id"))
            if name and call_id:
                # function_call carries JSON ``arguments``; custom_tool_call
                # (e.g. apply_patch) carries a raw string ``input`` instead.
                arguments = (
                    _as_str(payload.get("arguments")) or _as_str(payload.get("input")) or "{}"
                )
                pending_call_ids.append(call_id)
                items.append(_function_call(CODEX, name, arguments, call_id))
        elif payload_type in _CODEX_OUTPUT_TYPES:
            output = _stringify_output(payload.get("output"))
            explicit = _as_str(payload.get("call_id"))
            if explicit is not None:
                call_id = explicit
                if explicit in pending_call_ids:
                    pending_call_ids.remove(explicit)
            elif pending_call_ids:
                call_id = pending_call_ids.pop(0)
            else:
                call_id = None
            if output is not None and call_id:
                items.append(_function_call_output(call_id, output))
    return items


# ── Detection, titling, and top-level entry points ──────────────────────────


def detect_source(lines: Iterable[str]) -> str | None:
    """Detect whether *lines* are a Claude or Codex transcript.

    Scans records until one is decisive: a Codex rollout opens with a
    ``session_meta`` record and wraps items under ``payload``; a Claude
    transcript nests a ``message`` with a user/assistant role, or carries a
    known Claude metadata ``type``.

    :param lines: The transcript's lines.
    :returns: ``"claude"``, ``"codex"``, or ``None`` when undetermined.
    """
    for line in lines:
        record = _load_record(line)
        if record is None:
            continue
        record_type = _as_str(record.get("type"))
        if record_type == "session_meta":
            return CODEX
        if record_type in {"response_item", "turn_context", "event_msg"} and (
            _as_record(record.get("payload")) is not None
        ):
            return CODEX
        message = _as_record(record.get("message"))
        if message is not None and _as_str(message.get("role")) in {"user", "assistant"}:
            return CLAUDE
        if record_type in _CLAUDE_METADATA_TYPES:
            return CLAUDE
    return None


def _synthesize_title(text: str, *, limit: int = 60) -> str | None:
    """Collapse whitespace and truncate *text* into a one-line title."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _title_from_items(items: list[ImportedItem]) -> str | None:
    """Derive a title from the first user message's text, if any."""
    for item in items:
        if item.item_type != "message" or item.data.get("role") != "user":
            continue
        text = _text_from_blocks(item.data.get("content"), frozenset({"input_text"}))
        title = _synthesize_title(text)
        if title:
            return title
    return None


def parse_transcript(lines: Iterable[str], *, source: str = "auto") -> ParsedTranscript:
    """Parse transcript *lines* into a :class:`ParsedTranscript`.

    :param lines: The transcript's lines.
    :param source: ``"claude"``, ``"codex"``, or ``"auto"`` (detect).
    :returns: The parsed transcript with its items and a synthesized title.
    :raises TranscriptImportError: When the format cannot be detected, the
        source is unknown, or no importable items are found.
    """
    line_list = list(lines)
    resolved = source
    if resolved == "auto":
        detected = detect_source(line_list)
        if detected is None:
            raise TranscriptImportError(
                "could not detect transcript format; pass --source claude or --source codex"
            )
        resolved = detected
    if resolved == CLAUDE:
        items = parse_claude_lines(line_list)
    elif resolved == CODEX:
        items = parse_codex_lines(line_list)
    else:
        raise TranscriptImportError(
            f"unknown source {source!r}; expected 'claude', 'codex', or 'auto'"
        )
    if not items:
        raise TranscriptImportError("no importable messages or tool calls found in the transcript")
    return ParsedTranscript(source=resolved, items=items, title=_title_from_items(items))


def parse_transcript_file(path: Path, *, source: str = "auto") -> ParsedTranscript:
    """Read and parse a transcript file.

    :param path: Path to a Claude or Codex ``.jsonl`` transcript.
    :param source: ``"claude"``, ``"codex"``, or ``"auto"`` (detect).
    :returns: The parsed transcript.
    :raises TranscriptImportError: On read failure or unparseable content.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise TranscriptImportError(f"could not read transcript {path}: {err}") from err
    return parse_transcript(text.splitlines(), source=source)


def to_initial_items(items: list[ImportedItem]) -> list[dict[str, object]]:
    """Render items as the ``initial_items`` payload for ``POST /v1/sessions``."""
    return [{"type": item.item_type, "data": item.data} for item in items]
