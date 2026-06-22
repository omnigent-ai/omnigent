"""Tests for the ``comments.updated_at`` migration (``ecc0e25727b0``).

The column feeds the per-session comments change fingerprint surfaced on
``GET /v1/sessions`` / ``WS /v1/sessions/updates``. Existing rows must
backfill to ``created_at`` scaled to microseconds (a never-edited
comment's last mutation time is its creation time; ``updated_at`` is
stored in epoch-µs) and the column must end up NOT NULL to match the
ORM model.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

# Revision ids bounding the migration under test.
_PRIOR_HEAD = "j1a2b3c4d5e6"
_THIS_REVISION = "ecc0e25727b0"


def _new_engine(uri: str) -> sa.Engine:
    """
    Create a raw migration-test engine without auto-upgrading to head.

    :param uri: SQLAlchemy database URI, e.g.
        ``"sqlite:///tmp/test.db"``.
    :returns: SQLAlchemy engine with SQLite foreign keys enabled.
    """
    engine = sa.create_engine(uri)
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
    return engine


def _upgrade(engine: sa.Engine, uri: str, revision: str) -> None:
    """
    Run Alembic upgrade to a target revision on a raw engine.

    :param engine: SQLAlchemy engine under migration.
    :param uri: SQLAlchemy database URI, e.g.
        ``"sqlite:///tmp/test.db"``.
    :param revision: Alembic target revision, e.g. ``"ecc0e25727b0"``.
    :returns: None.
    """
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def test_comments_updated_at_backfilled_from_created_at(tmp_path: Path) -> None:
    """Upgrade backfills pre-existing rows with their ``created_at``.

    Rows are inserted at the prior head (no ``updated_at`` column yet),
    then the migration applies. A wrong backfill (e.g. 0 or NULL) would
    make every legacy comment's session fingerprint either constant or
    crash the NOT NULL ORM read.
    """
    uri = f"sqlite:///{tmp_path / 'comments-updated-at.db'}"
    engine = _new_engine(uri)
    try:
        _upgrade(engine, uri, _PRIOR_HEAD)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO comments "
                    "(id, conversation_id, path, start_index, end_index, "
                    " body, status, created_at) "
                    "VALUES (:id, :conv, :path, 0, 5, :body, 'draft', :ts)",
                ),
                [
                    {"id": "cmt_1", "conv": "conv_a", "path": "a.py", "body": "x", "ts": 1_000},
                    {"id": "cmt_2", "conv": "conv_a", "path": "a.py", "body": "y", "ts": 2_000},
                ],
            )

        _upgrade(engine, uri, _THIS_REVISION)

        with engine.connect() as conn:
            rows = {
                str(row["id"]): (row["created_at"], row["updated_at"])
                for row in conn.execute(
                    sa.text("SELECT id, created_at, updated_at FROM comments"),
                ).mappings()
            }
        # Each legacy row's updated_at must equal its own created_at
        # scaled to microseconds — a shared constant here would mean the
        # backfill ignored per-row values and legacy comments would all
        # fingerprint identically.
        us = 1_000_000
        assert rows == {"cmt_1": (1_000, 1_000 * us), "cmt_2": (2_000, 2_000 * us)}
    finally:
        engine.dispose()


def test_comments_updated_at_is_not_nullable_after_upgrade(tmp_path: Path) -> None:
    """The column lands NOT NULL, matching the ORM model.

    A nullable column would let future writers skip the field and feed
    NULL into ``max(updated_at)`` aggregates, silently breaking the
    fingerprint ordering.
    """
    uri = f"sqlite:///{tmp_path / 'comments-not-null.db'}"
    engine = _new_engine(uri)
    try:
        _upgrade(engine, uri, _THIS_REVISION)
        cols = {c["name"]: c for c in sa.inspect(engine).get_columns("comments")}
        assert "updated_at" in cols, "comments.updated_at missing — the migration did not apply."
        assert not cols["updated_at"]["nullable"], (
            "comments.updated_at must be NOT NULL after the tighten step."
        )
    finally:
        engine.dispose()
