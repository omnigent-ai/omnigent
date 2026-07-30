"""Tests for the enums-varchar→SMALLINT migration (``u1a2b3c4d5e6``).

Seeds a database at the prior revision with legacy string enum values,
upgrades through the migration, and asserts each column is now an int
code with the values correctly backfilled and the ``CHECK`` constraints
rejecting out-of-range codes. Also asserts the downgrade restores the
original strings, since the migration ships a reversible ``downgrade``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PRIOR = "t1a2b3c4d5e6"
_THIS = "u1a2b3c4d5e6"


def _seed_legacy_rows(engine: Engine) -> None:
    """Insert one row per table using the pre-migration string values."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations (id, created_at, updated_at, kind, "
                "root_conversation_id) VALUES "
                "('c1', 1, 1, 'default', 'c1'), ('c2', 1, 1, 'sub_agent', 'c2')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversation_items (id, conversation_id, response_id, "
                "created_at, status, position, type, data, search_text) VALUES "
                "('i1', 'c1', 'r1', 1, 'completed', 0, 'message', '{}', '')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO comments (id, conversation_id, path, start_index, "
                "end_index, body, status, created_at, updated_at) VALUES "
                "('cm1', 'c1', 'f', 0, 1, 'b', 'draft', 1, 1), "
                "('cm2', 'c1', 'f', 0, 1, 'b', 'addressed', 1, 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO account_tokens (id, kind, created_at, expires_at, "
                "invited_is_admin) VALUES ('t1', 'invite', 1, 2, 0), ('t2', 'magic', 1, 2, 0)"
            )
        )
        conn.execute(
            sa.text(
                # policies.scope holds legacy string values here (default/session);
                # the migration converts both type and scope to int codes.
                "INSERT INTO policies "
                "(id, name, created_at, type, handler, enabled, scope) VALUES "
                "('p1', 'n1', 1, 'python', 'h', 1, 'default'), "
                "('p2', 'n2', 1, 'url', 'http', 1, 'session')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO hosts (owner, name, host_id, status, created_at, updated_at) "
                "VALUES ('o', 'h1', 'hid1', 'online', 1, 1), ('o', 'h2', 'hid2', 'offline', 1, 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, created_at, name, bundle_location, version, kind) VALUES "
                "('a1', 1, 'tmpl', 'a1/b', 1, 'template'), "
                "('a2', 1, 'sess', 'a2/b', 1, 'session')"
            )
        )


@pytest.fixture
def seeded_engine(tmp_path: Path) -> Iterator[Engine]:
    """Engine at the prior revision, seeded with legacy string rows."""
    uri = f"sqlite:///{tmp_path / 'test.db'}"
    engine = sa.create_engine(uri)
    cfg = _build_alembic_config(uri)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        from alembic import command

        command.upgrade(cfg, _PRIOR)
    _seed_legacy_rows(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def _upgrade(engine: Engine, target: str) -> None:
    from alembic import command

    cfg = _build_alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, target)
    # Drop pooled connections so a subsequent sa.inspect()/SELECT reflects the
    # post-migration schema rather than a cached pre-migration reflection.
    engine.dispose()


def _downgrade(engine: Engine, target: str) -> None:
    from alembic import command

    cfg = _build_alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, target)
    engine.dispose()


def test_columns_become_smallint(seeded_engine: Engine) -> None:
    """After the migration every enum column is a SMALLINT."""
    _upgrade(seeded_engine, _THIS)
    insp = sa.inspect(seeded_engine)
    for table, column in [
        ("conversations", "kind"),
        ("conversation_items", "type"),
        ("conversation_items", "status"),
        ("comments", "status"),
        ("account_tokens", "kind"),
        ("policies", "type"),
        ("policies", "scope"),
        ("hosts", "status"),
        ("agents", "kind"),
    ]:
        col = next(c for c in insp.get_columns(table) if c["name"] == column)
        assert isinstance(col["type"], sa.SmallInteger), f"{table}.{column} is {col['type']}"
        assert not col["nullable"], f"{table}.{column} should stay NOT NULL"


def test_values_backfilled_to_codes(seeded_engine: Engine) -> None:
    """Legacy string values map to their stable int codes."""
    _upgrade(seeded_engine, _THIS)
    with seeded_engine.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT id, kind FROM conversations")).all())
        assert rows == {"c1": 1, "c2": 2}
        item = conn.execute(sa.text("SELECT type, status FROM conversation_items")).one()
        assert tuple(item) == (1, 1)
        comments = dict(conn.execute(sa.text("SELECT id, status FROM comments")).all())
        assert comments == {"cm1": 1, "cm2": 2}
        tokens = dict(conn.execute(sa.text("SELECT id, kind FROM account_tokens")).all())
        assert tokens == {"t1": 1, "t2": 2}
        policies = dict(conn.execute(sa.text("SELECT id, type FROM policies")).all())
        assert policies == {"p1": 1, "p2": 2}
        scopes = dict(conn.execute(sa.text("SELECT id, scope FROM policies")).all())
        assert scopes == {"p1": 1, "p2": 2}
        hosts = dict(conn.execute(sa.text("SELECT name, status FROM hosts")).all())
        assert hosts == {"h1": 1, "h2": 2}
        agents = dict(conn.execute(sa.text("SELECT id, kind FROM agents")).all())
        assert agents == {"a1": 1, "a2": 2}


def test_check_rejects_out_of_range_code(seeded_engine: Engine) -> None:
    """The swapped-in int CHECK rejects a code outside the enum."""
    _upgrade(seeded_engine, _THIS)
    with seeded_engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO conversations (id, created_at, updated_at, kind, "
                    "root_conversation_id) VALUES ('bad', 1, 1, 99, 'bad')"
                )
            )
            conn.commit()


def test_downgrade_restores_strings(seeded_engine: Engine) -> None:
    """The reversible downgrade maps int codes back to the original strings."""
    _upgrade(seeded_engine, _THIS)
    _downgrade(seeded_engine, _PRIOR)
    with seeded_engine.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT id, kind FROM conversations")).all())
        assert rows == {"c1": "default", "c2": "sub_agent"}
        hosts = dict(conn.execute(sa.text("SELECT name, status FROM hosts")).all())
        assert hosts == {"h1": "online", "h2": "offline"}
        comments = dict(conn.execute(sa.text("SELECT id, status FROM comments")).all())
        assert comments == {"cm1": "draft", "cm2": "addressed"}
