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
    assert project["main_session_id"]["nullable"] is True
    assert project["ticket_stages"]["nullable"] is True
    assert project["ticket_stages"]["default"] is None
    assert project["mandate"]["nullable"] is True
    assert project["mandate"]["default"] is None
    assert project["mandate_proposals"]["nullable"] is True
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


def test_main_pointer_migration_preserves_legacy_rows_and_upgrade_retries(tmp_path):
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

    uri = f"sqlite:///{tmp_path / 'main-pointer.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    project_id, session_id = uuid4().hex, uuid4().hex
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "3d4e5f6a7b8c")
            # Recreate the genuine pre-addition shape, not just its version stamp.
            connection.execute(sa.text("ALTER TABLE projects DROP COLUMN main_session_id"))
            connection.execute(
                sa.text(
                    "INSERT INTO projects "
                    "(workspace_id, id, name, user_id, created_at, orchestrator_session_id) "
                    "VALUES (0, :id, 'Legacy', 'owner', 1, :session)"
                ),
                {"id": bytes.fromhex(project_id), "session": bytes.fromhex(session_id)},
            )
        for _ in range(2):
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            store = SqlAlchemyProjectStore(uri)
            assert store.get(project_id, user_id="owner").main_session_id == session_id
            with engine.begin() as connection:
                row = connection.execute(sa.select(SqlProject.__table__)).mappings().one()
                assert (
                    row["main_session_id"] is None and row["orchestrator_session_id"] == session_id
                )
                config.attributes["connection"] = connection
                command.downgrade(config, "3d4e5f6a7b8c")
    finally:
        engine.dispose()
        clear_engine_cache()


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
            "project_tickets",
            f"{project} AND stage_id = 'investigating' AND {cursor}",
            "created_at, id",
            "ix_project_tickets_stage",
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
            "project_ticket_events",
            "(workspace_id, project_id, ticket_id, id) > (:workspace, :project, :ticket, :id)",
            "workspace_id, project_id, ticket_id, id",
            "sqlite_autoindex_project_ticket_events_1",
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
                    stage_id=status,
                    comment="Verified" if status == "done" else None,
                ),
                actor,
            )
        records.append((ticket, tickets.events(project.id, ticket["id"], user_id=actor.user_id)))
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.update(SqlProjectTicket)
                .where(SqlProjectTicket.project_id == project.id)
                .values(stage_id=None)
            )
            config.attributes["connection"] = connection
            command.downgrade(config, "e8a9b0c1d2e3")
        assert "lead_session_id" not in {
            column["name"] for column in sa.inspect(engine).get_columns("project_tickets")
        }
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        for ticket, events in records:
            assert tickets.get(project.id, ticket["id"], user_id=actor.user_id) == ticket
            assert tickets.events(project.id, ticket["id"], user_id=actor.user_id) == events
    finally:
        engine.dispose()
        clear_engine_cache()


