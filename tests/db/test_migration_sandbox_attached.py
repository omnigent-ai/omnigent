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
    with pytest.raises(RuntimeError, match="Detach externally owned"):
        command.downgrade(config, "mm1a2b3c4d5e")
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM hosts WHERE sandbox_attached = true"))
    command.downgrade(config, "mm1a2b3c4d5e")
    assert "sandbox_attached" not in {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    engine.dispose()
