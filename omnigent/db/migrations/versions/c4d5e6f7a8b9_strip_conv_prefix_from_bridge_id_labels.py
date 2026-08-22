"""strip the legacy conv_ prefix from native bridge-id label values

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-28 00:00:00.000000

Revision ``z7a2b3c4d5e6`` converted ids to bare 32-char hex, but it rewrote id
*columns* (plus the ``session_id`` echoed inside ``resource_event`` items). The
native harnesses also store a session id as a label *value* — ``value`` of the
``omnigent.<harness>_native.bridge_id`` labels — and those kept their old
``conv_<hex>`` spelling.

That splits a session in two: the terminal that launches the agent keys its
bridge directory on the (bare) session id, while the harness executor keys its
own on the (prefixed) label. The two then rendezvous in different directories,
the executor finds a foreign ``active_session_id`` in the bridge config, and
every turn fails the stale-session guard with "no longer active after /clear"
— no message ever reaches the terminal.

Rewrites the affected values in place. Idempotent: only rows still carrying the
prefix are touched, so a re-run is a no-op. ``-cleared`` suffixed values keep
their suffix (only the prefix is removed).

No downgrade: the prefix is a pre-migration spelling of the very same id, and
restoring it would re-break the sessions this fixes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every harness names its label ``omnigent.<harness>_native.bridge_id``, so one
# suffix match covers claude, codex, opencode and antigravity at once.
_LABEL_KEY_PATTERN = "%.bridge_id"
_LEGACY_PREFIX = "conv_"


def upgrade() -> None:
    """Drop the ``conv_`` prefix from every native bridge-id label value."""
    op.execute(
        sa.text(
            "UPDATE conversation_labels"
            " SET value = SUBSTR(value, :cut)"
            " WHERE key LIKE :key_pattern AND value LIKE :value_pattern"
        ).bindparams(
            # SUBSTR is 1-based in every dialect we target.
            cut=len(_LEGACY_PREFIX) + 1,
            key_pattern=_LABEL_KEY_PATTERN,
            value_pattern=f"{_LEGACY_PREFIX}%",
        )
    )


def downgrade() -> None:
    """No-op: the stripped prefix was a legacy spelling of the same id."""
