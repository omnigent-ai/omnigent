"""add git source columns to agents

Revision ID: ga10gitsrc01
Revises: d5e9f1a2b3c4
Create Date: 2026-07-30 00:00:00.000000

Adds git-source provenance to the agents table so a template agent can be
imported from a git repo and later refreshed to the tracked branch's HEAD:

- ``git_url``: nullable String(512) — clone URL. NULL ⇒ not git-backed.
- ``git_ref``: nullable String(256) — tracked branch, e.g. ``main``.
- ``git_subpath``: nullable String(512) — agent dir within the repo; NULL ⇒ root.
- ``git_commit``: nullable String(64) — last resolved commit SHA.
- ``git_host_id``: nullable String(128) — host that cloned the repo, so a
  refresh re-clones on the same host (it holds the ambient git credentials).

All columns are nullable with NO server default, so every pre-existing agent
row reads NULL and is treated as a non-git (upload-sourced) agent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ga10gitsrc01"
down_revision: str | None = "d5e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("git_url", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("git_ref", sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column("git_subpath", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("git_commit", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("git_host_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("git_host_id")
        batch_op.drop_column("git_commit")
        batch_op.drop_column("git_subpath")
        batch_op.drop_column("git_ref")
        batch_op.drop_column("git_url")
