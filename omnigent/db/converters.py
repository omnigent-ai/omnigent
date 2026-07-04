"""Converters from SQLAlchemy rows to internal entity dataclasses."""

from __future__ import annotations

import json

from omnigent.db.db_models import SqlAgent, SqlPublishedAgent
from omnigent.entities import Agent, PublishedAgent


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
    )


def sql_published_agent_to_entity(row: SqlPublishedAgent) -> PublishedAgent:
    """
    Convert a :class:`SqlPublishedAgent` ORM row to a :class:`PublishedAgent`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`PublishedAgent` dataclass instance.
    """
    try:
        tags: list[str] = json.loads(row.tags or "[]")
    except (ValueError, TypeError):
        tags = []
    return PublishedAgent(
        id=row.id,
        name=row.name,
        version=row.version,
        harness=row.harness,
        description=row.description,
        author=row.author,
        created_at=row.created_at,
        category=row.category,
        tags=tags,
        prompt_excerpt=row.prompt_excerpt,
        network_access=bool(row.network_access),
        write_access=bool(row.write_access),
        guardrails=row.guardrails,
        source_url=row.source_url,
        stars_count=row.stars_count or 0,
        bundle_location=row.bundle_location,
        updated_at=row.updated_at,
    )
