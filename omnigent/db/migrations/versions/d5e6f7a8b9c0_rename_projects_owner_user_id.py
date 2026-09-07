"""Rename ``projects.owner_user_id`` to ``user_id``; drop the name UNIQUE index

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-04 00:00:00.000000

Finishes the unification started in b3c1a2d4e5f6, which renamed
``hosts.owner`` and ``scheduled_tasks.owner_user_id`` to the schema-wide
``user_id`` convention. ``projects`` already existed at that point
(b1c2d3e4f5a6, five days earlier) but was left out, so it is the last column
still diverging. This brings it in line with ``session_permissions.user_id``,
``account_tokens.user_id``, ``device_grants.user_id``, ``hosts.user_id``, and
``scheduled_tasks.user_id``.

Type is unchanged (``VARCHAR(128)``, nullable). ``ix_projects_owner_user_id``
becomes ``ix_projects_user_id``, matching the ``ix_scheduled_tasks_user_id``
precedent.

``ix_projects_name`` — UNIQUE over (workspace_id, owner, name) — is **dropped,
not renamed**. It backed only the store's two ``_name_taken`` probes, which now
stand alone as the sole uniqueness check:

- It never held for single-user mode, where the owner column is NULL and SQL
  treats NULLs as distinct, so that deployment has always allowed duplicates.
- ``name`` is mutable (``update`` renames it), so a unique key over it is
  maintained on every rename.
- The ``?project=<name>`` member join tolerates duplicate names by
  construction: it unions first-class members with ``omni_project``
  label-projects matched on the same string, so name-collision merging is
  already its defined behaviour.

The cost is that two concurrent creates or renames to the same name can both
land. ``ix_projects_user_id`` still covers both probes via its
(workspace_id, user_id) prefix, then filters ``name`` over the owner's handful
of rows.

Neither change is wire-visible: the owner column was never part of the
``ProjectObject`` response, and dropping an index changes no response shape.

Dialect strategy
----------------
- **SQLite**: cannot rename a column in place; ``batch_alter_table`` with
  ``recreate="always"`` rebuilds the table with the new column name.
- **PostgreSQL / MySQL**: native ``ALTER TABLE ... RENAME COLUMN``
  (``recreate="auto"``), no copy.

As in b3c1a2d4e5f6, the dependent indexes are dropped before the rename: a
single batch that both renames a column and drops an index referencing it trips
Alembic's batch reflection, which maps the reflected index onto the
not-yet-renamed column.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Rename the owner column and drop ``ix_projects_name``."""
    recreate: Literal["always", "auto"] = "always" if _is_sqlite() else "auto"

    # Dropped for good, not recreated below — see the module docstring.
    op.drop_index("ix_projects_name", table_name="projects")
    op.drop_index("ix_projects_owner_user_id", table_name="projects")
    with op.batch_alter_table("projects", recreate=recreate) as batch_op:
        batch_op.alter_column(
            "owner_user_id",
            new_column_name="user_id",
            existing_type=sa.String(128),
            existing_nullable=True,
        )
    op.create_index(
        "ix_projects_user_id",
        "projects",
        ["workspace_id", "user_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Restore the ``owner_user_id`` column name and the UNIQUE name index.

    Recreating ``ix_projects_name`` can fail if duplicate names accumulated
    while the constraint was absent — deliberately, so the downgrade surfaces
    the conflict rather than silently discarding a row.
    """
    recreate: Literal["always", "auto"] = "always" if _is_sqlite() else "auto"

    op.drop_index("ix_projects_user_id", table_name="projects")
    with op.batch_alter_table("projects", recreate=recreate) as batch_op:
        batch_op.alter_column(
            "user_id",
            new_column_name="owner_user_id",
            existing_type=sa.String(128),
            existing_nullable=True,
        )
    op.create_index(
        "ix_projects_owner_user_id",
        "projects",
        ["workspace_id", "owner_user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_projects_name",
        "projects",
        ["workspace_id", "owner_user_id", "name"],
        unique=True,
    )
