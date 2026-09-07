"""E2E reproduction: DB-schema mismatch journeys must end in actionable errors.

A user whose ``chat.db`` was stamped by a newer Omnigent build hit two
failures in a row:

1. ``omnigent server`` misreported the newer-build database as "schema is
   out of date" and crashed attempting an automatic migration. This was
   fixed by the newer-schema guard ("reject schemas from newer builds"),
   which now stops with a clear "newer than this version of Omnigent"
   message; ``test_server_rejects_newer_build_database_with_clear_message``
   guards that behavior.

2. Following the error message's advice, the user ran
   ``omnigent debug db-upgrade`` but typo'd the database path
   (``~/omnigent/chat.db`` instead of ``~/.omnigent/chat.db``). SQLite
   cannot create a file inside a nonexistent directory, so the command
   crashed with a raw ``sqlite3.OperationalError: unable to open database
   file`` traceback that never mentions the path does not exist.
   ``test_db_upgrade_missing_db_path_reports_actionable_error`` reproduces
   that journey and FAILS until db-upgrade validates the SQLite path and
   prints an actionable missing-file message.

Both tests drive the real CLI as a subprocess (the same commands the user
ran); no live server or LLM is required.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Generous cap for one CLI subprocess (imports alone take a few seconds).
_CLI_TIMEOUT_S = 120

# An Alembic revision id that no build's migration chain will ever contain —
# stands in for a database stamped by a build newer than this one.
_UNKNOWN_REVISION = "ffffff999999"


def _cli_env(tmp_path: Path) -> dict[str, str]:
    """Environment for a spawned ``omnigent`` CLI, isolated from the host.

    Points config/data dirs at the test's tmp dir so the subprocess never
    reads or writes the developer's real ``~/.omnigent``, and guarantees the
    subprocess imports the same ``omnigent`` package the test process runs.

    :param tmp_path: The test's tmp directory.
    :returns: Env mapping for :func:`subprocess.run`.
    """
    import omnigent

    repo_root = Path(omnigent.__file__).resolve().parent.parent
    env = os.environ.copy()
    env["OMNIGENT_CONFIG_HOME"] = str(tmp_path / "config-home")
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "data-dir")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing}" if existing else str(repo_root)
    return env


def _run_cli(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent <args>`` exactly as a user would, capturing output.

    Uses ``python -m omnigent.cli`` so the observable console behavior is
    deterministic (no interactive crash prompt in a non-TTY test run).

    :param args: CLI arguments after the ``omnigent`` program name.
    :param tmp_path: The test's tmp directory (for env isolation).
    :returns: The completed process with captured stdout/stderr.
    """
    return subprocess.run(
        [sys.executable, "-m", "omnigent.cli", *args],
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
        env=_cli_env(tmp_path),
    )


def _seed_newer_build_db(db_path: Path) -> None:
    """Create a SQLite DB stamped with a revision unknown to this build.

    Mirrors what a newer Omnigent build leaves behind: an
    ``alembic_version`` table pointing at a migration this build's chain
    does not contain.

    :param db_path: Filesystem path of the SQLite file to create.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        conn.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (_UNKNOWN_REVISION,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.timeout(300)
def test_db_upgrade_missing_db_path_reports_actionable_error(
    tmp_path: Path,
) -> None:
    """``debug db-upgrade`` on a nonexistent SQLite path must say so.

    Journey (crash-report trace 2): the user follows the server's advice to
    run ``omnigent debug db-upgrade`` but points it at a path that does not
    exist (a typo'd directory). Today the command dies with a raw
    ``sqlite3.OperationalError: unable to open database file`` traceback
    that never tells the user the real problem — the database file (or its
    parent directory) is missing.

    FAILS until db-upgrade validates the SQLite path up front and prints an
    actionable message naming the missing file instead of the raw traceback.
    """
    missing_db = tmp_path / "no-such-dir" / "chat.db"
    assert not missing_db.parent.exists()

    result = _run_cli(["debug", "db-upgrade", f"sqlite:///{missing_db}"], tmp_path)
    combined = result.stdout + result.stderr

    # The command must still fail — a typo'd path is not upgradable.
    assert result.returncode != 0, (
        f"db-upgrade against a nonexistent path must fail; "
        f"got returncode 0 with output:\n{combined}"
    )

    # The failure must be presented as an actionable missing-file message,
    # not the raw DBAPI error that reproduces the reported crash.
    assert re.search(r"does not exist|doesn't exist|no such file|not found", combined, re.I), (
        "db-upgrade must tell the user the database file/path does not "
        f"exist; instead it printed:\n{combined}"
    )
    assert "unable to open database file" not in combined, (
        "db-upgrade leaked the raw sqlite3.OperationalError instead of an "
        f"actionable missing-file message:\n{combined}"
    )
    assert "Traceback (most recent call last)" not in combined, (
        f"db-upgrade crashed with a raw traceback instead of a friendly error message:\n{combined}"
    )


@pytest.mark.timeout(300)
def test_server_rejects_newer_build_database_with_clear_message(
    tmp_path: Path,
) -> None:
    """``omnigent server`` must reject a newer-build DB with clear guidance.

    Journey (crash-report trace 1): the user starts Omnigent locally against
    a ``chat.db`` written by a newer build. The server must not misreport it
    as "schema is out of date" and attempt (and fail) an automatic
    migration; it must state the database is newer than this build and tell
    the user to upgrade Omnigent. Regression guard for the newer-schema
    reject fix.
    """
    db_path = tmp_path / "chat.db"
    _seed_newer_build_db(db_path)

    result = _run_cli(
        [
            "server",
            "--database-uri",
            f"sqlite:///{db_path}",
            "--port",
            "45799",
        ],
        tmp_path,
    )
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        f"server must refuse to run against a newer-build database; "
        f"got returncode 0 with output:\n{combined}"
    )
    assert "newer than this version" in combined, (
        "server must report the database as newer than this build; "
        f"instead it printed:\n{combined}"
    )
    assert "schema is out of date" not in combined, (
        "server misreported a newer-build database as out of date "
        f"(the originally reported symptom):\n{combined}"
    )
