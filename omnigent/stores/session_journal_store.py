"""Persistent store of each session's append-only journal.

Entries are rows of ``session_journal_entries`` in the database that holds the
session's metadata. An append locks the session's ``session_journal_heads`` row,
which allocates positions, so one session's appends run one at a time while
other sessions' appends and the session's metadata updates don't wait. An
append carries an idempotency key, so a retried append returns the entry its
first attempt wrote instead of writing it twice.

Payloads are the kind's compressed JSON, at most
:data:`~omnigent.db.db_models.MAX_STORED_VALUE_BYTES` bytes stored. The store
checks the size before writing, so an agent gets ``entry_too_large``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from sqlalchemy import Select, delete, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from omnigent.db import compression
from omnigent.db.db_models import (
    MAX_STORED_VALUE_BYTES,
    SqlConversationMetadata,
    SqlSessionJournalEntry,
    SqlSessionJournalHead,
    current_workspace_id,
)
from omnigent.db.enum_codecs import decode_journal_entry_kind, encode_journal_entry_kind
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.session_journal import (
    IDEMPOTENCY_KEY_MAX_CHARS,
    JOURNAL_KINDS,
    JOURNAL_PAGE_MAX,
    STATUSES_NEEDING_DEPENDENCIES,
    InvalidJournalEntryError,
    JournalAppend,
    JournalEntry,
    JournalEntryTooLargeError,
    JournalPage,
    StepNotReadyError,
    normalize_payload,
    normalize_step_id,
)
from omnigent.errors import StaleCursorError
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

# Headroom for compression framing on incompressible text.
_FRAMING_BYTES = 32
_PLAN_KIND = encode_journal_entry_kind("plan")
_STATUS_KIND = encode_journal_entry_kind("status")
# A fork doesn't own the parent's workers, so their dispatches stay behind.
_KINDS_A_FORK_LEAVES = frozenset(
    encode_journal_entry_kind(kind) for kind in ("dispatch", "dispatch_result")
)
# Session deletion removes at most this many entries per transaction.
JOURNAL_DELETE_BATCH = 500

_Statement = TypeVar("_Statement", bound=Select[Any])


def _using(statement: _Statement, index: str) -> _Statement:
    """Pin a journal read to *index* on MySQL.

    The kind and kind-and-step indexes share their leading columns, and MySQL
    can pick the wrong one and sort, or walk the primary key and filter; the
    index audit caught both. SQLite and Postgres choose correctly unaided.
    """
    return statement.with_hint(SqlSessionJournalEntry, f"FORCE INDEX ({index})", "mysql")


def _page_index(*, kind: bool, step: bool) -> str:
    """The index that serves a page with these filters in position order."""
    if kind and step:
        return "ix_session_journal_entries_kind_step"
    if kind:
        return "ix_session_journal_entries_kind"
    if step:
        return "ix_session_journal_entries_step"
    return "PRIMARY"


class JournalSessionNotFoundError(Exception):
    """The session doesn't exist, or was deleted while the append ran."""


