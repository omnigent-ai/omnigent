"""The write-grant index is additive and agrees with model-created schemas."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlSessionPermission
from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_REVISION = "e4a7b9c2d6f1"
_PREVIOUS = "d0a1f879a01d"
_INDEX = "ix_session_permissions_write_access"
_TABLE = SqlSessionPermission.__table__
_COLUMNS = ["workspace_id", "conversation_id", "level", "user_id"]


def test_write_access_index_matches_model_at_head(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    indexes = {index["name"]: index for index in sa.inspect(engine).get_indexes(_TABLE.name)}
    assert indexes[_INDEX]["column_names"] == _COLUMNS
    assert not indexes[_INDEX]["unique"]
    assert indexes["ix_session_permissions_conversation_id"]["column_names"] == [
        "workspace_id",
        "conversation_id",
        "user_id",
    ]
    model_index = next(index for index in _TABLE.indexes if index.name == _INDEX)
    assert list(model_index.columns.keys()) == _COLUMNS
    assert not model_index.unique


def test_write_access_index_round_trip_preserves_rows_and_indexes(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    config = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    rows = [
        {"workspace_id": workspace, "conversation_id": "01" * 16, "user_id": user, "level": level}
        for workspace in (0, 42)
        for user, level in (("__public__", 1), ("reader", 1), ("editor", 2), ("owner", 4))
    ]
    query = sa.select(_TABLE).order_by(_TABLE.c.workspace_id, _TABLE.c.user_id)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, _PREVIOUS)
            connection.execute(_TABLE.insert(), rows)
            original_rows = connection.execute(query).all()
            original_indexes = sa.inspect(connection).get_indexes(_TABLE.name)
            assert _INDEX not in {index["name"] for index in original_indexes}

        # Exercise the production startup upgrade from the previous head.
        migrated = get_or_create_engine(uri)
        with migrated.connect() as connection:
            assert connection.execute(query).all() == original_rows
            indexes = sa.inspect(connection).get_indexes(_TABLE.name)
            assert (
                next(index for index in indexes if index["name"] == _INDEX)["column_names"]
                == _COLUMNS
            )
            assert [index for index in indexes if index["name"] != _INDEX] == original_indexes

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, _PREVIOUS)
            assert sa.inspect(connection).get_indexes(_TABLE.name) == original_indexes
            assert connection.execute(query).all() == original_rows
            command.upgrade(config, _REVISION)
            assert sa.inspect(connection).get_indexes(_TABLE.name) == indexes
            assert connection.execute(query).all() == original_rows
    finally:
        engine.dispose()
        clear_engine_cache()
