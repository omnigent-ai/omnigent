"""End-to-end regression: a transient mid-transaction MySQL disconnect silently
drops a session-persistence write.

The bug lives in :func:`omnigent.db.utils.run_write_transaction`, which replays
only CockroachDB serialization failures (40001) and MySQL deadlock victims
(1213). A connection-loss error raised mid-transaction -- pymysql 2013 ("Lost
connection to MySQL server during query") or 2006 ("MySQL server has gone
away") -- is not retryable, so it re-raises and the write is lost. The reported
signatures both come from a session append: the 2013 case on the
``UPDATE conversations SET next_position=...`` write, the 2006 case as a broken
pipe while the aborted transaction rolls back. Both escape the store as a
SQLAlchemy ``OperationalError`` (a ``StatementError``), which the server's
``_handle_statement_error`` maps to an HTTP 500 with a ``Database error:`` log
-- the KPI signature this guards against.

This test drives the REAL ``SqlAlchemyConversationStore.append()`` against a
REAL MySQL 8.0 server (matching the reported ``mysql+pymysql://`` deployment),
fronting it with a TCP relay that drops the connection mid-statement on the
reported ``UPDATE conversations`` write. On the buggy build the append re-raises
the pymysql disconnect and the item never persists; the fix replays the write
transaction so the append recovers and the item persists exactly once.

The reported disconnects (failover, restart, a ``wait_timeout`` kill) are
server-side events: MySQL tears the dead session down and releases its row
locks, so the replay runs cleanly. Severing only the TCP path would leave the
orphaned transaction holding the ``conversations`` row lock until InnoDB's
lock-wait timeout, so a background reaper kills that idle transaction once the
disconnect has fired -- mimicking the server-side teardown the real causes
perform.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pymysql = pytest.importorskip("pymysql")

from sqlalchemy.exc import OperationalError  # noqa: E402

from omnigent.entities import MessageData, NewConversationItem  # noqa: E402
from omnigent.stores.conversation_store.sqlalchemy_store import (  # noqa: E402
    SqlAlchemyConversationStore,
)


def _find_mysqld() -> str | None:
    for candidate in ("mysqld", "mariadbd"):
        found = shutil.which(candidate)
        if found:
            return found
    for path in ("/usr/sbin/mysqld", "/usr/sbin/mariadbd"):
        if os.path.exists(path):
            return path
    return None


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


class _MySQLServer:
    """A throwaway mysqld with a plaintext-auth ``omni`` user + ``omnigent`` DB.

    Runs with ``--skip-ssl`` so the drop relay can match the plaintext query
    bytes crossing the wire; TLS is a transport detail orthogonal to the bug.
    The ``omni`` user is granted ``PROCESS`` so the reaper can see and kill the
    transaction orphaned by the severed connection.
    """

    def __init__(self, mysqld: str) -> None:
        self._mysqld = mysqld
        self.datadir = tempfile.mkdtemp(prefix="omni-mysql-disconnect-")
        self.port = _free_port()
        self._log = os.path.join(self.datadir, "mysqld.log")
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        subprocess.run(
            [
                self._mysqld,
                "--no-defaults",
                f"--datadir={self.datadir}",
                "--initialize-insecure",
                f"--log-error={self._log}",
            ],
            check=True,
        )
        init_sql = os.path.join(self.datadir, "init.sql")
        Path(init_sql).write_text(
            "CREATE DATABASE IF NOT EXISTS omnigent CHARACTER SET utf8mb4;\n"
            "CREATE USER IF NOT EXISTS 'omni'@'%' IDENTIFIED WITH "
            "mysql_native_password BY 'omni';\n"
            "GRANT ALL PRIVILEGES ON omnigent.* TO 'omni'@'%';\n"
            "GRANT PROCESS ON *.* TO 'omni'@'%';\n"
            "FLUSH PRIVILEGES;\n"
        )
        self._proc = subprocess.Popen(
            [
                self._mysqld,
                "--no-defaults",
                f"--datadir={self.datadir}",
                f"--socket={os.path.join(self.datadir, 'mysqld.sock')}",
                f"--port={self.port}",
                "--bind-address=127.0.0.1",
                "--skip-ssl",
                "--skip-log-bin",
                "--performance-schema=OFF",
                "--innodb-buffer-pool-size=64M",
                "--secure-file-priv=",
                f"--init-file={init_sql}",
                f"--log-error={self._log}",
            ],
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                probe = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                probe.close()
                time.sleep(1.0)  # let --init-file finish creating the user/DB
                return
            except OSError:
                time.sleep(0.3)
        log = Path(self._log).read_text() if os.path.exists(self._log) else "(no log)"
        raise RuntimeError(f"mysqld did not start:\n{log}")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        shutil.rmtree(self.datadir, ignore_errors=True)


class _DropRelay:
    """TCP relay to MySQL that severs the connection on a trigger substring.

    ``arm(trigger)`` makes the relay RST-close both sides of the first
    connection whose client->server bytes contain ``trigger`` (one-shot), so a
    subsequent replay reconnects cleanly through the relay.
    """

    def __init__(self, upstream_port: int) -> None:
        self._upstream_port = upstream_port
        self.listen_port = _free_port()
        self._trigger: bytes | None = None
        self.drops = 0
        self._srv: socket.socket | None = None
        self._stop = False

    def arm(self, trigger: bytes) -> None:
        self._trigger = trigger

    def start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.listen_port))
        self._srv.listen(16)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        assert self._srv is not None
        self._srv.settimeout(0.5)
        while not self._stop:
            try:
                client, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    @staticmethod
    def _rst_close(sock: socket.socket) -> None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        with contextlib.suppress(OSError):
            sock.close()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(("127.0.0.1", self._upstream_port))
        except OSError:
            self._rst_close(client)
            return

        def pump(src: socket.socket, dst: socket.socket, inspect: bool) -> None:
            while not self._stop:
                try:
                    data = src.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                if inspect and self._trigger and self._trigger in data:
                    self._trigger = None
                    self.drops += 1
                    self._rst_close(upstream)
                    self._rst_close(client)
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    break
            self._rst_close(src)
            self._rst_close(dst)

        threading.Thread(target=pump, args=(client, upstream, True), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client, False), daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        if self._srv is not None:
            with contextlib.suppress(OSError):
                self._srv.close()


def _reap_orphaned_transaction(
    host: str, port: int, drops: Callable[[], int], stop: threading.Event
) -> None:
    """Model the server-side teardown a real transient disconnect performs.

    A failover, restart, or ``wait_timeout`` kill terminates the dead session
    and releases its row locks. Severing only the TCP path leaves the orphaned
    transaction holding the ``conversations`` row lock, so once the disconnect
    has fired, kill the idle in-flight transaction (``RUNNING`` with no active
    query) so the replay is not blocked on a lock the real causes would have
    released. Connects straight to the server, bypassing the relay.
    """
    while not stop.is_set() and drops() == 0:
        time.sleep(0.05)
    killed = False
    while not killed and not stop.wait(0.1):
        try:
            conn = pymysql.connect(
                host=host,
                port=port,
                user="omni",
                password="omni",
                database="omnigent",
                connect_timeout=5,
            )
        except Exception:
            continue
        try:
            conn.autocommit(True)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trx_mysql_thread_id FROM information_schema.innodb_trx "
                    "WHERE trx_state = 'RUNNING' AND trx_query IS NULL"
                )
                for (thread_id,) in cur.fetchall():
                    try:
                        cur.execute(f"KILL {int(thread_id)}")
                        killed = True
                    except Exception:
                        pass
        finally:
            conn.close()


@pytest.fixture(scope="module")
def mysql_server() -> Iterator[_MySQLServer]:
    mysqld = _find_mysqld()
    if mysqld is None:
        pytest.skip("mysqld/mariadbd not available")
    server = _MySQLServer(mysqld)
    try:
        server.start()
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        server.stop()
        pytest.skip(f"could not start a local mysqld: {exc}")
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def store_and_relay(
    mysql_server: _MySQLServer,
) -> Iterator[tuple[SqlAlchemyConversationStore, _DropRelay]]:
    relay = _DropRelay(mysql_server.port)
    relay.start()
    uri = f"mysql+pymysql://omni:omni@127.0.0.1:{relay.listen_port}/omnigent"
    store = SqlAlchemyConversationStore(uri)
    try:
        yield store, relay
    finally:
        relay.stop()


def _user_message(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp_disconnect_repro",
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def test_transient_mysql_disconnect_persists_session_write(
    store_and_relay: tuple[SqlAlchemyConversationStore, _DropRelay],
    mysql_server: _MySQLServer,
) -> None:
    """A transient disconnect during the append write must not lose the item.

    Fails on the buggy build: ``run_write_transaction`` re-raises the pymysql
    disconnect (2013/2006) without replay, so the second message never persists
    and the error escapes as the HTTP 500 / ``Database error:`` KPI signature.
    """
    store, relay = store_and_relay
    conv = store.create_conversation()

    store.append(conv.id, [_user_message("first message")])

    # Sever the connection mid-statement on the reported next_position write,
    # and reap the server-side transaction it orphans (as a real failover /
    # wait_timeout kill would) so the replay is not blocked on a stale lock.
    relay.arm(b"UPDATE conversations")
    stop = threading.Event()
    reaper = threading.Thread(
        target=_reap_orphaned_transaction,
        args=("127.0.0.1", mysql_server.port, lambda: relay.drops, stop),
        daemon=True,
    )
    reaper.start()

    disconnect: OperationalError | None = None
    try:
        store.append(conv.id, [_user_message("second message")])
    except OperationalError as exc:
        disconnect = exc
    finally:
        stop.set()
        reaper.join(timeout=5)

    assert relay.drops == 1, "relay did not sever the append's write"

    texts = [
        item.data.content[0].get("text")
        for item in store.list_items(conv.id, limit=1000).data
        if getattr(item.data, "content", None)
    ]
    assert disconnect is None, (
        "append() re-raised a transient MySQL disconnect instead of replaying "
        f"the write transaction: {disconnect!r}. Session persistence lost the "
        f"user message; only {texts!r} survived."
    )
    assert texts.count("second message") == 1, (
        f"the user message did not persist exactly once after a transient "
        f"disconnect; conversation items were {texts!r}."
    )
