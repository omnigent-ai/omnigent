"""Per-route gzip for endpoints that inline a whole file in their JSON body.

Most JSON responses are bounded metadata, where compression buys little. The
workspace-file reads are the exception: they return the file's entire contents
in a ``content`` field, so an uncompressed response costs a full file transfer
on every click. Source text compresses 70-300x, which turns a
multi-hundred-millisecond download into a few milliseconds.

This is a route class rather than an app- or router-level middleware so the
route table stays the single source of truth for *which* endpoints compress.
A middleware would have to re-derive that from the request path, duplicating
the router's matching and — because a path alone says nothing about the method
— wrapping the ``PUT``/``PATCH``/``DELETE`` handlers that share these paths.
Starlette's ``Route.handle`` rejects a mismatched method before it reaches
``self.app``, so a route class only ever wraps the methods its route declares.

FastAPI's decorators expose neither Starlette's per-route ``middleware=`` nor a
per-route class, so a custom :class:`~fastapi.routing.APIRoute` on a dedicated
router is the supported equivalent; ``include_router`` preserves it.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Bodies below this gain nothing from gzip once the header overhead and the
# CPU are accounted for. Matches the web-ui mount's threshold.
GZIP_MINIMUM_SIZE = 1024
# Level 4 reaches the same ratio as the default 9 on JSON and source text
# (measured ~330x on a 1 MB source file) for roughly half the CPU.
GZIP_COMPRESSLEVEL = 4
# ``request.state`` key a handler sets to opt its own response out of gzip.
# See :func:`skip_gzip`.
SKIP_GZIP_STATE_KEY = "skip_gzip"


def skip_gzip(request: Request) -> None:
    """
    Opt this response out of gzip, from inside the handler.

    For content that is already entropy-coded — base64 of compressed media
    (PNG/JPEG/PDF), which carries only base64's own ~25% redundancy — gzip
    returns ~1.3x for real event-loop time: 38 ms for a 1 MB file, 385 ms at
    the 10 MiB binary cap. Not worth blocking the loop for.

    The handler is the right place to decide this because it already holds the
    payload. The alternative — recovering the same fact from the serialized
    response bytes — has to re-derive domain information from transport data,
    which brought its own failure modes (a length-bounded prefix scan, and a
    dependency on where the producer places the field).

    Sets a flag on ``request.state``, which is backed by ``scope["state"]``, so
    :class:`GZipFileContentRoute` reads it back at send time. Response body,
    headers, status, and OpenAPI are all unaffected.

    :param request: The active request.
    :returns: None.
    """
    setattr(request.state, SKIP_GZIP_STATE_KEY, True)


def _client_accepts_gzip(accept_encoding: str) -> bool:
    """
    Return whether ``Accept-Encoding`` permits gzip.

    Coding tokens are case-insensitive, and ``q=0`` means "do not use this
    coding" (RFC 9110 §12.5.3) — so a plain substring test would compress for
    a client that explicitly declined.

    :param accept_encoding: Raw header value, e.g. ``"gzip, deflate, br"``.
    :returns: True when gzip is listed with a non-zero quality value.
    """
    for part in accept_encoding.split(","):
        token, _, params = part.strip().partition(";")
        if token.strip().lower() != "gzip":
            continue
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    return float(value.strip()) > 0
                except ValueError:
                    # Malformed q value — treat as unqualified acceptance.
                    return True
        return True
    return False


class _StateAwareGZipResponder(GZipResponder):
    """
    Gzip responder that honours a handler's :func:`skip_gzip` opt-out.

    The flag can only be read at send time: the handler sets it while running,
    which is after ``handle`` dispatches but before the response body is
    emitted. Reuses Starlette's own ``content_type_is_excluded`` opt-out — the
    flag it already sets for ``text/event-stream`` — so an excluded response
    takes the library's untouched pass-through path rather than a parallel one
    here.

    :param app: The wrapped ASGI app (the route's own ``handle``).
    :param minimum_size: Minimum body size to compress, e.g. ``1024``.
    :param compresslevel: gzip level passed through to Starlette.
    :param state: The request's ``scope["state"]`` dict — the same mapping
        ``request.state`` writes to, so the handler's later opt-out is visible
        through this reference.
    """

    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int,
        compresslevel: int,
        state: Mapping[str, object],
    ) -> None:
        super().__init__(app, minimum_size, compresslevel=compresslevel)
        self._state = state

    async def send_with_compression(self, message: Message) -> None:
        """
        Widen Starlette's exclusion set when the handler opted out.

        :param message: The outgoing ASGI message.
        :returns: None.
        """
        if (
            message["type"] == "http.response.body"
            and not self.started
            and self._state.get(SKIP_GZIP_STATE_KEY)
        ):
            # Starlette reads this flag on the first body message, then emits
            # the start message it buffered — untouched.
            self.content_type_is_excluded = True
        await super().send_with_compression(message)


class GZipFileContentRoute(APIRoute):
    """
    Route class that gzips this route's responses.

    Attach by grouping the read endpoints on their own router::

        file_read_router = APIRouter(route_class=GZipFileContentRoute)

        @file_read_router.get(path)
        async def read(...): ...

        router.include_router(file_read_router)

    (FastAPI's decorators do not accept a per-route class; ``route_class`` on
    the router is the supported form, and ``include_router`` preserves it.)

    Skips compression when the client did not ask for gzip, when the request
    carries a ``Range`` (a ``206``'s ``Content-Range`` describes the
    *unencoded* representation, so compressing the body would make the two
    disagree and break media seeking / PDF byte-range loads), and when the
    handler called :func:`skip_gzip`.
    """

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Compress this route's response when the client and handler allow it.

        :param scope: ASGI request scope, e.g. type ``"http"``.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        :returns: None.
        """
        if scope["type"] != "http":
            await super().handle(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not _client_accepts_gzip(headers.get("Accept-Encoding", "")) or "range" in headers:
            await super().handle(scope, receive, send)
            return

        # The base ``handle`` already has the ASGI signature the responder
        # calls, so it can serve as the wrapped app directly.
        responder = _StateAwareGZipResponder(
            super().handle,
            GZIP_MINIMUM_SIZE,
            GZIP_COMPRESSLEVEL,
            scope.setdefault("state", {}),
        )
        await responder(scope, receive, send)


__all__ = [
    "GZIP_COMPRESSLEVEL",
    "GZIP_MINIMUM_SIZE",
    "SKIP_GZIP_STATE_KEY",
    "GZipFileContentRoute",
    "skip_gzip",
]
