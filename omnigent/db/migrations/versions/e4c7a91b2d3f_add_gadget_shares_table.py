"""add gadget_shares table

Revision ID: e4c7a91b2d3f
Revises: 47d3e8fc3e25
Create Date: 2026-11-19 00:00:00.000000

Adds ``gadget_shares``, one row per user or group a gadget is shared with, at
read, edit or manage. Gadgets themselves live in the server's gadget store, so
the application deletes these rows when it deletes a gadget.

Additive and unused by earlier releases, so this schema can ship before the
application code that writes it. Rollout and rollback order:

- The upgrade runs at startup. A server started against the upgraded database
  needs a build that knows this revision; older builds refuse to start.
- To roll back to a build without this revision, first downgrade to
  ``47d3e8fc3e25`` with a build that knows it. The downgrade drops the table and
  any shares in it, so it is safe only before the first share is saved.

There are no foreign-key constraints (schema Rule R032).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar
from sqlalchemy.dialects.postgresql import VARCHAR as PGVarchar

revision: str = "e4c7a91b2d3f"
down_revision: str | None = "47d3e8fc3e25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Dialects that store a value of any type in any column, and ignore VARCHAR lengths.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Create the ``gadget_shares`` table."""
    constraints: list[sa.CheckConstraint] = []
    if op.get_bind().dialect.name in _SQLITE_FAMILY:
        constraints = [
            # 128 characters at up to four bytes each.
            sa.CheckConstraint(
                "length(CAST(user_id AS BLOB)) <= 512",
                name="ck_gadget_shares_user_id_bytes",
            ),
            # Gadget IDs are ASCII, so 64 bytes is the 64-character width.
            sa.CheckConstraint(
                "length(CAST(gadget_id AS BLOB)) <= 64",
                name="ck_gadget_shares_gadget_id_bytes",
            ),
            sa.CheckConstraint(
                "typeof(workspace_id) = 'integer'",
                name="ck_gadget_shares_workspace_id_integer",
            ),
            sa.CheckConstraint(
                "typeof(level) = 'integer'",
                name="ck_gadget_shares_level_integer",
            ),
        ]
    op.create_table(
        "gadget_shares",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        # The gadget store's ID: up to 64 of [a-z0-9_-], not necessarily a UUID.
        # Byte order on every engine, so listing pages continue in the index's order.
        sa.Column(
            "gadget_id",
            sa.String(64)
            .with_variant(
                MySQLVarchar(64, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
            )
            .with_variant(PGVarchar(64, collation="C"), "postgresql"),
            nullable=False,
        ),
        # String(128) matches session_permissions.user_id; a user or group ID. Compared
        # exactly on MySQL, whose default collation ignores case and accents and would let
        # two distinct principals match one grant.
        sa.Column(
            "user_id",
            sa.String(128).with_variant(
                MySQLVarchar(128, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        # Leads with workspace_id; the prefix serves per-gadget reads and deletes.
        sa.PrimaryKeyConstraint("workspace_id", "gadget_id", "user_id"),
        *constraints,
    )
    # Looks up the gadgets shared with one user, for a "shared with me" list.
    op.create_index(
        "ix_gadget_shares_user_id",
        "gadget_shares",
        ["workspace_id", "user_id", "gadget_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``gadget_shares`` index and table."""
    op.drop_index("ix_gadget_shares_user_id", table_name="gadget_shares")
    op.drop_table("gadget_shares")
