"""Storage for project Slack channel links and their audit trail.

A link (binding) ties one Slack channel, ``(slack_team_id, channel_id)``, to one
project. Both ends are unique in the database, so two claims racing for the same
channel or project can't both win. Callers check who may change a link; this
store only arbitrates and records.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProjectSlackAudit, SqlProjectSlackBinding, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError

BINDING_STATES = ("claimed", "active", "disabled", "error", "needs_reconnect")
_AUDIT_PAGE_MAX = 100


@dataclass(frozen=True)
class SlackBinding:
    """A project's Slack channel link as stored."""

    id: str
    project_id: str
    slack_team_id: str
    channel_id: str
    channel_name: str | None
    state: str
    destination: dict[str, Any] = field(default_factory=dict)
    automation_id: str | None = None
    has_relay_key: bool = False
    last_error: str | None = None
    owner_user_id: str = ""
    created_by_user_id: str = ""
    updated_by_user_id: str = ""
    created_at: int = 0
    updated_at: int = 0


@dataclass(frozen=True)
class SlackAuditEntry:
    id: str
    project_id: str
    binding_id: str | None
    actor_user_id: str | None
    action: str
    detail: dict[str, Any]
    created_at: int


class ChannelTakenError(OmnigentError):
    """The channel is already linked to another project."""

    def __init__(self, project_id: str) -> None:
        super().__init__(
            "That Slack channel is already linked to another project", code=ErrorCode.CONFLICT
        )
        self.project_id = project_id


