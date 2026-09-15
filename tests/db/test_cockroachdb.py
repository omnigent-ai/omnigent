"""Live CockroachDB compatibility checks selected by OMNIGENT_TEST_DB_URI."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, inspect, text

from omnigent.db.cockroachdb import (
    _CRDB_BOOTSTRAP_MARKER_TABLE,
    _CRDB_BOOTSTRAP_MARKER_TOKEN,
    CRDB_BASELINE_REVISION,
    _crdb_server_version,
    _prepare_crdb_schema_transaction,
)
from omnigent.db.utils import (
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
    get_or_create_engine,
    is_cockroachdb,
)


def _crdb_engine(db_uri: str) -> Engine:
    engine = get_or_create_engine(db_uri)
    if not is_cockroachdb(engine.dialect.name):
        pytest.skip("requires OMNIGENT_TEST_DB_URI pointing to CockroachDB")
    return engine


def test_cockroachdb_bootstrap_is_at_head(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    assert _get_current_db_revision(engine) == _get_head_db_revision(db_uri)


def test_cockroachdb_uses_read_committed(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    with engine.connect() as connection:
        isolation = connection.execute(text("SHOW transaction_isolation")).scalar_one()
    assert str(isolation).lower() == "read committed"


def test_cockroachdb_search_indexes_exist(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    expected = {"ix_conversations_title_trgm"}
    found: set[str] = set()
    with engine.connect() as connection:
        for table in ("conversation_items", "conversations"):
            found.update(
                str(row["index_name"])
                for row in connection.execute(text(f"SHOW INDEXES FROM {table}")).mappings()
            )
    assert expected <= found
    assert "ix_conversation_items_search_text_trgm" not in found


def test_cockroachdb_upgrades_from_supported_baseline(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    head = _get_head_db_revision(db_uri)
    if head == CRDB_BASELINE_REVISION:
        pytest.skip("requires a migration after the CRDB baseline")

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": CRDB_BASELINE_REVISION},
        )

    _initialize_or_verify_schema(engine, db_uri)

    assert _get_current_db_revision(engine) == head


def test_cockroachdb_resumes_empty_revision_and_repairs_indexes(db_uri: str) -> None:
    engine = _crdb_engine(db_uri)
    version = _crdb_server_version(engine)
    head = _get_head_db_revision(db_uri)
    index_name = "ix_agents_created_at"
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, version)
        connection.execute(
            text(
                f"CREATE TABLE {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                "(token STRING PRIMARY KEY, target_revision STRING NOT NULL)"
            )
        )
        connection.commit()
        _prepare_crdb_schema_transaction(connection, version)
        connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        connection.commit()
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                "(token, target_revision) VALUES (:token, :target_revision)"
            ),
            {"token": _CRDB_BOOTSTRAP_MARKER_TOKEN, "target_revision": head},
        )
        connection.execute(text("DELETE FROM alembic_version"))

    _initialize_or_verify_schema(engine, db_uri)

    with engine.connect() as connection:
        found = {
            str(row["index_name"])
            for row in connection.execute(text("SHOW INDEXES FROM agents")).mappings()
        }
    assert _get_current_db_revision(engine) == head
    assert _CRDB_BOOTSTRAP_MARKER_TABLE not in inspect(engine).get_table_names()
    assert index_name in found
