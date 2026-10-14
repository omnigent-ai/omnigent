"""Migration ``nn1a2b3c4d5e`` adds nullable credential epochs to accounts tables.

Existing users and credentials must upgrade with a ``NULL`` epoch (never
rotated) and otherwise unchanged, an interrupted upgrade must resume, and the
downgrade must drop the columns while keeping the rows.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_TABLES = ("users", "account_tokens", "device_grants")
_ROW_IDS = {"users": "alice", "account_tokens": "magic", "device_grants": "grant"}
_PREVIOUS = "mm1a2b3c4d5e"


def _columns(connection: sa.Connection, table: str) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(connection).get_columns(table)}


def test_upgrade_keeps_existing_credentials_and_downgrade_drops_columns(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'credential-epoch.db'}"
    config = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, _PREVIOUS)
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, is_admin, password_hash, account_generation) "
                    "VALUES ('alice', 0, 'hash', 'generation')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO account_tokens (id, kind, user_id, created_at, expires_at, "
                    "invited_is_admin, account_generation) VALUES ('magic', 2, 'alice', 1, 2, "
                    "0, 'generation')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO device_grants (id, device_code_hash, user_code, status, "
                    "user_id, created_at, expires_at, refresh_token_hash, account_generation) "
                    "VALUES ('grant', 'device', 'code', 4, 'alice', 1, 2, 'refresh', "
                    "'generation')"
                )
            )
            command.upgrade(config, "head")
            for table in _TABLES:
                column = _columns(connection, table)["credential_epoch"]
                assert column["nullable"] is True
                assert column["type"].length == 32
                row = connection.execute(
                    sa.text(
                        f"SELECT account_generation, credential_epoch FROM {table} WHERE id = :id"
                    ),
                    {"id": _ROW_IDS[table]},
                ).one()
                assert row == ("generation", None)

            command.downgrade(config, _PREVIOUS)
            for table in _TABLES:
                assert "credential_epoch" not in _columns(connection, table)
                kept = connection.execute(
                    sa.text(f"SELECT account_generation FROM {table} WHERE id = :id"),
                    {"id": _ROW_IDS[table]},
                ).scalar_one()
                assert kept == "generation"
    finally:
        engine.dispose()
        clear_engine_cache()


def test_upgrade_resumes_after_a_partially_applied_revision(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'credential-epoch-resume.db'}"
    config = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, _PREVIOUS)
            # Engines that commit DDL per statement can stop after the first column.
            connection.execute(
                sa.text("ALTER TABLE users ADD COLUMN credential_epoch VARCHAR(32)")
            )
            command.upgrade(config, "head")
            for table in _TABLES:
                assert "credential_epoch" in _columns(connection, table)
    finally:
        engine.dispose()
        clear_engine_cache()
