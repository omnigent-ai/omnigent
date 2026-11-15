"""Tests for the saved workflows migration (ce12af3da168)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.db_models import SqlWorkflow
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine
from omnigent.stores.workflow_store import WorkflowStore

_PREVIOUS = "5d872cd6632a"
_TABLE = "workflows"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.connect() as conn:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(conn, _crdb_server_version(engine))
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)
        conn.commit()


@pytest.mark.parametrize(
    "previous_revision", ["base", "9a2c7e6b4f10", "27d16059fab9", "e9b6a17c20d4"]
)
def test_upgrade_matches_the_model_and_downgrade_drops_the_table(
    tmp_path: Path, previous_revision: str
) -> None:
    uri = f"sqlite:///{tmp_path / 'workflows.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, previous_revision)
    _migrate(uri, engine, "head")
    inspector = sa.inspect(engine)

    table = SqlWorkflow.__table__
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    assert list(columns) == [column.name for column in table.columns]
    for column in table.columns:
        assert columns[column.name]["nullable"] is column.nullable, column.name
        assert columns[column.name]["default"] is None, column.name
    assert inspector.get_pk_constraint(_TABLE)["constrained_columns"] == ["workspace_id", "id"]
    indexes = inspector.get_indexes(_TABLE)
    assert {index["name"]: index["column_names"] for index in indexes} == {
        index.name: [column.name for column in index.columns] for index in table.indexes
    }
    assert not any(index["unique"] for index in indexes)
    assert inspector.get_unique_constraints(_TABLE) == []
    assert isinstance(columns["description"]["type"], sa.LargeBinary)

    _migrate(uri, engine, _PREVIOUS, down=True)
    assert _TABLE not in sa.inspect(engine).get_table_names()
    engine.dispose()
    clear_engine_cache()


@pytest.mark.parametrize(
    "description",
    [
        "Review café notes in C:\\reports.",
        "\x00\x02legacy text",
        pytest.param("界" * 2000, id="multibyte-limit"),
    ],
)
def test_description_envelopes_preserve_legacy_text_and_downgrade(
    db_uri: str, description: str
) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name in ("postgresql", "cockroachdb") and "\x00" in description:
        pytest.skip("The PostgreSQL driver cannot bind legacy text containing NUL")
    previous = "e9b6a17c20d4"
    _migrate(db_uri, engine, previous, down=True)
    try:
        workflow_id = "0123456789abcdef0123456789abcdef"
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO workflows (workspace_id, id, user_id, name, description, plan, "
                    "created_at, updated_at, planning_session_id) "
                    "VALUES (0, :id, NULL, 'Review', :description, :plan, 1, 1, NULL)"
                ),
                {
                    "id": bytes.fromhex(workflow_id),
                    "description": description,
                    "plan": b'{"steps":[]}',
                },
            )
        _migrate(db_uri, engine, "head")
        store = WorkflowStore(db_uri)
        assert store.get(workflow_id, user_id=None).description == description
        updated = store.update(
            workflow_id, user_id=None, description=description * min(5, 2000 // len(description))
        )
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}
        assert isinstance(columns["description"]["type"], sa.LargeBinary)
        assert not columns["description"]["nullable"]
        assert not any(name.startswith("_description_") for name in columns)
        with engine.connect() as connection:
            assert isinstance(
                connection.execute(sa.text("SELECT description FROM workflows")).scalar_one(),
                bytes,
            )
            assert (
                len(connection.execute(sa.text("SELECT description FROM workflows")).scalar_one())
                <= 2000
            )
        _migrate(db_uri, engine, previous, down=True)
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}
        assert isinstance(columns["description"]["type"], sa.String)
        assert not columns["description"]["nullable"]
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT description FROM workflows")).scalar_one()
                == updated.description
            )
        _migrate(db_uri, engine, "head")
        assert store.get(workflow_id, user_id=None) == updated
    finally:
        _migrate(db_uri, engine, "head")


def test_oversized_legacy_description_stays_intact_until_corrected(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    store = WorkflowStore(db_uri)
    saved = store.create(user_id=None, name="Review", description="Review notes.", steps=[])
    _migrate(db_uri, engine, "e9b6a17c20d4", down=True)
    noise = random.Random(0)
    description = "".join(chr(noise.randrange(0x4E00, 0x9FFF)) for _ in range(2000))
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE workflows SET description = :description"),
            {"description": description},
        )
    try:
        with pytest.raises(ValueError, match="description exceeds 2000 stored bytes"):
            _migrate(db_uri, engine, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT description FROM workflows")).scalar_one()
                == description
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE workflows SET description = :description"),
                {"description": saved.description},
            )
        _migrate(db_uri, engine, "head")
    assert store.get(saved.id, user_id=None) == saved


def test_drafts_downgrade_cleanly_and_existing_plans_upgrade_as_runnable(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'drafts.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, "head")
    store = WorkflowStore(uri)
    saved = store.create(
        user_id=None, name="saved", description="Run it.", steps=[{"id": "a", "name": "A"}]
    )
    draft = store.create(user_id=None, name="draft", description="Plan it.", steps=[])
    for index in range(251):
        store.create(user_id=None, name=f"draft {index}", description="Plan it.", steps=[])
    cleanup_reads = []

    def inspect_cleanup(_conn, _cursor, statement, parameters, _context, _executemany):
        query = getattr(_context.compiled, "statement", None)
        if isinstance(query, sa.Select) and any(
            getattr(table, "name", None) == "workflows" for table in query.get_final_froms()
        ):
            cleanup_reads.append(parameters)
            assert "LIMIT" in statement
        if isinstance(query, sa.Delete) and query.table.name == "workflows":
            assert _conn.get_execution_options().get("isolation_level") == "AUTOCOMMIT"

    sa.event.listen(engine, "before_cursor_execute", inspect_cleanup)
    _migrate(uri, engine, "3b9e4c71d2a8", down=True)
    sa.event.remove(engine, "before_cursor_execute", inspect_cleanup)
    assert len(cleanup_reads) >= 3
    _migrate(uri, engine, "head")
    assert store.get(saved.id, user_id=None) == saved
    assert store.get(draft.id, user_id=None) is None
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM workflows")).scalar_one() == 1
    engine.dispose()
    clear_engine_cache()
