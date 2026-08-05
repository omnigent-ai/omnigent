"""Tests for the projects schema changes in d5e6f7a8b9c0 and e6f7a8b9c0d1.

d5e6f7a8b9c0 renames ``projects.owner_user_id`` to ``user_id`` behind
``ix_projects_user_id`` and drops the ``ix_projects_name`` UNIQUE index (name
uniqueness moves wholly into the store). e6f7a8b9c0d1 then converts
``projects.config`` from ``TEXT`` to a compressed binary column.

Both downgrades are exercised for round-trip data integrity.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

# One step below d5e6f7a8b9c0 — where a full downgrade of both revisions lands.
_PREVIOUS_HEAD = "c4d5e6f7a8b9"

# One step below e6f7a8b9c0d1 — i.e. d5e6f7a8b9c0, config still ``TEXT``.
_BEFORE_COMPRESSED_CONFIG = "d5e6f7a8b9c0"

_PROJECT_ID = "beefbeefbeefbeefbeefbeefbeefbeef"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database at head."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_projects_user_id_column_at_head(db_engine: Engine) -> None:
    """After upgrade projects exposes ``user_id``, not ``owner_user_id``."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("projects")}
    assert "user_id" in cols, f"Expected projects.user_id; found {cols}"
    assert "owner_user_id" not in cols, f"projects.owner_user_id should be gone; found {cols}"


def test_projects_indexes_at_head(db_engine: Engine) -> None:
    """The owner-scope index is renamed; the UNIQUE name index is gone."""
    indexes = {i["name"]: i for i in sa.inspect(db_engine).get_indexes("projects")}
    assert "ix_projects_user_id" in indexes, f"Expected ix_projects_user_id; found {set(indexes)}"
    assert "ix_projects_owner_user_id" not in indexes, (
        f"ix_projects_owner_user_id should be gone; found {set(indexes)}"
    )
    # created_at trails the equality columns so "list my projects" sorts from
    # the index; id completes the key.
    assert indexes["ix_projects_user_id"]["column_names"] == [
        "workspace_id",
        "user_id",
        "created_at",
        "id",
    ]
    # Per-owner name uniqueness moved wholly into the store (_name_taken), so no
    # unique index remains — leaving this the table's only secondary index.
    assert "ix_projects_name" not in indexes, (
        f"ix_projects_name should be dropped; found {set(indexes)}"
    )
    assert set(indexes) == {"ix_projects_user_id"}, f"Unexpected extra indexes: {set(indexes)}"


def test_downgrade_restores_owner_user_id(tmp_path: Path) -> None:
    """Downgrade one step restores ``owner_user_id`` with data intact.

    Insert a project at head (``user_id``), downgrade (which renames the column
    back), and confirm the old column/index names are restored and the identity
    value survived. A final re-upgrade proves the rename is replayable.
    """
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO projects (workspace_id, id, name, user_id, created_at, updated_at) "
                f"VALUES (0, X'{_PROJECT_ID}', 'launch', 'alice@example.com', 1700000000, NULL)"
            )
        )
        conn.commit()

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    inspector = sa.inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("projects")}
    assert "owner_user_id" in cols and "user_id" not in cols, (
        f"projects.owner_user_id must be restored and user_id gone; found {cols}"
    )
    indexes = {i["name"]: i for i in inspector.get_indexes("projects")}
    assert "ix_projects_owner_user_id" in indexes, (
        f"ix_projects_owner_user_id must be restored; found {set(indexes)}"
    )
    assert "ix_projects_user_id" not in indexes
    # The downgrade also restores the UNIQUE name index it dropped.
    assert indexes["ix_projects_name"]["unique"], "ix_projects_name must be restored as UNIQUE"
    assert indexes["ix_projects_name"]["column_names"] == [
        "workspace_id",
        "owner_user_id",
        "name",
    ]

    with engine.connect() as conn:
        owner = conn.execute(
            sa.text(f"SELECT owner_user_id FROM projects WHERE id = X'{_PROJECT_ID}'")
        ).scalar_one_or_none()
    assert owner == "alice@example.com", f"owner value must survive downgrade; got {owner!r}"

    # Re-upgrade to head: user_id is back and the value survives both hops.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("projects")}
    assert "user_id" in cols and "owner_user_id" not in cols, (
        f"projects.user_id must be restored on re-upgrade; found {cols}"
    )
    with engine.connect() as conn:
        user_id = conn.execute(
            sa.text(f"SELECT user_id FROM projects WHERE id = X'{_PROJECT_ID}'")
        ).scalar_one_or_none()
    assert user_id == "alice@example.com", (
        f"user_id value must survive the full round-trip; got {user_id!r}"
    )

    engine.dispose()
    clear_engine_cache()


