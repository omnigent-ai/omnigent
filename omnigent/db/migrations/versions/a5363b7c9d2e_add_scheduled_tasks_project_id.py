"""Add first-class Project membership to scheduled tasks.

Revision ID: a5363b7c9d2e
Revises: ga1b2c3d4e5f
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "a5363b7c9d2e"
down_revision = "ga1b2c3d4e5f"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_scheduled_tasks_project_id"
_INDEX_COLUMNS = ["workspace_id", "user_id", "project_id", "created_at", "id"]


def _create_project_index() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(_INDEX_NAME, "scheduled_tasks", _INDEX_COLUMNS, unique=False)
        return
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX_NAME,
            "scheduled_tasks",
            _INDEX_COLUMNS,
            unique=False,
            postgresql_concurrently=True,
        )


def _drop_project_index() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.drop_index(_INDEX_NAME, table_name="scheduled_tasks")
        return
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="scheduled_tasks",
            postgresql_concurrently=True,
        )


def upgrade() -> None:
    """Add the nullable Project pointer and filtered-list index."""
    op.add_column(
        "scheduled_tasks",
        sa.Column("project_id", Uuid16(), nullable=True),
    )
    _create_project_index()


def downgrade() -> None:
    """Remove Project assignment while preserving task and run rows."""
    _drop_project_index()
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table(
        "scheduled_tasks", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.drop_column("project_id")
