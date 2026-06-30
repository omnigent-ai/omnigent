"""Origin precondition for POST routes that accept ``multipart/form-data``.

The JSON Content-Type guard in :mod:`omnigent.server.routes._content_type`
closes the simple-request CSRF vector for JSON-bodied routes, but it does
**not** help routes that legitimately accept ``multipart/form-data`` —
that media type is itself CORS-safelisted, so a cross-site ``fetch`` with
a ``FormData`` body reaches the handler with no preflight. For those
routes the only reliable browser-CSRF signal is the ``Origin`` header.

This dependency applies the same policy the WebSocket layer already
enforces (:func:`omnigent.server.ws_origin.origin_allowed`) so HTTP and
WebSocket share exactly one trust boundary:

- a **missing** ``Origin`` is allowed. Modern browsers attach ``Origin``
  to every cross-site (and same-site state-changing) request — it is on
  the forbidden-header list, so page JS cannot strip or forge it — so an
  absent ``Origin`` is not a browser CSRF vector. Allowing it keeps the
  project's first-party non-browser clients (the Python SDK, the runner,
  the REPL) and any older client working without change. Those clients
  may still announce themselves explicitly with the sentinel ``Origin``
  below, but are not required to.
- the first-party sentinel (:data:`OMNIGENT_INTERNAL_WS_ORIGIN`,
  ``"omnigent://internal"``) and any origin in
  ``OMNIGENT_WS_ALLOWED_ORIGINS`` pass;
- in single-user **local mode** (no cookie / proxy auth) a present
  ``Origin`` must be a loopback host — this is where the guard actually
  bites, since that deployment has no other CSRF defense;
- in authenticated (cookie / proxy) modes a present ``Origin`` passes
  unless an explicit allowlist is configured.

Like the content-type guard, this is a FastAPI dependency (attached via
``dependencies=[Depends(...)]``) that inspects only :attr:`Request.headers`
and never reads the body, so the handler's own multipart / JSON parsing is
unaffected.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from omnigent.server.auth import local_single_user_enabled
from omnigent.server.ws_origin import origin_allowed, parse_allowed_origins


def require_trusted_origin(request: Request) -> None:
    """Reject a request whose ``Origin`` header is present but untrusted.

    Use as a FastAPI dependency on state-changing routes that accept
    ``multipart/form-data`` (which the JSON Content-Type guard cannot
    protect, because multipart is CORS-safelisted). The check delegates to
    the shared :func:`origin_allowed` policy, so a request passes when it
    has **no** ``Origin`` (modern browsers always send one, so absence is
    not a browser CSRF vector — this preserves backward compatibility for
    non-browser and older clients), carries the first-party sentinel or an
    allowlisted origin, or (in local single-user mode) carries a loopback
    ``Origin``. A present ``Origin`` that is none of those is rejected.

    First-party non-browser clients (the Python SDK, the runner, the REPL)
    may announce themselves with ``Origin: omnigent://internal``
    (:data:`OMNIGENT_INTERNAL_WS_ORIGIN`), but are not required to.

    Only the request headers are inspected; the body is never read.

    :param request: The incoming FastAPI request.
    :returns: None.
    :raises HTTPException: ``403`` when the ``Origin`` header is present but
        not trusted for the current auth mode.
    """
    origin = request.headers.get("origin")
    # TEMPORARY fail-open on a missing Origin: ``origin_allowed`` permits
    # ``None`` so an absent header passes. This is a backward-compat measure
    # for clients not yet sending an Origin and is intended to be closed
    # soon — first-party clients (SDK, runner, the test harness) already
    # announce themselves with the sentinel Origin. To close it, reject a
    # ``None`` origin here before delegating.
    if origin_allowed(
        origin,
        local_mode=local_single_user_enabled(),
        extra_allowed=parse_allowed_origins(),
    ):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Forbidden: this endpoint requires a trusted Origin header. "
            "It accepts multipart uploads, which are CORS-safelisted, so a "
            "trusted Origin is required to prevent cross-site request forgery."
        ),
    )
