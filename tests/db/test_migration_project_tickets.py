import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from omnigent.db.db_models import (
    SqlProject,
    SqlProjectTicket,
    SqlProjectTicketEvent,
    SqlProjectTicketLink,
    SqlProjectTicketSession,
)
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

TABLES = (
    SqlProjectTicket.__table__,
    SqlProjectTicketEvent.__table__,
    SqlProjectTicketSession.__table__,
    SqlProjectTicketLink.__table__,
)
SCHEMA_PATHS = [
    "metadata",
    "migration",
    pytest.param(
        "configured",
        marks=pytest.mark.skipif(
            not os.environ.get("OMNIGENT_TEST_DB_URI"), reason="needs a configured database"
        ),
    ),
]


@pytest.fixture
def ticket_engine(tmp_path, request):
    if request.param == "configured":
        yield get_or_create_engine(request.getfixturevalue("db_uri"))
        return
    uri = f"sqlite:///{tmp_path / 'schema.db'}"
    engine = sa.create_engine(uri) if request.param == "metadata" else get_or_create_engine(uri)
    if request.param == "metadata":
        SqlProject.__table__.create(engine)
        for table in TABLES:
            table.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


@pytest.mark.parametrize("ticket_engine", SCHEMA_PATHS, indirect=True)
def test_schema_paths_agree_on_keys_bounds_defaults_and_indexes(ticket_engine):
    inspector = sa.inspect(ticket_engine)
    for table in TABLES:
        columns = {col["name"]: col for col in inspector.get_columns(table.name)}
        assert set(columns) == set(table.columns.keys())
        assert all(col["default"] is None for col in columns.values())
        assert inspector.get_pk_constraint(table.name)["constrained_columns"] == list(
            table.primary_key.columns.keys()
        )
        assert inspector.get_foreign_keys(table.name) == []
        expected = {index.name: [col.name for col in index.columns] for index in table.indexes}
        assert {
            index["name"]: index["column_names"] for index in inspector.get_indexes(table.name)
        } == expected
    project = {col["name"]: col for col in inspector.get_columns("projects")}
    assert project["orchestrator_session_id"]["nullable"] is True
    # SQLite ignores VARCHAR lengths, so the migration and metadata enforce a byte cap.
    event_table = sa.Table("project_ticket_events", sa.MetaData(), autoload_with=ticket_engine)
    with pytest.raises(sa.exc.DBAPIError), ticket_engine.begin() as connection:
        connection.execute(
            event_table.insert().values(
                workspace_id=0,
                project_id=b"a" * 16,
                ticket_id=b"b" * 16,
                id=b"c" * 16,
                position=0,
                kind=0,
                user_id="x" * 513,
                payload=b"payload",
                created_at=1,
                notification_state=0,
            )
        )


def test_mysql_uses_bounded_binary_and_total_rows_fit():
    for table in TABLES:
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
        assert "BLOB" not in ddl and "TEXT" not in ddl and "JSON" not in ddl
        bounds = [col.type.max_bytes for col in table.columns if hasattr(col.type, "max_bytes")]
        assert all(bound < 16384 for bound in bounds)
        assert sum(bounds) < 60000


@pytest.mark.parametrize("ticket_engine", ["metadata", "migration"], indirect=True)
def test_ticket_pages_seek_indexes_without_sorting(ticket_engine):
    project = "workspace_id = :workspace AND project_id = :project"
    ticket = f"{project} AND ticket_id = :ticket"
    cursor = "(created_at, id) > (:time, :id)"
    outbox_order = "workspace_id, project_id, ticket_id, position, id"
    queries = [
        (
            "project_tickets",
            f"{project} AND {cursor}",
            "created_at, id",
            "ix_project_tickets_created",
        ),
        (
            "project_tickets",
            f"{project} AND status = 0 AND {cursor}",
            "created_at, id",
            "ix_project_tickets_status",
        ),
        (
            "project_ticket_events",
            f"{ticket} AND position > 0",
            "position, id",
            "ix_project_ticket_events_position",
        ),
        (
            "project_ticket_events",
            f"{ticket} AND kind = 4 AND position > 0",
            "position, id",
            "ix_project_ticket_events_kind",
        ),
        (
            "project_ticket_events",
            f"notification_state = 0 AND ({outbox_order}) > "
            "(:workspace, :project, :ticket, 0, :id)",
            outbox_order,
            "ix_project_ticket_events_notification",
        ),
        (
            "project_ticket_sessions",
            f"{ticket} AND session_id > :id",
            "session_id",
            "sqlite_autoindex_project_ticket_sessions_1",
        ),
        (
            "project_ticket_links",
            f"{ticket} AND related_ticket_id > :id",
            "related_ticket_id",
            "sqlite_autoindex_project_ticket_links_1",
        ),
        (
            "project_ticket_links",
            f"{project} AND related_ticket_id = :ticket AND ticket_id > :id",
            "ticket_id",
            "ix_project_ticket_links_reverse",
        ),
    ]
    with ticket_engine.connect() as connection:
        for table, where, order, index in queries:
            plan = connection.execute(
                sa.text(
                    f"EXPLAIN QUERY PLAN SELECT * FROM {table} "
                    f"WHERE {where} ORDER BY {order} LIMIT 32"
                ),
                {
                    "workspace": 0,
                    "project": b"a" * 16,
                    "ticket": b"b" * 16,
                    "time": 1,
                    "id": b"c" * 16,
                },
            ).all()
            detail = " ".join(str(row[-1]) for row in plan)
            assert f"SEARCH {table} USING INDEX {index}" in detail, detail
            assert "TEMP B-TREE" not in detail


def test_migration_is_additive_and_reversible_before_ticket_writes(tmp_path):
    uri = f"sqlite:///{tmp_path / 'rollback.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "d3bda6dbade3")
        assert not {table.name for table in TABLES} & set(sa.inspect(engine).get_table_names())
        assert "orchestrator_session_id" not in {
            col["name"] for col in sa.inspect(engine).get_columns("projects")
        }
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "f9a0b1c2d3e4")
        assert {table.name for table in TABLES} <= set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
        clear_engine_cache()


def test_lifecycle_migration_preserves_existing_ticket_statuses_and_history(tmp_path):
    from omnigent.entities.project_ticket import CreateTicket, TicketActor, UpdateTicket
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore

    uri = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    actor = TicketActor("owner")
    project = SqlAlchemyProjectStore(uri).create(uuid4().hex, "Existing", actor.user_id)
    tickets = ProjectTicketStore(uri)
    records = []
    for status in ("open", "in_progress", "done"):
        ticket = tickets.create(project.id, CreateTicket(title=status), actor)
        if status != "open":
            ticket = tickets.update(
                project.id,
                ticket["id"],
                UpdateTicket(
                    status=status,
                    completion_summary="Verified" if status == "done" else None,
                ),
                actor,
            )
        records.append((ticket, tickets.events(project.id, ticket["id"], user_id=actor.user_id)))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "e8a9b0c1d2e3")
        assert "lead_session_id" not in {
            column["name"] for column in sa.inspect(engine).get_columns("project_tickets")
        }
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "f9a0b1c2d3e4")
        for ticket, events in records:
            assert tickets.get(project.id, ticket["id"], user_id=actor.user_id) == ticket
            assert tickets.events(project.id, ticket["id"], user_id=actor.user_id) == events
    finally:
        engine.dispose()
        clear_engine_cache()
