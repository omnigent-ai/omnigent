"""Add pending child spawn reservations without changing AP conversation tables.

Revision ID: as1b2c3d4e5f
Revises: b4e2d7a91c35
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision = "as1b2c3d4e5f"
down_revision = "b4e2d7a91c35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.dialects.mysql import BINARY

    digest = sa.LargeBinary(32).with_variant(BINARY(32), "mysql")
    op.create_table(
        "session_spawn_epochs",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("parent_id", Uuid16(), primary_key=True),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "session_spawns",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("creator_runner_id", sa.String(64), primary_key=True),
        sa.Column("epoch", sa.BigInteger(), primary_key=True),
        sa.Column("nonce", Uuid16(), primary_key=True),
        sa.Column("child_id", Uuid16(), nullable=False),
        sa.Column("parent_id", Uuid16(), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("account_generation", sa.String(32)),
        sa.Column("title", sa.String(768), nullable=False),
        sa.Column("request_digest", digest, nullable=False),
        sa.Column("name_digest", digest),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("phase", sa.SmallInteger(), nullable=False),
        sa.Column("create_payload", Uuid16()),
        sa.Column("input_payload", Uuid16()),
        sa.Column("input_digest", digest),
        sa.Column("file_mapping", BoundedBinary(7200)),
        sa.Column("cleanup_blob_keys", BoundedBinary(3600)),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("workspace_id", "child_id", name="uq_session_spawns_child"),
        sa.UniqueConstraint("workspace_id", "name_digest", name="uq_session_spawns_name"),
        sa.CheckConstraint("state IN (1, 2, 3, 4)", name="ck_session_spawns_state"),
        sa.CheckConstraint("phase IN (1, 2, 3, 4, 5, 6)", name="ck_session_spawns_phase"),
    )
    op.create_index(
        "ix_session_spawns_parent",
        "session_spawns",
        ["workspace_id", "parent_id", "child_id", "creator_runner_id", "epoch", "nonce"],
    )
    op.create_index(
        "ix_session_spawns_work",
        "session_spawns",
        ["workspace_id", "state", "phase", "child_id", "creator_runner_id", "epoch", "nonce"],
    )
    op.create_table(
        "session_spawn_payloads",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("child_id", Uuid16(), nullable=False),
        sa.Column("payload_digest", digest, nullable=False),
        sa.Column("encoded_digest", digest, nullable=False),
        sa.Column("cleanup_after", sa.Integer()),
        sa.Column("cleanup_attempts", sa.SmallInteger(), nullable=False),
    )
    op.create_index(
        "ix_session_spawns_global_work",
        "session_spawns",
        ["state", "phase", "workspace_id", "child_id", "creator_runner_id", "epoch", "nonce"],
    )
    op.create_index(
        "ix_session_spawns_expiry",
        "session_spawns",
        ["state", "created_at", "workspace_id", "child_id", "creator_runner_id", "epoch", "nonce"],
    )
    op.create_index(
        "ix_session_spawn_payloads_cleanup",
        "session_spawn_payloads",
        ["workspace_id", "cleanup_after", "id"],
    )
    op.create_index(
        "ix_session_spawn_payloads_global_cleanup",
        "session_spawn_payloads",
        ["cleanup_after", "workspace_id", "id"],
    )
    op.create_table(
        "session_spawn_materializations",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("child_id", Uuid16(), primary_key=True),
        sa.Column("creator_runner_id", sa.String(64), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("nonce", Uuid16(), nullable=False),
        sa.Column("create_digest", digest, nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    # Receipts fence retries and reserved IDs; do not erase them while work exists.
    table = sa.table("session_spawn_epochs", sa.column("parent_id", Uuid16()))
    if op.get_bind().execute(sa.select(table.c.parent_id).limit(1)).first() is not None:
        raise RuntimeError("Cannot remove retained child spawn receipts")
    markers = sa.table("session_spawn_materializations", sa.column("child_id", Uuid16()))
    if op.get_bind().execute(sa.select(markers.c.child_id).limit(1)).first() is not None:
        raise RuntimeError("Cannot remove retained child materialization markers")
    payloads = sa.table("session_spawn_payloads", sa.column("id", Uuid16()))
    if op.get_bind().execute(sa.select(payloads.c.id).limit(1)).first() is not None:
        raise RuntimeError("Cannot remove retained child payload cleanup records")
    op.drop_table("session_spawn_materializations")
    op.drop_table("session_spawn_payloads")
    op.drop_table("session_spawns")
    op.drop_table("session_spawn_epochs")
