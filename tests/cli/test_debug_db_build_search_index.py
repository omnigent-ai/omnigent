"""Tests for ``omnigent debug db-build-search-index``: refuses non-PostgreSQL
databases and, on PostgreSQL, builds and drops the ``gin_trgm_ops`` index idempotently.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.conversation_store.pg_content_search import CONTENT_SEARCH_INDEX


def _index_definition(uri: str) -> str | None:
    engine = get_or_create_engine(uri)
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": CONTENT_SEARCH_INDEX},
        ).scalar()


def test_refuses_sqlite_without_traceback(db_uri: str) -> None:
    """A non-PostgreSQL URL exits non-zero with an actionable message."""
    if not db_uri.startswith("sqlite"):
        pytest.skip("covers the SQLite refusal")

    result = CliRunner().invoke(cli, ["debug", "db-build-search-index", db_uri])

    assert result.exit_code != 0
    assert "PostgreSQL" in result.output
    assert "Traceback" not in result.output


def test_builds_then_reports_existing_then_drops(db_uri: str) -> None:
    """On PostgreSQL the command creates the index once, is idempotent, and can drop it."""
    if not db_uri.startswith("postgresql"):
        pytest.skip("requires a PostgreSQL test database")
    runner = CliRunner()

    first = runner.invoke(cli, ["debug", "db-build-search-index", db_uri])
    assert first.exit_code == 0, first.output
    assert "Created" in first.output
    assert "OMNIGENT_PG_CONTENT_SEARCH=auto" in first.output
    definition = _index_definition(db_uri)
    assert definition is not None
    assert "gin" in definition.lower() and "gin_trgm_ops" in definition
    assert "lower(" not in definition.lower(), definition

    second = runner.invoke(cli, ["debug", "db-build-search-index", db_uri])
    assert second.exit_code == 0, second.output
    assert "already exists" in second.output

    dropped = runner.invoke(cli, ["debug", "db-build-search-index", "--drop", db_uri])
    assert dropped.exit_code == 0, dropped.output
    assert "Dropped" in dropped.output
    assert _index_definition(db_uri) is None
