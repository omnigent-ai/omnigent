"""Change hosts primary key to (workspace_id, host_id).

Revision ID: v1a2b3c4d5e6
Revises: u1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Previously the ``hosts`` PK was ``(workspace_id, owner, name)`` with
``host_id`` carrying its own ``UNIQUE`` constraint (``uq_hosts_host_id``).
This migration promotes ``host_id`` into the PK alongside ``workspace_id``,
demotes ``owner`` and ``name`` to regular NOT NULL columns, drops the now-
redundant ``uq_hosts_host_id`` constraint, and adds a new
``uq_hosts_workspace_owner_name`` unique constraint so the upsert-on-connect
rotation logic (which looks up by ``(workspace_id, owner, name)`` to detect a
rotated ``host_id``) remains consistent.

Dialect strategy
----------------
- **SQLite**: cannot ALTER a primary key in place; uses
  ``batch_alter_table(recreate="always", copy_from=<spec>)`` to rebuild the
  table from an explicit definition.  PRAGMA foreign_keys is toggled off/on
  around the rebuild to prevent cascade issues.
- **PostgreSQL / MySQL**: supports native ALTER TABLE DDL to drop and recreate
  the primary key and swap the unique constraints without a table copy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "v1a2b3c4d5e6"
down_revision: str | None = "u1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dialect() -> str:
    return op.get_bind().dialect.name


# Explicit table spec used as the ``copy_from`` reference for the SQLite batch
# recreate.  Alembic uses this definition (not the live schema) when building
# the replacement table, so the PK and constraints in the spec are the ones
# that end up in the recreated table.
_UPGRADED_TABLE = sa.Table(
    "hosts",
    sa.MetaData(),
    sa.Column("workspace_id", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("host_id", sa.String(64), nullable=False),
    sa.Column("owner", sa.String(256), nullable=False),
    sa.Column("name", sa.String(64), nullable=False),
    # status is SmallInteger after u1a2b3c4d5e6 (enums→int migration).
    sa.Column("status", sa.SmallInteger, nullable=False),
    sa.Column("created_at", sa.Integer),
    sa.Column("updated_at", sa.Integer),
    sa.Column("token_hash", sa.String(64), nullable=True),
    sa.Column("token_expires_at", sa.Integer, nullable=True),
    sa.Column("sandbox_provider", sa.String(32), nullable=True),
    sa.Column("sandbox_id", sa.String(256), nullable=True),
    sa.Column("configured_harnesses", sa.Text, nullable=True),
    sa.PrimaryKeyConstraint("workspace_id", "host_id", name="pk_hosts"),
    sa.UniqueConstraint("workspace_id", "owner", "name", name="uq_hosts_workspace_owner_name"),
    sa.UniqueConstraint("token_hash", name="uq_hosts_token_hash"),
    # u1a2b3c4d5e6 created this integer-coded check; preserve it through the
    # PK rebuild so it survives in both the upgraded and downgraded states.
    sa.CheckConstraint("status IN (1, 2)", name="ck_hosts_status"),
)

_DOWNGRADED_TABLE = sa.Table(
    "hosts",
    sa.MetaData(),
    sa.Column("workspace_id", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("host_id", sa.String(64), nullable=False),
    sa.Column("owner", sa.String(256), nullable=False),
    sa.Column("name", sa.String(64), nullable=False),
    # status is SmallInteger (u1a2b3c4d5e6 is still applied on downgrade).
    sa.Column("status", sa.SmallInteger, nullable=False),
    sa.Column("created_at", sa.Integer),
    sa.Column("updated_at", sa.Integer),
    sa.Column("token_hash", sa.String(64), nullable=True),
    sa.Column("token_expires_at", sa.Integer, nullable=True),
    sa.Column("sandbox_provider", sa.String(32), nullable=True),
    sa.Column("sandbox_id", sa.String(256), nullable=True),
    sa.Column("configured_harnesses", sa.Text, nullable=True),
    sa.PrimaryKeyConstraint("workspace_id", "owner", "name", name="pk_hosts"),
    sa.UniqueConstraint("host_id", name="uq_hosts_host_id"),
    sa.UniqueConstraint("token_hash", name="uq_hosts_token_hash"),
    # u1a2b3c4d5e6 renamed the string check to an integer one with the same
    # name. The downgrade of u1a2b3c4d5e6 will drop it; keep it here so the
    # table round-trips correctly through the enums downgrade.
    sa.CheckConstraint("status IN (1, 2)", name="ck_hosts_status"),
)


def upgrade() -> None:
    """Promote host_id to PK; demote owner+name; swap unique constraints."""
    dialect = _dialect()

    if dialect == "sqlite":
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))
        with op.batch_alter_table("hosts", copy_from=_UPGRADED_TABLE, recreate="always"):
            pass
        op.execute(sa.text("PRAGMA foreign_keys = ON"))

    else:
        # PostgreSQL / MySQL: native ALTER TABLE DDL — no table copy needed.
        with op.batch_alter_table("hosts") as batch_op:
            # Drop old PK and the unique constraint that is being promoted.
            batch_op.drop_constraint("pk_hosts", type_="primary")
            batch_op.drop_constraint("uq_hosts_host_id", type_="unique")
            # New PK covering (workspace_id, host_id).
            batch_op.create_primary_key("pk_hosts", ["workspace_id", "host_id"])
            # Uniqueness on (workspace_id, owner, name) replaces the PK role.
            batch_op.create_unique_constraint(
                "uq_hosts_workspace_owner_name", ["workspace_id", "owner", "name"]
            )


def downgrade() -> None:
    """Restore (workspace_id, owner, name) PK; restore uq_hosts_host_id."""
    dialect = _dialect()

    if dialect == "sqlite":
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))
        with op.batch_alter_table("hosts", copy_from=_DOWNGRADED_TABLE, recreate="always"):
            pass
        op.execute(sa.text("PRAGMA foreign_keys = ON"))

    else:
        with op.batch_alter_table("hosts") as batch_op:
            batch_op.drop_constraint("pk_hosts", type_="primary")
            batch_op.drop_constraint("uq_hosts_workspace_owner_name", type_="unique")
            batch_op.create_primary_key("pk_hosts", ["workspace_id", "owner", "name"])
            batch_op.create_unique_constraint("uq_hosts_host_id", ["host_id"])
