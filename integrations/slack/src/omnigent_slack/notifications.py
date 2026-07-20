from __future__ import annotations

from typing import Any

from omnigent_slack.omnigent import OutputFile
from omnigent_slack.text import truncate_for_slack

# Status → checkbox glyph for the rendered todo list.
_TODO_MARK = {
    "completed": ":white_check_mark:",
    "in_progress": ":hourglass_flowing_sand:",
    "pending": ":white_large_square:",
}


def format_todos(todos: list[dict[str, Any]]) -> str | None:
    """Render a todo-list update as a Slack message, or ``None`` if empty.

    Uses ``activeForm`` (the gerund) for the in-progress item and ``content``
    otherwise, mirroring how Claude Code presents its own list.
    """
    lines: list[str] = []
    for todo in todos:
        status = str(todo.get("status") or "pending")
        mark = _TODO_MARK.get(status, ":white_large_square:")
        if status == "in_progress":
            label = todo.get("activeForm") or todo.get("content") or ""
        else:
            label = todo.get("content") or todo.get("activeForm") or ""
        label = str(label).strip()
        if not label:
            continue
        lines.append(f"{mark} {label}")
    if not lines:
        return None
    return truncate_for_slack("*Plan*\n" + "\n".join(lines))


def format_output_file(file: OutputFile) -> str:
    """Render a produced-file notice."""
    name = file.filename or file.file_id
    return f":page_facing_up: Produced a file: *{name}*"


def format_policy_denied(reason: str) -> str:
    """Render a policy-DENY notice (the block-without-asking counterpart)."""
    return f":no_entry: Blocked by policy: {truncate_for_slack(reason, limit=2000)}"
