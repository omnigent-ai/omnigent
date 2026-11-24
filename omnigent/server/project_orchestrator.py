"""Project ticket notifications for the current main conversation."""

from __future__ import annotations

import json
from typing import Any

from omnigent.runtime.project_ticket_instructions import FINAL_UPDATE_REMINDER


def _enters_completed_stage(event: dict[str, Any]) -> bool:
    stage = event["payload"].get("stage")
    return (
        event["kind"] == "updated" and isinstance(stage, dict) and bool(stage.get("is_complete"))
    )


def ticket_update_message(project_id: str, event: dict[str, Any]) -> str:
    return (
        f"Automatic ticket update for project {project_id}. "
        "Use the orchestrator skill to handle it.\n\n"
        f"Ticket {event['ticket_id']} · {event['kind']}\n"
        + json.dumps(event["payload"], ensure_ascii=False)
        + (f"\n\n{FINAL_UPDATE_REMINDER}" if _enters_completed_stage(event) else "")
    )


_SNIPPET_CHARS = 140


def _snippet(text: Any) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= _SNIPPET_CHARS else flat[: _SNIPPET_CHARS - 1] + "…"


def _digest_part(event: dict[str, Any]) -> str:
    kind, payload = event["kind"], event["payload"]
    if kind == "comment":
        return "agent note"
    if kind == "execution":
        return "execution note"
    if kind == "output":
        return "output"
    if kind == "created":
        return "created"
    changes = payload.get("changes")
    if isinstance(changes, dict):
        return "edited " + "/".join(sorted(changes)) if changes else "edited"
    if "related_ticket_id" in payload:
        return "related ticket " + ("linked" if payload.get("action") == "attach" else "unlinked")
    if "session_id" in payload:
        return "session " + str(payload.get("action", "linked"))
    return kind


def _digest_detail(event: dict[str, Any]) -> str | None:
    payload = event["payload"]
    if event["kind"] in ("comment", "execution") and payload.get("text"):
        return _snippet(payload["text"])
    if event["kind"] == "output" and payload.get("url"):
        return f"{payload.get('type', 'link')} {payload['url']}"
    return None


def ticket_digest_lines(events: list[dict[str, Any]]) -> list[str]:
    """One line per ticket: what changed, with counts, plus its latest note or output."""
    by_ticket: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_ticket.setdefault(event["ticket_id"], []).append(event)
    lines = []
    for ticket_id, ticket_events in by_ticket.items():
        counts: dict[str, int] = {}
        for event in ticket_events:
            part = _digest_part(event)
            counts[part] = counts.get(part, 0) + 1
        summary = ", ".join(
            f"{part} ×{count}" if count > 1 else part for part, count in counts.items()
        )
        title = ticket_events[-1].get("ticket_title") or ""
        latest = next(
            (detail for event in reversed(ticket_events) if (detail := _digest_detail(event))),
            None,
        )
        lines.append(
            f'- {ticket_id} "{_snippet(title)}": {summary}'
            + (f" — latest: {latest}" if latest else "")
        )
    return lines


def ticket_batch_message(project_id: str, events: list[dict[str, Any]]) -> str:
    """One orchestrator turn: urgent events in full, then a digest of the batched rest."""
    urgent = [event for event in events if event.get("urgent", True)]
    batched = [event for event in events if not event.get("urgent", True)]
    if len(urgent) == 1 and not batched:
        return ticket_update_message(project_id, urgent[0])
    sections = []
    for event in urgent:
        sections.append(
            f"Ticket {event['ticket_id']} · {event['kind']}\n"
            + json.dumps(event["payload"], ensure_ascii=False)
            + (f"\n\n{FINAL_UPDATE_REMINDER}" if _enters_completed_stage(event) else "")
        )
    if batched:
        sections.append(
            f"Digest of {len(batched)} background update"
            f"{'' if len(batched) == 1 else 's'} (agent notes, outputs and edits). "
            "Read a ticket only if a line needs action:\n"
            + "\n".join(ticket_digest_lines(batched))
        )
    header = (
        f"Automatic ticket update for project {project_id}. "
        "Use the orchestrator skill to handle it."
        if urgent
        else f"Ticket update digest for project {project_id}. "
        "Nothing here asked to wake you; act only where the orchestrator skill says to."
    )
    return header + "\n\n" + "\n\n".join(sections)
