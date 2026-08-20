"""WebSocket ``Origin`` enforcement for the Omnigent server.

Cross-Site WebSocket Hijacking (CSWSH) protection. FastAPI/Starlette do
not validate the WebSocket ``Origin`` header by default, so any web page
the user visits in their browser can open a WebSocket to a running
Omnigent server and drive the agent, read session updates, or attach to a
terminal. In single-user **local mode** there is no cookie / proxy auth to
stop it — the server falls back to the reserved ``"local"`` user — so the
``Origin`` header is the only signal that distinguishes the user's own UI
from a hostile cross-origin page.

This module provides:

- :func:`origin_allowed` — the pure, protocol-neutral policy function
  deciding whether a connection's ``Origin`` is acceptable for the
  current mode (shared by the WebSocket middleware here and the HTTP
  ``require_trusted_origin`` dependency);
- :class:`WebSocketOriginMiddleware` — an ASGI middleware that applies the
  policy to every WebSocket handshake *before* it reaches a route handler,
  so the check runs before any ``websocket.accept()`` (per the rule in
  ``.claude/skills/code-review/security-guidelines.md`` W1/W4);
- :data:`OMNIGENT_INTERNAL_WS_ORIGIN` — the sentinel ``Origin`` the
  project's own non-browser clients (runner, host/daemon, terminal-attach)
  set so the middleware allows them unambiguously.

Other auth modes (``oidc`` / ``accounts`` / multi-user ``header``)
authenticate every connection with a signed ``__Host-ap_session`` cookie
or a trusted-proxy header, so a cross-origin page cannot ride the user's
credentials. The middleware leaves those modes as passthrough unless the
deployment opts into an explicit allowlist via
``OMNIGENT_WS_ALLOWED_ORIGINS``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.server.auth import local_single_user_enabled

_logger = logging.getLogger(__name__)

# The sentinel ``Origin`` the project's own non-browser clients set is
# defined alongside the tunnel handshake constants in
# ``omnigent.runner.identity`` and re-exported here for the server-side
# policy. See :data:`OMNIGENT_INTERNAL_WS_ORIGIN` there for rationale.
__all__ = [
    "FORBIDDEN_ORIGIN_CLOSE_CODE",
    "OMNIGENT_INTERNAL_WS_ORIGIN",
    "WebSocketOriginMiddleware",
    "origin_allowed",
    "origin_hostname_is_loopback",
    "parse_allowed_origins",
]

# Optional comma-separated allowlist of additional permitted origins. When
# set it is honored in every mode (defense-in-depth for deployments); in
# non-local modes a non-empty allowlist also flips the default from
# passthrough to deny-by-default.
_ALLOWED_ORIGINS_ENV = "OMNIGENT_WS_ALLOWED_ORIGINS"

# Private-use WebSocket close code (4000-4999) for a rejected origin.
# Distinct from the auth-failure ``1008`` and the tunnel-mismatch ``4004``
# already used elsewhere so a forbidden-origin rejection is diagnosable.
FORBIDDEN_ORIGIN_CLOSE_CODE = 4403


def origin_hostname_is_loopback(origin: str) -> bool:
    """Return whether an ``Origin`` header points at a loopback host.

    Parses the ``Origin`` URL and inspects its hostname. ``localhost``,
    IPv4/IPv6 loopback addresses (``127.0.0.0/8``, ``::1``) and
    IPv4-mapped loopback (``::ffff:127.0.0.1``) all count as loopback;
    everything else (including a missing or unparseable host) does not.

    :param origin: The raw ``Origin`` header value, e.g.
        ``"http://localhost:8000"`` or ``"https://app.example.com"``.
    :returns: ``True`` when the origin's hostname is a loopback host.
    """
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    mapped_ipv4 = getattr(addr, "ipv4_mapped", None)
    return addr.is_loopback or (mapped_ipv4 is not None and mapped_ipv4.is_loopback)


def parse_allowed_origins() -> frozenset[str]:
    """Read the optional explicit origin allowlist from the environment.

    Reads ``OMNIGENT_WS_ALLOWED_ORIGINS`` (comma-separated). Whitespace
    around each entry is stripped and empty entries are dropped.

    :returns: The set of explicitly allowed origins, e.g.
        ``frozenset({"https://app.example.com"})``; empty when the env
        var is unset or blank.
    """
    raw = os.environ.get(_ALLOWED_ORIGINS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def origin_allowed(
    origin: str | None,
    *,
    local_mode: bool,
    extra_allowed: frozenset[str],
) -> bool:
    """Decide whether a connection's ``Origin`` header is acceptable.

    Protocol-neutral origin policy shared by the WebSocket handshake
    middleware (:class:`WebSocketOriginMiddleware`) and the HTTP
    ``require_trusted_origin`` dependency, so both surfaces enforce one
    trust boundary.

    Policy:

    - The first-party sentinel (:data:`OMNIGENT_INTERNAL_WS_ORIGIN`) and
      any origin in ``extra_allowed`` are always allowed.
    - A missing ``Origin`` is allowed: non-browser clients never send one,
      and browsers always do (the header is on the forbidden-header list,
      so page JS cannot strip or forge it), so its absence is not a
      browser CSRF / CSWSH vector.
    - In ``local_mode`` an ``Origin`` is allowed only when its hostname is
      a loopback host — this is the CSRF / CSWSH guard for the
      unauthenticated single-user local server.
    - In non-local modes the connection is authenticated by cookie / proxy
      header, so any ``Origin`` is allowed unless ``extra_allowed`` is
      non-empty, in which case only the allowlist (matched above) passes.

    :param origin: The connection's ``Origin`` header, or ``None`` when the
        client sent none, e.g. ``"http://localhost:8000"``.
    :param local_mode: Whether the server is a single-user local runtime
        (``OMNIGENT_LOCAL_SINGLE_USER`` truthy).
    :param extra_allowed: Explicitly allowlisted origins from
        :func:`parse_allowed_origins`.
    :returns: ``True`` when the handshake may proceed.
    """
    if origin == OMNIGENT_INTERNAL_WS_ORIGIN:
        return True
    if origin is not None and origin in extra_allowed:
        return True
    if origin is None:
        return True
    if local_mode:
        return origin_hostname_is_loopback(origin)
    # Non-local modes rely on cookie / proxy auth. Passthrough by default;
    # if a deployment configured an allowlist, anything not matched above
    # is denied.
    return not extra_allowed


def _origin_from_scope(scope: Scope) -> str | None:
    """Extract the ``Origin`` header from an ASGI connection scope.

    ASGI lowercases header names, so a byte-string match on ``b"origin"``
    is sufficient.

    :param scope: ASGI connection scope, e.g. one with
        ``type == "websocket"``.
    :returns: The decoded ``Origin`` value, or ``None`` when absent.
    """
    # Annotate the ASGI headers explicitly: ``Scope`` values are typed
    # ``Any``, so without this mypy infers ``value`` as ``Any`` and flags
    # the ``value.decode(...)`` return as an Any-return.
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", [])
    for key, value in headers:
        if key == b"origin":
            return value.decode("latin-1")
    return None


class WebSocketOriginMiddleware:
    """ASGI middleware enforcing the WebSocket ``Origin`` policy.

    Wraps the downstream app and, for ``websocket``-typed scopes only,
    rejects handshakes whose ``Origin`` is not permitted by
    :func:`origin_allowed` — closing the connection before it
    reaches a route handler (and thus before any ``websocket.accept()``).
    Non-WebSocket scopes and permitted handshakes pass through untouched.

    The server mode (``local_mode``) and the allowlist are read per
    connection from the environment, so behavior tracks the runtime
    configuration rather than being frozen at construction time.

    :param app: Downstream ASGI app.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Initialize the middleware.

        :param app: Downstream ASGI app.
        :returns: None.
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the origin policy for WebSocket handshakes.

        :param scope: ASGI connection scope, e.g. type ``"websocket"``.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        :returns: None.
        """
        if scope["type"] != "websocket":
            await self._app(scope, receive, send)
            return

        origin = _origin_from_scope(scope)
        if origin_allowed(
            origin,
            local_mode=local_single_user_enabled(),
            extra_allowed=parse_allowed_origins(),
        ):
            await self._app(scope, receive, send)
            return

        # Reject before the route runs: consume the initial
        # ``websocket.connect`` then close without accepting. The client
        # observes a failed handshake (close code, never an open socket).
        await receive()
        await send(
            {
                "type": "websocket.close",
                "code": FORBIDDEN_ORIGIN_CLOSE_CODE,
                "reason": "forbidden origin",
            }
        )
        _logger.warning("Rejected WebSocket handshake: forbidden origin %r", origin)
