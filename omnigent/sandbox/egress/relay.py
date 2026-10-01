"""TCP-to-Unix-socket relay for egress proxy hard enforcement.

Runs inside the network namespace on loopback. Programs connect to
``127.0.0.1:{relay_port}`` (set via ``HTTP_PROXY`` / ``HTTPS_PROXY``),
and this relay bridges each connection to the parent process's egress
proxy via a bind-mounted Unix socket.

The relay is started as a background daemon thread during sandbox
activation (:meth:`activate`) so that it's available before the
helper's RPC loop begins accepting commands.

Security:

- **Fail-loud bind**: If ``asyncio.start_server`` fails (e.g. port
  already bound by another process), :func:`start_relay` raises
  :class:`OSError`. Older versions silently logged and continued,
  letting the helper's HTTP traffic flow to whatever was already
  on the port — a same-user-process MITM risk on macOS where
  there's no network namespace isolation.
- **Random port**: The parent picks an ephemeral port via
  :func:`socket.bind('127.0.0.1', 0)` per helper rather than the
  legacy hardcoded ``18080``, so port-squat attacks must race
  every helper start instead of pre-binding a well-known port.

Historical note: a prior revision enforced a
``Proxy-Authorization: Basic <base64(omnigent:token)>`` header on
every inbound connection, with the token shared between parent and
helper via :class:`omnigent.sandbox.core.SandboxPolicy.
egress_auth_token`. That mechanism was removed because the token
was carried on the helper's ``Popen`` argv (visible via
``/proc/<pid>/cmdline`` and ``ps`` to any same-UID process), making
it the WEAKEST secret in the system — strictly weaker than the
random-ephemeral-port + fail-loud-bind guarantees the relay still
provides. The auth path also added per-connection latency for no
net protection gain. See ``omnigent/environments/os_env.py:
_start_egress_proxy_locked`` for the matching parent-side change.

Lifecycle::

    # Inside the sandboxed helper process, after activate_sandbox():
    start_relay(relay_port=37145,
                unix_socket_path="/tmp/scratch/.egress.sock")
    # relay runs in a background daemon thread until process exit
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_BUF_SIZE = 65536


def start_relay(
    relay_port: int,
    unix_socket_path: str | Path,
) -> threading.Event:
    """Start the TCP-to-Unix relay as a background daemon thread.

    The relay listens on ``127.0.0.1:{relay_port}`` and forwards
    each accepted connection to the Unix socket at *unix_socket_path*.
    The thread is daemonic and exits when the process terminates.

    Security: the bind happens synchronously here (not in the
    background thread) so a failed bind raises :class:`OSError`
    from this call rather than silently logging in the background.
    Callers (sandbox-activate paths) propagate this exception so
    the helper aborts rather than running with no egress isolation.
    This is the load-bearing defense against same-host port-squat
    MITM — without it, a port-bind race would result in the
    helper's HTTP traffic flowing through an attacker's listener.

    :param relay_port: TCP port to listen on (loopback only).
    :param unix_socket_path: Path to the Unix socket connecting to
        the parent's egress proxy.
    :returns: A :class:`threading.Event` that is set once the TCP
        listener is bound and accepting. Callers that need to
        synchronize with relay readiness (e.g. tests) ``wait()``
        on it; production callers can discard it.
    :raises OSError: If the relay cannot bind to the port. This is
        intentionally fail-loud — silently absorbing this error
        was the original vulnerability (helper traffic flowed to
        whatever was already bound on the port).
    """
    sock_path = str(unix_socket_path)
    ready = threading.Event()

    # Pre-bind synchronously so a bind failure (port already taken,
    # permission denied, etc.) raises BEFORE we spawn the background
    # thread. The helper's activate path then aborts loudly instead
    # of silently running with no egress enforcement.
    import socket as _socket

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", relay_port))
    except OSError as exc:
        sock.close()
        raise OSError(
            f"Egress relay cannot bind 127.0.0.1:{relay_port}: {exc}. "
            f"This usually means another process is already bound to "
            f"the port (port-squat attack vector); aborting the helper "
            f"rather than silently forwarding its HTTP traffic to the "
            f"unknown listener."
        ) from exc
    sock.setblocking(False)

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve(sock, sock_path, ready))
        except Exception:
            logger.exception("Egress relay crashed")
        finally:
            # If we crashed before reaching serve_forever, unblock any
            # waiter so it fails fast instead of hanging.
            ready.set()
            loop.close()

    thread = threading.Thread(target=_run, name="egress-relay", daemon=True)
    thread.start()
    logger.info(
        "Egress relay started on 127.0.0.1:%d -> unix:%s",
        relay_port,
        sock_path,
    )
    return ready


async def _serve(
    sock: object,  # socket.socket — typed loosely to avoid an import in the signature.
    unix_socket_path: str,
    ready: threading.Event,
) -> None:
    """Asyncio server loop for the relay."""
    # Strong-reference set for in-flight handler tasks. asyncio's
    # ``start_server`` only weak-refs handlers via the protocol's
    # ``self._task``, which it drops on ``connection_lost``; without
    # this set the handler can be garbage-collected mid-forward when
    # one side of the proxied connection half-closes.
    active: set[asyncio.Task[None]] = set()

    async def _handle(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        current = asyncio.current_task()
        if current is not None:
            active.add(current)
            current.add_done_callback(active.discard)
        try:
            try:
                ux_reader, ux_writer = await asyncio.open_unix_connection(unix_socket_path)
            except OSError as exc:
                logger.warning(
                    "Cannot connect to egress proxy socket %s: %s",
                    unix_socket_path,
                    exc,
                )
                client_writer.close()
                return

            try:
                await asyncio.gather(
                    _pipe(client_reader, ux_writer),
                    _pipe(ux_reader, client_writer),
                )
            except Exception:  # noqa: BLE001 — connection teardown is best-effort
                pass
            finally:
                for w in (client_writer, ux_writer):
                    try:  # noqa: SIM105
                        w.close()
                    except Exception:  # noqa: BLE001 — close is best-effort
                        pass
        except Exception:
            logger.exception("Egress relay handler crashed")
            # ``close()`` is best-effort — the connection may already be torn down
            # by the time the exception handler runs.
            with contextlib.suppress(Exception):
                client_writer.close()

    server = await asyncio.start_server(_handle, sock=sock)
    ready.set()
    async with server:
        await server.serve_forever()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy data from *reader* to *writer* until EOF or error."""
    try:
        while True:
            data = await reader.read(_BUF_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass
