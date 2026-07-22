"""SQLAlchemy-backed comments store."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select

from omnigent.db.db_models import SqlComment, current_workspace_id
from omnigent.db.enum_codecs import decode_comment_status, encode_comment_status
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch_us,
)
from omnigent.entities import Comment, CommentsFingerprint
from omnigent.stores.comment_store import CommentStore


def _to_entity(row: SqlComment) -> Comment:
    """Convert a :class:`SqlComment` ORM row to a :class:`Comment`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Comment` dataclass instance.
    """
    return Comment(
        id=row.id,
        conversation_id=row.conversation_id,
        path=row.path,
        start_index=row.start_index,
        end_index=row.end_index,
        body=row.body,
        status=decode_comment_status(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        anchor_content=row.anchor_content,
        created_by=row.created_by,
    )


class SqlAlchemyCommentStore(CommentStore):
    """SQLAlchemy-backed implementation of :class:`CommentStore`.

    Persists comments in a relational database via SQLAlchemy ORM.
    All write operations are wrapped in managed sessions that
    auto-commit on success.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy comments store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def get(self, comment_id: str, conversation_id: str) -> Comment | None:
        """Fetch a single comment by id, scoped to a conversation. See base class for contract."""
        with self._session() as session:
            # conversation_id is part of the PK, so the lookup itself enforces
            # the conversation scoping — a wrong conversation simply misses.
            row = session.get(SqlComment, (current_workspace_id(), conversation_id, comment_id))
            if row is None:
                return None
            return _to_entity(row)

    def add(
        self,
        conversation_id: str,
        path: str,
        body: str,
        start_index: int,
        end_index: int,
        anchor_content: str | None = None,
        created_by: str | None = None,
    ) -> Comment:
        """Create and persist a new comment. See base class for contract."""
        # One clock read for both timestamps so a never-edited comment's
        # updated_at (µs, fingerprint precision) and created_at (seconds,
        # display) describe the same instant — the invariant the migration
        # backfill (created_at * 1e6) and docs rely on.
        created_us = now_epoch_us()
        row = SqlComment(
            id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            path=path,
            start_index=start_index,
            end_index=end_index,
            body=body,
            status=encode_comment_status("draft"),
            created_at=created_us // 1_000_000,
            updated_at=created_us,
            anchor_content=anchor_content,
            created_by=created_by,
        )
        with self._session() as session:
            session.add(row)
            return _to_entity(row)

    def list_for_conversation(
        self,
        conversation_id: str,
        path: str | None = None,
    ) -> list[Comment]:
        """Return comments for a conversation. See base class for contract."""
        stmt = select(SqlComment).where(
            SqlComment.workspace_id == current_workspace_id(),
            SqlComment.conversation_id == conversation_id,
        )
        if path is not None:
            stmt = stmt.where(SqlComment.path == path)
        # created_at is seconds-granular; id breaks same-second ties so the
        # listing has a stable, deterministic order.
        stmt = stmt.order_by(SqlComment.created_at, SqlComment.id)
        with self._session() as session:
            rows = list(session.execute(stmt).scalars().all())
            return [_to_entity(r) for r in rows]

    def update_comment(
        self,
        comment_id: str,
        conversation_id: str,
        *,
        status: str | None = None,
        body: str | None = None,
    ) -> Comment | None:
        """Update a comment's fields, scoped to a conversation. See base class for contract."""
        with self._session() as session:
            row = session.get(SqlComment, (current_workspace_id(), conversation_id, comment_id))
            if row is None:
                return None
            if status is not None:
                row.status = encode_comment_status(status)
            if body is not None:
                row.body = body
            if status is not None or body is not None:
                row.updated_at = now_epoch_us()
            return _to_entity(row)

    def delete(self, comment_id: str, conversation_id: str) -> Comment | None:
        """Delete a single comment by id, scoped to a conversation. See base class for contract."""
        with self._session() as session:
            row = session.get(SqlComment, (current_workspace_id(), conversation_id, comment_id))
            if row is None:
                return None
            entity = _to_entity(row)
            session.delete(row)
            return entity

    def get_comments_fingerprints(
        self, conversation_ids: list[str]
    ) -> dict[str, CommentsFingerprint]:
        """Return per-conversation comment fingerprints. See base class for contract."""
        if not conversation_ids:
            return {}
        stmt = (
            select(
                SqlComment.conversation_id,
                func.count(SqlComment.id),
                func.max(SqlComment.updated_at),
            )
            .where(
                SqlComment.workspace_id == current_workspace_id(),
                SqlComment.conversation_id.in_(conversation_ids),
            )
            .group_by(SqlComment.conversation_id)
        )
        with self._session() as session:
            return {
                conversation_id: CommentsFingerprint(count=count, last_updated_at=last_updated)
                for conversation_id, count, last_updated in session.execute(stmt)
            }

    def remove_conversation(self, conversation_id: str) -> None:
        """Delete all comments for a conversation. See base class for contract."""
        stmt = delete(SqlComment).where(
            SqlComment.workspace_id == current_workspace_id(),
            SqlComment.conversation_id == conversation_id,
        )
        with self._session() as session:
            session.execute(stmt)
