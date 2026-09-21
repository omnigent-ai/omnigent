"""Database tests retain the cold schema initialization path."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from omnigent.db import utils


def test_default_database_fixture_runs_migrations(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    _worker_db_uri: str,
    _sqlite_db_template: Path | None,
) -> None:
    assert _sqlite_db_template is None
    if _worker_db_uri:
        assert request.getfixturevalue("db_uri") == _worker_db_uri
        return

    migrated = []
    original = utils._run_migrations

    def record_migration(engine: Engine, uri: str) -> None:
        migrated.append(uri)
        original(engine, uri)

    monkeypatch.setattr(utils, "_run_migrations", record_migration)
    uri = request.getfixturevalue("db_uri")
    assert migrated == [uri]
