"""SQLAlchemy-backed memory store (SQLite by default)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from omnigent.db.db_models import SqlMemory, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.stores.memory_store import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MemoryEntry,
    MemoryPage,
    MemoryStore,
    validate_actor_id,
    validate_content,
    validate_limit,
    validate_path,
    validate_path_prefix,
)


def _to_entity(row: SqlMemory, *, with_content: bool = True) -> MemoryEntry:
    """Convert an ORM row to a :class:`MemoryEntry`."""
    return MemoryEntry(
        actor_id=row.actor_id,
        path=row.path,
        content=row.content if with_content else "",
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyMemoryStore(MemoryStore):
    """
    :class:`MemoryStore` on any SQLAlchemy database.

    Entries are read by path and listed in path order over the primary key. There
    is no search: matching text inside memory bodies would read every one of an
    actor's rows, and the bodies are stored compressed, opaque to the database.
    """

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g.
            ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.memory_store"
        )
        self._write_session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.memory_store", immediate=True
        )

    def write(
        self, actor_id: str, path: str, content: str, description: str | None = None
    ) -> MemoryEntry:
        validate_actor_id(actor_id)
        validate_path(path)
        validate_content(content, description)
        workspace_id = current_workspace_id()
        now = now_epoch()

        def write(session: Session) -> MemoryEntry:
            row = session.get(SqlMemory, (workspace_id, actor_id, path))
            if row is None:
                row = SqlMemory(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    path=path,
                    content=content,
                    description=description,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.content = content
                row.description = description
                row.updated_at = now
            session.flush()
            return _to_entity(row)

        try:
            return run_write_transaction(self._write_session, "write_memory", write)
        except IntegrityError:
            # A concurrent first write inserted the same path between our get and
            # flush. Retry as an update in a fresh managed transaction.
            pass
        return run_write_transaction(self._write_session, "write_memory_retry", write)

    def get(self, actor_id: str, path: str) -> MemoryEntry | None:
        validate_actor_id(actor_id)
        validate_path(path)
        with self._session("get_memory") as session:
            row = session.get(SqlMemory, (current_workspace_id(), actor_id, path))
            return None if row is None else _to_entity(row)

    def list(
        self,
        actor_id: str,
        path_prefix: str | None = None,
        page_token: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> MemoryPage:
        validate_actor_id(actor_id)
        prefix = validate_path_prefix(path_prefix)
        validate_limit(page_size, MAX_PAGE_SIZE)
        if page_token is not None:
            # The token is the last path returned; reject anything that isn't one.
            validate_path(page_token)
        owned = (SqlMemory.workspace_id == current_workspace_id(), SqlMemory.actor_id == actor_id)
        # Listings carry no bodies, so leave the content column unread.
        under = select(SqlMemory).options(defer(SqlMemory.content)).where(*owned)
        if prefix is not None:
            # Paths compare byte for byte, and "0" follows "/", so everything under
            # the prefix is the key range [prefix + "/", prefix + "0").
            under = under.where(SqlMemory.path >= prefix + "/", SqlMemory.path < prefix + "0")
        if page_token is not None:
            under = under.where(SqlMemory.path > page_token)
        with self._session("list_memories") as session:
            rows: list[SqlMemory] = []
            if prefix is not None and page_token is None:
                # The prefix's own entry sorts before every path under it.
                exact = session.execute(
                    select(SqlMemory)
                    .options(defer(SqlMemory.content))
                    .where(*owned, SqlMemory.path == prefix)
                ).scalar_one_or_none()
                if exact is not None:
                    rows.append(exact)
            rows.extend(
                session.execute(under.order_by(SqlMemory.path).limit(page_size + 1 - len(rows)))
                .scalars()
                .all()
            )
            entries = [_to_entity(row, with_content=False) for row in rows[:page_size]]
        next_token = entries[-1].path if len(rows) > page_size else None
        return MemoryPage(entries=entries, next_page_token=next_token)

    def delete(self, actor_id: str, path: str) -> bool:
        validate_actor_id(actor_id)
        validate_path(path)
        workspace_id = current_workspace_id()

        def write(session: Session) -> bool:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    delete(SqlMemory).where(
                        SqlMemory.workspace_id == workspace_id,
                        SqlMemory.actor_id == actor_id,
                        SqlMemory.path == path,
                    )
                ),
            )
            return result.rowcount > 0

        return run_write_transaction(self._write_session, "delete_memory", write)
