"""SQLAlchemy-backed session-lifecycle-push store (OMN-104)."""

from __future__ import annotations

from sqlalchemy import desc, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from omnigent.db.db_models import (
    SqlSessionElicitation,
    SqlSessionLifecycleCursor,
    SqlSessionLifecycleOutbox,
    current_workspace_id,
)
from omnigent.db.enum_codecs import (
    decode_session_elicitation_status,
    decode_session_lifecycle_event_type,
    decode_session_lifecycle_outbox_status,
    encode_session_elicitation_status,
    encode_session_lifecycle_event_type,
    encode_session_lifecycle_outbox_status,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.entities import LifecycleOutboxEvent, SessionElicitation
from omnigent.stores.session_lifecycle_store import SessionLifecycleStore

_LEASE_BLOCKING_STATUSES = (
    encode_session_lifecycle_outbox_status("pending"),
    encode_session_lifecycle_outbox_status("leased"),
)
_CLAIMABLE_STATUSES = (
    encode_session_lifecycle_outbox_status("pending"),
    encode_session_lifecycle_outbox_status("dead_letter"),
)


def _outbox_to_entity(row: SqlSessionLifecycleOutbox) -> LifecycleOutboxEvent:
    return LifecycleOutboxEvent(
        id=row.id,
        session_id=row.session_id,
        event_type=decode_session_lifecycle_event_type(row.event_type),
        transition_key=row.transition_key,
        sequence=row.sequence,
        event_version=row.event_version,
        payload=row.payload,
        status=decode_session_lifecycle_outbox_status(row.status),
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        workspace_id=row.workspace_id,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        last_attempt_at=row.last_attempt_at,
        delivered_at=row.delivered_at,
        last_http_status=row.last_http_status,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
    )


def _elicitation_to_entity(row: SqlSessionElicitation) -> SessionElicitation:
    return SessionElicitation(
        id=row.id,
        session_id=row.session_id,
        status=decode_session_elicitation_status(row.status),
        request_payload=row.request_payload,
        created_at=row.created_at,
        workspace_id=row.workspace_id,
        decision_payload=row.decision_payload,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        resolved_at=row.resolved_at,
    )


class SqlAlchemySessionLifecycleStore(SessionLifecycleStore):
    """SQLAlchemy-backed implementation of :class:`SessionLifecycleStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy session-lifecycle store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        # SQLite: BEGIN IMMEDIATE acquires the write lock up front, so the
        # cursor read-modify-write below is race-free without SELECT ... FOR
        # UPDATE (which SQLite doesn't support). Postgres still gets an
        # explicit .with_for_update() — see _allocate_sequence.
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.session_lifecycle_store",
            immediate=True,
        )
        self._supports_for_update = self._engine.dialect.name != "sqlite"

    # ── Sequence allocation ──────────────────────────────────────

    def _allocate_sequence(self, session: Session, session_id: str) -> int:
        """Lock (creating if absent) the session's cursor row and return the next sequence."""
        workspace_id = current_workspace_id()
        stmt = select(SqlSessionLifecycleCursor).where(
            SqlSessionLifecycleCursor.workspace_id == workspace_id,
            SqlSessionLifecycleCursor.session_id == session_id,
        )
        if self._supports_for_update:
            stmt = stmt.with_for_update()
        cursor = session.execute(stmt).scalar_one_or_none()
        if cursor is None:
            cursor = SqlSessionLifecycleCursor(
                workspace_id=workspace_id, session_id=session_id, next_sequence=1
            )
            try:
                with session.begin_nested():
                    session.add(cursor)
                    session.flush()
            except IntegrityError:
                # Lost a race to create the cursor row; another concurrent
                # producer for this session's first-ever event won. The
                # SAVEPOINT rollback above undid only our failed insert (the
                # outer transaction/lock is untouched) — re-select-with-lock,
                # the row now exists, so the lock acquires cleanly this time.
                stmt = select(SqlSessionLifecycleCursor).where(
                    SqlSessionLifecycleCursor.workspace_id == workspace_id,
                    SqlSessionLifecycleCursor.session_id == session_id,
                )
                if self._supports_for_update:
                    stmt = stmt.with_for_update()
                cursor = session.execute(stmt).scalar_one()
        allocated = cursor.next_sequence
        cursor.next_sequence = allocated + 1
        session.flush()
        return allocated

    # ── Outbox: producer side ───────────────────────────────────

    def record_lifecycle_event(
        self,
        *,
        event_id: str,
        session_id: str,
        event_type: str,
        transition_key: str,
        payload: str,
        now: int,
    ) -> tuple[LifecycleOutboxEvent, bool]:
        with self._session("record_lifecycle_event") as session:
            existing = self._get_by_transition(session, session_id, event_type, transition_key)
            if existing is not None:
                return _outbox_to_entity(existing), False
            row = SqlSessionLifecycleOutbox(
                id=event_id,
                session_id=session_id,
                event_type=encode_session_lifecycle_event_type(event_type),
                transition_key=transition_key,
                sequence=self._allocate_sequence(session, session_id),
                event_version=1,
                payload=payload,
                status=encode_session_lifecycle_outbox_status("pending"),
                attempt_count=0,
                next_attempt_at=now,
                created_at=now,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                # Lost a race with another producer call for the same
                # transition (the unique constraint is the storage-level
                # idempotency backstop). Return the winner's row.
                existing = self._get_by_transition(session, session_id, event_type, transition_key)
                assert existing is not None
                return _outbox_to_entity(existing), False
            return _outbox_to_entity(row), True

    def _get_by_transition(
        self, session: Session, session_id: str, event_type: str, transition_key: str
    ) -> SqlSessionLifecycleOutbox | None:
        stmt = select(SqlSessionLifecycleOutbox).where(
            SqlSessionLifecycleOutbox.workspace_id == current_workspace_id(),
            SqlSessionLifecycleOutbox.session_id == session_id,
            SqlSessionLifecycleOutbox.event_type
            == encode_session_lifecycle_event_type(event_type),
            SqlSessionLifecycleOutbox.transition_key == transition_key,
        )
        return session.execute(stmt).scalar_one_or_none()

    def record_elicitation_raised(
        self,
        *,
        elicitation_id: str,
        session_id: str,
        request_payload: str,
        outbox_event_id: str,
        transition_key: str,
        outbox_payload: str,
        now: int,
    ) -> tuple[SessionElicitation, LifecycleOutboxEvent | None, bool]:
        with self._session("record_elicitation_raised") as session:
            existing = session.get(SqlSessionElicitation, (current_workspace_id(), elicitation_id))
            if existing is not None:
                return _elicitation_to_entity(existing), None, False
            elicitation = SqlSessionElicitation(
                id=elicitation_id,
                session_id=session_id,
                status=encode_session_elicitation_status("pending"),
                request_payload=request_payload,
                created_at=now,
            )
            outbox_row = SqlSessionLifecycleOutbox(
                id=outbox_event_id,
                session_id=session_id,
                event_type=encode_session_lifecycle_event_type("session.awaiting_decision"),
                transition_key=transition_key,
                sequence=self._allocate_sequence(session, session_id),
                event_version=1,
                payload=outbox_payload,
                status=encode_session_lifecycle_outbox_status("pending"),
                attempt_count=0,
                next_attempt_at=now,
                created_at=now,
            )
            try:
                with session.begin_nested():
                    session.add(elicitation)
                    session.add(outbox_row)
                    session.flush()
            except IntegrityError:
                # A concurrent retry of the same registration call (same
                # caller-supplied elicitation_id) won the race — this is the
                # same logical request, so surface the winner's rows rather
                # than erroring.
                existing = session.get(
                    SqlSessionElicitation, (current_workspace_id(), elicitation_id)
                )
                assert existing is not None
                existing_outbox = self._get_by_transition(
                    session, session_id, "session.awaiting_decision", transition_key
                )
                return (
                    _elicitation_to_entity(existing),
                    _outbox_to_entity(existing_outbox) if existing_outbox is not None else None,
                    False,
                )
            return _elicitation_to_entity(elicitation), _outbox_to_entity(outbox_row), True

    def get_elicitation(self, elicitation_id: str) -> SessionElicitation | None:
        with self._session("get_elicitation") as session:
            row = session.get(SqlSessionElicitation, (current_workspace_id(), elicitation_id))
            return _elicitation_to_entity(row) if row is not None else None

    def list_decided_undelivered(self, session_id: str) -> list[SessionElicitation]:
        decided_code = encode_session_elicitation_status("decided")
        with self._session("list_decided_undelivered") as session:
            stmt = (
                select(SqlSessionElicitation)
                .where(
                    SqlSessionElicitation.workspace_id == current_workspace_id(),
                    SqlSessionElicitation.session_id == session_id,
                    SqlSessionElicitation.status == decided_code,
                )
                .order_by(SqlSessionElicitation.decided_at.asc())
            )
            rows = session.execute(stmt).scalars().all()
            return [_elicitation_to_entity(r) for r in rows]

    def record_decision(
        self,
        elicitation_id: str,
        *,
        decision_payload: str,
        decided_by: str | None,
        now: int,
    ) -> SessionElicitation | None:
        pending_code = encode_session_elicitation_status("pending")
        with self._session("record_decision") as session:
            row = session.get(SqlSessionElicitation, (current_workspace_id(), elicitation_id))
            if row is None:
                return None
            if row.status == pending_code:
                row.status = encode_session_elicitation_status("decided")
                row.decision_payload = decision_payload
                row.decided_by = decided_by
                row.decided_at = now
                session.flush()
            return _elicitation_to_entity(row)

    def record_elicitation_resolved(
        self,
        elicitation_id: str,
        *,
        outbox_event_id: str,
        transition_key: str,
        outbox_payload: str,
        resolved_at: int,
    ) -> tuple[SessionElicitation | None, LifecycleOutboxEvent | None, bool]:
        decided_code = encode_session_elicitation_status("decided")
        with self._session("record_elicitation_resolved") as session:
            row = session.get(SqlSessionElicitation, (current_workspace_id(), elicitation_id))
            if row is None:
                return None, None, False
            if row.status != decided_code:
                # Already resolved (or never decided) — idempotent no-op;
                # surface the existing outbox row if one was already written.
                existing_outbox = self._get_by_transition(
                    session, row.session_id, "session.resumed", transition_key
                )
                return (
                    _elicitation_to_entity(row),
                    _outbox_to_entity(existing_outbox) if existing_outbox is not None else None,
                    False,
                )
            row.status = encode_session_elicitation_status("delivered_to_runner")
            row.resolved_at = resolved_at
            outbox_row = SqlSessionLifecycleOutbox(
                id=outbox_event_id,
                session_id=row.session_id,
                event_type=encode_session_lifecycle_event_type("session.resumed"),
                transition_key=transition_key,
                sequence=self._allocate_sequence(session, row.session_id),
                event_version=1,
                payload=outbox_payload,
                status=encode_session_lifecycle_outbox_status("pending"),
                attempt_count=0,
                next_attempt_at=resolved_at,
                created_at=resolved_at,
            )
            session.add(outbox_row)
            session.flush()
            return _elicitation_to_entity(row), _outbox_to_entity(outbox_row), True

    # ── Outbox: dispatcher side ─────────────────────────────────

    def claim_batch(
        self,
        *,
        limit: int,
        now: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> list[LifecycleOutboxEvent]:
        Blocker = aliased(SqlSessionLifecycleOutbox)
        blocked = (
            select(1)
            .where(
                Blocker.workspace_id == SqlSessionLifecycleOutbox.workspace_id,
                Blocker.session_id == SqlSessionLifecycleOutbox.session_id,
                Blocker.sequence < SqlSessionLifecycleOutbox.sequence,
                Blocker.status.in_(_LEASE_BLOCKING_STATUSES),
            )
            .correlate(SqlSessionLifecycleOutbox)
        )
        stmt = (
            select(SqlSessionLifecycleOutbox)
            .where(
                SqlSessionLifecycleOutbox.status.in_(_CLAIMABLE_STATUSES),
                SqlSessionLifecycleOutbox.next_attempt_at <= now,
                ~exists(blocked),
            )
            .order_by(SqlSessionLifecycleOutbox.next_attempt_at.asc())
            .limit(limit)
        )
        if self._supports_for_update:
            stmt = stmt.with_for_update(skip_locked=True)
        with self._session("claim_batch") as session:
            rows = session.execute(stmt).scalars().all()
            claimed: list[LifecycleOutboxEvent] = []
            for row in rows:
                row.status = encode_session_lifecycle_outbox_status("leased")
                row.attempt_count += 1
                row.last_attempt_at = now
                row.lease_owner = lease_owner
                row.lease_expires_at = now + lease_seconds
                claimed.append(_outbox_to_entity(row))
            session.flush()
            return claimed

    def mark_delivered(
        self, event_id: str, *, workspace_id: int, delivered_at: int, http_status: int
    ) -> None:
        leased_code = encode_session_lifecycle_outbox_status("leased")
        with self._session("mark_delivered") as session:
            row = session.get(SqlSessionLifecycleOutbox, (workspace_id, event_id))
            if row is None or row.status != leased_code:
                return
            row.status = encode_session_lifecycle_outbox_status("delivered")
            row.delivered_at = delivered_at
            row.last_http_status = http_status
            row.lease_owner = None
            row.lease_expires_at = None

    def mark_delivery_failed(
        self,
        event_id: str,
        *,
        workspace_id: int,
        next_attempt_at: int,
        dead_letter_after_attempts: int,
        http_status: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        leased_code = encode_session_lifecycle_outbox_status("leased")
        with self._session("mark_delivery_failed") as session:
            row = session.get(SqlSessionLifecycleOutbox, (workspace_id, event_id))
            if row is None or row.status != leased_code:
                return
            row.status = encode_session_lifecycle_outbox_status(
                "dead_letter" if row.attempt_count >= dead_letter_after_attempts else "pending"
            )
            row.next_attempt_at = next_attempt_at
            row.last_http_status = http_status
            row.last_error_code = error_code
            row.last_error_message = error_message
            row.lease_owner = None
            row.lease_expires_at = None

    def reclaim_expired_leases(self, *, now: int) -> int:
        leased_code = encode_session_lifecycle_outbox_status("leased")
        pending_code = encode_session_lifecycle_outbox_status("pending")
        with self._session("reclaim_expired_leases") as session:
            stmt = select(SqlSessionLifecycleOutbox).where(
                SqlSessionLifecycleOutbox.status == leased_code,
                SqlSessionLifecycleOutbox.lease_expires_at.is_not(None),
                SqlSessionLifecycleOutbox.lease_expires_at < now,
            )
            rows = session.execute(stmt).scalars().all()
            for row in rows:
                row.status = pending_code
                row.lease_owner = None
                row.lease_expires_at = None
                row.next_attempt_at = now
            session.flush()
            return len(rows)

    # ── Reads ────────────────────────────────────────────────────

    def list_deliveries(
        self,
        session_id: str,
        *,
        limit: int = 100,
        after_id: str | None = None,
    ) -> tuple[list[LifecycleOutboxEvent], str | None]:
        with self._session("list_deliveries") as session:
            stmt = (
                select(SqlSessionLifecycleOutbox)
                .where(
                    SqlSessionLifecycleOutbox.workspace_id == current_workspace_id(),
                    SqlSessionLifecycleOutbox.session_id == session_id,
                )
                .order_by(desc(SqlSessionLifecycleOutbox.sequence))
            )
            if after_id is not None:
                cursor_seq = (
                    select(SqlSessionLifecycleOutbox.sequence)
                    .where(
                        SqlSessionLifecycleOutbox.workspace_id == current_workspace_id(),
                        SqlSessionLifecycleOutbox.id == after_id,
                    )
                    .scalar_subquery()
                )
                stmt = stmt.where(SqlSessionLifecycleOutbox.sequence < cursor_seq)
            rows = session.execute(stmt.limit(limit + 1)).scalars().all()
            next_cursor = rows[limit - 1].id if len(rows) > limit else None
            page = rows[:limit]
            return [_outbox_to_entity(r) for r in page], next_cursor

    def latest_delivery(self, session_id: str) -> LifecycleOutboxEvent | None:
        with self._session("latest_delivery") as session:
            stmt = (
                select(SqlSessionLifecycleOutbox)
                .where(
                    SqlSessionLifecycleOutbox.workspace_id == current_workspace_id(),
                    SqlSessionLifecycleOutbox.session_id == session_id,
                )
                .order_by(desc(SqlSessionLifecycleOutbox.sequence))
                .limit(1)
            )
            row = session.execute(stmt).scalars().first()
            return _outbox_to_entity(row) if row is not None else None
