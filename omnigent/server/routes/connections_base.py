"""Provider-agnostic connection routes: the shared OAuth connect / callback /
status / disconnect flow for per-user integrations (GitHub, Databricks, ...).

A provider supplies a :class:`ConnectionHooks` with only the bits that differ —
where to send the user (``begin``), how to exchange the code and persist the
result (``complete``), and which non-secret fields to surface (``status_fields``)
— and this factory owns everything shared: signed, user-bound, short-lived
``state`` (CSRF + replay protection, rebound to the authenticated caller on
callback), same-origin ``return_to`` sanitising, and the four endpoints under
``/connections/{provider}/``. Adding a provider is a hooks object plus a
registry entry (see :mod:`omnigent.server.connections_registry`), not another
copy of this flow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user

_logger = logging.getLogger(__name__)

# The OAuth state JWT only has to survive the user's round trip to the
# provider's consent screen, so it is deliberately short-lived.
_STATE_TTL_S = 600
_STATE_ALG = "HS256"

# Fallback landing after connect/disconnect when no (safe) return_to survives.
_DEFAULT_RETURN_TO = "/settings"


class ConnectionError(Exception):
    """A provider hook failed in a way that should land on the graceful
    ``?<provider>=error`` redirect rather than surface a raw 500."""


@dataclass
class ConnectStart:
    """What a provider needs to begin authorization: the fully-built authorize
    URL (already carrying the signed state) to redirect the user to."""

    authorize_url: str


class ConnectionHooks(Protocol):
    """The provider-specific half of a connection flow. Everything else — state
    signing, CSRF/replay binding, redirect sanitising, the endpoints — is shared
    in :func:`create_connection_router`."""

    #: URL segment + status marker, e.g. ``"github"`` / ``"databricks"``.
    provider: str
    #: Connection store exposing ``get(user_id)`` and ``delete(user_id)``.
    store: Any

    def signing_key(self) -> bytes | str:
        """The HMAC key the ``state`` JWT is signed/verified with."""

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        """Non-secret, provider-specific ``status`` fields (never tokens)."""

    def begin(self, request: Request, build_state: Any) -> ConnectStart | None:
        """Start authorization. ``build_state(extra_claims)`` returns a signed
        state string carrying ``extra_claims`` alongside the shared ones. Return
        ``None`` on bad input (e.g. a missing/invalid workspace)."""

    async def complete(self, user_id: str, code: str, claims: dict[str, Any]) -> None:
        """Exchange *code* and persist the connection for *user_id*. Raise
        :class:`ConnectionError` on any exchange/identity/store failure."""


def sanitize_return_to(raw: str | None) -> str:
    """Clamp a caller-supplied return path to a safe same-origin path.

    Only relative paths beginning with a single ``/`` are accepted, so a
    redirect can never be pointed at an external origin (``//evil.com``,
    ``https://evil.com``). A leading ``/`` alone is not sufficient: a browser
    reads ``/\\evil.com`` (backslash normalized to ``/``) and ``/%09//evil.com``
    (control char stripped) as protocol-relative, so reject any backslash or
    control/whitespace char outright.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return _DEFAULT_RETURN_TO
    if "\\" in raw or any(ord(c) < 0x20 or c == "\x7f" for c in raw):
        return _DEFAULT_RETURN_TO
    return raw


def redirect_with_status(provider: str, return_to: str, status: str) -> RedirectResponse:
    """Redirect back to *return_to* with a ``?<provider>=<status>`` marker."""
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(
        url=f"{return_to}{sep}{urlencode({provider: status})}", status_code=302
    )


def create_connection_router(
    hooks: ConnectionHooks,
    *,
    auth_provider: AuthProvider | None = None,
    extra_routes: Any = None,
) -> APIRouter:
    """Build the shared ``/connections/{provider}/*`` router for *hooks*.

    :param hooks: The provider adapter (state key, begin, complete, status).
    :param auth_provider: Identity resolution, or ``None`` (single-user/local).
    :param extra_routes: Optional ``(router) -> None`` to mount provider-only
        endpoints (e.g. GitHub's repo/branch listing) on the same router.
    :returns: A FastAPI router with status / connect / callback / disconnect.
    """
    provider = hooks.provider
    store = hooks.store
    router = APIRouter()
    key = hooks.signing_key()

    def _current_user(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    def _verify_state(state: str) -> dict:
        return jwt.decode(state, key, algorithms=[_STATE_ALG])

    @router.get(f"/connections/{provider}/status")
    async def status(request: Request) -> dict[str, object]:
        """Return the caller's connection status. Never surfaces tokens."""
        user_id = _current_user(request)
        connection = await asyncio.to_thread(store.get, user_id)
        return {
            "enabled": True,
            "connected": connection is not None,
            "connected_at": connection.created_at if connection is not None else None,
            **hooks.status_fields(connection),
        }

    @router.get(f"/connections/{provider}/connect")
    async def connect(request: Request, return_to: str | None = None) -> RedirectResponse:
        """Redirect the user into the provider's authorization flow."""
        user_id = _current_user(request)
        clamped = sanitize_return_to(return_to)

        def build_state(extra_claims: dict[str, Any]) -> str:
            payload = {
                "sub": user_id,
                "return_to": clamped,
                "exp": int(time.time()) + _STATE_TTL_S,
                **extra_claims,
            }
            return jwt.encode(payload, key, algorithm=_STATE_ALG)

        start = hooks.begin(request, build_state)
        if start is None:
            return redirect_with_status(provider, clamped, "error")
        return RedirectResponse(url=start.authorize_url, status_code=302)

    @router.get(f"/connections/{provider}/callback")
    async def callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> RedirectResponse:
        """Handle the provider redirect: validate state, exchange, persist.

        The signed state is rebound to the authenticated caller so a callback
        can't be replayed or cross-bound to another user. Redirects back to the
        state's ``return_to`` with a ``?<provider>=connected|error`` marker.
        """
        user_id = _current_user(request)
        if not code or not state:
            return redirect_with_status(provider, _DEFAULT_RETURN_TO, "error")
        try:
            claims = _verify_state(state)
        except jwt.PyJWTError:
            _logger.warning("%s callback with invalid state", provider)
            return redirect_with_status(provider, _DEFAULT_RETURN_TO, "error")
        return_to = sanitize_return_to(claims.get("return_to"))
        if claims.get("sub") != user_id:
            _logger.warning("%s callback state/user mismatch", provider)
            return redirect_with_status(provider, return_to, "error")
        try:
            await hooks.complete(user_id, code, claims)
        except ConnectionError as exc:
            _logger.warning("%s connect failed for %s: %s", provider, user_id, exc)
            return redirect_with_status(provider, return_to, "error")
        return redirect_with_status(provider, return_to, "connected")

    @router.post(f"/connections/{provider}/disconnect")
    async def disconnect(request: Request) -> dict[str, bool]:
        """Remove the caller's connection."""
        user_id = _current_user(request)
        removed = await asyncio.to_thread(store.delete, user_id)
        return {"disconnected": removed}

    if extra_routes is not None:
        extra_routes(router)
    return router
