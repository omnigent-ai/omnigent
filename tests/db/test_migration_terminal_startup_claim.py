"""Upgrade already-owned session rows without granting a legacy startup claim."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def test_terminal_startup_claim_upgrade_preserves_existing_ownership(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'terminal-startup.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    table = "omnigent_conversation_metadata"
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "d42f66e0a5b1")
            connection.execute(
                sa.text(
                    f"INSERT INTO {table} "
                    "(workspace_id,id,kind,runner_id,live_status,runner_binding_epoch,"
                    "live_status_revision,live_status_observation_id,terminal_pending) "
                    "VALUES (:workspace_id,:id,1,'runner-a',2,7,11,'observed-a',1)"
                ),
                [
                    {"workspace_id": workspace, "id": bytes.fromhex("a" * 32)}
                    for workspace in (0, 9)
                ],
            )
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "e53a77f1b6c2")
            columns = {
                column["name"]: column for column in sa.inspect(connection).get_columns(table)
            }
            assert isinstance(columns["terminal_startup_claim"]["type"], sa.LargeBinary)
            assert columns["terminal_startup_claim"]["nullable"]
            assert connection.execute(
                sa.text(
                    f"SELECT workspace_id,runner_id,runner_binding_epoch,live_status_revision,"
                    "live_status_observation_id,terminal_pending,terminal_startup_claim "
                    f"FROM {table} ORDER BY workspace_id"
                )
            ).all() == [
                (0, "runner-a", 7, 11, "observed-a", 1, None),
                (9, "runner-a", 7, 11, "observed-a", 1, None),
            ]
            connection.execute(
                sa.text(f"UPDATE {table} SET terminal_startup_claim=:claim"),
                {"claim": b"\x00\x00{}"},
            )
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "d42f66e0a5b1")
            assert "terminal_startup_claim" not in {
                column["name"] for column in sa.inspect(connection).get_columns(table)
            }
            assert connection.execute(
                sa.text(
                    f"SELECT runner_binding_epoch,live_status_revision,"
                    f"live_status_observation_id,terminal_pending FROM {table}"
                )
            ).all() == [(7, 11, "observed-a", 1), (7, 11, "observed-a", 1)]
            command.upgrade(config, "e53a77f1b6c2")
            assert connection.execute(
                sa.text(f"SELECT terminal_startup_claim FROM {table}")
            ).all() == [(None,), (None,)]
    finally:
        engine.dispose()
