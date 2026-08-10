"""Session-lifecycle-push store — the transactional outbox and durable
elicitation ledger for OMN-104 (push session lifecycle events to the
configured manager webhook).

This store owns three tables: ``session_lifecycle_outbox`` (the outbox),
``session_lifecycle_cursors`` (its per-session sequence allocator), and
``session_elicitations`` (the durable human-decision ledger). See
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import LifecycleOutboxEvent, SessionElicitation


class SessionLifecycleStore(ABC):
    """Abstract base for session-lifecycle-outbox and elicitation-ledger persistence."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the session-lifecycle store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    # ── Outbox: producer side ───────────────────────────────────

    @abstractmethod
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
        """
        Insert a ``session.completed`` / ``session.failed`` outbox row.

        Idempotent via the ``(workspace_id, session_id, event_type,
        transition_key)`` unique constraint: a retried producer call for the
        same transition resolves to the existing row rather than a duplicate.

        :param event_id: Deterministic ``event_id`` (UUIDv5).
        :param session_id: The session this event reports on.
        :param event_type: ``"session.completed"`` or ``"session.failed"``.
        :param transition_key: e.g. ``"turn:{response_id}:completed"``.
        :param payload: Redacted JSON event ``data``, already serialized.
        :param now: Unix epoch seconds to stamp ``created_at``.
        :returns: ``(row, inserted)`` — ``inserted`` is ``False`` when an
            identical transition already had a row (idempotent no-op).
        """
        ...

    @abstractmethod
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
        """
        Transactionally insert the durable elicitation row and its
        ``session.awaiting_decision`` outbox row.

        :param elicitation_id: The elicitation id (``== SessionElicitation.id``).
        :param session_id: The owning session.
        :param request_payload: Allowlisted request fields, serialized JSON.
        :param outbox_event_id: Deterministic ``event_id`` for the
            ``session.awaiting_decision`` outbox row.
        :param transition_key: e.g. ``"elicitation:{id}:awaiting_decision"``.
        :param outbox_payload: Redacted JSON event ``data`` for the outbox row.
        :param now: Unix epoch seconds to stamp ``created_at``.
        :returns: ``(elicitation, outbox_event, inserted)``. ``inserted`` is
            ``False`` when the elicitation id already existed (idempotent
            no-op; ``outbox_event`` is then ``None``).
        """
        ...

    @abstractmethod
    def get_elicitation(self, elicitation_id: str) -> SessionElicitation | None:
        """Return an elicitation ledger row by id, or ``None`` if not found."""
        ...

    @abstractmethod
    def list_decided_undelivered(self, session_id: str) -> list[SessionElicitation]:
        """
        List a session's ``decided``-but-undelivered elicitations.

        Powers reconnect redelivery (§5.4's "server restarts, runner alive"
        case): on tunnel reconnect, the server re-POSTs each of these rows'
        decision to the runner rather than leaving a durably-recorded verdict
        stranded.

        :param session_id: The session whose runner just reconnected.
        :returns: Rows with ``status == "decided"``, oldest first.
        """
        ...

    @abstractmethod
    def record_decision(
        self,
        elicitation_id: str,
        *,
        decision_payload: str,
        decided_by: str | None,
        now: int,
    ) -> SessionElicitation | None:
        """
        Durably record a verdict. Conditional on ``status = pending``.

        Idempotent: a duplicate call (manager retry) after the verdict is
        already recorded is a no-op that returns the existing (unchanged) row.

        :param elicitation_id: The elicitation being decided.
        :param decision_payload: Allowlisted decision fields, serialized JSON.
        :param decided_by: Manager identity from the signed callback, or
            ``None`` for a web-UI verdict.
        :param now: Unix epoch seconds to stamp ``decided_at``.
        :returns: The row after the write, or ``None`` if no elicitation with
            that id exists.
        """
        ...

    @abstractmethod
    def record_elicitation_resolved(
        self,
        elicitation_id: str,
        *,
        outbox_event_id: str,
        transition_key: str,
        outbox_payload: str,
        resolved_at: int,
    ) -> tuple[SessionElicitation | None, LifecycleOutboxEvent | None, bool]:
        """
        Transactionally mark an elicitation ``delivered_to_runner`` and
        insert its ``session.resumed`` outbox row.

        Conditional on ``status = decided`` — only called once the runner has
        truthfully acknowledged consuming the decision.

        :returns: ``(elicitation, outbox_event, inserted)``. ``inserted`` is
            ``False`` (and ``outbox_event`` the existing row) on a duplicate
            call, or ``(None, None, False)`` if no ``decided`` elicitation
            with that id exists.
        """
        ...

    # ── Outbox: dispatcher side ─────────────────────────────────

    @abstractmethod
    def claim_batch(
        self,
        *,
        limit: int,
        now: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> list[LifecycleOutboxEvent]:
        """
        Claim up to ``limit`` deliverable rows for this replica.

        Only claims a row when no earlier (lower ``sequence``) row for the
        same ``(workspace_id, session_id)`` is still ``pending``/``leased``
        (per-session ordering — never issues event N+1 before N reaches a
        terminal delivery state). Increments ``attempt_count`` and stamps
        ``last_attempt_at``/``lease_owner``/``lease_expires_at`` as part of
        the claim.

        :param limit: Maximum rows to claim.
        :param now: Unix epoch seconds "now".
        :param lease_owner: This replica's identity.
        :param lease_seconds: How long the claim's lease is held before it
            becomes reclaimable.
        :returns: Claimed rows, each with ``status = "leased"``.
        """
        ...

    @abstractmethod
    def mark_delivered(
        self, event_id: str, *, workspace_id: int, delivered_at: int, http_status: int
    ) -> None:
        """
        Mark a leased row ``delivered``. No-op if not currently leased.

        Takes an explicit ``workspace_id`` (from the claimed
        :class:`LifecycleOutboxEvent`) rather than the request-scoped
        ``current_workspace_id()`` — the dispatcher runs outside any request,
        so no ambient workspace scope is set.
        """
        ...

    @abstractmethod
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
        """
        Release a leased row's lease after a failed delivery attempt.

        Sets ``status = dead_letter`` once ``attempt_count >=
        dead_letter_after_attempts``, else back to ``pending`` — either way it
        keeps retrying at ``next_attempt_at`` (escalation, never abandonment).
        No-op if not currently leased. See :meth:`mark_delivered` on why
        ``workspace_id`` is explicit here.
        """
        ...

    @abstractmethod
    def reclaim_expired_leases(self, *, now: int) -> int:
        """
        Reclaim rows whose lease expired without the claiming replica
        completing delivery (crash mid-claim). Returns rows to ``pending``.

        :param now: Unix epoch seconds "now".
        :returns: Number of rows reclaimed.
        """
        ...

    # ── Reads ────────────────────────────────────────────────────

    @abstractmethod
    def list_deliveries(
        self,
        session_id: str,
        *,
        limit: int = 100,
        after_id: str | None = None,
    ) -> tuple[list[LifecycleOutboxEvent], str | None]:
        """List a session's outbox rows, most recent (``sequence``) first, cursor-paginated."""
        ...

    @abstractmethod
    def latest_delivery(self, session_id: str) -> LifecycleOutboxEvent | None:
        """Return a session's most recent (highest-``sequence``) outbox row, or ``None``."""
        ...
