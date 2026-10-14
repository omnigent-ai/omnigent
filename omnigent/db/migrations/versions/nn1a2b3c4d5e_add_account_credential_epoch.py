"""add credential epochs to accounts users and credentials

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-14 00:00:00.000000

Adds a nullable ``credential_epoch`` (32-character hex) to ``users``,
``account_tokens`` and ``device_grants``. A user's epoch names the current
set of sign-in credentials, separately from ``account_generation``, which
identifies the registration that owns hosts and scheduled tasks. Rotating it
is a single-row update of ``users``; no owned rows change.

``NULL`` means the epoch has never been rotated, so existing rows need no
backfill and every credential issued before this revision stays valid.

Deployment: additive and nullable; older application code ignores the
columns. Apply before or after the application release that reads them. Roll
back the application first, then downgrade this revision if needed; dropping
the columns discards rotation state, so credentials revoked by a rotation
would validate again under older code until they expire.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "credential_epoch"
_TABLES = ("users", "account_tokens", "device_grants")


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        # MySQL and CockroachDB commit DDL per statement; skip columns a
        # previously interrupted upgrade already added.
        if _COLUMN in {column["name"] for column in sa.inspect(bind).get_columns(table)}:
            continue
        op.add_column(table, sa.Column(_COLUMN, sa.String(32), nullable=True))


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column(_COLUMN)
