"""Backfill trusted routing metadata for the closed personal projects.

Revision ID: gb1c2d3e4f5a
Revises: ga1b2c3d4e5f
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
import zstandard
from alembic import op

revision: str = "gb1c2d3e4f5a"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERSONAL_PROJECTS = frozenset({"chatgpt-playground", "planar-jacobian", "planar-jc-codex"})


def _decode(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, memoryview):
        data = value.tobytes()
    elif isinstance(value, bytes | bytearray):
        data = bytes(value)
    else:
        raise TypeError(f"unsupported projects.config value: {type(value).__name__}")
    if not data or data[0] != 0:
        return data.decode("utf-8")
    codec, payload = data[1], data[2:]
    if codec == 1:
        return zstandard.ZstdDecompressor().decompress(payload).decode("utf-8")
    return payload.decode("utf-8")


def _encode(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) < 64:
        return b"\x00\x00" + raw
    return b"\x00\x01" + zstandard.ZstdCompressor(level=19).compress(raw)


def upgrade() -> None:
    """Stamp known personal projects without trusting session labels."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT workspace_id, id, name, config FROM projects WHERE name IN (:p1, :p2, :p3)"
        ),
        {"p1": "chatgpt-playground", "p2": "planar-jacobian", "p3": "planar-jc-codex"},
    ).fetchall()
    for workspace_id, project_id, name, raw_config in rows:
        if name not in _PERSONAL_PROJECTS:
            continue
        config = json.loads(_decode(raw_config)) if raw_config is not None else {}
        if not isinstance(config, dict):
            config = {}
        existing_route = config.get("quota_route")
        if existing_route not in (None, "personal-llmq"):
            raise RuntimeError(f"personal project {name!r} has conflicting routing metadata")
        config["quota_route"] = "personal-llmq"
        encoded = _encode(json.dumps(config, separators=(",", ":"), sort_keys=True))
        bind.execute(
            sa.text("UPDATE projects SET config = :config WHERE workspace_id = :ws AND id = :id"),
            {"config": encoded, "ws": workspace_id, "id": project_id},
        )


def downgrade() -> None:
    """Retain routing metadata; removing an authorization marker is unsafe."""
