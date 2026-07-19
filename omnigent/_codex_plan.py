"""Normalize Codex plan notifications for the shared Tasks UI."""

from __future__ import annotations


def todos_from_plan_update(params: object) -> list[dict[str, object]] | None:
    """Map ``turn/plan/updated`` params to the shared todo schema."""
    if not isinstance(params, dict):
        return None
    plan = params.get("plan")
    if not isinstance(plan, list) or not plan:
        return None

    todos: list[dict[str, object]] = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        if not isinstance(step, str) or not step:
            continue
        status = entry.get("status")
        todos.append(
            {
                "content": step,
                "status": (
                    "completed"
                    if status == "completed"
                    else "in_progress"
                    if status in {"inProgress", "in_progress"}
                    else "pending"
                ),
                "activeForm": step,
            }
        )
    return todos or None
