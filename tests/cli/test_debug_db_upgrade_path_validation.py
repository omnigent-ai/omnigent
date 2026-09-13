"""Tests for ``omnigent debug db-upgrade`` SQLite path validation.

``db-upgrade`` used to hand its URL straight to ``create_engine`` /
``_run_migrations``. For a file-backed SQLite URL that meant a typo'd path
either crashed with a raw ``sqlite3.OperationalError: unable to open
database file`` traceback (parent directory absent) or silently created a
brand-new empty database and reported "Upgrade complete." (parent present,
file absent). Both journeys must instead fail fast with an actionable
message naming the missing path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from omnigent.cli import _require_existing_sqlite_db, cli


def test_missing_parent_dir_raises_actionable_error(tmp_path: Path) -> None:
    """A path whose parent directory does not exist must name the path."""
    missing = tmp_path / "no-such-dir" / "chat.db"

    with pytest.raises(click.ClickException) as exc_info:
        _require_existing_sqlite_db(f"sqlite:///{missing}")

    msg = str(exc_info.value.message)
    assert str(missing) in msg
    assert "does not exist" in msg


def test_missing_file_in_existing_dir_raises_instead_of_creating(
    tmp_path: Path,
) -> None:
    """A missing file must not be silently created as a fresh database."""
    missing = tmp_path / "chat.db"

    with pytest.raises(click.ClickException, match="does not exist"):
        _require_existing_sqlite_db(f"sqlite:///{missing}")

    assert not missing.exists(), "validation must not create the database file as a side effect"


def test_existing_file_passes_validation(tmp_path: Path) -> None:
    """An existing SQLite file is accepted (upgrade may proceed)."""
    db_path = tmp_path / "chat.db"
    sqlite3.connect(db_path).close()

    _require_existing_sqlite_db(f"sqlite:///{db_path}")


def test_non_sqlite_url_is_not_checked() -> None:
    """Postgres and friends have no local file to validate."""
    _require_existing_sqlite_db("postgresql://user:pw@host/dbname")


def test_in_memory_sqlite_is_not_checked() -> None:
    """In-memory SQLite has no backing file to validate."""
    _require_existing_sqlite_db("sqlite://")
    _require_existing_sqlite_db("sqlite:///:memory:")


def test_malformed_url_raises_click_exception() -> None:
    """A URL SQLAlchemy cannot parse surfaces as a CLI error, not a traceback."""
    with pytest.raises(click.ClickException, match="Invalid database URL"):
        _require_existing_sqlite_db("not a database url")


def test_db_upgrade_command_reports_missing_path(tmp_path: Path) -> None:
    """The full command exits non-zero with the friendly message, no traceback."""
    missing = tmp_path / "typo-dir" / "chat.db"

    result = CliRunner().invoke(cli, ["debug", "db-upgrade", f"sqlite:///{missing}"])

    assert result.exit_code != 0
    assert "does not exist" in result.output
    assert "unable to open database file" not in result.output
