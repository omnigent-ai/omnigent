"""add gateway_inference to hosts

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-30 00:00:00.000000

Adds ``hosts.gateway_inference`` — the JSON-encoded per-harness map a host
reports alongside its readiness, recording whether that harness family's launch
on the host resolves AI-Gateway-backed inference (e.g.
``'{"claude-native": true, "codex": false}'``). A family the host could not
evaluate is omitted from the map; NULL means the host never reported the map at
all (an older host build) and is treated as unknown, never as "nothing is
gateway-backed". Surfaced via ``GET /v1/hosts`` so the web UI only offers Smart
Routing where the routing apply layer can actually rewrite the launch model.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``gateway_inference`` column to ``hosts``.

    Batch mode so the DDL runs on SQLite too, and so the project's
    migration-safety test (which requires every schema change to go
    through ``batch_alter_table``) passes.
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("gateway_inference", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ``gateway_inference`` column from ``hosts``.

    Batch mode so ``DROP COLUMN`` works on SQLite (rejected by the bare
    ``op`` proxy pre-3.35).
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("gateway_inference")