def test_config_is_binary_at_head(db_engine: Engine) -> None:
    """``projects.config`` is a binary column at head, not ``TEXT``.

    e6f7a8b9c0d1 converts it so the value is stored zstd-compressed by
    ``omnigent.db.compression``, whose framed bytes can contain NUL and would be
    rejected by a ``TEXT`` column on PostgreSQL.
    """
    types = {c["name"]: c["type"] for c in sa.inspect(db_engine).get_columns("projects")}
    assert isinstance(types["config"], sa.LargeBinary), (
        f"projects.config should be binary at head, got {types['config']!r}"
    )


def test_config_round_trips_through_the_compressed_column(db_engine: Engine) -> None:
    """A framed value written at head decodes back to its plaintext.

    Writes through the ORM type (which compresses) and reads back through it,
    so the column's codec is exercised end to end rather than assumed.
    """
    from omnigent.db.db_models import SqlProject

    # Long enough to exceed the codec's compress threshold, so the stored bytes
    # are a real zstd frame rather than the raw passthrough.
    payload = '{"harness": "claude", "model": "opus", "note": "' + ("x" * 512) + '"}'

    with db_engine.begin() as conn:
        conn.execute(
            sa.insert(SqlProject.__table__).values(
                workspace_id=0,
                id=_PROJECT_ID,
                name="launch",
                user_id="alice@example.com",
                created_at=1700000000,
                updated_at=None,
                config=payload,
            )
        )

    with db_engine.connect() as conn:
        # Through the typed column: decompressed back to the original str.
        decoded = conn.execute(
            sa.select(SqlProject.__table__.c.config).where(
                SqlProject.__table__.c.id == _PROJECT_ID
            )
        ).scalar_one()
        assert decoded == payload, "config must round-trip through the compressed column"

        # Raw bytes: framed (leading NUL sentinel) and smaller than the input,
        # proving compression actually happened on disk.
        raw = conn.execute(
            sa.text(f"SELECT config FROM projects WHERE id = X'{_PROJECT_ID}'")
        ).scalar_one()
    stored = raw.tobytes() if isinstance(raw, memoryview) else raw
    assert isinstance(stored, bytes), f"expected bytes on disk, got {type(stored).__name__}"
    assert stored[0] == 0x00, "stored value should carry the compression frame sentinel"
    assert len(stored) < len(payload.encode("utf-8")), "payload should be compressed on disk"


def test_downgrade_restores_plaintext_config(tmp_path: Path) -> None:
    """Downgrading e6f7a8b9c0d1 decompresses config back to readable ``TEXT``.

    The downgrade must rewrite each row before flipping the type, or the value
    would be left as unreadable compressed bytes in a text column.
    """
    uri = f"sqlite:///{tmp_path / 'config_downgrade.db'}"
    engine = get_or_create_engine(uri)
    payload = '{"harness": "codex", "pad": "' + ("y" * 512) + '"}'

    from omnigent.db.db_models import SqlProject

    with engine.begin() as conn:
        conn.execute(
            sa.insert(SqlProject.__table__).values(
                workspace_id=0,
                id=_PROJECT_ID,
                name="launch",
                user_id="alice@example.com",
                created_at=1700000000,
                updated_at=None,
                config=payload,
            )
        )

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _BEFORE_COMPRESSED_CONFIG)

    # Read with untyped SQL so no codec is applied: the value must already be
    # plaintext on disk.
    with engine.connect() as conn:
        stored = conn.execute(
            sa.text(f"SELECT config FROM projects WHERE id = X'{_PROJECT_ID}'")
        ).scalar_one()
    plain = stored.decode("utf-8") if isinstance(stored, bytes) else stored
    assert plain == payload, f"config must be plaintext after downgrade; got {plain!r:.80}"

    # Re-upgrade: the value survives both hops.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    with engine.connect() as conn:
        decoded = conn.execute(
            sa.select(SqlProject.__table__.c.config).where(
                SqlProject.__table__.c.id == _PROJECT_ID
            )
        ).scalar_one()
    assert decoded == payload, "config must survive the full down/up round-trip"

    engine.dispose()
    clear_engine_cache()
