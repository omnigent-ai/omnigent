"""SQLAlchemy-backed registry store."""

from __future__ import annotations

import json

from sqlalchemy import desc, func, select, update

from omnigent.db.converters import sql_published_agent_to_entity
from omnigent.db.db_models import SqlPublishedAgent
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.entities import PagedList, PublishedAgent
from omnigent.stores.registry_store import RegistryStore


class SqlAlchemyRegistryStore(RegistryStore):
    """SQLAlchemy-backed implementation of :class:`RegistryStore`.

    Persists community-published agents in a relational database via
    SQLAlchemy ORM. All write operations are wrapped in managed sessions
    that auto-commit on success.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy registry store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def publish(
        self,
        publication_id: str,
        name: str,
        version: str,
        harness: str,
        description: str,
        author: str,
        created_at: int,
        *,
        category: str | None = None,
        tags: list[str] | None = None,
        prompt_excerpt: str | None = None,
        network_access: bool = False,
        write_access: bool = False,
        guardrails: str | None = None,
        source_url: str | None = None,
        bundle_location: str | None = None,
    ) -> PublishedAgent:
        """Persist a new agent publication. See base class for contract."""
        row = SqlPublishedAgent(
            id=publication_id,
            name=name,
            version=version,
            harness=harness,
            description=description,
            author=author,
            created_at=created_at,
            category=category,
            tags=json.dumps(tags or []),
            prompt_excerpt=prompt_excerpt,
            network_access=network_access,
            write_access=write_access,
            guardrails=guardrails,
            source_url=source_url,
            stars_count=0,
            bundle_location=bundle_location,
        )
        with self._session() as session:
            session.add(row)
            return sql_published_agent_to_entity(row)

    def get(self, name: str, version: str) -> PublishedAgent | None:
        """Fetch a specific ``name@version`` publication. See base class for contract."""
        with self._session() as session:
            row = session.execute(
                select(SqlPublishedAgent).where(
                    SqlPublishedAgent.name == name,
                    SqlPublishedAgent.version == version,
                )
            ).scalar_one_or_none()
            return sql_published_agent_to_entity(row) if row else None

    def get_latest(self, name: str) -> PublishedAgent | None:
        """Fetch the most recently published version by name. See base class for contract."""
        with self._session() as session:
            row = session.execute(
                select(SqlPublishedAgent)
                .where(SqlPublishedAgent.name == name)
                .order_by(desc(SqlPublishedAgent.created_at))
                .limit(1)
            ).scalar_one_or_none()
            return sql_published_agent_to_entity(row) if row else None

    def browse(
        self,
        *,
        category: str | None = None,
        harness: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        limit: int = 20,
        after: str | None = None,
    ) -> PagedList[PublishedAgent]:
        """Browse published agents with optional filters. See base class for contract."""
        with self._session() as session:
            stmt = select(SqlPublishedAgent)

            if category is not None:
                stmt = stmt.where(SqlPublishedAgent.category == category)
            if harness is not None:
                stmt = stmt.where(SqlPublishedAgent.harness == harness)
            if tag is not None:
                # Tags are stored as a JSON array string; use LIKE for portability
                # across SQLite and PostgreSQL without JSON operators.
                stmt = stmt.where(
                    func.lower(SqlPublishedAgent.tags).contains(
                        json.dumps(tag).lower()
                    )
                )
            if q is not None:
                pattern = f"%{q.lower()}%"
                stmt = stmt.where(
                    func.lower(SqlPublishedAgent.name).like(pattern)
                    | func.lower(SqlPublishedAgent.description).like(pattern)
                )

            if after is not None:
                # Cursor: get the created_at of the pivot row, then fetch
                # everything older (created_at < pivot or same ts with earlier id).
                sub = (
                    select(SqlPublishedAgent.created_at)
                    .where(SqlPublishedAgent.id == after)
                    .scalar_subquery()
                )
                stmt = stmt.where(
                    (SqlPublishedAgent.created_at < sub)
                    | (
                        (SqlPublishedAgent.created_at == sub)
                        & (SqlPublishedAgent.id < after)
                    )
                )

            stmt = stmt.order_by(
                desc(SqlPublishedAgent.created_at), desc(SqlPublishedAgent.id)
            ).limit(limit + 1)

            rows = list(session.execute(stmt).scalars().all())
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
            entities = [sql_published_agent_to_entity(r) for r in rows]
            return PagedList(
                data=entities,
                first_id=entities[0].id if entities else None,
                last_id=entities[-1].id if entities else None,
                has_more=has_more,
            )

    def star(self, name: str, version: str) -> int:
        """Atomically increment stars_count. See base class for contract."""
        with self._session() as session:
            result = session.execute(
                update(SqlPublishedAgent)
                .where(
                    SqlPublishedAgent.name == name,
                    SqlPublishedAgent.version == version,
                )
                .values(
                    stars_count=SqlPublishedAgent.stars_count + 1,
                    updated_at=now_epoch(),
                )
                .returning(SqlPublishedAgent.stars_count)
            )
            new_count = result.scalar_one_or_none()
            if new_count is None:
                raise KeyError(f"Agent not found: {name}@{version}")
            return new_count
