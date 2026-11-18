"""SQLAlchemy-backed comments store."""

from __future__ import annotations

import json
import uuid
from typing import Literal, cast

from sqlalchemy import and_, delete, func, or_, select, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlComment, SqlCommentReply, current_workspace_id
from omnigent.db.enum_codecs import (
    decode_comment_reply_author_kind,
    decode_comment_status,
    encode_comment_reply_author_kind,
    encode_comment_status,
)
from omnigent.db.utils import (
    NamedManagedSessionMaker,
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)
from omnigent.entities import Comment, CommentsFingerprint
from omnigent.entities.comment import (
    CommentReply,
    CommentReplyAuthorKind,
    CommentReplySummary,
    PullRequestCommentAnchor,
)
from omnigent.stores.comment_store import (
    AGENT_COMMENT_REPLY_WINDOW,
    MAX_COMMENT_PAGE_SIZE,
    MAX_REPLIES_PER_COMMENT,
    CommentReplyLimitError,
    CommentStore,
    serialize_comment_anchor,
    validate_reply_body,
)


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
        reply_count=row.reply_count,
        last_reply=CommentReplySummary(
            author_kind=cast(
                CommentReplyAuthorKind,
                decode_comment_reply_author_kind(row.last_reply_author_kind),
            ),
            created_by=row.last_reply_created_by,
            created_at=row.last_reply_created_at,
        )
        if row.last_reply_author_kind is not None and row.last_reply_created_at is not None
        else None,
        anchor_metadata=PullRequestCommentAnchor(**json.loads(row.anchor_metadata))
        if row.anchor_metadata
        else None,
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
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.comment_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.comment_store",
            immediate=True,
        )

    def get(self, comment_id: str, conversation_id: str) -> Comment | None:
        """Fetch a single comment by id, scoped to a conversation. See base class for contract."""
        with self._session("select_comment_by_id") as session:
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
        anchor_metadata: PullRequestCommentAnchor | None = None,
    ) -> Comment:
        """Create and persist a new comment. See base class for contract."""
        # One clock read for both timestamps so a never-edited comment's
        # updated_at (µs, fingerprint precision) and created_at (seconds,
        # display) describe the same instant — the invariant the migration
        # backfill (created_at * 1e6) and docs rely on.
        created_us = now_epoch_us()
        comment_id = uuid.uuid4().hex
        serialized_anchor = serialize_comment_anchor(anchor_metadata)

        def write(session: Session) -> Comment:
            row = SqlComment(
                id=comment_id,
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
                anchor_metadata=serialized_anchor,
                reply_count=0,
            )
            session.add(row)
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_comment", write)

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
        with self._session("list_comments") as session:
            rows = list(session.execute(stmt).scalars().all())
            return [_to_entity(r) for r in rows]

    def list_page(
        self,
        conversation_id: str,
        *,
        limit: int = MAX_COMMENT_PAGE_SIZE,
        after_id: str | None = None,
    ) -> tuple[list[Comment], str | None]:
        """Read a bounded session page. See base class for contract."""
        if not 1 <= limit <= MAX_COMMENT_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_COMMENT_PAGE_SIZE}")
        stmt = select(SqlComment).where(
            SqlComment.workspace_id == current_workspace_id(),
            SqlComment.conversation_id == conversation_id,
        )
        if after_id is not None:
            stmt = stmt.where(SqlComment.id > after_id)
        # The workspace/conversation/id primary key supports this range and order.
        stmt = stmt.order_by(SqlComment.id).limit(limit + 1)
        with self._session("list_comment_page") as session:
            rows = list(session.execute(stmt).scalars().all())
            page = rows[:limit]
            next_cursor = page[-1].id if len(rows) > limit else None
            return [_to_entity(row) for row in page], next_cursor

    def update_comment(
        self,
        comment_id: str,
        conversation_id: str,
        *,
        status: str | None = None,
        body: str | None = None,
    ) -> Comment | None:
        """Update a comment's fields, scoped to a conversation. See base class for contract."""

        def write(session: Session) -> Comment | None:
            row = session.get(
                SqlComment,
                (current_workspace_id(), conversation_id, comment_id),
                with_for_update=True,
            )
            if row is None:
                return None
            if status is not None:
                row.status = encode_comment_status(status)
            if body is not None:
                row.body = body
            if status is not None or body is not None:
                row.updated_at = max(now_epoch_us(), row.updated_at + 1)
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_comment", write)

    def delete(self, comment_id: str, conversation_id: str) -> Comment | None:
        """Delete a single comment by id, scoped to a conversation. See base class for contract."""

        def write(session: Session) -> Comment | None:
            row = session.get(
                SqlComment,
                (current_workspace_id(), conversation_id, comment_id),
                with_for_update=True,
            )
            if row is None:
                return None
            entity = _to_entity(row)
            session.execute(
                delete(SqlCommentReply).where(*self._thread_scope(comment_id, conversation_id))
            )
            session.delete(row)
            return entity

        return run_write_transaction(self._session_immediate, "delete_comment", write)

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
        with self._session("select_comment_fingerprints") as session:
            return {
                conversation_id: CommentsFingerprint(count=count, last_updated_at=last_updated)
                for conversation_id, count, last_updated in session.execute(stmt)
            }

    @staticmethod
    def _thread_scope(comment_id: str, conversation_id: str) -> tuple:
        """Return the complete tenant/thread prefix."""
        return (
            SqlCommentReply.workspace_id == current_workspace_id(),
            SqlCommentReply.conversation_id == conversation_id,
            SqlCommentReply.comment_id == comment_id,
        )

    def add_reply(
        self,
        comment_id: str,
        conversation_id: str,
        body: str,
        *,
        author_kind: str = "user",
        created_by: str | None = None,
    ) -> CommentReply | None:
        """Serialize reply appends with parent deletion and enforce the thread cap."""
        validate_reply_body(body)
        author_code = encode_comment_reply_author_kind(author_kind)
        if author_kind == "agent" and created_by is not None:
            raise ValueError("Agent replies must not have a user identity")
        if created_by is not None and (len(created_by) > 128 or len(created_by.encode()) > 512):
            raise ValueError("Reply author exceeds 128 characters or 512 UTF-8 bytes")

        def write(session: Session) -> CommentReply | None:
            parent = session.get(
                SqlComment,
                (current_workspace_id(), conversation_id, comment_id),
                with_for_update=True,
            )
            if parent is None:
                return None
            if parent.reply_count >= MAX_REPLIES_PER_COMMENT:
                raise CommentReplyLimitError(
                    f"A comment can have at most {MAX_REPLIES_PER_COMMENT} replies"
                )
            created_at = max(now_epoch_us(), parent.updated_at + 1)
            row = SqlCommentReply(
                workspace_id=current_workspace_id(),
                conversation_id=conversation_id,
                comment_id=comment_id,
                id=uuid.uuid4().hex,
                author_kind=author_code,
                body=body,
                created_by=created_by,
                created_at=created_at,
            )
            session.add(row)
            parent.updated_at = created_at
            parent.reply_count += 1
            parent.last_reply_author_kind = author_code
            parent.last_reply_created_by = created_by
            parent.last_reply_created_at = created_at
            return _reply_entity(row)

        return run_write_transaction(self._session_immediate, "append_reply", write)

    def get_reply(
        self, comment_id: str, conversation_id: str, reply_id: str
    ) -> CommentReply | None:
        """Fetch only within the full thread primary key."""
        with self._session("read_reply") as session:
            row = session.get(
                SqlCommentReply, (current_workspace_id(), conversation_id, comment_id, reply_id)
            )
            return _reply_entity(row) if row else None

    def list_replies(
        self,
        comment_id: str,
        conversation_id: str,
        *,
        limit: int = 50,
        after_created_at: int | None = None,
        after_id: str | None = None,
        order: Literal["asc", "desc"] = "asc",
    ) -> tuple[list[CommentReply], tuple[int, str] | None]:
        """Page on the thread index, with ID breaking equal timestamp ties."""
        if order not in ("asc", "desc"):
            raise ValueError("order must be asc or desc")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if (after_created_at is None) != (after_id is None):
            raise ValueError("after_created_at and after_id must be supplied together")
        stmt = select(SqlCommentReply).where(*self._thread_scope(comment_id, conversation_id))
        if after_created_at is not None:
            stmt = stmt.where(
                or_(
                    (SqlCommentReply.created_at > after_created_at)
                    if order == "asc"
                    else (SqlCommentReply.created_at < after_created_at),
                    and_(
                        SqlCommentReply.created_at == after_created_at,
                        (SqlCommentReply.id > after_id)
                        if order == "asc"
                        else (SqlCommentReply.id < after_id),
                    ),
                )
            )
        ordering = (
            (SqlCommentReply.created_at, SqlCommentReply.id)
            if order == "asc"
            else (SqlCommentReply.created_at.desc(), SqlCommentReply.id.desc())
        )
        stmt = stmt.order_by(*ordering).limit(limit + 1)
        with self._session("page_replies") as session:
            rows = list(session.execute(stmt).scalars())
            page = rows[:limit]
            cursor = (page[-1].created_at, page[-1].id) if len(rows) > limit else None
            return [_reply_entity(row) for row in page], cursor

    def recent_replies(
        self, comment_id: str, conversation_id: str, *, limit: int = AGENT_COMMENT_REPLY_WINDOW
    ) -> list[CommentReply]:
        """Read the latest agent-context window and restore chronological order."""
        if not 1 <= limit <= MAX_REPLIES_PER_COMMENT:
            raise ValueError(f"limit must be between 1 and {MAX_REPLIES_PER_COMMENT}")
        stmt = (
            select(SqlCommentReply)
            .where(*self._thread_scope(comment_id, conversation_id))
            .order_by(SqlCommentReply.created_at.desc(), SqlCommentReply.id.desc())
            .limit(limit)
        )
        with self._session("recent_replies") as session:
            return [_reply_entity(row) for row in reversed(list(session.execute(stmt).scalars()))]

    def delete_reply(self, comment_id: str, conversation_id: str, reply_id: str) -> bool:
        """Lock the parent before deleting, matching append's lock order."""

        def write(session: Session) -> bool:
            parent = session.get(
                SqlComment,
                (current_workspace_id(), conversation_id, comment_id),
                with_for_update=True,
            )
            if parent is None:
                return False
            row = session.get(
                SqlCommentReply, (current_workspace_id(), conversation_id, comment_id, reply_id)
            )
            if row is None:
                return False
            session.delete(row)
            parent.reply_count -= 1
            if row.created_at == parent.last_reply_created_at:
                latest = session.execute(
                    select(
                        SqlCommentReply.author_kind,
                        SqlCommentReply.created_by,
                        SqlCommentReply.created_at,
                    )
                    .where(
                        *self._thread_scope(comment_id, conversation_id),
                        SqlCommentReply.id != reply_id,
                    )
                    .order_by(SqlCommentReply.created_at.desc(), SqlCommentReply.id.desc())
                    .limit(1)
                    .with_for_update()
                ).first()
                parent.last_reply_author_kind = latest.author_kind if latest else None
                parent.last_reply_created_by = latest.created_by if latest else None
                parent.last_reply_created_at = latest.created_at if latest else None
            parent.updated_at = max(now_epoch_us(), parent.updated_at + 1)
            return True

        return run_write_transaction(self._session_immediate, "delete_reply", write)

    def remove_conversation(self, conversation_id: str) -> None:
        """Delete all comments for a conversation. See base class for contract."""
        stmt = delete(SqlComment).where(
            SqlComment.workspace_id == current_workspace_id(),
            SqlComment.conversation_id == conversation_id,
        )

        def write(session: Session) -> None:
            session.execute(stmt)

        run_write_transaction(self._session_immediate, "delete_conversation_comments", write)
        delete_conversation_replies(self._session_immediate, conversation_id)