@pytest.mark.parametrize("interrupt", [False, True])
def test_note_migration_preserves_text_metadata_and_retries_without_duplicates(
    tmp_path, interrupt
):
    from omnigent.entities.project_ticket import CreateTicket, TicketActor, UpdateTicket
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore

    uri = f"sqlite:///{tmp_path / 'notes.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    actor = TicketActor("owner", uuid4().hex, uuid4().hex)
    project = SqlAlchemyProjectStore(uri).create(uuid4().hex, "Existing notes", actor.user_id)
    tickets = ProjectTicketStore(uri)
    history_ticket, snapshot_ticket, empty_ticket = [
        tickets.create(
            project.id, CreateTicket(title=name, description="Keep this description"), actor
        )
        for name in ("History", "Snapshot", "Empty")
    ]
    tickets.link_session(project.id, history_ticket["id"], actor.session_id, "attach", actor)
    tickets.update(
        project.id,
        history_ticket["id"],
        UpdateTicket(labels=[{"key": "needs-human"}]),
        actor,
    )
    tickets.link_ticket(project.id, history_ticket["id"], snapshot_ticket["id"], "attach", actor)
    plan = "  **Approach**\n\n- café 🦊\n\n"
    review = "Evidence: `check`\n  Keep the spacing.  "
    result = "**Delivered**\n\n[Evidence](https://example.com/result)\n"
    ticket_table = SqlProjectTicket.__table__
    event_table = SqlProjectTicketEvent.__table__
    original_tables = (*TABLES, SqlProject.__table__)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "1b2c3d4e5f6a")
            for record in (history_ticket, snapshot_ticket):
                connection.execute(
                    ticket_table.update()
                    .where(ticket_table.c.id == record["id"])
                    .values(
                        plan=plan,
                        review_summary=review,
                        completion_summary=result,
                    )
                )
            connection.execute(
                ticket_table.update()
                .where(ticket_table.c.id == empty_ticket["id"])
                .values(
                    plan="",
                    review_summary=None,
                    completion_summary="",
                )
            )
            position = connection.execute(
                sa.select(ticket_table.c.next_event_position).where(
                    ticket_table.c.id == history_ticket["id"]
                )
            ).scalar_one()
            historical_ids = [uuid4().hex for _ in range(103)]
            for offset, event_id in enumerate(historical_ids):
                changes = (
                    {"plan": plan, "review_summary": review}
                    if offset < 2
                    else {"title": f"Previous title {offset}"}
                )
                connection.execute(
                    event_table.insert().values(
                        workspace_id=0,
                        project_id=project.id,
                        ticket_id=history_ticket["id"],
                        id=event_id,
                        position=position + offset,
                        kind=1,
                        user_id=actor.user_id,
                        session_id=actor.session_id,
                        agent_id=actor.agent_id,
                        payload={"changes": changes},
                        created_at=1000 + offset,
                        notification_state=1,
                    )
                )
            connection.execute(
                ticket_table.update()
                .where(ticket_table.c.id == history_ticket["id"])
                .values(
                    next_event_position=position + len(historical_ids),
                )
            )
            before = {
                table.name: [dict(row) for row in connection.execute(sa.select(table)).mappings()]
                for table in original_tables
            }

        first_ticket = None

        def fail_next_ticket(_conn, _cursor, statement, _parameters, context, _many):
            nonlocal first_ticket
            if not statement.startswith("INSERT INTO project_ticket_events"):
                return
            ticket_id = context.compiled_parameters[0]["ticket_id"]
            if first_ticket is None:
                first_ticket = ticket_id
            elif ticket_id != first_ticket:
                raise RuntimeError("Interrupted note import")

        if interrupt:
            sa.event.listen(engine, "before_cursor_execute", fail_next_ticket)
            try:
                with pytest.raises(RuntimeError, match="Interrupted note import"):
                    with engine.connect() as connection:
                        config.attributes["connection"] = connection
                        command.upgrade(config, "head")
            finally:
                sa.event.remove(engine, "before_cursor_execute", fail_next_ticket)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    == "1b2c3d4e5f6a"
                )
                partial_count = connection.execute(
                    sa.select(sa.func.count()).select_from(event_table)
                ).scalar_one()
                assert (
                    len(before[event_table.name])
                    < partial_count
                    < len(before[event_table.name]) + 4
                )

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with engine.connect() as connection:
            after = {
                table.name: [dict(row) for row in connection.execute(sa.select(table)).mappings()]
                for table in original_tables
            }
        original_ids = {row["id"] for row in before[event_table.name]}
        preserved = {
            row["id"]: row for row in after[event_table.name] if row["id"] in original_ids
        }
        assert preserved == {row["id"]: row for row in before[event_table.name]}
        imports = [row for row in after[event_table.name] if row["id"] not in original_ids]
        assert len(imports) == 4
        for row in imports:
            assert row["kind"] == 2 and row["notification_state"] == 2
            assert row["user_id"] is row["session_id"] is row["agent_id"] is None
            assert row["payload"]["imported"] is True
        expected_snapshot = [
            f"### Plan\n\n{plan}",
            f"### Review notes\n\n{review}",
            f"### Result\n\n{result}",
        ]
        assert sorted(
            row["payload"]["text"] for row in imports if row["ticket_id"] == snapshot_ticket["id"]
        ) == sorted(expected_snapshot)
        assert [
            row["payload"]["text"] for row in imports if row["ticket_id"] == history_ticket["id"]
        ] == [expected_snapshot[-1]]
        for original in before[ticket_table.name]:
            saved = next(
                row.copy() for row in after[ticket_table.name] if row["id"] == original["id"]
            )
            added = [row for row in imports if row["ticket_id"] == original["id"]]
            assert sorted(row["position"] for row in added) == list(
                range(
                    original["next_event_position"], original["next_event_position"] + len(added)
                )
            )
            saved["next_event_position"] -= len(added)
            saved["comment_count"] -= len(added)
            assert saved == original
        for table in (
            SqlProject.__table__,
            SqlProjectTicketSession.__table__,
            SqlProjectTicketLink.__table__,
        ):
            assert after[table.name] == before[table.name]

        events = tickets.events(project.id, history_ticket["id"], user_id=actor.user_id)["data"]
        projected = [event for event in events if event["id"] in historical_ids[:2]]
        assert len(projected) == 2
        assert all(
            event["payload"]["text"] == "\n\n".join(expected_snapshot[:2]) for event in projected
        )
        assert [event["created_at"] for event in projected] == [1000, 1001]
        assert all(
            event["user_id"] == actor.user_id and event["session_id"] == actor.session_id
            for event in projected
        )

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "1b2c3d4e5f6a")
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for table in original_tables:
                assert [
                    dict(row) for row in connection.execute(sa.select(table)).mappings()
                ] == after[table.name]
    finally:
        engine.dispose()
        clear_engine_cache()


