"""Project ticket notifications for the current main conversation."""

from __future__ import annotations

import json
from typing import Any


def ticket_update_message(project_id: str, event: dict[str, Any]) -> str:
    return (
        f"Automatic ticket update for project {project_id}. "
        "Use the orchestrator skill to handle it.\n\n"
        f"Ticket {event['ticket_id']} · {event['kind']}\n"
        + json.dumps(event["payload"], ensure_ascii=False)
    )
