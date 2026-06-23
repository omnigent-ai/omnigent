"""add claude_profile to conversations

Revision ID: n1a2b3c4d5e6
Revises: m1a2b3c4d5e6
Create Date: 2026-06-17 00:00:00.000000

Adds the per-session Claude Code account profile to the conversations table
(issue #503):

- ``claude_profile``: nullable String(64) — per-session Claude Code account
  profile name (e.g. ``"work"``). NULL means use the agent spec's
  ``executor.config.claude_profile``, else the Claude CLI's default
  ``~/.claude``.

Set once via ``POST /v1/sessions`` (the new-chat account picker) and read by
the runner when it builds the claude-sdk harness spawn env on the first turn:
the runner resolves the name to a ``config_dir`` against its local
``~/.omnigent/config.yaml`` ``claude_profiles:`` block and injects it as
``CLAUDE_CONFIG_DIR`` on the spawned Claude CLI subprocess, isolating
credentials / settings / session state per profile. Only the profile name is
stored — never a path or credential.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n1a2b3c4d5e6"
down_revision: str | None = "m1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column("claude_profile", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("claude_profile")
