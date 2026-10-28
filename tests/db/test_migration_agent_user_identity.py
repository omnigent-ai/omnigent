"""Identity backfill preserves legacy agents and survives an interrupted batch."""

from importlib import import_module

import pytest
import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine, installed_agent_id
from tests.db.test_migration_compress_inference_snapshot import _migrate

_REVISION = "e1a2b3c4d5f6"
_PREVIOUS = "d0a1f879a01d"


@pytest.mark.parametrize("interrupt", [False, True])
def test_agent_identity_backfill(db_uri: str, interrupt: bool) -> None:
    migration = import_module(f"omnigent.db.migrations.versions.{_REVISION}_agent_user_identity")
    engine = get_or_create_engine(db_uri)
    _migrate(engine, _PREVIOUS, downgrade=True)
    agents = sa.Table("agents", sa.MetaData(), autoload_with=engine)
    rows = []
    for number in range(1, migration._BATCH_SIZE + 4):
        row_id = number.to_bytes(16, "big")
        rows.append(
            {
                "workspace_id": 0,
                "id": row_id,
                "created_at": number,
                "name": "Orion" if number == 1 else "orion",
                "bundle_location": f"{row_id.hex()}/bundle",
                "version": 1,
                "kind": 2,
                "created_by": "alice@example.test",
            }
        )
    rows.extend(
        [
            {**rows[0], "workspace_id": 17},
            {**rows[1], "workspace_id": 17, "created_by": None},
            {**rows[2], "workspace_id": 17, "created_by": "bob@example.test"},
            {**rows[3], "workspace_id": 17, "bundle_location": f"ag_{rows[3]['id'].hex()}/bundle"},
            {**rows[4], "workspace_id": 17, "bundle_location": "another-agent/bundle"},
            {**rows[5], "workspace_id": 17, "kind": 1},
        ]
    )
    try:
        with engine.begin() as connection:
            connection.execute(agents.insert(), rows)
        if interrupt:
            updates = 0

            def fail_second_batch(conn, cursor, statement, parameters, context, executemany):
                nonlocal updates
                if statement.startswith("UPDATE agents SET user_identity="):
                    updates += 1
                    if updates == migration._BATCH_SIZE + 1:
                        raise RuntimeError("interrupted identity backfill")

            sa.event.listen(engine, "before_cursor_execute", fail_second_batch)
            try:
                with pytest.raises(RuntimeError, match="interrupted identity backfill"):
                    _migrate(engine, _REVISION)
            finally:
                sa.event.remove(engine, "before_cursor_execute", fail_second_batch)
        _migrate(engine, _REVISION)
        with engine.connect() as connection:
            stored = {
                (row.workspace_id, bytes(row.id)): row
                for row in connection.execute(sa.text("SELECT * FROM agents"))
            }
        assert len(stored) == len(rows)
        for row in rows:
            actual = stored[(row["workspace_id"], row["id"])]
            for name, value in row.items():
                assert getattr(actual, name) == value
            original = row["kind"] == 2 and row["bundle_location"] != "another-agent/bundle"
            expected = (
                bytes.fromhex(installed_agent_id(row["created_by"], row["name"]))
                if original
                else None
            )
            assert actual.user_identity == expected
        index = next(
            i
            for i in sa.inspect(engine).get_indexes("agents")
            if i["name"] == "ix_agents_user_identity"
        )
        assert index["column_names"] == ["workspace_id", "user_identity", "created_at", "id"]
        assert not index["unique"]
        _migrate(engine, _PREVIOUS, downgrade=True)
        assert "user_identity" not in {c["name"] for c in sa.inspect(engine).get_columns("agents")}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM agents")).scalar_one() == len(
                rows
            )
        _migrate(engine, _REVISION)
    finally:
        _migrate(engine, "head")