def _stored_payload(payload: Mapping[str, Any]) -> str:
    """The payload's JSON, refused when it doesn't fit the column compressed."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) + _FRAMING_BYTES <= MAX_STORED_VALUE_BYTES:
        return text
    size = len(compression.encode(text) or b"")
    if size > MAX_STORED_VALUE_BYTES:
        raise JournalEntryTooLargeError("the stored entry", size, MAX_STORED_VALUE_BYTES)
    return text


def _entry_id(value: str, name: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError as exc:
        raise ValueError(f"{name} must be an entry ID") from exc


class SessionJournalStore:
    """SQLAlchemy-backed store of each session's journal."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy URI of the database holding
            ``omnigent_conversation_metadata``, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._dialect = engine.dialect.name
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_journal"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_journal", immediate=True
        )

    def _entries(self, rows: Sequence[SqlSessionJournalEntry]) -> list[JournalEntry]:
        """Decode rows into entries."""
        return [
            JournalEntry(
                id=row.id,
                conversation_id=row.conversation_id,
                position=row.position,
                created_at=row.created_at,
                kind=decode_journal_entry_kind(row.kind),
                step=row.step,
                scheduled_task_run_id=row.scheduled_task_run_id,
                payload=json.loads(row.payload),
            )
            for row in rows
        ]

    # ── Appends ───────────────────────────────────────

    def _lock_head(self, session: Session, conversation_id: str) -> int:
        """Lock the session's heads row, creating it on the first append.

        :returns: The position the next entry gets.
        """
        values = {
            "workspace_id": current_workspace_id(),
            "conversation_id": conversation_id,
            "next_position": 0,
        }
        if self._dialect == "mysql":
            statement = (
                mysql_insert(SqlSessionJournalHead)
                .values(**values)
                .on_duplicate_key_update(next_position=SqlSessionJournalHead.next_position)
            )
        elif self._dialect == "sqlite":
            statement = (
                sqlite_insert(SqlSessionJournalHead).values(**values).on_conflict_do_nothing()
            )
        else:
            statement = pg_insert(SqlSessionJournalHead).values(**values).on_conflict_do_nothing()
        session.execute(statement)
        # SQLite has no FOR UPDATE; its immediate transaction already holds the write lock.
        return session.execute(
            select(SqlSessionJournalHead.next_position)
            .where(
                SqlSessionJournalHead.workspace_id == current_workspace_id(),
                SqlSessionJournalHead.conversation_id == conversation_id,
            )
            .with_for_update()
        ).scalar_one()

    @staticmethod
    def _session_exists(session: Session, conversation_id: str) -> bool:
        """Whether the session's metadata row exists; a plain read that locks nothing."""
        return (
            session.execute(
                select(SqlConversationMetadata.id).where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id == conversation_id,
                )
            ).first()
            is not None
        )

    @staticmethod
    def _newest_plan_row(session: Session, conversation_id: str) -> SqlSessionJournalEntry | None:
        statement = (
            select(SqlSessionJournalEntry)
            .where(
                SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                SqlSessionJournalEntry.conversation_id == conversation_id,
                SqlSessionJournalEntry.kind == _PLAN_KIND,
            )
            .order_by(SqlSessionJournalEntry.position.desc())
            .limit(1)
        )
        return session.execute(
            _using(statement, "ix_session_journal_entries_kind")
        ).scalar_one_or_none()

    @staticmethod
    def _default_step(steps: list[dict[str, Any]] | None) -> str:
        """The step a status names when it names none: the only step of a one-step plan.

        :param steps: The current plan's steps, or None when the session has no plan.
        """
        if steps is None or len(steps) != 1:
            raise InvalidJournalEntryError(
                "step is required unless the current plan has exactly one step"
                if steps is not None
                else "step is required: the session has no plan"
            )
        return steps[0]["id"]

    def _unfinished_dependencies(
        self,
        session: Session,
        conversation_id: str,
        steps: list[dict[str, Any]] | None,
        step: str,
    ) -> list[str]:
        """The steps *step* depends on whose newest status isn't ``done``, in plan order.

        A step that isn't in the current plan, or a session with no plan, waits on
        nothing here.
        """
        current = next((item for item in steps or [] if item["id"] == step), None)
        waiting = []
        for dependency in current["depends_on"] if current else []:
            newest = session.execute(
                _using(
                    select(SqlSessionJournalEntry)
                    .where(
                        SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                        SqlSessionJournalEntry.conversation_id == conversation_id,
                        SqlSessionJournalEntry.kind == _STATUS_KIND,
                        SqlSessionJournalEntry.step == dependency,
                    )
                    .order_by(SqlSessionJournalEntry.position.desc())
                    .limit(1),
                    "ix_session_journal_entries_kind_step",
                )
            ).scalar_one_or_none()
            if newest is None or self._entries([newest])[0].payload.get("status") != "done":
                waiting.append(dependency)
        return waiting

    def append(
        self,
        conversation_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        step: str | None = None,
        scheduled_task_run_id: str | None = None,
    ) -> JournalAppend:
        """Append an entry to the session's journal.

        :param conversation_id: The session, e.g. ``"0123…cdef"``.
        :param kind: One of :data:`~omnigent.entities.session_journal.JOURNAL_KINDS`.
        :param payload: The kind's fields, checked with
            :func:`~omnigent.entities.session_journal.normalize_payload`. A plan's
            version is set here: the previous plan's version plus one.
        :param idempotency_key: Names the write, e.g. ``"tool:toolu_01…"``. A
            retry with the same key returns the entry the first attempt wrote.
        :param step: The plan step the entry is about. A status without one
            names the only step of a one-step plan; a plan never has one.
        :param scheduled_task_run_id: The automation run the session is serving.
        :returns: The entry, and whether this call appended it.
        :raises InvalidJournalEntryError: If a field is invalid.
        :raises JournalEntryTooLargeError: If the stored payload is too large.
        :raises StepNotReadyError: If a ``started`` or ``done`` status names a step
            whose dependencies in the current plan aren't all done.
        :raises JournalSessionNotFoundError: If the session doesn't exist.
        """
        fields = normalize_payload(kind, payload)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidJournalEntryError("idempotency_key must not be empty")
        if len(idempotency_key) > IDEMPOTENCY_KEY_MAX_CHARS:
            raise InvalidJournalEntryError(
                f"idempotency_key must be at most {IDEMPOTENCY_KEY_MAX_CHARS} characters"
            )
        if step is not None:
            if kind == "plan":
                raise InvalidJournalEntryError("a plan names no step; its steps are in the graph")
            step = normalize_step_id(step)
        # A plan's version is known only under the lock, so its size is checked there.
        stored = None if kind == "plan" else _stored_payload(fields)
        code = encode_journal_entry_kind(kind)

        def write(session: Session) -> tuple[SqlSessionJournalEntry, bool, dict[str, Any] | None]:
            position = self._lock_head(session, conversation_id)
            if not self._session_exists(session, conversation_id):
                raise JournalSessionNotFoundError(f"session {conversation_id!r} not found")
            earlier = session.execute(
                _using(
                    select(SqlSessionJournalEntry)
                    .where(
                        SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                        SqlSessionJournalEntry.conversation_id == conversation_id,
                        SqlSessionJournalEntry.idempotency_key == idempotency_key,
                    )
                    .order_by(SqlSessionJournalEntry.position)
                    .limit(1),
                    "ix_session_journal_entries_idempotency_key",
                )
            ).scalar_one_or_none()
            if earlier is not None:
                if earlier.kind != code:
                    raise InvalidJournalEntryError(
                        "idempotency_key was already used for a different kind of entry"
                    )
                return earlier, False, None
            entry_text, entry_payload = stored, fields
            if kind == "plan":
                previous = self._newest_plan_row(session, conversation_id)
                version = (
                    1 if previous is None else self._entries([previous])[0].payload["version"] + 1
                )
                entry_payload = {"version": version, **fields}
                entry_text = _stored_payload(entry_payload)
            entry_step = step
            if kind == "status":
                plan = self._newest_plan_row(session, conversation_id)
                steps = self._entries([plan])[0].payload["steps"] if plan is not None else None
                if entry_step is None:
                    entry_step = self._default_step(steps)
                if fields["status"] in STATUSES_NEEDING_DEPENDENCIES:
                    waiting = self._unfinished_dependencies(
                        session, conversation_id, steps, entry_step
                    )
                    if waiting:
                        raise StepNotReadyError(entry_step, waiting)
            row = SqlSessionJournalEntry(
                workspace_id=current_workspace_id(),
                conversation_id=conversation_id,
                position=position,
                created_at=now_epoch(),
                id=uuid.uuid4().hex,
                kind=code,
                scheduled_task_run_id=scheduled_task_run_id,
                step=entry_step,
                idempotency_key=idempotency_key,
                payload=entry_text,
            )
            session.add(row)
            session.execute(
                update(SqlSessionJournalHead)
                .where(
                    SqlSessionJournalHead.workspace_id == current_workspace_id(),
                    SqlSessionJournalHead.conversation_id == conversation_id,
                )
                .values(next_position=position + 1)
            )
            return row, True, entry_payload

        row, created, entry_payload = run_write_transaction(
            self._session_immediate, "append", write
        )
        if entry_payload is None:
            return JournalAppend(entry=self._entries([row])[0], created=created)
        entry = JournalEntry(
            id=row.id,
            conversation_id=row.conversation_id,
            position=row.position,
            created_at=row.created_at,
            kind=kind,
            step=row.step,
            scheduled_task_run_id=row.scheduled_task_run_id,
            payload=entry_payload,
        )
        return JournalAppend(entry=entry, created=created)

    # ── Reads ─────────────────────────────────────────

    @staticmethod
    def _position(session: Session, conversation_id: str, entry_id: str) -> int:
        position = session.execute(
            _using(
                select(SqlSessionJournalEntry.position).where(
                    SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                    SqlSessionJournalEntry.conversation_id == conversation_id,
                    SqlSessionJournalEntry.id == entry_id,
                ),
                "ix_session_journal_entries_id",
            )
        ).scalar_one_or_none()
        if position is None:
            raise StaleCursorError(entry_id)
        return position

    def list_entries(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        order: str = "asc",
        after: str | None = None,
        before: str | None = None,
        kind: str | None = None,
        step: str | None = None,
    ) -> JournalPage:
        """One page of the session's journal, ordered by position.

        Cursors follow conversation items: ``after`` continues in the sort
        direction, ``before`` goes against it, and both may be given.

        :param conversation_id: The session.
        :param limit: Page size, 1 to
            :data:`~omnigent.entities.session_journal.JOURNAL_PAGE_MAX`.
        :param order: ``"asc"`` (oldest first) or ``"desc"`` (newest first).
        :param after: An entry ID; only entries after it in sort order.
        :param before: An entry ID; only entries before it in sort order.
        :param kind: Only entries of this kind, e.g. ``"plan"``.
        :param step: Only entries about this step, e.g. ``"propose"``.
        :raises ValueError: If an argument is invalid.
        :raises StaleCursorError: If a cursor names no entry of this journal.
        """
        if not 1 <= limit <= JOURNAL_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {JOURNAL_PAGE_MAX}")
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        if kind is not None and kind not in JOURNAL_KINDS:
            raise ValueError(f"kind must be one of {', '.join(JOURNAL_KINDS)}")
        if step is not None:
            step = normalize_step_id(step)
        after_id = _entry_id(after, "after") if after is not None else None
        before_id = _entry_id(before, "before") if before is not None else None
        ascending = order == "asc"
        position = SqlSessionJournalEntry.position
        stmt = select(SqlSessionJournalEntry).where(
            SqlSessionJournalEntry.workspace_id == current_workspace_id(),
            SqlSessionJournalEntry.conversation_id == conversation_id,
        )
        if kind is not None:
            stmt = stmt.where(SqlSessionJournalEntry.kind == encode_journal_entry_kind(kind))
        if step is not None:
            stmt = stmt.where(SqlSessionJournalEntry.step == step)
        with self._session("read_page") as session:
            if after_id is not None:
                cut = self._position(session, conversation_id, after_id)
                stmt = stmt.where(position > cut if ascending else position < cut)
            if before_id is not None:
                cut = self._position(session, conversation_id, before_id)
                stmt = stmt.where(position < cut if ascending else position > cut)
            stmt = stmt.order_by(position.asc() if ascending else position.desc())
            stmt = _using(stmt, _page_index(kind=kind is not None, step=step is not None))
            rows = list(session.execute(stmt.limit(limit + 1)).scalars())
        return JournalPage(entries=self._entries(rows[:limit]), has_more=len(rows) > limit)

    def current_plan(self, conversation_id: str) -> JournalEntry | None:
        """The session's newest plan entry, or ``None`` when it has no plan."""
        with self._session("read_current_plan") as session:
            row = self._newest_plan_row(session, conversation_id)
        return None if row is None else self._entries([row])[0]


