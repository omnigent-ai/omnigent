"""Migration ``nn1a2b3c4d5e`` drops ``ix_agents_created_at``.

Both agent listings walk ``ix_agents_kind_owner_created``, so the old index is
gone at head, and the listings still seek an index rather than scan the table.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy import event

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

_OLD = "ix_agents_created_at"
_SHARED = "ix_agents_kind_owner_created"


def _agent_indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {i["name"]: i["column_names"] for i in sa.inspect(engine).get_indexes("agents")}


def test_index_is_gone_at_head(tmp_path: Path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        assert _OLD not in _agent_indexes(engine)
        assert _SHARED in _agent_indexes(engine)
    finally:
        clear_engine_cache()


def test_downgrade_restores_index(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "nn1a2b3c4d5e")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "mm1a2b3c4d5e")
        assert _agent_indexes(engine)[_OLD] == ["workspace_id", "created_at", "id"]
    finally:
        engine.dispose()
        clear_engine_cache()


def test_both_listings_seek_the_shared_index(tmp_path: Path) -> None:
    """The store's own listing SQL, explained: an index seek, never a table scan."""
    uri = f"sqlite:///{tmp_path / 'plans.db'}"
    store = SqlAlchemyAgentStore(uri)
    for i in range(3):
        store.create(f"{i + 1:032x}", f"server{i}", f"x/{i}")
        own = f"{i + 100:032x}"
        store.create_user_agent(own, f"mine{i}", f"{own}/sha", owner="alice@example.com")
    engine = get_or_create_engine(uri)
    seen: list[tuple[str, tuple[object, ...]]] = []

    def record(_conn, _cursor, statement, parameters, _context, _many) -> None:
        seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        store.list(limit=2)
        server = seen[-1]
        store.list_user_agents("alice@example.com", limit=1, after=f"{101:032x}")
        mine = seen[-1]
    finally:
        event.remove(engine, "before_cursor_execute", record)
    try:
        with engine.connect() as conn:
            plans = {
                label: " / ".join(
                    row[-1] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}", params)
                )
                for label, (sql, params) in (("server", server), ("mine", mine))
            }
    finally:
        clear_engine_cache()

    for label, plan in plans.items():
        assert f"USING INDEX {_SHARED}" in plan, (label, plan)
        assert "SCAN agents" not in plan, (label, plan)
        assert "TEMP B-TREE" not in plan, f"{label}: the index must supply the order: {plan}"
    assert "created_at<?" in plans["mine"], "a deep page must seek to the cursor"
