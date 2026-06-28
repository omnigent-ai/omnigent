"""add updated_at to comments (per-session change fingerprint)

Revision ID: ecc0e25727b0
Revises: j1a2b3c4d5e6
Create Date: 2026-06-10 00:00:00.000000

Adds an ``updated_at`` column to ``comments`` so each row records when its
body or status last changed, in Unix epoch **microseconds** — microsecond
precision keeps back-to-back mutations within the same second
distinguishable to diff-based consumers while remaining an exact
integer in JavaScript (epoch-µs < ``Number.MAX_SAFE_INTEGER``). The per-session aggregate
(count + max ``updated_at``) is surfaced on ``GET /v1/sessions`` and the
``WS /v1/sessions/updates`` stream as a change fingerprint, letting web
clients refresh their comment list when another user or the agent mutates
comments.

Existing rows are backfilled with ``created_at`` scaled to microseconds
(a never-edited comment's ``updated_at`` is its creation time), after
which the column is tightened to ``NOT NULL`` to match the ORM model.
``BigInteger`` because epoch-µs overflows a 32-bit column on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ecc0e25727b0"
down_revision: str | None = "j1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Three steps because SQLite can't add a NOT NULL column without a
    # default: add nullable, backfill from created_at, then tighten.
    op.add_column("comments", sa.Column("updated_at", sa.BigInteger(), nullable=True))
    # CAST first: created_at is int4 on PostgreSQL and int4 * int4 stays
    # int4, so epoch-seconds * 1e6 overflows on any table with rows.
    # MySQL/MariaDB uses CAST(x AS SIGNED) not CAST(x AS BIGINT).
    bind = op.get_bind()
    if bind.dialect.name in ("mysql", "mariadb"):
        cast_expr = "CAST(created_at AS SIGNED)"
    else:
        cast_expr = "CAST(created_at AS BIGINT)"
    op.execute(
        f"UPDATE comments SET updated_at = {cast_expr} * 1000000 "
        "WHERE updated_at IS NULL"
    )
    with op.batch_alter_table("comments") as batch_op:
        batch_op.alter_column("updated_at", existing_type=sa.BigInteger(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("comments") as batch_op:
        batch_op.drop_column("updated_at")
