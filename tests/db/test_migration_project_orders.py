"""Project order migration preserves project data through upgrade and downgrade."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


def test_project_orders_migration_round_trip(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'orders.db'}"
    engine = get_or_create_engine(uri)
    store = SqlAlchemyProjectStore(uri)
    project = store.create("a" * 32, "Existing", None)
    store.save_order([project.id], user_id=None)
    assert "project_orders" not in sa.inspect(engine).get_table_names()
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("users")}
    assert columns["project_order"]["nullable"] is True
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "gg1b2c3d4e5f")
    assert "project_order" not in {c["name"] for c in sa.inspect(engine).get_columns("users")}
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT id FROM users WHERE id = 'local'")).scalar_one()
            == "local"
        )
    assert store.get(project.id, user_id=None) == project
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE users SET is_admin = 1, password_hash = 'existing-hash', "
                "created_at = 123, last_login_at = 456 WHERE id = 'local'"
            )
        )
        users_before = connection.execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one()
        config.attributes["connection"] = connection
        command.upgrade(config, "gh1b2c3d4e5f")
        assert (
            connection.execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one() == users_before
        )
        assert connection.execute(
            sa.text(
                "SELECT is_admin, password_hash, created_at, last_login_at, project_order "
                "FROM users WHERE id = 'local'"
            )
        ).one() == (1, "existing-hash", 123, 456, None)
    assert store.get_order(user_id=None) is None
    assert store.get(project.id, user_id=None) == project
    store.save_order([project.id], user_id=None)
    assert store.get_order(user_id=None) == [project.id]
