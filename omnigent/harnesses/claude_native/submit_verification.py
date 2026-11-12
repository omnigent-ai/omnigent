"""Read fresh native outcomes without attributing them to a web input."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from omnigent.util.json_types import JsonObject

_MAX_FRESH_BYTES = 16 * 1024 * 1024
_BLOCKED_HEADER = "UserPromptSubmit operation blocked by hook:\n"
_PROMPT_SEPARATOR = "\n\nOriginal prompt: "


@dataclass(frozen=True)
class SubmitBoundary:
    """A pinned native session and its transcript immediately before submit."""

    path: Path
    native_session_id: str
    byte_offset: int
    prefix_sha256: str
    file_id: tuple[int, int] | None


def serialize_submit_boundary(boundary: SubmitBoundary) -> JsonObject:
    """Persist a captured checkpoint without retaining the submitted prompt."""
    payload: JsonObject = {
        "schema_version": 1,
        "path": str(boundary.path),
        "native_session_id": boundary.native_session_id,
        "byte_offset": boundary.byte_offset,
        "prefix_sha256": boundary.prefix_sha256,
        "file_id": list(boundary.file_id) if boundary.file_id is not None else None,
    }
    if parse_submit_boundary(payload) != boundary:
        raise ValueError("Invalid native submit checkpoint")
    return payload


def parse_submit_boundary(value: object) -> SubmitBoundary | None:
    """Validate persisted checkpoints before using them as completion evidence."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "path",
        "native_session_id",
        "byte_offset",
        "prefix_sha256",
        "file_id",
    }:
        return None
    raw_path, native_session_id = value.get("path"), value.get("native_session_id")
    offset, prefix, file_id = (
        value.get("byte_offset"),
        value.get("prefix_sha256"),
        value.get("file_id"),
    )
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(raw_path, str)
        or "\x00" in raw_path
        or not isinstance(native_session_id, str)
        or not native_session_id
        or type(offset) is not int
        or offset < 0
        or not isinstance(prefix, str)
        or re.fullmatch(r"[0-9a-f]{64}", prefix) is None
    ):
        return None
    path = Path(raw_path)
    if not path.is_absolute() or str(path) != raw_path or ".." in path.parts:
        return None
    if file_id is not None and not (
        isinstance(file_id, list)
        and len(file_id) == 2
        and all(type(part) is int and part >= 0 for part in file_id)
    ):
        return None
    return SubmitBoundary(
        path,
        native_session_id,
        offset,
        prefix,
        (file_id[0], file_id[1]) if file_id is not None else None,
    )


def capture_submit_boundary(path: Path, native_session_id: str) -> SubmitBoundary | None:
    """Capture a complete append boundary; unreadable or partial files stay unknown."""
    try:
        path = path.resolve()
        try:
            with path.open("rb") as handle:
                identity = os.fstat(handle.fileno())
                file_id = (identity.st_dev, identity.st_ino)
                handle.seek(0, 2)
                offset = handle.tell()
                handle.seek(max(0, offset - 256))
                prefix = handle.read(min(offset, 256))
                if prefix and not prefix.endswith(b"\n"):
                    return None
        except FileNotFoundError:
            offset, prefix, file_id = 0, b"", None
        return SubmitBoundary(
            path=path,
            native_session_id=native_session_id,
            byte_offset=offset,
            prefix_sha256=hashlib.sha256(prefix).hexdigest(),
            file_id=file_id,
        )
    except (OSError, ValueError, RuntimeError):
        return None


def read_fresh_submit_records(boundary: SubmitBoundary) -> list[JsonObject]:
    """Return complete appended records while the captured file still matches."""
    try:
        with boundary.path.open("rb") as handle:
            identity = os.fstat(handle.fileno())
            if boundary.file_id is not None and boundary.file_id != (
                identity.st_dev,
                identity.st_ino,
            ):
                return []
            handle.seek(0, 2)
            if handle.tell() < boundary.byte_offset:
                return []
            handle.seek(max(0, boundary.byte_offset - 256))
            prefix = handle.read(min(boundary.byte_offset, 256))
            if hashlib.sha256(prefix).hexdigest() != boundary.prefix_sha256:
                return []
            data = handle.read(_MAX_FRESH_BYTES + 1)
            if len(data) > _MAX_FRESH_BYTES:
                return []
        records: list[JsonObject] = []
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(entry, dict):
                records.append(cast(JsonObject, entry))
        return records
    except (OSError, ValueError, RuntimeError):
        return []


