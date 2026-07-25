"""Index policies by a name checksum instead of the raw name.

Revision ID: x1a2b3c4d5e6
Revises: w1a2b3c4d5e6
Create Date: 2026-07-08 00:00:00.000000

The ``policies`` table enforced name uniqueness on the ``VARCHAR(256)``
``name`` column via two structures:

- ``ix_policies_default_name`` — partial UNIQUE index on ``name`` where
  ``scope = 1`` (default policies must have globally-unique names).
- ``uq_policies_session_id_name`` — composite UNIQUE on ``(session_id, name)``
  (session-scoped policies must have unique names within a session).

This migration adds a ``name_cksum`` column holding ``sha256(name)`` (a fixed
32-byte digest) and repoints both structures at it, so the index entries are
compact and fixed-width instead of a wide varchar. Uniqueness semantics are
unchanged: two names collide iff their digests do.

SQLite has no ``sha256()`` SQL function, so ``name_cksum`` is back-filled in
Python. The NOT NULL flip and the constraint swap run in a
``batch_alter_table`` (``recreate="always"`` on SQLite) guarded by the same
``PRAGMA foreign_keys`` toggle as the surrounding policy migrations.

Column type by dialect: ``LargeBinary`` renders as ``BYTEA`` (Postgres) /
``BLOB`` (SQLite), but MySQL cannot index a ``BLOB`` without a key-prefix
length, so the column is ``BINARY(32)`` on MySQL — an exact fit for the digest
and fully indexable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

revision: str = "x1a2b3c4d5e6"
down_revision: str | None = "w1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# BYTEA/BLOB elsewhere, BINARY(32) on MySQL (BLOB is not indexable there).
_CKSUM32 = sa.LargeBinary(length=32).with_variant(MySQLBinary(32), "mysql")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _name_cksum(name: str) -> bytes:
    """sha256 digest of a policy name (kept self-contained in the migration)."""
    return hashlib.sha256(name.encode("utf-8")).digest()


def _backfill_name_cksum() -> None:
    """Compute ``name_cksum`` from ``name`` for every existing row, in Python."""
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT workspace_id, id, name FROM policies")).fetchall()
    for workspace_id, policy_id, name in rows:
        bind.execute(
            sa.text(
                "UPDATE policies SET name_cksum = :cksum "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"cksum": _name_cksum(name), "workspace_id": workspace_id, "id": policy_id},
        )


def upgrade() -> None:
    """
    1. Add ``name_cksum`` as nullable so we can back-fill before NOT NULL.
    2. Back-fill ``name_cksum = sha256(name)`` in Python.
    3. Drop the old ``name``-keyed index/constraint; add the ``name_cksum`` ones;
       make ``name_cksum`` NOT NULL.
    """
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # ADD COLUMN is native on SQLite; add nullable first so back-fill can run.
    op.add_column("policies", sa.Column("name_cksum", _CKSUM32, nullable=True))
    _backfill_name_cksum()

    # Partial-index predicate references scope; drop it before the batch rebuild
    # so batch mode doesn't copy a stale index, then create the replacement.
    op.drop_index("ix_policies_default_name", table_name="policies")

    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.alter_column("name_cksum", existing_type=_CKSUM32, nullable=False)
        batch_op.drop_constraint("uq_policies_session_id_name", type_="unique")
        batch_op.create_unique_constraint(
            "uq_policies_session_id_name_cksum", ["session_id", "name_cksum"]
        )

    op.create_index(
        "ix_policies_default_name_cksum",
        "policies",
        ["name_cksum"],
        unique=True,
        sqlite_where=sa.text("scope = 1"),
        postgresql_where=sa.text("scope = 1"),
    )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Restore the ``name``-keyed index/constraint and drop ``name_cksum``."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    op.drop_index("ix_policies_default_name_cksum", table_name="policies")

    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint("uq_policies_session_id_name_cksum", type_="unique")
        batch_op.create_unique_constraint("uq_policies_session_id_name", ["session_id", "name"])
        batch_op.drop_column("name_cksum")

    op.create_index(
        "ix_policies_default_name",
        "policies",
        ["name"],
        unique=True,
        sqlite_where=sa.text("scope = 1"),
        postgresql_where=sa.text("scope = 1"),
    )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
