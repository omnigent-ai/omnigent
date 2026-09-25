"""Upgrade binds existing durable authority to an account generation."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_account_revocation_migration_preserves_identity_and_grants(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'account-migration.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "hh1b2c3d4e5f")
        connection.execute(
            sa.text(
                "INSERT INTO users (id, is_admin, password_hash) VALUES ('alice', 1, "
                "'existing-hash'), ('external', 0, NULL)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO device_grants (id, device_code_hash, user_code, status, "
                "user_id, created_at, expires_at, refresh_token_hash) VALUES ('grant', "
                "'device', 'code', 4, 'alice', 1, 2, 'refresh')"
            )
        )
        command.upgrade(config, "head")
        account = connection.execute(
            sa.text(
                "SELECT account_generation, password_hash, is_admin, deleted_at FROM "
                "users WHERE id = 'alice'"
            )
        ).one()
        assert len(account.account_generation) == 32
        assert (account.password_hash, account.is_admin, account.deleted_at) == (
            "existing-hash",
            1,
            None,
        )
        grant = connection.execute(
            sa.text(
                "SELECT account_generation, refresh_token_hash FROM device_grants WHERE "
                "id = 'grant'"
            )
        ).one()
        assert grant == (account.account_generation, "refresh")
        assert (
            connection.execute(
                sa.text("SELECT account_generation FROM users WHERE id = 'external'")
            ).scalar_one()
            is None
        )
        connection.execute(
            sa.text(
                "UPDATE users SET deleted_at = 1, password_hash = NULL, is_admin = 0 "
                "WHERE id = 'alice'"
            )
        )
        command.downgrade(config, "hh1b2c3d4e5f")
        assert (
            connection.execute(
                sa.text("SELECT COUNT(*) FROM users WHERE id = 'alice'")
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("column", ["account_generation", "deleted_at"])
def test_mysql_account_migration_resumes_after_committed_column(db_uri, column):
    from omnigent.db.utils import _get_current_db_revision, _initialize_or_verify_schema
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.device_grant_store import DeviceGrantStore

    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "mysql":
        pytest.skip("requires OMNIGENT_TEST_DB_URI pointing to MySQL")
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("migration-user", "original-password-hash")
    grants = DeviceGrantStore(db_uri)
    grants.create_redeemed_grant(
        "migration-grant",
        user_id="migration-user",
        client_id="cli",
        refresh_token_hash="original-refresh-hash",
        created_at=100,
    )
    config = _build_alembic_config(db_uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "hh1b2c3d4e5f")

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"ALTER TABLE users ADD COLUMN {column}"):
            raise RuntimeError("injected interruption after committed column")

    sa.event.listen(engine, "after_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="automatic migration failed") as exc:
            _initialize_or_verify_schema(engine, db_uri)
        assert isinstance(exc.value.__cause__, RuntimeError)
        assert str(exc.value.__cause__) == "injected interruption after committed column"
    finally:
        sa.event.remove(engine, "after_cursor_execute", interrupt)
    assert _get_current_db_revision(engine) == "hh1b2c3d4e5f"
    assert column in {c["name"] for c in sa.inspect(engine).get_columns("users")}
    _initialize_or_verify_schema(engine, db_uri)
    account = accounts.get_user("migration-user")
    assert account is not None and len(account.account_generation) == 32
    assert accounts.get_password_hash("migration-user") == "original-password-hash"
    grant = grants.authorize_access("migration-grant")
    assert grant is not None and grant.account_generation == account.account_generation
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text(
                    "SELECT refresh_token_hash FROM device_grants WHERE id = 'migration-grant'"
                )
            ).scalar_one()
            == "original-refresh-hash"
        )
    from alembic.script import ScriptDirectory

    assert (
        _get_current_db_revision(engine) == ScriptDirectory.from_config(config).get_current_head()
    )
