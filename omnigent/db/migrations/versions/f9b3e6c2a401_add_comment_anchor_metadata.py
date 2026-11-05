"""Persist provider-specific anchor metadata on session comments.

Revision ID: f9b3e6c2a401
Revises: c7cd88f24c1c
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText

revision: str = "f9b3e6c2a401"
down_revision: str | None = "c7cd88f24c1c"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ANCHOR_BYTES_CHECK = "ck_comments_anchor_metadata_bytes"
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Add optional immutable anchor context without changing file comments."""
    if op.get_bind().dialect.name in _SQLITE_FAMILY:
        op.execute(
            "ALTER TABLE comments ADD COLUMN anchor_metadata BLOB "
            f"CONSTRAINT {_ANCHOR_BYTES_CHECK} "
            "CHECK (anchor_metadata IS NULL "
            "OR length(CAST(anchor_metadata AS BLOB)) <= 16383)"
        )
        return
    op.add_column(
        "comments",
        sa.Column("anchor_metadata", CompressedText(max_stored_bytes=16_383), nullable=True),
    )
    op.create_check_constraint(
        _ANCHOR_BYTES_CHECK,
        "comments",
        "anchor_metadata IS NULL OR octet_length(anchor_metadata) <= 16383",
    )


def downgrade() -> None:
    """Remove provider-specific anchor context."""
    with op.batch_alter_table("comments") as batch_op:
        batch_op.drop_constraint(_ANCHOR_BYTES_CHECK, type_="check")
        batch_op.drop_column("anchor_metadata")
