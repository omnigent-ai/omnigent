"""Read fresh native outcomes without attributing them to a web input."""

from __future__ import annotations

import hashlib
import json
import os
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
