"""Claude Code transcript adapter.

Claude Code stores one JSONL transcript per session at
``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` (the filename stem is
the session id). Each line is an envelope with a top-level ``type``; the real
payload for conversation turns is the verbatim Anthropic Messages object under
``message``.

v1 imports top-level sessions only:

- Records with ``isSidechain: true`` are skipped (sub-agent turns inlined into
  the parent transcript).
- Per-session ``.../<sessionId>/subagents/*.jsonl`` files are skipped at
  discovery time.
- Sidecar envelope types (``ai-title``, ``custom-title``, ``summary``,
  ``attachment``, ``system``, ...) carry no ``message`` turn; only
  ``ai-title`` / ``custom-title`` are harvested for the conversation title.
"""

from __future__ import annotations

import json
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

# Claude marks records it generated without a real model call (e.g. injected
# system reminders surfaced as assistant text) with this sentinel model. Never
# use it as the imported agent string.
_SYNTHETIC_MODEL = "<synthetic>"


class ClaudeCodeAdapter(TranscriptAdapter):
    """Importer for Claude Code (``claude``) session transcripts."""

    harness_name = "claude_code"

    def default_root(self) -> Path:
        """:returns: ``~/.claude/projects``."""
        return Path.home() / ".claude" / "projects"

    def discover(self, root: Path) -> list[TranscriptRef]:
        """Find every top-level session transcript under *root*.

        Skips ``subagents/`` directories so sub-agent transcripts are not
        imported as standalone sessions.

        :param root: Directory to scan, e.g. ``~/.claude/projects``.
        :returns: One ref per top-level ``<sessionId>.jsonl`` file.
        """
        if not root.is_dir():
            return []
        refs: list[TranscriptRef] = []
        for path in sorted(root.rglob("*.jsonl")):
            if "subagents" in path.parts:
                continue
            refs.append(TranscriptRef(path=path, session_id=path.stem))
        return refs

    def parse(self, path: Path) -> ParsedTranscript:
        """Parse a Claude Code transcript into Omnigent items.

        :param path: Path to a ``<sessionId>.jsonl`` file.
        :returns: Parsed transcript with metadata and ordered items.
        """
        records = _read_jsonl(path)
        model = _resolve_model(records) or self.harness_name
        items = _build_items(records, model)

        return ParsedTranscript(
            items=items,
            external_session_id=_first_str(records, "sessionId") or path.stem,
            title=_resolve_title(records) or title_from_first_user_message(items),
            model=model,
            cwd=_first_str(records, "cwd"),
            git_branch=_first_str(records, "gitBranch"),
            created_at=earliest_timestamp(records),
        )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Decode a JSONL file into a list of objects, skipping blank/garbage
    lines so a single corrupt record never aborts the whole import.

    :param path: Transcript file path.
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
    """Convert transcript records into ordered conversation items.

    Mirrors the live bridge's grouping: contiguous assistant records share one
    response, tool results join the active assistant turn, and a user prompt
    starts a fresh one.

    :param records: Decoded transcript records in file order.
    :param model: Resolved agent string for assistant-side items.
    :returns: Ordered conversation items.
    """
    grouper = ResponseGrouper()
    items: list[NewConversationItem] = []
    for record in records:
        if record.get("isSidechain") is True:
            continue
        if record.get("type") not in ("user", "assistant"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            items.extend(_user_items(message, grouper))
        elif role == "assistant":
            items.extend(_assistant_items(message, model, grouper))
    return items


def _user_items(
    message: dict[str, object],
    grouper: ResponseGrouper,
) -> list[NewConversationItem]:
    """Build items from a ``role=user`` record.

    A user record carries either user-typed text (a real prompt) or
    ``tool_result`` blocks (outputs answering the prior assistant turn), and
    occasionally both. Tool outputs are emitted first so they stay grouped with
    the active assistant turn; the user prompt then opens a new turn.

    :param message: The ``message`` object (Anthropic Messages shape).
    :param grouper: Shared response-id grouper.
    :returns: Function-call-output items followed by an optional user message.
    """
    content = message.get("content")
    text_blocks: list[dict[str, object]] = []
    outputs: list[NewConversationItem] = []

    if isinstance(content, str):
        if content.strip():
            text_blocks.append({"type": "input_text", "text": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_blocks.append({"type": "input_text", "text": text})
            elif block_type == "tool_result":
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                outputs.append(
                    NewConversationItem(
                        type="function_call_output",
                        response_id=grouper.for_tool_output(),
                        data=FunctionCallOutputData(
                            call_id=call_id,
                            output=_tool_result_output(block),
                        ),
                    )
                )

    items: list[NewConversationItem] = list(outputs)
    if text_blocks:
        items.append(
            NewConversationItem(
                type="message",
                response_id=grouper.for_user_message(),
                data=MessageData(role="user", content=text_blocks),
            )
        )
    return items


def _assistant_items(
    message: dict[str, object],
    model: str,
    grouper: ResponseGrouper,
) -> list[NewConversationItem]:
    """Build items from a ``role=assistant`` record.

    ``text`` blocks become assistant messages, ``thinking`` blocks become
    reasoning items, and ``tool_use`` blocks become function calls — all
    sharing the active assistant turn's response id.

    :param message: The ``message`` object (Anthropic Messages shape).
    :param model: Resolved agent string (required on assistant-side items).
    :param grouper: Shared response-id grouper.
    :returns: Ordered assistant-side items.
    """
    content = message.get("content")
    items: list[NewConversationItem] = []

    if isinstance(content, str):
        if content.strip():
            items.append(_assistant_message(content, model, grouper))
        return items
    if not isinstance(content, list):
        return items

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                items.append(_assistant_message(text, model, grouper))
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking:
                items.append(
                    NewConversationItem(
                        type="reasoning",
                        response_id=grouper.for_assistant_turn(),
                        data=ReasoningData(
                            agent=model,
                            summary=[{"type": "summary_text", "text": thinking}],
                        ),
                    )
                )
        elif block_type == "tool_use":
            tool_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {}
            items.append(
                NewConversationItem(
                    type="function_call",
                    response_id=grouper.for_assistant_turn(),
                    data=FunctionCallData(
                        agent=model,
                        name=name,
                        arguments=json.dumps(arguments, separators=(",", ":")),
                        call_id=tool_id,
                    ),
                )
            )
    return items


def _assistant_message(
    text: str,
    model: str,
    grouper: ResponseGrouper,
) -> NewConversationItem:
    """Build one assistant message item from a text block.

    :param text: Assistant message text.
    :param model: Resolved agent string.
    :param grouper: Shared response-id grouper.
    :returns: An assistant ``message`` item.
    """
    return NewConversationItem(
        type="message",
        response_id=grouper.for_assistant_turn(),
        data=MessageData(
            role="assistant",
            agent=model,
            content=[{"type": "output_text", "text": text}],
        ),
    )


def _tool_result_output(block: dict[str, object]) -> str:
    """Stringify a Claude ``tool_result`` block's content for storage.

    :param block: A ``tool_result`` content block.
    :returns: The output text — the string content verbatim, or a compact JSON
        dump of structured content, or ``""`` when absent.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, separators=(",", ":"))


def _resolve_model(records: list[dict[str, object]]) -> str | None:
    """Return the first real assistant model named in the transcript.

    :param records: Decoded transcript records.
    :returns: A model string, or ``None`` when none is present.
    """
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if isinstance(model, str) and model and model != _SYNTHETIC_MODEL:
            return model
    return None


def _resolve_title(records: list[dict[str, object]]) -> str | None:
    """Harvest a title from sidecar records.

    A user-set ``custom-title`` wins over a generated ``ai-title``.

    :param records: Decoded transcript records.
    :returns: A title string, or ``None`` when no title sidecar is present.
    """
    custom_title: str | None = None
    ai_title: str | None = None
    for record in records:
        record_type = record.get("type")
        if record_type == "custom-title":
            value = record.get("customTitle")
            if isinstance(value, str) and value.strip():
                custom_title = value.strip()
        elif record_type == "ai-title":
            value = record.get("aiTitle")
            if isinstance(value, str) and value.strip():
                ai_title = value.strip()
    return custom_title or ai_title


def _first_str(records: list[dict[str, object]], key: str) -> str | None:
    """Return the first non-empty string value for *key* across records.

    :param records: Decoded transcript records.
    :param key: Envelope field name, e.g. ``"cwd"`` or ``"gitBranch"``.
    :returns: The first non-empty string, or ``None``.
    """
    for record in records:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None
