"""add blob_key to files

Revision ID: hh1b2c3d4e5f
Revises: hh2c3d4e5f6a
Create Date: 2026-09-16 00:00:00.000000

Adds ``blob_key`` to the ``files`` table so a file row can point at an
artifact-store blob other than its own ``id``:

- ``blob_key``: nullable ``Uuid16`` — the artifact-store key holding the
  row's bytes. Normally equals ``id`` (each upload owns its blob). A forked
  file row sets it to the source row's blob so the fork copies no bytes and
  many rows share one blob. NULL on pre-existing rows means "the blob is
  under ``id``"; callers read the bytes as ``COALESCE(blob_key, id)``.

Left nullable with no backfill: existing rows keep their blob under ``id``
and resolve via the COALESCE fallback, so no data has to move. New rows are
written with ``blob_key`` populated (``= id`` for uploads, ``= source blob``
for fork copies).

Because a blob can now be shared, blob deletion is reference-counted: the
byte is removed only when no surviving row references that ``blob_key``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "hh1b2c3d4e5f"
down_revision: str | None = "hh2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from sqlalchemy import Column

    with op.batch_alter_table("files") as batch_op:
        batch_op.add_column(Column("blob_key", Uuid16(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("files") as batch_op:
        batch_op.drop_column("blob_key")
