"""E2E: Postgres-backed local server with the psycopg driver missing.

Reproduces the missing-Postgres-driver journey: a user points
``OMNIGENT_DATABASE_URI`` at a standalone Postgres database from a base
install (no extras, so no psycopg), runs the daemon-owned ``omnigent
start``, and the local server crashes at boot with a bare
``ModuleNotFoundError: No module named 'psycopg'`` that only lands in
the server log file — the terminal shows a generic "daemon exited"
error with no actionable guidance about the missing driver.

Each test spawns the REAL CLI (``python -m omnigent.cli start``) in an
isolated ``$HOME`` so the daemon, pidfiles, and logs never touch the
developer's ``~/.omnigent``. No Postgres server is needed: the failure
fires at DBAPI import time, before any connection attempt.

These tests require the Postgres driver to be ABSENT from the
environment (the base-install condition of the bug); they skip when
psycopg / psycopg2 is importable (e.g. the databricks extra lane).

Expected behavior (the acceptance criteria) asserted here:

1. A Postgres URI without the driver fails with an actionable terminal
   message naming the missing driver / install command — not only a
   pointer at log directories.  (FAILS on the buggy build.)
2. A bare ``postgresql://`` URI (which SQLAlchemy maps to the psycopg2
   DBAPI) gets dialect-correct guidance too — either normalized to
   ``postgresql+psycopg://`` or an actionable psycopg-naming error.
   (FAILS on the buggy build.)
3. A dedicated ``postgres`` extra exists in packaging so the driver is
   not only reachable via the heavyweight ``databricks`` extra.
   (FAILS on the buggy build.)
4. The failure must never echo the database password, and must not
   print the misleading "run `omnigent setup`" model-credential hint
   (regression guard for the removed unconditional hint).  (Passes.)

Usage::

    .venv/bin/python -m pytest tests/e2e/test_postgres_missing_driver_e2e.py -v
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The whole journey is "daemon spawns, server crashes at import, daemon
# exits, CLI reports" — observed ~4-10s. The CLI's own discovery timeout
# is 120s, so budget past it for a slow CI box.
_CLI_TIMEOUT_S = 180.0

_DB_PASSWORD = "s3cretpw-e2e-missing-driver"  # deliberately fake

# Env that would leak the harness's own config/creds into the CLI under
# test or break the isolated-HOME containment (mirrors
# test_local_server_lifecycle_e2e._ENV_TO_CLEAR).
_ENV_TO_CLEAR = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE",
    "CODEX",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_OIDC_ISSUER",
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
)

_missing = importlib.util.find_spec


def _run_start(home: Path, database_uri: str) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent start`` against a Postgres URI in an isolated HOME.

    :param home: Isolated home dir; ``<home>/.omnigent`` receives the
        daemon pidfiles and the host/server logs.
    :param database_uri: The ``OMNIGENT_DATABASE_URI`` value under test,
        e.g. ``"postgresql+psycopg://u:pw@127.0.0.1:5432/omnigent"``.
    :returns: The completed CLI process with captured stdout/stderr.
    """
    env = dict(os.environ)
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["OMNIGENT_DATABASE_URI"] = database_uri
    return subprocess.run(
        [sys.executable, "-m", "omnigent.cli", "start"],
        env=env,
        cwd=str(home),
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
        check=False,
    )


def _server_log_text(home: Path) -> str:
    """Concatenate every daemon-spawned server log under the isolated HOME.

    :param home: The isolated home dir used by :func:`_run_start`.
    :returns: The combined server log text ("" when no log was written).
    """
    log_dir = home / ".omnigent" / "logs" / "server"
    if not log_dir.is_dir():
        return ""
    return "\n".join(p.read_text(errors="replace") for p in sorted(log_dir.glob("server-*.log")))


def _assert_actionable_driver_error(
    result: subprocess.CompletedProcess[str], server_log: str, dbapi_module: str
) -> None:
    """Assert the acceptance criteria for a missing-driver Postgres boot.

    :param result: The completed ``omnigent start`` process.
    :param server_log: Combined server log text (journey anchor).
    :param dbapi_module: The DBAPI module SQLAlchemy tried to import for
        this URI dialect, e.g. ``"psycopg"`` or ``"psycopg2"``.
    """
    terminal = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"expected `omnigent start` to fail without the Postgres driver, got "
        f"rc={result.returncode}.\n--- terminal ---\n{terminal}"
    )
    # Journey anchor: the boot must have died on the missing Postgres
    # driver (not some unrelated failure), visible in the server log
    # either as the raw ModuleNotFoundError or as a translated error
    # that names the driver.
    assert re.search(rf"(?i)\b{re.escape(dbapi_module)}\b|\bpsycopg\b", server_log), (
        f"server boot did not fail on the missing {dbapi_module!r} driver — "
        f"the reproduction precondition was not met.\n--- server log ---\n"
        f"{server_log[-4000:]}"
    )
    # Credentials must never be echoed — not on the terminal, not in logs.
    assert _DB_PASSWORD not in terminal, (
        f"database password leaked to the terminal.\n--- terminal ---\n{terminal}"
    )
    assert _DB_PASSWORD not in server_log, "database password leaked into the server log"
    # THE BUG: the terminal must carry an actionable message
    # naming the missing Postgres driver and how to install it — today it
    # only prints a generic "daemon exited ... see logs" pointer, and the
    # ModuleNotFoundError is findable only by digging through log files.
    assert re.search(r"(?i)psycopg", terminal), (
        "terminal output never names the missing Postgres driver (psycopg); the "
        "user is left with a generic daemon error and two log directories.\n"
        f"--- terminal ---\n{terminal}"
    )
    assert re.search(r"(?i)install|postgresql\+psycopg://", terminal), (
        "terminal output offers no actionable recovery (an install command such "
        "as `pip install 'omnigent[postgres]'` / `uv tool install omnigent "
        "--with 'psycopg[binary]'`, or a dialect-correct URI suggestion).\n"
        f"--- terminal ---\n{terminal}"
    )


