"""Persistent store of each session's append-only journal.

Entries are rows of ``session_journal_entries`` in the database that holds the
session's metadata. An append locks the session's ``session_journal_heads`` row,
which allocates positions, so one session's appends run one at a time while
other sessions' appends and the session's metadata updates don't wait. An
append carries an idempotency key, so a retried append returns the entry its
first attempt wrote instead of writing it twice.

An entry's content is the kind's compressed JSON, at most
:data:`~omnigent.db.db_models.MAX_STORED_VALUE_BYTES` bytes stored. The store
encodes it, not the column type: a deployment that encrypts journal content
overrides :meth:`SessionJournalStore._encode_contents` and
:meth:`SessionJournalStore._decode_contents` to transform a whole batch in one
call. The size check runs on the encoded bytes, so an agent gets
``entry_too_large`` before the write, encryption overhead included.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from itertools import batched
from typing import Any, TypeVar

from sqlalchemy import Select, and_, delete, or_, select, update
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
    NamedManagedSessionMaker,
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.session_journal import (
    IDEMPOTENCY_KEY_MAX_CHARS,
    JOURNAL_KINDS,
    JOURNAL_MAX_ENTRIES,
    JOURNAL_PAGE_MAX,
    InvalidJournalEntryError,
    JournalAppend,
    JournalEntry,
    JournalEntryTooLargeError,
    JournalFullError,
    JournalPage,
    StepNotReadyError,
    WorkflowRunPage,
    normalize_content,
    normalize_step_id,
)
from omnigent.errors import StaleCursorError
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

_PLAN_KIND = encode_journal_entry_kind("plan")
_STATUS_KIND = encode_journal_entry_kind("status")
_PLAN_STATUS_KIND = encode_journal_entry_kind("plan_status")
# A fork doesn't own the parent's workers, so their dispatches stay behind.
_KINDS_A_FORK_LEAVES = frozenset(
    encode_journal_entry_kind(kind) for kind in ("dispatch", "dispatch_result")
)
# Session deletion removes at most this many entries per transaction, and a
# statement's IN list names at most this many keys.
JOURNAL_DELETE_BATCH = 200
# Journal copies read and write at most this many entries per transaction.
JOURNAL_COPY_BATCH = 200

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


def _set_next_position(session: Session, conversation_id: str, next_position: int) -> None:
    """Advance a session's heads row, a primary-key update."""
    session.execute(
        update(SqlSessionJournalHead)
        .where(
            SqlSessionJournalHead.workspace_id == current_workspace_id(),
            SqlSessionJournalHead.conversation_id == conversation_id,
        )
        .values(next_position=next_position)
        .with_hint("FORCE INDEX (PRIMARY)", dialect_name="mysql")
    )


