"""Codex transcript adapter.

Codex stores one JSONL rollout per session at
``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``. Each line is an
envelope ``{timestamp, type, payload}``:

- ``session_meta`` (first line) — ``payload.id`` (session id), ``cwd``,
  ``git`` (``{branch, ...}``), ``timestamp``.
- ``turn_context`` — ``payload.model`` (and reasoning effort).
- ``response_item`` — the real conversation items, in OpenAI Responses shape.
- ``event_msg`` — telemetry, skipped.

``response_item`` payload types map ~1:1 to Omnigent items. ``arguments`` and
``output`` are already strings, so they pass through untouched. Codex's
``custom_tool_call`` / ``custom_tool_call_output`` (e.g. ``apply_patch``) are
treated as ordinary function calls so tool history survives the import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from omnigent.entities import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
    ReasoningData,
)
from omnigent.importers.base import (
    ParsedTranscript,
    ResponseGrouper,
    TranscriptAdapter,
    TranscriptRef,
    earliest_timestamp,
    title_from_first_user_message,
)

# The uuid trailing a ``rollout-<ts>-<uuid>.jsonl`` filename; equals the
# session id recorded in ``session_meta.payload.id``.
_ROLLOUT_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


class CodexAdapter(TranscriptAdapter):
    """Importer for Codex (``codex``) session rollouts."""

    harness_name = "codex"

    def default_root(self) -> Path:
        """:returns: ``~/.codex/sessions``."""
        return Path.home() / ".codex" / "sessions"

    def discover(self, root: Path) -> list[TranscriptRef]:
        """Find every rollout transcript under *root*.

        :param root: Directory to scan, e.g. ``~/.codex/sessions``.
        :returns: One ref per ``rollout-*.jsonl`` file.
        """
        if not root.is_dir():
            return []
        refs: list[TranscriptRef] = []
        for path in sorted(root.rglob("rollout-*.jsonl")):
            match = _ROLLOUT_UUID_RE.search(path.stem)
            refs.append(
                TranscriptRef(
                    path=path,
                    session_id=match.group(1) if match else path.stem,
                )
            )
        return refs

    def parse(self, path: Path) -> ParsedTranscript:
        """Parse a Codex rollout into Omnigent items.

        :param path: Path to a ``rollout-*.jsonl`` file.
        :returns: Parsed transcript with metadata and ordered items.
        """
        records = _read_jsonl(path)
        meta = _first_payload(records, "session_meta")
        turn_context = _first_payload(records, "turn_context")

        model = _str_field(turn_context, "model") or self.harness_name
        items = _build_items(records, model)

        git = meta.get("git")
        git_branch = git.get("branch") if isinstance(git, dict) else None

        match = _ROLLOUT_UUID_RE.search(path.stem)
        external_session_id = _str_field(meta, "id") or (match.group(1) if match else None)

        return ParsedTranscript(
            items=items,
            external_session_id=external_session_id,
            title=title_from_first_user_message(items),
            model=model,
            cwd=_str_field(meta, "cwd"),
            git_branch=git_branch if isinstance(git_branch, str) and git_branch else None,
            # The canonical per-record timestamp lives on the top-level envelope,
            # not inside the session_meta payload (which may omit it), so scan all
            # records for the earliest rather than reading meta["timestamp"].
            created_at=earliest_timestamp(records),
        )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Decode a JSONL file into objects, skipping blank/garbage lines.

    :param path: Rollout file path.
    :returns: Decoded JSON objects in file order.
    """
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def _build_items(records: list[dict[str, object]], model: str) -> list[NewConversationItem]:
    """Convert ``response_item`` records into ordered conversation items.

    Other envelope types (``session_meta``, ``turn_context``, ``event_msg``)
    are skipped. Grouping follows the shared :class:`ResponseGrouper` rules.

    :param records: Decoded rollout records in file order.
    :param model: Resolved agent string for assistant-side items.
    :returns: Ordered conversation items.
    """
    grouper = ResponseGrouper()
    items: list[NewConversationItem] = []
    for record in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        item = _item_from_payload(payload, model, grouper)
        if item is not None:
            items.append(item)
    return items


def _item_from_payload(
    payload: dict[str, object],
    model: str,
    grouper: ResponseGrouper,
) -> NewConversationItem | None:
    """Convert one ``response_item`` payload into a conversation item.

    :param payload: The ``response_item`` payload (OpenAI Responses shape).
    :param model: Resolved agent string for assistant-side items.
    :param grouper: Shared response-id grouper.
    :returns: A conversation item, or ``None`` for unsupported / empty payloads
        (e.g. ``developer`` / ``system`` messages, which Omnigent can't model).
    """
    payload_type = payload.get("type")

    if payload_type == "message":
        return _message_item(payload, model, grouper)
    if payload_type in ("function_call", "custom_tool_call"):
        return _function_call_item(payload, model, grouper)
    if payload_type in ("function_call_output", "custom_tool_call_output"):
        return _function_call_output_item(payload, grouper)
    if payload_type == "reasoning":
        return _reasoning_item(payload, model, grouper)
    return None


