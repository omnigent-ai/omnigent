"""Recovery schema upgrades independently and preserves existing session data."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlConversation, SqlSessionRecovery
from omnigent.db.utils import _build_alembic_config


def test_session_recovery_migration_round_trip(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'recovery.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "nn1a2b3c4d5e")
            connection.execute(
                sa.insert(SqlConversation).values(
                    workspace_id=42,
                    id="a" * 32,
                    root_conversation_id="a" * 32,
                    created_at=100,
                    updated_at=100,
                ),
            )
            command.upgrade(cfg, "7c91e4a2b6d8")
            inspector = sa.inspect(connection)
            assert inspector.get_pk_constraint("session_recovery")["constrained_columns"] == [
                "workspace_id",
                "session_id",
            ]
            assert inspector.get_foreign_keys("session_recovery") == []
            columns = inspector.get_columns("session_recovery")
            assert {column["name"] for column in columns if column["nullable"]} == {
                "lease_until",
                "claim_id",
                "outcome_json",
                "terminal_reason",
            }
            assert all(column["default"] is None for column in columns)
            assert inspector.get_indexes("session_recovery")[0]["column_names"] == [
                "workspace_id",
                "host_id",
                "lease_until",
                "session_id",
            ]
            connection.execute(
                sa.insert(SqlSessionRecovery).values(
                    workspace_id=42,
                    session_id="a" * 32,
                    host_id="b" * 32,
                    episode_key="c" * 64,
                    attempts=1,
                    next_attempt_at=2**31 + 30,
                    lease_until=2**31 + 600,
                    claim_id="d" * 32,
                    outcome_json='{"status":"restarted"}',
                    terminal_reason=None,
                )
            )
            assert connection.execute(
                sa.text(
                    "SELECT length(session_id), length(host_id), length(claim_id) "
                    "FROM session_recovery"
                )
            ).one() == (16, 16, 16)
            assert (
                connection.scalar(sa.select(SqlSessionRecovery.outcome_json))
                == '{"status":"restarted"}'
            )
            command.downgrade(cfg, "nn1a2b3c4d5e")
            assert "session_recovery" not in sa.inspect(connection).get_table_names()
            assert connection.scalar(sa.text("SELECT count(*) FROM conversations")) == 1
            command.upgrade(cfg, "head")
            assert "session_recovery" in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
