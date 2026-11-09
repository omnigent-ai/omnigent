"""Persist admin limits on grants and runner bindings.

Revision ID: a83c29e4f617
Revises: f1c7a9e2b406

Apply before deploying credential attenuation. Defaults preserve existing grants
and bindings and permit older servers to keep writing during rollout. Rolling
back application code also rolls back enforcement of these limits.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a83c29e4f617"
down_revision: str | None = "f1c7a9e2b406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, column in (
        ("device_grants", "admin_allowed"),
        ("omnigent_conversation_metadata", "runner_admin_allowed"),
    ):
        if column not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(
                table, sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.true())
            )


def downgrade() -> None:
    op.drop_column("omnigent_conversation_metadata", "runner_admin_allowed")
    op.drop_column("device_grants", "admin_allowed")
