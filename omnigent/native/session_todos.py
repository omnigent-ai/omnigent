import json
from typing import Any

_MAX_TODOS, _MAX_TEXT_LENGTH, _MAX_SERIALIZED_BYTES = 100, 4096, 256 * 1024
# A harness's own ID for a to-do, e.g. Claude's task ID, so a renamed task stays the same task.
_MAX_ID_LENGTH = 128


def validate_session_todos(value: object) -> list[dict[str, Any]]:
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
            or todo["status"] not in {"pending", "in_progress", "completed"}
            or not isinstance(todo.get("activeForm"), str)
        ):
            continue
        content, active_form = todo["content"], todo["activeForm"]
        if len(content) > _MAX_TEXT_LENGTH or len(active_form) > _MAX_TEXT_LENGTH:
            raise ValueError(f"session todo text exceeds {_MAX_TEXT_LENGTH} characters")
        status = todo["status"]
        entry = {"content": content, "status": status, "activeForm": active_form}
        todo_id = todo.get("id")
        if isinstance(todo_id, str) and 0 < len(todo_id) <= _MAX_ID_LENGTH:
            entry["id"] = todo_id
        normalized.append(entry)

    size = len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode())
    if size > _MAX_SERIALIZED_BYTES:
        raise ValueError(f"session todos cannot exceed {_MAX_SERIALIZED_BYTES} serialized bytes")
    return normalized
