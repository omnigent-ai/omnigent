"""E2E regression: a mid-transaction DB disconnect fails session persistence.

A server pointed at a remote database (the reported deployment is MySQL via
``pymysql``) loses writes when the connection drops **mid-statement**:
``(2006, "MySQL server has gone away")`` raised while SQLAlchemy rolls a
transaction back, or ``(2013, 'Lost connection to MySQL server during query')``
on the ``UPDATE conversations SET next_position=... WHERE ...`` write the
conversation-item ``append`` path flushes.

The dialect-agnostic product path both signatures share:

1. ``omnigent.db.utils.run_write_transaction`` runs the write. If a transient
   disconnect ``OperationalError`` re-raises without a replay, a write the
   very next connection would have persisted is lost outright.
2. The resulting ``sqlalchemy.exc.OperationalError`` is a ``StatementError``,
   so ``omnigent.server.app._handle_statement_error`` catches it, logs
   ``"Database error: (...OperationalError) ..."`` and returns HTTP 500
   (``code=internal_error``, ``error_category=SERVER``, ``impact=BLOCKING``) --
   the observed surface.

**Environment fidelity.** The CI sandbox blocks the PyPI registry, the internal
package proxy, and git, so ``pymysql`` cannot be installed and the server
cannot open a real ``mysql+pymysql://`` connection here. Instead we drive the
**identical, dialect-agnostic product code** against a genuine out-of-process
remote database server -- the ``cloudflare_d1`` SQLite-over-HTTP dialect (a test
dependency, the same one ``test_server_banner_db_url_redaction_e2e.py`` boots the
server against) -- and drop its connection **mid-statement**. The D1 dialect
turns a dropped connection into a DBAPI ``OperationalError`` (``"HTTP request
failed: Server disconnected without sending a response"``), the faithful analog
of "MySQL server has gone away". Unlike the MySQL dialect, whose
``is_disconnect`` natively recognizes 2006/2013, the D1 dialect does not
classify its dropped-connection error -- so each test engine installs a
``handle_error`` listener (:func:`_make_disconnect_aware_engine`) that marks it
as a disconnect, keeping the stand-in faithful to the reported transport.

The assertions encode the CORRECT post-fix behavior, so the test FAILS on the
buggy build and PASSES once a transient mid-transaction disconnect is recovered
(connection-lost errors replayed the way CockroachDB serialization failures
already are):

* :func:`test_transient_mid_transaction_disconnect_is_recovered` -- the fail->pass
  regression guard. A write whose connection drops once, then recovers, must
  ultimately persist rather than raising an uncaught ``OperationalError``.
* :func:`test_mid_transaction_disconnect_surfaces_as_500` -- documents the exact
  reported surface: the disconnect ``OperationalError`` routed through the real
  ``_handle_statement_error`` yields HTTP 500 and the ``"Database error: (...
  OperationalError) ..."`` log line.

Run::

    .venv/bin/python -m pytest tests/e2e/test_mysql_disconnect_persistence_e2e.py -v
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as saexc

# The stand-in transport: skip cleanly if the dialect (a test dependency) is
# absent so the file never hard-fails on an unexpected environment.
pytest.importorskip(
    "sqlalchemy_cloudflare_d1",
    reason="cloudflare_d1 dialect is the stand-in remote DB transport for this repro",
)

# D1 auto-commits; the dialect still emits these transaction-control keywords,
# which the emulator must accept without touching the backing store.
_TXN_KEYWORDS = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}


class _DisconnectingD1Emulator:
    """A cloudflare_d1 ``/raw`` REST server backed by a local SQLite file.

    Executes each posted statement on one autocommit SQLite connection
    (matching D1 semantics), but when ``arm(sql_substring)`` is set it drops
    the socket abruptly -- without sending a response -- the moment it receives
    a statement containing that substring. The D1 dialect surfaces that as a
    DBAPI ``OperationalError``, the analog of a MySQL "server has gone away"
    mid-statement.
    """

    def __init__(self, backing_db: Path) -> None:
        self._backing = sqlite3.connect(
            str(backing_db), isolation_level=None, check_same_thread=False
        )
        self._lock = threading.Lock()
        self._trigger: str | None = None
        self._one_shot = False
        self.drops = 0
        emulator = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                sql = body.get("sql", "")
                params = body.get("params") or []
                if emulator._should_drop(sql):
                    emulator.drops += 1
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                    except OSError:
                        pass
                    return
                head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
                try:
                    with emulator._lock:
                        if head in _TXN_KEYWORDS:
                            columns: list[str] = []
                            rows: list[list[object]] = []
                        else:
                            cur = emulator._backing.execute(sql, params)
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
                except Exception as exc:  # noqa: BLE001 - mirror the D1 error envelope
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

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _should_drop(self, sql: str) -> bool:
        if self._trigger is None or self._trigger not in sql:
            return False
        if self._one_shot:
            # Disarm so the retry (post-fix) reaches a live server.
            self._trigger = None
        return True

    def arm(self, sql_substring: str, *, one_shot: bool) -> None:
        """Drop the connection on the next statement containing *sql_substring*."""
        self._trigger = sql_substring
        self._one_shot = one_shot

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._backing.close()


@pytest.fixture()
def d1_disconnect(tmp_path: Path) -> Iterator[_DisconnectingD1Emulator]:
    """A running D1 emulator with a probe table, wired via ``CF_D1_BASE_URL``.

    Restores the ambient proxy/``CF_D1_BASE_URL`` env on teardown so the
    loopback emulator is reachable without an intervening corporate proxy.
    """
    backing = tmp_path / "d1-backing.db"
    conn = sqlite3.connect(str(backing))
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    conn.execute("INSERT INTO probe (id, n) VALUES (1, 0)")
    conn.commit()
    conn.close()

    emulator = _DisconnectingD1Emulator(backing)
    emulator.start()

    saved = {k: os.environ.get(k) for k in ("CF_D1_BASE_URL", "no_proxy", "NO_PROXY")}
    os.environ["CF_D1_BASE_URL"] = emulator.base_url
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    try:
        yield emulator
    finally:
        emulator.stop()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _make_disconnect_aware_engine() -> sa.Engine:
    """Create a stand-in engine whose disconnects are classified as such.

    The D1 dialect does not implement ``is_disconnect`` for its dropped-
    connection ``OperationalError``, so mark it via SQLAlchemy's public
    ``handle_error`` hook -- the same classification the MySQL dialect
    performs natively for 2006/2013 in the reported environment.
    """
    from sqlalchemy_cloudflare_d1.connection import (
        OperationalError as D1OperationalError,
    )

    engine = sa.create_engine("cloudflare_d1://omni:pw@omnigentdb")

    @sa.event.listens_for(engine, "handle_error")
    def _mark_disconnect(ctx: sa.engine.ExceptionContext) -> None:
        exc = ctx.original_exception
        if isinstance(exc, D1OperationalError) and "HTTP request failed" in str(exc):
            ctx.is_disconnect = True

    return engine


def _capture_disconnect_operational_error(
    emulator: _DisconnectingD1Emulator,
) -> saexc.OperationalError:
    """Run a statement against the remote server as it drops the connection.

    :returns: the genuine ``sqlalchemy.exc.OperationalError`` the disconnect
        raised -- the same class and shape the reported pymysql 2006/2013
        errors carry.
    """
    engine = _make_disconnect_aware_engine()
    emulator.arm("SELECT n FROM probe", one_shot=False)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT n FROM probe WHERE id = 1")).scalar_one()
    except saexc.OperationalError as exc:
        return exc
    finally:
        engine.dispose()
    raise AssertionError("expected a mid-statement disconnect to raise OperationalError")


def test_transient_mid_transaction_disconnect_is_recovered(
    d1_disconnect: _DisconnectingD1Emulator,
) -> None:
    """A transient mid-transaction disconnect must not lose the write.

    Drives the REAL ``run_write_transaction`` against a remote database server
    whose connection drops once (then recovers) during the write -- the
    dialect-agnostic core of both reported disconnect signatures.

    Post-fix contract (asserted here): the write is replayed against the
    recovered connection and ultimately persists, exactly as CockroachDB
    serialization failures are already replayed.

    Buggy build: ``run_write_transaction`` runs the callback once, the
    disconnect raises ``sqlalchemy.exc.OperationalError`` (a
    ``StatementError``), and the loop re-raises it without a replay -- so
    this test fails with that uncaught OperationalError, reproducing the
    lost write.
    """
    from omnigent.db.utils import (
        is_cockroachdb,
        make_named_managed_session_maker,
        run_write_transaction,
    )

    engine = _make_disconnect_aware_engine()
    # Guard the premise: this is NOT the one dialect whose serialization
    # failures were already replayed, so any recovery must come from a real
    # connection-loss fix.
    assert not is_cockroachdb(engine.dialect.name)
    maker = make_named_managed_session_maker(engine, query_name_prefix="disconnect_probe")

    attempts = {"n": 0}

    def _bump(session: sa.orm.Session) -> int:
        attempts["n"] += 1
        session.execute(sa.text("UPDATE probe SET n = n + 1 WHERE id = 1"))
        return session.execute(sa.text("SELECT n FROM probe WHERE id = 1")).scalar_one()

    # Drop the connection mid-query on the UPDATE itself (the reported 2013
    # shape), then serve normally so a replay can succeed. The emulator
    # autocommits each statement, so dropping before the UPDATE executes is
    # the faithful equivalent of the aborted server-side transaction: the
    # first attempt persists nothing, and a correct replay applies the write
    # exactly once.
    d1_disconnect.arm("UPDATE probe", one_shot=True)

    try:
        result = run_write_transaction(maker, "bump_probe", _bump)
    except saexc.OperationalError as exc:
        # Reproduced: the transient disconnect was never recovered. Surface the
        # observed class/shape so the failure reads as the bug, not noise.
        assert isinstance(exc, saexc.StatementError)
        orig = type(exc.orig).__module__ + "." + type(exc.orig).__name__ if exc.orig else None
        pytest.fail(
            "run_write_transaction did NOT recover a transient mid-transaction "
            "disconnect: a connection that dropped once, then recovered, lost "
            f"the write. Raised {type(exc).__name__} "
            f"(orig={orig}) after {attempts['n']} attempt(s), drops="
            f"{d1_disconnect.drops}. First line: {str(exc).splitlines()[0]}"
        )
    finally:
        engine.dispose()

    assert result == 1, f"write should persist once after recovery, got {result!r}"
    assert d1_disconnect.drops == 1, "the disconnect must have actually fired"


def test_mid_transaction_disconnect_surfaces_as_500(
    d1_disconnect: _DisconnectingD1Emulator,
    tmp_path: Path,
) -> None:
    """The disconnect ``OperationalError`` maps to the reported 500 + log line.

    Documents the exact user-observable surface: a mid-statement disconnect
    ``OperationalError`` routed through the REAL
    ``omnigent.server.app._handle_statement_error`` (registered by
    ``create_app``) returns HTTP 500 and logs ``"Database error: (...
    OperationalError) ..."``.
    """
    from fastapi.testclient import TestClient

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    disconnect_error = _capture_disconnect_operational_error(d1_disconnect)
    assert isinstance(disconnect_error, saexc.StatementError)

    # Build the real app (SQLite-backed stores) so its production
    # StatementError handler is the one under test.
    del os.environ["CF_D1_BASE_URL"]  # app stores use sqlite, not the emulator
    db_uri = f"sqlite:///{tmp_path}/app.db"
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )

    async def _probe() -> None:
        # Re-raise the genuine disconnect error captured above, as a real
        # request path would when its DB write dies mid-statement.
        raise disconnect_error

    app.add_api_route("/__db_disconnect_probe", _probe, methods=["GET"])
    # The SPA catch-all is registered last; move the probe ahead of it.
    app.router.routes.insert(0, app.router.routes.pop())

    # Capture directly on the app logger: it does not propagate to root, so
    # caplog's root handler never sees the ``_handle_statement_error`` record.
    captured_messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_messages.append(record.getMessage())

    app_logger = logging.getLogger("omnigent.server.app")
    handler = _Capture(level=logging.ERROR)
    app_logger.addHandler(handler)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/__db_disconnect_probe")
    finally:
        app_logger.removeHandler(handler)

    assert resp.status_code == 500, f"expected the reported 500, got {resp.status_code}"
    assert resp.json() == {
        "error": {"code": "internal_error", "message": "An internal error occurred."}
    }
    db_error_logs = [
        message for message in captured_messages if message.startswith("Database error:")
    ]
    assert db_error_logs, "expected the _handle_statement_error 'Database error:' log line"
    assert "OperationalError" in db_error_logs[0]
