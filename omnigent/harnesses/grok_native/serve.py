"""Spawn and await a per-session ``grok agent serve`` WebSocket server.

``grok agent serve`` listens on ``127.0.0.1:<port>`` and authenticates clients
with a secret in the WS URL (``ws://<addr>/ws?server-key=<secret>``). One
server is launched per Omnigent session; its lifetime is the pane-equivalent
resource a ``native-server`` harness manages. See docs/GROK_NATIVE_DESIGN.md.
"""

from __future__ import annotations

import secrets
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
        # ``--plugin-dir`` and other ``agent``-level options precede ``serve``
        # in clap's subcommand order; splicing after the subcommand would let
        # them silently land on the wrong parser.
        agent_opts, serve_opts = [], []
        it = iter(extra_args)
        for arg in it:
            (agent_opts if arg == "--plugin-dir" else serve_opts).append(arg)
            if arg == "--plugin-dir":
                agent_opts.append(next(it, ""))
        argv = [executable, "agent", *agent_opts, "serve", *argv[3:], *serve_opts]
    return argv


def build_grok_serve_endpoint(
    *,
    port: int | None = None,
    secret: str | None = None,
) -> GrokServeEndpoint:
    """Allocate an endpoint (port + secret + URL) for a new serve process."""
    from omnigent.harnesses.grok_native.probe import allocate_grok_port

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
    "build_grok_serve_endpoint",
    "grok_serve_argv",
    "grok_ws_url",
    "mint_grok_secret",
]
