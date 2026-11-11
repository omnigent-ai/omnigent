"""Spawn and await a per-session ``grok agent serve`` WebSocket server.

``grok agent serve`` listens on ``127.0.0.1:<port>`` and authenticates clients
with a secret in the WS URL (``ws://<addr>/ws?server-key=<secret>``). One
server is launched per Omnigent session; its lifetime is the pane-equivalent
resource a ``native-server`` harness manages. See docs/GROK_NATIVE_DESIGN.md.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
from dataclasses import dataclass

_DEFAULT_BIND_HOST = "127.0.0.1"


@dataclass(frozen=True)
class GrokServeEndpoint:
    """A ready ``grok agent serve`` endpoint a WS client can dial.

    :param host: Bind host (loopback).
    :param port: Bound port the server accepted.
    :param secret: Client auth token embedded in the WS URL.
    :param ws_url: Full ``ws://`` URL including ``?server-key=``.
    """

    host: str
    port: int
    secret: str
    ws_url: str


def allocate_grok_port() -> int:
    """Return an ephemeral loopback port for a new serve process."""
    with socket.socket() as sock:
        sock.bind((_DEFAULT_BIND_HOST, 0))
        return sock.getsockname()[1]


def mint_grok_secret() -> str:
    """Return a fresh per-session client secret for ``--secret``."""
    return secrets.token_urlsafe(24)


def grok_ws_url(host: str, port: int, secret: str) -> str:
    """Build the ``ws://`` URL clients dial (secret rides as ``server-key``)."""
    return f"ws://{host}:{port}/ws?server-key={secret}"


def grok_serve_argv(
    executable: str,
    *,
    port: int,
    secret: str,
    extra_args: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """Build the ``grok agent serve`` argv for one session."""
    argv = [
        executable,
        "agent",
        "serve",
        "--bind",
        f"{_DEFAULT_BIND_HOST}:{port}",
        "--secret",
        secret,
    ]
    if extra_args:
        argv.extend(extra_args)
    return argv


async def wait_for_grok_serve(
    port: int,
    *,
    timeout_s: float = 30.0,
    poll_s: float = 0.1,
) -> None:
    """Wait until the serve port accepts a TCP connection.

    The server prints its WS URL once bound; a connectable port is the cheap
    readiness signal (the ACP ``initialize`` handshake happens on the client
    side of the WS connect, not at bind time).

    :raises TimeoutError: when the port never comes up (e.g. crashed spawn).
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(_DEFAULT_BIND_HOST, port)
        except OSError:
            await asyncio.sleep(poll_s)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"grok agent serve did not bind {port} within {timeout_s}s")


def build_grok_serve_endpoint(
    *,
    port: int | None = None,
    secret: str | None = None,
) -> GrokServeEndpoint:
    """Allocate an endpoint (port + secret + URL) for a new serve process."""
    port = allocate_grok_port() if port is None else port
    secret = mint_grok_secret() if secret is None else secret
    return GrokServeEndpoint(
        host=_DEFAULT_BIND_HOST,
        port=port,
        secret=secret,
        ws_url=grok_ws_url(_DEFAULT_BIND_HOST, port, secret),
    )


__all__ = [
    "GrokServeEndpoint",
    "allocate_grok_port",
    "build_grok_serve_endpoint",
    "grok_serve_argv",
    "grok_ws_url",
    "mint_grok_secret",
    "wait_for_grok_serve",
]
