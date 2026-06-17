"""Content-Type preconditions for JSON-bodied POST routes.

Several session POST handlers parse their request body with Starlette's
``await request.json()``, which decodes the body as JSON regardless of
the declared ``Content-Type``. That makes them reachable by a cross-site
*simple* request (one that carries ``Content-Type: text/plain`` so the
browser skips the CORS preflight) whose plain-text payload is in fact
valid JSON — a CSRF vector, since the handler happily processes it.

The dependencies here close that gap by requiring an explicit JSON
``Content-Type`` (or, for the bundled-create route, ``multipart/form-data``)
*before* the handler runs. They inspect only :attr:`Request.headers` and
never touch the body, so the handler's existing ``await request.json()``
and the verbatim runner pass-throughs keep working unchanged.

These are FastAPI dependencies (attached via ``dependencies=[Depends(...)]``
on the route decorator) rather than per-handler inline checks so the rule
lives in exactly one place and reads identically across every route.
"""

from __future__ import annotations

from fastapi import HTTPException, Request


def _request_media_type(request: Request) -> str:
    """Return the lowercased media type of the request's Content-Type.

    Strips any parameters (e.g. ``; charset=utf-8``) and surrounding
    whitespace, yielding just the ``type/subtype`` token. Returns the
    empty string when the header is absent, so a missing Content-Type
    is treated as "no acceptable type" by the callers below.

    :param request: The incoming FastAPI request.
    :returns: The lowercased media type, e.g. ``"application/json"``,
        or ``""`` when no ``Content-Type`` header is present.
    """
    raw = request.headers.get("content-type")
    if raw is None:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def _is_json_media_type(media_type: str) -> bool:
    """Return whether a media type denotes JSON.

    Accepts the canonical ``application/json`` as well as any structured
    JSON suffix type ``application/<subtype>+json`` (RFC 6839), e.g.
    ``application/ld+json`` or ``application/vnd.api+json``.

    :param media_type: An already-normalized (lowercased, parameter-stripped)
        media type, e.g. ``"application/json"`` or ``"text/plain"``.
    :returns: ``True`` if the type is JSON, ``False`` otherwise.
    """
    if media_type == "application/json":
        return True
    return media_type.startswith("application/") and media_type.endswith("+json")


def require_json_content_type(request: Request) -> None:
    """Reject any request that does not declare a JSON ``Content-Type``.

    Use as a FastAPI dependency on POST routes whose body is parsed with
    ``await request.json()``. Accepts ``application/json`` and
    ``application/<subtype>+json``; rejects a missing header and every
    other media type (``text/plain``, form-encoded, multipart,
    octet-stream, ...) with ``415 Unsupported Media Type``.

    Only the request headers are inspected — the body is never read — so
    the downstream handler's own body parsing is unaffected.

    :param request: The incoming FastAPI request.
    :raises HTTPException: ``415`` when the ``Content-Type`` is missing or
        is not a JSON media type.
    """
    media_type = _request_media_type(request)
    if _is_json_media_type(media_type):
        return
    raise HTTPException(
        status_code=415,
        detail=(
            "Unsupported Media Type: this endpoint requires a JSON request "
            "body with Content-Type 'application/json' (or 'application/*+json')."
        ),
    )


def require_json_or_multipart_content_type(request: Request) -> None:
    """Reject requests that are neither JSON nor ``multipart/form-data``.

    The bundled-create variant of :func:`require_json_content_type`, for
    the one route (``POST /v1/sessions``) that legitimately accepts both a
    JSON body and a ``multipart/form-data`` upload (the bundled-create
    form path). Accepts ``application/json`` /
    ``application/<subtype>+json`` and ``multipart/form-data``; rejects a
    missing header and every other media type (``text/plain``,
    form-encoded, octet-stream, ...) with ``415`` *before* the handler's
    content-type dispatch runs.

    Only the request headers are inspected — the body is never read.

    :param request: The incoming FastAPI request.
    :raises HTTPException: ``415`` when the ``Content-Type`` is missing or
        is neither a JSON media type nor ``multipart/form-data``.
    """
    media_type = _request_media_type(request)
    if _is_json_media_type(media_type) or media_type == "multipart/form-data":
        return
    raise HTTPException(
        status_code=415,
        detail=(
            "Unsupported Media Type: this endpoint requires Content-Type "
            "'application/json' (or 'application/*+json') for a JSON body, or "
            "'multipart/form-data' for a bundled create."
        ),
    )
