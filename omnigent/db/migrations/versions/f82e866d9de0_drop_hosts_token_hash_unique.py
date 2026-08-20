"""Drop the hosts token_hash unique constraint.

Revision ID: f82e866d9de0
Revises: d4c1b9e6f3a2
Create Date: 2026-07-21 13:00:00.000000

Removes ``uq_hosts_token_hash`` (``workspace_id, token_hash``). The launch-token
auth path no longer looks a host up by its token digest: the tunnel endpoint is
``/hosts/{host_id}/tunnel``, so ``resolve_launch_token`` now seeks the row by the
``(workspace_id, host_id)`` primary key and compares the stored digest to the
presented token's digest in Python (constant-time). With the lookup keyed on the
PK, nothing rides this constraint, and its uniqueness guarantee was never load-
bearing — launch tokens are 256-bit ``secrets.token_urlsafe(32)`` values whose
digests do not collide in practice.

Dropping the unique constraint runs in a ``batch_alter_table``
(``recreate="always"`` on SQLite) guarded by the ``PRAGMA foreign_keys`` toggle
the other host migrations use. Downgrade restores the constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f82e866d9de0"
down_revision: str | None = "d4c1b9e6f3a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Drop the (workspace_id, token_hash) unique constraint on hosts."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    with op.batch_alter_table("hosts", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint("uq_hosts_token_hash", type_="unique")

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Restore the (workspace_id, token_hash) unique constraint on hosts."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    with op.batch_alter_table("hosts", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.create_unique_constraint("uq_hosts_token_hash", ["workspace_id", "token_hash"])

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