def _reply_entity(row: SqlCommentReply) -> CommentReply:
    """Keep storage author codes and encryption envelopes private."""
    return CommentReply(
        id=row.id,
        comment_id=row.comment_id,
        conversation_id=row.conversation_id,
        author_kind=cast(
            CommentReplyAuthorKind, decode_comment_reply_author_kind(row.author_kind)
        ),
        body=row.body,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def delete_conversation_replies(
    session_maker: NamedManagedSessionMaker,
    conversation_id: str,
) -> None:
    """Remove a conversation's replies in bounded primary-key prefix transactions."""
    # Each transaction deletes at most one bounded primary-key prefix page.
    while True:

        def remove_batch(session: Session) -> int:
            scope = (
                SqlCommentReply.workspace_id == current_workspace_id(),
                SqlCommentReply.conversation_id == conversation_id,
            )
            keys = session.execute(
                select(SqlCommentReply.comment_id, SqlCommentReply.id)
                .where(*scope)
                .order_by(SqlCommentReply.comment_id, SqlCommentReply.id)
                .limit(MAX_COMMENT_PAGE_SIZE)
            ).all()
            if keys:
                result = cast(
                    CursorResult,
                    session.execute(
                        delete(SqlCommentReply).where(
                            *scope,
                            tuple_(SqlCommentReply.comment_id, SqlCommentReply.id).in_(keys),
                        )
                    ),
                )
                if result.rowcount == 0:
                    return 0
            return len(keys)

        if (
            run_write_transaction(session_maker, "remove_reply_batch", remove_batch)
            < MAX_COMMENT_PAGE_SIZE
        ):
            break
