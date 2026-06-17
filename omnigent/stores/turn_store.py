"""
Persistent store for server-authoritative turns.

A *turn* is the durable record of a single agent response/dispatch
(``turn_id`` == the ``resp_...`` response/task id). Promoting it to a
first-class, leased row is what decouples a turn's lifecycle from the
client connection: a client disconnect updates only ``attached`` /
``last_client_seen`` and can never transition a ``RUNNING`` turn. See
issue #466 and ``designs/PHASE1_SERVER_AUTHORITATIVE_TURNS.md``.

This store is the **persistence half** of Phase 1. The pure
state-machine and failure-taxonomy logic it relies on lives in
:mod:`omnigent.runtime.turn_state`. The runtime wiring that *calls* this
store — the server dispatch call-site, the runner-side heartbeat task,
and the periodic orphan-sweep scheduler — is intentionally **not** here;
it lands in a later PR. Every method on this store is exercisable with a
SQLite database and an injected clock, with no tunnel or asyncio.

Concurrency model (both reviewers converged on this):

* The **server** owns ``lease_epoch`` and the ``QUEUED -> RUNNING`` claim.
  ``conversations.runner_id`` is *affinity* (durable routing), not a
  lease; the lease (``lease_epoch`` / ``lease_owner`` / ``lease_expires_at``)
  is per-turn execution ownership that expires and fences stale writers.
* ``lease_epoch`` is bumped in exactly **two** places: a claim
  (:meth:`claim`) and a sweeper takeover (:meth:`sweep_expired`). A
  heartbeat renews ``lease_expires_at`` only and never bumps the epoch.
* Every ownership-sensitive write is a **fenced compare-and-set**:
  ``UPDATE ... WHERE id=:id AND lease_owner=:me AND lease_epoch=:epoch
  AND status=:expected``. Zero rows updated => the caller lost the lease
  (e.g. the sweeper already declared it ``RUNNER_LOST`` and bumped the
  epoch) and must abort, not retry.

The clock is always injected (callers pass ``now`` / ``ttl``) so a single
server clock governs both heartbeat writes and sweep comparisons — no
cross-host skew — and tests are deterministic.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import Engine, and_, select, update

from omnigent.db.db_models import SqlIdempotencyKey, SqlTurn
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.runtime.turn_state import (
    ERROR_RUNNER_LOST,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    SWEEPABLE_STATUSES,
    is_terminal,
    validate_transition,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Turn:
    """
    A server-authoritative turn record.

    Mirrors :class:`omnigent.db.db_models.SqlTurn`; see that model for
    per-column semantics. This dataclass is the read-side entity returned
    by the store so callers don't hold a live ORM row past the session.

    :param id: Turn id (== the ``resp_...`` response/task id).
    :param conversation_id: Owning conversation/session id.
    :param status: Lifecycle state (see
        :mod:`omnigent.runtime.turn_state`).
    :param error_code: Failure taxonomy value on a terminal failure, else
        ``None``.
    :param error_message: Optional human-readable failure detail.
    :param vendor: Worker vendor the turn dispatched to.
    :param intent: Send intent that created the turn
        (``enqueue`` / ``steer`` / ``attach``).
    :param input_json: The send payload, persisted before work starts.
    :param lease_owner: Runner id currently owning execution, or ``None``.
    :param lease_epoch: Monotonic fencing token.
    :param last_heartbeat_at: Epoch seconds of the last heartbeat, or
        ``None``.
    :param lease_expires_at: Epoch seconds the lease expires, or ``None``.
    :param attached: Whether a client is currently observing the stream.
    :param last_client_seen: Epoch seconds a client was last attached, or
        ``None``.
    :param created_at: Epoch seconds the row was created.
    :param start_ts: Epoch seconds the turn entered ``RUNNING``, or
        ``None``.
    :param end_ts: Epoch seconds the turn reached a terminal state, or
        ``None``.
    :param checkpoint_id: Phase 4 forward-compat; always ``None`` here.
    """

    id: str
    conversation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    vendor: str
    intent: str
    input_json: str
    lease_owner: str | None
    lease_epoch: int
    last_heartbeat_at: int | None
    lease_expires_at: int | None
    attached: bool
    last_client_seen: int | None
    created_at: int
    start_ts: int | None
    end_ts: int | None
    checkpoint_id: str | None


def _to_turn(row: SqlTurn) -> Turn:
    """
    Convert a :class:`SqlTurn` ORM row to a :class:`Turn` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A detached :class:`Turn` dataclass instance.
    """
    return Turn(
        id=row.id,
        conversation_id=row.conversation_id,
        status=row.status,
        error_code=row.error_code,
        error_message=row.error_message,
        vendor=row.vendor,
        intent=row.intent,
        input_json=row.input_json,
        lease_owner=row.lease_owner,
        lease_epoch=row.lease_epoch,
        last_heartbeat_at=row.last_heartbeat_at,
        lease_expires_at=row.lease_expires_at,
        attached=row.attached,
        last_client_seen=row.last_client_seen,
        created_at=row.created_at,
        start_ts=row.start_ts,
        end_ts=row.end_ts,
        checkpoint_id=row.checkpoint_id,
    )


def request_fingerprint(payload: str) -> str:
    """
    Return the SHA-256 hex digest of an idempotency request payload.

    Stored alongside the idempotency key so a second use of the same key
    with a *different* body is detected and rejected (HTTP 422) rather
    than silently returning the wrong turn.

    :param payload: The canonicalized request body to fingerprint.
    :returns: 64-char hex SHA-256 digest.
    """
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IdempotencyConflictError(Exception):
    """
    Raised when an idempotency key is reused with a different payload.

    The caller maps this to HTTP 422: the key was already bound to a turn
    created from a different request body, so returning the stored turn
    would silently serve the wrong work.

    :param key: The conflicting idempotency key.
    :param turn_id: The turn the key is already bound to.
    """

    def __init__(self, key: str, turn_id: str) -> None:
        super().__init__(
            f"idempotency key {key!r} already bound to turn {turn_id!r} "
            f"with a different request fingerprint"
        )
        self.key = key
        self.turn_id = turn_id


class TurnStore:
    """
    SQLAlchemy-backed store for server-authoritative turns.

    All ownership-sensitive mutations are fenced compare-and-sets that
    return a boolean (or ``None``) indicating whether the caller still
    held the lease, never raising on a lost-lease miss — losing a lease
    is an expected runtime outcome, not an error.

    :param storage_location: SQLAlchemy database URI, e.g.
        ``"sqlite:///omnigent.db"`` or ``"postgresql://..."``.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the store, creating or reusing the shared engine.

        :param storage_location: SQLAlchemy database URI.
        """
        self._engine: Engine = get_or_create_engine(storage_location)
        # ``immediate=True`` takes the SQLite write lock at BEGIN so the
        # check-then-act in :meth:`upsert_idempotency_key` and the fenced
        # CAS updates can't interleave with a racing dispatch; a no-op on
        # PostgreSQL, which fences via the partial-unique index + the
        # ``WHERE``-guarded UPDATE rowcount.
        self._session = make_managed_session_maker(self._engine, immediate=True)

    # ── creation / reads ───────────────────────────────────────────

    def create_turn(
        self,
        *,
        turn_id: str,
        conversation_id: str,
        vendor: str,
        intent: str,
        input_json: str,
        now: int,
    ) -> Turn:
        """
        Insert a new turn in ``CREATED`` and return it.

        The row is persisted *before* any work is forwarded to a runner,
        so an idempotent replay can resolve back to it and a crash before
        dispatch leaves a recoverable record rather than lost intent.

        A second active turn for the same conversation violates the
        ``ux_turns_one_active_per_conversation`` partial-unique index and
        raises :class:`sqlalchemy.exc.IntegrityError`; the API layer
        converts that to a ``409 Attach-Required``.

        :param turn_id: The turn id (== ``resp_...`` task id).
        :param conversation_id: Owning conversation id.
        :param vendor: Worker vendor, e.g. ``"claude_code"``.
        :param intent: Send intent (``enqueue`` / ``steer`` / ``attach``).
        :param input_json: The send payload, stored durably.
        :param now: Current epoch seconds (injected clock).
        :returns: The created :class:`Turn`.
        """
        with self._session() as session:
            row = SqlTurn(
                id=turn_id,
                conversation_id=conversation_id,
                status=STATUS_CREATED,
                vendor=vendor,
                intent=intent,
                input_json=input_json,
                lease_epoch=0,
                attached=False,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _to_turn(row)

    def get_turn(self, turn_id: str) -> Turn | None:
        """
        Return the turn with id *turn_id*, or ``None`` if absent.

        :param turn_id: The turn id to look up.
        :returns: The :class:`Turn`, or ``None``.
        """
        with self._session() as session:
            row = session.get(SqlTurn, turn_id)
            return _to_turn(row) if row is not None else None

    # ── lease lifecycle (fenced) ───────────────────────────────────

    def claim(
        self,
        *,
        turn_id: str,
        runner_id: str,
        now: int,
        ttl: int,
    ) -> int | None:
        """
        Claim a queued turn for *runner_id*, transitioning it to ``RUNNING``.

        This is the server-side claim: it bumps ``lease_epoch`` (one of
        only two places that does), stamps ``lease_owner`` /
        ``lease_expires_at`` / ``start_ts``, and moves the turn to
        ``RUNNING`` — all guarded so exactly one claim can win. The runner
        then carries the returned epoch as a fencing token and never
        mutates it.

        The guard accepts ``CREATED`` or ``QUEUED`` as the source (an
        interactive/attach dispatch may claim straight from ``CREATED``),
        consistent with :data:`omnigent.runtime.turn_state` legality.

        :param turn_id: The turn to claim.
        :param runner_id: The claiming runner's id (the pinned
            ``conversations.runner_id``).
        :param now: Current epoch seconds (injected clock).
        :param ttl: Lease time-to-live in seconds; ``lease_expires_at``
            becomes ``now + ttl``.
        :returns: The new ``lease_epoch`` on success, or ``None`` if the
            turn was not claimable (already running/terminal, or gone).
        """
        with self._session() as session:
            row = session.get(SqlTurn, turn_id)
            if row is None:
                return None
            if row.status not in (STATUS_CREATED, STATUS_QUEUED):
                # Already claimed, terminal, or paused — not claimable.
                return None
            validate_transition(row.status, STATUS_RUNNING)
            new_epoch = row.lease_epoch + 1
            # Fenced CAS: re-assert the observed status so a concurrent
            # claim that won first leaves this one updating zero rows.
            result = session.execute(
                update(SqlTurn)
                .where(
                    and_(
                        SqlTurn.id == turn_id,
                        SqlTurn.status == row.status,
                        SqlTurn.lease_epoch == row.lease_epoch,
                    )
                )
                .values(
                    status=STATUS_RUNNING,
                    lease_owner=runner_id,
                    lease_epoch=new_epoch,
                    last_heartbeat_at=now,
                    lease_expires_at=now + ttl,
                    start_ts=now,
                )
            )
            if result.rowcount != 1:
                return None
            return new_epoch

    def heartbeat(
        self,
        *,
        turn_id: str,
        runner_id: str,
        lease_epoch: int,
        now: int,
        ttl: int,
    ) -> bool:
        """
        Renew the lease on a running turn, extending ``lease_expires_at``.

        A heartbeat **only** pushes out ``lease_expires_at`` (to
        ``now + ttl``) and stamps ``last_heartbeat_at``; it never bumps
        ``lease_epoch`` and never changes ``status``. The CAS is fenced on
        owner + epoch + sweepable status, so once the sweeper has declared
        the turn ``RUNNER_LOST`` (bumping the epoch), the now-stale runner's
        heartbeat updates zero rows and learns it lost the lease.

        :param turn_id: The turn to heartbeat.
        :param runner_id: The runner asserting ownership.
        :param lease_epoch: The epoch the runner believes it holds.
        :param now: Current epoch seconds (injected clock).
        :param ttl: Lease time-to-live in seconds.
        :returns: ``True`` if the lease was renewed; ``False`` if the
            caller no longer holds it (stale epoch, wrong owner, or the
            turn left a sweepable status).
        """
        with self._session() as session:
            result = session.execute(
                update(SqlTurn)
                .where(
                    and_(
                        SqlTurn.id == turn_id,
                        SqlTurn.lease_owner == runner_id,
                        SqlTurn.lease_epoch == lease_epoch,
                        SqlTurn.status.in_(tuple(SWEEPABLE_STATUSES)),
                    )
                )
                .values(last_heartbeat_at=now, lease_expires_at=now + ttl)
            )
            return result.rowcount == 1

    def complete(
        self,
        *,
        turn_id: str,
        runner_id: str,
        lease_epoch: int,
        terminal_status: str,
        now: int,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """
        Transition a running turn to a terminal state under the lease.

        Used by the runner supervisor for ``COMPLETED`` /
        ``FAILED(WORKER_*)`` / ``CANCELLED``. The write is a fenced CAS on
        owner + epoch, so a runner that was already declared ``RUNNER_LOST``
        by the sweeper (epoch bumped) cannot overwrite the terminal record
        — its ``complete`` returns ``False`` and it reconciles/discards
        instead of split-braining. The reconnect path that handles that
        rejection is specified for the wiring PR.

        :param turn_id: The turn to finalize.
        :param runner_id: The runner asserting ownership.
        :paramlease_epoch: The epoch the runner believes it holds.
        :param terminal_status: One of ``COMPLETED`` / ``FAILED`` /
            ``CANCELLED``.
        :param now: Current epoch seconds (injected clock).
        :param error_code: Failure taxonomy value (required-by-convention
            for ``FAILED``; ``None`` for ``COMPLETED``).
        :param error_message: Optional human-readable detail.
        :returns: ``True`` if the turn was finalized; ``False`` if the
            caller no longer holds the lease.
        :raises ValueError: If *terminal_status* is not terminal.
        """
        if not is_terminal(terminal_status):
            raise ValueError(f"complete() requires a terminal status, got {terminal_status!r}")
        with self._session() as session:
            result = session.execute(
                update(SqlTurn)
                .where(
                    and_(
                        SqlTurn.id == turn_id,
                        SqlTurn.lease_owner == runner_id,
                        SqlTurn.lease_epoch == lease_epoch,
                        SqlTurn.status.in_(tuple(SWEEPABLE_STATUSES)),
                    )
                )
                .values(
                    status=terminal_status,
                    error_code=error_code,
                    error_message=error_message,
                    end_ts=now,
                )
            )
            return result.rowcount == 1

    def sweep_expired(self, *, now: int, limit: int = 100) -> list[str]:
        """
        Mark expired leased turns ``FAILED(RUNNER_LOST)`` and return their ids.

        The server-side orphan sweeper. It selects sweepable turns
        (``RUNNING`` / ``PAUSING``) whose ``lease_expires_at < now`` and
        transitions each to ``FAILED`` with ``error_code=RUNNER_LOST``,
        **bumping ``lease_epoch``**. The epoch bump is the fence: if the
        runner was merely disconnected (not dead) and reconnects, its
        heartbeat/complete CAS on the old epoch now fails, so it cannot
        resurrect a turn the server has already given up on.

        Critically, this flips **DB state only** — it does not and cannot
        terminate runner-side execution. A disconnect shorter than the TTL
        leaves the turn ``RUNNING`` and results flush on reconnect; only a
        disconnect longer than the TTL surfaces as ``RUNNER_LOST`` (Phase 1:
        surfaced, never auto-resumed). TTL sizing is therefore the single
        knob that buys disconnect tolerance.

        :param now: Current epoch seconds (injected clock).
        :param limit: Max turns to sweep in one pass (batching for large
            backlogs).
        :returns: The ids of turns transitioned to ``RUNNER_LOST`` this
            pass.
        """
        swept: list[str] = []
        with self._session() as session:
            candidates = session.execute(
                select(SqlTurn.id, SqlTurn.lease_epoch, SqlTurn.status)
                .where(
                    and_(
                        SqlTurn.status.in_(tuple(SWEEPABLE_STATUSES)),
                        SqlTurn.lease_expires_at.is_not(None),
                        SqlTurn.lease_expires_at < now,
                    )
                )
                .order_by(SqlTurn.lease_expires_at.asc())
                .limit(limit)
            ).all()
            for turn_id, epoch, status in candidates:
                validate_transition(status, STATUS_FAILED)
                # Fenced CAS per row: only sweep if the epoch is unchanged
                # since we read it, so we never race a legitimate claim or
                # a concurrent sweeper replica.
                result = session.execute(
                    update(SqlTurn)
                    .where(
                        and_(
                            SqlTurn.id == turn_id,
                            SqlTurn.lease_epoch == epoch,
                            SqlTurn.status.in_(tuple(SWEEPABLE_STATUSES)),
                        )
                    )
                    .values(
                        status=STATUS_FAILED,
                        error_code=ERROR_RUNNER_LOST,
                        lease_epoch=epoch + 1,
                        end_ts=now,
                    )
                )
                if result.rowcount == 1:
                    swept.append(turn_id)
        return swept

    # ── client liveness (observation only) ─────────────────────────

    def mark_client_liveness(self, *, turn_id: str, attached: bool, now: int) -> None:
        """
        Record client attach/detach without ever touching turn lifecycle.

        This is the **only** method the HTTP/SSE observation path may call,
        and by construction its ``UPDATE ... SET`` clause writes *only*
        ``attached`` and ``last_client_seen`` — never ``status``,
        ``lease_*``, or any execution column. A unit test asserts the
        emitted SQL touches nothing else. This is design rule 1 enforced in
        code, not convention: a dropped client can never kill a turn.

        :param turn_id: The turn whose client liveness changed.
        :param attached: ``True`` on attach, ``False`` on disconnect.
        :param now: Current epoch seconds (injected clock); recorded as
            ``last_client_seen``.
        """
        with self._session() as session:
            session.execute(
                update(SqlTurn)
                .where(SqlTurn.id == turn_id)
                .values(attached=attached, last_client_seen=now)
            )

    # ── idempotency ────────────────────────────────────────────────

    def lookup_idempotency_key(self, key: str) -> tuple[str, str] | None:
        """
        Return ``(turn_id, request_fingerprint)`` for *key*, or ``None``.

        :param key: The client-generated idempotency key.
        :returns: A ``(turn_id, request_fingerprint)`` tuple if the key is
            known, else ``None``.
        """
        with self._session() as session:
            row = session.get(SqlIdempotencyKey, key)
            if row is None:
                return None
            return (row.turn_id, row.request_fingerprint)

    def upsert_idempotency_key(
        self,
        *,
        key: str,
        conversation_id: str,
        turn_id: str,
        fingerprint: str,
        now: int,
    ) -> str:
        """
        Bind an idempotency key to a turn, or return the existing binding.

        On a fresh key, inserts the binding and returns *turn_id*. On a
        replay with a **matching** fingerprint, returns the previously
        bound turn id (the caller then serves that turn rather than
        creating a new one). On a replay with a **different** fingerprint,
        raises :class:`IdempotencyConflictError` (HTTP 422).

        :param key: The client-generated idempotency key.
        :param conversation_id: Owning conversation id.
        :param turn_id: The turn to bind on first use.
        :param fingerprint: SHA-256 of the canonicalized request payload
            (see :func:`request_fingerprint`).
        :param now: Current epoch seconds (injected clock).
        :returns: The bound turn id — *turn_id* on first use, or the
            stored turn id on a matching replay.
        :raises IdempotencyConflictError: On a same-key/different-body
            reuse.
        """
        with self._session() as session:
            existing = session.get(SqlIdempotencyKey, key)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError(key, existing.turn_id)
                return existing.turn_id
            session.add(
                SqlIdempotencyKey(
                    key=key,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    request_fingerprint=fingerprint,
                    created_at=now,
                )
            )
            return turn_id

    def purge_idempotency_keys_before(self, *, cutoff: int) -> int:
        """
        Delete idempotency keys created before *cutoff* (TTL GC).

        A cheap retention sweep so the table doesn't grow unbounded; the
        cutoff is the client retry horizon (the design suggests ~7 days).
        Bindings are also cascade-deleted with their conversation.

        :param cutoff: Epoch-seconds threshold; keys with
            ``created_at < cutoff`` are removed.
        :returns: The number of keys deleted.
        """
        from sqlalchemy import delete as sql_delete

        with self._session() as session:
            result = session.execute(
                sql_delete(SqlIdempotencyKey).where(SqlIdempotencyKey.created_at < cutoff)
            )
            return result.rowcount or 0
