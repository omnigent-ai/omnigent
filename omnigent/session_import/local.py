"""Read and normalize local coding-harness transcripts."""

from __future__ import annotations

import json
import os
from pathlib import Path

from omnigent.claude_native_bridge import read_transcript_items_from_offset
from omnigent.codex_native import _CODEX_THREAD_ID_RE, _find_codex_rollout
from omnigent.entities import NewConversationItem, parse_item_data
from omnigent.session_import.models import (
    ImportSource,
    LocalSessionImport,
    SessionImportNotFoundError,
)


def _find_transcript(root: Path, session_id: str) -> Path | None:
    """Return the newest parent JSONL transcript whose stem matches the id."""
    matches = [
        path
        for path in root.rglob("*.jsonl")
        if path.stem == session_id and "subagents" not in path.parts and path.is_file()
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _recent_unique_session_ids(
    candidates: list[tuple[Path, str]],
    *,
    limit: int,
) -> tuple[str, ...]:
    """Return unique session ids ordered from newest transcript to oldest."""
    newest_by_id: dict[str, float] = {}
    for path, session_id in candidates:
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        newest_by_id[session_id] = max(newest_by_id.get(session_id, 0), modified_at)
    ordered = sorted(
        newest_by_id,
        key=lambda session_id: (newest_by_id[session_id], session_id),
        reverse=True,
    )
    return tuple(ordered[:limit])


def list_recent_local_session_ids(
    source: ImportSource,
    *,
    limit: int,
) -> tuple[str, ...]:
    """List recent parent session ids for one local harness."""
    if source == "claude":
        configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
        home = Path(configured_home).expanduser() if configured_home else Path.home() / ".claude"
        root = home / "projects"
        candidates = [
            (path, path.stem)
            for path in root.rglob("*.jsonl")
            if "subagents" not in path.parts and path.is_file()
        ]
        return _recent_unique_session_ids(candidates, limit=limit)

    configured_home = os.environ.get("CODEX_HOME")
    home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    rollouts: list[Path] = []
    sessions = home / "sessions"
    archived_sessions = home / "archived_sessions"
    if sessions.is_dir():
        rollouts.extend(path for path in sessions.glob("**/rollout-*.jsonl") if path.is_file())
    if archived_sessions.is_dir():
        rollouts.extend(
            path for path in archived_sessions.glob("rollout-*.jsonl") if path.is_file()
        )
    candidates = []
    for path in rollouts:
        session_id = path.stem[-36:]
        if _CODEX_THREAD_ID_RE.fullmatch(session_id):
            candidates.append((path, session_id))
    return _recent_unique_session_ids(candidates, limit=limit)


def _claude_workspace(transcript_path: Path) -> str | None:
    """Read the first usable cwd recorded in a Claude transcript."""
    with transcript_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            cwd_value = record.get("cwd") if isinstance(record, dict) else None
            if isinstance(cwd_value, str):
                cwd = cwd_value.strip()
                if cwd:
                    return cwd
    return None


def load_claude_session(
    session_id: str,
    *,
    claude_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Claude Code parent session from its local JSONL transcript."""
    configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
    home = claude_home or (Path(configured_home).expanduser() if configured_home else None)
    root = (home or Path.home() / ".claude") / "projects"
    transcript_path = _find_transcript(root, session_id)
    if transcript_path is None:
        raise SessionImportNotFoundError(f"Claude Code session {session_id!r} was not found")

    parsed = read_transcript_items_from_offset(
        transcript_path,
        0,
        start_line=0,
        agent_name="claude-native-ui",
    )
    items = tuple(
        NewConversationItem(
            type=item.item_type,
            response_id=item.response_id,
            data=parse_item_data(item.item_type, item.data),
        )
        for item in parsed.items
    )
    if not items:
        raise SessionImportNotFoundError(
            f"Claude Code session {session_id!r} has no importable history"
        )
    return LocalSessionImport(
        source="claude",
        external_session_id=session_id,
        workspace=_claude_workspace(transcript_path),
        items=items,
    )


def _codex_message_data(payload: dict[str, object]) -> dict[str, object] | None:
    """Convert a visible Codex message payload to Omnigent message data."""
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    expected_type = "input_text" if role == "user" else "output_text"
    raw_content = payload.get("content")
    if not isinstance(raw_content, list):
        return None
    content: list[dict[str, object]] = []
    for block in raw_content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            content.append({"type": expected_type, "text": text})
        elif role == "user" and block.get("type") in {"input_image", "input_file"}:
            content.append(dict(block))
    if not content:
        return None
    data: dict[str, object] = {"role": role, "content": content}
    if role == "assistant":
        data["agent"] = "codex-native-ui"
    elif _codex_internal_user_message(content):
        data["is_meta"] = True
    return data


_CODEX_INTERNAL_USER_PREFIXES = (
    "# AGENTS.md instructions for ",
    "<app-context>",
    "<collaboration_mode>",
    "<environment_context>",
    "<permissions instructions>",
    "<plugins_instructions>",
    "<skill>",
    "<skills_instructions>",
    "The following is the Codex agent history ",
    "The following is the Codex agent history added ",
)


def _codex_internal_user_message(content: list[dict[str, object]]) -> bool:
    """Identify Codex-injected user-role context that should stay hidden."""
    text = next(
        (block.get("text") for block in content if isinstance(block.get("text"), str)),
        None,
    )
    return isinstance(text, str) and text.lstrip().startswith(_CODEX_INTERNAL_USER_PREFIXES)


def _codex_tool_output(value: object) -> str | None:
    """Flatten Codex string or typed-text-block tool output."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    text_blocks = [
        block["text"]
        for block in value
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "".join(text_blocks) if text_blocks else None


def _codex_response_item(
    payload: dict[str, object],
    *,
    response_id: str,
) -> NewConversationItem | None:
    """Convert one supported Codex response item to an Omnigent item."""
    item_type = payload.get("type")
    normalized_type = item_type
    data: dict[str, object] | None = None
    if item_type == "message":
        data = _codex_message_data(payload)
    elif item_type in {"function_call", "custom_tool_call"}:
        name = payload.get("name")
        arguments = payload.get("arguments" if item_type == "function_call" else "input")
        call_id = payload.get("call_id")
        if all(isinstance(value, str) for value in (name, arguments, call_id)):
            data = {
                "agent": "codex-native-ui",
                "name": name,
                "arguments": arguments,
                "call_id": call_id,
            }
            normalized_type = "function_call"
    elif item_type in {"function_call_output", "custom_tool_call_output"}:
        call_id = payload.get("call_id")
        output = _codex_tool_output(payload.get("output"))
        if isinstance(call_id, str) and output is not None:
            data = {"call_id": call_id, "output": output}
            normalized_type = "function_call_output"
    if data is None or not isinstance(normalized_type, str):
        return None
    return NewConversationItem(
        type=normalized_type,
        response_id=response_id[:64],
        data=parse_item_data(normalized_type, data),
    )


def _find_archived_codex_rollout(codex_home: Path, session_id: str) -> Path | None:
    """Return the newest archived Codex rollout matching a session id."""
    archived_sessions = codex_home / "archived_sessions"
    if not archived_sessions.is_dir():
        return None
    suffix = f"-{session_id}.jsonl"
    matches = [
        path
        for path in archived_sessions.glob("rollout-*.jsonl")
        if path.name.endswith(suffix) and path.is_file()
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def load_codex_session(
    session_id: str,
    *,
    codex_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Codex session from its local rollout JSONL file."""
    configured_home = os.environ.get("CODEX_HOME")
    home = codex_home or (Path(configured_home).expanduser() if configured_home else None)
    home = home or Path.home() / ".codex"
    rollout_path = _find_codex_rollout(home, session_id) or _find_archived_codex_rollout(
        home, session_id
    )
    if rollout_path is None:
        raise SessionImportNotFoundError(f"Codex session {session_id!r} was not found")

    workspace: str | None = None
    turn_id = "history"
    items: list[NewConversationItem] = []
    with rollout_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            if record.get("type") == "session_meta":
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    workspace = cwd.strip()
                continue
            if record.get("type") == "turn_context":
                candidate = payload.get("turn_id")
                if isinstance(candidate, str) and candidate:
                    turn_id = candidate
                continue
            if record.get("type") != "response_item":
                continue
            item = _codex_response_item(payload, response_id=f"codex:{turn_id}")
            if item is not None:
                items.append(item)

    if not items:
        raise SessionImportNotFoundError(f"Codex session {session_id!r} has no importable history")
    return LocalSessionImport(
        source="codex",
        external_session_id=session_id,
        workspace=workspace,
        items=tuple(items),
    )


def load_local_session(source: ImportSource, session_id: str) -> LocalSessionImport:
    """Load one local session from the selected first-party harness."""
    if source == "claude":
        return load_claude_session(session_id)
    return load_codex_session(session_id)


__all__ = [
    "list_recent_local_session_ids",
    "load_claude_session",
    "load_codex_session",
    "load_local_session",
]
