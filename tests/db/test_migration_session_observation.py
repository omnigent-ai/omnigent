"""Upgrade legacy ownership rows and preserve them across a downgrade."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def test_session_observation_migration_preserves_default_zero_and_existing_state(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'session-observations.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    table = "omnigent_conversation_metadata"
    new_columns = {
        "runner_binding_epoch",
        "live_status_revision",
        "live_status_observation_id",
        "live_status_response_id",
        "live_status_error_key",
        "live_status_error_labels",
        "terminal_pending",
        "runner_offline_decision",
    }
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "mm1a2b3c4d5e")
            connection.execute(
                sa.text(
                    f"INSERT INTO {table} (workspace_id,id,kind,runner_id,live_status) "
                    "VALUES (:workspace_id,:id,1,'runner-a',2)"
                ),
                [
                    {"workspace_id": workspace_id, "id": bytes.fromhex("a" * 32)}
                    for workspace_id in (0, 9)
                ],
            )
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "d42f66e0a5b1")
            types = {
                column["name"]: column["type"]
                for column in sa.inspect(connection).get_columns(table)
            }
            assert all(
                isinstance(types[name], sa.LargeBinary)
                for name in (
                    "live_status_response_id",
                    "live_status_error_labels",
                    "runner_offline_decision",
                )
            )
            rows = connection.execute(
                sa.text(
                    f"SELECT workspace_id,runner_id,live_status,runner_binding_epoch,"
                    "live_status_revision,live_status_observation_id,runner_offline_decision,"
                    "terminal_pending "
                    f"FROM {table} ORDER BY workspace_id"
                )
            ).all()
            assert rows == [
                (0, "runner-a", 2, 0, 0, None, None, 0),
                (9, "runner-a", 2, 0, 0, None, None, 0),
            ]
            connection.execute(
                sa.text(
                    f"UPDATE {table} SET runner_binding_epoch=7,live_status_revision=11,"
                    "live_status_observation_id='observed',runner_offline_decision=:decision,"
                    "terminal_pending=1"
                ),
                {"decision": b"\x00\x00{}"},
            )
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "mm1a2b3c4d5e")
        assert not new_columns.intersection(
            column["name"] for column in sa.inspect(engine).get_columns(table)
        )
        with engine.begin() as connection:
            assert connection.execute(
                sa.text(
                    f"SELECT workspace_id,runner_id,live_status FROM {table} ORDER BY workspace_id"
                )
            ).all() == [(0, "runner-a", 2), (9, "runner-a", 2)]
            config.attributes["connection"] = connection
            command.upgrade(config, "d42f66e0a5b1")
            assert connection.execute(
                sa.text(
                    "SELECT runner_binding_epoch,live_status_revision,terminal_pending "
                    f"FROM {table}"
                )
            ).all() == [(0, 0, 0), (0, 0, 0)]
    finally:
        engine.dispose()
