"""Tests for the saved workflows migration (ce12af3da168)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlWorkflow
from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from omnigent.stores.workflow_store import WorkflowStore

_PREVIOUS = "5d872cd6632a"
_TABLE = "workflows"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


def test_upgrade_matches_the_model_and_downgrade_drops_the_table(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'workflows.db'}"
    engine = sa.create_engine(uri)
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

    _migrate(uri, engine, _PREVIOUS, down=True)
    assert _TABLE not in sa.inspect(engine).get_table_names()
    engine.dispose()
    clear_engine_cache()


def test_drafts_downgrade_cleanly_and_existing_plans_upgrade_as_runnable(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'drafts.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, "head")
    store = WorkflowStore(uri)
    saved = store.create(
        user_id=None, name="saved", description="Run it.", steps=[{"id": "a", "name": "A"}]
    )
    draft = store.create(user_id=None, name="draft", description="Plan it.", steps=[])
    _migrate(uri, engine, "3b9e4c71d2a8", down=True)
    _migrate(uri, engine, "head")
    assert store.get(saved.id, user_id=None) == saved
    assert store.get(draft.id, user_id=None) is None
    engine.dispose()
    clear_engine_cache()