@pytest.mark.skipif(
    _missing("psycopg") is not None,
    reason="requires the base install condition: psycopg absent",
)
def test_missing_psycopg_surfaces_actionable_error_on_terminal(tmp_path: Path) -> None:
    """`omnigent start` + psycopg3 Postgres URI without the driver.

    The daemon-owned local server crashes at boot on ``import psycopg``;
    the terminal must surface an actionable driver-install message, not
    only the generic "daemon exited before its Omnigent server became
    ready" pointer at two log directories.
    """
    home = tmp_path / "home"
    home.mkdir()
    uri = f"postgresql+psycopg://omnigent:{_DB_PASSWORD}@127.0.0.1:5432/omnigent"
    result = _run_start(home, uri)
    _assert_actionable_driver_error(result, _server_log_text(home), "psycopg")


@pytest.mark.skipif(
    _missing("psycopg") is not None or _missing("psycopg2") is not None,
    reason="requires the base install condition: psycopg/psycopg2 absent",
)
def test_bare_postgresql_uri_gets_dialect_correct_guidance(tmp_path: Path) -> None:
    """`omnigent start` + bare ``postgresql://`` URI without any driver.

    A bare ``postgresql://`` URI makes SQLAlchemy select the *psycopg2*
    DBAPI. ``normalize_database_url`` exists but is only applied in the
    Docker entrypoint, so the local-server path dies on ``No module
    named 'psycopg2'`` — and installing psycopg3 (the packaged driver)
    would not even fix it. The core path must either normalize the URI
    to ``postgresql+psycopg://`` or give dialect-correct guidance.
    """
    home = tmp_path / "home"
    home.mkdir()
    uri = f"postgresql://omnigent:{_DB_PASSWORD}@127.0.0.1:5432/omnigent"
    result = _run_start(home, uri)
    _assert_actionable_driver_error(result, _server_log_text(home), "psycopg2")


def test_postgres_extra_is_packaged() -> None:
    """A dedicated ``postgres`` extra must exist in packaging.

    Standalone Postgres users must not have to discover that the driver
    hides inside the ``databricks`` extra (which also drags in
    databricks-sdk, databricks-mcp, opentelemetry-distro, pymysql).
    """
    import tomllib

    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    assert "postgres" in extras, (
        "no `postgres` extra in pyproject.toml — the Postgres driver is only "
        f"reachable via the heavyweight extras: {sorted(extras)}"
    )
    joined = " ".join(extras["postgres"]).lower()
    assert "psycopg" in joined or "omnigent[postgres]" in joined, (
        f"the `postgres` extra does not provide the psycopg driver: {extras['postgres']}"
    )


@pytest.mark.skipif(
    _missing("psycopg") is not None,
    reason="requires the base install condition: psycopg absent",
)
def test_no_misleading_setup_hint_on_driver_failure(tmp_path: Path) -> None:
    """The missing-driver failure must not suggest ``omnigent setup``.

    Regression guard for the reported misleading recovery hint: the
    setup wizard configures model credentials and cannot install a
    missing Python package, so it must never be offered for this error
    class. (The unconditional hint was removed on main; this pins it.)
    """
    home = tmp_path / "home"
    home.mkdir()
    uri = f"postgresql+psycopg://omnigent:{_DB_PASSWORD}@127.0.0.1:5432/omnigent"
    result = _run_start(home, uri)
    terminal = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"expected `omnigent start` to fail without the Postgres driver, got "
        f"rc={result.returncode}.\n--- terminal ---\n{terminal}"
    )
    assert not re.search(r"(?i)run\s+`?(omnigent|omni)\s+setup`?", terminal), (
        "the misleading `omnigent setup` model-credential hint is back on a "
        f"missing-dependency failure.\n--- terminal ---\n{terminal}"
    )
