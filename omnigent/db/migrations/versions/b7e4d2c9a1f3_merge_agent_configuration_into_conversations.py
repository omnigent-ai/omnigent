"""Merge agent_configuration back into conversations.

Revision ID: b7e4d2c9a1f3
Revises: c7d2e9f4a1b8
Create Date: 2026-07-20 00:00:00.000000

Reverses ``bb2c3d4e5f6a``: the 1-to-1 ``agent_configuration`` companion table
is folded back onto ``conversations``. The agent binding (``agent_id``) returns
as a first-class indexed column, and the four per-session overrides
(reasoning_effort, model_override, cost_control_mode_override, harness_override)
collapse into a single nullable ``session_overrides`` JSON blob (``VARCHAR(512)``)
that is ``NULL`` when the session uses all agent/spec defaults.

The overrides were never filtered in SQL — only ever read/written alongside the
conversation — so a blob loses no query capability while dropping a table, an
extra INSERT, a JOIN, and the paired-row repair/fork/delete plumbing. ``agent_id``
stays a real column so the agent→conversation reverse lookup and the
``agent_id`` / ``has_agent_id`` / ``agent_name`` list filters remain index-backed
(``ix_conversations_agent_id``).

Data copy (both directions in Python, keyset-batched by primary key to bound
memory): SQL JSON construction is not portable, and — more importantly — the
copy must not rely on column-to-column type coercion. ``bb2c3d4e5f6a`` created
``agent_configuration.agent_id`` / ``conversation_id`` as ``VARCHAR`` even though
``conversations`` stores ids as 16 raw bytes (``Uuid16``); a SQL
``UPDATE … SET binary_col = (SELECT varchar_col …)`` would fail on Postgres/MySQL
(binary vs varchar), invisibly to SQLite. So every id is normalised to 16 bytes
in Python (:func:`_as_uuid_bytes`) before it is bound into a binary column,
making the migration correct regardless of the source column's declared type or
stored form.

Column type by dialect: ``agent_id`` is ``Uuid16`` (16 raw bytes) — ``BYTEA``
(Postgres) / ``BLOB`` (SQLite), ``BINARY(16)`` on MySQL. ``session_overrides``
is a plain bounded ``VARCHAR(512)`` (the length is a cap, not a preallocation;
NULL/short blobs cost only their bytes).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16, uuid_to_bytes

revision: str = "b7e4d2c9a1f3"
down_revision: str | None = "c7d2e9f4a1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed key order so the encoded object is stable across writes. Mirrors the
# store's _SESSION_OVERRIDE_KEYS (kept self-contained in the migration).
_OVERRIDE_KEYS = (
    "reasoning_effort",
    "model_override",
    "cost_control_mode_override",
    "harness_override",
)
_BACKFILL_BATCH = 1000


def _as_uuid_bytes(value: object) -> bytes | None:
    """Normalise a stored id to the 16 raw bytes a ``Uuid16`` column holds.

    Accepts whatever the driver returns for the source column, regardless of
    its declared type: ``None``; 16 raw bytes / ``memoryview`` (binary column);
    or a 32-char hex string, optionally dashed / legacy-prefixed, possibly
    delivered as ``bytes`` (varchar column). This lets the copy target a binary
    column on every dialect without relying on implicit type coercion.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) == 16:
            return raw
        # A varchar id surfaced as bytes (e.g. hex text) — decode then normalise.
        return uuid_to_bytes(raw.decode("ascii"))
    return uuid_to_bytes(value)  # str / uuid.UUID


def _encode_overrides(values: dict[str, str | None]) -> str | None:
    """Pack set overrides into a compact JSON blob, or ``None`` when all unset."""
    data = {key: values[key] for key in _OVERRIDE_KEYS if values.get(key) is not None}
    return json.dumps(data, separators=(",", ":")) if data else None


def _backfill_binding_and_overrides() -> None:
    """Copy ``agent_configuration`` onto ``conversations`` (agent_id + blob).

    Pages over ``agent_configuration`` by its ``(workspace_id, conversation_id)``
    primary key so memory stays bounded to one batch. Only rows that carry a
    binding or at least one override trigger an UPDATE; all-default rows keep both
    columns NULL. Ids are normalised to bytes so the binary ``conversations``
    columns are written correctly on every dialect.
    """
    bind = op.get_bind()
    last_ws: int | None = None
    last_id: object = None
    while True:
        if last_ws is None:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, conversation_id, agent_id, reasoning_effort,"
                    " model_override, cost_control_mode_override, harness_override"
                    " FROM agent_configuration"
                    " ORDER BY workspace_id, conversation_id LIMIT :lim"
                ),
                {"lim": _BACKFILL_BATCH},
            ).fetchall()
        else:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, conversation_id, agent_id, reasoning_effort,"
                    " model_override, cost_control_mode_override, harness_override"
                    " FROM agent_configuration"
                    " WHERE workspace_id > :ws"
                    "    OR (workspace_id = :ws AND conversation_id > :id)"
                    " ORDER BY workspace_id, conversation_id LIMIT :lim"
                ),
                {"ws": last_ws, "id": last_id, "lim": _BACKFILL_BATCH},
            ).fetchall()
        if not rows:
            break
        for ws, conv_id, agent_id, reasoning, model, cost, harness in rows:
            agent_bytes = _as_uuid_bytes(agent_id)
            blob = _encode_overrides(
                {
                    "reasoning_effort": reasoning,
                    "model_override": model,
                    "cost_control_mode_override": cost,
                    "harness_override": harness,
                }
            )
            if agent_bytes is not None or blob is not None:
                bind.execute(
                    sa.text(
                        "UPDATE conversations SET agent_id = :aid, session_overrides = :so"
                        " WHERE workspace_id = :ws AND id = :id"
                    ),
                    {"aid": agent_bytes, "so": blob, "ws": ws, "id": _as_uuid_bytes(conv_id)},
                )
        last_ws, last_id = rows[-1][0], rows[-1][1]
        if len(rows) < _BACKFILL_BATCH:
            break