# ── Deletion with the session ─────────────────────────


def copy_journal_for_fork(
    session: Session, source_id: str, fork_id: str, *, created_through: int | None = None
) -> int:
    """Copy a session's journal into its new fork, in order, in the fork's transaction.

    The fork's entries get their own IDs and positions from 0, keep their times,
    steps and contents, and drop their automation run and workflow, so a run's or
    a workflow's history lists each entry once. Dispatches and their results stay
    with the parent.

    :param source_id: The session being forked.
    :param fork_id: The new fork, which has no journal yet.
    :param created_through: Copy only entries written at or before this epoch
        second, the time of the fork's last copied item; ``None`` copies them all.
    :returns: How many entries were copied.
    """
    scope = (
        SqlSessionJournalEntry.workspace_id == current_workspace_id(),
        SqlSessionJournalEntry.conversation_id == source_id,
    )
    query = select(SqlSessionJournalEntry).where(*scope).order_by(SqlSessionJournalEntry.position)
    if created_through is not None:
        # An append takes its position and time under the session's lock, so the
        # entries written by then are a prefix. Bounding it by position keeps the
        # copy a plain primary-key range; a time filter made Postgres sort it.
        last = session.execute(
            _using(
                select(SqlSessionJournalEntry.position)
                .where(*scope, SqlSessionJournalEntry.created_at <= created_through)
                .order_by(SqlSessionJournalEntry.position.desc())
                .limit(1),
                "PRIMARY",
            )
        ).scalar()
        if last is None:
            return 0
        query = query.where(SqlSessionJournalEntry.position <= last)
    copies = [
        SqlSessionJournalEntry(
            workspace_id=row.workspace_id,
            conversation_id=fork_id,
            position=position,
            created_at=row.created_at,
            id=uuid.uuid4().hex,
            kind=row.kind,
            scheduled_task_run_id=None,
            workflow_id=None,
            step=row.step,
            idempotency_key=row.idempotency_key,
            payload=row.payload,
        )
        for position, row in enumerate(
            row
            for row in session.execute(_using(query, "PRIMARY")).scalars()
            if row.kind not in _KINDS_A_FORK_LEAVES
        )
    ]
    if copies:
        session.add_all(copies)
        session.add(
            SqlSessionJournalHead(
                workspace_id=current_workspace_id(),
                conversation_id=fork_id,
                next_position=len(copies),
            )
        )
    return len(copies)


