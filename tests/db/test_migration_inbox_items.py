"""Tests for the inbox_items migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_REVISION = "fcc79fe2b985"
_PREVIOUS = "f1c7a9e2b406"


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
    indexes = {
        index["name"]: (index["column_names"], bool(index["unique"]))
        for index in inspector.get_indexes("inbox_items")
    }
    assert indexes == {
        "ix_inbox_items_user_created": (["workspace_id", "user_id", "created_at", "id"], False),
        "ix_inbox_items_user_state_created": (
            ["workspace_id", "user_id", "state", "created_at", "id"],
            False,
        ),
        "uq_inbox_items_user_dedupe_key": (["workspace_id", "user_id", "dedupe_key"], True),
        "uq_inbox_items_session_slot": (
            ["workspace_id", "user_id", "source_session_id", "session_slot"],
            True,
        ),
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
        "session_slot": 0,
        "agent_id": bytes(range(1, 17)),
        "kind": 1,
        "title": "t",
        "body_md": b"b",
        "links": b"[]",
        "actions": b"[]",
        "topic": "x",
        "dedupe_key": "v1:x",
        "state": 1,
        "resolution": 0,
        "created_at": 1,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "t" * 16_384},
        {"created_at": "x" * 16_384},
        {"session_slot": 0.5},
        {"session_slot": 10},
        {"body_md": b"b" * 16_384},
        {"source_session_id": b"short"},
    ],
    ids=[
        "long-title",
        "text-timestamp",
        "fractional-slot",
        "slot-past-cap",
        "large-body",
        "bad-uuid",
    ],
)
def test_sqlite_rejects_values_outside_the_column_bounds(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    uri = f"sqlite:///{tmp_path / 'inbox.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, _REVISION)
    insert = sa.text(
        "INSERT INTO inbox_items (workspace_id, id, user_id, source_session_id, session_slot, "
        "agent_id, kind, title, body_md, links, actions, topic, dedupe_key, state, resolution, "
        "created_at) VALUES (:workspace_id, :id, :user_id, :source_session_id, :session_slot, "
        ":agent_id, :kind, :title, :body_md, :links, :actions, :topic, :dedupe_key, :state, "
        ":resolution, :created_at)"
    )
    with engine.begin() as conn:
        conn.execute(insert, _row())
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            insert,
            _row(**{"id": bytes([1] * 16), "dedupe_key": "v1:y", "session_slot": 1, **overrides}),
        )
    engine.dispose()
    clear_engine_cache()
