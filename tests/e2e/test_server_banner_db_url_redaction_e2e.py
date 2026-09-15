"""E2E: the server startup banner must not print database credentials.

An operator who points ``omnigent server`` at a remote database passes a
credential-bearing SQLAlchemy URL (``...://user:PASSWORD@host/db``) via
``--database-uri`` / ``DATABASE_URL``. The startup banner echoes that URL to
stdout, which in a container deployment lands verbatim in ``docker logs`` and
every log aggregator downstream — so the database password must be masked
there.

This test drives the REAL user journey: it spawns the actual
``omnigent server`` CLI subprocess with a password-bearing database URI,
captures its stdout (exactly what ``docker logs`` would show), and asserts
the password does not appear in it.

The standard CI lanes have no Postgres server or psycopg driver, so the
credential-bearing URI uses the ``cloudflare_d1`` dialect (a test
dependency): a remote-DB dialect whose URL carries ``user:password@host``
credentials just like Postgres, served here by a tiny in-process HTTP
emulator backed by a pre-migrated SQLite file. The banner echoes the raw URI
string regardless of dialect, so the leak reproduces identically.

Run::

    .venv/bin/python -m pytest tests/e2e/test_server_banner_db_url_redaction_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A cold `omnigent server` imports the whole stack before it prints the
# banner; reuse the suite-wide boot budget.
_BANNER_TIMEOUT_S = HEALTH_TIMEOUT_S * 2

# The secret that must never reach stdout. Distinctive so a substring match
# cannot false-positive on anything else the server prints.
_DB_PASSWORD = "s3cretpw-hunter2"

# Ambient config/credential vars would leak the harness's own setup into the
# server under test or break HOME isolation; clearing whole prefixes keeps the
# subprocess hermetic even for vars this file doesn't know about.
_ENV_PREFIXES_TO_CLEAR = ("DATABRICKS_", "ANTHROPIC_", "OPENAI_", "OMNIGENT_")

_TXN_KEYWORDS = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}


def _free_port() -> int:
    """Bind port 0 and return the OS-assigned free TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_d1_emulator(db_path: Path) -> ThreadingHTTPServer:
    """Serve the Cloudflare D1 ``/raw`` REST endpoint from a local SQLite file.

    The ``cloudflare_d1`` SQLAlchemy dialect POSTs every statement to
    ``{base}/raw``; this handler executes each one on a single autocommit
    SQLite connection (matching D1 semantics) so a real server process can
    boot against a ``cloudflare_d1://user:password@db`` URI with no external
    service.

    :param db_path: The SQLite file backing the emulated database.
    :returns: An unstarted :class:`ThreadingHTTPServer` bound to a free
        loopback port.
    """
    backing = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            sql = body.get("sql", "")
            params = body.get("params") or []
            head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
            try:
                with lock:
                    if head in _TXN_KEYWORDS:  # D1 auto-commits; ignore txn control
                        columns: list[str] = []
                        rows: list[list[object]] = []
                    else:
                        cur = backing.execute(sql, params)
                        if cur.description:
                            columns = [d[0] for d in cur.description]
                            rows = [list(r) for r in cur.fetchall()]
                        else:
                            columns, rows = [], []
                payload = {
                    "success": True,
                    "result": [
                        {
                            "results": {"columns": columns, "rows": rows},
                            "meta": {},
                            "success": True,
                        }
                    ],
                }
                code = 200
            except Exception as exc:  # surface SQL errors in the D1 error envelope
                payload = {"success": False, "errors": [{"message": str(exc)}]}
                code = 400
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:  # quiet the test output
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _write_alembic_d1_shim(shim_dir: Path) -> None:
    """Register a ``cloudflare_d1`` Alembic impl in the server subprocess.

    Alembic resolves a DDL impl by dialect name and has none for
    ``cloudflare_d1``; since D1 is SQLite over REST, the SQLite impl is
    correct. Written as a ``sitecustomize`` module so prepending
    ``shim_dir`` to ``PYTHONPATH`` applies it to the spawned server without
    touching its code.

    :param shim_dir: Directory to hold the ``sitecustomize.py`` module.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    (shim_dir / "sitecustomize.py").write_text(
        "try:\n"
        "    from alembic.ddl.sqlite import SQLiteImpl\n"
        "\n"
        "    class _CloudflareD1Impl(SQLiteImpl):\n"
        '        __dialect__ = "cloudflare_d1"\n'
        "except Exception:\n"
        "    pass\n"
    )


def _server_env(home: Path, shim_dir: Path, d1_port: int) -> dict[str, str]:
    """Build the isolated environment for the ``omnigent server`` subprocess.

    :param home: Isolated ``$HOME`` so the pidfile / logs never touch the
        real ``~/.omnigent``.
    :param shim_dir: Directory containing the Alembic dialect shim.
    :param d1_port: Loopback port of the in-process D1 emulator.
    :returns: The environment mapping for :class:`subprocess.Popen`.
    """
    env = os.environ.copy()
    for key in [k for k in env if k.startswith(_ENV_PREFIXES_TO_CLEAR)]:
        env.pop(key)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = (
        f"{shim_dir}{os.pathsep}{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    env["CF_D1_BASE_URL"] = f"http://127.0.0.1:{d1_port}"
    # The dialect reaches the loopback emulator over HTTP; keep any ambient
    # corporate proxy out of that hop.
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


@pytest.mark.timeout(240)
def test_server_startup_banner_does_not_print_db_password(tmp_path: Path) -> None:
    """The startup banner must mask the database password, not echo it.

    Journey: an operator starts ``omnigent server`` against a remote database
    whose URL carries ``user:password@host`` credentials, then reads the
    first lines of the process log (``docker logs`` in a container deploy).
    The ``database:`` banner line must not contain the password.
    """
    # The backing DB is pre-migrated in-process so the spawned server's
    # schema check is a fast no-op (running full migrations through the REST
    # emulator is slow and irrelevant to the banner under test).
    backing_db = tmp_path / "d1.db"
    from omnigent.db.utils import get_or_create_engine

    get_or_create_engine(f"sqlite:///{backing_db}")

    emulator = _make_d1_emulator(backing_db)
    d1_port = emulator.server_address[1]
    emulator_thread = threading.Thread(target=emulator.serve_forever, daemon=True)
    emulator_thread.start()

    shim_dir = tmp_path / "shim"
    _write_alembic_d1_shim(shim_dir)
    home = tmp_path / "home"
    home.mkdir()

    db_uri = f"cloudflare_d1://omnigent:{_DB_PASSWORD}@omnigentdb"
    server_port = _free_port()
    log_path = tmp_path / "server-stdout.log"

    proc: subprocess.Popen[bytes] | None = None
    try:
        with log_path.open("wb") as log_file:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnigent.cli",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(server_port),
                    "--database-uri",
                    db_uri,
                    "--artifact-location",
                    str(tmp_path / "artifacts"),
                    "--no-open",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=_server_env(home, shim_dir, d1_port),
                cwd=str(_REPO_ROOT),
            )

            # Wait for the banner (printed once the app is built, before
            # uvicorn binds) — this is the moment the leak happens.
            deadline = time.monotonic() + _BANNER_TIMEOUT_S
            while time.monotonic() < deadline:
                output = log_path.read_text(errors="replace")
                if "Starting omnigent server" in output:
                    break
                if proc.poll() is not None:
                    pytest.fail(
                        "omnigent server exited before printing its startup banner:\n"
                        + output[-4000:]
                    )
                time.sleep(POLL_INTERVAL_S)
            else:
                pytest.fail(
                    "omnigent server did not print its startup banner within "
                    f"{_BANNER_TIMEOUT_S:.0f}s:\n" + log_path.read_text(errors="replace")[-4000:]
                )

            # Give the remaining banner lines (database/artifacts/log) a
            # moment to flush after the first line appears.
            banner_deadline = time.monotonic() + 10.0
            while time.monotonic() < banner_deadline:
                output = log_path.read_text(errors="replace")
                if "database:" in output:
                    break
                time.sleep(POLL_INTERVAL_S)

        output = log_path.read_text(errors="replace")

        # Guard against a vacuous pass: the banner and its database line must
        # actually have been printed for the redaction assertion to mean
        # anything.
        assert "Starting omnigent server" in output
        assert "database:" in output, f"banner has no database line:\n{output[-4000:]}"

        # The actual bug: the raw password appears verbatim in stdout — the
        # exact text a container ships to `docker logs` and log aggregators.
        assert _DB_PASSWORD not in output, (
            "database password leaked into the server startup banner "
            "(stdout would land in `docker logs`):\n"
            + "\n".join(line for line in output.splitlines() if _DB_PASSWORD in line)
        )
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        emulator.shutdown()
        emulator.server_close()
        emulator_thread.join(timeout=5)