def delete_journal_heads(session: Session, conversation_ids: Sequence[str]) -> None:
    """Delete the sessions' heads rows, waiting for any append that holds one.

    Runs before :func:`delete_journal_entries_batch`: an append that starts
    afterwards finds the session gone and writes nothing.
    """
    session.execute(
        delete(SqlSessionJournalHead).where(
            SqlSessionJournalHead.workspace_id == current_workspace_id(),
            SqlSessionJournalHead.conversation_id.in_(conversation_ids),
        )
    )


def delete_journal_entries_batch(session: Session, conversation_ids: Sequence[str]) -> int:
    """Delete up to :data:`JOURNAL_DELETE_BATCH` of the sessions' entries.

    :returns: How many were deleted; fewer than a batch means none are left.
    """
    keys = session.execute(
        select(SqlSessionJournalEntry.conversation_id, SqlSessionJournalEntry.position)
        .where(
            SqlSessionJournalEntry.workspace_id == current_workspace_id(),
            SqlSessionJournalEntry.conversation_id.in_(conversation_ids),
        )
        .limit(JOURNAL_DELETE_BATCH)
    ).all()
    positions: dict[str, list[int]] = {}
    for conversation_id, position in keys:
        positions.setdefault(conversation_id, []).append(position)
    for conversation_id, batch in positions.items():
        session.execute(
            delete(SqlSessionJournalEntry).where(
                SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                SqlSessionJournalEntry.conversation_id == conversation_id,
                SqlSessionJournalEntry.position.in_(batch),
            )
        )
    return len(keys)
