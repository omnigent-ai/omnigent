"""Converters from SQLAlchemy rows to internal entity dataclasses."""

from __future__ import annotations

from omnigent.db.db_models import SqlAgent
from omnigent.entities import Agent


def sql_agent_to_entity(row: SqlAgent) -> Agent:
    """
    Convert a :class:`SqlAgent` ORM row to an :class:`Agent` entity.

    :param row: The SQLAlchemy ORM row to convert.
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
        session_id=row.session_id,
        owner=row.owner,
    )
