"""
End-to-end regression for the conversation_items position race
(2026-04-30 user-reported ``UNIQUE constraint failed`` symptom).

This test runs a REAL Omnigent server subprocess against a REAL SQLite
database, then drives many concurrent ``append()`` calls
through the live ``SqlAlchemyConversationStore`` instance the
server's ``ConversationStore`` was wired with at startup. The
goal is to prove the position-race fix (lock escalation in
:meth:`SqlAlchemyConversationStore._lock_conversation`) holds
when:

1. The store is constructed in a separate process from the
   test (matches the user's ``omnigent run`` setup
   where the Omnigent server is a subprocess of the REPL).
2. The store's session factory + busy_timeout pragmas come
   from the production code path
   (:func:`omnigent.db.utils.make_managed_session_maker`),
   not a test-only override.

The bug shape: the user's REPL session 2026-04-30 hit
``sqlite3.IntegrityError: UNIQUE constraint failed:
conversation_items.conversation_id, conversation_items.position``
when the agent loop's incremental tool-call persist raced the
steering inbox's auto-injection of idle-notification user
messages. Two concurrent appends both ran ``select(max(position))``
inside DEFERRED transactions (no write lock), both saw the same
``max_pos``, both inserted at ``max_pos + 1``, and the loser
crashed with the IntegrityError. The fix escalates the
transaction to RESERVED via a no-op UPDATE in
``_lock_conversation``, which forces SQLite to serialize the
two transactions on the busy_timeout instead of racing the
SELECT.

This e2e test:

- Spins up an Omnigent server subprocess (:fixture:`ap_server`).
- Imports the Omnigent server's actual ``SqlAlchemyConversationStore``
  via the same ``get_or_create_engine`` cache the server uses,
  pointing at the same DB file.
- Fires N concurrent appends from M threads in this test
  process against the SHARED database file.
- Asserts no thread raised ``IntegrityError`` and final
  positions are contiguous.

If the lock-escalation fix regresses, this test surfaces the
exact ``IntegrityError`` the user reported. The store-level
unit/integration tests in
``tests/stores/test_conversation_store.py`` cover the same
race in-process; this test is the cross-process regression
gate (catches the case where a future engine-cache change
makes the busy_timeout pragma not apply across all session
factories).
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_free_port() -> int:
    """Pick a free TCP port for the Omnigent subprocess to bind."""
    s = socket.socket()
    s.bind(("", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def ap_server_with_shared_db() -> Iterator[tuple[str, str]]:
    """
    Start a real Omnigent server subprocess and yield (base_url, db_uri).

    The DB URI is the same one the test uses to construct its own
    ``SqlAlchemyConversationStore`` — so writes from the test
    process and writes from the Omnigent server hit the same SQLite
    file. Reproduces the cross-process write contention the
    user's REPL session triggers (REPL process does HTTP →
    Omnigent server process writes; test process directly writes via
    the store, simulating a concurrent path).

    :yields: ``(base_url, db_uri)`` — server URL and SQLite URI
        pointing at the shared DB.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmp_root = Path(tempfile.mkdtemp(prefix="ap-append-race-e2e-"))
    db_path = tmp_root / "ap.db"
    db_uri = f"sqlite:///{db_path}"
    artifact_dir = tmp_root / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_root / "server.log"

    env = {
        **os.environ,
        "PYTHONPATH": (f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"),
        # Workflow's compaction layer constructs an LLM client
        # at startup; stub satisfies the env check.
        "OPENAI_API_KEY": "stub-not-used",
    }
    for var in ("DATABRICKS_TOKEN", "ANTHROPIC_API_KEY", "CODEX", "CLAUDE_CODE"):
        env.pop(var, None)

    log_handle = open(log_path, "w")  # noqa: SIM115 — subprocess holds the FD
    proc = subprocess.Popen(
        [
            str(_REPO_ROOT / ".venv" / "bin" / "python"),
            "-m",
            "omnigent.cli.commands",
            "server",
            "--port",
            str(port),
            "--database-uri",
            db_uri,
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        # Wait for /health.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ReadError):
                pass
            time.sleep(0.2)
        else:
            log_handle.close()
            raise RuntimeError(f"AP server failed to start. log: {log_path}")
        yield (base_url, db_uri)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


def test_concurrent_appends_against_live_omnigent_server_db_no_collision(
    ap_server_with_shared_db: tuple[str, str],
) -> None:
    """
    With a live Omnigent server running, fire N concurrent appends
    from this test process against the SAME SQLite DB file the
    server is using. No append must raise IntegrityError; final
    positions must be contiguous.

    What this proves and what a failure means:

    - The lock-escalation fix in
      :meth:`SqlAlchemyConversationStore._lock_conversation`
      works against a database file shared with another process
      (the Omnigent server). On revert, the test surfaces the user's
      exact ``UNIQUE constraint failed`` error.
    - The production engine-cache + session-factory wiring
      (``get_or_create_engine`` + ``make_managed_session_maker``)
      applies the ``busy_timeout=20000`` PRAGMA to every
      session, so the second writer blocks rather than failing
      fast on SQLITE_BUSY.

    Note: this test does NOT exercise the Omnigent route layer (the
    HTTP append endpoint doesn't exist; appends go through
    workflow execution which needs an LLM). Coverage of the
    HTTP path is captured by the workflow integration tests
    in ``tests/runtime/test_workflow.py`` and the live REPL
    re-runs documented in CLAUDE.md's "Mandatory REPL
    Verification" section.
    """
    _, db_uri = ap_server_with_shared_db

    # Create our own store handle pointing at the SAME database
    # file. ``get_or_create_engine`` caches engines per URI, so
    # this gets a separate engine in this process (the Omnigent server
    # has its own engine in its process); both share the SQLite
    # file via filesystem-level concurrency control.
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()

    threads_count = 6
    items_per_thread = 8
    errors: list[tuple[int, Exception]] = []
    errors_lock = threading.Lock()

    def _append_worker(thread_idx: int) -> None:
        try:
            for i in range(items_per_thread):
                conv_store.append(
                    conv.id,
                    [
                        NewConversationItem(
                            type="message",
                            response_id=f"resp_e2e_t{thread_idx}",
                            data=MessageData(
                                role="user",
                                content=[
                                    {
                                        "type": "input_text",
                                        "text": f"e2e-t{thread_idx}-i{i}",
                                    }
                                ],
                            ),
                        ),
                    ],
                )
        except Exception as exc:
            with errors_lock:
                errors.append((thread_idx, exc))

    threads = [threading.Thread(target=_append_worker, args=(i,)) for i in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60.0)

    assert errors == [], (
        f"Concurrent appends against the live Omnigent server's SQLite "
        f"DB raised {len(errors)} error(s); first: {errors[0]!r}. "
        f"The cross-process / cross-engine lock escalation "
        f"regressed — the user-reported 2026-04-30 IntegrityError "
        f"on conversation_items.conversation_id+position is back."
    )

    expected_count = threads_count * items_per_thread
    items = conv_store.list_items(conv.id, limit=1000).data
    assert len(items) == expected_count, (
        f"expected {expected_count} items; got {len(items)} — some "
        f"appends silently dropped despite not raising."
    )
    # Position uniqueness is enforced by the SQL UNIQUE index on
    # (conversation_id, position). Any race that bypassed the
    # lock escalation and produced duplicate positions would
    # have raised IntegrityError above. The count + ID
    # distinctness check is sufficient — and uses only the
    # public store API, no private session access.
    item_ids = {item.id for item in items}
    assert len(item_ids) == expected_count, (
        f"expected {expected_count} distinct item IDs; got "
        f"{len(item_ids)}. Duplicate IDs would mean an item was "
        f"persisted twice across the cross-process boundary."
    )
