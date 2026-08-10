"""Durable session-lifecycle-push entities (OMN-104) — persisted in the
``session_lifecycle_outbox`` and ``session_elicitations`` tables.

A :class:`LifecycleOutboxEvent` is one row of the transactional outbox that
durably records a ``session.completed`` / ``session.failed`` /
``session.awaiting_decision`` / ``session.resumed`` event before it is
delivered (and retried until acknowledged) to the configured manager
webhook. A :class:`SessionElicitation` is a durable ledger row recording a
human-decision elicitation's request/verdict, narrower than a general
cross-restart resume guarantee (see
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §5.4).

This module holds the plain dataclasses the store converts ORM rows into;
the store owns JSON (de)serialization of the compressed payload columns.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LifecycleOutboxEvent:
    """
    One row of the ``session_lifecycle_outbox`` transactional outbox.

    :param id: The externally stable ``event_id`` (deterministic UUIDv5, bare
        32-char hex string).
    :param session_id: The session this event reports on.
    :param event_type: One of ``"session.completed"`` / ``"session.failed"``
        / ``"session.awaiting_decision"`` / ``"session.resumed"``.
    :param transition_key: Stable source identity the event was derived
        from, e.g. ``"turn:{response_id}:completed"``.
    :param sequence: Monotonic per ``(workspace_id, session_id)``.
    :param event_version: Wire envelope version.
    :param payload: JSON event ``data`` (already redacted/allowlisted).
    :param status: Delivery status — ``"pending"`` / ``"leased"`` /
        ``"delivered"`` / ``"dead_letter"`` / ``"paused"``.
    :param attempt_count: Number of delivery attempts made so far.
    :param next_attempt_at: Unix epoch seconds; the dispatcher's claim filter.
    :param created_at: Unix epoch seconds the row was inserted.
    :param workspace_id: Tenant partition key that owns this row.
    :param lease_owner: Replica identity holding the current lease, or
        ``None``.
    :param lease_expires_at: Unix epoch seconds the current lease expires, or
        ``None``.
    :param last_attempt_at: Unix epoch seconds of the most recent delivery
        attempt, or ``None``.
    :param delivered_at: Unix epoch seconds the row reached ``delivered``, or
        ``None``.
    :param last_http_status: HTTP status of the most recent attempt, or
        ``None``.
    :param last_error_code: Sanitized failure classification, or ``None``.
    :param last_error_message: Sanitized failure detail, or ``None``.
    """

    id: str
    session_id: str
    event_type: str
    transition_key: str
    sequence: int
    event_version: int
    payload: str
    status: str
    attempt_count: int
    next_attempt_at: int
    created_at: int
    workspace_id: int = 0
    lease_owner: str | None = None
    lease_expires_at: int | None = None
    last_attempt_at: int | None = None
    delivered_at: int | None = None
    last_http_status: int | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


@dataclass
class SessionElicitation:
    """
    A durable elicitation ledger row, persisted in ``session_elicitations``.

    :param id: The elicitation id (``== elicitation_id``).
    :param session_id: The owning session.
    :param status: ``"pending"`` / ``"decided"`` / ``"delivered_to_runner"``
        / ``"expired"``.
    :param request_payload: Allowlisted elicitation request fields, as JSON.
    :param created_at: Unix epoch seconds the row was created.
    :param workspace_id: Tenant partition key that owns this row.
    :param decision_payload: Allowlisted decision fields, as JSON, or
        ``None`` until a verdict is recorded.
    :param decided_by: The manager identity from the signed callback, or
        ``None`` for a web-UI verdict.
    :param decided_at: Unix epoch seconds the verdict was durably recorded,
        or ``None``.
    :param resolved_at: Unix epoch seconds a live/reconnected runner actually
        consumed the decision, or ``None``.
    """

    id: str
    session_id: str
    status: str
    request_payload: str
    created_at: int
    workspace_id: int = 0
    decision_payload: str | None = None
    decided_by: str | None = None
    decided_at: int | None = None
    resolved_at: int | None = None