def has_matching_hook_refusal(
    boundary: SubmitBoundary,
    submitted_text: str,
    *,
    normalize_prompt: Callable[[str], str],
) -> bool:
    """Match a fresh explicit refusal only to stop retries, without assigning ownership."""
    for entry in read_fresh_submit_records(boundary):
        if (
            entry.get("type") != "system"
            or entry.get("isSidechain") is True
            or entry.get("sessionId") != boundary.native_session_id
            or not isinstance(entry.get("uuid"), str)
            or not entry["uuid"]
        ):
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.startswith(_BLOCKED_HEADER):
            continue
        body = content.removeprefix(_BLOCKED_HEADER)
        ending = entry.get("promptSubmitEnding")
        if isinstance(ending, dict):
            outcome = ending.get("ending")
            if not isinstance(outcome, dict) or outcome.get("kind") != "blocked_by_hook":
                continue
            reason = outcome.get("reason")
            if not isinstance(reason, str) or not body.startswith(reason + _PROMPT_SEPARATOR):
                continue
            prompt = body[len(reason + _PROMPT_SEPARATOR) :]
        elif ending is None and body.count(_PROMPT_SEPARATOR) == 1:
            _reason, _separator, prompt = body.partition(_PROMPT_SEPARATOR)
        else:
            continue
        if normalize_prompt(prompt) == submitted_text:
            return True
    return False


def has_matching_local_command_completion(boundary: SubmitBoundary, submitted_text: str) -> bool:
    """Recognize a complete matching native command after the pinned append boundary."""
    from omnigent.harnesses.claude_native.bridge import (
        _COMMAND_STDOUT_RE,
        _compact_noop_stdout,
        _first_slash_command_name,
        _is_slash_command_echo,
        _parse_slash_command_record,
        _unwrap_pasted_content_markers,
        is_root_claude_hook_payload,
    )

    if "subagents" in boundary.path.parts:
        return False
    submitted = (
        _unwrap_pasted_content_markers(submitted_text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    name = _first_slash_command_name(submitted)
    if name is None:
        return False
    arguments = submitted[len(name) + 1 :].strip()
    pending_echo: str | None = None
    ambiguous_echo = False
    matching_echoes: set[str] = set()
    completions: set[str] = set()
    for entry in read_fresh_submit_records(boundary):
        if (
            entry.get("sessionId") != boundary.native_session_id
            or (entry.get("isSidechain") is not None and entry.get("isSidechain") is not False)
            or entry.get("agentId")
            or entry.get("agent_id")
            or not is_root_claude_hook_payload(
                {"transcript_path": entry.get("transcriptPath") or entry.get("transcript_path")}
            )
            or entry.get("isMeta") is True
            or not isinstance(entry.get("uuid"), str)
            or not entry["uuid"]
        ):
            continue
        content: object = None
        if entry.get("type") == "system" and entry.get("subtype") == "local_command":
            content = entry.get("content")
        elif entry.get("type") == "user":
            message = entry.get("message")
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
        if not isinstance(content, str):
            continue
        if _is_slash_command_echo(content):
            command = _parse_slash_command_record(content)
            if command is None or command.name != name or command.arguments != arguments:
                pending_echo = None
                ambiguous_echo = True
                continue
            matching_echoes.add(entry["uuid"])
            if command.output is not None:
                completions.add(entry["uuid"])
                pending_echo = None
                continue
            ambiguous_echo = ambiguous_echo or pending_echo is not None
            pending_echo = entry["uuid"]
            continue
        if (
            not content.lstrip().startswith("<local-command-stdout>")
            or _COMMAND_STDOUT_RE.search(content) is None
        ):
            pending_echo = None
            continue
        command_run = entry.get("commandRun")
        if isinstance(command_run, dict):
            command_name, command_args = command_run.get("command"), command_run.get("args", "")
            if (
                isinstance(command_name, str)
                and command_name.removeprefix("/") == name
                and isinstance(command_args, str)
                and command_args.strip() == arguments
            ):
                completions.add(pending_echo or entry["uuid"])
                pending_echo = None
                continue
            pending_echo = None
            continue
        parent = entry.get("parentUuid")
        if pending_echo is not None and parent == pending_echo:
            completions.add(pending_echo)
            pending_echo = None
            continue
        if (
            pending_echo is not None
            and parent is None
            and not ambiguous_echo
            and name == "compact"
            and _compact_noop_stdout(content) is not None
        ):
            completions.add(pending_echo)
        pending_echo = None
    return len(completions) == 1 and len(matching_echoes) <= 1
