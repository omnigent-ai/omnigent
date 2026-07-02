"""add projects, project_permissions, and conversations.project_id

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-07-01 00:00:00.000000

Promotes "projects" from an implicit ``omni_project`` conversation label to
a first-class, shareable entity:

- ``projects`` table: id, name, owner (``created_by``), timestamps, with
  names unique per owner.
- ``project_permissions`` table: the project ACL, the direct analogue of
  ``session_permissions`` keyed by ``(user_id, project_id)``. Session access
  inherits a project's grants, so sharing a project covers every chat in it
  (including chats filed later).
- ``conversations.project_id``: the first-class replacement for the
  ``omni_project`` label. Added as a plain column (no DB-level FK) mirroring
  the ``host_id`` precedent (a7b3c9d1e5f2) to avoid a batch rebuild of the
  large conversations table.

Backfill: for each distinct ``(owner, name)`` — where ``owner`` is a
``level >= 4`` grantee of a session carrying ``omni_project=name`` — create a
project owned by that user, point that user's matching sessions at it, and
seed the owner's project grant. Per-(owner, name) grouping preserves the
per-user separation the label model had (two users' "Client X" stay
distinct). The ``omni_project`` labels are left in place, so ``downgrade`` is
lossless.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "o1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECT_LABEL_KEY = "omni_project"
_LEVEL_OWNER = 4
# Sentinel grantees are not real owners — exclude them when deriving a
# project's owner from the session grants.
_SENTINELS = ("__public__", "__members__")


def upgrade() -> None:
    """Create the project tables + column and backfill from labels."""
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
        sa.UniqueConstraint("created_by", "name", name="uq_projects_owner_name"),
    )
    op.create_index("ix_projects_created_by", "projects", ["created_by"])

    op.create_table(
        "project_permissions",
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("level", sa.Integer, nullable=False),
        sa.CheckConstraint("level IN (1, 2, 3, 4)", name="ck_project_permissions_level"),
    )
    op.create_index(
        "ix_project_permissions_project_id",
        "project_permissions",
        ["project_id"],
    )

    op.add_column(
        "conversations",
        sa.Column("project_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])

    _backfill_projects_from_labels()


def _backfill_projects_from_labels() -> None:
    """Materialize one project per distinct ``(owner, label value)``."""
    conn = op.get_bind()
    now = int(time.time())

    owner_names = conn.execute(
        sa.text(
            "SELECT DISTINCT cl.value AS name, sp.user_id AS owner "
            "FROM conversation_labels cl "
            "JOIN session_permissions sp ON sp.conversation_id = cl.conversation_id "
            "WHERE cl.key = :key AND sp.level >= :owner_level "
            "AND sp.user_id NOT IN :sentinels"
        ).bindparams(
            sa.bindparam("sentinels", value=_SENTINELS, expanding=True),
        ),
        {"key": _PROJECT_LABEL_KEY, "owner_level": _LEVEL_OWNER},
    ).fetchall()

    for name, owner in owner_names:
        project_id = f"proj_{uuid.uuid4().hex}"
        conn.execute(
            sa.text(
                "INSERT INTO projects (id, name, created_by, created_at, updated_at) "
                "VALUES (:id, :name, :owner, :now, :now)"
            ),
            {"id": project_id, "name": name, "owner": owner, "now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO project_permissions (user_id, project_id, level) "
                "VALUES (:owner, :project_id, :level)"
            ),
            {"owner": owner, "project_id": project_id, "level": _LEVEL_OWNER},
        )
        # File this owner's matching sessions under the new project.
        conn.execute(
            sa.text(
                "UPDATE conversations SET project_id = :project_id WHERE id IN ("
                "  SELECT cl.conversation_id FROM conversation_labels cl "
                "  JOIN session_permissions sp ON sp.conversation_id = cl.conversation_id "
                "  WHERE cl.key = :key AND cl.value = :name "
                "  AND sp.user_id = :owner AND sp.level >= :owner_level"
                ")"
            ),
            {
                "project_id": project_id,
                "key": _PROJECT_LABEL_KEY,
                "name": name,
                "owner": owner,
                "owner_level": _LEVEL_OWNER,
            },
        )


def downgrade() -> None:
    """Drop the project column and tables (labels still hold membership)."""
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("project_id")
    op.drop_index("ix_project_permissions_project_id", table_name="project_permissions")
    op.drop_table("project_permissions")
    op.drop_index("ix_projects_created_by", table_name="projects")
    op.drop_table("projects")
