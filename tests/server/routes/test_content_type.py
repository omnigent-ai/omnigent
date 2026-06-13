"""
Unit tests for the JSON Content-Type guard dependencies.

Covers :func:`require_json_content_type` and
:func:`require_json_or_multipart_content_type` from
``omnigent.server.routes._content_type`` across the full matrix of
Content-Type values: canonical JSON, the ``application/*+json``
structured suffix, charset parameters, case insensitivity, a missing
header, and the non-JSON / multipart types that must (or must not) be
rejected.

The dependencies only read ``request.headers``, so each case is driven
by a real :class:`starlette.requests.Request` built from a minimal ASGI
scope carrying just the header under test — no body, transport, or mock
is involved.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from omnigent.server.routes._content_type import (
    require_json_content_type,
    require_json_or_multipart_content_type,
)


def _request_with_content_type(content_type: str | None) -> Request:
    """
    Build a real Starlette ``Request`` carrying (or omitting) a Content-Type.

    :param content_type: The ``Content-Type`` header value to set, e.g.
        ``"application/json"``. ``None`` omits the header entirely,
        modelling a request with no declared content type.
    :returns: A real :class:`starlette.requests.Request` whose only
        meaningful state is its header set.
    """
    raw_headers: list[tuple[bytes, bytes]] = []
    if content_type is not None:
        raw_headers.append((b"content-type", content_type.encode("latin-1")))
    return Request({"type": "http", "method": "POST", "headers": raw_headers})


# Content-Type values both dependencies must accept as JSON: canonical,
# charset parameter, structured +json suffix, and an uppercased variant
# (the media type compare is case-insensitive).
_JSON_ACCEPTED = [
    "application/json",
    "application/json; charset=utf-8",
    "application/ld+json",
    "application/vnd.api+json",
    "APPLICATION/JSON",
    "Application/JSON; charset=UTF-8",
]

# Content-Type values that are neither JSON nor multipart — both
# dependencies must reject these with 415. ``None`` is the missing-header
# case; "" is a present-but-empty header.
_NON_JSON_NON_MULTIPART = [
    None,
    "",
    "text/plain",
    "text/plain; charset=utf-8",
    "application/x-www-form-urlencoded",
    "application/octet-stream",
    "text/json",  # not application/* — must NOT be treated as JSON
]

# Multipart variants: rejected by the json-only dependency, accepted by
# the json-or-multipart dependency.
_MULTIPART = [
    "multipart/form-data",
    "multipart/form-data; boundary=----WebKitFormBoundaryabc123",
]


@pytest.mark.parametrize("content_type", _JSON_ACCEPTED)
def test_require_json_accepts_json_media_types(content_type: str) -> None:
    """
    ``require_json_content_type`` returns ``None`` for every JSON type.

    Accepting these proves the guard does not regress the legitimate JSON
    clients: a raise here would turn a real ``application/json`` (or
    ``application/ld+json``, or charset-suffixed, or uppercased) request
    into a spurious 415.
    """
    # Returns None (no exception) → the request is allowed through to the handler.
    assert require_json_content_type(_request_with_content_type(content_type)) is None


@pytest.mark.parametrize("content_type", _NON_JSON_NON_MULTIPART + _MULTIPART)
def test_require_json_rejects_non_json(content_type: str | None) -> None:
    """
    ``require_json_content_type`` raises 415 for non-JSON types.

    This is the CSRF guard itself: a missing header, ``text/plain`` (the
    cross-site simple-request vector), form/octet types, and even
    ``multipart/form-data`` must all be rejected by the json-only
    dependency. If any of these did NOT raise, a cross-site request could
    smuggle a JSON body past ``request.json()``.
    """
    with pytest.raises(HTTPException) as exc_info:
        require_json_content_type(_request_with_content_type(content_type))
    # 415 Unsupported Media Type is the documented rejection status; any
    # other code (or no raise) would mean the guard let a non-JSON type in.
    assert exc_info.value.status_code == 415


@pytest.mark.parametrize("content_type", _JSON_ACCEPTED + _MULTIPART)
def test_require_json_or_multipart_accepts_json_and_multipart(content_type: str) -> None:
    """
    ``require_json_or_multipart_content_type`` accepts JSON and multipart.

    The bundled-create route legitimately serves both a JSON body and a
    ``multipart/form-data`` upload, so this dependency must let both
    through (including a multipart type carrying a ``boundary`` parameter).
    A raise here would break either the JSON create or the bundled
    multipart create path.
    """
    # Returns None for every JSON type AND every multipart variant.
    assert require_json_or_multipart_content_type(_request_with_content_type(content_type)) is None


@pytest.mark.parametrize("content_type", _NON_JSON_NON_MULTIPART)
def test_require_json_or_multipart_rejects_other_types(content_type: str | None) -> None:
    """
    ``require_json_or_multipart_content_type`` still rejects simple types.

    The multipart-allowing variant must keep rejecting ``text/plain``, a
    missing header, and other non-JSON / non-multipart types with 415 —
    otherwise the bundled-create route would inherit the same CSRF gap the
    guard exists to close.
    """
    with pytest.raises(HTTPException) as exc_info:
        require_json_or_multipart_content_type(_request_with_content_type(content_type))
    assert exc_info.value.status_code == 415
