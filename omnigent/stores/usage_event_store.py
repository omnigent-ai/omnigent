"""Store for project usage events: enumerated names and IDs, kept in the server's database."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select

from omnigent.db.db_models import SqlUsageEvent, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.telemetry.surface import CLIENT_SURFACES
from omnigent.util.uuid import uuid_to_bytes

#: Each event's allowed property keys; anything else is dropped.
EVENT_PROPERTIES: dict[str, frozenset[str]] = {
    "message.sent": frozenset(),
    "session.started": frozenset(),
    "ticket.filed": frozenset(),
    "role.created": frozenset(),
    "role.updated": frozenset({"fields"}),
    "role.deleted": frozenset(),
    "board.read": frozenset({"action"}),
}
#: Role fields an edit may name; their values are never stored.
ROLE_FIELDS = frozenset({"name", "instructions", "agent_id", "model", "effort", "scope"})
#: What another project's Lead read of a board.
BOARD_READ_ACTIONS = frozenset({"list", "get", "comments", "stages"})
SURFACES = CLIENT_SURFACES | {"agent", "unknown"}


@dataclass(frozen=True)
class UsageEvent:
    name: str
    project_id: str | None
    user_id: str | None
    session_id: str | None
    surface: str | None
    subject_id: str | None
    props: dict[str, Any]
    created_at: int


def _hex(value: str | None) -> str | None:
    return None if value is None else uuid_to_bytes(value).hex()


def clean_properties(name: str, props: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep only an event's allowed, enumerated properties."""
    allowed = EVENT_PROPERTIES[name]
    cleaned: dict[str, Any] = {}
    for key, value in (props or {}).items():
        if key not in allowed:
            continue
        if key == "fields":
            fields = sorted({field for field in value if field in ROLE_FIELDS})
            if fields:
                cleaned[key] = fields
        elif key == "action" and value in BOARD_READ_ACTIONS:
            cleaned[key] = value
    return cleaned or None


class UsageEventStore:
    """Appends, reads and prunes usage events."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.usage_event_store"
        )

    def record(
        self,
        name: str,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        surface: str | None = None,
        subject_id: str | None = None,
        props: dict[str, Any] | None = None,
        created_at: int | None = None,
    ) -> None:
        """Append one event.

        :raises ValueError: for an unknown event name or surface.
        """
        if name not in EVENT_PROPERTIES:
            raise ValueError(f"unknown usage event {name!r}")
        if surface is not None and surface not in SURFACES:
            raise ValueError(f"unknown usage surface {surface!r}")
        row = SqlUsageEvent(
            workspace_id=current_workspace_id(),
            id=uuid.uuid4().hex,
            name=name,
            project_id=_hex(project_id),
            user_id=user_id,
            session_id=_hex(session_id),
            surface=surface,
            subject_id=None if subject_id is None else subject_id[:64],
            props=clean_properties(name, props),
            created_at=now_epoch() if created_at is None else created_at,
        )

        def write(session: Any) -> None:
            session.add(row)

        run_write_transaction(self._session, "record_usage_event", write)

    def events(
        self,
        project_id: str,
        *,
        since_epoch: int | None,
        names: Iterable[str],
    ) -> list[UsageEvent]:
        """A project's events of the given names since *since_epoch*, oldest first."""
        with self._session("list_project_usage_events") as session:
            stmt = select(SqlUsageEvent).where(
                SqlUsageEvent.workspace_id == current_workspace_id(),
                SqlUsageEvent.project_id == _hex(project_id),
                SqlUsageEvent.name.in_(tuple(names)),
            )
            if since_epoch is not None:
                stmt = stmt.where(SqlUsageEvent.created_at >= since_epoch)
            return [
                UsageEvent(
                    row.name,
                    row.project_id,
                    row.user_id,
                    row.session_id,
                    row.surface,
                    row.subject_id,
                    row.props or {},
                    row.created_at,
                )
                for row in session.scalars(stmt.order_by(SqlUsageEvent.created_at))
            ]

    def first_event_at(self) -> int | None:
        """When this server's oldest kept event was recorded, or ``None`` with none yet."""
        with self._session("first_usage_event_time") as session:
            return session.scalar(
                select(func.min(SqlUsageEvent.created_at)).where(
                    SqlUsageEvent.workspace_id == current_workspace_id()
                )
            )

    def prune(self, before_epoch: int) -> int:
        """Delete events recorded before *before_epoch*; returns how many."""

        def write(session: Any) -> int:
            result = session.execute(
                delete(SqlUsageEvent).where(
                    SqlUsageEvent.workspace_id == current_workspace_id(),
                    SqlUsageEvent.created_at < before_epoch,
                )
            )
            return int(result.rowcount or 0)

        return run_write_transaction(self._session, "prune_usage_events", write)
