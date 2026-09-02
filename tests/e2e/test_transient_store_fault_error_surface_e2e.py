"""
End-to-end regression: transient store-availability
faults (e.g. an estore/Lakebase rate limit) must not surface to API
clients as the generic ``500 internal_error`` catch-all, and must not
be logged as full internal stack traces.

The reproduction runs a REAL ``omnigent server`` subprocess against a
REAL SQLite database, then holds an EXCLUSIVE lock on that database
from the test process. Every server write now blocks on the store's
20s ``busy_timeout`` and fails with a *transient, retryable*
``sqlalchemy.exc.OperationalError`` ("database is locked") — the exact
error class a rate-limited / suspended hosted store raises. Today the
server's catch-all ``@app.exception_handler(Exception)`` in
``omnigent/server/app.py`` converts that into::

    500 {"error": {"code": "internal_error",
                   "message": "An internal error occurred."}}

with a full stack trace logged at ERROR level — indistinguishable from
a genuine server defect, and giving the client no retryable signal.

The desired (post-fix) behavior this test asserts: a transient
store-availability fault is answered with a *retryable* error — a
status other than the blanket 500, or at minimum an error code more
specific than ``internal_error`` — and the expected/transient failure
is not logged as a raw internal stack trace.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The store applies PRAGMA busy_timeout=20000 (omnigent/db/utils.py), so a
# write against the locked DB fails after ~20s; budget generously past it.
_LOCKED_REQUEST_TIMEOUT_S = 120.0


def _find_free_port() -> int:
    """Pick a free TCP port for the server subprocess to bind."""
    s = socket.socket()
    s.bind(("", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def ap_server_with_lockable_db() -> Iterator[tuple[str, Path, Path]]:
    """
    Start a real Omnigent server subprocess on a private SQLite DB.

    :yields: ``(base_url, db_path, server_log_path)`` — the server URL,
        the SQLite file the test can lock to simulate a transiently
        unavailable store, and the captured server log.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmp_root = Path(tempfile.mkdtemp(prefix="store-fault-e2e-"))
    db_path = tmp_root / "store-fault.db"
    artifact_dir = tmp_root / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_root / "server.log"

    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        # The compaction layer constructs an LLM client at startup; a
        # stub satisfies the env check (no LLM call is made here).
        "OPENAI_API_KEY": "stub-not-used",
    }
    for var in ("DATABRICKS_TOKEN", "ANTHROPIC_API_KEY", "CODEX", "CLAUDE_CODE"):
        env.pop(var, None)
    # Ambient OMNIGENT_* runner/host vars (present when the test itself runs
    # inside an omnigent-managed environment) would redirect the spawned
    # server's logging and data dirs; the subprocess must see none of them.
    for var in [k for k in env if k == "OMNIGENT" or k.startswith("OMNIGENT_")]:
        env.pop(var, None)
    # Keep the server's own data dir (DBs/logs) inside the test tmp root so
    # the run never depends on a writable $HOME.
    env["OMNIGENT_DATA_DIR"] = str(tmp_root / "data")

    log_handle = open(log_path, "w")  # noqa: SIM115 — subprocess holds the FD
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2.0, trust_env=False)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            log_handle.close()
            raise RuntimeError(
                f"omnigent server failed to start. log: {log_path}\n"
                f"{log_path.read_text(errors='replace')[-4000:]}"
            )
        yield (base_url, db_path, log_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


@pytest.mark.timeout(240)
def test_transient_store_fault_is_not_generic_500_with_stack_trace(
    ap_server_with_lockable_db: tuple[str, Path, Path],
) -> None:
    """
    With the store transiently unavailable (EXCLUSIVE-locked SQLite,
    the local analog of an estore rate limit), a write API call must
    not be answered with the blanket ``500 internal_error`` catch-all,
    and the transient fault must not be logged as a raw stack trace.

    Today this fails: the catch-all exception handler in
    ``omnigent/server/app.py`` answers ``500`` with code
    ``internal_error`` and logs the full ``OperationalError``
    traceback. After the fix, a transient/retryable store fault should
    map to a retryable, specifically-coded error (e.g. 503/429 with a
    ``store_unavailable``-style code) without an internal stack trace.
    """
    base_url, db_path, log_path = ap_server_with_lockable_db
    client = httpx.Client(base_url=base_url, trust_env=False)

    # Baseline: the write path works before the fault is injected.
    baseline = client.post("/v1/projects", json={"name": "store-fault-baseline"}, timeout=30.0)
    assert baseline.status_code == 200, baseline.text

    # Inject the fault: hold an EXCLUSIVE lock so every server write
    # blocks on busy_timeout and fails with a transient
    # OperationalError ("database is locked") — the same retryable
    # error class a rate-limited hosted store raises.
    locker = sqlite3.connect(str(db_path), timeout=1)
    locker.isolation_level = None
    locker.execute("BEGIN EXCLUSIVE")
    try:
        resp = client.post(
            "/v1/projects",
            json={"name": "store-fault-under-fault"},
            timeout=_LOCKED_REQUEST_TIMEOUT_S,
        )
    finally:
        locker.execute("ROLLBACK")
        locker.close()

    # It must still be an error — the write genuinely could not land.
    assert resp.status_code >= 400, (
        f"expected an error while the store was unavailable, got "
        f"{resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    error = body.get("error", {})

    # No stack trace may leak into the response body.
    assert "Traceback" not in resp.text, resp.text[:500]

    # Facet 1: the transient store fault must NOT be the
    # generic catch-all 500 internal_error — the client needs a
    # retryable, specifically-coded signal.
    assert (resp.status_code, error.get("code")) != (500, "internal_error"), (
        "transient store-availability fault (retryable OperationalError) "
        "was surfaced as the generic 500 internal_error catch-all: "
        f"{resp.status_code} {resp.text[:300]}"
    )

    # Facet 2: the expected/transient fault must not be
    # logged as a raw internal stack trace (that is how a genuine
    # server defect is logged; a rate-limit-shaped fault should be a
    # concise, categorized log line).
    log_text = log_path.read_text(errors="replace")
    assert "Traceback (most recent call last)" not in log_text, (
        "transient store-availability fault was logged as a full internal stack trace"
    )

    # The fault is transient: once the store is available again the
    # same write succeeds.
    after = client.post("/v1/projects", json={"name": "store-fault-after"}, timeout=30.0)
    assert after.status_code == 200, after.text
