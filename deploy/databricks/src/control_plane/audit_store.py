"""Append-only audit log for governed control-plane actions.

Backs the "audited action" requirement for delegated registration and
visibility changes. Writes are best-effort from the route handlers; the
log is read by admins via ``GET /v1/control-plane/audit``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import desc, select

from control_plane.models import SqlAgentAudit
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)

logger = logging.getLogger("omnigent-app.control_plane.audit")


@dataclass(frozen=True)
class AuditEntry:
    """One audit-log row.

    :param id: Autoincrement id.
    :param ts: Epoch seconds of the action.
    :param actor: Email of the actor.
    :param action: Action name, e.g. ``"publish"``.
    :param agent_id: Affected agent id, or ``None``.
    :param detail: Free-text detail, or ``None``.
    """

    id: int
    ts: int
    actor: str
    action: str
    agent_id: str | None
    detail: str | None


class AuditStore:
    """SQLAlchemy-backed audit log.

    :param storage_location: SQLAlchemy DB URI; shares the engine/pool.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def record(
        self,
        *,
        actor: str,
        action: str,
        agent_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append an audit row.

        :param actor: Email of the user performing the action.
        :param action: Short action name, e.g. ``"publish"`` or
            ``"visibility_change"``.
        :param agent_id: The affected agent id, if any.
        :param detail: Free-text detail.

        Best-effort by contract: an audit write must never fail the governed
        action it records (the action has usually already committed by the
        time we get here). A write failure is logged and swallowed.
        """
        try:
            with self._session() as session:
                session.add(
                    SqlAgentAudit(
                        ts=now_epoch(),
                        actor=actor,
                        action=action,
                        agent_id=agent_id,
                        detail=detail,
                    )
                )
        except Exception:  # noqa: BLE001 — best-effort: log, never fail the action
            logger.warning(
                "control_plane.audit: failed to record action=%s agent=%s actor=%s",
                action,
                agent_id,
                actor,
                exc_info=True,
            )

    def list_recent(self, limit: int = 100) -> list[AuditEntry]:
        """Return the most recent audit rows, newest first.

        :param limit: Maximum rows to return (1–1000).
        :returns: List of :class:`AuditEntry`.
        """
        limit = max(1, min(limit, 1000))
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlAgentAudit)
                    .order_by(desc(SqlAgentAudit.ts), desc(SqlAgentAudit.id))
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                AuditEntry(
                    id=r.id,
                    ts=r.ts,
                    actor=r.actor,
                    action=r.action,
                    agent_id=r.agent_id,
                    detail=r.detail,
                )
                for r in rows
            ]
