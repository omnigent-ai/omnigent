"""Inference snapshot compression preserves sessions and bounds stored bytes."""

from __future__ import annotations

import json
import random
from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.compression import decode, encode
from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_TABLE = "omnigent_conversation_metadata"
_PREVIOUS = "kk1a2b3c4d5e"
_REVISION = "ll1a2b3c4d5e"
_MIGRATION = "omnigent.db.migrations.versions.ll1a2b3c4d5e_compress_inference_snapshot"


def _migrate(engine: sa.Engine, revision: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)


def _rows(engine: sa.Engine) -> dict[tuple[int, bytes], sa.Row]:
    with engine.connect() as connection:
        return {
            (row.workspace_id, bytes(row.id)): row
            for row in connection.execute(sa.text(f"SELECT * FROM {_TABLE}"))
        }


def test_compression_migration_retains_fitting_snapshots_and_all_sessions(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = import_module(_MIGRATION)
    monkeypatch.setattr(migration, "_BATCH_SIZE", 2)
    engine = get_or_create_engine(db_uri)
    _migrate(engine, _PREVIOUS, downgrade=True)
    metadata = sa.Table(_TABLE, sa.MetaData(), autoload_with=engine)
    conversations = sa.Table("conversations", sa.MetaData(), autoload_with=engine)
    small = '{"provider":"gateway"}'
    unicode = json.dumps({"label": "模型 😀" * 20}, ensure_ascii=False)
    compressible = json.dumps({"models": [f"gateway-model-{i}" for i in range(5000)]})
    oversized = json.dumps({"catalog": random.Random(0).randbytes(100_000).hex()})
    assert len(compressible.encode()) > 65_535
    assert len(encode(compressible)) <= 65_535
    assert len(encode(oversized)) > 65_535
    original = {
        (0, bytes([i]) * 16): value
        for i, value in enumerate([None, small, unicode, compressible, oversized], start=1)
    }
    original[(17, bytes([4]) * 16)] = small
    try:
        with engine.begin() as connection:
            for (workspace_id, row_id), value in original.items():
                connection.execute(
                    metadata.insert().values(
                        workspace_id=workspace_id,
                        id=row_id,
                        kind=1,
                        inference_snapshot=value,
                        task_summary="preserve metadata",
                        session_state=encode('{"untouched":true}'),
                    )
                )
                connection.execute(
                    conversations.insert().values(
                        workspace_id=workspace_id,
                        id=row_id,
                        root_conversation_id=row_id,
                        created_at=1,
                        updated_at=2,
                        title="Preserve conversation",
                    )
                )
        inspector = sa.inspect(engine)
        indexes = inspector.get_indexes(_TABLE)
        checks = inspector.get_check_constraints(_TABLE)
        before = _rows(engine)
        _migrate(engine, _REVISION)
        after = _rows(engine)
        assert set(after) == set(before)
        for key, row in after.items():
            expected = None if original[key] == oversized else original[key]
            assert decode(row.inference_snapshot) == expected
            assert row.inference_snapshot == encode(expected)
            assert row.inference_snapshot is None or len(row.inference_snapshot) <= 65_535
            assert {k: v for k, v in row._mapping.items() if k != "inference_snapshot"} == {
                k: v for k, v in before[key]._mapping.items() if k != "inference_snapshot"
            }
        inspector = sa.inspect(engine)
        assert inspector.get_indexes(_TABLE) == indexes
        assert inspector.get_check_constraints(_TABLE) == checks
        columns = {c["name"]: c for c in inspector.get_columns(_TABLE)}
        assert columns["inference_snapshot"]["nullable"]
        assert isinstance(columns["inference_snapshot"]["type"], sa.LargeBinary)
        assert not any(name.startswith("_inference_snapshot") for name in columns)
        store = SqlAlchemyConversationStore(db_uri)
        for (workspace_id, row_id), value in original.items():
            if workspace_id != 0:
                continue
            conversation = store.get_conversation(row_id.hex())
            assert conversation is not None
            assert conversation.title == "Preserve conversation"
            expected = None if value is None or value == oversized else json.loads(value)
            assert conversation.inference_snapshot == expected
            assert conversation.session_state == {"untouched": True}

        _migrate(engine, _PREVIOUS, downgrade=True)
        for key, row in _rows(engine).items():
            assert row.inference_snapshot == (
                None if original[key] == oversized else original[key]
            )
        _migrate(engine, _REVISION)
        assert _rows(engine) == after
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        assert _rows(engine) == after
    finally:
        _migrate(engine, "head")


@pytest.mark.parametrize("to_binary", [False, True])
def test_migration_can_finish_interrupted_column_swap(tmp_path: Path, to_binary: bool) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'swap.db'}")
    temporary = "_inference_snapshot_blob" if to_binary else "_inference_snapshot_text"
    value = encode('{"saved":true}') if to_binary else '{"saved":true}'
    table = sa.Table(
        _TABLE,
        sa.MetaData(),
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", sa.LargeBinary(), primary_key=True),
        sa.Column(temporary, sa.LargeBinary() if to_binary else sa.Text()),
        sa.Column("task_summary", sa.Text()),
    )
    table.create(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                table.insert().values(
                    {"workspace_id": 0, "id": b"a" * 16, temporary: value, "task_summary": "keep"}
                )
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration = import_module(_MIGRATION)
                (migration.upgrade if to_binary else migration.downgrade)()
        row = _rows(engine)[(0, b"a" * 16)]
        assert row.inference_snapshot == value
        assert row.task_summary == "keep"
    finally:
        engine.dispose()


def test_offline_migration_does_not_emit_incomplete_sql() -> None:
    context = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True})
    with Operations.context(context):
        with pytest.raises(RuntimeError, match="requires an online migration"):
            import_module(_MIGRATION).upgrade()