@pytest.mark.parametrize("interruption", [None, "page", "column"])
def test_response_field_removal_preserves_other_data_and_retries(tmp_path, interruption):
    from omnigent.db.envelope import open_envelope
    from omnigent.entities.project_ticket import (
        CommentOnTicket,
        CreateTicket,
        TicketActor,
        UpdateTicket,
        UpdateTicketStages,
    )
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore

    uri = f"sqlite:///{tmp_path / 'field-removal.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    actor = TicketActor("owner", uuid4().hex, uuid4().hex)
    projects = SqlAlchemyProjectStore(uri)
    project, other = [projects.create(uuid4().hex, name, actor.user_id) for name in ("A", "B")]
    tickets = ProjectTicketStore(uri)
    tickets.update_stages(
        project.id,
        UpdateTicketStages(
            stages=[
                {"id": "triage", "name": "Triage", "instructions": "Read evidence"},
                {"id": "shipped", "name": "Shipped"},
            ]
        ),
        actor,
    )
    records = [
        tickets.create(owner.id, CreateTicket(title=title, description="Keep the scope"), actor)
        for owner, title in ((project, "Work"), (project, "Related"), (other, "Other work"))
    ]
    main, related, _ = records
    tickets.link_session(
        project.id, main["id"], actor.session_id, "attach", actor, involvement="Stage owner"
    )
    tickets.link_ticket(project.id, main["id"], related["id"], "attach", actor)
    tickets.update(
        project.id,
        main["id"],
        UpdateTicket(stage_id="shipped", labels=[{"key": "needs-human", "value": "decision"}]),
        actor,
    )
    prose = "**Evidence**\n\nKeep café 🦊 and the authored `lead_session_id` text.\n"
    tickets.comment(
        project.id,
        main["id"],
        CommentOnTicket(text=prose, outputs=[{"type": "link", "url": "https://example.com/work"}]),
        actor,
    )
    cases = [
        (1, {"lead_session_id": actor.session_id}, None),
        (1, {"changes": {"lead_session_id": None}}, None),
        (
            1,
            {"changes": {"lead_session_id": actor.session_id, "plan": prose, "tags": []}},
            {"changes": {"plan": prose, "tags": []}},
        ),
        (
            1,
            {
                "action": "remove",
                "session_id": actor.session_id,
                "changes": {"lead_session_id": None},
            },
            {"action": "remove", "session_id": actor.session_id},
        ),
        (2, {"lead_session_id": actor.session_id, "text": prose}, {"text": prose}),
        (0, {"lead_session_id": actor.session_id}, {}),
        (
            1,
            {
                "lead_session_id": None,
                "changes": {"lead_session_id": actor.session_id, "description": prose},
            },
            {"changes": {"description": prose}},
        ),
    ]
    event_table = SqlProjectTicketEvent.__table__
    original_tables = (*TABLES, SqlProject.__table__)

    def snapshot(connection):
        return {
            table.name: list(
                connection.exec_driver_sql(
                    f"SELECT * FROM {table.name} "
                    f"ORDER BY {', '.join(table.primary_key.columns.keys())}"
                ).mappings()
            )
            for table in original_tables
        }

    expected_payloads = {}
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "2c3d4e5f6a7b")
            legacy = sa.Table("project_tickets", sa.MetaData(), autoload_with=connection)
            for record in records:
                connection.execute(
                    legacy.update()
                    .where(legacy.c.id == bytes.fromhex(record["id"]))
                    .values(lead_session_id=bytes.fromhex(actor.session_id))
                )
            for offset in range(110):
                record = records[offset % len(records)]
                kind, payload, expected = cases[offset % len(cases)]
                event_id = uuid4().hex
                expected_payloads[bytes.fromhex(event_id)] = expected
                connection.execute(
                    event_table.insert().values(
                        workspace_id=0,
                        project_id=record["project_id"],
                        ticket_id=record["id"],
                        id=event_id,
                        position=100 + offset,
                        kind=kind,
                        user_id=actor.user_id,
                        session_id=actor.session_id,
                        agent_id=actor.agent_id,
                        payload=payload,
                        created_at=1000 + offset,
                        notification_state=offset % 3,
                    )
                )
            for record in records:
                connection.execute(
                    legacy.update()
                    .where(legacy.c.id == bytes.fromhex(record["id"]))
                    .values(next_event_position=210)
                )
            before = snapshot(connection)

        mutations = 0

        def interrupt_cleanup(_conn, _cursor, statement, _parameters, _context, _many):
            nonlocal mutations
            if statement.startswith(
                ("UPDATE project_ticket_events", "DELETE FROM project_ticket_events")
            ):
                mutations += 1
                if interruption == "page" and mutations == 101:
                    raise RuntimeError("Interrupted field cleanup")
            if interruption == "column" and statement.startswith(
                "ALTER TABLE project_tickets DROP COLUMN"
            ):
                raise RuntimeError("Interrupted field cleanup")

        if interruption:
            sa.event.listen(engine, "after_cursor_execute", interrupt_cleanup)
            try:
                with pytest.raises(RuntimeError, match="Interrupted field cleanup"):
                    with engine.connect() as connection:
                        config.attributes["connection"] = connection
                        command.upgrade(config, "head")
            finally:
                sa.event.remove(engine, "after_cursor_execute", interrupt_cleanup)
            with engine.connect() as connection:
                assert (
                    connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                    == "2c3d4e5f6a7b"
                )
                partial = connection.execute(
                    sa.select(event_table.c.id, event_table.c.payload)
                ).all()
                remaining = sum(
                    "lead_session_id" in row.payload
                    or "lead_session_id" in row.payload.get("changes", {})
                    for row in partial
                )
                assert 0 < remaining < 110 if interruption == "page" else remaining == 0
            assert (
                "lead_session_id"
                in {col["name"] for col in sa.inspect(engine).get_columns("project_tickets")}
            ) == (interruption == "page")

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        assert "lead_session_id" not in {
            col["name"] for col in sa.inspect(engine).get_columns("project_tickets")
        }
        with engine.connect() as connection:
            after = snapshot(connection)
        assert after["project_tickets"] == [
            {key: value for key, value in row.items() if key != "lead_session_id"}
            for row in before["project_tickets"]
        ]
        for table in (
            SqlProject.__table__,
            SqlProjectTicketSession.__table__,
            SqlProjectTicketLink.__table__,
        ):
            assert after[table.name] == before[table.name]
        surviving = {row["id"]: dict(row) for row in after[event_table.name]}
        removed = {event_id for event_id, payload in expected_payloads.items() if payload is None}
        assert set(surviving) == {row["id"] for row in before[event_table.name]} - removed
        for original in before[event_table.name]:
            event_id = original["id"]
            if event_id in removed:
                continue
            row = surviving[event_id]
            if event_id not in expected_payloads:
                assert row == original
            else:
                assert open_envelope(row["payload"]) == expected_payloads[event_id]
                assert {key: value for key, value in row.items() if key != "payload"} == {
                    key: value for key, value in original.items() if key != "payload"
                }

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "2c3d4e5f6a7b")
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM project_tickets WHERE lead_session_id IS NOT NULL"
                    )
                )
                == 0
            )
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            assert snapshot(connection) == after
        page = tickets.events(project.id, main["id"], user_id=actor.user_id, after=99)
        assert page["last_position"] > 99
        assert all(entry["position"] >= 100 for entry in page["data"])
    finally:
        engine.dispose()
        clear_engine_cache()


