"""Converters from SQLAlchemy rows to internal entity dataclasses."""

from __future__ import annotations

from omnigent.db.db_models import AGENT_KIND_TEMPLATE, SqlAgent
from omnigent.entities import Agent


def sql_agent_to_entity(row: SqlAgent, session_id: str | None = None) -> Agent:
    """
    Convert a :class:`SqlAgent` ORM row to an :class:`Agent` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :param session_id: Owning conversation id when this agent is
        session-scoped; ``None`` for template agents. Callers that know
        the owning conversation id (e.g. the conversation store) pass it
        directly; the agent store leaves it ``None`` for templates (where
        ``row.kind == AGENT_KIND_TEMPLATE``).
    :returns: An :class:`Agent` dataclass instance.
    """
    return Agent(
        id=row.id,
        created_at=row.created_at,
        name=row.name,
        bundle_location=row.bundle_location,
        version=row.version,
        description=row.description,
        updated_at=row.updated_at,
        session_id=None if row.kind == AGENT_KIND_TEMPLATE else session_id,
    )
