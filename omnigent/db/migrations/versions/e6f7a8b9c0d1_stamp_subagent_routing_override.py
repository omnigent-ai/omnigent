"""stamp subagent_routing_override on already-routed sessions

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-04 00:00:00.000000

The per-session subagent-routing switch used to be tri-state: unset meant
"inherit", and the spawn gate re-derived routing from the session's own (or its
parent's) ``cost_control_mode_override``. The switch is now two-state — only an
explicit ``"on"`` routes spawns — and the create route stamps ``"on"`` on
sessions that start on Smart Routing.

Sessions that already exist carry no stamp, so without this backfill a live
Smart Routing session would silently stop routing its subagents the moment the
new gate ships. This writes the value the old inherit resolution would have
produced: ``"on"`` wherever the row has no ``subagent_routing_override`` key and
either its own or its parent's ``cost_control_mode_override`` is ``"on"``. Rows
with an explicit ``"on"``/``"off"`` are already unambiguous and are left alone.

Data-only: no DDL, so nothing to batch for SQLite.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)

# Key order the store writes ``conversations.session_overrides`` in, inlined so
# this migration keeps working if the store's tuple later changes shape.
_SESSION_OVERRIDE_KEYS = (
    "reasoning_effort",
    "model_override",
    "cost_control_mode_override",
    "subagent_routing_override",
    "harness_override",
)

# ``conversations.session_overrides`` is a ``VARCHAR(512)``; a longer blob would
# be truncated or rejected, so an over-long row is skipped rather than corrupted.
_SESSION_OVERRIDES_MAX_LEN = 512

# Rows read per round trip, so a large conversations table is not loaded whole.
_CHUNK = 500


def _decode(raw: str | None) -> dict[str, Any]:
    """Parse one ``session_overrides`` blob, tolerating junk.

    :param raw: The stored JSON object string, or ``None`` for an unset blob.
    :returns: The decoded mapping, or ``{}`` when absent or unparseable.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _encode(overrides: dict[str, Any]) -> str:
    """Re-serialize an override mapping in the store's fixed key order.

    :param overrides: Mapping of override key to value; ``None`` values and
        unknown keys are dropped, exactly as the store's encoder does.
    :returns: Compact JSON object string.
    """
    data = {
        key: overrides[key] for key in _SESSION_OVERRIDE_KEYS if overrides.get(key) is not None
    }
    return json.dumps(data, separators=(",", ":"))


def upgrade() -> None:
    """Stamp ``subagent_routing_override="on"`` on already-routed sessions.

    Reads each conversation next to its parent's overrides, then writes back
    only the rows whose blob actually changed.
    """
    bind = op.get_bind()
    select_chunk = sa.text(
        "SELECT c.workspace_id AS workspace_id, c.id AS id,"
        " c.session_overrides AS session_overrides,"
        " p.session_overrides AS parent_session_overrides"
        " FROM conversations c"
        " LEFT JOIN conversations p"
        " ON p.workspace_id = c.workspace_id AND p.id = c.parent_conversation_id"
        " ORDER BY c.workspace_id, c.id"
        " LIMIT :limit OFFSET :offset"
    )
    pending: list[dict[str, Any]] = []
    offset = 0
    while True:
        rows = bind.execute(select_chunk, {"limit": _CHUNK, "offset": offset}).mappings().all()
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            overrides = _decode(row["session_overrides"])
            if overrides.get("subagent_routing_override") is not None:
                continue
            parent = _decode(row["parent_session_overrides"])
            if "on" not in (
                overrides.get("cost_control_mode_override"),
                parent.get("cost_control_mode_override"),
            ):
                continue
            overrides["subagent_routing_override"] = "on"
            blob = _encode(overrides)
            if len(blob) > _SESSION_OVERRIDES_MAX_LEN:
                _logger.warning(
                    "skipping subagent-routing stamp for conversation %s:"
                    " session_overrides would be %d chars (max %d)",
                    row["id"],
                    len(blob),
                    _SESSION_OVERRIDES_MAX_LEN,
                )
                continue
            pending.append(
                {
                    "workspace_id": row["workspace_id"],
                    "id": row["id"],
                    "session_overrides": blob,
                }
            )
        if len(rows) < _CHUNK:
            break

    if not pending:
        return
    update = sa.text(
        "UPDATE conversations SET session_overrides = :session_overrides"
        " WHERE workspace_id = :workspace_id AND id = :id"
    )
    for params in pending:
        bind.execute(update, params)


def downgrade() -> None:
    """No-op: the stamped values are indistinguishable from user intent.

    Every value this migration wrote is exactly what the old tri-state inherit
    resolution already produced for that row, so leaving it in place keeps
    pre-downgrade behavior intact. Clearing it would be lossy — the same rows
    may since have been set to ``"on"`` deliberately through the UI, and this
    migration cannot tell the two apart.
    """
