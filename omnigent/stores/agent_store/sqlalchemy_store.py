"""SQLAlchemy-backed agent store."""

from __future__ import annotations

from sqlalchemy import and_, asc, desc, or_, select
from sqlalchemy.exc import IntegrityError

from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import (
    SqlAgent,
    SqlConversation,
    current_workspace_id,
)
from omnigent.db.enum_codecs import encode_agent_kind
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Agent, PagedList
from omnigent.stores.agent_store import AgentStore


class SqlAlchemyAgentStore(AgentStore):
    """
    SQLAlchemy-backed implementation of :class:`AgentStore`.

    Persists agents in a relational database via SQLAlchemy ORM.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy agent store.

        Creates or reuses a SQLAlchemy engine and session factory
        for the given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///agents.db"`` or
            ``"postgresql://user:pass@host/db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None = None,
    ) -> Agent:
        """
        Register a new template agent in the database.

        :param agent_id: Pre-generated unique agent identifier,
            e.g. ``"ag_0f1a2b3c..."``.
        :param name: Human-readable agent name. Must be unique,
            e.g. ``"code-assistant"``.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param description: Optional free-text description.
        :returns: The newly created :class:`Agent`.
        """
        row = SqlAgent(
            id=agent_id,
            created_at=now_epoch(),
            name=name,
            bundle_location=bundle_location,
            version=1,
            kind=encode_agent_kind("template"),
            description=description,
        )
        with self._session() as session:
            # Template names are unique within a workspace. This can't be a
            # partial unique index (MySQL has none), so enforce it here.
            conflict = session.execute(
                select(SqlAgent.id).where(
                    SqlAgent.workspace_id == current_workspace_id(),
                    SqlAgent.name == name,
                    SqlAgent.kind == encode_agent_kind("template"),
                )
            ).first()
            if conflict is not None:
                raise IntegrityError(
                    "Duplicate template agent name",
                    params={"name": name},
                    orig=Exception(f"UNIQUE constraint: name={name!r}"),
                )
            session.add(row)
            return sql_agent_to_entity(row)

    def get(self, agent_id: str) -> Agent | None:
        """
        Fetch an agent by its unique ID.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        with self._session() as session:
            row = session.get(SqlAgent, (current_workspace_id(), agent_id))
            if row is None:
                return None
            # For session-scoped agents, derive the owning conversation id
            # from the forward pointer so callers can use agent.session_id.
            session_id: str | None = None
            if row.kind == encode_agent_kind("session"):
                session_id = session.execute(
                    select(SqlConversation.id)
                    .where(
                        SqlConversation.workspace_id == current_workspace_id(),
                        SqlConversation.agent_id == agent_id,
                    )
                    .limit(1)
                ).scalar_one_or_none()
            return sql_agent_to_entity(row, session_id=session_id)

    def get_by_name(self, name: str) -> Agent | None:
        """
        Look up a registered template agent by its unique name.

        Only agents with ``kind = 'template'`` are returned; session-scoped
        copies bound to a specific conversation are excluded.

        :param name: The template agent's unique name,
            e.g. ``"code-assistant"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlAgent).where(
                    SqlAgent.workspace_id == current_workspace_id(),
                    SqlAgent.name == name,
                    SqlAgent.kind == encode_agent_kind("template"),
                )
            ).scalar_one_or_none()
            return sql_agent_to_entity(row) if row else None

    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> PagedList[Agent]:
        """
        List registered template agents with cursor-based pagination.

        Only agents with ``kind = 'template'`` are returned; session-scoped
        copies are excluded.

        :param limit: Maximum number of agents to return.
        :param after: Cursor agent ID; return agents appearing
            after this agent in sort order,
            e.g. ``"agent_abc123"``.
        :param before: Cursor agent ID; return agents appearing
            before this agent in sort order.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :returns: A :class:`PagedList` of :class:`Agent` objects.
        """
        with self._session() as session:
            is_desc = order == "desc"
            sort_fn = desc if is_desc else asc
            is_template = SqlAgent.kind == encode_agent_kind("template")
            in_workspace = SqlAgent.workspace_id == current_workspace_id()
            stmt = select(SqlAgent).where(in_workspace, is_template)
            if after:
                sub = (
                    select(SqlAgent.created_at)
                    .where(in_workspace, SqlAgent.id == after, is_template)
                    .scalar_subquery()
                )
                ts_cmp = SqlAgent.created_at < sub if is_desc else SqlAgent.created_at > sub
                id_cmp = SqlAgent.id < after if is_desc else SqlAgent.id > after
                stmt = stmt.where(or_(ts_cmp, and_(SqlAgent.created_at == sub, id_cmp)))
            if before:
                sub = (
                    select(SqlAgent.created_at)
                    .where(in_workspace, SqlAgent.id == before, is_template)
                    .scalar_subquery()
                )
                ts_cmp = SqlAgent.created_at > sub if is_desc else SqlAgent.created_at < sub
                id_cmp = SqlAgent.id > before if is_desc else SqlAgent.id < before
                stmt = stmt.where(or_(ts_cmp, and_(SqlAgent.created_at == sub, id_cmp)))
            stmt = stmt.order_by(sort_fn(SqlAgent.created_at), sort_fn(SqlAgent.id)).limit(
                limit + 1
            )
            rows = list(session.execute(stmt).scalars().all())
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
            entities = [sql_agent_to_entity(r) for r in rows]
            return PagedList(
                data=entities,
                first_id=entities[0].id if entities else None,
                last_id=entities[-1].id if entities else None,
                has_more=has_more,
            )

    def get_names(self, agent_ids: list[str]) -> dict[str, str]:
        """
        Batch-fetch agent names for a list of IDs.

        Uses a single SQL ``IN`` query. IDs not found in the store
        are omitted from the result.

        :param agent_ids: List of agent identifiers to look up,
            e.g. ``["ag_abc123", "ag_def456"]``.
        :returns: Mapping of ``{agent_id: agent_name}`` for found
            agents.
        """
        if not agent_ids:
            return {}
        with self._session() as session:
            rows = session.execute(
                select(SqlAgent.id, SqlAgent.name).where(
                    SqlAgent.workspace_id == current_workspace_id(),
                    SqlAgent.id.in_(agent_ids),
                )
            ).all()
            return {row.id: row.name for row in rows}

    def update(
        self,
        agent_id: str,
        bundle_location: str,
    ) -> Agent | None:
        """
        Update an agent's bundle location, bump version, and set
        ``updated_at``.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :param bundle_location: New artifact store key for the
            bundle, e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :returns: The updated :class:`Agent`, or ``None`` if not
            found.
        """
        with self._session() as session:
            row = session.get(SqlAgent, (current_workspace_id(), agent_id))
            if not row:
                return None
            row.bundle_location = bundle_location
            row.version = row.version + 1
            row.updated_at = now_epoch()
            session_id: str | None = None
            if row.kind == encode_agent_kind("session"):
                session_id = session.execute(
                    select(SqlConversation.id)
                    .where(
                        SqlConversation.workspace_id == current_workspace_id(),
                        SqlConversation.agent_id == agent_id,
                    )
                    .limit(1)
                ).scalar_one_or_none()
            return sql_agent_to_entity(row, session_id=session_id)

    def delete(self, agent_id: str) -> bool:
        """
        Delete an agent by ID.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: ``True`` if the agent was deleted, ``False`` if
            it did not exist.
        """
        with self._session() as session:
            row = session.get(SqlAgent, (current_workspace_id(), agent_id))
            if not row:
                return False
            session.delete(row)
            return True
