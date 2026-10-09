"""Regression against a real MySQL server: a transient statement disconnect
must be retried so a session-persistence write persists exactly once.

:func:`omnigent.db.utils.run_write_transaction` replays CockroachDB
serialization failures (40001) and MySQL deadlock victims (1213). A
statement-phase connection loss -- pymysql 2013 ("Lost connection to MySQL
server during query") or 2006 ("MySQL server has gone away") on the
``UPDATE conversations`` write -- must replay too, or the append re-raises a
SQLAlchemy ``OperationalError`` that the server maps to an HTTP 500 with a
``Database error:`` log, the KPI signature this guards against.

This test drives the real ``SqlAlchemyConversationStore.append()`` against a
real MySQL 8.0 server (the reported ``mysql+pymysql://`` deployment), fronts it
with a TCP relay that severs the connection mid-statement on that write, and
asserts the item still persists exactly once.

A real disconnect (failover, restart, a ``wait_timeout`` kill) also releases
the dead session's row locks. Severing only the TCP path would leave the
orphaned transaction holding the ``conversations`` row lock until InnoDB's
lock-wait timeout, so a background reaper kills that transaction once the
disconnect has fired, mimicking that server-side teardown.
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
from typing import Any

import pytest

pymysql = pytest.importorskip("pymysql")

from sqlalchemy.exc import OperationalError  # noqa: E402

from omnigent.db.utils import clear_engine_cache  # noqa: E402
from omnigent.entities import MessageData, NewConversationItem  # noqa: E402
from omnigent.stores.conversation_store.sqlalchemy_store import (  # noqa: E402
    SqlAlchemyConversationStore,
)


def _find_mysqld() -> str | None:
    # The server command uses MySQL-8 syntax (--initialize-insecure, --skip-ssl,
    # mysql_native_password), so only a real mysqld is a valid backend here.
    found = shutil.which("mysqld")
    if found:
        return found
    return "/usr/sbin/mysqld" if os.path.exists("/usr/sbin/mysqld") else None


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
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                # mysqld exited (bad datadir, port clash, init-file error); stop
                # waiting and surface the log instead of polling for 60s.
                break
            try:
                # Authenticating as the init-file user also proves the --init-file
                # has finished creating the omni user and omnigent database.
                conn = pymysql.connect(
                    host="127.0.0.1",
                    port=self.port,
                    user="omni",
                    password="omni",
                    database="omnigent",
                    connect_timeout=2,
                )
                conn.close()
                return
            except Exception as exc:
                last_err = exc
                time.sleep(0.3)
        log = Path(self._log).read_text() if os.path.exists(self._log) else "(no log)"
        raise RuntimeError(f"mysqld did not start (last error: {last_err}):\n{log}")

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
        self.listen_port = 0  # assigned when start() binds port 0
        self._trigger: bytes | None = None
        self.drops = 0
        self._lock = threading.Lock()
        self._srv: socket.socket | None = None
        self._stop = False

    def arm(self, trigger: bytes) -> None:
        with self._lock:
            self._trigger = trigger

    def start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind port 0 and read the kernel-assigned port to avoid the
        # pick-then-bind race of handing out a port that another process grabs.
        self._srv.bind(("127.0.0.1", 0))
        self.listen_port = self._srv.getsockname()[1]
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
            tail = b""
            while not self._stop:
                try:
                    data = src.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                if inspect:
                    with self._lock:
                        trigger = self._trigger
                        hit = trigger is not None and trigger in tail + data
                        if hit:
                            self._trigger = None
                            self.drops += 1
                    if hit:
                        self._rst_close(upstream)
                        self._rst_close(client)
                        return
                    # Retain enough trailing bytes to catch a trigger split
                    # across recv() boundaries.
                    if trigger is not None:
                        keep = len(trigger) - 1
                        tail = (tail + data)[-keep:] if keep > 0 else b""
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
    transaction holding the ``conversations`` row lock. While the write is in
    flight (before the drop), record the thread id of the oldest ``RUNNING``
    transaction -- the orphan-to-be; once the disconnect has fired, kill that
    exact thread. The replay reconnects on a new thread, so it is never the
    target. The cold fallback, used only if the orphan was never observed, is
    bounded to transactions that started no later than the drop, so it cannot
    hit the replay either. The reaper's own connection is excluded, and it
    connects straight to the server, bypassing the relay.
    """
    orphan_thread_id: int | None = None
    drop_time: Any = None
    conn: Any = None
    try:
        while not stop.is_set():
            if conn is None:
                try:
                    conn = pymysql.connect(
                        host=host,
                        port=port,
                        user="omni",
                        password="omni",
                        database="omnigent",
                        connect_timeout=5,
                    )
                    conn.autocommit(True)
                except Exception:
                    conn = None
                    if stop.wait(0.02):
                        return
                    continue
            dropped = drops() > 0
            try:
                with conn.cursor() as cur:
                    if not dropped:
                        cur.execute(
                            "SELECT trx_mysql_thread_id "
                            "FROM information_schema.innodb_trx "
                            "WHERE trx_state = 'RUNNING' "
                            "AND trx_mysql_thread_id <> CONNECTION_ID() "
                            "ORDER BY trx_started ASC LIMIT 1"
                        )
                        row = cur.fetchone()
                        if row is not None:
                            orphan_thread_id = int(row[0])
                    else:
                        if drop_time is None:
                            cur.execute("SELECT NOW(6)")
                            drop_time = cur.fetchone()[0]
                        target = orphan_thread_id
                        if target is None:
                            cur.execute(
                                "SELECT trx_mysql_thread_id "
                                "FROM information_schema.innodb_trx "
                                "WHERE trx_state = 'RUNNING' AND trx_query IS NULL "
                                "AND trx_started <= %s "
                                "AND trx_mysql_thread_id <> CONNECTION_ID() "
                                "ORDER BY trx_started ASC LIMIT 1",
                                (drop_time,),
                            )
                            row = cur.fetchone()
                            target = int(row[0]) if row is not None else None
                        if target is not None:
                            # The orphan may vanish on its own before the KILL.
                            with contextlib.suppress(Exception):
                                cur.execute(f"KILL {target}")
                            return
            except Exception:
                conn = None
                continue
            if stop.wait(0.01 if not dropped else 0.05):
                return
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


@pytest.fixture(scope="module")
def mysql_server() -> Iterator[_MySQLServer]:
    mysqld = _find_mysqld()
    if mysqld is None:
        pytest.skip("mysqld not available")
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
        # Dispose the store's cached engine so each test's per-port URI does not
        # leak a connection pool for the module's lifetime.
        clear_engine_cache()


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

    Without the replay, ``run_write_transaction`` re-raises the pymysql
    disconnect (2013/2006), so the second message never persists and the error
    escapes as the HTTP 500 / ``Database error:`` KPI signature.
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
