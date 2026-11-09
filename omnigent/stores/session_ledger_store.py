"""Persistent store of each session's append-only ledger.

Entries are rows of ``session_ledger_entries`` in the database that holds the
session's metadata. An append locks the session's ``session_ledger_heads`` row,
which allocates positions, so one session's appends run one at a time while
other sessions' appends and the session's metadata updates don't wait. An
append carries an idempotency key, so a retried append returns the entry its
first attempt wrote instead of writing it twice.

Payloads are stored in an envelope private to this store: a version byte, a
variant byte, then the variant's body. The plaintext variant's body is the
kind's JSON in a :mod:`omnigent.db.compression` frame; the ciphertext variant
wraps that frame for a store that encrypts (see :meth:`SessionLedgerStore._seal`).
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
    LEDGER_PAYLOAD_MAX_STORED_BYTES,
    SqlConversationMetadata,
    SqlSessionLedgerEntry,
    SqlSessionLedgerHead,
    current_workspace_id,
)
from omnigent.db.enum_codecs import decode_ledger_entry_kind, encode_ledger_entry_kind
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.session_ledger import (
    IDEMPOTENCY_KEY_MAX_CHARS,
    LEDGER_KINDS,
    LEDGER_PAGE_MAX,
    PLAN_MAX_RAW_BYTES,
    STATUSES_NEEDING_DEPENDENCIES,
    InvalidLedgerEntryError,
    LedgerAppend,
    LedgerEntry,
    LedgerEntryTooLargeError,
    LedgerPage,
    StepNotReadyError,
    normalize_payload,
    normalize_step_id,
)
from omnigent.errors import StaleCursorError
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

_ENVELOPE_VERSION = 1
_PLAINTEXT = 1
_CIPHERTEXT = 2
# Guards decompression of a damaged row; every kind's raw limit is well below it.
_MAX_DECODED_BYTES = 2 * PLAN_MAX_RAW_BYTES
_PLAN_KIND = encode_ledger_entry_kind("plan")
_STATUS_KIND = encode_ledger_entry_kind("status")
# A fork doesn't own the parent's workers, so their dispatches stay behind.
_KINDS_A_FORK_LEAVES = frozenset(
    encode_ledger_entry_kind(kind) for kind in ("dispatch", "dispatch_result")
)
# Session deletion removes at most this many entries per transaction.
LEDGER_DELETE_BATCH = 500

_Statement = TypeVar("_Statement", bound=Select[Any])


def _using(statement: _Statement, index: str) -> _Statement:
    """Pin a ledger read to *index* on MySQL.

    The kind and kind-and-step indexes share their leading columns, and MySQL
    can pick the wrong one and sort, or walk the primary key and filter; the
    index audit caught both. SQLite and Postgres choose correctly unaided.
    """
    return statement.with_hint(SqlSessionLedgerEntry, f"FORCE INDEX ({index})", "mysql")


def _page_index(*, kind: bool, step: bool) -> str:
    """The index that serves a page with these filters in position order."""
    if kind and step:
        return "ix_session_ledger_entries_kind_step"
    if kind:
        return "ix_session_ledger_entries_kind"
    if step:
        return "ix_session_ledger_entries_step"
    return "PRIMARY"


class LedgerSessionNotFoundError(Exception):
    """The session doesn't exist, or was deleted while the append ran."""


class UnreadableLedgerEntryError(Exception):
    """A stored envelope can't be read by this store."""


def _document(payload: Mapping[str, Any]) -> bytes:
    """The kind's JSON in a compression frame."""
    encoded = compression.encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    assert encoded is not None
    return encoded


def _payload(document: bytes) -> dict[str, Any]:
    try:
        text = compression.decode(document, max_decoded_bytes=_MAX_DECODED_BYTES)
        payload = json.loads(text or "")
    except (ValueError, UnicodeDecodeError) as exc:
        raise UnreadableLedgerEntryError("unreadable ledger payload") from exc
    if not isinstance(payload, dict):
        raise UnreadableLedgerEntryError("unrecognized ledger payload")
    return payload


