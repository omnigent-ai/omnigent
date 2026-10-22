"""The additive schema works on an existing database and survives app rollback."""

import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import _build_alembic_config
from omnigent.session_authority import orchestration_scope
from omnigent.stores.session_link_store import SessionLinkStore, invalidate_link_epoch


def test_session_links_upgrade_and_downgrade(db_uri: str) -> None:
    engine = sa.create_engine(db_uri)
    config = _build_alembic_config(db_uri)
    tables = (
        "session_link_epochs",
        "session_input_authorities",
        "linked_requests",
        "linked_replies",
    )
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "c8e4f2a6b901")
            command.upgrade(config, "head")
        inspector = sa.inspect(engine)
        for name in tables:
            expected = OmnigentBase.metadata.tables[name]
            actual = inspector.get_columns(name)
            assert {column["name"] for column in actual} == set(expected.columns.keys())
            assert all(column["default"] is None for column in actual)
            assert not inspector.get_foreign_keys(name)
            assert inspector.get_pk_constraint(name)["constrained_columns"][0] == "workspace_id"
            assert {index["name"] for index in inspector.get_indexes(name)} == {
                index.name for index in expected.indexes
            }
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "c8e4f2a6b901")
        assert not set(tables) & set(sa.inspect(engine).get_table_names())
        assert "conversations" in sa.inspect(engine).get_table_names()
        assert "ix_conversation_metadata_host_id" in {
            index["name"]
            for index in sa.inspect(engine).get_indexes("omnigent_conversation_metadata")
        }
        # Default-off/custom deployments must tolerate the schema not being installed.
        with orchestration_scope(False), Session(engine) as session:
            invalidate_link_epoch(session, "a" * 32)
            store = SessionLinkStore(db_uri)
            assert store.affected_handoff_deadline(user_id="alice") is None
            assert store.accepted_inputs(["b" * 32]) == set()
        assert not set(tables) & set(sa.inspect(engine).get_table_names())
    finally:
        try:
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()
