"""Exercise preference migration transactions on SQLite and the configured SQL backend."""

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.compression import encode
from omnigent.db.utils import (
    _build_alembic_config,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
    get_or_create_engine,
)


def _downgrade_to_user_preferences(engine: sa.Engine, db_uri: str) -> None:
    config = _build_alembic_config(db_uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "ii1a2b3c4d5e")


@pytest.mark.parametrize("failed_attempts", [0, 2, 3])
def test_preferences_migration_round_trip_and_copy_failures(
    db_uri: str, failed_attempts: int, capfd: pytest.CaptureFixture[str]
) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name == "cockroachdb":
        pytest.skip("CockroachDB transaction restarts are covered in test_cockroachdb.py")

    # Reflection keeps these values raw: ORM compression must not transform migration data.
    preferences = sa.Table("preferences", sa.MetaData(), autoload_with=engine)
    _downgrade_to_user_preferences(engine, db_uri)
    users = sa.Table("users", sa.MetaData(), autoload_with=engine)
    original = {
        (0, "alice"): encode('{"sort_mode":"manual","ordered_project_ids":["' + "a" * 32 + '"]}'),
        (17, "alice"): b"\x00\x01invalid compression frame",
        (0, "legacy"): b'["' + b"b" * 32 + b'"]',
        (0, "large"): bytes(range(256)) * 300,
        (0, "unset"): None,
        (0, "existing"): b"older source value",
    }
    with engine.begin() as connection:
        connection.execute(users.delete())
        connection.execute(
            users.insert(),
            [
                {"workspace_id": workspace_id, "id": user_id, "project_order": value}
                for (workspace_id, user_id), value in original.items()
            ],
        )

    expected = {
        (workspace_id, user_id, "project_order"): value
        for (workspace_id, user_id), value in original.items()
        if value is not None and failed_attempts < 3
    }
    if failed_attempts == 0:
        preferences.create(engine)
        preexisting = {
            (0, "existing", "project_order"): b"newer destination value",
            (0, "existing", "theme"): b"dark",
        }
        with engine.begin() as connection:
            connection.execute(
                preferences.insert(),
                [
                    {"workspace_id": workspace_id, "user_id": user_id, "key": key, "value": value}
                    for (workspace_id, user_id, key), value in preexisting.items()
                ],
            )
        expected.update(preexisting)

    attempts = 0

    def fail_copy(conn, cursor, statement, parameters, context, executemany):
        nonlocal attempts
        if statement.startswith("INSERT INTO preferences"):
            attempts += 1
            if attempts <= failed_attempts:
                # A real server error aborts PostgreSQL's transaction until savepoint rollback.
                return "INSERT INTO missing_migration_table SELECT 1", ()
        return statement, parameters

    sa.event.listen(engine, "before_cursor_execute", fail_copy, retval=True)
    try:
        _initialize_or_verify_schema(engine, db_uri)
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail_copy)

    assert attempts == (1 if failed_attempts == 0 else 3)
    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)
    assert "project_order" not in {
        column["name"] for column in sa.inspect(engine).get_columns("users")
    }
    warning = "Could not migrate project order preferences after 3 attempts"
    assert (warning in capfd.readouterr().err) == (failed_attempts == 3)
    _initialize_or_verify_schema(engine, db_uri)
    with engine.connect() as connection:
        saved = {
            (row.workspace_id, row.user_id, row.key): row.value
            for row in connection.execute(preferences.select())
        }
    assert saved == expected

    _downgrade_to_user_preferences(engine, db_uri)
    try:
        with engine.connect() as connection:
            restored = {
                (row.workspace_id, row.id): row.project_order
                for row in connection.execute(
                    sa.select(users.c.workspace_id, users.c.id, users.c.project_order)
                )
            }
        assert restored == {
            (workspace_id, user_id): expected.get((workspace_id, user_id, "project_order"))
            for workspace_id, user_id in original
        }
        assert not sa.inspect(engine).has_table("preferences")
    finally:
        _initialize_or_verify_schema(engine, db_uri)
