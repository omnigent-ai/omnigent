"""The project_daily_cost migration (a7c3e9f1b5d2): clean both ways, backfill exact."""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine
from omnigent.server.project_status_spend import legacy_window_rows
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


def _rows(store: SqlAlchemyConversationStore, project_id: str) -> dict[tuple[str, str], int]:
    return {
        (root, day): micros
        for root, day, micros in store.list_project_daily_costs(project_id, None, "9999-12-31")
    }


def test_downgrade_drops_and_upgrade_backfills_the_same_rows(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'spend.db'}"
    engine = get_or_create_engine(uri)
    try:
        conversations = SqlAlchemyConversationStore(uri)
        project = SqlAlchemyProjectStore(uri).create(uuid.uuid4().hex, "zz-backfill", "owner")
        root = conversations.create_conversation(project_id=project.id)
        sub_agent = conversations.create_conversation(parent_conversation_id=root.id)
        conversations.add_session_daily_cost(root.id, "2026-06-10", 0.1234567)
        conversations.add_session_daily_cost(sub_agent.id, "2026-06-10", 2.5)
        conversations.add_session_daily_cost(sub_agent.id, "2026-06-01", 0.0049999)
        written = _rows(conversations, project.id)
        assert written

        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "e7a3c5b9d2f4")
        assert "project_daily_cost" not in sa.inspect(engine).get_table_names()

        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "a7c3e9f1b5d2")
        assert _rows(conversations, project.id) == written
        legacy: dict[tuple[str, str], int] = {}
        for root_id, day, micros in legacy_window_rows(
            conversations, project.id, None, "9999-12-31"
        ):
            legacy[(root_id, day)] = legacy.get((root_id, day), 0) + micros
        assert written == legacy
    finally:
        clear_engine_cache()
