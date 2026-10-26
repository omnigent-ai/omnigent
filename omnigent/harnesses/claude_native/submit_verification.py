"""Read fresh native outcomes without attributing them to a web input."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

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


def _seek_boundary(handle: BinaryIO, boundary: SubmitBoundary) -> bytes | None:
    """Position *handle* at *boundary*; return the bytes before it, or ``None`` once replaced."""
    identity = os.fstat(handle.fileno())
    if boundary.file_id is not None and boundary.file_id != (identity.st_dev, identity.st_ino):
        return None
    handle.seek(0, 2)
    if handle.tell() < boundary.byte_offset:
        return None
    handle.seek(max(0, boundary.byte_offset - 256))
    prefix = handle.read(min(boundary.byte_offset, 256))
    if hashlib.sha256(prefix).hexdigest() != boundary.prefix_sha256:
        return None
    return prefix


def read_fresh_submit_records(boundary: SubmitBoundary) -> list[JsonObject]:
    """Return complete appended records while the captured file still matches."""
    try:
        with boundary.path.open("rb") as handle:
            if _seek_boundary(handle, boundary) is None:
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


@dataclass(frozen=True)
class QueuedPrompt:
    """A prompt Claude Code may still hold in its queue."""

    # Byte offset of its ``enqueue`` record, which identifies the entry.
    offset: int
    text: str | None


@dataclass(frozen=True)
class PromptQueue:
    """Claude Code's prompt queue as its log shows it up to ``boundary``."""

    boundary: SubmitBoundary
    # Prompts that may still be queued, oldest first.
    prompts: tuple[QueuedPrompt, ...] = ()
    # Dequeues since the queue was last seen empty; a dequeue names no prompt.
    dequeued: int = 0


def start_prompt_queue(path: Path, native_session_id: str) -> PromptQueue:
    """
    Follow the queue from the end of the transcript's last complete record, taken as empty there.

    :raises OSError: When the transcript cannot be read now.
    """
    path = path.resolve()
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        empty = hashlib.sha256(b"").hexdigest()
        return PromptQueue(SubmitBoundary(path, native_session_id, 0, empty, None))
    with handle:
        identity = os.fstat(handle.fileno())
        offset = handle.seek(0, 2)
        while offset > 0:
            start = max(0, offset - 64 * 1024)
            handle.seek(start)
            newline = handle.read(offset - start).rfind(b"\n")
            if newline >= 0:
                offset = start + newline + 1
                break
            offset = start
        handle.seek(max(0, offset - 256))
        prefix = handle.read(min(offset, 256))
    boundary = SubmitBoundary(
        path=path,
        native_session_id=native_session_id,
        byte_offset=offset,
        prefix_sha256=hashlib.sha256(prefix).hexdigest(),
        file_id=(identity.st_dev, identity.st_ino),
    )
    return PromptQueue(boundary)


def advance_prompt_queue(queue: PromptQueue) -> PromptQueue | None:
    """
    Apply Claude Code's queue log appended after ``queue.boundary``.

    Claude picks a ``dequeue`` by priority or filter, so dequeues show the queue
    empty only once they cover every outstanding prompt. ``remove``, ``popAll`` and
    ``popOne`` name the prompt they take out. The queue is taken to be empty where
    following starts, so a prompt queued before then can be miscounted.

    :param queue: The queue up to its boundary, from :func:`start_prompt_queue`.
    :returns: The queue up to the last complete record, or ``None`` once the
        transcript no longer continues from the boundary.
    :raises OSError: When the transcript cannot be read now.
    """
    boundary = queue.boundary
    prompts = list(queue.prompts)
    dequeued = queue.dequeued
    with boundary.path.open("rb") as handle:
        prefix = _seek_boundary(handle, boundary)
        if prefix is None:
            return None
        identity = os.fstat(handle.fileno())
        offset = boundary.byte_offset
        for line in handle:
            if not line.endswith(b"\n"):
                break
            entry = _queue_operation(line, boundary.native_session_id)
            if entry is not None:
                content = entry.get("content")
                text = content if isinstance(content, str) else None
                operation = entry.get("operation")
                if operation == "enqueue":
                    prompts.append(QueuedPrompt(offset, text))
                elif operation == "dequeue":
                    dequeued += 1
                elif operation in ("remove", "popAll", "popOne"):
                    named = next((prompt for prompt in prompts if prompt.text == text), None)
                    if named is not None:
                        prompts.remove(named)
                if dequeued >= len(prompts):
                    # Any extra dequeues took prompts queued before following started.
                    prompts, dequeued = [], 0
            offset += len(line)
            prefix = (prefix + line[-256:])[-256:]
    end = SubmitBoundary(
        path=boundary.path,
        native_session_id=boundary.native_session_id,
        byte_offset=offset,
        prefix_sha256=hashlib.sha256(prefix).hexdigest(),
        file_id=(identity.st_dev, identity.st_ino),
    )
    return PromptQueue(end, tuple(prompts), dequeued)


def _queue_operation(line: bytes, native_session_id: str) -> JsonObject | None:
    """Parse *line* when it is a queue operation of *native_session_id*."""
    if b'"queue-operation"' not in line:
        return None
    try:
        entry = json.loads(line)
    except (ValueError, UnicodeError):
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("type") != "queue-operation"
        or entry.get("sessionId") != native_session_id
    ):
        return None
    return cast(JsonObject, entry)


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
