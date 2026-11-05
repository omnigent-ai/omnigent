"""Persistent store of the objectives an agent records for its session.

Each objective is a ``session_objectives`` row, in the database that holds the
session's metadata. Recording an objective appends one; recording an outcome
updates one: the objective it names, or else the session's latest one (within
the automation run being served, when there is one). An outcome recorded with
no objective to resolve gets a row of its own, without objective text.

Writes lock the session's ``omnigent_conversation_metadata`` row, so a session's
writes are serialized and its objective cap holds under concurrency. The
agent's words are stored as opaque envelopes (see :func:`_encode_payload`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select, union_all
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SESSION_OBJECTIVE_PAYLOAD_MAX_BYTES,
    SqlConversationMetadata,
    SqlSessionObjective,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)
from omnigent.entities.session_objective import (
    OBJECTIVES_PAGE_MAX,
    SessionObjective,
    SessionObjectiveOutcome,
    SessionObjectivePage,
    normalize_objective,
    normalize_outcome,
)
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes


def _strict_uuid(value: str) -> str:
    """The bare hex form of a UUID; raises :class:`InvalidUuidError` for anything else."""
    return uuid_to_bytes(value).hex()


# A session that records more is almost certainly looping; the cap keeps one
# session's rows, and the delete that runs with the session, bounded.
MAX_OBJECTIVES_PER_SESSION = 500
# How many runs one run-history statement reads, each with its own LIMIT.
_RUN_READS_PER_STATEMENT = 50
_COLUMNS = tuple(SqlSessionObjective.__table__.c)


class SessionObjectiveLimitError(Exception):
    """The session already holds :data:`MAX_OBJECTIVES_PER_SESSION` objectives."""


class SessionObjectiveNotFoundError(Exception):
    """The session, or the objective named in it, does not exist."""


class UnreadableSessionObjectiveError(Exception):
    """A stored envelope cannot be read by this build."""


def _encode_payload(fields: dict[str, str]) -> bytes:
    """Wrap the agent's words in the storage-private envelope.

    ``{"plaintext": {...}}`` today; the ``ciphertext`` key is reserved for an
    encrypted variant, so the stored format stays unambiguous when one arrives.
    """
    payload = json.dumps({"plaintext": fields}, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    # The normalizers bound every field well below this; a breach is a bug.
    if len(payload) > SESSION_OBJECTIVE_PAYLOAD_MAX_BYTES:
        raise ValueError("session objective payload exceeds its stored size")
    return payload


def _decode_payload(payload: bytes, keys: tuple[str, ...]) -> dict[str, str]:
    try:
        envelope = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UnreadableSessionObjectiveError("unreadable session objective envelope") from exc
    if isinstance(envelope, dict) and "ciphertext" in envelope:
        raise UnreadableSessionObjectiveError(
            "an encrypted session objective needs a store that can decrypt it"
        )
    fields = envelope.get("plaintext") if isinstance(envelope, dict) else None
    if not isinstance(fields, dict) or not all(isinstance(fields.get(k), str) for k in keys):
        raise UnreadableSessionObjectiveError("unrecognized session objective envelope")
    return {key: fields[key] for key in keys}


def _to_entity(row: Any) -> SessionObjective:
    """Build the entity from an ORM row or a result row with the table's columns."""
    objective = (
        _decode_payload(row.objective_payload, ("text",))["text"]
        if row.objective_payload is not None
        else None
    )
    outcome = None
    if row.outcome_payload is not None:
        fields = _decode_payload(row.outcome_payload, ("emoji", "text"))
        outcome = SessionObjectiveOutcome(
            success=bool(row.outcome_success),
            emoji=fields["emoji"],
            text=fields["text"],
            recorded_at=int(row.outcome_recorded_at or 0),
        )
    return SessionObjective(
        id=row.id,
        conversation_id=row.conversation_id,
        created_at=row.created_at,
        objective=objective,
        scheduled_task_run_id=row.scheduled_task_run_id,
        outcome=outcome,
    )


def encode_cursor(objective: SessionObjective) -> str:
    """The cursor that resumes listing after *objective*."""
    return f"{objective.created_at}-{objective.id}"


def _decode_cursor(cursor: str) -> tuple[int, str]:
    created_at, _, object_id = cursor.partition("-")
    try:
        return int(created_at), _strict_uuid(object_id)
    except (ValueError, InvalidUuidError) as exc:
        raise ValueError("invalid cursor") from exc


