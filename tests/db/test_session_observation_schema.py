"""Keep metadata bootstrap compatible with migrations and existing session writers."""

from pathlib import Path

import sqlalchemy as sa

from omnigent.db.db_models import SqlConversationMetadata
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_DEFAULTS = {
    "runner_binding_epoch": 0,
    "live_status_revision": 0,
    "live_status_observation_id": None,
    "live_status_response_id": None,
    "live_status_error_key": None,
    "live_status_error_labels": None,
    "terminal_pending": False,
    "terminal_startup_claim": None,
    "runner_offline_decision": None,
}
_OWNED_VALUES = {
    "runner_binding_epoch": 7,
    "live_status_revision": 11,
    "live_status_observation_id": "observation-a",
    "live_status_response_id": '"response-a"',
    "live_status_error_key": "error-a",
    "live_status_error_labels": '{"source":"test"}',
    "terminal_pending": True,
    "terminal_startup_claim": '{"claim":"test"}',
    "runner_offline_decision": '{"runner":"runner-a"}',
}


def test_metadata_bootstrap_includes_observation_schema(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'metadata-bootstrap.db'}")
    table = SqlConversationMetadata.__table__
    try:
        table.create(engine)
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns(table.name)}
        assert _DEFAULTS.keys() <= columns.keys()
        for name in ("runner_binding_epoch", "live_status_revision"):
            assert isinstance(columns[name]["type"], sa.BigInteger)
            assert not columns[name]["nullable"]
        assert isinstance(columns["terminal_pending"]["type"], sa.Boolean)
        assert not columns["terminal_pending"]["nullable"]
        for name in ("live_status_observation_id", "live_status_error_key"):
            assert isinstance(columns[name]["type"], sa.String)
            assert columns[name]["type"].length == 64
            assert columns[name]["nullable"]
        for name in (
            "live_status_response_id",
            "live_status_error_labels",
            "terminal_startup_claim",
            "runner_offline_decision",
        ):
            assert isinstance(columns[name]["type"], sa.LargeBinary)
            assert columns[name]["nullable"]
        with engine.begin() as connection:
            connection.execute(
                sa.text(f"INSERT INTO {table.name} (workspace_id,id,kind) VALUES (0,:id,1)"),
                {"id": bytes.fromhex("a" * 32)},
            )
            values = (
                connection.execute(sa.select(*(table.c[name] for name in _DEFAULTS)))
                .mappings()
                .one()
            )
        assert dict(values) == _DEFAULTS
    finally:
        engine.dispose()


def test_existing_conversation_writers_preserve_observation_columns(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    columns = {
        column["name"]
        for column in sa.inspect(engine).get_columns(SqlConversationMetadata.__tablename__)
    }
    assert _DEFAULTS.keys() <= columns
    store = SqlAlchemyConversationStore(db_uri)
    created = store.create_conversation(
        title="Existing session",
        runner_id="runner-a",
        terminal_launch_args=["--existing"],
        inference_snapshot={"provider_id": "test-provider"},
    )
    table = SqlConversationMetadata.__table__
    row = sa.and_(table.c.workspace_id == 0, table.c.id == created.id)
    query = sa.select(*(table.c[name] for name in _DEFAULTS)).where(row)
    with engine.begin() as connection:
        assert dict(connection.execute(query).mappings().one()) == _DEFAULTS
        connection.execute(table.update().where(row).values(**_OWNED_VALUES))
    assert store.get_conversation(created.id) == created
    updated = store.update_conversation(
        created.id, title="Updated session", terminal_launch_args=["--updated"]
    )
    assert updated is not None
    assert updated.title == "Updated session"
    assert updated.runner_id == "runner-a"
    assert updated.terminal_launch_args == ["--updated"]
    assert updated.inference_snapshot == created.inference_snapshot
    assert store.get_conversation(created.id) == updated
    with engine.connect() as connection:
        assert dict(connection.execute(query).mappings().one()) == _OWNED_VALUES