def _restore_agent_configuration_overrides() -> None:
    """Downgrade: fan ``conversations.session_overrides`` back out to columns.

    Pages over ``conversations`` by its ``(workspace_id, id)`` primary key,
    parses each blob, and writes the four override columns onto the (already
    inserted, 1-to-1) ``agent_configuration`` rows.
    """
    bind = op.get_bind()
    last_ws: int | None = None
    last_id: object = None
    while True:
        if last_ws is None:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, id, session_overrides FROM conversations"
                    " WHERE session_overrides IS NOT NULL"
                    " ORDER BY workspace_id, id LIMIT :lim"
                ),
                {"lim": _BACKFILL_BATCH},
            ).fetchall()
        else:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, id, session_overrides FROM conversations"
                    " WHERE session_overrides IS NOT NULL"
                    "   AND (workspace_id > :ws OR (workspace_id = :ws AND id > :id))"
                    " ORDER BY workspace_id, id LIMIT :lim"
                ),
                {"ws": last_ws, "id": last_id, "lim": _BACKFILL_BATCH},
            ).fetchall()
        if not rows:
            break
        for ws, conv_id, blob in rows:
            data = json.loads(blob) if blob else {}
            bind.execute(
                sa.text(
                    "UPDATE agent_configuration SET"
                    " reasoning_effort = :reasoning_effort,"
                    " model_override = :model_override,"
                    " cost_control_mode_override = :cost_control_mode_override,"
                    " harness_override = :harness_override"
                    " WHERE workspace_id = :ws AND conversation_id = :id"
                ),
                {
                    "reasoning_effort": data.get("reasoning_effort"),
                    "model_override": data.get("model_override"),
                    "cost_control_mode_override": data.get("cost_control_mode_override"),
                    "harness_override": data.get("harness_override"),
                    "ws": ws,
                    "id": _as_uuid_bytes(conv_id),
                },
            )
        last_ws, last_id = rows[-1][0], rows[-1][1]
        if len(rows) < _BACKFILL_BATCH:
            break


def upgrade() -> None:
    """
    1. Add ``agent_id`` and ``session_overrides`` to ``conversations`` (nullable).
    2. Copy the binding + overrides across in Python (byte-normalised ids).
    3. Create ``ix_conversations_agent_id``; drop ``ix_agent_configuration_agent_id``.
    4. Drop the ``agent_configuration`` table.

    Adding nullable columns is native DDL on every dialect, so no table rebuild
    / ``batch_alter_table`` is needed on the upgrade path.
    """
    op.add_column("conversations", sa.Column("agent_id", Uuid16(), nullable=True))
    op.add_column("conversations", sa.Column("session_overrides", sa.String(512), nullable=True))

    _backfill_binding_and_overrides()

    op.create_index(
        "ix_conversations_agent_id",
        "conversations",
        ["workspace_id", "agent_id", "id"],
    )
    op.drop_index("ix_agent_configuration_agent_id", table_name="agent_configuration")
    op.drop_table("agent_configuration")


def downgrade() -> None:
    """Recreate ``agent_configuration``, copy the binding + overrides back, and
    drop the merged columns from ``conversations``.

    The recreated table uses ``Uuid16`` (binary) id columns — matching
    ``conversations`` — so the ``INSERT … SELECT`` binding copy is a clean
    binary-to-binary move on every dialect (the original split declared these
    ``VARCHAR``; binary is the consistent, coercion-free choice here).
    """
    op.create_table(
        "agent_configuration",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=True),
        sa.Column("reasoning_effort", sa.String(32), nullable=True),
        sa.Column("model_override", sa.String(128), nullable=True),
        sa.Column("cost_control_mode_override", sa.String(8), nullable=True),
        sa.Column("harness_override", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id"),
    )
    op.create_index(
        "ix_agent_configuration_agent_id",
        "agent_configuration",
        ["workspace_id", "agent_id", "conversation_id"],
    )

    # One agent_configuration row per conversation, agent_id carried directly
    # (binary→binary, so no coercion needed).
    op.execute(
        """
        INSERT INTO agent_configuration (workspace_id, conversation_id, agent_id)
        SELECT workspace_id, id, agent_id FROM conversations
        """
    )
    _restore_agent_configuration_overrides()

    op.drop_index("ix_conversations_agent_id", table_name="conversations")
    # DROP COLUMN needs batch_alter_table on older SQLite.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("session_overrides")
        batch_op.drop_column("agent_id")