class SessionObjectiveStore:
    """SQLAlchemy-backed store of each session's objectives and outcomes."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy URI of the database holding
            ``omnigent_conversation_metadata``, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_objective_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_objective_store", immediate=True
        )

    @staticmethod
    def _lock_session(session: Session, conversation_id: str) -> None:
        """Serialize the session's writes on its metadata row; raise if it is gone."""
        found = session.execute(
            select(SqlConversationMetadata.id)
            .where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id == conversation_id,
            )
            .with_for_update()
        ).one_or_none()
        if found is None:
            raise SessionObjectiveNotFoundError(f"session {conversation_id!r} not found")

    @staticmethod
    def _in_session(conversation_id: str) -> Select[tuple[SqlSessionObjective]]:
        return select(SqlSessionObjective).where(
            SqlSessionObjective.workspace_id == current_workspace_id(),
            SqlSessionObjective.conversation_id == conversation_id,
        )

    def _append(
        self,
        session: Session,
        conversation_id: str,
        *,
        scheduled_task_run_id: str | None,
        values: dict[str, Any],
    ) -> SqlSessionObjective:
        held = session.execute(
            select(func.count()).select_from(
                select(SqlSessionObjective.id)
                .where(
                    SqlSessionObjective.workspace_id == current_workspace_id(),
                    SqlSessionObjective.conversation_id == conversation_id,
                )
                .limit(MAX_OBJECTIVES_PER_SESSION)
                .subquery()
            )
        ).scalar_one()
        if held >= MAX_OBJECTIVES_PER_SESSION:
            raise SessionObjectiveLimitError(
                f"a session holds at most {MAX_OBJECTIVES_PER_SESSION} objectives"
            )
        row = SqlSessionObjective(
            conversation_id=conversation_id,
            id=uuid.uuid4().hex,
            created_at=now_epoch_us(),
            scheduled_task_run_id=scheduled_task_run_id,
            **values,
        )
        session.add(row)
        return row

    def record_objective(
        self,
        conversation_id: str,
        objective: str,
        *,
        scheduled_task_run_id: str | None = None,
    ) -> SessionObjective:
        """Append an objective; it becomes the session's latest.

        :param conversation_id: The session, e.g. ``"0123...cdef"``.
        :param objective: What the agent sets out to do; normalized with
            :func:`~omnigent.entities.session_objective.normalize_objective`.
        :param scheduled_task_run_id: The automation run the session is
            serving, or ``None``.
        :raises ValueError: If *objective* is invalid.
        :raises SessionObjectiveNotFoundError: If the session does not exist.
        :raises SessionObjectiveLimitError: If the session is full.
        """
        payload = _encode_payload({"text": normalize_objective(objective)})

        def write(session: Session) -> SessionObjective:
            self._lock_session(session, conversation_id)
            row = self._append(
                session,
                conversation_id,
                scheduled_task_run_id=scheduled_task_run_id,
                values={"objective_payload": payload},
            )
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "record_session_objective", write)

    def record_outcome(
        self,
        conversation_id: str,
        *,
        success: bool,
        emoji: str,
        text: str,
        objective_id: str | None = None,
        scheduled_task_run_id: str | None = None,
    ) -> SessionObjective:
        """Record how an objective went, replacing any earlier outcome for it.

        :param conversation_id: The session.
        :param success: Whether the agent reached the objective.
        :param emoji: One emoji; normalized with
            :func:`~omnigent.entities.session_objective.normalize_outcome`.
        :param text: A few words on the outcome.
        :param objective_id: The objective to resolve. ``None`` resolves the
            latest objective: the latest of *scheduled_task_run_id*'s when it is
            set, else the session's. With nothing to resolve, the outcome gets
            an objective of its own, without text.
        :param scheduled_task_run_id: The automation run the session is
            serving, or ``None``; also stamped on a row this call creates.
        :returns: The objective with its new outcome.
        :raises ValueError: If the emoji or text is invalid, or *objective_id*
            is not a UUID.
        :raises SessionObjectiveNotFoundError: If the session, or the named
            objective in it, does not exist.
        :raises SessionObjectiveLimitError: If a new row is needed and the
            session is full.
        """
        emoji, text = normalize_outcome(emoji, text)
        named = _strict_uuid(objective_id) if objective_id is not None else None
        outcome = {
            "outcome_success": success,
            "outcome_payload": _encode_payload({"emoji": emoji, "text": text}),
        }

        def write(session: Session) -> SessionObjective:
            self._lock_session(session, conversation_id)
            stmt = self._in_session(conversation_id)
            if named is not None:
                stmt = stmt.where(SqlSessionObjective.id == named)
            else:
                if scheduled_task_run_id is not None:
                    stmt = stmt.where(
                        SqlSessionObjective.scheduled_task_run_id == scheduled_task_run_id
                    )
                stmt = stmt.order_by(
                    SqlSessionObjective.created_at.desc(), SqlSessionObjective.id.desc()
                ).limit(1)
            row = session.execute(stmt).scalars().one_or_none()
            recorded_at = now_epoch_us()
            if row is None:
                if named is not None:
                    raise SessionObjectiveNotFoundError(
                        f"objective {named!r} not found in session {conversation_id!r}"
                    )
                row = self._append(
                    session,
                    conversation_id,
                    scheduled_task_run_id=scheduled_task_run_id,
                    values={**outcome, "outcome_recorded_at": recorded_at},
                )
                return _to_entity(row)
            row.outcome_success = success
            row.outcome_payload = outcome["outcome_payload"]
            row.outcome_recorded_at = recorded_at
            return _to_entity(row)

        return run_write_transaction(
            self._session_immediate, "record_session_objective_outcome", write
        )

    def list_objectives(
        self, conversation_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> SessionObjectivePage:
        """One page of the session's objectives, oldest first.

        :param conversation_id: The session.
        :param limit: Page size, 1 to
            :data:`~omnigent.entities.session_objective.OBJECTIVES_PAGE_MAX`.
        :param cursor: ``next_cursor`` of the previous page, or ``None``.
        :raises ValueError: If *limit* is out of range or *cursor* is invalid.
        """
        if not 1 <= limit <= OBJECTIVES_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {OBJECTIVES_PAGE_MAX}")
        stmt = self._in_session(conversation_id)
        if cursor is not None:
            created_at, after_id = _decode_cursor(cursor)
            stmt = stmt.where(
                (SqlSessionObjective.created_at > created_at)
                | (
                    (SqlSessionObjective.created_at == created_at)
                    & (SqlSessionObjective.id > after_id)
                )
            )
        stmt = stmt.order_by(SqlSessionObjective.created_at, SqlSessionObjective.id).limit(
            limit + 1
        )
        with self._session("list_session_objectives") as session:
            rows = session.execute(stmt).scalars().all()
            data = [_to_entity(row) for row in rows[:limit]]
        return SessionObjectivePage(
            data=data, next_cursor=encode_cursor(data[-1]) if len(rows) > limit else None
        )

    def list_run_objectives(
        self, run_ids: Sequence[str], *, limit_per_run: int
    ) -> dict[str, tuple[list[SessionObjective], bool]]:
        """The latest objectives of each automation run, oldest first.

        Each run's rows come from its own limited read of
        ``ix_session_objectives_run_created``, combined into one statement
        per chunk of runs, so a run with many objectives cannot crowd out the
        others and no read scans more than ``limit_per_run + 1`` rows.

        :param run_ids: The runs, typically one page of run history.
        :param limit_per_run: How many objectives to return per run, at least 1.
        :returns: For each run that has objectives, its newest
            *limit_per_run* objectives in creation order, and whether it has more.
        """
        if limit_per_run < 1:
            raise ValueError("limit_per_run must be at least 1")
        newest: dict[str, list[Any]] = {}
        unique = list(dict.fromkeys(run_ids))
        with self._session("list_scheduled_run_objectives") as session:
            for start in range(0, len(unique), _RUN_READS_PER_STATEMENT):
                chunk = unique[start : start + _RUN_READS_PER_STATEMENT]
                reads = [
                    select(*_COLUMNS)
                    .where(
                        SqlSessionObjective.workspace_id == current_workspace_id(),
                        SqlSessionObjective.scheduled_task_run_id == run_id,
                    )
                    .order_by(SqlSessionObjective.created_at.desc(), SqlSessionObjective.id.desc())
                    .limit(limit_per_run + 1)
                    .subquery()
                    for run_id in chunk
                ]
                stmt = union_all(*(select(*read.c) for read in reads))
                for row in session.execute(stmt):
                    newest.setdefault(row.scheduled_task_run_id, []).append(row)
        found: dict[str, tuple[list[SessionObjective], bool]] = {}
        for run_id, rows in newest.items():
            rows.sort(key=lambda row: (row.created_at, row.id))
            more = len(rows) > limit_per_run
            found[run_id] = ([_to_entity(row) for row in rows[-limit_per_run:]], more)
        return found
