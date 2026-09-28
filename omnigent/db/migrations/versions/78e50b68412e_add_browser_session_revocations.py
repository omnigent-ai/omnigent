"""add browser_session_revocations table

Revision ID: 78e50b68412e
Revises: mm1a2b3c4d5e
Create Date: 2026-09-27 00:00:00.000000

Adds ``browser_session_revocations``: one row per browser session (``sid``)
ended by logout, kept until the session's absolute expiry so no replica, and
no restart, can accept or renew a cookie from that session again.

The table is brand-new, so it carries the tenant-partition ``workspace_id``
column as the leading primary-key member like every other table, no foreign
keys, and no SQL ``DEFAULT`` values: the application supplies every column.
The epoch-second columns are 64-bit, since ``expires_at`` follows an
operator-configurable session lifetime and can pass the 32-bit limit.

Deployment: purely additive. Nothing reads or writes the table until an
application release that records logouts ships, so this revision can be
applied, and rolled back, on its own. Apply it before that release; the
server also applies it on startup. Roll that release back before
downgrading this revision: it reads the table on every cookie-authenticated
request. As with every migration, a build that predates this revision
refuses to start against a database at it, so downgrade before rolling the
schema release back. The downgrade drops the table: done while the
application has recorded logouts, sessions logged out before it can be
accepted again until they expire, so rotate the session cookie secret or
wait out the maximum session lifetime. Deployments that apply schema
outside alembic must create the same table and index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "78e50b68412e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``browser_session_revocations`` table."""
    op.create_table(
        "browser_session_revocations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("sid", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("revoked_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "sid"),
    )
    op.create_index(
        "ix_browser_session_revocations_expires_at",
        "browser_session_revocations",
        ["workspace_id", "expires_at", "sid"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``browser_session_revocations`` table."""
    op.drop_index(
        "ix_browser_session_revocations_expires_at",
        table_name="browser_session_revocations",
    )
    op.drop_table("browser_session_revocations")
