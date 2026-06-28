"""SQLAlchemy-backed MCP server store."""

from __future__ import annotations

import json

from sqlalchemy import desc, select

from omnigent.db.converters import sql_mcp_server_to_entity
from omnigent.db.db_models import SqlMcpServer
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import McpServer
from omnigent.stores.mcp_server_store import McpServerStore


def _dump_dict(value: dict[str, str] | None) -> str | None:
    """Encode a dict to JSON text, or ``None`` when empty."""
    return json.dumps(dict(value)) if value else None


def _dump_list(value: list[str] | None) -> str | None:
    """Encode a list to JSON text, or ``None`` when empty."""
    return json.dumps(list(value)) if value else None


class SqlAlchemyMcpServerStore(McpServerStore):
    """SQLAlchemy-backed implementation of :class:`McpServerStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy MCP server store.

        :param storage_location: SQLAlchemy database URI.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def create(
        self,
        server_id: str,
        owner: str | None,
        name: str,
        transport: str,
        *,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        description: str | None = None,
    ) -> McpServer:
        """Register a new MCP server in the database."""
        row = SqlMcpServer(
            id=server_id,
            created_at=now_epoch(),
            owner=owner,
            name=name,
            transport=transport,
            url=url,
            headers_json=_dump_dict(headers),
            command=command,
            args_json=_dump_list(args),
            env_json=_dump_dict(env),
            description=description,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return sql_mcp_server_to_entity(row)

    def get(self, server_id: str) -> McpServer | None:
        """Fetch a server by id."""
        with self._session() as session:
            row = session.get(SqlMcpServer, server_id)
            return sql_mcp_server_to_entity(row) if row else None

    def get_by_name(self, owner: str | None, name: str) -> McpServer | None:
        """Fetch one of the owner's servers by name."""
        with self._session() as session:
            stmt = select(SqlMcpServer).where(
                SqlMcpServer.name == name,
                SqlMcpServer.owner == owner if owner is not None else SqlMcpServer.owner.is_(None),
            )
            row = session.execute(stmt).scalar_one_or_none()
            return sql_mcp_server_to_entity(row) if row else None

    def list_for_owner(self, owner: str | None) -> list[McpServer]:
        """List the owner's servers, newest first."""
        with self._session() as session:
            owner_clause = (
                SqlMcpServer.owner == owner if owner is not None else SqlMcpServer.owner.is_(None)
            )
            stmt = (
                select(SqlMcpServer)
                .where(owner_clause)
                .order_by(desc(SqlMcpServer.created_at), desc(SqlMcpServer.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [sql_mcp_server_to_entity(r) for r in rows]

    def update(
        self,
        server_id: str,
        *,
        name: str,
        transport: str,
        url: str | None,
        headers: dict[str, str],
        command: str | None,
        args: list[str],
        env: dict[str, str],
        description: str | None,
    ) -> McpServer | None:
        """Replace a server's config and bump ``updated_at``."""
        with self._session() as session:
            row = session.get(SqlMcpServer, server_id)
            if not row:
                return None
            row.name = name
            row.transport = transport
            row.url = url
            row.headers_json = _dump_dict(headers)
            row.command = command
            row.args_json = _dump_list(args)
            row.env_json = _dump_dict(env)
            row.description = description
            row.updated_at = now_epoch()
            session.flush()
            return sql_mcp_server_to_entity(row)

    def delete(self, server_id: str) -> bool:
        """Delete a server by id."""
        with self._session() as session:
            row = session.get(SqlMcpServer, server_id)
            if not row:
                return False
            session.delete(row)
            return True
