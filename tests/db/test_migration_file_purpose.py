"""Tests for the file-purpose migration (``a8c4e1f6b2d9``)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_PRIOR_REVISION = "d5e9f1a2b3c4"
_THIS_REVISION = "a8c4e1f6b2d9"
_INDEX = "ix_files_session_id_created_at"


def _migrate(engine: sa.Engine, uri: str, revision: str, *, downgrade: bool = False) -> None:
    """Run one migration target on a caller-owned SQLite connection."""
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def _index_columns(engine: sa.Engine) -> list[str]:
    """Return the ordered columns in the session-file seek index."""
    indexes = {index["name"]: index for index in sa.inspect(engine).get_indexes("files")}
    return [column for column in indexes[_INDEX]["column_names"] if column is not None]


def test_file_purpose_backfill_index_and_downgrade(tmp_path: Path) -> None:
    """Existing files become uploads and downgrade restores the prior shape."""
    uri = f"sqlite:///{tmp_path / 'file-purpose.db'}"
    engine = sa.create_engine(uri)
    file_id = bytes.fromhex("c9b7bd37959cc093d2b9e9ebf4d9b35b")
    session_id = bytes.fromhex("79b22ebd2309e48fdeb450c65611d51b")
    try:
        _migrate(engine, uri, _PRIOR_REVISION)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO files "
                    "(workspace_id, id, created_at, filename, bytes, content_type, session_id) "
                    "VALUES (0, :id, 1, 'legacy.png', 24, 'image/png', :session_id)"
                ),
                {"id": file_id, "session_id": session_id},
            )

        _migrate(engine, uri, _THIS_REVISION)

        columns = {column["name"]: column for column in sa.inspect(engine).get_columns("files")}
        assert columns["purpose"]["nullable"] is False
        with engine.connect() as connection:
            purpose = connection.execute(
                sa.text("SELECT purpose FROM files WHERE id = :id"),
                {"id": file_id},
            ).scalar_one()
        assert purpose == "user_upload"
        assert _index_columns(engine) == [
            "workspace_id",
            "session_id",
            "purpose",
            "created_at",
            "id",
        ]

        _migrate(engine, uri, _PRIOR_REVISION, downgrade=True)

        downgraded_columns = {column["name"] for column in sa.inspect(engine).get_columns("files")}
        assert "purpose" not in downgraded_columns
        assert _index_columns(engine) == [
            "workspace_id",
            "session_id",
            "created_at",
            "id",
        ]
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT filename FROM files WHERE id = :id"),
                    {"id": file_id},
                ).scalar_one()
                == "legacy.png"
            )
    finally:
        engine.dispose()
