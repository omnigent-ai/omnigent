"""add project_id to omnigent_conversation_metadata

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-22 00:00:00.000000

Phase 1b of the projects feature (see ``designs/PROJECTS_PRD.md``): links
sessions to first-class projects. Adds a nullable ``project_id`` (Uuid16) to
``omnigent_conversation_metadata`` — the session→project membership pointer.
``NULL`` means unfiled. ``b1c2d3e4f5a6`` created the ``projects`` container;
this migration adds the column that references it, alongside the store/route
code that reads and writes it, so no column ships unused.

Additive. There are no foreign-key constraints (schema Rule R032): the
project relationship is enforced by the application, not the database. The
column coexists with the implicit ``omni_project`` label via the store's
dual-read, so existing label-projects keep working with no backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``project_id`` and its owner-scoped list index."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("project_id", Uuid16(), nullable=True))
    # Backs "list sessions in project X" and per-project counts
    # (GROUP BY project_id) scoped to the tenant partition.
    op.create_index(
        "ix_conversation_metadata_project_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "project_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the index and ``project_id`` column."""
    op.drop_index(
        "ix_conversation_metadata_project_id",
        table_name="omnigent_conversation_metadata",
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("project_id")
