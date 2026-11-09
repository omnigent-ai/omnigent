"""SQLAlchemy-backed Inbox item store."""

from __future__ import annotations

import heapq
import json
import uuid
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from omnigent.db.db_models import INBOX_ITEMS_PER_SESSION, SqlInboxItem, current_workspace_id
from omnigent.db.enum_codecs import (
    decode_inbox_item_kind,
    decode_inbox_item_resolution,
    decode_inbox_item_state,
    encode_inbox_item_kind,
    encode_inbox_item_resolution,
    encode_inbox_item_state,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import InboxItem
from omnigent.stores.inbox_item_store import (
    DuplicateInboxItemError,
    InboxItemStore,
    InboxItemTooLargeError,
    InboxSessionCapError,
    NewInboxItem,
)

_OPEN_STATE_CODES = frozenset(encode_inbox_item_state(state) for state in ("unread", "read"))
# Retries after a concurrent post took the same session slot.
_SLOT_RETRIES = 3
_T = TypeVar("_T")


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
        actions=json.loads(row.actions),
        chart=json.loads(row.chart) if row.chart is not None else None,
        primary_link=row.primary_link,
        topic=row.topic,
        dedupe_key=row.dedupe_key,
        state=decode_inbox_item_state(row.state),
        resolution=decode_inbox_item_resolution(row.resolution),
        feedback_text=row.feedback_text,
        created_at=row.created_at,
        read_at=row.read_at,
        acted_at=row.acted_at,
        expires_at=row.expires_at,
        resolution_action=row.resolution_action,
    )


def _sized(write: Callable[[], _T]) -> _T:
    """Run *write*, turning a stored-size overflow into :class:`InboxItemTooLargeError`."""
    try:
        return write()
    except StatementError as exc:
        if isinstance(exc.orig, ValueError):
            raise InboxItemTooLargeError(str(exc.orig)) from exc
        raise


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
        """Load an item by primary key, treating another owner's item as missing."""
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
        for attempt in range(_SLOT_RETRIES + 1):
            try:
                return _sized(lambda: self._insert(item, retire_item_id))
            except IntegrityError as exc:
                # Either a concurrent post took our slot, or another item holds the key.
                if self.get_by_dedupe_key(item.user_id, item.dedupe_key) is not None:
                    raise DuplicateInboxItemError(item.dedupe_key) from exc
                if attempt == _SLOT_RETRIES:
                    raise
        raise AssertionError("unreachable")

    def _insert(self, item: NewInboxItem, retire_item_id: str | None) -> InboxItem:
        item_id = uuid.uuid4().hex
        created_at = now_epoch()
        used_slots = (
            select(SqlInboxItem.session_slot)
            .where(
                SqlInboxItem.workspace_id == current_workspace_id(),
                SqlInboxItem.user_id == item.user_id,
                SqlInboxItem.source_session_id == item.source_session_id,
            )
            .limit(INBOX_ITEMS_PER_SESSION)
        )

        def write(session: Session) -> InboxItem:
            taken = set(session.execute(used_slots).scalars())
            free = [slot for slot in range(INBOX_ITEMS_PER_SESSION) if slot not in taken]
            if not free:
                raise InboxSessionCapError(item.source_session_id)
            if retire_item_id is not None:
                retired = self._owned_row(session, retire_item_id, item.user_id)
                if retired is not None and retired.dedupe_key == item.dedupe_key:
                    retired.dedupe_key = f"{item.dedupe_key}#{retired.id}"
                    session.flush()
            row = SqlInboxItem(
                id=item_id,
                user_id=item.user_id,
                source_session_id=item.source_session_id,
                session_slot=free[0],
                agent_id=item.agent_id,
                prompt_version=item.prompt_version,
                kind=encode_inbox_item_kind(item.kind),
                title=item.title,
                body_md=item.body_md,
                links=json.dumps(item.links),
                actions=json.dumps(item.actions),
                chart=json.dumps(item.chart) if item.chart is not None else None,
                primary_link=item.primary_link,
                topic=item.topic,
                dedupe_key=item.dedupe_key,
                state=encode_inbox_item_state("unread"),
                resolution=encode_inbox_item_resolution("none"),
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
            # A row-value comparison seeks the index instead of filtering earlier history.
            stmt = stmt.where(tuple_(SqlInboxItem.created_at, SqlInboxItem.id) < after)
        stmt = stmt.order_by(SqlInboxItem.created_at.desc(), SqlInboxItem.id.desc()).limit(limit)
        return list(session.execute(stmt).scalars().all())

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
            row = self._owned_row(session, item_id, user_id)
            if row is None:
                return None
            if state is not None:
                new_code = encode_inbox_item_state(state)
                if new_code != encode_inbox_item_state("unread") and row.read_at is None:
                    row.read_at = now
                # A read or unread mark never reopens a resolved item; only a
                # resolution of "none" does, and it sets the state itself.
                if resolution is not None or row.state in _OPEN_STATE_CODES:
                    row.state = new_code
            if resolution is not None:
                row.resolution = encode_inbox_item_resolution(resolution)
                row.resolution_action = resolution_action
                if resolution != "none":
                    row.acted_at = now
            if feedback_text is not None:
                row.feedback_text = feedback_text
            session.flush()
            return _to_entity(row)

        return _sized(
            lambda: run_write_transaction(self._session_immediate, "update_inbox_item", write)
        )
