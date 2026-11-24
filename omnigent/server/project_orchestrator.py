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
