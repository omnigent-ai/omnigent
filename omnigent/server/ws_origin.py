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

An allowlist entry's host may start with ``*.`` to trust every
subdomain of a domain in one entry, e.g. ``https://*.ts.net`` for a
Tailscale MagicDNS tailnet (see :func:`_wildcard_entry_suffix`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.process_logging import log_once
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
    around each entry is stripped and empty entries are dropped. An
    entry's host may start with ``*.`` to match every subdomain of a
    domain (see :func:`_origin_matches_wildcard_entry`); entries are
    returned as-is, unparsed, and wildcard expansion happens later at
    match time in :func:`origin_allowed`.

    An entry containing ``*`` that doesn't parse as a valid wildcard (see
    :func:`_wildcard_entry_suffix`) logs a warning — it will never match
    any ``Origin``, and without this, the only symptom is a legitimate
    origin mysteriously getting rejected with no diagnostic pointing at
    the bad entry.

    :returns: The set of explicitly allowed origins, e.g.
        ``frozenset({"https://app.example.com", "https://*.ts.net"})``;
        empty when the env var is unset or blank.
    """
    raw = os.environ.get(_ALLOWED_ORIGINS_ENV, "")
    entries = frozenset(part.strip() for part in raw.split(",") if part.strip())
    for entry in entries:
        if "*" in entry and _wildcard_entry_suffix(entry) is None:
            # This runs on every connection; log once so a persistent
            # misconfiguration doesn't flood the logs.
            log_once(
                _logger,
                logging.WARNING,
                "%s entry %r looks like a wildcard pattern but is malformed "
                "(expected 'scheme://*.<domain>', with '*' as the entire "
                "leftmost label) — it will never match any Origin",
                _ALLOWED_ORIGINS_ENV,
                entry,
            )
    return entries


def _wildcard_entry_suffix(entry: str) -> tuple[str, str, int | None] | None:
    """Parse a ``scheme://*.<domain>[:port]`` allowlist entry.

    :param entry: One raw allowlist entry, e.g. ``"https://*.ts.net"``.
    :returns: ``(scheme, dotted_suffix, port)`` — ``dotted_suffix`` keeps
        its leading ``.`` (e.g. ``".ts.net"``) so a suffix match can never
        cross a label boundary — when ``entry``'s host is exactly a
        ``*.`` leftmost label followed by a non-empty domain, and the
        entry carries nothing beyond ``scheme://*.<domain>[:port]`` (no
        path, query, fragment, or userinfo — those have no meaning for an
        ``Origin``, which is exactly ``scheme://host[:port]``, so any
        entry carrying one is a malformed pattern, not a decorated valid
        one). ``None`` when ``entry`` is not a wildcard entry at all, or
        is one with an ambiguous, decorated, or empty pattern (a bare
        ``*``, ``*.``, a second ``*`` anywhere, a path/query/fragment/
        userinfo, or the wildcard outside the leftmost label) — those
        never match anything rather than risk over-matching.
    """
    try:
        parts = urlsplit(entry)
        port = parts.port  # raises ValueError lazily for an out-of-range port
    except ValueError:
        return None
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        return None
    host = parts.hostname
    if not parts.scheme or host is None or not host.startswith("*."):
        return None
    suffix = host[1:]  # keep the leading "." from "*.<domain>" -> ".<domain>"
    if suffix == "." or "*" in suffix:
        return None
    return parts.scheme, suffix, port


def _origin_matches_wildcard_entry(origin: str, entry: str) -> bool:
    """Check whether ``origin`` matches one ``*.``-prefixed allowlist entry.

    :param origin: The connection's ``Origin`` header value.
    :param entry: One raw allowlist entry, e.g. ``"https://*.ts.net"``.
    :returns: ``True`` when ``entry`` is a valid wildcard entry and
        ``origin`` shares its scheme and port and its hostname ends with
        the entry's domain suffix (at any subdomain depth, but never the
        bare domain itself — the leading ``.`` kept in the suffix rules
        that out).
    """
    parsed_entry = _wildcard_entry_suffix(entry)
    if parsed_entry is None:
        return False
    entry_scheme, suffix, entry_port = parsed_entry
    try:
        parts = urlsplit(origin)
        origin_port = parts.port  # raises ValueError lazily for an out-of-range port
    except ValueError:
        return False
    host = parts.hostname
    if host is None:
        return False
    return parts.scheme == entry_scheme and origin_port == entry_port and host.endswith(suffix)


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

    - The first-party sentinel (:data:`OMNIGENT_INTERNAL_WS_ORIGIN`), any
      origin literally in ``extra_allowed``, and any origin matching a
      ``*.``-prefixed wildcard entry in ``extra_allowed`` (e.g.
      ``https://*.ts.net`` trusting every ``*.ts.net`` subdomain — see
      :func:`_origin_matches_wildcard_entry`) are always allowed. An
      origin containing ``*`` is never admitted via literal equality —
      ``*`` isn't a valid hostname character, so no real browser can ever
      send one; it is resolved purely as a wildcard pattern (valid →
      suffix match, invalid → matches nothing), never as an exact-string
      replay of a configured entry.
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
    if origin is not None and (
        ("*" not in origin and origin in extra_allowed)
        or any(_origin_matches_wildcard_entry(origin, entry) for entry in extra_allowed)
    ):
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
