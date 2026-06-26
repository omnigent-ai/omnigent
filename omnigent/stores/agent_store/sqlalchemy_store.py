"""SQLAlchemy-backed agent store."""

from __future__ import annotations

from sqlalchemy import and_, asc, desc, or_, select

from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import SqlAgent
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
        owner: str | None = None,
    ) -> Agent:
        """
        Register a new agent in the database.

        :param agent_id: Pre-generated unique agent identifier,
            e.g. ``"ag_0f1a2b3c..."``.
        :param name: Human-readable agent name. Unique among built-ins
            when ``owner`` is ``None``, else unique per owner.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param description: Optional free-text description.
        :param owner: Owning user id for a standalone agent, else
            ``None`` for an operator built-in.
        :returns: The newly created :class:`Agent`.
        """
        row = SqlAgent(
            id=agent_id,
            created_at=now_epoch(),
            name=name,
            bundle_location=bundle_location,
            version=1,
            description=description,
            owner=owner,
        )
        with self._session() as session:
            session.add(row)
            return sql_agent_to_entity(row)

    def list_for_owner(self, owner: str) -> list[Agent]:
        """
        List standalone agents owned by ``owner``, newest-first.

        :param owner: Owning user id, e.g. ``"alice@example.com"``.
        :returns: The owner's standalone agents (``session_id IS NULL``
            and ``owner`` matching).
        """
        with self._session() as session:
            rows = session.execute(
                select(SqlAgent)
                .where(SqlAgent.session_id.is_(None), SqlAgent.owner == owner)
                .order_by(desc(SqlAgent.created_at), desc(SqlAgent.id))
            ).scalars().all()
            return [sql_agent_to_entity(r) for r in rows]

    def get(self, agent_id: str) -> Agent | None:
        """
        Fetch an agent by its unique ID.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        with self._session() as session:
            row = session.get(SqlAgent, agent_id)
            return sql_agent_to_entity(row) if row else None

    def get_by_name(self, name: str) -> Agent | None:
        """
        Look up a registered template agent by its unique name.

        :param name: The template agent's unique name,
            e.g. ``"code-assistant"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlAgent).where(
                    SqlAgent.name == name,
                    SqlAgent.session_id.is_(None),
                    SqlAgent.owner.is_(None),
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
            # Built-in/template agents only: no session AND no owner, so the
            # built-in picker never leaks one user's standalone agents to
            # another (those are listed via ``list_for_owner``).
            template_agent = and_(SqlAgent.session_id.is_(None), SqlAgent.owner.is_(None))
            stmt = select(SqlAgent).where(template_agent)
            if after:
                sub = (
                    select(SqlAgent.created_at)
                    .where(SqlAgent.id == after, template_agent)
                    .scalar_subquery()
                )
                # "after" = further in sort direction
                ts_cmp = SqlAgent.created_at < sub if is_desc else SqlAgent.created_at > sub
                id_cmp = SqlAgent.id < after if is_desc else SqlAgent.id > after
                stmt = stmt.where(or_(ts_cmp, and_(SqlAgent.created_at == sub, id_cmp)))
            if before:
                sub = (
                    select(SqlAgent.created_at)
                    .where(SqlAgent.id == before, template_agent)
                    .scalar_subquery()
                )
                # "before" = opposite of sort direction
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
                select(SqlAgent.id, SqlAgent.name).where(SqlAgent.id.in_(agent_ids))
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
            row = session.get(SqlAgent, agent_id)
            if not row:
                return None
            row.bundle_location = bundle_location
            row.version = row.version + 1
            row.updated_at = now_epoch()
            return sql_agent_to_entity(row)

    def delete(self, agent_id: str) -> bool:
        """
        Delete an agent by ID.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: ``True`` if the agent was deleted, ``False`` if
            it did not exist.
        """
        with self._session() as session:
            row = session.get(SqlAgent, agent_id)
            if not row:
                return False
            session.delete(row)
            return True
