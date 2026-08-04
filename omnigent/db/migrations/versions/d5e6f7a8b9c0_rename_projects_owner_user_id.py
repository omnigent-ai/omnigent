"""Rename ``projects.owner_user_id`` to ``user_id``

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

Type is unchanged (``VARCHAR(128)``, nullable). Both indexes covering the
column are recreated: ``ix_projects_owner_user_id`` → ``ix_projects_user_id``
(matching the ``ix_scheduled_tasks_user_id`` precedent) and ``ix_projects_name``
keeps its name, since it is named for the ``name`` column that makes it unique —
``_is_name_conflict`` in the project store matches on that index name.

The rename is not wire-visible: ``owner_user_id`` was never part of the
``ProjectObject`` response, so no client contract changes.

Dialect strategy
----------------
- **SQLite**: cannot rename a column in place; ``batch_alter_table`` with
  ``recreate="always"`` rebuilds the table with the new column name.
- **PostgreSQL / MySQL**: native ``ALTER TABLE ... RENAME COLUMN``
  (``recreate="auto"``), no copy.

As in b3c1a2d4e5f6, the dependent indexes are dropped before the rename and
recreated after: a single batch that both renames a column and drops an index
referencing it trips Alembic's batch reflection, which maps the reflected index
onto the not-yet-renamed column.
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
    """Rename ``projects.owner_user_id`` → ``user_id``."""
    recreate: Literal["always", "auto"] = "always" if _is_sqlite() else "auto"

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
    op.create_index(
        "ix_projects_name",
        "projects",
        ["workspace_id", "user_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    """Restore the ``owner_user_id`` column name."""
    recreate: Literal["always", "auto"] = "always" if _is_sqlite() else "auto"

    op.drop_index("ix_projects_name", table_name="projects")
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
