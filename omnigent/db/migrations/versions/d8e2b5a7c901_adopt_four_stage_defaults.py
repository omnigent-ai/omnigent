"""Adopt the Open → Working → Review → Done defaults and give every ticket a stage ID.

Revision ID: d8e2b5a7c901
Revises: 7b8c9d0e1f2a

Saved configurations identical to the former five-stage default revert to NULL, so
they follow the new defaults. Planning tickets in default projects move to Working.
Rows without a stage ID get one from their numeric status, which lets readers drop
the status fallback and its index. Projects with custom stages keep them. Tickets
are rewritten in bounded pages without activity events. Downgrade restores the
index only; folded tickets remain in Working.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16
from omnigent.db.envelope import EnvelopedJSON

revision: str = "d8e2b5a7c901"
down_revision: str | None = "7b8c9d0e1f2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAGE_SIZE = 500
_STATUS_STAGES = {0: "open", 1: "in_progress", 2: "done", 3: "planning", 4: "review"}
_STAGE_STATUSES = {stage: status for status, stage in _STATUS_STAGES.items()}
_FORMER_DEFAULT = [
    ("open", "Open"),
    ("planning", "Planning"),
    ("in_progress", "Working"),
    ("review", "Review"),
    ("done", "Done"),
]
_PROJECTS = sa.table(
    "projects",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("id", Uuid16()),
    sa.column("ticket_stages", EnvelopedJSON(16383)),
)
_TICKETS = sa.table(
    "project_tickets",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("project_id", Uuid16()),
    sa.column("id", Uuid16()),
    sa.column("status", sa.SmallInteger()),
    sa.column("stage_id", sa.String(64)),
)


def _is_former_default(configuration: Mapping[str, Any]) -> bool:
    stages = configuration.get("stages") or []
    return [(stage.get("id"), stage.get("name")) for stage in stages] == _FORMER_DEFAULT and all(
        not stage.get("instructions") and stage.get("color") is None and stage.get("icon") is None
        for stage in stages
    )


def _custom_projects(connection: sa.Connection) -> set[tuple[int, Any]]:
    custom: set[tuple[int, Any]] = set()
    rows = connection.execute(
        sa.select(_PROJECTS.c.workspace_id, _PROJECTS.c.id, _PROJECTS.c.ticket_stages).where(
            _PROJECTS.c.ticket_stages.is_not(None)
        )
    ).all()
    for row in rows:
        if _is_former_default(row.ticket_stages):
            connection.execute(
                _PROJECTS.update()
                .where(_PROJECTS.c.workspace_id == row.workspace_id, _PROJECTS.c.id == row.id)
                .values(ticket_stages=None)
            )
        else:
            custom.add((row.workspace_id, row.id))
    return custom


def _rewrite_tickets(connection: sa.Connection, custom: set[tuple[int, Any]]) -> None:
    key = sa.tuple_(_TICKETS.c.workspace_id, _TICKETS.c.project_id, _TICKETS.c.id)
    query = (
        sa.select(_TICKETS)
        .where(sa.or_(_TICKETS.c.stage_id.is_(None), _TICKETS.c.stage_id == "planning"))
        .order_by(_TICKETS.c.workspace_id, _TICKETS.c.project_id, _TICKETS.c.id)
        .limit(_PAGE_SIZE)
    )
    after = None
    while True:
        page = connection.execute(query if after is None else query.where(key > after)).all()
        if not page:
            return
        for row in page:
            stage_id = row.stage_id or _STATUS_STAGES.get(row.status, "open")
            if stage_id == "planning" and (row.workspace_id, row.project_id) not in custom:
                stage_id = "in_progress"
            if stage_id == row.stage_id:
                continue
            connection.execute(
                _TICKETS.update()
                .where(
                    _TICKETS.c.workspace_id == row.workspace_id,
                    _TICKETS.c.project_id == row.project_id,
                    _TICKETS.c.id == row.id,
                )
                .values(stage_id=stage_id, status=_STAGE_STATUSES.get(stage_id, row.status))
            )
        last = page[-1]
        after = (last.workspace_id, last.project_id, last.id)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Adopting the new ticket stages requires an online migration")
    connection = op.get_bind()
    _rewrite_tickets(connection, _custom_projects(connection))
    op.drop_index("ix_project_tickets_legacy_stage", table_name="project_tickets")


def downgrade() -> None:
    op.create_index(
        "ix_project_tickets_legacy_stage",
        "project_tickets",
        ["workspace_id", "project_id", "stage_id", "status", "created_at", "id"],
    )