def _entry_id(value: str, name: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError as exc:
        raise ValueError(f"{name} must be an entry ID") from exc


class SessionLedgerStore:
    """SQLAlchemy-backed store of each session's ledger."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy URI of the database holding
            ``omnigent_conversation_metadata``, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._dialect = engine.dialect.name
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_ledger"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_ledger", immediate=True
        )

    # ── Payload envelope ──────────────────────────────

    def _seal(self, documents: Sequence[bytes]) -> list[bytes]:
        """Wrap compressed documents in the envelope's plaintext variant.

        A store that encrypts overrides this and :meth:`_open`, sealing a whole
        batch in one call and returning the ciphertext variant.
        """
        return [bytes((_ENVELOPE_VERSION, _PLAINTEXT)) + document for document in documents]

    def _open(self, envelopes: Sequence[bytes]) -> list[bytes]:
        """Return each envelope's compressed document.

        :raises UnreadableLedgerEntryError: For an unknown envelope, or a
            ciphertext this store can't decrypt.
        """
        documents = []
        for envelope in envelopes:
            raw = bytes(envelope)
            if len(raw) < 2 or raw[0] != _ENVELOPE_VERSION:
                raise UnreadableLedgerEntryError("unrecognized ledger envelope")
            if raw[1] == _CIPHERTEXT:
                raise UnreadableLedgerEntryError(
                    "an encrypted ledger entry needs a store that can decrypt it"
                )
            if raw[1] != _PLAINTEXT:
                raise UnreadableLedgerEntryError("unrecognized ledger envelope variant")
            documents.append(raw[2:])
        return documents

    def _seal_payload(self, payload: Mapping[str, Any]) -> bytes:
        """Seal one payload, enforcing the stored-size limit."""
        sealed = self._seal([_document(payload)])[0]
        if len(sealed) > LEDGER_PAYLOAD_MAX_STORED_BYTES:
            raise LedgerEntryTooLargeError(
                "the stored entry", len(sealed), LEDGER_PAYLOAD_MAX_STORED_BYTES
            )
        return sealed

    def _entries(self, rows: Sequence[SqlSessionLedgerEntry]) -> list[LedgerEntry]:
        """Decode rows into entries, opening their payloads in one batch."""
        documents = self._open([row.payload for row in rows]) if rows else []
        return [
            LedgerEntry(
                id=row.id,
                conversation_id=row.conversation_id,
                position=row.position,
                created_at=row.created_at,
                kind=decode_ledger_entry_kind(row.kind),
                step=row.step,
                scheduled_task_run_id=row.scheduled_task_run_id,
                payload=_payload(document),
            )
            for row, document in zip(rows, documents, strict=True)
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
                mysql_insert(SqlSessionLedgerHead)
                .values(**values)
                .on_duplicate_key_update(next_position=SqlSessionLedgerHead.next_position)
            )
        elif self._dialect == "sqlite":
            statement = (
                sqlite_insert(SqlSessionLedgerHead).values(**values).on_conflict_do_nothing()
            )
        else:
            statement = pg_insert(SqlSessionLedgerHead).values(**values).on_conflict_do_nothing()
        session.execute(statement)
        # SQLite has no FOR UPDATE; its immediate transaction already holds the write lock.
        return session.execute(
            select(SqlSessionLedgerHead.next_position)
            .where(
                SqlSessionLedgerHead.workspace_id == current_workspace_id(),
                SqlSessionLedgerHead.conversation_id == conversation_id,
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
    def _newest_plan_row(session: Session, conversation_id: str) -> SqlSessionLedgerEntry | None:
        statement = (
            select(SqlSessionLedgerEntry)
            .where(
                SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
                SqlSessionLedgerEntry.conversation_id == conversation_id,
                SqlSessionLedgerEntry.kind == _PLAN_KIND,
            )
            .order_by(SqlSessionLedgerEntry.position.desc())
            .limit(1)
        )
        return session.execute(
            _using(statement, "ix_session_ledger_entries_kind")
        ).scalar_one_or_none()

    @staticmethod
    def _default_step(steps: list[dict[str, Any]] | None) -> str:
        """The step a status names when it names none: the only step of a one-step plan.

        :param steps: The current plan's steps, or None when the session has no plan.
        """
        if steps is None or len(steps) != 1:
            raise InvalidLedgerEntryError(
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
                    select(SqlSessionLedgerEntry)
                    .where(
                        SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
                        SqlSessionLedgerEntry.conversation_id == conversation_id,
                        SqlSessionLedgerEntry.kind == _STATUS_KIND,
                        SqlSessionLedgerEntry.step == dependency,
                    )
                    .order_by(SqlSessionLedgerEntry.position.desc())
                    .limit(1),
                    "ix_session_ledger_entries_kind_step",
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
    ) -> LedgerAppend:
        """Append an entry to the session's ledger.

        :param conversation_id: The session, e.g. ``"0123…cdef"``.
        :param kind: One of :data:`~omnigent.entities.session_ledger.LEDGER_KINDS`.
        :param payload: The kind's fields, checked with
            :func:`~omnigent.entities.session_ledger.normalize_payload`. A plan's
            version is set here: the previous plan's version plus one.
        :param idempotency_key: Names the write, e.g. ``"tool:toolu_01…"``. A
            retry with the same key returns the entry the first attempt wrote.
        :param step: The plan step the entry is about. A status without one
            names the only step of a one-step plan; a plan never has one.
        :param scheduled_task_run_id: The automation run the session is serving.
        :returns: The entry, and whether this call appended it.
        :raises InvalidLedgerEntryError: If a field is invalid.
        :raises LedgerEntryTooLargeError: If the stored payload is too large.
        :raises StepNotReadyError: If a ``started`` or ``done`` status names a step
            whose dependencies in the current plan aren't all done.
        :raises LedgerSessionNotFoundError: If the session doesn't exist.
        """
        fields = normalize_payload(kind, payload)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidLedgerEntryError("idempotency_key must not be empty")
        if len(idempotency_key) > IDEMPOTENCY_KEY_MAX_CHARS:
            raise InvalidLedgerEntryError(
                f"idempotency_key must be at most {IDEMPOTENCY_KEY_MAX_CHARS} characters"
            )
        if step is not None:
            if kind == "plan":
                raise InvalidLedgerEntryError("a plan names no step; its steps are in the graph")
            step = normalize_step_id(step)
        # A plan's version is known only under the lock, so a plan is sealed there.
        sealed = None if kind == "plan" else self._seal_payload(fields)
        code = encode_ledger_entry_kind(kind)

        def write(session: Session) -> tuple[SqlSessionLedgerEntry, bool, dict[str, Any] | None]:
            position = self._lock_head(session, conversation_id)
            if not self._session_exists(session, conversation_id):
                raise LedgerSessionNotFoundError(f"session {conversation_id!r} not found")
            earlier = session.execute(
                _using(
                    select(SqlSessionLedgerEntry)
                    .where(
                        SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
                        SqlSessionLedgerEntry.conversation_id == conversation_id,
                        SqlSessionLedgerEntry.idempotency_key == idempotency_key,
                    )
                    .order_by(SqlSessionLedgerEntry.position)
                    .limit(1),
                    "ix_session_ledger_entries_idempotency_key",
                )
            ).scalar_one_or_none()
            if earlier is not None:
                if earlier.kind != code:
                    raise InvalidLedgerEntryError(
                        "idempotency_key was already used for a different kind of entry"
                    )
                return earlier, False, None
            stored, entry_payload = sealed, fields
            if kind == "plan":
                previous = self._newest_plan_row(session, conversation_id)
                version = (
                    1 if previous is None else self._entries([previous])[0].payload["version"] + 1
                )
                entry_payload = {"version": version, **fields}
                stored = self._seal_payload(entry_payload)
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
            row = SqlSessionLedgerEntry(
                workspace_id=current_workspace_id(),
                conversation_id=conversation_id,
                position=position,
                created_at=now_epoch(),
                id=uuid.uuid4().hex,
                kind=code,
                scheduled_task_run_id=scheduled_task_run_id,
                step=entry_step,
                idempotency_key=idempotency_key,
                payload=stored,
            )
            session.add(row)
            session.execute(
                update(SqlSessionLedgerHead)
                .where(
                    SqlSessionLedgerHead.workspace_id == current_workspace_id(),
                    SqlSessionLedgerHead.conversation_id == conversation_id,
                )
                .values(next_position=position + 1)
            )
            return row, True, entry_payload

        row, created, entry_payload = run_write_transaction(
            self._session_immediate, "append", write
        )
        if entry_payload is None:
            return LedgerAppend(entry=self._entries([row])[0], created=created)
        entry = LedgerEntry(
            id=row.id,
            conversation_id=row.conversation_id,
            position=row.position,
            created_at=row.created_at,
            kind=kind,
            step=row.step,
            scheduled_task_run_id=row.scheduled_task_run_id,
            payload=entry_payload,
        )
        return LedgerAppend(entry=entry, created=created)

    # ── Reads ─────────────────────────────────────────

    @staticmethod
    def _position(session: Session, conversation_id: str, entry_id: str) -> int:
        position = session.execute(
            _using(
                select(SqlSessionLedgerEntry.position).where(
                    SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
                    SqlSessionLedgerEntry.conversation_id == conversation_id,
                    SqlSessionLedgerEntry.id == entry_id,
                ),
                "ix_session_ledger_entries_id",
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
    ) -> LedgerPage:
        """One page of the session's ledger, ordered by position.

        Cursors follow conversation items: ``after`` continues in the sort
        direction, ``before`` goes against it, and both may be given.

        :param conversation_id: The session.
        :param limit: Page size, 1 to
            :data:`~omnigent.entities.session_ledger.LEDGER_PAGE_MAX`.
        :param order: ``"asc"`` (oldest first) or ``"desc"`` (newest first).
        :param after: An entry ID; only entries after it in sort order.
        :param before: An entry ID; only entries before it in sort order.
        :param kind: Only entries of this kind, e.g. ``"plan"``.
        :param step: Only entries about this step, e.g. ``"propose"``.
        :raises ValueError: If an argument is invalid.
        :raises StaleCursorError: If a cursor names no entry of this ledger.
        """
        if not 1 <= limit <= LEDGER_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {LEDGER_PAGE_MAX}")
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        if kind is not None and kind not in LEDGER_KINDS:
            raise ValueError(f"kind must be one of {', '.join(LEDGER_KINDS)}")
        if step is not None:
            step = normalize_step_id(step)
        after_id = _entry_id(after, "after") if after is not None else None
        before_id = _entry_id(before, "before") if before is not None else None
        ascending = order == "asc"
        position = SqlSessionLedgerEntry.position
        stmt = select(SqlSessionLedgerEntry).where(
            SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
            SqlSessionLedgerEntry.conversation_id == conversation_id,
        )
        if kind is not None:
            stmt = stmt.where(SqlSessionLedgerEntry.kind == encode_ledger_entry_kind(kind))
        if step is not None:
            stmt = stmt.where(SqlSessionLedgerEntry.step == step)
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
        return LedgerPage(entries=self._entries(rows[:limit]), has_more=len(rows) > limit)

    def current_plan(self, conversation_id: str) -> LedgerEntry | None:
        """The session's newest plan entry, or ``None`` when it has no plan."""
        with self._session("read_current_plan") as session:
            row = self._newest_plan_row(session, conversation_id)
        return None if row is None else self._entries([row])[0]


# ── Deletion with the session ─────────────────────────


def copy_ledger_for_fork(
    session: Session, source_id: str, fork_id: str, *, created_through: int | None = None
) -> int:
    """Copy a session's ledger into its new fork, in order, in the fork's transaction.

    The fork's entries get their own IDs and positions from 0, keep their times,
    steps and contents, and drop their automation run, so a run's history lists
    each entry once. Dispatches and their results stay with the parent.

    :param source_id: The session being forked.
    :param fork_id: The new fork, which has no ledger yet.
    :param created_through: Copy only entries written at or before this epoch
        second, the time of the fork's last copied item; ``None`` copies them all.
    :returns: How many entries were copied.
    """
    scope = (
        SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
        SqlSessionLedgerEntry.conversation_id == source_id,
    )
    query = select(SqlSessionLedgerEntry).where(*scope).order_by(SqlSessionLedgerEntry.position)
    if created_through is not None:
        # An append takes its position and time under the session's lock, so the
        # entries written by then are a prefix. Bounding it by position keeps the
        # copy a plain primary-key range; a time filter made Postgres sort it.
        last = session.execute(
            _using(
                select(SqlSessionLedgerEntry.position)
                .where(*scope, SqlSessionLedgerEntry.created_at <= created_through)
                .order_by(SqlSessionLedgerEntry.position.desc())
                .limit(1),
                "PRIMARY",
            )
        ).scalar()
        if last is None:
            return 0
        query = query.where(SqlSessionLedgerEntry.position <= last)
    copies = [
        SqlSessionLedgerEntry(
            workspace_id=row.workspace_id,
            conversation_id=fork_id,
            position=position,
            created_at=row.created_at,
            id=uuid.uuid4().hex,
            kind=row.kind,
            scheduled_task_run_id=None,
            step=row.step,
            idempotency_key=row.idempotency_key,
            # The sealed bytes as stored: the fork shares the parent's workspace key.
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
            SqlSessionLedgerHead(
                workspace_id=current_workspace_id(),
                conversation_id=fork_id,
                next_position=len(copies),
            )
        )
    return len(copies)


def delete_ledger_heads(session: Session, conversation_ids: Sequence[str]) -> None:
    """Delete the sessions' heads rows, waiting for any append that holds one.

    Runs before :func:`delete_ledger_entries_batch`: an append that starts
    afterwards finds the session gone and writes nothing.
    """
    session.execute(
        delete(SqlSessionLedgerHead).where(
            SqlSessionLedgerHead.workspace_id == current_workspace_id(),
            SqlSessionLedgerHead.conversation_id.in_(conversation_ids),
        )
    )


def delete_ledger_entries_batch(session: Session, conversation_ids: Sequence[str]) -> int:
    """Delete up to :data:`LEDGER_DELETE_BATCH` of the sessions' entries.

    :returns: How many were deleted; fewer than a batch means none are left.
    """
    keys = session.execute(
        select(SqlSessionLedgerEntry.conversation_id, SqlSessionLedgerEntry.position)
        .where(
            SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
            SqlSessionLedgerEntry.conversation_id.in_(conversation_ids),
        )
        .limit(LEDGER_DELETE_BATCH)
    ).all()
    positions: dict[str, list[int]] = {}
    for conversation_id, position in keys:
        positions.setdefault(conversation_id, []).append(position)
    for conversation_id, batch in positions.items():
        session.execute(
            delete(SqlSessionLedgerEntry).where(
                SqlSessionLedgerEntry.workspace_id == current_workspace_id(),
                SqlSessionLedgerEntry.conversation_id == conversation_id,
                SqlSessionLedgerEntry.position.in_(batch),
            )
        )
    return len(keys)
