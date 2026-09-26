"""Upgrades from both published incarnations of the project merge revision."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


@pytest.mark.parametrize("state", ["old_merge", "repaired", "fresh"])
def test_upgrade_preserves_projects_and_order_preferences(tmp_path: Path, state: str) -> None:
    uri = f"sqlite:///{tmp_path / 'projects.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            if state == "fresh":
                command.upgrade(config, "c18e2f7a4b90")
            else:
                # The previous production graph joined these two heads, before gh existed.
                command.upgrade(config, "gg1b2c3d4e5f")
                command.upgrade(config, "a5363b7c9d2e")
                command.stamp(config, "c18e2f7a4b90", purge=True)
            columns = {c["name"] for c in sa.inspect(connection).get_columns("users")}
            assert ("project_order" in columns) == (state == "fresh")
            if state == "repaired":
                connection.execute(sa.text("ALTER TABLE users ADD COLUMN project_order BLOB"))
            connection.execute(sa.text("INSERT INTO users (id, is_admin) VALUES ('owner', false)"))
            connection.execute(
                sa.text(
                    "INSERT INTO projects "
                    "(workspace_id, id, name, user_id, created_at, updated_at) "
                    "VALUES (1, :id, 'Preserved project', 'owner', 123, 456)"
                ),
                {"id": bytes.fromhex("a" * 32)},
            )
            if state != "old_merge":
                connection.execute(
                    sa.text("UPDATE users SET project_order = :value WHERE id = 'owner'"),
                    {"value": b"existing preference bytes"},
                )
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            column = next(
                c
                for c in sa.inspect(connection).get_columns("users")
                if c["name"] == "project_order"
            )
            assert isinstance(column["type"], sa.LargeBinary)
            assert column["nullable"]
            assert (
                connection.execute(sa.text("SELECT name FROM projects")).scalar_one()
                == "Preserved project"
            )
            assert connection.execute(
                sa.text("SELECT project_order FROM users WHERE id = 'owner'")
            ).scalar_one() == (None if state == "old_merge" else b"existing preference bytes")
            command.downgrade(config, "c18e2f7a4b90")
            assert "project_order" in {
                c["name"] for c in sa.inspect(connection).get_columns("users")
            }
    finally:
        engine.dispose()
