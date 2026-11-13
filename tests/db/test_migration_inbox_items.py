"""Tests for the inbox_items migration."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_REVISION = "fcc79fe2b985"
_PREVIOUS = "a8be64286ffc"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


def test_upgrade_creates_table_and_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'inbox.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, _REVISION)
    inspector = sa.inspect(engine)
    assert "inbox_items" in inspector.get_table_names()
    assert inspector.get_pk_constraint("inbox_items")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]
    assert inspector.get_foreign_keys("inbox_items") == []
    assert [column["name"] for column in inspector.get_columns("inbox_items")] == [
        "workspace_id",
        "id",
        "user_id",
        "source_session_id",
        "agent_id",
        "dedupe_key",
        "status",
        "content",
        "resolution",
        "created_at",
        "resolved_at",
        "expires_at",
    ]
    indexes = {
        index["name"]: (index["column_names"], bool(index["unique"]))
        for index in inspector.get_indexes("inbox_items")
    }
    assert indexes == {
        "ix_inbox_items_user_created": (["workspace_id", "user_id", "created_at", "id"], False),
        "ix_inbox_items_user_status_created": (
            ["workspace_id", "user_id", "status", "created_at", "id"],
            False,
        ),
        "ix_inbox_items_session": (
            ["workspace_id", "user_id", "source_session_id", "id"],
            False,
        ),
        "uq_inbox_items_user_dedupe_key": (["workspace_id", "user_id", "dedupe_key"], True),
    }
    # The application supplies every value on this new table.
    assert all(column["default"] is None for column in inspector.get_columns("inbox_items"))

    _migrate(uri, engine, _PREVIOUS, down=True)
    assert "inbox_items" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "workspace_id": 0,
        "id": bytes(16),
        "user_id": "alice",
        "source_session_id": bytes(range(16)),
        "agent_id": bytes(range(1, 17)),
        "dedupe_key": "v1:x",
        "status": 1,
        "content": b"\x01\x00{}",
        "resolution": None,
        "created_at": 1,
    }
    row.update(overrides)
    return row


_INSERT = sa.text(
    "INSERT INTO inbox_items (workspace_id, id, user_id, source_session_id, agent_id, "
    "dedupe_key, status, content, resolution, created_at) VALUES "
    "(:workspace_id, :id, :user_id, :source_session_id, :agent_id, :dedupe_key, :status, "
    ":content, :resolution, :created_at)"
)


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    uri = f"sqlite:///{tmp_path / 'inbox.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, _REVISION)
    with engine.begin() as conn:
        conn.execute(_INSERT, _row())
    yield engine
    engine.dispose()
    clear_engine_cache()


def _second(**overrides: object) -> dict[str, object]:
    """A row distinct from the fixture's in id and duplicate key."""
    return _row(**{"id": bytes([1] * 16), "dedupe_key": "v1:y", **overrides})


@pytest.mark.parametrize(
    "overrides",
    [
        {"dedupe_key": "é" * 256 + "k"},
        {"created_at": "x" * 16_384},
        {"status": 5},
        {"content": b"c" * 16_384},
        {"resolution": b"r" * 16_384},
        {"source_session_id": b"short"},
    ],
    ids=[
        "multibyte-dedupe-key-one-byte-over",
        "text-timestamp",
        "unknown-status",
        "content-one-byte-over",
        "resolution-one-byte-over",
        "bad-uuid",
    ],
)
def test_sqlite_rejects_values_outside_the_column_bounds(
    engine: sa.Engine, overrides: dict[str, object]
) -> None:
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT, _second(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"dedupe_key": "é" * 256},
        {"content": b"c" * 16_383},
        {"resolution": b"r" * 16_383},
        {"status": 4},
    ],
    ids=[
        "multibyte-dedupe-key-at-limit",
        "content-at-limit",
        "resolution-at-limit",
        "last-status",
    ],
)
def test_sqlite_accepts_values_at_the_column_bounds(
    engine: sa.Engine, overrides: dict[str, object]
) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT, _second(**overrides))


@pytest.mark.parametrize(
    ("overrides", "allowed"),
    [
        ({"dedupe_key": "v1:x"}, False),
        ({"dedupe_key": "v1:x", "user_id": "bob"}, True),
    ],
    ids=["same-owner-duplicate-key", "other-owner-same-key"],
)
def test_unique_index_scopes_keys_to_their_owner(
    engine: sa.Engine, overrides: dict[str, object], allowed: bool
) -> None:
    if allowed:
        with engine.begin() as conn:
            conn.execute(_INSERT, _second(**overrides))
    else:
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(_INSERT, _second(**overrides))
