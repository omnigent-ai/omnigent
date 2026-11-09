"""SQLAlchemy-backed Inbox item store."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlInboxItem, current_workspace_id
from omnigent.db.enum_codecs import (
    decode_inbox_item_feedback,
    decode_inbox_item_kind,
    decode_inbox_item_state,
    encode_inbox_item_feedback,
    encode_inbox_item_kind,
    encode_inbox_item_state,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import InboxItem
from omnigent.stores.inbox_item_store import DuplicateInboxItemError, InboxItemStore, NewInboxItem

_ACTED_STATES = frozenset({"done", "dismissed"})


def _to_entity(row: SqlInboxItem) -> InboxItem:
    """Convert an ORM row to an :class:`InboxItem`."""
    return InboxItem(
        id=row.id,
        user_id=row.user_id,
        source_session_id=row.source_session_id,
        agent_id=row.agent_id,
        prompt_version=row.prompt_version,
        kind=decode_inbox_item_kind(row.kind),
        title=row.title,
        body_md=row.body_md,
        links=json.loads(row.links),
        suggested_prompt=row.suggested_prompt,
        primary_link=row.primary_link,
        topic=row.topic,
        dedupe_key=row.dedupe_key,
        state=decode_inbox_item_state(row.state),
        feedback=decode_inbox_item_feedback(row.feedback),
        feedback_text=row.feedback_text,
        created_at=row.created_at,
        read_at=row.read_at,
        acted_at=row.acted_at,
        expires_at=row.expires_at,
    )


class SqlAlchemyInboxItemStore(InboxItemStore):
    """SQLAlchemy-backed implementation of :class:`InboxItemStore`."""

    def __init__(self, storage_location: str) -> None:
        """Initialize the store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.inbox_item_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.inbox_item_store",
            immediate=True,
        )

    def _owned_row(self, session: Session, item_id: str, user_id: str) -> SqlInboxItem | None:
        row = session.get(SqlInboxItem, (current_workspace_id(), item_id))
        if row is None or row.user_id != user_id:
            return None
        return row

    def get(self, item_id: str, user_id: str) -> InboxItem | None:
        """See base class."""
        with self._session("select_inbox_item") as session:
            row = self._owned_row(session, item_id, user_id)
            return _to_entity(row) if row is not None else None

    def get_by_dedupe_key(self, user_id: str, dedupe_key: str) -> InboxItem | None:
        """See base class."""
        stmt = select(SqlInboxItem).where(
            SqlInboxItem.workspace_id == current_workspace_id(),
            SqlInboxItem.user_id == user_id,
            SqlInboxItem.dedupe_key == dedupe_key,
        )
        with self._session("select_inbox_item_by_dedupe_key") as session:
            row = session.execute(stmt).scalars().first()
            return _to_entity(row) if row is not None else None

    def create(self, item: NewInboxItem, *, retire_item_id: str | None = None) -> InboxItem:
        """See base class."""
        item_id = uuid.uuid4().hex
        created_at = now_epoch()

        def write(session: Session) -> InboxItem:
            if retire_item_id is not None:
                retired = self._owned_row(session, retire_item_id, item.user_id)
                if retired is not None and retired.dedupe_key == item.dedupe_key:
                    retired.dedupe_key = f"{item.dedupe_key}#{retired.id}"
                    session.flush()
            row = SqlInboxItem(
                id=item_id,
                user_id=item.user_id,
                source_session_id=item.source_session_id,
                agent_id=item.agent_id,
                prompt_version=item.prompt_version,
                kind=encode_inbox_item_kind(item.kind),
                title=item.title,
                body_md=item.body_md,
                links=json.dumps(item.links),
                suggested_prompt=item.suggested_prompt,
                primary_link=item.primary_link,
                topic=item.topic,
                dedupe_key=item.dedupe_key,
                state=encode_inbox_item_state("unread"),
                feedback=encode_inbox_item_feedback("none"),
                created_at=created_at,
                expires_at=item.expires_at,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        try:
            return run_write_transaction(self._session_immediate, "insert_inbox_item", write)
        except IntegrityError as exc:
            raise DuplicateInboxItemError(item.dedupe_key) from exc

    def list_for_user(
        self,
        user_id: str,
        *,
        states: frozenset[str] | None = None,
        created_after: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[InboxItem], tuple[int, str] | None]:
        """See base class."""
        stmt = select(SqlInboxItem).where(
            SqlInboxItem.workspace_id == current_workspace_id(),
            SqlInboxItem.user_id == user_id,
        )
        if states is not None:
            stmt = stmt.where(SqlInboxItem.state.in_([encode_inbox_item_state(s) for s in states]))
        if created_after is not None:
            stmt = stmt.where(SqlInboxItem.created_at >= created_after)
        if after is not None:
            cursor_created_at, cursor_id = after
            stmt = stmt.where(
                or_(
                    SqlInboxItem.created_at < cursor_created_at,
                    and_(
                        SqlInboxItem.created_at == cursor_created_at,
                        SqlInboxItem.id < cursor_id,
                    ),
                )
            )
        stmt = stmt.order_by(SqlInboxItem.created_at.desc(), SqlInboxItem.id.desc()).limit(
            limit + 1
        )
        with self._session("list_inbox_items") as session:
            rows = list(session.execute(stmt).scalars().all())
            items = [_to_entity(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and items:
            next_cursor = (items[-1].created_at, items[-1].id)
        return items, next_cursor

    def count_unread(self, user_id: str) -> int:
        """See base class."""
        stmt = select(func.count(SqlInboxItem.id)).where(
            SqlInboxItem.workspace_id == current_workspace_id(),
            SqlInboxItem.user_id == user_id,
            SqlInboxItem.state == encode_inbox_item_state("unread"),
        )
        with self._session("count_unread_inbox_items") as session:
            return int(session.execute(stmt).scalar_one())

    def count_for_session(self, user_id: str, source_session_id: str) -> int:
        """See base class."""
        stmt = select(func.count(SqlInboxItem.id)).where(
            SqlInboxItem.workspace_id == current_workspace_id(),
            SqlInboxItem.user_id == user_id,
            SqlInboxItem.source_session_id == source_session_id,
        )
        with self._session("count_session_inbox_items") as session:
            return int(session.execute(stmt).scalar_one())

    def update(
        self,
        item_id: str,
        user_id: str,
        *,
        state: str | None = None,
        feedback: str | None = None,
        feedback_text: str | None = None,
        now: int,
    ) -> InboxItem | None:
        """See base class."""

        def write(session: Session) -> InboxItem | None:
            row = self._owned_row(session, item_id, user_id)
            if row is None:
                return None
            if state is not None:
                new_code = encode_inbox_item_state(state)
                if new_code != encode_inbox_item_state("unread") and row.read_at is None:
                    row.read_at = now
                if state in _ACTED_STATES and new_code != row.state:
                    row.acted_at = now
                row.state = new_code
            if feedback is not None:
                row.feedback = encode_inbox_item_feedback(feedback)
                row.acted_at = now
            if feedback_text is not None:
                row.feedback_text = feedback_text
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_inbox_item", write)
