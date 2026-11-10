"""SQLAlchemy-backed Inbox item store."""

from __future__ import annotations

import heapq
import uuid
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, tuple_
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlInboxItem, current_workspace_id
from omnigent.db.enum_codecs import decode_inbox_item_state, encode_inbox_item_state
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import InboxItem
from omnigent.stores.inbox_item_store import (
    INBOX_ITEMS_PER_SESSION,
    DuplicateInboxItemError,
    InboxItemStore,
    InboxItemTooLargeError,
    InboxSessionCapError,
    NewInboxItem,
)

_OPEN_STATE_CODES = frozenset(encode_inbox_item_state(state) for state in ("unread", "read"))
_T = TypeVar("_T")


def _answer(stored: dict[str, Any] | None) -> tuple[str, int | None, str | None]:
    """Unpack a stored resolution envelope into its reason, action index, and comment."""
    if stored is None:
        return "none", None, None
    return stored.get("reason", "none"), stored.get("action"), stored.get("text")


def _resolution_envelope(
    reason: str, action: int | None, text: str | None
) -> dict[str, Any] | None:
    """The stored resolution, or ``None`` while the owner has not answered."""
    if reason == "none" and text is None:
        return None
    stored: dict[str, Any] = {"reason": reason}
    if action is not None:
        stored["action"] = action
    if text is not None:
        stored["text"] = text
    return stored


def _to_entity(row: SqlInboxItem) -> InboxItem:
    """Convert an ORM row to an :class:`InboxItem`."""
    content = row.content
    reason, action, text = _answer(row.resolution)
    return InboxItem(
        id=row.id,
        user_id=row.user_id,
        source_session_id=row.source_session_id,
        agent_id=row.agent_id,
        prompt_version=row.prompt_version,
        kind=content["kind"],
        title=content["title"],
        body_md=content["body_md"],
        links=content["links"],
        actions=content["actions"],
        media=content["media"],
        primary_link=content["primary_link"],
        topic=content["topic"],
        dedupe_key=row.dedupe_key,
        state=decode_inbox_item_state(row.state),
        resolution=reason,
        feedback_text=text,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        expires_at=row.expires_at,
        resolution_action=action,
    )


def _sized(write: Callable[[], _T]) -> _T:
    """Run *write*, turning a stored-size overflow into :class:`InboxItemTooLargeError`."""
    try:
        return write()
    except StatementError as exc:
        if isinstance(exc.orig, ValueError):
            raise InboxItemTooLargeError(str(exc.orig)) from exc
        raise


