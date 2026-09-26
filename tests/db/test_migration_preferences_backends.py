"""Exercise preference migration transactions on SQLite and the configured SQL backend."""

import tempfile
from pathlib import Path

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


def _reach_revision(engine: sa.Engine, db_uri: str, target_revision: str) -> None:
    """Reach a specific revision by upgrading from an empty database state.

    Drops all application tables and clears the alembic_version table,
    then upgrades to the target revision. Avoids issues with downgrading
    through merge points which can leave multiple heads in alembic_version.
    """
    from alembic.script import ScriptDirectory

    # Drop all non-alembic tables to clean the schema
    with engine.begin() as connection:
        inspector = sa.inspect(connection)
        for table_name in inspector.get_table_names():
            if table_name != "alembic_version":
                # Use appropriate quoting for different dialects
                if engine.dialect.name == "sqlite":
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS [{table_name}]"))
                else:
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS {table_name}"))
        # Clear alembic_version to reset migration state to empty
        if "alembic_version" in inspector.get_table_names():
            connection.execute(sa.text("DELETE FROM alembic_version"))

    # Upgrade to the target revision from the now-empty state.
    # Get the head revisions to verify target is reachable.
    config = _build_alembic_config(db_uri)
    script = ScriptDirectory.from_config(config)

    # Verify the target revision exists
    try:
        script.get_revision(target_revision)
    except Exception:
        raise ValueError(f"Target revision '{target_revision}' not found in migration history")

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, target_revision)


def _get_schema_at_revision(db_uri: str, target_revision: str, table_name: str) -> sa.Table:
    """Reflect a table schema at a specific revision using a temporary database."""
    from sqlalchemy.engine import make_url

    url = make_url(db_uri)

    if url.drivername == "sqlite":
        # For SQLite, create a temp database
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db_path = Path(tmpdir) / "temp.db"
            temp_uri = f"sqlite:///{temp_db_path}"
            temp_engine = get_or_create_engine(temp_uri)
            try:
                config = _build_alembic_config(temp_uri)
                with temp_engine.begin() as connection:
                    config.attributes["connection"] = connection
                    command.upgrade(config, target_revision)
                # Reflect the table from temp engine
                metadata = sa.MetaData()
                table = sa.Table(table_name, metadata, autoload_with=temp_engine)
                return table
            finally:
                temp_engine.dispose()
    else:
        # For other backends, use a separate temporary schema/connection
        temp_engine = get_or_create_engine(db_uri)
        try:
            # Temporarily drop tables and upgrade in a separate connection
            with temp_engine.begin() as connection:
                inspector = sa.inspect(connection)
                for t_name in inspector.get_table_names():
                    if t_name != "alembic_version":
                        connection.execute(sa.text(f"DROP TABLE IF EXISTS {t_name}"))
                connection.execute(sa.text("DELETE FROM alembic_version"))

            config = _build_alembic_config(db_uri)
            with temp_engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, target_revision)

            # Reflect the table
            metadata = sa.MetaData()
            table = sa.Table(table_name, metadata, autoload_with=temp_engine)
            return table
        finally:
            temp_engine.dispose()


def _downgrade_to_revision(engine: sa.Engine, db_uri: str, target_revision: str) -> None:
    """Downgrade to a specific revision, handling merge points safely.

    When downgrading through merge points, Alembic may fail with "Ambiguous walk".
    This function tries direct downgrade first, then falls back to clearing the
    database and re-upgrading to the target, which avoids merge point issues.
    """
    config = _build_alembic_config(db_uri)

    # Get current revision
    current = _get_current_db_revision(engine)
    if current == target_revision:
        return  # Already at target

    # Try direct downgrade first
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, target_revision)
        return
    except Exception as e:
        # Direct downgrade failed, possibly due to merge point.
        # Fall back to clearing and re-upgrading to the target.
        if "Ambiguous walk" not in str(e):
            raise

    # Clear the database and upgrade to the target from scratch.
    # This avoids issues with merge points when downgrading.
    with engine.begin() as connection:
        inspector = sa.inspect(connection)
        for table_name in inspector.get_table_names():
            if table_name != "alembic_version":
                # Use appropriate quoting for different dialects
                if engine.dialect.name == "sqlite":
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS [{table_name}]"))
                else:
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS {table_name}"))
        # Clear alembic_version to reset migration state
        if "alembic_version" in inspector.get_table_names():
            connection.execute(sa.text("DELETE FROM alembic_version"))

    # Upgrade to target revision from empty state
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, target_revision)


@pytest.mark.parametrize("failed_attempts", [0, 2, 3])
def test_preferences_migration_round_trip_and_copy_failures(
    db_uri: str, failed_attempts: int, capfd: pytest.CaptureFixture[str]
) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name == "cockroachdb":
        pytest.skip("CockroachDB transaction restarts are covered in test_cockroachdb.py")

    # Reflect schemas at specific revisions before setting up test data.
    # Use temporary databases to avoid multiple heads from reaching intermediate revisions.
    preferences = _get_schema_at_revision(db_uri, "jj1a2b3c4d5e", "preferences")
    users = _get_schema_at_revision(db_uri, "ii1a2b3c4d5e", "users")

    # Set up the main database at the revision before the migration we're testing
    _reach_revision(engine, db_uri, "ii1a2b3c4d5e")

    # Re-reflect the users table from the main engine after reaching ii state
    # to ensure all column information is correct for the actual engine
    users_metadata = sa.MetaData()
    users = sa.Table("users", users_metadata, autoload_with=engine)

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
        if value is not None and len(value) <= 65_535 and failed_attempts < 3
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

    # Verify downgrade by downgrading from current state back to ii1a2b3c4d5e.
    # Use step-by-step downgrade to handle merge points safely.
    _downgrade_to_revision(engine, db_uri, "ii1a2b3c4d5e")
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
        # Try to upgrade back to head, handling any multiple-heads issues from downgrade.
        # If direct upgrade fails due to merge points, use the fallback approach.
        try:
            _initialize_or_verify_schema(engine, db_uri)
        except Exception as e:
            if "more than one head" not in str(e):
                raise
            # Multiple heads - use fallback: clear and re-upgrade to head
            with engine.begin() as connection:
                inspector = sa.inspect(connection)
                for table_name in inspector.get_table_names():
                    if table_name != "alembic_version":
                        if engine.dialect.name == "sqlite":
                            connection.execute(sa.text(f"DROP TABLE IF EXISTS [{table_name}]"))
                        else:
                            connection.execute(sa.text(f"DROP TABLE IF EXISTS {table_name}"))
                if "alembic_version" in inspector.get_table_names():
                    connection.execute(sa.text("DELETE FROM alembic_version"))

            config = _build_alembic_config(db_uri)
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
