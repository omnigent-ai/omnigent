"""Bounded native-harness Plan data shared by persistence and event ingestion."""

from __future__ import annotations

import json
from typing import Any

_VALID_STATUSES = {"pending", "in_progress", "completed"}
_MAX_TODOS = 100
_MAX_TEXT_LENGTH = 4096
_MAX_SERIALIZED_BYTES = 256 * 1024


def validate_session_todos(value: object) -> list[dict[str, Any]]:
    """Keep valid display fields, rejecting oversized payloads before storage."""
    if not isinstance(value, list):
        raise ValueError("session todos must be a list")
    if len(value) > _MAX_TODOS:
        raise ValueError(f"session todos cannot exceed {_MAX_TODOS} items")
    normalized: list[dict[str, Any]] = []
    for todo in value:
        if (
            not isinstance(todo, dict)
            or not isinstance(todo.get("content"), str)
            or not isinstance(todo.get("status"), str)
            or todo["status"] not in _VALID_STATUSES
            or not isinstance(todo.get("activeForm"), str)
        ):
            continue
        content, active_form = todo["content"], todo["activeForm"]
        if len(content) > _MAX_TEXT_LENGTH or len(active_form) > _MAX_TEXT_LENGTH:
            raise ValueError(
                f"session todo text fields cannot exceed {_MAX_TEXT_LENGTH} characters"
            )
        normalized.append(
            {"content": content, "status": todo["status"], "activeForm": active_form}
        )
    if (
        len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode())
        > _MAX_SERIALIZED_BYTES
    ):
        raise ValueError(f"session todos cannot exceed {_MAX_SERIALIZED_BYTES} serialized bytes")
    return normalized
