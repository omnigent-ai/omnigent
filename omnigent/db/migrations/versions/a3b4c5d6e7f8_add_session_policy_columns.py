"""restructure policies table for session-scoped handler policies

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-06-02 00:00:00.000000

Restructures the ``policies`` table from agent-scoped prompt policies
to session-scoped handler policies. Drops unused agent-scoped columns
(``agent_id``, ``actions``, ``phases``, ``prompt``) and adds
``session_id`` (FK to conversations) and ``handler`` (text).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "b2c3d4e5f6a7"


def upgrade() -> None:
    """Restructure policies table for session-scoped handler policies."""
    bind = op.get_bind()
    if bind.dialect.name in ("mysql", "mariadb"):
        # MySQL/MariaDB: cannot drop ix_policies_agent_id while the FK
        # constraint on agent_id (auto-named by MySQL, e.g. policies_ibfk_1)
        # still exists. Discover and drop the FK first, then proceed normally.
        from sqlalchemy import inspect as sa_inspect

        fks = sa_inspect(bind).get_foreign_keys("policies")
        agent_fk = next(
            (fk for fk in fks if "agent_id" in fk["constrained_columns"]), None
        )
        if agent_fk and agent_fk.get("name"):
            with op.batch_alter_table("policies") as pre_op:
                pre_op.drop_constraint(agent_fk["name"], type_="foreignkey")

    with op.batch_alter_table("policies") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("handler", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("factory_params", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_policies_session_id",
            "conversations",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_policies_session_id", ["session_id"])
        batch_op.create_unique_constraint("uq_policies_session_id_name", ["session_id", "name"])
        batch_op.drop_index("ix_policies_agent_id")
        batch_op.drop_constraint("uq_policies_agent_id_name", type_="unique")
        batch_op.drop_column("agent_id")
        batch_op.drop_column("actions")
        batch_op.drop_column("phases")
        batch_op.drop_column("prompt")


def downgrade() -> None:
    """Restore agent-scoped columns and remove session-scoped ones."""
    with op.batch_alter_table("policies") as batch_op:
        batch_op.add_column(sa.Column("prompt", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("phases", sa.Text(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("actions", sa.Text(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("agent_id", sa.String(64), nullable=True))
        batch_op.create_unique_constraint("uq_policies_agent_id_name", ["agent_id", "name"])
        batch_op.create_index("ix_policies_agent_id", ["agent_id"])
        batch_op.drop_constraint("uq_policies_session_id_name", type_="unique")
        batch_op.drop_index("ix_policies_session_id")
        batch_op.drop_constraint("fk_policies_session_id", type_="foreignkey")
        batch_op.drop_column("factory_params")
        batch_op.drop_column("handler")
        batch_op.drop_column("session_id")
