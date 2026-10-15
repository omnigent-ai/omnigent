"""Tenant-scoped Forum state and serialized scheduler leases."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from sqlalchemy import select

from omnigent.db.db_models import SqlForum, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.server.forum.state import ForumState


class ForumStore:
    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._read = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.forum_store"
        )
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.forum_store", immediate=True
        )

    def create(self, forum_id: str, state: ForumState) -> None:
        with self._write("create_forum") as session:
            session.add(
                SqlForum(
                    id=forum_id,
                    workspace_id=current_workspace_id(),
                    state=json.dumps(state.to_dict()),
                    created_at=int(time.time()),
                )
            )

    def get(self, forum_id: str) -> ForumState | None:
        with self._read("read_forum") as session:
            row = session.get(SqlForum, (current_workspace_id(), forum_id))
            return ForumState.from_dict(json.loads(row.state)) if row else None

    def ids(self, *, after: str | None = None, limit: int = 100) -> list[str]:
        with self._read("list_forums") as session:
            query = select(SqlForum.id).where(SqlForum.workspace_id == current_workspace_id())
            if after:
                query = query.where(SqlForum.id > after)
            return list(session.scalars(query.order_by(SqlForum.id).limit(limit)))

    def mutate(
        self, forum_id: str, fn: Callable[[ForumState], object], *, owner: str | None = None
    ) -> ForumState | None:
        with self._write("advance_forum") as session:
            row = session.scalar(
                select(SqlForum)
                .where(SqlForum.workspace_id == current_workspace_id(), SqlForum.id == forum_id)
                .with_for_update()
            )
            if row is None:
                return None
            now = time.time()
            if owner is not None:
                if row.lease_owner not in {None, owner} and row.lease_until > now:
                    return None
                row.lease_owner = owner
                row.lease_until = now + 30
            state = ForumState.from_dict(json.loads(row.state))
            fn(state)
            row.state = json.dumps(state.to_dict())
            return state

    def release(self, forum_id: str, owner: str) -> None:
        with self._write("release_forum") as session:
            row = session.scalar(
                select(SqlForum)
                .where(SqlForum.workspace_id == current_workspace_id(), SqlForum.id == forum_id)
                .with_for_update()
            )
            if row and row.lease_owner == owner:
                row.lease_owner = None
                row.lease_until = 0

    def delete(self, forum_id: str) -> None:
        with self._write("delete_forum") as session:
            row = session.get(SqlForum, (current_workspace_id(), forum_id))
            if row is not None:
                session.delete(row)
