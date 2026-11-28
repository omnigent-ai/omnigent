"""Test doubles for the Azure DevOps REST client.

:class:`RecordingTransport` answers requests by method and path and records every
one. The ``ado_transport`` fixture fails a test at teardown when any request left
``dev.azure.com``, so a test cannot quietly reach ``vssps.dev.azure.com``, the host
that corporate TLS proxies break.

A test module loads the fixture with
``pytest_plugins = ["tests.runner.azure_devops_fixtures"]``. That module also imports
helpers from here first, so this one opts out of assertion rewriting:

PYTEST_DONT_REWRITE
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
import pytest

ALLOWED_HOST = "dev.azure.com"

Handler = Callable[[httpx.Request], httpx.Response]


def request_path(request: httpx.Request) -> str:
    """Return the percent-encoded path of ``request``, without its query string."""
    return request.url.raw_path.split(b"?", 1)[0].decode("ascii")


def request_query(request: httpx.Request) -> list[tuple[str, str]]:
    """Return the decoded query parameters of ``request`` in the order they were sent."""
    return list(request.url.params.multi_items())


def _canned(
    status: int, json: Any, text: str | None, headers: Mapping[str, str] | None
) -> Handler:
    """Build a handler that returns the same response for every request."""

    def handler(_request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text, headers=headers)
        return httpx.Response(status, json=json, headers=headers)

    return handler


class RecordingTransport(httpx.MockTransport):
    """Mock transport that routes by method and path and records every request.

    A route matches the percent-encoded request path exactly, so a test also checks
    the encoding the client produced. A request with no route gets a 404 with a JSON
    error body.

    :ivar requests: Every request received, in order.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], Handler] = {}
        super().__init__(self._handle)

    def route(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        status: int = 200,
        text: str | None = None,
        headers: Mapping[str, str] | None = None,
        handler: Handler | None = None,
    ) -> None:
        """Answer ``method`` requests for ``path`` with a canned response.

        :param path: The percent-encoded path, without the query string.
        :param json: JSON body of the response.
        :param text: Plain-text body, used instead of ``json`` when given.
        :param handler: Builds the response per request, for paging and other
            request-dependent answers. Overrides the canned fields.
        """
        self._routes[(method.upper(), path)] = handler or _canned(status, json, text, headers)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request_path(request)
        handler = self._routes.get((request.method, path))
        if handler is None:
            return httpx.Response(
                404, json={"message": f"no test route for {request.method} {path}"}
            )
        return handler(request)


def assert_only_dev_azure_com(transport: RecordingTransport) -> None:
    """Fail when a recorded request went to a host other than ``dev.azure.com``.

    That includes ``vssps.dev.azure.com``.
    """
    stray = [
        str(request.url) for request in transport.requests if request.url.host != ALLOWED_HOST
    ]
    assert not stray, f"requests left {ALLOWED_HOST}: {stray}"


@pytest.fixture
def ado_transport() -> Iterator[RecordingTransport]:
    """Yield a :class:`RecordingTransport`; fail the test at teardown on a stray host."""
    transport = RecordingTransport()
    yield transport
    assert_only_dev_azure_com(transport)
