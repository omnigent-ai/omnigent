"""Persist account generations and revocation tombstones.

Revision ID: ar1b2c3d4e5f
Revises: hh1b2c3d4e5f
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "ar1b2c3d4e5f"
down_revision = "hh1b2c3d4e5f"
branch_labels = None
depends_on = None

_TABLES = ("users", "account_tokens", "device_grants", "scheduled_tasks", "hosts")


def upgrade() -> None:
    bind = op.get_bind()
    is_crdb = bind.dialect.name == "cockroachdb"
    for name in _TABLES:
        if "account_generation" in {
            column["name"] for column in sa.inspect(bind).get_columns(name)
        }:
            continue
        op.add_column(name, sa.Column("account_generation", sa.String(32), nullable=True))
    if "deleted_at" not in {column["name"] for column in sa.inspect(bind).get_columns("users")}:
        op.add_column("users", sa.Column("deleted_at", sa.Integer(), nullable=True))
    if is_crdb:
        # CRDB publishes new columns at commit. The column checks above let
        # startup resume if it stops between this commit and the backfill.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    users = sa.table(
        "users",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", sa.String(128)),
        sa.column("password_hash", sa.String(256)),
        sa.column("account_generation", sa.String(32)),
    )
    # Bounded batches also work on drivers that disallow interleaved streaming cursors.
    while True:
        rows = bind.execute(
            sa.select(users.c.workspace_id, users.c.id)
            .where(users.c.password_hash.is_not(None), users.c.account_generation.is_(None))
            .limit(1000)
        ).all()
        if not rows:
            break
        for workspace_id, user_id in rows:
            bind.execute(
                users.update()
                .where(users.c.workspace_id == workspace_id, users.c.id == user_id)
                .values(account_generation=uuid.uuid4().hex)
            )
    for name in _TABLES[1:]:
        owned = sa.table(
            name,
            sa.column("workspace_id", sa.BigInteger()),
            sa.column("user_id", sa.String(128)),
            sa.column("account_generation", sa.String(32)),
        )
        bind.execute(
            owned.update().values(
                account_generation=sa.select(users.c.account_generation)
                .where(users.c.workspace_id == owned.c.workspace_id, users.c.id == owned.c.user_id)
                .scalar_subquery()
            )
        )


def downgrade() -> None:
    # Revoked rows must not become passwordless live identities on downgrade.
    bind = op.get_bind()
    users = sa.table("users", sa.column("deleted_at", sa.Integer()))
    bind.execute(users.delete().where(users.c.deleted_at.is_not(None)))
    with op.batch_alter_table("users") as batch:
        batch.drop_column("deleted_at")
    for name in reversed(_TABLES):
        with op.batch_alter_table(name) as batch:
            batch.drop_column("account_generation")
