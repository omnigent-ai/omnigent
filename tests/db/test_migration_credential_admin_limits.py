"""Credential ceilings preserve legacy rows and old-writer defaults."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_credential_admin_limits_migration(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'credential-limits.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "f1c7a9e2b406")
        connection.execute(
            sa.text(
                "INSERT INTO device_grants (id, device_code_hash, user_code, status, "
                "created_at, expires_at) VALUES ('existing', 'hash', 'code', 4, 1, 2)"
            )
        )
        command.upgrade(config, "a83c29e4f617")
        assert (
            connection.execute(
                sa.text("SELECT admin_allowed FROM device_grants WHERE id = 'existing'")
            ).scalar_one()
            == 1
        )
        connection.execute(
            sa.text(
                "INSERT INTO device_grants (id, device_code_hash, user_code, status, "
                "created_at, expires_at) VALUES ('old-writer', 'hash2', 'code2', 4, 1, 2)"
            )
        )
        assert (
            connection.execute(
                sa.text("SELECT admin_allowed FROM device_grants WHERE id = 'old-writer'")
            ).scalar_one()
            == 1
        )
        for table, name in (
            ("device_grants", "admin_allowed"),
            ("omnigent_conversation_metadata", "runner_admin_allowed"),
        ):
            column = next(
                c for c in sa.inspect(connection).get_columns(table) if c["name"] == name
            )
            assert not column["nullable"]
        command.downgrade(config, "f1c7a9e2b406")
        assert connection.execute(sa.text("SELECT count(*) FROM device_grants")).scalar_one() == 2
