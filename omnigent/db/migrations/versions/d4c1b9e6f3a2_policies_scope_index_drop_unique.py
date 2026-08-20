"""Consolidate policies listing indexes; drop the name unique key.

Revision ID: d4c1b9e6f3a2
Revises: a7f3c1b9e2d4
Create Date: 2026-07-21 12:00:00.000000

Reworks the ``policies`` secondary indexes, all schema-only:

- Drop ``ix_policies_created_at`` (``workspace_id, created_at, id``) and
  ``ix_policies_session_id`` (``workspace_id, session_id, id``).
- Add one combined ``ix_policies_scope_session``
  (``workspace_id, scope, session_id, id``) that serves both listing paths:
  ``list_defaults`` (``WHERE workspace_id=? AND scope='default'``) rides the
  ``(workspace_id, scope)`` prefix, and ``list_for_session``
  (``WHERE workspace_id=? AND scope='session' AND session_id=?``) rides the
  full key. ``scope`` must lead ``session_id`` so the defaults query — which
  does not constrain ``session_id`` — can still seek. ``created_at`` is left
  out on purpose: with ``session_id`` between ``scope`` and ``id`` it cannot
  cover the ``ORDER BY created_at, id`` for both queries, so both sort their
  small result set in memory (as the session listing already did).
  NOTE: ``list_for_session`` gained a ``scope='session'`` predicate so it can
  seek this key; a ``session_id`` lookup without ``scope`` would table-scan.
- Drop the ``uq_policies_session_id_name_cksum`` unique constraint. Session-name
  uniqueness now lives in the store (``SqlAlchemyPolicyStore.create`` /
  ``update``), matching how default-name uniqueness has always been enforced
  there. ``ix_policies_name_cksum`` still backs those lookups.

Dropping the unique constraint runs in a ``batch_alter_table``
(``recreate="always"`` on SQLite) guarded by the same ``PRAGMA foreign_keys``
toggle as the other policy migrations. ``DROP``/``CREATE INDEX`` is native on
every dialect. Downgrade restores the two indexes and the unique key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4c1b9e6f3a2"
down_revision: str | None = "a7f3c1b9e2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Collapse the two listing indexes into one; drop the name unique key."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # Drop the changing indexes before the rebuild so batch mode doesn't copy
    # them onto the recreated table.
    op.drop_index("ix_policies_created_at", table_name="policies")
    op.drop_index("ix_policies_session_id", table_name="policies")

    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint("uq_policies_session_id_name_cksum", type_="unique")

    op.create_index(
        "ix_policies_scope_session",
        "policies",
        ["workspace_id", "scope", "session_id", "id"],
    )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Restore the split listing indexes and the (session_id, name_cksum) key."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    op.drop_index("ix_policies_scope_session", table_name="policies")

    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.create_unique_constraint(
            "uq_policies_session_id_name_cksum",
            ["workspace_id", "session_id", "name_cksum"],
        )

    op.create_index(
        "ix_policies_session_id",
        "policies",
        ["workspace_id", "session_id", "id"],
    )
    op.create_index(
        "ix_policies_created_at",
        "policies",
        ["workspace_id", "created_at", "id"],
    )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
