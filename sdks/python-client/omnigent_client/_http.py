"""HTTP transport helpers shared by the client and its callers."""

from __future__ import annotations

import ipaddress
from urllib.parse import quote, urlsplit


def is_loopback_url(url: str) -> bool:
    """
    Report whether *url* addresses this machine's loopback interface.

    Loopback traffic must never go through an HTTP proxy: the proxy
    resolves ``127.0.0.1`` against itself, so the request reaches the
    wrong host or is refused outright. httpx reads proxy settings from
    the environment by default, and on Windows that also covers the
    system registry — so a machine with any proxy configured cannot
    reach its own local server unless the caller passes
    ``trust_env=False``.

    :param url: Absolute URL to classify, e.g.
        ``"http://127.0.0.1:6767"``.
    :returns: ``True`` for loopback targets — ``localhost``, any
        ``.localhost`` name, ``127.0.0.0/8``, or ``::1``. ``False``
        otherwise, including for a URL with no host.
    """
    host = urlsplit(url).hostname
    if host is None:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def session_url(base_url: str, session_id: str, *subpath: str) -> str:
    """
    Build ``{base_url}/v1/sessions/{session_id}[/segment...]``.

    Every segment must be one opaque path component. Empty strings,
    ``.``, ``..`` and anything containing ``/`` are rejected before a
    request is built: the server decodes the path before matching
    routes, so percent-encoding alone cannot stop an id such as
    ``"conv_a/permissions/bob"`` from selecting a different endpoint,
    and HTTP clients collapse dot segments onto the parent route. Other
    reserved characters are percent-encoded so they stay inside the
    segment (a ``?`` cannot start a query string).

    :param base_url: Server base URL without a trailing slash, e.g.
        ``"http://localhost:8000"``.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param subpath: Further segments after the id, e.g. ``"items"`` or
        a caller-supplied elicitation id.
    :returns: The absolute URL, e.g.
        ``"http://localhost:8000/v1/sessions/conv_abc123/items"``.
    :raises ValueError: If any segment is not a single path component.
    """
    segments = (session_id, *subpath)
    for segment in segments:
        if not segment or segment in (".", "..") or "/" in segment:
            raise ValueError(f"invalid session URL segment {segment!r}")
    encoded = (quote(segment, safe="") for segment in segments)
    return "/".join((f"{base_url}/v1/sessions", *encoded))
