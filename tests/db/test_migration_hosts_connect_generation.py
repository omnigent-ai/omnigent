"""Schema checks for nullable host connect-generation tokens."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.enum_codecs import encode_host_status
from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
    now_epoch,
)

_PARENT_REVISION = "mm1a2b3c4d5e"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database with the full migration chain applied."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def _connect_generation_column(engine: Engine) -> dict[str, object] | None:
    matches = [
        c for c in sa.inspect(engine).get_columns("hosts") if c["name"] == "connect_generation"
    ]
    assert len(matches) <= 1, matches
    return matches[0] if matches else None


def _migrate(engine: Engine, target: str) -> None:
    config = _build_alembic_config(str(engine.url))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        if target == "head":
            command.upgrade(config, "head")
        else:
            command.downgrade(config, target)


def test_connect_generation_column_present_and_nullable(db_engine: Engine) -> None:
    """The migration adds a nullable BIGINT, including for legacy rows."""
    col = _connect_generation_column(db_engine)
    assert col is not None, "the migration did not add hosts.connect_generation"
    assert col["nullable"], "pre-migration rows have no connect token and must stay readable"
    assert isinstance(col["type"], sa.BigInteger), f"expected BIGINT, got {col['type']!r}"


def test_legacy_rows_keep_null_tokens_and_migration_is_retry_safe(db_engine: Engine) -> None:
    """A host persisted before the column stays NULL; repeated runs are no-ops."""
    _migrate(db_engine, _PARENT_REVISION)
    assert _connect_generation_column(db_engine) is None

    now = now_epoch()
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hosts (workspace_id, host_id, user_id, name, status, created_at, "
                "updated_at) VALUES (0, :host_id, 'legacy@example.com', 'legacy-laptop', "
                ":status, :now, :now)"
            ),
            {
                "host_id": bytes.fromhex("1444b179a19322377dcc75cf7fcd1bd2"),
                "status": encode_host_status("online"),
                "now": now,
            },
        )

    _migrate(db_engine, "head")
    assert _connect_generation_column(db_engine) is not None
    with db_engine.connect() as conn:
        tokens = conn.execute(sa.text("SELECT connect_generation FROM hosts")).scalars().all()
    assert tokens == [None]

    # A retried upgrade (version table behind, column already present) must not fail.
    with db_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE alembic_version SET version_num = :rev"), {"rev": _PARENT_REVISION}
        )
    _migrate(db_engine, "head")
    assert _connect_generation_column(db_engine) is not None

    _migrate(db_engine, _PARENT_REVISION)
    assert _connect_generation_column(db_engine) is None