def _entry_id(value: str, name: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError as exc:
        raise ValueError(f"{name} must be an entry ID") from exc


def _workflow_id(value: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError as exc:
        raise InvalidJournalEntryError("workflow_id must be a workflow ID") from exc


def _run_cursor(plan: JournalEntry) -> str:
    """Where the next page of a workflow's runs starts, e.g. ``"1762900000.0123…cdef.4"``."""
    return f"{plan.created_at}.{plan.conversation_id}.{plan.position}"


def _parse_run_cursor(cursor: str) -> tuple[int, str, int]:
    """The ``(created_at, conversation_id, position)`` a runs cursor names."""
    parts = cursor.split(".")
    try:
        if len(parts) != 3:
            raise ValueError(cursor)
        return int(parts[0]), uuid_to_bytes(parts[1]).hex(), int(parts[2])
    except (ValueError, InvalidUuidError) as exc:
        raise ValueError("before must be a cursor from a previous page") from exc


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

    def _encode_contents(self, texts: Sequence[str]) -> list[bytes]:
        """Encode entries' JSON for the ``content`` column, one value per text, in order.

        Compressed and framed by default. A deployment that encrypts journal
        content overrides this and :meth:`_decode_contents`, transforming the
        whole batch in one call; its values must say they're encrypted
        (:func:`~omnigent.db.compression.encode_encrypted`) so a reader without
        the key fails closed.
        """
        return [compression.encode(text) or b"" for text in texts]

    def _decode_contents(self, stored: Sequence[bytes]) -> list[str]:
        """Decode a batch of ``content`` values, the inverse of :meth:`_encode_contents`.

        An encrypted value raises here: only the deployment that wrote it can read it.
        """
        return [compression.decode(bytes(value)) or "" for value in stored]

    def _stored_contents(self, contents: Sequence[Mapping[str, Any]]) -> list[bytes]:
        """The entries' encoded ``content``, refused when one doesn't fit the column."""
        stored = self._encode_contents(
            [
                json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                for content in contents
            ]
        )
        for value in stored:
            if len(value) > MAX_STORED_VALUE_BYTES:
                raise JournalEntryTooLargeError(
                    "the stored entry", len(value), MAX_STORED_VALUE_BYTES
                )
        return stored

    def _entries(self, rows: Sequence[SqlSessionJournalEntry]) -> list[JournalEntry]:
        """Decode rows into entries, their contents in one batch."""
        contents = self._decode_contents([row.content for row in rows]) if rows else []
        return [
            JournalEntry(
                id=row.id,
                conversation_id=row.conversation_id,
                position=row.position,
                created_at=row.created_at,
                kind=decode_journal_entry_kind(row.kind),
                step=row.step,
                scheduled_task_run_id=row.scheduled_task_run_id,
                content=json.loads(content),
                workflow_id=row.workflow_id,
            )
            for row, content in zip(rows, contents, strict=True)
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
            .with_hint(SqlSessionJournalHead, "FORCE INDEX (PRIMARY)", "mysql")
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
    def _newest_kind_row(
        session: Session, conversation_id: str, kind: int, *, before_position: int | None = None
    ) -> SqlSessionJournalEntry | None:
        statement = (
            select(SqlSessionJournalEntry)
            .where(
                SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                SqlSessionJournalEntry.conversation_id == conversation_id,
                SqlSessionJournalEntry.kind == kind,
            )
            .order_by(SqlSessionJournalEntry.position.desc())
            .limit(1)
        )
        if before_position is not None:
            statement = statement.where(SqlSessionJournalEntry.position < before_position)
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
        if current is None or not current["depends_on"]:
            return []
        # A saved workflow starts fresh; replans retain progress within that run.
        run = self._newest_kind_row(session, conversation_id, _PLAN_STATUS_KIND)
        newest = [
            self._newest_status_row(
                session,
                conversation_id,
                dependency,
                start_position=run.position if run is not None else None,
            )
            for dependency in current["depends_on"]
        ]
        done = {
            entry.step
            for entry in self._entries([row for row in newest if row is not None])
            if entry.content.get("status") == "done"
        }
        return [dependency for dependency in current["depends_on"] if dependency not in done]

    @staticmethod
    def _newest_status_row(
        session: Session,
        conversation_id: str,
        step: str,
        *,
        start_position: int | None = None,
        end_position: int | None = None,
    ) -> SqlSessionJournalEntry | None:
        """The step's newest status, optionally within ``[start_position, end_position)``."""
        entries = SqlSessionJournalEntry
        statement = select(entries).where(
            entries.workspace_id == current_workspace_id(),
            entries.conversation_id == conversation_id,
            entries.kind == _STATUS_KIND,
            entries.step == step,
        )
        if start_position is not None:
            statement = statement.where(entries.position >= start_position)
        if end_position is not None:
            statement = statement.where(entries.position < end_position)
        return session.execute(
            _using(
                statement.order_by(entries.position.desc()).limit(1),
                "ix_session_journal_entries_kind_step",
            )
        ).scalar_one_or_none()

    def append(
        self,
        conversation_id: str,
        kind: str,
        content: Mapping[str, Any],
        *,
        idempotency_key: str,
        step: str | None = None,
        scheduled_task_run_id: str | None = None,
        workflow_id: str | None = None,
        start_run: bool = True,
    ) -> JournalAppend:
        """Append an entry to the session's journal.

        :param conversation_id: The session, e.g. ``"0123…cdef"``.
        :param kind: One of :data:`~omnigent.entities.session_journal.JOURNAL_KINDS`.
        :param content: The kind's fields, checked with
            :func:`~omnigent.entities.session_journal.normalize_content`. A plan's
            version is set here: the previous plan's version plus one.
        :param idempotency_key: Names the write, e.g. ``"tool:toolu_01…"``. A
            retry with the same key returns the entry the first attempt wrote.
        :param step: The plan step the entry is about. A status without one
            names the only step of a one-step plan; a plan never has one.
        :param scheduled_task_run_id: The automation run the session is serving.
        :param workflow_id: The saved workflow a plan starts a run of, e.g.
            ``"0123…cdef"``. Only a plan names one; a status or note takes the
            current plan's.
        :param start_run: False to associate a saved workflow with ongoing work.
        :returns: The entry, and whether this call appended it.
        :raises InvalidJournalEntryError: If a field is invalid.
        :raises JournalEntryTooLargeError: If the stored content is too large.
        :raises JournalFullError: If the journal already holds
            :data:`~omnigent.entities.session_journal.JOURNAL_MAX_ENTRIES` entries.
        :raises StepNotReadyError: If a status names a step whose dependencies in
            the current plan aren't all done.
        :raises JournalSessionNotFoundError: If the session doesn't exist.
        """
        fields = normalize_content(kind, content)
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
        if workflow_id is not None:
            if kind != "plan":
                raise InvalidJournalEntryError("only a plan names a workflow")
            workflow_id = _workflow_id(workflow_id)
        # A plan's version is known only under the lock, so its size is checked there.
        stored = None if kind == "plan" else self._stored_contents([fields])[0]
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
            entry_text, entry_content = stored, fields
            run_started_text = None
            starts_run = False
            if kind == "plan":
                previous = self._newest_kind_row(session, conversation_id, _PLAN_KIND)
                version = (
                    1 if previous is None else self._entries([previous])[0].content["version"] + 1
                )
                entry_content = {"version": version, **fields}
                starts_run = workflow_id is not None and start_run
                if not starts_run:
                    boundary = self._newest_kind_row(session, conversation_id, _PLAN_STATUS_KIND)
                    starts_run = (
                        boundary.scheduled_task_run_id if boundary else None
                    ) != scheduled_task_run_id
                encoded = self._stored_contents(
                    [entry_content, *([{"status": "started"}] if starts_run else [])]
                )
                entry_text = encoded[0]
                run_started_text = encoded[1] if starts_run else None
            entry_step = step
            entry_workflow_id = workflow_id
            if kind == "note":
                plan = self._newest_kind_row(session, conversation_id, _PLAN_KIND)
                entry_workflow_id = plan.workflow_id if plan is not None else None
            if kind == "status":
                plan = self._newest_kind_row(session, conversation_id, _PLAN_KIND)
                entry_workflow_id = plan.workflow_id if plan is not None else None
                steps = self._entries([plan])[0].content["steps"] if plan is not None else None
                if entry_step is None:
                    entry_step = self._default_step(steps)
                waiting = self._unfinished_dependencies(
                    session, conversation_id, steps, entry_step
                )
                if waiting:
                    raise StepNotReadyError(entry_step, waiting)
            if position + (2 if starts_run else 1) > JOURNAL_MAX_ENTRIES:
                raise JournalFullError(JOURNAL_MAX_ENTRIES)
            stamp = now_epoch()
            entry_id = uuid.uuid4().hex
            if starts_run:
                session.add(
                    SqlSessionJournalEntry(
                        workspace_id=current_workspace_id(),
                        conversation_id=conversation_id,
                        position=position,
                        created_at=stamp,
                        id=uuid.uuid4().hex,
                        kind=_PLAN_STATUS_KIND,
                        scheduled_task_run_id=scheduled_task_run_id,
                        workflow_id=workflow_id,
                        step=None,
                        idempotency_key=f"run:{entry_id}",
                        content=run_started_text,
                    )
                )
                position += 1
            row = SqlSessionJournalEntry(
                workspace_id=current_workspace_id(),
                conversation_id=conversation_id,
                position=position,
                created_at=stamp,
                id=entry_id,
                kind=code,
                scheduled_task_run_id=scheduled_task_run_id,
                workflow_id=entry_workflow_id,
                step=entry_step,
                idempotency_key=idempotency_key,
                content=entry_text,
            )
            session.add(row)
            _set_next_position(session, conversation_id, position + 1)
            return row, True, entry_content

        row, created, entry_content = run_write_transaction(
            self._session_immediate, "append", write
        )
        if entry_content is None:
            return JournalAppend(entry=self._entries([row])[0], created=created)
        entry = JournalEntry(
            id=row.id,
            conversation_id=row.conversation_id,
            position=row.position,
            created_at=row.created_at,
            kind=kind,
            step=row.step,
            scheduled_task_run_id=row.scheduled_task_run_id,
            content=entry_content,
            workflow_id=row.workflow_id,
        )
        return JournalAppend(entry=entry, created=created)

    # ── Reads ─────────────────────────────────────────

    @staticmethod
    def _position(session: Session, conversation_id: str, entry_id: str) -> int:
        position = session.execute(
            _using(
                select(SqlSessionJournalEntry.position)
                .where(
                    SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                    SqlSessionJournalEntry.conversation_id == conversation_id,
                    SqlSessionJournalEntry.id == entry_id,
                )
                .limit(1),
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
            row = self._newest_kind_row(session, conversation_id, _PLAN_KIND)
        return None if row is None else self._entries([row])[0]

    def current_run_plan_ids(self, plans: Sequence[JournalEntry]) -> set[str]:
        """Which plans belong to their session's latest run, including its replans.

        Uses one indexed run-boundary lookup per distinct session in the page.
        """
        with self._session("read_current_run_plans") as session:
            boundaries = {
                conversation_id: self._newest_kind_row(session, conversation_id, _PLAN_STATUS_KIND)
                for conversation_id in dict.fromkeys(plan.conversation_id for plan in plans)
            }
            return {
                plan.id
                for plan in plans
                if (boundary := boundaries[plan.conversation_id]) is None
                or plan.position > boundary.position
            }

    def list_workflow_runs(
        self, workflow_id: str, *, limit: int = 20, before: str | None = None
    ) -> WorkflowRunPage:
        """A saved workflow's runs in every session, newest first.

        A run is the plan entry that named the workflow. Replans name none, so a
        session that ran the workflow twice has two runs, and a replan none.

        :param workflow_id: The workflow, e.g. ``"0123…cdef"``.
        :param limit: Page size, 1 to
            :data:`~omnigent.entities.session_journal.JOURNAL_PAGE_MAX`.
        :param before: The previous page's ``next_cursor``.
        :raises ValueError: If an argument is invalid.
        """
        if not 1 <= limit <= JOURNAL_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {JOURNAL_PAGE_MAX}")
        try:
            workflow = uuid_to_bytes(workflow_id).hex()
        except InvalidUuidError as exc:
            raise ValueError("workflow_id must be a workflow ID") from exc
        entries = SqlSessionJournalEntry
        stmt = select(entries).where(
            entries.workspace_id == current_workspace_id(),
            entries.workflow_id == workflow,
            entries.kind == _PLAN_KIND,
        )
        if before is not None:
            created_at, conversation_id, position = _parse_run_cursor(before)
            # The bound starts every engine's scan at the cursor; the OR settles ties.
            stmt = stmt.where(
                entries.created_at <= created_at,
                or_(
                    entries.created_at < created_at,
                    and_(
                        entries.created_at == created_at, entries.conversation_id < conversation_id
                    ),
                    and_(
                        entries.created_at == created_at,
                        entries.conversation_id == conversation_id,
                        entries.position < position,
                    ),
                ),
            )
        stmt = stmt.order_by(
            entries.created_at.desc(), entries.conversation_id.desc(), entries.position.desc()
        ).limit(limit + 1)
        with self._session("list_workflow_runs") as session:
            rows = list(
                session.execute(_using(stmt, "ix_session_journal_entries_workflow")).scalars()
            )
        plans = self._entries(rows[:limit])
        return WorkflowRunPage(
            plans=plans, next_cursor=_run_cursor(plans[-1]) if len(rows) > limit else None
        )

    def run_start_plans(self, runs: Sequence[tuple[str, str]]) -> list[JournalEntry | None]:
        """Each automation run's first plan: the one it started with.

        One run-index lookup per run, e.g. for a page of an automation's runs.

        :param runs: ``(scheduled_task_run_id, conversation_id)`` pairs; each
            run serves one session.
        :returns: Each run's first plan entry in order, or ``None`` for a run
            that wrote no plan.
        """
        entries = SqlSessionJournalEntry
        rows: list[SqlSessionJournalEntry | None] = []
        with self._session("read_run_start_plans") as session:
            for run_id, conversation_id in runs:
                row = session.execute(
                    _using(
                        select(entries)
                        .where(
                            entries.workspace_id == current_workspace_id(),
                            entries.scheduled_task_run_id == run_id,
                            entries.conversation_id == conversation_id,
                            entries.kind == _PLAN_KIND,
                        )
                        .order_by(entries.position)
                        .limit(1),
                        "ix_session_journal_entries_scheduled_task_run_id",
                    )
                ).scalar_one_or_none()
                rows.append(row)
            found = iter(self._entries([row for row in rows if row is not None]))
        return [None if row is None else next(found) for row in rows]

    def plan_step_statuses(self, plans: Sequence[JournalEntry]) -> list[dict[str, JournalEntry]]:
        """For each plan, its steps' newest statuses before the next run starts.

        Two kind lookups per plan and one kind-and-step lookup per step keep
        runs with reused step IDs separate while retaining progress through replans.

        :param plans: Plan entries, e.g. a page of a workflow's runs.
        :returns: One ``{step: entry}`` per plan, in order, e.g. the ``done``
            status of ``"tests"``; a step with no status yet is left out.
        """
        found: list[tuple[int, str, SqlSessionJournalEntry]] = []
        entries = SqlSessionJournalEntry
        with self._session("read_plan_step_statuses") as session:
            for index, plan in enumerate(plans):
                boundary = self._newest_kind_row(
                    session, plan.conversation_id, _PLAN_STATUS_KIND, before_position=plan.position
                )
                next_position = session.execute(
                    _using(
                        select(entries.position)
                        .where(
                            entries.workspace_id == current_workspace_id(),
                            entries.conversation_id == plan.conversation_id,
                            entries.kind == _PLAN_STATUS_KIND,
                            entries.position > plan.position,
                        )
                        .order_by(entries.position)
                        .limit(1),
                        "ix_session_journal_entries_kind",
                    )
                ).scalar_one_or_none()
                for step in plan.content.get("steps", []):
                    row = self._newest_status_row(
                        session,
                        plan.conversation_id,
                        step["id"],
                        start_position=boundary.position if boundary is not None else None,
                        end_position=next_position,
                    )
                    if row is not None:
                        found.append((index, step["id"], row))
            decoded = self._entries([row for _, _, row in found])
        statuses: list[dict[str, JournalEntry]] = [{} for _ in plans]
        for (index, step_id, _), entry in zip(found, decoded, strict=True):
            statuses[index][step_id] = entry
        return statuses


# ── Deletion with the session ─────────────────────────


def fork_journal_entry_id(fork_id: str, entry_id: str) -> str:
    """Give a copied entry and its transcript references the same new ID."""
    return uuid.uuid5(uuid.UUID(fork_id), entry_id).hex


def reserve_fork_journal(sessions: NamedManagedSessionMaker, fork_id: str) -> None:
    """Claim an unused journal before staging a fork or enabling failure cleanup."""
    with sessions("reserve_fork_journal") as session:
        if session.get(SqlConversationMetadata, (current_workspace_id(), fork_id)) or (
            session.execute(
                _using(
                    select(SqlSessionJournalEntry.position)
                    .where(
                        SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                        SqlSessionJournalEntry.conversation_id == fork_id,
                    )
                    .limit(1),
                    "PRIMARY",
                )
            ).first()
            is not None
        ):
            raise ValueError(f"fork journal already exists: {fork_id!r}")
        # The unique head also excludes a concurrent fork of an empty journal.
        session.add(
            SqlSessionJournalHead(
                workspace_id=current_workspace_id(),
                conversation_id=fork_id,
                next_position=0,
            )
        )


def copy_journal_for_fork(
    sessions: NamedManagedSessionMaker,
    source_id: str,
    fork_id: str,
    *,
    through_entry_ids: Sequence[str] | None = None,
) -> int:
    """Stage a fixed journal prefix in bounded transactions before publishing the fork.

    The fork's entries get their own IDs and positions from 0, keep their times,
    steps and contents, and drop their automation run and workflow, so a run's or
    a workflow's history lists each entry once. Internal plan-status markers
    retain the run boundaries. Dispatches and their results stay with the parent.

    :param source_id: The session being forked.
    :param fork_id: The new fork, whose journal this caller has reserved.
    :param through_entry_ids: Journal checkpoints in the copied transcript.
        Copy through their newest entry; ``None`` copies the whole journal.
    :returns: How many entries the fork starts with, including run boundaries.
    """
    scope = (
        SqlSessionJournalEntry.workspace_id == current_workspace_id(),
        SqlSessionJournalEntry.conversation_id == source_id,
    )
    with sessions("snapshot_fork_journal") as session:
        source = session.get(
            SqlConversationMetadata, (current_workspace_id(), source_id), with_for_update=True
        )
        last_position = session.execute(
            _using(
                select(SqlSessionJournalEntry.position)
                .where(*scope)
                .order_by(SqlSessionJournalEntry.position.desc())
                .limit(1),
                "PRIMARY",
            )
        ).scalar()
        if last_position is None:
            return 0
        if source is None:
            raise JournalSessionNotFoundError(source_id)

    if through_entry_ids is not None:
        boundary = None
        for batch in batched(through_entry_ids, JOURNAL_COPY_BATCH):
            with sessions("read_fork_journal_boundary") as session:
                positions = session.scalars(
                    _using(
                        select(SqlSessionJournalEntry.position)
                        .where(*scope, SqlSessionJournalEntry.id.in_(batch))
                        .limit(len(batch)),
                        "ix_session_journal_entries_id",
                    )
                ).all()
            if positions:
                boundary = max(boundary if boundary is not None else -1, *positions)
        if boundary is None:
            return 0
        last_position = min(last_position, boundary)

    after = -1
    copied = 0

    def copy_batch(session: Session) -> tuple[int, int, bool]:
        rows = session.scalars(
            _using(
                select(SqlSessionJournalEntry)
                .where(
                    *scope,
                    SqlSessionJournalEntry.position > after,
                    SqlSessionJournalEntry.position <= last_position,
                )
                .order_by(SqlSessionJournalEntry.position)
                .limit(JOURNAL_COPY_BATCH),
                "PRIMARY",
            )
        ).all()
        cursor, position = after, copied
        for row in rows:
            if row.position != cursor + 1:
                raise JournalSessionNotFoundError(source_id)
            cursor = row.position
            if row.kind in _KINDS_A_FORK_LEAVES:
                continue
            session.add(
                SqlSessionJournalEntry(
                    workspace_id=row.workspace_id,
                    conversation_id=fork_id,
                    position=position,
                    created_at=row.created_at,
                    id=fork_journal_entry_id(fork_id, row.id),
                    kind=row.kind,
                    scheduled_task_run_id=None,
                    workflow_id=None,
                    step=row.step,
                    idempotency_key=row.idempotency_key,
                    content=row.content,
                )
            )
            position += 1
        if cursor == after:
            raise JournalSessionNotFoundError(source_id)
        return cursor, position, cursor == last_position

    done = False
    while not done:
        after, copied, done = run_write_transaction(
            sessions, "copy_fork_journal_batch", copy_batch
        )
    with sessions("finish_fork_journal") as session:
        _set_next_position(session, fork_id, copied)
    return copied


def discard_fork_journal(sessions: NamedManagedSessionMaker, fork_id: str) -> None:
    """Remove an unpublished fork's staged journal after a failed copy or insert."""
    while (
        run_write_transaction(
            sessions,
            "discard_fork_journal_batch",
            lambda session: delete_journal_entries_batch(session, [fork_id]),
        )
        == JOURNAL_DELETE_BATCH
    ):
        pass
    # Keep the reservation until no staged rows remain, excluding competing retries.
    with sessions("discard_fork_journal_head") as session:
        delete_journal_heads(session, [fork_id])


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

    The batch is read in primary-key order, so each session's share is a
    contiguous run of positions, deleted as one bounded range.

    :returns: How many were deleted; fewer than a batch means none are left.
    """
    keys = session.execute(
        _using(
            select(SqlSessionJournalEntry.conversation_id, SqlSessionJournalEntry.position)
            .where(
                SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                SqlSessionJournalEntry.conversation_id.in_(conversation_ids),
            )
            .order_by(SqlSessionJournalEntry.conversation_id, SqlSessionJournalEntry.position)
            .limit(JOURNAL_DELETE_BATCH),
            "PRIMARY",
        )
    ).all()
    ranges: dict[str, tuple[int, int]] = {}
    for conversation_id, position in keys:
        first, _ = ranges.get(conversation_id, (position, position))
        ranges[conversation_id] = (first, position)
    for conversation_id, (first, last) in ranges.items():
        session.execute(
            delete(SqlSessionJournalEntry).where(
                SqlSessionJournalEntry.workspace_id == current_workspace_id(),
                SqlSessionJournalEntry.conversation_id == conversation_id,
                SqlSessionJournalEntry.position.between(first, last),
            )
        )
    return len(keys)