def _expired(item: InboxItem) -> bool:
    return item.expires_at is not None and item.expires_at <= now_epoch()


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

    def _owned_row(
        self, session: Session, item_id: str, user_id: str, *, for_update: bool = False
    ) -> SqlInboxItem | None:
        """Load an item by primary key, treating another owner's item as missing.

        ``for_update`` locks the row, so concurrent updates apply one after the
        other and each sees the state the previous one committed.
        """
        row = session.get(
            SqlInboxItem, (current_workspace_id(), item_id), with_for_update=for_update
        )
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
        for attempt in range(2):
            try:
                return _sized(partial(self._insert, item, retire_item_id))
            except InboxSessionCapError:
                # A full session reports a repeat of one of its items as a duplicate.
                if self._live_duplicate(item, retire_item_id) is not None:
                    raise DuplicateInboxItemError(item.dedupe_key) from None
                raise
            except IntegrityError as exc:
                # Another item holds the key.
                holder = self.get_by_dedupe_key(item.user_id, item.dedupe_key)
                if holder is None or holder.id == retire_item_id or attempt == 1:
                    raise
                if not _expired(holder):
                    raise DuplicateInboxItemError(item.dedupe_key) from exc
                # The holder expired, so this post replaces it, even when it was
                # inserted after the caller's duplicate check.
                retire_item_id = holder.id
        raise AssertionError("unreachable")

    def _live_duplicate(self, item: NewInboxItem, retire_item_id: str | None) -> InboxItem | None:
        """The item holding *item*'s key, unless it is the expired item being replaced."""
        existing = self.get_by_dedupe_key(item.user_id, item.dedupe_key)
        if existing is None or existing.id == retire_item_id or _expired(existing):
            return None
        return existing

    def _insert(self, item: NewInboxItem, retire_item_id: str | None) -> InboxItem:
        item_id = uuid.uuid4().hex
        created_at = now_epoch()
        # Counting stops at the cap, so a session's row count never grows the query.
        posted = (
            select(SqlInboxItem.id)
            .where(
                SqlInboxItem.workspace_id == current_workspace_id(),
                SqlInboxItem.user_id == item.user_id,
                SqlInboxItem.source_session_id == item.source_session_id,
            )
            .limit(INBOX_ITEMS_PER_SESSION)
            .subquery()
        )
        count_posted = select(func.count()).select_from(posted)

        def write(session: Session) -> InboxItem:
            require_active_account(session, item.user_id)
            if session.execute(count_posted).scalar_one() >= INBOX_ITEMS_PER_SESSION:
                raise InboxSessionCapError(item.source_session_id)
            if retire_item_id is not None:
                retired = self._owned_row(session, retire_item_id, item.user_id, for_update=True)
                if retired is not None and retired.dedupe_key == item.dedupe_key:
                    retired.dedupe_key = f"{item.dedupe_key}#{retired.id}"
                    session.flush()
            row = SqlInboxItem(
                id=item_id,
                user_id=item.user_id,
                source_session_id=item.source_session_id,
                agent_id=item.agent_id,
                prompt_version=item.prompt_version,
                dedupe_key=item.dedupe_key,
                state=encode_inbox_item_state("unread"),
                content={
                    "kind": item.kind,
                    "title": item.title,
                    "body_md": item.body_md,
                    "links": item.links,
                    "actions": item.actions,
                    "topic": item.topic,
                    "primary_link": item.primary_link,
                    "media": item.media,
                },
                created_at=created_at,
                expires_at=item.expires_at,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_inbox_item", write)

    def _page(
        self,
        session: Session,
        user_id: str,
        *,
        state: str | None,
        created_after: int | None,
        after: tuple[int, str] | None,
        limit: int,
    ) -> list[SqlInboxItem]:
        """One newest-first page over the owner's items, optionally in one state.

        With a state, the filter and order match ``ix_inbox_items_user_state_created``;
        without, ``ix_inbox_items_user_created``. Either way the scan is O(limit).
        """
        stmt: Select[tuple[SqlInboxItem]] = select(SqlInboxItem).where(
            SqlInboxItem.workspace_id == current_workspace_id(),
            SqlInboxItem.user_id == user_id,
        )
        if state is not None:
            stmt = stmt.where(SqlInboxItem.state == encode_inbox_item_state(state))
        if created_after is not None:
            stmt = stmt.where(SqlInboxItem.created_at >= created_after)
        if after is not None:
            stmt = stmt.where(self._before_cursor(after))
        stmt = stmt.order_by(SqlInboxItem.created_at.desc(), SqlInboxItem.id.desc()).limit(limit)
        return list(session.execute(stmt).scalars().all())

    def _before_cursor(self, after: tuple[int, str]) -> ColumnElement[bool]:
        """Rows older than the ``(created_at, id)`` cursor, as a predicate that seeks.

        A row-value comparison seeks the index on most engines; MySQL needs it
        spelled as scalar ranges, as in the scheduled-task store.
        """
        created_at, item_id = after
        if self._engine.dialect.name != "mysql":
            return tuple_(SqlInboxItem.created_at, SqlInboxItem.id) < after
        return or_(
            SqlInboxItem.created_at < created_at,
            and_(SqlInboxItem.created_at == created_at, SqlInboxItem.id < item_id),
        )

    def list_for_user(
        self,
        user_id: str,
        *,
        states: frozenset[str] | None = None,
        created_after: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[InboxItem], tuple[int, str] | None]:
        """See base class.

        Several states are read as one bounded page per state, then merged.
        """
        page_states: list[str | None] = [*sorted(states)] if states else [None]
        with self._session("list_inbox_items") as session:
            pages = [
                self._page(
                    session,
                    user_id,
                    state=state,
                    created_after=created_after,
                    after=after,
                    limit=limit + 1,
                )
                for state in page_states
            ]
            merged = heapq.merge(*pages, key=lambda row: (row.created_at, row.id), reverse=True)
            rows = [row for _, row in zip(range(limit + 1), merged, strict=False)]
            items = [_to_entity(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and items:
            next_cursor = (items[-1].created_at, items[-1].id)
        return items, next_cursor

    def count_unread(self, user_id: str, *, limit: int) -> int:
        """See base class."""
        bounded = (
            select(SqlInboxItem.id)
            .where(
                SqlInboxItem.workspace_id == current_workspace_id(),
                SqlInboxItem.user_id == user_id,
                SqlInboxItem.state == encode_inbox_item_state("unread"),
            )
            .limit(limit)
            .subquery()
        )
        with self._session("count_unread_inbox_items") as session:
            return int(session.execute(select(func.count()).select_from(bounded)).scalar_one())

    def update(
        self,
        item_id: str,
        user_id: str,
        *,
        state: str | None = None,
        resolution: str | None = None,
        resolution_action: int | None = None,
        feedback_text: str | None = None,
        now: int,
    ) -> InboxItem | None:
        """See base class."""

        def write(session: Session) -> InboxItem | None:
            require_active_account(session, user_id)
            row = self._owned_row(session, item_id, user_id, for_update=True)
            if row is None:
                return None
            reason, action, text = _answer(row.resolution)
            # A read or unread mark never reopens a resolved item; only a
            # resolution of "none" does, and it sets the state itself.
            if state is not None and (resolution is not None or row.state in _OPEN_STATE_CODES):
                row.state = encode_inbox_item_state(state)
            if resolution is not None:
                reason, action = resolution, resolution_action
                if resolution != "none":
                    row.resolved_at = now
            if feedback_text is not None:
                text = feedback_text
            row.resolution = _resolution_envelope(reason, action, text)
            session.flush()
            return _to_entity(row)

        return _sized(
            lambda: run_write_transaction(self._session_immediate, "update_inbox_item", write)
        )