def test_stages_migration_preserves_legacy_rows_and_refuses_lossy_downgrade(tmp_path):
    uri = f"sqlite:///{tmp_path / 'stages.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0a1b2c3d4e5f")
            tables = {
                name: sa.Table(name, sa.MetaData(), autoload_with=connection)
                for name in ("projects", *(table.name for table in TABLES))
            }
            connection.execute(
                tables["projects"]
                .insert()
                .values(workspace_id=0, id=b"p" * 16, name="Existing", user_id=None, created_at=1)
            )
            for status in range(5):
                connection.execute(
                    tables["project_tickets"]
                    .insert()
                    .values(
                        workspace_id=0,
                        project_id=b"p" * 16,
                        id=bytes([status]) * 16,
                        title=b"original title",
                        description=b"original description",
                        status=status,
                        tags=b"original tags",
                        completion_summary=b"original summary" if status == 2 else None,
                        plan=b"original plan",
                        review_summary=b"original review",
                        latest_update=b"original update",
                        comment_count=1,
                        next_event_position=1,
                        created_at=1,
                        updated_at=2,
                    )
                )
            connection.execute(
                tables["project_ticket_events"]
                .insert()
                .values(
                    workspace_id=0,
                    project_id=b"p" * 16,
                    ticket_id=bytes(16),
                    id=b"e" * 16,
                    position=0,
                    kind=1,
                    payload=b"historical status",
                    created_at=2,
                    notification_state=0,
                )
            )
            connection.execute(
                tables["project_ticket_sessions"]
                .insert()
                .values(
                    workspace_id=0,
                    project_id=b"p" * 16,
                    ticket_id=bytes(16),
                    session_id=b"s" * 16,
                    involvement=b"original involvement",
                    created_at=1,
                )
            )
            connection.execute(
                tables["project_ticket_links"]
                .insert()
                .values(
                    workspace_id=0,
                    project_id=b"p" * 16,
                    ticket_id=bytes(16),
                    related_ticket_id=bytes([1]) * 16,
                    created_at=1,
                )
            )
            before = {
                name: list(connection.execute(table.select()).mappings())
                for name, table in tables.items()
            }
            command.upgrade(config, "1b2c3d4e5f6a")
            for name, table in tables.items():
                assert list(connection.execute(table.select()).mappings()) == before[name]
            assert (
                connection.scalar(sa.text("SELECT stage_id FROM project_tickets LIMIT 1")) is None
            )
            assert connection.scalar(sa.text("SELECT ticket_stages FROM projects LIMIT 1")) is None
            command.downgrade(config, "0a1b2c3d4e5f")
            command.upgrade(config, "1b2c3d4e5f6a")
            connection.execute(
                sa.text("UPDATE project_tickets SET stage_id = 'investigating' WHERE id = :id"),
                {"id": bytes(16)},
            )
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="Stage data exists"):
                command.downgrade(config, "0a1b2c3d4e5f")
        assert "stage_id" in {
            col["name"] for col in sa.inspect(engine).get_columns("project_tickets")
        }
    finally:
        engine.dispose()
        clear_engine_cache()


def test_involvement_migration_preserves_existing_session_links(tmp_path):
    uri = f"sqlite:///{tmp_path / 'involvement.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    link = {
        "workspace_id": 0,
        "project_id": b"a" * 16,
        "ticket_id": b"b" * 16,
        "session_id": b"c" * 16,
        "created_at": 1,
    }
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "f9a0b1c2d3e4")
            table = sa.Table("project_ticket_sessions", sa.MetaData(), autoload_with=connection)
            connection.execute(table.insert().values(**link))
            command.upgrade(config, "0a1b2c3d4e5f")
            upgraded = sa.Table("project_ticket_sessions", sa.MetaData(), autoload_with=connection)
            assert dict(connection.execute(upgraded.select()).mappings().one()) == {
                **link,
                "involvement": None,
            }
    finally:
        engine.dispose()
        clear_engine_cache()
