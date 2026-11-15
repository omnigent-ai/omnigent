"""Add workflow planning sessions.

Revision ID: 9a2c7e6b4f10
Revises: 3b9e4c71d2a8

Existing workflows remain runnable. Apply before upgrading the server.

Rollback: stop writers, then use a build containing this migration to run
``OMNIGENT_DB_URL=<uri> alembic -c omnigent/db/alembic.ini downgrade
9a2c7e6b4f10@3b9e4c71d2a8`` before starting the older server. This preserves
the automation branch, removes drafts and session links, and keeps saved plans.
Draft cleanup commits bounded batches and can be restarted.
Decrypt saved plans with the deployment's codec before downgrading; all plans
are checked before the first draft is removed.
"""

from __future__ import annotations

import json
from contextlib import nullcontext

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision: str = "9a2c7e6b4f10"
down_revision: str | None = "3b9e4c71d2a8"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_CLEANUP_BATCH = 200


def _commit_cockroach_batch(connection: sa.Connection) -> None:
    # The CRDB migration caller owns the transaction, so Alembic cannot autocommit it.
    if connection.dialect.name == "cockroachdb":
        connection.commit()
        connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def upgrade() -> None:
    """Link a workflow to its planning conversation."""
    with op.batch_alter_table("workflows") as batch:
        batch.add_column(sa.Column("planning_session_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Older servers cannot interpret a workflow with an empty plan."""
    workflows = sa.table(
        "workflows",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("plan", CompressedText()),
    )
    connection = op.get_bind()
    # Preserve preceding revisions whose DDL already committed if preflight fails.
    _commit_cockroach_batch(connection)
    key = sa.tuple_(workflows.c.workspace_id, workflows.c.id)
    query = sa.select(workflows).order_by(workflows.c.workspace_id, workflows.c.id)
    # A later encrypted page must not strand a partially deleted draft set.
    try:
        first_page = connection.execute(query.limit(_CLEANUP_BATCH)).all()
        page = first_page
        while page:
            after = (page[-1].workspace_id, page[-1].id)
            page = connection.execute(query.where(key > after).limit(_CLEANUP_BATCH)).all()
    except ValueError as exc:
        raise RuntimeError(
            "Workflow plans must be readable before downgrading; decrypt encrypted plans first"
        ) from exc
    page = first_page
    cockroach = connection.dialect.name == "cockroachdb"
    if page:
        # Each cleanup statement commits independently; the revision is advanced
        # only after cleanup and DDL finish, so an interrupted downgrade is retryable.
        with nullcontext() if cockroach else op.get_context().autocommit_block():
            while page:
                drafts = [
                    (row.workspace_id, row.id) for row in page if not json.loads(row.plan)["steps"]
                ]
                if drafts:
                    connection.execute(workflows.delete().where(key.in_(drafts)))
                _commit_cockroach_batch(connection)
                after = (page[-1].workspace_id, page[-1].id)
                page = connection.execute(query.where(key > after).limit(_CLEANUP_BATCH)).all()
    _commit_cockroach_batch(connection)
    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("planning_session_id")
    _commit_cockroach_batch(connection)