def _message_item(
    payload: dict[str, object],
    model: str,
    grouper: ResponseGrouper,
) -> NewConversationItem | None:
    """Convert a ``message`` payload into a user/assistant message item.

    Roles other than ``user`` / ``assistant`` (``developer`` / ``system``
    scaffolding) are skipped — Omnigent's message model has no place for them.

    :param payload: The ``message`` payload.
    :param model: Resolved agent string (required for assistant messages).
    :param grouper: Shared response-id grouper.
    :returns: A ``message`` item, or ``None`` when the role is unsupported or
        the message holds no text.
    """
    role = payload.get("role")
    if role == "user":
        api_type = "input_text"
    elif role == "assistant":
        api_type = "output_text"
    else:
        return None

    texts = _texts_from_content(payload.get("content"))
    if not texts:
        return None

    content = [{"type": api_type, "text": text} for text in texts]
    if role == "user":
        return NewConversationItem(
            type="message",
            response_id=grouper.for_user_message(),
            data=MessageData(role="user", content=content),
        )
    return NewConversationItem(
        type="message",
        response_id=grouper.for_assistant_turn(),
        data=MessageData(role="assistant", agent=model, content=content),
    )


def _function_call_item(
    payload: dict[str, object],
    model: str,
    grouper: ResponseGrouper,
) -> NewConversationItem | None:
    """Convert a (custom) tool-call payload into a ``function_call`` item.

    ``function_call`` carries ``arguments`` (a JSON string); ``custom_tool_call``
    carries ``input`` (a string). Both pass through as the item's ``arguments``.

    :param payload: The call payload.
    :param model: Resolved agent string.
    :param grouper: Shared response-id grouper.
    :returns: A ``function_call`` item, or ``None`` when required fields are
        missing.
    """
    name = payload.get("name")
    call_id = payload.get("call_id")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(call_id, str) or not call_id:
        return None
    raw_arguments = payload.get("arguments")
    if not isinstance(raw_arguments, str):
        raw_arguments = payload.get("input")
    arguments = raw_arguments if isinstance(raw_arguments, str) else ""
    return NewConversationItem(
        type="function_call",
        response_id=grouper.for_assistant_turn(),
        data=FunctionCallData(
            agent=model,
            name=name,
            arguments=arguments,
            call_id=call_id,
        ),
    )


def _function_call_output_item(
    payload: dict[str, object],
    grouper: ResponseGrouper,
) -> NewConversationItem | None:
    """Convert a (custom) tool-output payload into a ``function_call_output``.

    :param payload: The output payload; ``output`` is already a string.
    :param grouper: Shared response-id grouper.
    :returns: A ``function_call_output`` item, or ``None`` when the call id is
        missing.
    """
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    output = payload.get("output")
    if not isinstance(output, str):
        output = "" if output is None else json.dumps(output, separators=(",", ":"))
    return NewConversationItem(
        type="function_call_output",
        response_id=grouper.for_tool_output(),
        data=FunctionCallOutputData(call_id=call_id, output=output),
    )


def _reasoning_item(
    payload: dict[str, object],
    model: str,
    grouper: ResponseGrouper,
) -> NewConversationItem:
    """Convert a ``reasoning`` payload into a ``reasoning`` item.

    ``summary`` (a list of ``summary_text`` blocks, often empty) and
    ``encrypted_content`` pass through; ``content`` is carried when present.

    :param payload: The ``reasoning`` payload.
    :param model: Resolved agent string.
    :param grouper: Shared response-id grouper.
    :returns: A ``reasoning`` item.
    """
    raw_summary = payload.get("summary")
    summary: list[dict[str, str]] = []
    if isinstance(raw_summary, list):
        summary = [block for block in raw_summary if isinstance(block, dict)]
    raw_content = payload.get("content")
    content: list[dict[str, str]] | None = None
    if isinstance(raw_content, list):
        content = [block for block in raw_content if isinstance(block, dict)]
    encrypted = payload.get("encrypted_content")
    return NewConversationItem(
        type="reasoning",
        response_id=grouper.for_assistant_turn(),
        data=ReasoningData(
            agent=model,
            summary=summary,
            content=content,
            encrypted_content=encrypted if isinstance(encrypted, str) else None,
        ),
    )


def _texts_from_content(content: object) -> list[str]:
    """Extract the text of each ``input_text`` / ``output_text`` / ``text``
    block, preserving block boundaries.

    :param content: A ``response_item`` message ``content`` value (list of
        blocks or, defensively, a bare string).
    :returns: One string per non-empty text block; empty when none are present.
    """
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text", "text"):
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return texts


def _first_payload(records: list[dict[str, object]], record_type: str) -> dict[str, object]:
    """Return the first payload dict for an envelope *record_type*.

    :param records: Decoded rollout records.
    :param record_type: Envelope type, e.g. ``"session_meta"``.
    :returns: The payload dict, or ``{}`` when absent / malformed.
    """
    for record in records:
        if record.get("type") == record_type:
            payload = record.get("payload")
            if isinstance(payload, dict):
                return payload
    return {}


def _str_field(payload: dict[str, object], key: str) -> str | None:
    """Return a non-empty string field from a payload, else ``None``.

    :param payload: A payload dict.
    :param key: Field name.
    :returns: The string value, or ``None``.
    """
    value = payload.get(key)
    return value if isinstance(value, str) and value else None
