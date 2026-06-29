"""Converters from SQLAlchemy rows to internal entity dataclasses."""

from __future__ import annotations

import json

from omnigent.db.db_models import SqlAgent, SqlMcpServer
from omnigent.entities import Agent, McpServer


def _loads_dict(raw: str | None) -> dict[str, str]:
    """Decode a JSON object column to a ``dict[str, str]`` (``{}`` if null)."""
    if not raw:
        return {}
    value = json.loads(raw)
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _loads_list(raw: str | None) -> list[str]:
    """Decode a JSON array column to a ``list[str]`` (``[]`` if null)."""
    if not raw:
        return []
    value = json.loads(raw)
    return [str(v) for v in value] if isinstance(value, list) else []


def sql_mcp_server_to_entity(row: SqlMcpServer) -> McpServer:
    """
    Convert a :class:`SqlMcpServer` ORM row to an :class:`McpServer`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: An :class:`McpServer` dataclass instance.
    """
    return McpServer(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        owner=row.owner,
        name=row.name,
        transport=row.transport,
        url=row.url,
        headers=_loads_dict(row.headers_json),
        command=row.command,
        args=_loads_list(row.args_json),
        env=_loads_dict(row.env_json),
        description=row.description,
    )


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
