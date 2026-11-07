"""Attachment ownership must survive migration and old application inserts."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def test_attachment_default_and_safe_rollback(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'attached.db'}"
    config = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    command.upgrade(config, "mm1a2b3c4d5e")
    insert = sa.text(
        "INSERT INTO hosts (host_id, user_id, name, status, created_at, updated_at) "
        "VALUES (:id, 'alice', :name, 2, 1, 1)"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"id": bytes.fromhex("01" * 16), "name": "before"})
    command.upgrade(config, "nn1a2b3c4d5e")
    with engine.begin() as conn:
        conn.execute(insert, {"id": bytes.fromhex("02" * 16), "name": "after"})
        assert conn.execute(sa.text("SELECT sandbox_attached FROM hosts")).scalars().all() == [
            0,
            0,
        ]
        conn.execute(sa.text("UPDATE hosts SET sandbox_attached = true WHERE name = 'after'"))
    command.upgrade(config, "no1a2b3c4d5e")
    with engine.begin() as conn:
        conn.execute(insert, {"id": bytes.fromhex("03" * 16), "name": "old-writer"})
        assert (
            conn.execute(sa.text("SELECT display_name FROM hosts")).scalars().all() == [None] * 3
        )
        conn.execute(sa.text("UPDATE hosts SET display_name = 'My project' WHERE name = 'after'"))
        assert (
            conn.execute(
                sa.text("SELECT name FROM hosts WHERE display_name IS NOT NULL")
            ).scalar_one()
            == "after"
        )
    command.downgrade(config, "nn1a2b3c4d5e")
    assert "display_name" not in {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    with pytest.raises(RuntimeError, match="Detach externally owned"):
        command.downgrade(config, "mm1a2b3c4d5e")
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM hosts WHERE sandbox_attached = true"))
    command.downgrade(config, "mm1a2b3c4d5e")
    assert "sandbox_attached" not in {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    engine.dispose()


@pytest.mark.parametrize(
    "parent", ["no1a2b3c4d5e", "np1a2b3c4d5e", "c8e4f2a6b901", "nq1a2b3c4d5e", "b2e4f6a8c013"]
)
def test_sandbox_schema_merges_with_main(tmp_path: Path, parent: str) -> None:
    uri = f"sqlite:///{tmp_path / 'merged.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, parent)
    command.upgrade(config, "head")
    engine = sa.create_engine(uri)
    try:
        inspector = sa.inspect(engine)
        assert {"sandbox_attached", "display_name"} <= {
            column["name"] for column in inspector.get_columns("hosts")
        }
        assert "ix_conversation_metadata_host_id" in {
            index["name"] for index in inspector.get_indexes("omnigent_conversation_metadata")
        }
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["nr1a2b3c4d5e"]
    finally:
        engine.dispose()