def _binding(row: SqlProjectSlackBinding) -> SlackBinding:
    return SlackBinding(
        id=row.id,
        project_id=row.project_id,
        slack_team_id=row.slack_team_id,
        channel_id=row.channel_id,
        channel_name=row.channel_name,
        state=row.state,
        destination=dict(row.destination or {}),
        automation_id=row.automation_id,
        has_relay_key=row.relay_key_hash is not None,
        last_error=row.last_error,
        owner_user_id=row.owner_user_id,
        created_by_user_id=row.created_by_user_id,
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _audit(row: SqlProjectSlackAudit) -> SlackAuditEntry:
    return SlackAuditEntry(
        id=row.id,
        project_id=row.project_id,
        binding_id=row.binding_id,
        actor_user_id=row.actor_user_id,
        action=row.action,
        detail=dict(row.detail or {}),
        created_at=row.created_at,
    )


class ProjectSlackStore:
    """Claims, updates and releases project Slack links, and keeps their audit log."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_slack_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_slack_store", immediate=True
        )

    @staticmethod
    def _row_for_project(session: Session, project_id: str) -> SqlProjectSlackBinding | None:
        return session.scalar(
            select(SqlProjectSlackBinding).where(
                SqlProjectSlackBinding.workspace_id == current_workspace_id(),
                SqlProjectSlackBinding.project_id == project_id,
            )
        )

    @staticmethod
    def _record(
        session: Session,
        project_id: str,
        binding_id: str | None,
        actor_user_id: str | None,
        action: str,
        detail: dict[str, Any] | None,
        now: int,
    ) -> None:
        session.add(
            SqlProjectSlackAudit(
                workspace_id=current_workspace_id(),
                id=uuid.uuid4().hex,
                project_id=project_id,
                binding_id=binding_id,
                actor_user_id=actor_user_id,
                action=action,
                detail=detail or {},
                created_at=now,
            )
        )

    def get(self, project_id: str) -> SlackBinding | None:
        """The project's link, or ``None`` when it has none."""
        with self._session("get_project_slack_binding") as session:
            row = self._row_for_project(session, project_id)
            return None if row is None else _binding(row)

    def get_by_id(self, binding_id: str) -> SlackBinding | None:
        with self._session("get_slack_binding_by_id") as session:
            row = session.get(SqlProjectSlackBinding, (current_workspace_id(), binding_id))
            return None if row is None else _binding(row)

    def holder_of(self, slack_team_id: str, channel_id: str) -> str | None:
        """The project that holds a channel, if any."""
        with self._session("find_slack_channel_holder") as session:
            return session.scalar(
                select(SqlProjectSlackBinding.project_id).where(
                    SqlProjectSlackBinding.workspace_id == current_workspace_id(),
                    SqlProjectSlackBinding.slack_team_id == slack_team_id,
                    SqlProjectSlackBinding.channel_id == channel_id,
                )
            )

    def claim(
        self,
        project_id: str,
        *,
        slack_team_id: str,
        channel_id: str,
        channel_name: str | None,
        destination: dict[str, Any],
        owner_user_id: str,
        actor_user_id: str,
    ) -> SlackBinding:
        """Link the project to a channel, or change which channel it is linked to.

        A new link starts ``claimed``; changing the channel of an existing link
        resets it to ``claimed`` so the watcher is reprovisioned for the new one.

        :raises ChannelTakenError: another project already holds the channel.
        """
        now = now_epoch()

        def write(session: Session) -> SlackBinding:
            row = self._row_for_project(session, project_id)
            if row is None:
                row = SqlProjectSlackBinding(
                    workspace_id=current_workspace_id(),
                    id=uuid.uuid4().hex,
                    project_id=project_id,
                    slack_team_id=slack_team_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    state="claimed",
                    destination=destination,
                    owner_user_id=owner_user_id,
                    created_by_user_id=actor_user_id,
                    updated_by_user_id=actor_user_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                action = "linked"
            else:
                moved = (row.slack_team_id, row.channel_id) != (slack_team_id, channel_id)
                row.slack_team_id = slack_team_id
                row.channel_id = channel_id
                row.channel_name = channel_name
                row.destination = destination
                row.updated_by_user_id = actor_user_id
                row.updated_at = now
                if moved:
                    row.state = "claimed"
                    row.last_error = None
                action = "relinked" if moved else "updated"
            session.flush()
            self._record(
                session,
                project_id,
                row.id,
                actor_user_id,
                action,
                {"slack_team_id": slack_team_id, "channel_id": channel_id},
                now,
            )
            return _binding(row)

        try:
            return run_write_transaction(self._session_immediate, "claim_slack_channel", write)
        except IntegrityError as exc:
            holder = self.holder_of(slack_team_id, channel_id)
            if holder is not None and holder != project_id:
                raise ChannelTakenError(holder) from exc
            raise OmnigentError(
                "Another change to this project's Slack link landed at the same time; retry.",
                code=ErrorCode.CONFLICT,
            ) from exc

    def set_state(
        self,
        project_id: str,
        state: str,
        *,
        actor_user_id: str | None,
        last_error: str | None = None,
        automation_id: str | None = None,
        relay_key_hash: str | None = None,
        action: str | None = None,
    ) -> SlackBinding:
        """Move the link to *state*, recording why.

        :raises OmnigentError: ``NOT_FOUND`` when the project has no link.
        """
        if state not in BINDING_STATES:
            raise ValueError(f"unknown Slack binding state {state!r}")
        now = now_epoch()

        def write(session: Session) -> SlackBinding:
            row = self._row_for_project(session, project_id)
            if row is None:
                raise OmnigentError("This project has no Slack link", code=ErrorCode.NOT_FOUND)
            row.state = state
            row.last_error = last_error
            if automation_id is not None:
                row.automation_id = automation_id
            if relay_key_hash is not None:
                row.relay_key_hash = relay_key_hash
            if actor_user_id is not None:
                row.updated_by_user_id = actor_user_id
            row.updated_at = now
            self._record(
                session,
                project_id,
                row.id,
                actor_user_id,
                action or state,
                {"error": last_error} if last_error else None,
                now,
            )
            return _binding(row)

        return run_write_transaction(self._session_immediate, "set_slack_binding_state", write)

    def release(self, project_id: str, *, actor_user_id: str) -> SlackBinding | None:
        """Unlink: delete the project's link so its channel is free. ``None`` if none."""
        now = now_epoch()

        def write(session: Session) -> SlackBinding | None:
            row = self._row_for_project(session, project_id)
            if row is None:
                return None
            released = _binding(row)
            session.execute(
                delete(SqlProjectSlackBinding).where(
                    SqlProjectSlackBinding.workspace_id == current_workspace_id(),
                    SqlProjectSlackBinding.id == row.id,
                )
            )
            self._record(
                session,
                project_id,
                released.id,
                actor_user_id,
                "unlinked",
                {"slack_team_id": released.slack_team_id, "channel_id": released.channel_id},
                now,
            )
            return released

        return run_write_transaction(self._session_immediate, "release_slack_channel", write)

    def record(
        self,
        project_id: str,
        action: str,
        *,
        binding_id: str | None = None,
        actor_user_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit entry, e.g. a relay delivery or a refused event."""
        now = now_epoch()

        def write(session: Session) -> None:
            self._record(session, project_id, binding_id, actor_user_id, action, detail, now)

        run_write_transaction(self._session, "record_slack_audit", write)

    def audit(self, project_id: str, *, limit: int = 50) -> list[SlackAuditEntry]:
        """The project's audit entries, newest first."""
        with self._session("list_project_slack_audit") as session:
            rows = session.scalars(
                select(SqlProjectSlackAudit)
                .where(
                    SqlProjectSlackAudit.workspace_id == current_workspace_id(),
                    SqlProjectSlackAudit.project_id == project_id,
                )
                .order_by(SqlProjectSlackAudit.created_at.desc())
                .limit(max(1, min(limit, _AUDIT_PAGE_MAX)))
            ).all()
            return [_audit(row) for row in rows]
