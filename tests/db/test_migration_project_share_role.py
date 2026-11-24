"""Migration ``b4d8f2a6c0e3`` adds a role to project shares.

Shares written before it, or by an older binary that never sets the column,
read as ordinary members. The downgrade drops the column and keeps every share.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_REVISION = "b4d8f2a6c0e3"
_PREVIOUS = "e5c2a9f7d3b1"

_INSERT = sa.text(
    "INSERT INTO project_shares (workspace_id, project_id, user_id, created_at, created_by)"
    " VALUES (0, :pid, :uid, 1, 'alice@example.com')"
)


def _migrate(
    cfg: Config, engine: sa.Engine, step: Callable[[Config, str], None], revision: str
) -> None:
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        step(cfg, revision)


def test_old_rows_and_old_writers_default_to_member(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roles.db'}"
    cfg = _build_alembic_config(uri)
    get_or_create_engine(uri)
    clear_engine_cache()
    engine = sa.create_engine(uri)
    try:
        _migrate(cfg, engine, command.downgrade, _PREVIOUS)
        with engine.begin() as conn:
            conn.execute(_INSERT, {"pid": b"\x01" * 16, "uid": "bob@example.com"})
        _migrate(cfg, engine, command.upgrade, _REVISION)
        with engine.begin() as conn:
            # An older binary's insert omits the column.
            conn.execute(_INSERT, {"pid": b"\x01" * 16, "uid": "carol@example.com"})
            roles = conn.execute(
                sa.text("SELECT user_id, role FROM project_shares ORDER BY user_id")
            ).all()
        assert [tuple(r) for r in roles] == [
            ("bob@example.com", "member"),
            ("carol@example.com", "member"),
        ]

        _migrate(cfg, engine, command.downgrade, _PREVIOUS)
        with engine.connect() as conn:
            assert "role" not in {
                c["name"] for c in sa.inspect(conn).get_columns("project_shares")
            }
            assert conn.execute(sa.text("SELECT COUNT(*) FROM project_shares")).scalar() == 2
    finally:
        engine.dispose()
        clear_engine_cache()
