"""Lossless, stable conversion of retired ticket fields into ordinary notes."""

from collections.abc import Mapping
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5

LEGACY_TICKET_NOTE_FIELDS = {
    "plan": "Plan",
    "review_summary": "Review notes",
    "completion_summary": "Result",
}


def legacy_ticket_notes(values: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: f"### {label}\n\n{text}"
        for field, label in LEGACY_TICKET_NOTE_FIELDS.items()
        if isinstance(text := values.get(field), str) and text
    }


def legacy_ticket_note_id(
    workspace_id: int, project_id: str, ticket_id: str, field: str, text: str
) -> str:
    digest = sha256(text.encode("utf-8")).hexdigest()
    return uuid5(
        NAMESPACE_URL,
        f"omnigent:ticket-note:{workspace_id}:{project_id}:{ticket_id}:{field}:{digest}",
    ).hex
